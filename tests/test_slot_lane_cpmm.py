"""Raydium CPMM on the slot lane, SHADOW only (2026-09-27, lever 1; design doc section 6).

A routed buy whose own logs frame carried the CPMM swap into its coin, from a SOL- or USDC-quoted
pool, is built natively on the lane and timed. It is never reserved, locked or sent, whatever the
lane's own mode: MEMECOIN_SLOT_LANE_CPMM has no live mode yet. Events are real census swaps
(tests/fixtures/memecoin/raydium_cpmm_frames.json).
"""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import raydium_cpmm_buy as C
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "raydium_cpmm_frames.json").read_text())
EVENTS = [C.parse_swap_event(base64.b64decode(e["event_b64"])) for e in FX["events"]]
SOL_EV = next(e for e in EVENTS if e.input_mint == C.WSOL_MINT)
USDC_EV = next(e for e in EVENTS if e.input_mint == C.USDC_MINT)
EXOTIC_EV = next(e for e in EVENTS if e.input_mint not in C.SUPPORTED_QUOTES)
BLOCKHASH = "11111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _fresh_epoch_cache(monkeypatch):
    """The epoch cache is process-wide: each test starts with none cached and none tried."""
    monkeypatch.setitem(C._EPOCH, "value", None)
    monkeypatch.setitem(C._EPOCH, "tried", None)


def _no_pool_reads(method, params=None):
    """A pool cache rpc for the lane thread: no pool read. The epoch read belongs to learn_pending
    (the prefetch thread) and answers ANTHROPIC's epoch at the 2026-09-28 read."""
    if method == "getEpochInfo":
        return {"epoch": 1044, "slotIndex": 0, "slotsInEpoch": 432000}
    pytest.fail("the lane thread must not read the network")


def _cache(*events):
    cache = C.CpmmPoolCache(rpc=_no_pool_reads)
    for ev in events:
        state = C.parse_pool_state(base64.b64decode(FX["pool_states_b64"][ev.pool]))
        coin_dec = state.decimals_1 if ev.output_mint == state.mint_1 else state.decimals_0
        bps = round(10_000 * ev.output_transfer_fee / ev.output_amount) if ev.output_transfer_fee else 0
        cache._facts[ev.pool] = {"state": state, "config": C.AmmConfig(*FX["config_rates"][state.amm_config]),
                                 "mints": {ev.output_mint: (10**15, coin_dec, bps, 2**64 - 1),
                                           ev.input_mint: (10**18, 9, 0, 0)}}
    return cache


def _routed(ev, mint=None, events=None):
    return S.RoutedBuy(source_user_id="u1", handle="alice", owner="Owner1111", signature="SIG",
                       slot=100, mint=mint or ev.output_mint, token_delta_atomic=ev.output_amount,
                       token_decimals=6, usdc_spent_atomic=1, recv_monotonic=1.0, recv_utc="t",
                       paired_monotonic=1.0, venue_events=tuple(events if events is not None else (ev,)))


def _snap():
    return SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH, last_valid_height=1)


def _leg(clip_usd, lamports=1_000_000_000, clock=lambda: 50.0):
    from sniper.execution.pump_curve_buy import clip_atomic
    return {"ixs": [], "tables": [], "usdc_atomic": clip_atomic(clip_usd),
            "min_out_lamports": lamports, "fetched_at": clock(), "quote": {}}


# --- conversion --------------------------------------------------------------------------------

def test_a_routed_buy_with_its_own_cpmm_swap_becomes_a_cpmm_slot_buy():
    buy = L.cpmm_slot_buy(_routed(SOL_EV))
    assert buy.venue == "raydium_cpmm" and buy.venue_event is SOL_EV
    assert buy.mint == SOL_EV.output_mint and buy.pool == SOL_EV.pool
    assert buy.recv_monotonic == 1.0 and buy.signature == "SIG"


@pytest.mark.parametrize("routed", [
    _routed(SOL_EV, mint="OtherMint111"),                 # the swap is not into the routed coin
    _routed(SOL_EV, events=(SOL_EV, SOL_EV)),             # two swaps into the coin: not one buy
    _routed(SOL_EV, events=()),                           # no CPMM swap in the frame
])
def test_anything_else_is_not_a_cpmm_buy(routed):
    assert L.cpmm_slot_buy(routed) is None


def test_an_exotic_quote_cpmm_buy_is_converted_and_refused_by_name_on_the_real_frame():
    """ResellCalendar bought AjxqTiS6 three times through an ANTHROPIC-quoted CPMM pool (2026-09-27
    22:58-23:13Z). The lane decoded nothing visible: cpmm_slot_buy dropped the unsupported quote
    before any line. Now it converts, admission refuses by name, and the shadow line says which."""
    frame = FX["exotic_quote_frame"]
    swaps = S._cpmm_swaps({"params": {"result": {"value": {"logs": frame["logMessages"]}}}}, True)
    assert len(swaps) == 1 and swaps[0].input_mint.startswith("Pren1F")
    ev = swaps[0]
    buy = L.cpmm_slot_buy(_routed(ev))
    assert buy is not None and buy.venue_event is ev and buy.mint == ev.output_mint
    reason, _ = C.market_facts(buy, C.CpmmPoolCache(rpc=None), 122.5)
    assert reason == "slot_lane_cpmm_quote_not_supported"
    line = C.shadow_line(buy, {"reason": reason}, None)
    assert "-> slot_lane_cpmm_quote_not_supported" in line and "quote=Pren1FvF" in line


def test_the_pairer_carries_the_frames_swaps_onto_the_routed_buy():
    pairer = S.RoutedPairer(wait_s=5)
    pairer.watch("O", "u1", "alice")
    for kind, _program in S.ROUTED_PROGRAMS:
        pairer.set_baseline("O", kind, 10, {"usdcAcct": 5_000_000, "coinAcct": 0})
    pairer.on_token("O", S.ROUTED_PROGRAMS[0][0], 11, "usdcAcct", S.USDC_MINT, 1_000_000, 6,
                    recv_monotonic=1.0, recv_utc="t")
    pairer.on_token("O", S.ROUTED_PROGRAMS[0][0], 11, "coinAcct", SOL_EV.output_mint, 777, 6,
                    recv_monotonic=1.0, recv_utc="t")
    out = pairer.on_logs("O", 11, "SIG", lane=False, recv_monotonic=1.1, recv_utc="t",
                         venue_events=(SOL_EV,))
    assert len(out) == 1 and out[0].venue_events == (SOL_EV,)


# --- the socket-side decode ----------------------------------------------------------------------

def _frame(logs):
    return {"params": {"result": {"context": {"slot": 1}, "value": {"logs": logs}}}}


def test_the_socket_decodes_nothing_when_off_and_never_raises():
    frame = FX["frames"][1]
    assert S._cpmm_swaps(_frame(frame["logMessages"]), False) == ()
    assert len(S._cpmm_swaps(_frame(frame["logMessages"]), True)) >= 1
    assert S._cpmm_swaps({"params": None}, True) == ()


@pytest.mark.parametrize("value,mode", [("off", "off"), ("shadow", "shadow"), ("live", "live"),
                                        ("bogus", "off"), ("", "off")])
def test_the_lane_modes_include_a_real_live(monkeypatch, value, mode):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM", value, raising=False)
    assert C.lane_mode() == mode


# --- market facts and the build ------------------------------------------------------------------

def test_market_facts_price_from_the_post_trade_reserves():
    reason, facts = C.market_facts(L.cpmm_slot_buy(_routed(SOL_EV)), _cache(SOL_EV), 122.5)
    assert reason is None and facts["mint"] == SOL_EV.output_mint and facts["pool"] == SOL_EV.pool
    assert facts["price_usd"] > 0 and facts["mcap_usd"] > 0 and facts["their_usd"] > 0


def _parsed_mint(*exts, decimals=6):
    return {"data": {"parsed": {"info": {"supply": "1000000", "decimals": decimals, "extensions": [
        {"extension": name, "state": state} for name, state in exts]}}}}


def test_a_cpmm_coin_with_an_uncovered_extension_refuses_and_names_it():
    """(i) of the CPMM live blockers: the same allowlist as LaunchLab (one shared function), and the
    refusal names the extension so the shadow read counts refusals by extension."""
    from sniper.execution import raydium_launchlab_buy as LL
    assert LL.coin_facts is C.coin_facts and LL.coin_refusal is C.coin_refusal
    hooked = _parsed_mint(("metadataPointer", {}), ("transferHook", {"programId": "Hook1111"}))
    assert C.coin_refusal(hooked) == "extension_transferHook" and C.coin_facts(hooked) is None
    assert C.coin_refusal(_parsed_mint(("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 300, "maximumFee": 9},
                                                              "olderTransferFee": {"transferFeeBasisPoints": 300, "maximumFee": 9}}))) is None
    cache = _cache(SOL_EV)
    cache._facts[SOL_EV.pool]["refusals"] = {SOL_EV.output_mint: "extension_transferHook"}
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(SOL_EV)), cache, 122.5)
    assert reason == "slot_lane_cpmm_coin_extension_transferHook"


def test_the_cpmm_pool_cache_records_each_mints_refusal():
    state_b64 = FX["pool_states_b64"][SOL_EV.pool]
    st = C.parse_pool_state(base64.b64decode(state_b64))
    coin_is_0 = SOL_EV.output_mint == st.mint_0

    def rpc(method, params):
        if method == "getAccountInfo" and params[0] == SOL_EV.pool:
            return {"value": {"owner": C.CPMM_PROGRAM, "data": [state_b64, "base64"]}}
        if method == "getAccountInfo":
            return {"value": {"data": [FX["config_raw_b64"][st.amm_config], "base64"]}}
        coin = _parsed_mint(("permanentDelegate", {"delegate": "Issuer1"}))
        quote = _parsed_mint(decimals=9)
        return {"value": [coin, quote] if coin_is_0 else [quote, coin]}
    facts = C.CpmmPoolCache(rpc=rpc).learn(SOL_EV.pool)
    assert isinstance(facts["quote_read_at"], float)
    assert facts["refusals"][SOL_EV.output_mint] == "extension_permanentDelegate"
    assert facts["refusals"][SOL_EV.input_mint] is None
    assert C.quote_facts(facts["mint_values"][SOL_EV.input_mint]) == (9, 0, 0)   # the on-demand leg's input


def test_an_unknown_pool_refuses_and_is_queued_for_the_prefetch_thread():
    cache = C.CpmmPoolCache(rpc=None)
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(SOL_EV)), cache, 122.5)
    assert reason == "pool_unknown" and cache._pending == [SOL_EV.pool]


def test_a_wsol_pool_builds_on_the_prefetched_leg_and_prices_the_min_out_under_expected():
    b = C.SlotLaneCpmmBuilder(user="3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6", rpc=None,
                              leg_fn=lambda: _leg(100.0), clock=lambda: 50.0)
    b.pools = _cache(SOL_EV)
    msg, detail = b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap())
    assert msg is not None, detail
    assert 0 < detail["min_tokens_out"] <= detail["expected_tokens_out"]
    assert detail["target_mint"] == SOL_EV.output_mint and detail["route_target"].endswith(SOL_EV.pool)
    programs = {str(msg.account_keys[ix.program_id_index]) for ix in msg.instructions}
    assert C.CPMM_PROGRAM in programs


def test_a_usdc_pool_needs_no_leg():
    b = C.SlotLaneCpmmBuilder(user="3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6", rpc=None,
                              leg_fn=lambda: pytest.fail("a USDC pool must not read the SOL leg"))
    b.pools = _cache(USDC_EV)
    msg, detail = b.build(L.cpmm_slot_buy(_routed(USDC_EV)), 20.0, _snap())
    assert msg is not None, detail
    assert b.last_build["spend_lamports"] == 20_000_000       # the clip, in USDC atomic


@pytest.mark.parametrize("leg,reason", [(None, "leg_stale"), (_leg(100.0, clock=lambda: 0.0), "leg_stale"),
                                        (_leg(200.0), "leg_over_clip")])
def test_a_stale_or_mismatched_leg_refuses(leg, reason):
    b = C.SlotLaneCpmmBuilder(user="3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6", rpc=None,
                              leg_fn=lambda: leg, clock=lambda: 50.0)
    b.pools = _cache(SOL_EV)
    assert b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap()) == (None, reason)


# --- SHADOW CAN NEVER REACH A SEND ---------------------------------------------------------------

def test_a_cpmm_buy_on_a_live_lane_is_built_and_never_reserved_locked_or_sent(monkeypatch):
    import sniper.execution.exec_lock as lock
    import sniper.services.memecoin_risk as risk
    monkeypatch.setattr(lock, "wallet_exec_lock", lambda *a, **k: pytest.fail("wallet lock taken"))
    monkeypatch.setattr(risk, "reserve_entry", lambda *a, **k: pytest.fail("reservation written"))
    monkeypatch.setattr(L, "admission_refusal", lambda buy, **k: (None, {"mint": buy.mint,
                                                                         "price_usd": 1.0,
                                                                         "mcap_usd": 1.0,
                                                                         "their_usd": 1.0}))
    built = []

    class Clock:
        @staticmethod
        def perf_counter():
            return 0.0

        @staticmethod
        def monotonic():
            return 1.0

    lane = L.SlotLane(
        conn=None, executor={"slot_execute_fn": lambda *a, **k: pytest.fail("SENT")},
        build_fn=lambda buy, clip, snap, **k: built.append(buy.venue) or ("MSG", {"expected_tokens_out": 9,
                                                                                  "min_tokens_out": 7}),
        snapshot_fn=lambda: SimpleNamespace(sol_usd=1.0, usdc_balance=1.0, sol_balance=1.0,
                                            blockhash=BLOCKHASH, observed_mono={}),
        armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=lambda: "t",
        lock_key="sol-x", mode="live", shadow_clip_fn=lambda: 100.0, clock=Clock,
        cpmm_pools=_cache(SOL_EV))
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    result = lane.handle(L.cpmm_slot_buy(_routed(SOL_EV)))
    assert result["status"] == "shadow" and result["reason"] == "built exp=9 min=7"
    assert built == ["raydium_cpmm"]
    assert result["expected_tokens_out"] == 9 and result["min_tokens_out"] == 7
    assert result["signal_age_ms"] == 0.0              # recv 1.0, decision 1.0 on this clock


def test_the_lane_refuses_a_cpmm_buy_it_has_no_pool_cache_for():
    reason, _ = L.admission_refusal(L.cpmm_slot_buy(_routed(SOL_EV)), armed_ids={"u1"},
                                    sol_usd=1.0, kill_tripped=False)
    assert reason == "slot_lane_venue_raydium_cpmm"


# --- the shadow read: fresh reserves and the per-event line -------------------------------------

def _vault(amount):
    return base64.b64encode(b"\0" * 64 + int(amount).to_bytes(8, "little") + b"\0" * 93).decode()


def _fresh_rpc(pool_b64, v0, v1, slot=777):
    def rpc(method, params):
        keys = params[0]
        if len(keys) == 1:
            return {"context": {"slot": slot}, "value": [{"data": [pool_b64, "base64"]}]}
        return {"context": {"slot": slot}, "value": [{"data": [_vault(v0), "base64"]},
                                                     {"data": [_vault(v1), "base64"]}]}
    return rpc


def test_fresh_reserves_are_vaults_minus_the_pools_counters_on_the_input_side():
    b64 = FX["pool_states_b64"][SOL_EV.pool]
    st = C.parse_pool_state(base64.b64decode(b64))
    v0, v1 = 10**15, 3 * 10**15
    r0 = v0 - st.protocol_fees_0 - st.fund_fees_0 - st.creator_fees_0
    r1 = v1 - st.protocol_fees_1 - st.fund_fees_1 - st.creator_fees_1
    assert C.fresh_reserves(_fresh_rpc(b64, v0, v1), SOL_EV.pool, input_is_0=True) == (r0, r1, 777)
    assert C.fresh_reserves(_fresh_rpc(b64, v0, v1), SOL_EV.pool, input_is_0=False) == (r1, r0, 777)


def test_the_shadow_line_carries_age_build_tokens_and_the_reserve_deltas():
    buy = L.cpmm_slot_buy(_routed(SOL_EV))
    result = {"reason": "built exp=9 min=7", "signal_age_ms": 12.34, "build_ms": 1.5,
              "expected_tokens_out": 9, "min_tokens_out": 7, "event_reserve_in": 100,
              "event_reserve_out": 50}
    line = C.shadow_line(buy, result, (103, 49, 777))
    for part in ("slot lane cpmm shadow alice", "age_ms=12.3", "build_ms=1.5", "exp=9", "min=7",
                 "reserve_in_delta=3", "reserve_out_delta=-1", "fresh_slot=777"):
        assert part in line, line
    assert "reserve_in_delta=None" in C.shadow_line(buy, result, None)


def test_the_verifier_logs_each_event_and_drops_when_full():
    import threading
    lines, gate = [], threading.Event()
    b64 = FX["pool_states_b64"][SOL_EV.pool]

    def slow_rpc(method, params):
        gate.wait(5)
        return _fresh_rpc(b64, 10**15, 10**15)(method, params)

    v = C.ShadowVerifier(rpc=slow_rpc, log=lines.append, maxsize=1)
    buy = L.cpmm_slot_buy(_routed(SOL_EV))
    result = {"reason": "built", "event_reserve_in": 1, "event_reserve_out": 1, "input_is_0": True}
    for _ in range(5):
        v.offer(buy, result)
    gate.set()
    import time
    for _ in range(100):
        if v.stats["logged"] + v.stats["dropped"] >= 5:
            break
        time.sleep(0.02)
    assert v.stats["dropped"] >= 3 and v.stats["logged"] >= 1
    assert all("slot lane cpmm shadow" in line for line in lines)


def test_the_heartbeat_names_the_cpmm_shadow_counters(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "cpmm_shadow": {"logged": 3, "dropped": 1, "read_failed": 0, "junk": 9},
        "cpmm_pools": {"learned": 2, "learn_failed": 1, "queued": 4, "junk": 9}})
    assert json.loads(path.read_text())["slot_lane"]["cpmm_shadow"] == {
        "logged": 3, "dropped": 1, "read_failed": 0, "sim_ok": None, "sim_err": None,
        "sim_failed": None, "sim_skipped": None,
        "pools_learned": 2, "pools_learn_failed": 1, "pools_queued": 4}


# --- the shadow simulation (the cohort read: would it have landed) ---------------------------------

def _sim_logs(ev_b64):
    return ["Program %s invoke [1]" % C.CPMM_PROGRAM, "Program data: " + ev_b64,
            "Program %s success" % C.CPMM_PROGRAM]


def test_the_cpmm_simulation_reads_the_error_and_only_our_pools_event():
    raw = FX["events"][0]["event_b64"]
    ev = C.parse_swap_event(base64.b64decode(raw))
    assert C.simulated_outcome({"err": None, "logs": _sim_logs(raw)}, ev.pool) == (True, ev.output_amount)
    assert C.simulated_outcome({"err": {"InstructionError": [3, {"Custom": 6005}]},
                                "logs": _sim_logs(raw)}, ev.pool) == (False, None)
    assert C.simulated_outcome({"err": None, "logs": _sim_logs(raw)}, "OtherPool") == (False, None)


def test_the_cpmm_verifier_simulates_on_its_own_rpc_only_when_the_lane_is_idle(monkeypatch):
    import threading
    from sniper.execution import raydium_launchlab_buy as LL
    monkeypatch.setattr(LL, "SIM_IDLE_WAIT_S", 0.01)
    raw = next(e["event_b64"] for e in FX["events"]
               if C.parse_swap_event(base64.b64decode(e["event_b64"])).pool == SOL_EV.pool)
    sims, lines = [], []
    idle = threading.Event()
    v = C.ShadowVerifier(rpc=lambda *a: pytest.fail("the fresh read is off in this test"),
                         log=lines.append, idle=idle, start=False,
                         sim_rpc=lambda m, p: sims.append(m) or {"context": {"slot": 9},
                                                                 "value": {"err": None, "logs": _sim_logs(raw)}})
    buy = L.cpmm_slot_buy(_routed(SOL_EV))
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA"})
    assert sims == [] and v.stats["sim_skipped"] == 1          # the lane was busy: never started
    idle.set()
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA"})
    assert sims == ["simulateTransaction"] and v.stats["sim_ok"] == 1
    assert "sim_ok=True" in lines[-1] and "sim_slot=9" in lines[-1]


NO_ANSWER = {"null_result": {"result": None}, "null_value": {"context": {"slot": 9}, "value": None},
             "absent_value": {"context": {"slot": 9}}, "absent_err": {"value": {"logs": []}},
             "error_envelope": {"error": {"code": -32005}}}


@pytest.mark.parametrize("answer", sorted(NO_ANSWER))
def test_the_cpmm_verifier_counts_no_answer_as_failed_not_a_program_error(answer):
    """Gamma LOW on SOL-2 (2026-10-06): a null or missing simulation value was {} and counted
    sim_err, a program error. No answer is unknown: sim_failed, no outcome on the line."""
    lines = []
    v = C.ShadowVerifier(rpc=None, log=lines.append, start=False,
                         sim_rpc=lambda m, p: NO_ANSWER[answer])
    v.verify_once(L.cpmm_slot_buy(_routed(SOL_EV)), {"reason": "built", "unsigned_b64": "AAAA"})
    assert v.stats["sim_failed"] == 1 and v.stats["sim_err"] == 0 and v.stats["sim_ok"] == 0
    assert "sim_ok=None" in lines[-1]


# --- phase 2b: the ZEC leg (MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG) ---------------------------------------
# 35 of the 83 census CPMM swaps were ZEC-quoted. The leg is a third warm Jupiter leg on the funded
# key (role slot_leg), so it is default off and never spent on another asset's pool.

ZEC_EV = next(e for e in EVENTS if e.input_mint == C.ZEC_MINT)
PEPE_EV = next(e for e in EVENTS if e.input_mint.startswith("PEPE"))
USER = "3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6"


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _jup(calls, clock, *, ok=True, zec_out=41_000_000, latency_s=0.0):
    """Fake funded Jupiter: 1 ZEC = $600 at a $25 clip (4,166,666 atomic at 8 decimals)."""
    def jup(method, path, body=None):
        calls.append((method, path, body))
        clock.t += latency_s
        if not ok:
            return 500, {}
        if path.startswith("/quote"):
            return 200, {"outAmount": str(zec_out), "otherAmountThreshold": str(zec_out * 985 // 1000),
                         "routePlan": [{"swapInfo": {"label": "Whirlpool", "ammKey": "GTHKH8s8"}}]}
        return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111",
                                         "data": base64.b64encode(b"\x00").decode(),
                                         "accounts": [{"pubkey": USER, "isSigner": True, "isWritable": True}]},
                     "setupInstructions": [], "cleanupInstruction": None, "addressLookupTableAddresses": []}
    return jup


def _zec_live(monkeypatch):
    """ZEC as if it were on the live list: the only case its leg runs (zec_leg_on)."""
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", C.CPMM_LIVE_QUOTES | {C.ZEC_MINT})


def _zec_builder(monkeypatch, calls, clock, on=True, **jup_kw):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", on, raising=False)
    _zec_live(monkeypatch)                               # the leg's mechanics; it runs only when ZEC is live
    b = C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=lambda: None, clock=clock,
                              jup_call=_jup(calls, clock, **jup_kw), refresh_after_s=1.5)
    b.pools = _cache(ZEC_EV)
    return b


def test_zec_leg_off_by_default_costs_no_call_and_zec_stays_unsupported(monkeypatch):
    calls = []
    b = _zec_builder(monkeypatch, calls, _Clock(), on=cfg.MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG)
    assert cfg.MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG is False
    b.refresh(250.0)
    assert calls == []
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(ZEC_EV)), b.pools, 122.5)
    assert reason == "slot_lane_cpmm_quote_not_supported"


def test_zec_leg_quotes_usdc_to_zec_on_one_whirlpool_hop_and_prices_zec(monkeypatch):
    calls, clock = [], _Clock()
    b = _zec_builder(monkeypatch, calls, clock, zec_out=41_666_666)
    b.refresh(250.0)
    quote_path = calls[0][1]
    assert "outputMint=%s" % C.ZEC_MINT in quote_path and "dexes=Whirlpool" in quote_path
    assert "onlyDirectRoutes=true" in quote_path
    assert json.loads(calls[1][2])["wrapAndUnwrapSol"] is False
    assert b._legs[C.ZEC_MINT]["output_mint"] == C.ZEC_MINT
    assert b.pools.quote_usd[C.ZEC_MINT] == pytest.approx(250.0 / 0.41666666)


def test_a_zec_pool_builds_on_the_zec_leg_from_the_users_zec_account(monkeypatch):
    calls, clock = [], _Clock()
    b = _zec_builder(monkeypatch, calls, clock)
    b.refresh(250.0)
    buy = L.cpmm_slot_buy(_routed(ZEC_EV))
    assert buy is not None and buy.venue_event is ZEC_EV
    msg, detail = b.build(buy, 250.0, _snap())
    assert msg is not None, detail
    assert b.last_build["spend_lamports"] == 41_000_000 * 985 // 1000     # the leg's floor, in ZEC
    from sniper.execution.pump_native_exit import TOKEN_PROGRAM, _pk, ata
    keys = [str(k) for k in msg.account_keys]
    state = b.pools.get(ZEC_EV.pool)["state"]
    zec_prog = state.program_0 if state.mint_0 == C.ZEC_MINT else state.program_1
    assert str(ata(_pk(USER), _pk(C.ZEC_MINT), _pk(zec_prog))) in keys
    # no wSOL account is opened or closed on a ZEC build
    assert str(ata(_pk(USER), _pk(C.WSOL_MINT), _pk(TOKEN_PROGRAM))) not in keys


def test_a_leg_for_one_asset_is_never_spent_on_another_assets_pool(monkeypatch):
    calls, clock = [], _Clock()
    b = _zec_builder(monkeypatch, calls, clock)
    b.refresh(250.0)
    b._legs[C.ZEC_MINT] = dict(b._legs[C.ZEC_MINT], output_mint=C.WSOL_MINT)
    assert b.build(L.cpmm_slot_buy(_routed(ZEC_EV)), 250.0, _snap()) == (None, "leg_wrong_quote")
    # and the wSOL leg (no output_mint key: the PumpSwap builder's) never feeds a ZEC pool
    b._legs.clear()
    b.leg_fn = lambda: _leg(250.0, clock=clock)
    assert b.build(L.cpmm_slot_buy(_routed(ZEC_EV)), 250.0, _snap()) == (None, "leg_stale")


def test_the_zec_leg_stays_off_while_zec_is_not_live_even_with_cpmm_live(monkeypatch):
    """Alpha 2026-09-27: no shadow path calls the funded seam. With CPMM live on SOL/USDC
    (2026-09-28) a ZEC pool is still shadow, so its leg must stay off whatever its flag says."""
    import sniper.execution.pump_curve_buy as P
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM", "live", raising=False)
    monkeypatch.setattr(P, "_funded_jup_call", lambda *a, **k: pytest.fail("shadow reached the funded seam"))
    assert C.lane_mode() == "live" and C.ZEC_MINT not in C.CPMM_LIVE_QUOTES
    assert not C.zec_leg_on() and C.ZEC_MINT not in C.supported_quotes()
    b = C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=lambda: None)
    b.refresh(250.0)
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(ZEC_EV)), b.pools, 122.5)
    assert reason == "slot_lane_cpmm_quote_not_supported"


def test_an_unsupported_quote_stays_unsupported_with_the_zec_leg_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", True, raising=False)
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(PEPE_EV)), C.CpmmPoolCache(rpc=None), 122.5)
    assert reason == "slot_lane_cpmm_quote_not_supported"


def test_a_failing_zec_route_backs_off_and_drops_its_price_once_stale(monkeypatch):
    calls, clock = [], _Clock()
    b = _zec_builder(monkeypatch, calls, clock)
    b.refresh(250.0)
    assert C.ZEC_MINT in b.pools.quote_usd
    b.jup_call = _jup(calls, clock, ok=False)
    n = len(calls)
    for _ in range(8):                      # 4 s of 0.5 s prefetch ticks
        clock.t += 0.5
        b.refresh(250.0)
    failed_quotes = len(calls) - n
    assert failed_quotes <= 3               # at most one retry per refresh_after_s, not every tick
    assert C.ZEC_MINT not in b.pools.quote_usd and C.ZEC_MINT not in b._legs
    reason, _ = C.market_facts(L.cpmm_slot_buy(_routed(ZEC_EV)), b.pools, 122.5)
    assert reason == "slot_lane_cpmm_quote_price_unknown"


def test_zec_leg_funded_calls_per_minute_at_the_lanes_cadence(monkeypatch):
    """The number the commit quotes: 0.5 s prefetch ticks, 1.5 s refresh, 150 ms per Jupiter call."""
    calls, clock = [], _Clock()
    b = _zec_builder(monkeypatch, calls, clock, latency_s=0.15)
    end = clock.t + 60.0
    while clock.t < end:
        b.refresh(250.0)
        clock.t += 0.5
    assert 60 <= len(calls) <= 80, len(calls)


# --- (ii) the CPMM live path, built with the live list EMPTY (design section 7) -------------------

class _Routed:
    def __init__(self):
        self.written = []

    def write(self, routed):
        self.written.append(routed.signature)
        return "inserted"


def test_the_live_list_is_sol_and_usdc_and_an_exotic_quote_stays_shadow(monkeypatch):
    """Pinned (2026-09-28, operator "go live"): CPMM sends SOL- and USDC-quoted buys only. An exotic
    quote has no live leg and no residue sell path, so it stays shadow: routed row first."""
    assert C.CPMM_LIVE_QUOTES == frozenset({C.WSOL_MINT, C.USDC_MINT})
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    assert L.shadow_only(L.cpmm_slot_buy(_routed(USDC_EV))) is False
    assert L.shadow_only(L.cpmm_slot_buy(_routed(SOL_EV))) is False
    assert L.shadow_only(L.cpmm_slot_buy(_routed(EXOTIC_EV))) is True
    routed_rows, put = _Routed(), []
    L.route_routed_buy(_routed(EXOTIC_EV), handoff=routed_rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=True, pump_on=False, lane_live=True)
    assert routed_rows.written == ["SIG"] and [b.venue for b in put] == ["raydium_cpmm"]   # row first
    monkeypatch.setattr(C, "cpmm_live", lambda: False)                        # mode shadow: all shadow
    assert L.shadow_only(L.cpmm_slot_buy(_routed(USDC_EV))) is True


def test_a_live_quote_goes_to_the_lane_only_and_writes_no_routed_row(monkeypatch):
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    routed_rows, put = _Routed(), []
    L.route_routed_buy(_routed(USDC_EV), handoff=routed_rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=True, pump_on=False, lane_live=True)
    assert routed_rows.written == [] and [b.venue for b in put] == ["raydium_cpmm"]
    assert L.shadow_only(put[0]) is False
    # a SOL-quoted pool is not on the list: routed row first, shadow build
    L.route_routed_buy(_routed(SOL_EV), handoff=routed_rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=True, pump_on=False, lane_live=True)
    assert routed_rows.written == ["SIG"] and L.shadow_only(put[-1]) is True


@pytest.mark.parametrize("result,writes", [
    ({"status": "rejected", "reason": "not_approved"}, True),            # refused before reserving
    ({"status": "rejected", "reason": "slot_lane_signal_too_old"}, True),
    ({"status": "rejected", "reason": "pool_unknown_learning"}, False),  # the learner settles it
    ({"status": "aborted", "reason": "build refused", "pending_id": 7}, False),   # attempt row decides
    ({"status": "failed", "reason": "tx_failed", "pending_id": 7}, False),
    ({"status": "filled", "pending_id": 7, "position_id": 1}, False),
])
def test_a_live_cpmm_refusal_before_reserving_writes_its_routed_row_once(monkeypatch, result, writes):
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    rows = _Routed()
    lane = L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live", routed_handoff=rows)
    lane._routed_fallback(L.cpmm_slot_buy(_routed(USDC_EV)), dict(result))
    assert rows.written == (["SIG"] if writes else [])
    lane._routed_fallback(L.cpmm_slot_buy(_routed(SOL_EV)), dict(result))     # SOL not live: never
    assert rows.written == (["SIG"] if writes else [])


def test_a_live_cpmm_buy_runs_the_funded_path_with_a_validity_the_executor_reads(tmp_path, monkeypatch):
    """The lane's live path (reservation, build, executor) was written for the pump builders: a
    CPMM build must hand the executor every field it and the trace read. Real ledger, fake executor."""
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    import contextlib
    import sqlite3
    import time
    from solders.message import MessageV0
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    builder = C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=None)
    builder.pools = _cache(USDC_EV)
    buy = L.cpmm_slot_buy(_routed(USDC_EV))
    _r, facts = C.market_facts(buy, builder.pools, 150.0)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (None, dict(facts)))
    seen, rows = {}, _Routed()

    def slot_execute_fn(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                        entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen.update(pending=dict(pending), message=message, validity=validity)
        return {"status": "filled", "position_id": 1}
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        now = time.monotonic()
        snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                                  last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                      "blockhash": now - 1})
        lane = L.SlotLane(conn=c, executor={"slot_execute_fn": slot_execute_fn},
                          build_fn=L.dispatch_build({"raydium_cpmm": builder}), snapshot_fn=lambda: snap,
                          armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                          now_iso_fn=lambda: "2026-09-27T23:50:00+00:00", lock_key="sol-test", mode="live",
                          routed_handoff=rows)
        result = lane.handle(dataclasses_replace(buy, recv_monotonic=now))
        assert result["status"] == "filled", result
        v, pending = seen["validity"], seen["pending"]
        assert isinstance(seen["message"], MessageV0)
        assert v["target_mint"] == pending["mint"] == USDC_EV.output_mint
        assert v["blockhash"] == BLOCKHASH and v["route_target"].endswith(USDC_EV.pool)
        assert v["pump_price_limit"] and v["min_tokens_out"] <= v["expected_tokens_out"]
        assert pending["origin_event_id"] == "slotlane:SIG" and rows.written == []   # one path
        row = c.execute("SELECT terminal_status, signal_source FROM memecoin_entry_attempts").fetchone()
        assert tuple(row) == ("filled", "slot_lane")


def dataclasses_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)



# --- CPMM exotic quotes through the on-demand leg (MEMECOIN_SLOT_LANE_CPMM_ONDEMAND_LEG), SHADOW -----
# 65 of the 83 census CPMM swaps, and tonight's first live CPMM buy (ANTHROPIC), were quoted in an
# asset the lane keeps no leg for. The LaunchLab (b) worker serves CPMM too (one worker, one backoff).

def _exotic_builder(ev, quote=(6, 0, 0)):
    b = C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=None)
    b.pools = _cache(ev)
    fee = {"transferFeeBasisPoints": quote[1], "maximumFee": quote[2]}
    b.pools._facts[ev.pool]["mint_values"] = {ev.input_mint: _parsed_mint(
        ("transferFeeConfig", {"newerTransferFee": fee, "olderTransferFee": fee}), decimals=quote[0])}
    return b


def _paper(calls, out):
    def jup(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/quote"):
            return 200, {"outAmount": str(out), "otherAmountThreshold": str(out * 985 // 1000),
                         "routePlan": [{"swapInfo": {"label": "HumidiFi", "ammKey": "QuotePool1"}}]}
        return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111",
                                         "data": base64.b64encode(b"\0").decode(),
                                         "accounts": [{"pubkey": USER, "isSigner": True, "isWritable": True}]},
                     "setupInstructions": [], "cleanupInstruction": None, "addressLookupTableAddresses": []}
    return jup


def _cpmm_worker(b, calls, out, admission=lambda buy, quote: None, **kw):
    from sniper.execution import raydium_launchlab_buy as LL
    results = []
    w = LL.OnDemandLegWorker(builders={"raydium_cpmm": b}, admission_fn=admission,
                             snapshot_fn=lambda: SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH,
                                                                 last_valid_height=1),
                             clip_fn=lambda: 100.0, offer_fn=lambda buy, r: results.append(r),
                             jup_call=_paper(calls, out), start=False, **kw)
    return w, results


def test_an_exotic_quote_cpmm_buy_is_built_on_one_paper_key_leg_and_records_its_residue():
    calls, seen = [], []
    b = _exotic_builder(PEPE_EV)
    out = PEPE_EV.input_amount // 10
    w, _ = _cpmm_worker(b, calls, out, admission=lambda buy, quote: seen.append(quote))
    r = w.process(L.cpmm_slot_buy(_routed(PEPE_EV)))
    assert r["reason"].startswith("built"), r
    assert "outputMint=%s" % PEPE_EV.input_mint in calls[0][1] and "maxAccounts=32" in calls[0][1]
    assert len(calls) == 2 and seen and seen[0][0] == 6 and seen[0][1] > 0      # admission priced it
    assert r["residue_q"] == out - out * 985 // 1000 and r["unsigned_b64"] and r["leg_ms"] >= 0
    assert w.stats["offered_raydium_cpmm"] == 0 and w.stats["built"] == 1     # process() direct


def test_the_cpmm_quotes_transfer_fee_prices_the_buy():
    calls = []
    b = _exotic_builder(PEPE_EV, quote=(6, 300, 2**64 - 1))
    out = PEPE_EV.input_amount // 10
    w, _ = _cpmm_worker(b, calls, out)
    r = w.process(L.cpmm_slot_buy(_routed(PEPE_EV)))
    pool = b.pools.get(PEPE_EV.pool)
    reserve_in, reserve_out = C.post_trade_reserves(PEPE_EV, pool["config"])
    _g, net = C.expected_buy(out * 985 // 1000, reserve_in, reserve_out, pool["state"], pool["config"],
                             input_is_0=PEPE_EV.input_mint == pool["state"].mint_0,
                             out_fee_bps=pool["mints"][PEPE_EV.output_mint][2],
                             out_fee_max=pool["mints"][PEPE_EV.output_mint][3], in_fee=(300, 2**64 - 1))
    assert r["expected_tokens_out"] == net


def test_an_unknown_exotic_pool_is_learned_on_the_worker_thread_not_refused():
    learned = []
    b = _exotic_builder(PEPE_EV)
    facts = b.pools._facts.pop(PEPE_EV.pool)
    b.pools.learn = lambda pool: learned.append(pool) or b.pools._facts.setdefault(pool, facts)
    quote_mint, quote, refusal = b.ondemand_quote(L.cpmm_slot_buy(_routed(PEPE_EV)))
    assert learned == [PEPE_EV.pool] and quote_mint == PEPE_EV.input_mint and quote == (6, 0, 0)
    assert refusal is None


def test_no_cpmm_shadow_path_reaches_the_funded_jupiter_seam(monkeypatch):
    import sniper.execution.pump_curve_buy as P
    from sniper.execution import raydium_launchlab_buy as LL
    monkeypatch.setattr(P, "_funded_jup_call", lambda *a, **k: pytest.fail("shadow reached the funded seam"))
    paper = []
    monkeypatch.setattr(LL, "paper_jup_call", lambda m, p, b=None: paper.append(p) or _paper([], PEPE_EV.input_amount // 10)(m, p, b))
    b = _exotic_builder(PEPE_EV)
    w = LL.OnDemandLegWorker(builders={"raydium_cpmm": b}, admission_fn=lambda buy, q: None,
                             snapshot_fn=lambda: SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH, last_valid_height=1),
                             clip_fn=lambda: 100.0, offer_fn=None, start=False)       # default client
    assert w.process(L.cpmm_slot_buy(_routed(PEPE_EV)))["reason"].startswith("built")
    assert len(paper) == 2


def test_one_worker_one_backoff_across_venues_and_only_its_venues(monkeypatch):
    """The paper key is shared, so a 429 on one venue's leg holds every venue's legs for 60 s; a
    venue the worker was not given is never taken."""
    from sniper.execution import raydium_launchlab_buy as LL

    class Clock:
        t = 100.0

        @classmethod
        def monotonic(cls):
            return cls.t
    calls = []
    b = _exotic_builder(PEPE_EV)
    w, _ = _cpmm_worker(b, calls, PEPE_EV.input_amount // 10, clock=Clock)
    w.jup_call = lambda m, p, body=None: calls.append(p) or (429, None)
    buy = L.cpmm_slot_buy(_routed(PEPE_EV))
    assert w.process(buy)["reason"] == "slot_lane_cpmm_leg_rate_limited" and w.stats["leg_rate_limited"] == 1
    Clock.t = 150.0
    assert w.process(buy)["reason"] == "slot_lane_cpmm_leg_backoff" and len(calls) == 1
    launchlab_buy = SimpleNamespace(venue="raydium_launchlab", signature="S2")
    assert w.offer(launchlab_buy) is False and w.stats["offered"] == 0
    assert "slot_lane_cpmm_quote_not_supported" in L.ONDEMAND_QUOTE_REASONS


# --- Epoch-aware quote fees (Alpha, 2026-09-28) ------------------------------------------------------
# ANTHROPIC's transfer-fee schedule as read on 2026-09-28 at epoch 1044. The on-demand dry run refused
# it as unmovable because its newer and older fees differ.
ANTHROPIC_FEES = ("transferFeeConfig", {
    "newerTransferFee": {"epoch": 1043, "maximumFee": 18446744073709551615, "transferFeeBasisPoints": 300},
    "olderTransferFee": {"epoch": 1039, "maximumFee": 18446744073709551615, "transferFeeBasisPoints": 100}})


def test_a_fee_changing_quote_takes_its_fee_at_the_cached_epoch():
    mint = _parsed_mint(ANTHROPIC_FEES)
    assert C.quote_facts(mint, epoch=1043) == (6, 300, 2**64 - 1)       # epoch == newer epoch: newer
    assert C.quote_facts(mint, epoch=1042) == (6, 100, 2**64 - 1)       # newer epoch - 1: older
    assert C.quote_facts(mint, epoch=1044) == (6, 300, 2**64 - 1)
    assert C.quote_facts(mint) is None                                  # epoch unknown: refuses as before
    assert C.coin_facts(mint) is None                                   # coins stay epoch-blind (scope)


def test_the_epoch_is_read_at_most_once_per_ten_minutes_and_unknown_once_stale():
    reads, now = [], [1000.0]
    clock = lambda: now[0]

    def rpc(method, params):
        reads.append(method)
        return {"epoch": 1044, "slotIndex": 0, "slotsInEpoch": 432000}
    C.refresh_epoch(rpc, clock)
    assert reads == ["getEpochInfo"] and C.epoch_now(clock) == 1044
    now[0] += C.EPOCH_REFRESH_S - 1
    C.refresh_epoch(rpc, clock)
    assert len(reads) == 1                                              # at most one read per 10 min
    now[0] += 1
    C.refresh_epoch(lambda m, p: 1 / 0, clock)                          # a failed read keeps the value
    assert C.epoch_now(clock) == 1044
    now[0] = 1000.0 + C.EPOCH_MAX_AGE_S
    assert C.epoch_now(clock) == 1044
    now[0] += 0.001
    assert C.epoch_now(clock) is None                                   # stale beyond 30 min: refuse


def test_an_epoch_that_may_have_ended_since_the_read_is_unknown():
    now = [0.0]
    clock = lambda: now[0]
    C.refresh_epoch(lambda m, p: {"epoch": 1042, "slotIndex": 431900, "slotsInEpoch": 432000}, clock)
    now[0] = 100 * C.MIN_SLOT_S - 0.001
    assert C.epoch_now(clock) == 1042
    now[0] = 100 * C.MIN_SLOT_S
    assert C.epoch_now(clock) is None                                   # 100 slots left: 1043 may run


def test_the_on_demand_leg_prices_a_fee_changing_quote_at_the_cached_epoch_and_reads_nothing():
    calls = []
    b = _exotic_builder(PEPE_EV)
    b.pools._facts[PEPE_EV.pool]["mint_values"] = {PEPE_EV.input_mint: _parsed_mint(ANTHROPIC_FEES)}
    out = PEPE_EV.input_amount // 10
    w, _ = _cpmm_worker(b, calls, out)
    buy = L.cpmm_slot_buy(_routed(PEPE_EV))
    assert w.process(buy)["reason"] == "slot_lane_cpmm_quote_unmovable" and not calls   # none cached
    b.pools.learn_pending()                     # the prefetch thread: its one epoch read (1044)
    assert C.epoch_now() == 1044
    b.pools.rpc = lambda *a: pytest.fail("the worker read the network for a known pool")
    assert b.ondemand_quote(buy)[1] == (6, 300, 2**64 - 1)
    r = w.process(buy)
    pool = b.pools.get(PEPE_EV.pool)
    reserve_in, reserve_out = C.post_trade_reserves(PEPE_EV, pool["config"])
    _g, net = C.expected_buy(out * 985 // 1000, reserve_in, reserve_out, pool["state"], pool["config"],
                             input_is_0=PEPE_EV.input_mint == pool["state"].mint_0,
                             out_fee_bps=pool["mints"][PEPE_EV.output_mint][2],
                             out_fee_max=pool["mints"][PEPE_EV.output_mint][3], in_fee=(300, 2**64 - 1))
    assert r["reason"].startswith("built") and r["expected_tokens_out"] == net


def test_the_lane_threads_admission_never_reads_the_epoch():
    """Alpha: the epoch comes from the cached value only; the lane thread never reads it."""
    cache = _cache(PEPE_EV, SOL_EV)
    cache.rpc = lambda *a: pytest.fail("the lane thread read the network")
    for ev, quote in ((PEPE_EV, (6, 1.0)), (PEPE_EV, None), (SOL_EV, None)):
        L.admission_refusal(L.cpmm_slot_buy(_routed(ev)), armed_ids={"u1"}, sol_usd=122.5,
                            kill_tripped=False, cpmm_pools=cache, venue_quote=quote)
    assert C._EPOCH["tried"] is None


def test_a_slow_epoch_read_is_dropped_within_the_bound_and_not_retried_for_ten_minutes():
    """Alpha: the prefetch thread also refreshes the funded legs, so the epoch read is bounded at
    0.5 s; a late read keeps the old value and backs off the full 600 s."""
    import threading
    import time
    assert C.EPOCH_READ_TIMEOUT_S <= 0.5
    release, reads, now = threading.Event(), [], [0.0]
    clock = lambda: now[0]

    def slow(method, params):
        reads.append(method)
        release.wait(5)
        return {"epoch": 9999, "slotIndex": 0, "slotsInEpoch": 432000}
    C.refresh_epoch(lambda m, p: {"epoch": 1044, "slotIndex": 0, "slotsInEpoch": 432000}, clock)
    now[0] = C.EPOCH_REFRESH_S
    t0 = time.perf_counter()
    C.refresh_epoch(slow, clock)
    assert time.perf_counter() - t0 < C.EPOCH_READ_TIMEOUT_S + 0.25       # returned within the bound
    assert reads == ["getEpochInfo"] and C.epoch_now(clock) == 1044        # the old value kept
    now[0] += 1.0
    C.refresh_epoch(slow, clock)                                           # the next tick
    now[0] = 2 * C.EPOCH_REFRESH_S - 0.001
    C.refresh_epoch(slow, clock)
    assert reads == ["getEpochInfo"]                                       # no re-read inside 600 s
    release.set()
    for th in threading.enumerate():
        if th.name == "slot-lane-epoch-read":
            th.join(2)
    assert C.epoch_now(clock) == 1044                                      # the late answer dropped


# --- CPMM / LaunchLab LIVE on SOL and USDC (2026-09-28, operator "go live"): the cap and the canary --

def _live_lane(tmp_path, monkeypatch, execute):
    import contextlib
    import sqlite3
    import time
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    builder = C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=None)
    builder.pools = _cache(USDC_EV)
    buy = L.cpmm_slot_buy(_routed(USDC_EV))
    _r, facts = C.market_facts(buy, builder.pools, 150.0)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (None, dict(facts)))
    rows = _Routed()
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    now = time.monotonic()
    snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute},
                      build_fn=L.dispatch_build({"raydium_cpmm": builder}), snapshot_fn=lambda: snap,
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-09-27T23:50:00+00:00", lock_key="sol-test", mode="live",
                      routed_handoff=rows)
    return lane, dataclasses_replace(buy, recv_monotonic=now), rows, contextlib.closing(c)


def test_a_live_buy_over_the_hourly_cap_is_refused_before_reserving_and_routed(tmp_path, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 0)
    lane, buy, rows, closing = _live_lane(
        tmp_path, monkeypatch, lambda *a, **k: pytest.fail("a capped buy was sent"))
    with closing as c:
        result = lane.handle(buy)
        assert result == {"status": "rejected", "reason": "slot_lane_venue_live_cap",
                          "routed_fallback": "inserted"}
        assert rows.written == ["SIG"]                 # the on-chain lane gets the buy, once
        assert c.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_the_cap_counts_sends_over_a_sliding_hour(monkeypatch):
    class Clock:
        t = 1000.0

        def monotonic(self):
            return self.t
    clock = Clock()
    lane = L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live", clock=clock)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 2)
    cpmm, pump = L.cpmm_slot_buy(_routed(USDC_EV)), SimpleNamespaceBuy(venue="pump_amm")
    lane._venue_live_sends["raydium_cpmm"].extend([clock.t - 3599.0, clock.t - 10.0])
    assert lane._venue_live_capped(cpmm) == "slot_lane_venue_live_cap"
    assert lane._venue_live_capped(pump) is None          # the pump venues are not capped here
    clock.t += 1.5                                        # the older send leaves the hour
    assert lane._venue_live_capped(cpmm) is None and len(lane._venue_live_sends["raydium_cpmm"]) == 1


def SimpleNamespaceBuy(**kw):
    return SimpleNamespace(**kw)


def test_the_first_five_live_sends_per_venue_log_a_canary_line(tmp_path, monkeypatch, caplog):
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 6)

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        return {"status": "failed", "reason": "chain_error:slippage", "sig": "TX1",
                "landing_slot": 101}
    lane, buy, rows, closing = _live_lane(tmp_path, monkeypatch, execute)
    with closing, caplog.at_level("WARNING", logger=L.__name__):
        result = lane.handle(buy)
        assert result["status"] == "failed" and len(lane._venue_live_sends["raydium_cpmm"]) == 1
        lines = [r.getMessage() for r in caplog.records if "LIVE CANARY" in r.getMessage()]
        assert len(lines) == 1, lines
        assert ("raydium_cpmm 1/5" in lines[0] and "landing=101 land-S=1" in lines[0]
                and "outcome=failed/chain_error:slippage" in lines[0] and "sig=TX1" in lines[0])
        assert "exp=None" not in lines[0] and "min=None" not in lines[0]
        lane._canary["raydium_cpmm"] = 5                 # after five, no more lines
        lane._canary_line(buy, {}, {"status": "filled"})
        assert len([r for r in caplog.records if "LIVE CANARY" in r.getMessage()]) == 1


# --- the cap counts PER VENUE (2026-10-01, Alpha; Gamma's canary is 2 an hour per venue) ------------

def _cap_lane():
    class Clock:
        t = 1000.0

        def monotonic(self):
            return self.t
    return L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live", clock=Clock())


def test_one_venues_spent_cap_does_not_cap_another_venue(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 2)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    lane = _cap_lane()
    cpmm, damm = L.cpmm_slot_buy(_routed(USDC_EV)), SimpleNamespaceBuy(venue="meteora_damm_v2")
    lane._venue_live_sends["raydium_cpmm"].extend([lane.clock.t - 10.0, lane.clock.t - 5.0])
    assert lane._venue_live_capped(cpmm) == "slot_lane_venue_live_cap"
    assert lane._venue_live_capped(damm) is None, "DAMM v2 counts its own sends"


def test_a_per_venue_override_caps_only_that_venue(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 6)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE",
                        L.cfg._parse_venue_caps("meteora_damm_v2=2"), raising=False)
    lane = _cap_lane()
    cpmm, damm = L.cpmm_slot_buy(_routed(USDC_EV)), SimpleNamespaceBuy(venue="meteora_damm_v2")
    for venue in ("raydium_cpmm", "meteora_damm_v2"):
        lane._venue_live_sends[venue].extend([lane.clock.t - 10.0, lane.clock.t - 5.0])
    assert lane._venue_live_capped(damm) == "slot_lane_venue_live_cap"     # its own 2
    assert lane._venue_live_capped(cpmm) is None                           # the default 6


def test_a_malformed_venue_cap_closes_that_venue_instead_of_loosening_it():
    caps = L.cfg._parse_venue_caps("meteora_damm_v2=two, raydium_cpmm = 3 ,junk,=4, x=-1,")
    # A part with no '=' or no venue is kept under its own text, so the start line names it.
    assert caps == {"meteora_damm_v2": 0, "raydium_cpmm": 3, "junk": 0, "=4": 0, "x": 0}
    assert L.cfg._parse_venue_caps("") == {}


def test_a_tip_is_appended_last_and_passes_the_sender_guard():
    """Sender for every slot-lane venue (Alpha 2026-10-07, MEMECOIN_SLOT_LANE_SENDER_ALL): the tip
    transfer is the build's last instruction, exactly one, exactly the asked lamports."""
    from sniper.execution import solana_sender_ab as S
    b = C.SlotLaneCpmmBuilder(user="3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6", rpc=None,
                              leg_fn=lambda: _leg(100.0), clock=lambda: 50.0)
    b.pools = _cache(SOL_EV)
    plain, _ = b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap())
    msg, detail = b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap(),
                          tip=(S.TIP_ACCOUNTS[0], S.TIP_LAMPORTS))
    assert msg is not None, detail
    assert S.tip_transfers(msg) == [(S.TIP_ACCOUNTS[0], 5_000)]
    assert S.guard_refusal(msg, 5_000) is None
    assert len(msg.instructions) == len(plain.instructions) + 1
    assert S.tip_transfers(plain) == []


def test_a_treated_crowded_send_prices_the_target_from_this_builds_cu_limit(monkeypatch):
    """Crowded fee on every slot-lane venue (Alpha GO 2026-10-16): CPMM takes the treated priority."""
    from sniper.settings import memecoin as cfg
    from test_memecoin_crowded_fee import budget, fee_lamports
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000)
    b = C.SlotLaneCpmmBuilder(user="3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6", rpc=None,
                              leg_fn=lambda: _leg(100.0), clock=lambda: 50.0)
    b.pools = _cache(SOL_EV)
    assert b.accepts_priority_lamports is True
    plain, plain_detail = b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap())
    msg, detail = b.build(L.cpmm_slot_buy(_routed(SOL_EV)), 100.0, _snap(), priority_lamports=10_000_000)
    limit, price = budget(msg)
    assert limit == detail["cu_limit"] and fee_lamports(limit, price) >= 10_000_000
    assert detail["priority_bid"]["crowded"] == "raised" and "priority_bid" not in plain_detail
    assert budget(plain)[1] < price
