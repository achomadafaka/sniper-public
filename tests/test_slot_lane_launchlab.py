"""Raydium LaunchLab on the slot lane, SHADOW only (2026-09-27, Alpha GO, phase (a)).

A routed buy whose own logs frame carried LaunchLab's TradeEvent for its coin, from a pool quoted in
SOL, USDC or (with the ZEC leg on) ZEC, is built natively, then simulated once off the lane thread.
It is never reserved, locked or sent, whatever the lane's own mode. A pool miss goes to the
MissLearner like a PumpSwap one. Events are real census buys
(tests/fixtures/memecoin/raydium_launchlab_frames.json).
"""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import raydium_cpmm_buy as C
from sniper.execution import raydium_launchlab_buy as LL
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "raydium_launchlab_frames.json").read_text())
BUYS = FX["buys"]
BLOCKHASH = "11111111111111111111111111111111"
USER = "3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6"


def _ev(b):
    return LL.parse_trade_event(base64.b64decode(b["event_b64"]))


def _pool(b):
    return LL.parse_pool_state(base64.b64decode(FX["pool_states_b64"][b["ix_accounts"][4]]))


def _buy_quoted(mint):
    return next(b for b in BUYS if b["ix_accounts"][10] == mint)


SOL_B, USDC_B, ZEC_B = _buy_quoted(C.WSOL_MINT), _buy_quoted(C.USDC_MINT), _buy_quoted(C.ZEC_MINT)
NVDA_B = _buy_quoted("Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh")


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


def _facts(b, coin=None):
    curve, trade, _q = LL.parse_global_config(base64.b64decode(FX["global_configs_b64"][b["ix_accounts"][2]]))
    platform, creator = LL.parse_platform_config(base64.b64decode(FX["platform_configs_b64"][b["ix_accounts"][3]]))
    p = _pool(b)
    return {"pool": p, "rates": LL.Rates(curve, trade, platform, creator),
            "coin": coin if coin is not None else (10**15, p.base_decimals, *b["base_fee"][:2])}


def _quote_value(facts):
    """A jsonParsed quote mint whose quote_facts are `facts` (decimals, fee bps, fee max); None
    stays None (a mint the lane cannot read)."""
    if facts is None:
        return None
    fee = {"transferFeeBasisPoints": facts[1], "maximumFee": facts[2]}
    return {"data": {"parsed": {"info": {"supply": "1", "decimals": facts[0], "extensions": [
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": dict(fee, epoch=2),
                                                     "olderTransferFee": dict(fee, epoch=1)}}]}}}}


def _cache(*buys, quote_usd=None):
    cache = LL.LaunchLabPoolCache(rpc=_no_pool_reads,
                                  quote_usd=quote_usd)
    for b in buys:
        cache._facts[b["ix_accounts"][4]] = _facts(b)
    return cache


def _routed(b, events=None, mint=None):
    return S.RoutedBuy(source_user_id="u1", handle="alice", owner="Owner1111", signature="SIG",
                       slot=100, mint=mint or b["ix_accounts"][9], token_delta_atomic=1,
                       token_decimals=6, usdc_spent_atomic=1, recv_monotonic=1.0, recv_utc="t",
                       paired_monotonic=1.0,
                       venue_events=tuple(events if events is not None else (_ev(b),)))


def _snap():
    return SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH, last_valid_height=1)


def _leg(clip_usd, out=1_000_000_000, mint=C.WSOL_MINT, fetched_at=50.0):
    from sniper.execution.pump_curve_buy import clip_atomic
    return {"ixs": [], "tables": [], "usdc_atomic": clip_atomic(clip_usd), "min_out_lamports": out,
            "out_lamports": out, "fetched_at": fetched_at, "quote": {}, "output_mint": mint}


def _builder(leg_for=None, *buys, quote_usd=None):
    b = LL.SlotLaneLaunchLabBuilder(user=USER, rpc=None, leg_for=leg_for, clock=lambda: 50.0)
    b.pools = _cache(*buys, quote_usd=quote_usd)
    return b


# --- conversion ----------------------------------------------------------------------------------

def test_a_routed_buy_with_one_launchlab_buy_becomes_a_launchlab_slot_buy():
    buy = L.launchlab_slot_buy(_routed(SOL_B))
    assert buy.venue == "raydium_launchlab" and buy.venue_event == _ev(SOL_B)
    assert buy.pool == SOL_B["ix_accounts"][4] and buy.mint == SOL_B["ix_accounts"][9]


@pytest.mark.parametrize("events", [(), "two"])
def test_no_or_two_launchlab_buys_is_not_a_launchlab_buy(events):
    evs = (_ev(SOL_B), _ev(USDC_B)) if events == "two" else ()
    assert L.launchlab_slot_buy(_routed(SOL_B, events=evs)) is None


def test_the_socket_decodes_launchlab_trades_only_when_on():
    frame = {"params": {"result": {"context": {"slot": 1}, "value": {
        "logs": next(iter(FX["frame_logs"].values()))}}}}
    assert S._launchlab_trades(frame, False) == ()
    assert len(S._launchlab_trades(frame, True)) == 1
    assert S._launchlab_trades({"params": None}, True) == ()


def test_the_routed_thread_puts_a_launchlab_buy_only_when_on():
    put = []
    for on in (False, True):
        L.route_routed_buy(_routed(SOL_B), handoff=None, log=lambda line: None, put=put.append,
                           amm_pools=None, cpmm_on=False, pump_on=False, lane_live=False,
                           launchlab_on=on)
    assert [b.venue for b in put] == ["raydium_launchlab"]


# --- market facts ----------------------------------------------------------------------------------

def test_market_facts_price_from_the_events_post_trade_reserves():
    reason, facts = LL.market_facts(L.launchlab_slot_buy(_routed(SOL_B)), _cache(SOL_B), 122.5)
    assert reason is None and facts["pool"] == SOL_B["ix_accounts"][4]
    ev, p = _ev(SOL_B), _pool(SOL_B)
    spot = ((ev.virtual_quote + ev.real_quote_after) / 1e9) / ((ev.virtual_base - ev.real_base_after)
                                                               / 10 ** p.base_decimals) * 122.5
    assert facts["price_usd"] == pytest.approx(spot)
    assert facts["their_usd"] == pytest.approx(ev.amount_in / 1e9 * 122.5)


def test_an_unknown_pool_refuses_and_is_queued():
    cache = LL.LaunchLabPoolCache(rpc=None)
    reason, _ = LL.market_facts(L.launchlab_slot_buy(_routed(SOL_B)), cache, 122.5)
    assert reason == "pool_unknown" and cache._pending == [SOL_B["ix_accounts"][4]]


@pytest.mark.parametrize("case,reason", [
    ("mint", "slot_lane_event_mint_mismatch"),
    ("exotic", "slot_lane_launchlab_quote_not_supported"),
    ("coin", "slot_lane_launchlab_coin_unsupported"),
    ("zec_price", "slot_lane_launchlab_quote_price_unknown"),
])
def test_market_facts_refusals(monkeypatch, case, reason):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", True, raising=False)
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", C.CPMM_LIVE_QUOTES | {C.ZEC_MINT})   # ZEC's leg runs only when ZEC is live
    b = {"mint": SOL_B, "exotic": NVDA_B, "coin": SOL_B, "zec_price": ZEC_B}[case]
    cache = _cache(b)
    if case == "coin":
        cache._facts[b["ix_accounts"][4]]["coin"] = None
    buy = L.launchlab_slot_buy(_routed(b, mint="OtherMint111" if case == "mint" else None))
    assert LL.market_facts(buy, cache, 122.5)[0] == reason


def test_a_coin_with_any_other_extension_is_unsupported():
    def mint(*exts):
        return {"data": {"parsed": {"info": {"supply": "1000", "decimals": 6, "extensions": [
            {"extension": e, "state": {}} for e in exts]}}}}
    assert LL.coin_facts(mint("metadataPointer", "tokenMetadata")) == (1000, 6, 0, 0)
    for bad in ("transferHook", "permanentDelegate", "defaultAccountState", "pausableConfig",
                "nonTransferable"):
        assert LL.coin_facts(mint("metadataPointer", bad)) is None


# --- the build --------------------------------------------------------------------------------------

def test_a_sol_pool_builds_on_the_wsol_leg_and_closes_the_wsol_account():
    b = _builder(lambda mint: _leg(100.0) if mint == C.WSOL_MINT else None, SOL_B)
    msg, detail = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap())
    assert msg is not None, detail
    assert 0 < detail["min_tokens_out"] <= detail["expected_tokens_out"]
    assert detail["unsigned_b64"] and detail["route_target"].endswith(SOL_B["ix_accounts"][4])
    programs = [str(msg.account_keys[ix.program_id_index]) for ix in msg.instructions]
    assert LL.LAUNCHLAB_PROGRAM in programs and programs[-1] == LL.TOKEN_PROGRAM    # CloseAccount
    assert b.last_build["spend_lamports"] == 1_000_000_000


def test_the_build_prices_our_buy_on_the_sources_post_trade_curve():
    b = _builder(lambda mint: _leg(100.0), SOL_B)
    _msg, detail = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap())
    ev, f = _ev(SOL_B), _facts(SOL_B)
    _gross, net = LL.expected_buy(1_000_000_000, virtual_base=ev.virtual_base,
                                  virtual_quote=ev.virtual_quote, real_base=ev.real_base_after,
                                  real_quote=ev.real_quote_after, total_base_sell=ev.total_base_sell,
                                  rates=f["rates"], out_fee=f["coin"][2:])
    assert detail["expected_tokens_out"] == net


def test_a_usdc_pool_needs_no_leg():
    b = _builder(lambda mint: pytest.fail("a USDC pool must not read a leg"), USDC_B)
    msg, detail = b.build(L.launchlab_slot_buy(_routed(USDC_B)), 20.0, _snap())
    assert msg is not None, detail
    assert b.last_build["spend_lamports"] == 20_000_000


def test_a_zec_pool_builds_on_the_zec_leg_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", True, raising=False)
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(C, "CPMM_LIVE_QUOTES", C.CPMM_LIVE_QUOTES | {C.ZEC_MINT})   # ZEC's leg runs only when ZEC is live
    legs = {C.ZEC_MINT: _leg(100.0, out=16_000_000, mint=C.ZEC_MINT)}
    b = _builder(legs.get, ZEC_B, quote_usd={C.ZEC_MINT: 600.0})
    msg, detail = b.build(L.launchlab_slot_buy(_routed(ZEC_B)), 100.0, _snap())
    assert msg is not None, detail
    programs = [str(msg.account_keys[ix.program_id_index]) for ix in msg.instructions]
    assert programs[-1] == LL.LAUNCHLAB_PROGRAM                  # nothing to close after a ZEC buy
    legs[C.ZEC_MINT] = _leg(100.0, mint=C.WSOL_MINT)
    assert b.build(L.launchlab_slot_buy(_routed(ZEC_B)), 100.0, _snap()) == (None, "leg_wrong_quote")


@pytest.mark.parametrize("leg,reason", [(None, "leg_stale"), (_leg(100.0, fetched_at=0.0), "leg_stale"),
                                        (_leg(200.0), "leg_over_clip")])
def test_a_stale_or_mismatched_leg_refuses(leg, reason):
    b = _builder(lambda mint: leg, SOL_B)
    assert b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap()) == (None, reason)


# --- SHADOW CAN NEVER REACH A SEND -------------------------------------------------------------------

def test_a_launchlab_buy_on_a_live_lane_is_built_and_never_reserved_locked_or_sent(monkeypatch):
    import sniper.execution.exec_lock as lock
    import sniper.services.memecoin_risk as risk
    monkeypatch.setattr(lock, "wallet_exec_lock", lambda *a, **k: pytest.fail("wallet lock taken"))
    monkeypatch.setattr(risk, "reserve_entry", lambda *a, **k: pytest.fail("reservation written"))
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
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
        build_fn=lambda buy, clip, snap, **k: built.append(buy.venue) or (
            "MSG", {"expected_tokens_out": 9, "min_tokens_out": 7, "unsigned_b64": "AAAA"}),
        snapshot_fn=lambda: SimpleNamespace(sol_usd=122.5, usdc_balance=1.0, sol_balance=1.0,
                                            blockhash=BLOCKHASH, observed_mono={}),
        armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=lambda: "t",
        lock_key="sol-x", mode="live", shadow_clip_fn=lambda: 100.0, clock=Clock,
        launchlab_pools=_cache(SOL_B))
    monkeypatch.setattr(L, "admission_refusal", lambda buy, **k: (None, {
        "mint": buy.mint, "price_usd": 1.0, "mcap_usd": 1.0, "their_usd": 1.0}))
    result = lane.handle(L.launchlab_slot_buy(_routed(SOL_B)))
    assert result["status"] == "shadow" and built == ["raydium_launchlab"]
    assert result["unsigned_b64"] == "AAAA"


def test_the_lane_refuses_a_launchlab_buy_it_has_no_pool_cache_for():
    reason, _ = L.admission_refusal(L.launchlab_slot_buy(_routed(SOL_B)), armed_ids={"u1"},
                                    sol_usd=1.0, kill_tripped=False)
    assert reason == "slot_lane_venue_raydium_launchlab"


# --- the pool miss: the MissLearner, generalised ---------------------------------------------------

class _FakeCache:
    def __init__(self):
        self.learned = []

    def learn(self, pool):
        self.learned.append(pool)
        return {"pool": pool}


def test_the_miss_learner_reads_a_launchlab_miss_with_the_launchlab_cache_and_retries():
    amm, ll = _FakeCache(), _FakeCache()
    retried, handed = [], []

    class NoTimer:
        def __init__(self, *a, **k):
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            pass

    learner = L.MissLearner(pools={"pump_amm": amm, "raydium_launchlab": ll}, put=retried.append,
                            handoff_fn=lambda buy, reason: handed.append(buy), log=lambda line: None,
                            timer=NoTimer)
    buy = L.launchlab_slot_buy(_routed(SOL_B))
    assert learner.offer(buy) is True
    learner.learn_once(learner._venue_jobs["raydium_launchlab"].get_nowait())
    assert ll.learned == [buy.pool] and amm.learned == [] and retried == [buy] and handed == []


def test_a_launchlab_read_never_sits_in_front_of_a_pumpswap_miss():
    """One queue per venue: the funded venue's read is next on its own queue whatever is waiting on
    the shadow venue's, and a full shadow venue does not refuse a PumpSwap miss."""
    class NoTimer:
        def __init__(self, *a, **k):
            self.daemon = False

        def start(self):
            pass

    learner = L.MissLearner(pools={"pump_amm": _FakeCache(), "raydium_launchlab": _FakeCache()},
                            put=None, handoff_fn=None, log=lambda line: None, timer=NoTimer, max_waiting=1)
    ll_buy = L.launchlab_slot_buy(_routed(SOL_B))
    amm_buy = SimpleNamespace(signature="SIG2", pool="AmmPool1", venue="pump_amm", handle="b", slot=1)
    assert learner.offer(ll_buy) and learner.offer(amm_buy)
    assert learner._jobs.get_nowait() == "AmmPool1" and learner._jobs.empty()
    assert learner._venue_jobs["raydium_launchlab"].get_nowait() == ll_buy.pool


def test_the_miss_learner_declines_a_venue_it_has_no_cache_for():
    learner = L.MissLearner(pools={"pump_amm": _FakeCache()}, put=None, handoff_fn=None,
                            log=lambda line: None)
    assert learner.offer(L.launchlab_slot_buy(_routed(SOL_B))) is False


def test_the_pool_cache_learns_a_pool_its_configs_once_and_its_coin():
    accounts = {k: ("base64", v) for k, v in FX["pool_states_b64"].items()}
    accounts.update({k: ("base64", v) for k, v in FX["global_configs_b64"].items()})
    accounts.update({k: ("base64", v) for k, v in FX["platform_configs_b64"].items()})
    calls = []

    def rpc(method, params):
        calls.append(params[0])
        value = []
        for key in params[0]:
            if key in accounts:
                value.append({"owner": LL.LAUNCHLAB_PROGRAM, "data": [accounts[key][1], "base64"]})
            else:                                   # the coin, jsonParsed
                value.append({"data": {"parsed": {"info": {"supply": "1000000000000000", "decimals": 6,
                                                           "extensions": []}}}})
        return {"context": {"slot": 1}, "value": value}

    cache = LL.LaunchLabPoolCache(rpc=rpc)
    first, second = next((a, b) for a in BUYS for b in BUYS if a["ix_accounts"][2:4] == b["ix_accounts"][2:4]
                         and a["ix_accounts"][4] != b["ix_accounts"][4])
    facts = cache.learn(first["ix_accounts"][4])
    assert isinstance(facts["quote_read_at"], float)
    assert facts["pool"].base_mint == first["ix_accounts"][9] and facts["coin"] == (10**15, 6, 0, 0)
    assert facts["rates"] == LL.Rates(0, 2500, 10000, 0)
    assert calls[1] == [first["ix_accounts"][2], first["ix_accounts"][3], first["ix_accounts"][9],
                        first["ix_accounts"][10]]
    assert C.quote_facts(facts["quote_value"]) == (6, 0, 0)
    cache.learn(second["ix_accounts"][4])           # a second pool on those configs: coin and quote only
    assert calls[-1] == [second["ix_accounts"][9], second["ix_accounts"][10]]


# --- the shadow verifier: the ERROR decides, never the event alone ---------------------------------

def test_a_failed_simulation_that_still_logged_a_trade_event_is_not_a_fill():
    """Captured on chain (research key, 2026-09-27): our buy_exact_in with min_out = predicted net
    + 1 failed ExceededSlippage 6004 AFTER LaunchLab logged its TradeEvent."""
    failed = FX["failed_simulation"]
    assert failed["err"] == {"InstructionError": [3, {"Custom": 6004}]}
    assert len(LL.trade_events(failed["logs"])) == 1                 # the event IS there
    assert LL.simulated_outcome({"err": failed["err"], "logs": failed["logs"]}) == (False, None)
    ok, out = LL.simulated_outcome({"err": None, "logs": failed["logs"]})
    assert ok and out == LL.trade_events(failed["logs"])[0].amount_out


def test_the_verifier_simulates_once_and_logs_the_outcome():
    lines, calls = [], []
    failed = FX["failed_simulation"]

    def rpc(method, params):
        calls.append(method)
        return {"context": {"slot": 5}, "value": {"err": failed["err"], "logs": failed["logs"]}}

    v = LL.ShadowVerifier(rpc=rpc, log=lines.append, start=False)
    buy = L.launchlab_slot_buy(_routed(SOL_B))
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA", "expected_tokens_out": 9})
    assert calls == ["simulateTransaction"] and v.stats["sim_err"] == 1 and v.stats["sim_ok"] == 0
    assert "sim_ok=False sim_out=None" in lines[0] and "6004" in lines[0]


NO_ANSWER = {"null_result": {"result": None}, "null_value": {"context": {"slot": 9}, "value": None},
             "absent_value": {"context": {"slot": 9}}, "absent_err": {"value": {"logs": []}},
             "error_envelope": {"error": {"code": -32005}}}


@pytest.mark.parametrize("answer", sorted(NO_ANSWER))
def test_the_verifier_counts_no_answer_as_failed_not_a_program_error(answer):
    """Gamma LOW on SOL-2 (2026-10-06): a null or missing simulation value was {} and counted
    sim_err, a program error. No answer is unknown: sim_failed, no outcome on the line."""
    lines = []
    v = LL.ShadowVerifier(rpc=lambda m, p: NO_ANSWER[answer], log=lines.append, start=False)
    v.verify_once(L.launchlab_slot_buy(_routed(SOL_B)), {"reason": "built", "unsigned_b64": "AAAA"})
    assert v.stats["sim_failed"] == 1 and v.stats["sim_err"] == 0 and v.stats["sim_ok"] == 0
    assert "sim_ok=None" in lines[-1]


def test_a_simulation_never_starts_while_the_lane_is_handling_a_buy(monkeypatch):
    import threading
    monkeypatch.setattr(LL, "SIM_IDLE_WAIT_S", 0.01)
    lines, calls = [], []
    idle = threading.Event()                              # the lane is busy
    v = LL.ShadowVerifier(rpc=lambda m, p: calls.append(m) or {"value": {"err": None, "logs": []}},
                          log=lines.append, idle=idle, start=False)
    buy = L.launchlab_slot_buy(_routed(SOL_B))
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA"})
    assert calls == [] and v.stats["sim_skipped"] == 1 and len(lines) == 1
    idle.set()                                            # the lane waits for its next buy
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA"})
    assert calls == ["simulateTransaction"]


def test_the_simulation_rpc_is_its_own_url_with_a_two_second_timeout(monkeypatch):
    import io
    import urllib.request
    seen = {}

    def urlopen(req, timeout):
        seen.update(url=req.full_url, timeout=timeout)
        return io.BytesIO(b'{"result": {"value": {"err": null}}}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert LL.http_rpc("https://watch.example/?k=1")("simulateTransaction", []) == {"value": {"err": None}}
    assert seen == {"url": "https://watch.example/?k=1", "timeout": 2.0} and LL.SIM_TIMEOUT_S <= 2.0


def test_the_heartbeat_names_the_launchlab_shadow_counters(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "launchlab_shadow": {"logged": 3, "dropped": 1, "sim_ok": 1, "sim_err": 2, "sim_failed": 0,
                             "sim_skipped": 5, "x": 9},
        "launchlab_pools": {"learned": 2, "learn_failed": 1, "queued": 4, "x": 9}})
    assert json.loads(path.read_text())["slot_lane"]["launchlab_shadow"] == {
        "logged": 3, "dropped": 1, "sim_ok": 1, "sim_err": 2, "sim_failed": 0, "sim_skipped": 5,
        "pools_learned": 2, "pools_learn_failed": 1, "pools_queued": 4}


def test_the_shadow_read_parses_both_verifiers_own_lines():
    """scripts/research/slot_lane_shadow_read.py is the cohort read Alpha asks for before live: it
    must read the lines the verifiers actually write, not a hand-typed copy of them."""
    import importlib.util
    from test_slot_lane_cpmm import SOL_EV, _routed as cpmm_routed
    spec = importlib.util.spec_from_file_location(
        "slot_lane_shadow_read", Path(__file__).parent.parent / "scripts" / "research" / "slot_lane_shadow_read.py")
    read = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(read)
    result = {"reason": "built exp=90 min=80", "signal_age_ms": 12.5, "build_ms": 0.3,
              "expected_tokens_out": 90, "min_tokens_out": 80, "expected_gross": 95}
    cpmm = C.shadow_line(L.cpmm_slot_buy(cpmm_routed(SOL_EV)), dict(result, event_reserve_in=10),
                         (10, 20, 101), (True, 95, None, 102))
    launchlab = LL.shadow_line(L.launchlab_slot_buy(_routed(SOL_B)), result,
                               (False, None, {"InstructionError": [3, {"Custom": 6004}]}, 103))
    assert all(" sig=SIG recv=t" in line for line in (cpmm, launchlab))    # the ledger join key
    rows = [read.parse("2026-09-27T23:30:00+00:00 " + line) for line in (cpmm, launchlab)]
    assert [r["venue"] for r in rows] == ["cpmm", "launchlab"]
    assert rows[0]["sim_ok"] is True and rows[0]["sim_out"] == 95 and rows[0]["gross"] == 95
    assert rows[1]["sim_ok"] is False and "6004" in rows[1]["sim_err"] and rows[1]["sim_slot"] == 103
    text = read.report(rows)
    assert "cpmm: 1 events, 1 built, 1 simulated, 1 would have landed (100%)" in text, text
    assert "launchlab: 1 events, 1 built, 1 simulated, 0 would have landed (0%), 1 failed on slippage" in text, text



# --- phase (b): the on-demand quote leg (MEMECOIN_SLOT_LANE_LAUNCHLAB_ONDEMAND_LEG), SHADOW ONLY ------

GP_B = _buy_quoted("HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ")      # quote with a 3% transfer fee


def _jup(calls, out=10_000_000_000, gate=None):
    def jup(method, path, body=None):
        if gate is not None:
            gate.wait(5)
        calls.append((method, path, body))
        if path.startswith("/quote"):
            return 200, {"outAmount": str(out), "otherAmountThreshold": str(out * 985 // 1000),
                         "routePlan": [{"swapInfo": {"label": "Meteora DLMM", "ammKey": "QuotePool1"}}]}
        return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111",
                                         "data": base64.b64encode(b"\0").decode(),
                                         "accounts": [{"pubkey": USER, "isSigner": True, "isWritable": True}]},
                     "setupInstructions": [], "cleanupInstruction": None, "addressLookupTableAddresses": []}
    return jup


def _worker(b, calls, admission=lambda buy, quote: None, quote=(8, 0, 0), **kw):
    builder = _builder(None, b)
    builder.pools._facts[b["ix_accounts"][4]]["quote_value"] = _quote_value(quote)
    results = []
    w = LL.OnDemandLegWorker(builders={"raydium_launchlab": builder}, admission_fn=admission, snapshot_fn=_snap,
                             clip_fn=lambda: 100.0, offer_fn=lambda buy, r: results.append(r),
                             jup_call=_jup(calls, **kw), start=False)
    return w, results


def test_an_exotic_quote_buy_is_built_on_one_exact_in_leg_and_records_its_residue():
    calls = []
    w, _ = _worker(NVDA_B, calls, out=44_000_000)                  # 0.44 NVDAx for $100 (8 decimals)
    r = w.process(L.launchlab_slot_buy(_routed(NVDA_B)))
    assert r["status"] == "shadow" and r["reason"].startswith("built"), r
    quote_path = calls[0][1]
    assert "outputMint=%s" % NVDA_B["ix_accounts"][10] in quote_path and "maxAccounts=32" in quote_path
    assert "dexes=" not in quote_path and "swapMode" not in quote_path            # ExactIn, no pin
    assert json.loads(calls[1][2])["wrapAndUnwrapSol"] is False and len(calls) == 2
    assert r["residue_q"] == 44_000_000 - 44_000_000 * 985 // 1000               # out - spent floor
    assert r["residue_usd"] > 0 and r["leg_ms"] >= 0 and r["unsigned_b64"] and r["leg_mode"] == "ExactIn"
    assert w.stats["built"] == 1


def test_the_quotes_transfer_fee_prices_the_buy():
    calls = []
    fee = GP_B["quote_fee"][:2]
    w, _ = _worker(GP_B, calls, quote=(6, *fee), out=1_000_000_000)
    r = w.process(L.launchlab_slot_buy(_routed(GP_B)))
    ev, f = _ev(GP_B), _facts(GP_B)
    _g, net = LL.expected_buy(1_000_000_000 * 985 // 1000, virtual_base=ev.virtual_base,
                              virtual_quote=ev.virtual_quote, real_base=ev.real_base_after,
                              real_quote=ev.real_quote_after, total_base_sell=ev.total_base_sell,
                              rates=f["rates"], in_fee=fee, out_fee=f["coin"][2:])
    assert fee[0] == 300 and r["expected_tokens_out"] == net


@pytest.mark.parametrize("case,reason", [("unmovable", "slot_lane_launchlab_quote_unmovable"),
                                          ("admission", "mcap $1 over ceiling $0"),
                                          ("leg", "slot_lane_launchlab_leg_failed"),
                                          ("zero", "slot_lane_launchlab_leg_failed")])
def test_on_demand_refusals_are_results_not_silence(case, reason):
    calls = []
    kw = {"quote": None} if case == "unmovable" else {}
    if case == "admission":
        kw["admission"] = lambda buy, quote: "mcap $1 over ceiling $0"
    w, _ = _worker(NVDA_B, calls, out=0 if case == "zero" else 44_000_000, **kw)
    if case == "leg":
        w.jup_call = lambda *a, **k: (500, {})
    r = w.process(L.launchlab_slot_buy(_routed(NVDA_B)))
    assert r["reason"] == reason and w.stats["refused"] == 1


def test_the_worker_never_blocks_the_lane_and_keeps_at_most_two_in_flight():
    import threading
    import time
    gate, calls, results = threading.Event(), [], []
    builder = _builder(None, NVDA_B)
    builder.pools._facts[NVDA_B["ix_accounts"][4]]["quote_value"] = _quote_value((8, 0, 0))
    w = LL.OnDemandLegWorker(builders={"raydium_launchlab": builder}, admission_fn=lambda b, q: None, snapshot_fn=_snap,
                             clip_fn=lambda: 100.0, offer_fn=lambda b, r: results.append(r),
                             jup_call=_jup(calls, out=44_000_000, gate=gate))
    buy = L.launchlab_slot_buy(_routed(NVDA_B))
    t0 = time.perf_counter()
    taken = [w.offer(buy) for _ in range(3)]
    assert (time.perf_counter() - t0) < 0.05                   # offer() returned while legs block
    assert taken == [True, True, False] and w.stats["busy"] == 1
    gate.set()
    for _ in range(100):
        if len(results) == 2:
            break
        time.sleep(0.02)
    assert len(results) == 2 and w.offer(buy) is True            # a slot freed after each result


def test_quote_facts_refuse_only_what_stops_a_transfer():
    def mint(*exts):
        return {"data": {"parsed": {"info": {"supply": "1000", "decimals": 8, "extensions": [
            {"extension": n, "state": st} for n, st in exts]}}}}
    assert LL.quote_facts(mint(("permanentDelegate", {"delegate": "X"}),
                               ("scaledUiAmountConfig", {}), ("transferHook", {"programId": None}))) == (8, 0, 0)
    for bad in (("transferHook", {"programId": "HookProgram1"}), ("nonTransferable", {}),
                ("defaultAccountState", {"accountState": "frozen"}), ("pausableConfig", {"paused": True})):
        assert LL.quote_facts(mint(bad)) is None


def test_the_heartbeat_names_the_on_demand_counters(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "ondemand_leg": {"offered": 5, "offered_raydium_cpmm": 2, "offered_raydium_launchlab": 3,
                         "busy": 1, "leg_failed": 0, "built": 3, "refused": 1,
                         "leg_rate_limited": 1, "leg_backoff": 2, "x": 9}})
    assert json.loads(path.read_text())["slot_lane"]["ondemand_leg"] == {
        "offered": 5, "offered_raydium_cpmm": 2, "offered_raydium_launchlab": 3, "busy": 1,
        "leg_failed": 0, "built": 3, "refused": 1, "leg_rate_limited": 1, "leg_backoff": 2}


def test_no_shadow_path_reaches_the_funded_jupiter_seam(monkeypatch):
    """Alpha 2026-09-27: while LaunchLab is shadow-only, the on-demand leg's client is the paper key;
    a funded-seam call from here would cool the funded hosts on a 429."""
    import sniper.execution.pump_curve_buy as P
    monkeypatch.setattr(P, "_funded_jup_call", lambda *a, **k: pytest.fail("shadow reached the funded seam"))
    paper = []
    monkeypatch.setattr(LL, "paper_jup_call", lambda m, p, b=None: paper.append(p) or _jup([], out=44_000_000)(m, p, b))
    builder = _builder(None, NVDA_B)
    builder.pools._facts[NVDA_B["ix_accounts"][4]]["quote_value"] = _quote_value((8, 0, 0))
    w = LL.OnDemandLegWorker(builders={"raydium_launchlab": builder}, admission_fn=lambda b, q: None, snapshot_fn=_snap,
                             clip_fn=lambda: 100.0, offer_fn=None, start=False)      # default client
    assert w.process(L.launchlab_slot_buy(_routed(NVDA_B)))["reason"].startswith("built")
    assert len(paper) == 2


def test_a_rate_limited_leg_skips_the_event_and_backs_off_sixty_seconds():
    class Clock:
        t = 100.0

        @classmethod
        def monotonic(cls):
            return cls.t
    calls = []
    w, _ = _worker(NVDA_B, calls, out=44_000_000)
    w.clock = Clock
    w.jup_call = lambda m, p, b=None: calls.append(p) or (429, None)
    buy = L.launchlab_slot_buy(_routed(NVDA_B))
    assert w.process(buy)["reason"] == "slot_lane_launchlab_leg_rate_limited" and len(calls) == 1
    Clock.t = 159.0
    assert w.process(buy)["reason"] == "slot_lane_launchlab_leg_backoff" and len(calls) == 1
    Clock.t = 160.5
    w.jup_call = _jup(calls, out=44_000_000)
    assert w.process(buy)["reason"].startswith("built")
    assert (w.stats["leg_rate_limited"], w.stats["leg_backoff"]) == (1, 1)


def test_the_paper_client_is_the_paper_key_on_api_jup_ag_and_never_keyless(monkeypatch):
    import io
    import urllib.request
    seen = {}
    monkeypatch.delenv("MEMECOIN_PAPER_JUPITER_API_KEY", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("keyless call"))
    assert LL.paper_jup_call("GET", "/quote?x=1") == (0, None)
    monkeypatch.setenv("MEMECOIN_PAPER_JUPITER_API_KEY", "paper-test-key")

    class Resp(io.BytesIO):
        status = 200

    def urlopen(req, timeout):
        seen.update(url=req.full_url, key=req.get_header("X-api-key"), timeout=timeout)
        return Resp(b'{"outAmount": "5"}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert LL.paper_jup_call("GET", "/quote?x=1") == (200, {"outAmount": "5"})
    assert seen == {"url": "https://api.jup.ag/swap/v1/quote?x=1", "key": "paper-test-key", "timeout": 3.0}


# --- the LaunchLab live path, built with the live list EMPTY (as CPMM's (ii)) ----------------------

class _RoutedRows:
    def __init__(self):
        self.written = []

    def write(self, routed):
        self.written.append(routed.signature)
        return "inserted"


def test_the_quote_comes_from_the_pool_cache_and_an_unknown_pool_is_shadow(monkeypatch):
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    monkeypatch.setattr(LL, "LAUNCHLAB_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    known = L.launchlab_slot_buy(_routed(USDC_B), _cache(USDC_B))
    assert known.quote_mint == C.USDC_MINT and L.shadow_only(known) is False
    unknown = L.launchlab_slot_buy(_routed(USDC_B), LL.LaunchLabPoolCache(rpc=None))
    assert unknown.quote_mint is None and L.shadow_only(unknown) is True


def test_the_launchlab_live_list_is_sol_and_usdc_and_an_exotic_quote_stays_shadow(monkeypatch):
    """Pinned (2026-09-28, operator "go live"): LaunchLab sends SOL- and USDC-quoted buys only. An
    exotic quote (NVDAx here) stays shadow: its routed row goes first, as before."""
    assert LL.LAUNCHLAB_LIVE_QUOTES == frozenset({C.WSOL_MINT, C.USDC_MINT})
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    assert L.shadow_only(L.launchlab_slot_buy(_routed(USDC_B), _cache(USDC_B))) is False
    assert L.shadow_only(L.launchlab_slot_buy(_routed(SOL_B), _cache(SOL_B))) is False
    rows, put = _RoutedRows(), []
    L.route_routed_buy(_routed(NVDA_B), handoff=rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                       launchlab_on=True, launchlab_pools=_cache(NVDA_B))
    assert rows.written == ["SIG"] and [b.venue for b in put] == ["raydium_launchlab"]
    assert L.shadow_only(put[0]) is True


def test_a_live_launchlab_quote_goes_to_the_lane_only_and_writes_no_routed_row(monkeypatch):
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    monkeypatch.setattr(LL, "LAUNCHLAB_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    rows, put = _RoutedRows(), []
    L.route_routed_buy(_routed(USDC_B), handoff=rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                       launchlab_on=True, launchlab_pools=_cache(USDC_B))
    assert rows.written == [] and [b.venue for b in put] == ["raydium_launchlab"]
    # SOL is not on the list: routed row first, shadow build
    L.route_routed_buy(_routed(SOL_B), handoff=rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                       launchlab_on=True, launchlab_pools=_cache(SOL_B))
    assert rows.written == ["SIG"] and L.shadow_only(put[-1]) is True


@pytest.mark.parametrize("result,writes", [
    ({"status": "rejected", "reason": "not_approved"}, True),
    ({"status": "rejected", "reason": "slot_lane_stale_blockhash"}, True),
    ({"status": "rejected", "reason": "pool_unknown_learning"}, False),
    ({"status": "aborted", "reason": "build refused", "pending_id": 3}, False),
    ({"status": "filled", "pending_id": 3, "position_id": 1}, False),
])
def test_a_live_launchlab_refusal_before_reserving_writes_its_routed_row_once(monkeypatch, result, writes):
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    monkeypatch.setattr(LL, "LAUNCHLAB_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    rows = _RoutedRows()
    lane = L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live", routed_handoff=rows)
    lane._routed_fallback(L.launchlab_slot_buy(_routed(USDC_B), _cache(USDC_B)), dict(result))
    assert rows.written == (["SIG"] if writes else [])
    lane._routed_fallback(L.launchlab_slot_buy(_routed(SOL_B), _cache(SOL_B)), dict(result))   # SOL: never
    assert rows.written == (["SIG"] if writes else [])


def test_a_live_launchlab_buy_runs_the_funded_path_with_a_validity_the_executor_reads(tmp_path, monkeypatch):
    """Real ledger, reservation and attempt row; fake executor. A USDC-quoted census buy."""
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    import contextlib
    import dataclasses
    import sqlite3
    import time
    from solders.message import MessageV0
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    monkeypatch.setattr(LL, "LAUNCHLAB_LIVE_QUOTES", frozenset({C.USDC_MINT}))
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    builder = _builder(None, USDC_B)
    buy = L.launchlab_slot_buy(_routed(USDC_B), builder.pools)
    _r, facts = LL.market_facts(buy, builder.pools, 150.0)
    assert _r is None, _r
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (None, dict(facts)))
    seen, rows = {}, _RoutedRows()

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
                          build_fn=L.dispatch_build({"raydium_launchlab": builder}), snapshot_fn=lambda: snap,
                          armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                          now_iso_fn=lambda: "2026-09-28T00:10:00+00:00", lock_key="sol-test", mode="live",
                          routed_handoff=rows)
        result = lane.handle(dataclasses.replace(buy, recv_monotonic=now))
        assert result["status"] == "filled", result
        v, pending = seen["validity"], seen["pending"]
        assert isinstance(seen["message"], MessageV0)
        assert v["target_mint"] == pending["mint"] == USDC_B["ix_accounts"][9]
        assert v["blockhash"] == BLOCKHASH and v["route_target"].endswith(USDC_B["ix_accounts"][4])
        assert v["pump_price_limit"] and v["min_tokens_out"] <= v["expected_tokens_out"]
        assert pending["origin_event_id"] == "slotlane:SIG" and rows.written == []
        row = c.execute("SELECT terminal_status, signal_source FROM memecoin_entry_attempts").fetchone()
        assert tuple(row) == ("filled", "slot_lane")


def test_the_quote_refusal_line_names_the_learned_pools_quote():
    """The first live LaunchLab lines (2026-09-28T02:02:46Z) printed quote=None: the refusal now
    carries the learned pool's quote mint, so the read counts unsupported quotes by asset."""
    cache = _cache(NVDA_B)
    buy = L.launchlab_slot_buy(_routed(NVDA_B))
    reason, facts = LL.market_facts(buy, cache, 122.5)
    assert reason == "slot_lane_launchlab_quote_not_supported" and facts == {"quote": NVDA_B["ix_accounts"][10]}
    line = LL.shadow_line(buy, {"status": "rejected", "reason": reason, **facts}, None)
    assert "-> slot_lane_launchlab_quote_not_supported" in line and "quote=Xsc9qvGR" in line


def test_the_launchlab_on_demand_quote_takes_the_fee_at_the_cached_epoch():
    changing = _quote_value((6, 100, 2**64 - 1))
    changing["data"]["parsed"]["info"]["extensions"][0]["state"]["newerTransferFee"].update(
        epoch=1043, transferFeeBasisPoints=300)
    builder = _builder(None, NVDA_B)
    builder.pools._facts[NVDA_B["ix_accounts"][4]]["quote_value"] = changing
    buy = L.launchlab_slot_buy(_routed(NVDA_B))
    assert builder.ondemand_quote(buy) == (NVDA_B["ix_accounts"][10], None, None)      # epoch unknown
    C.refresh_epoch(lambda m, p: {"epoch": 1043, "slotIndex": 0, "slotsInEpoch": 432000})
    assert builder.ondemand_quote(buy) == (NVDA_B["ix_accounts"][10], (6, 300, 2**64 - 1), None)


def test_a_tip_is_appended_last_and_passes_the_sender_guard():
    from sniper.execution import solana_sender_ab as S
    b = _builder(lambda mint: _leg(100.0) if mint == C.WSOL_MINT else None, SOL_B)
    plain, _ = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap())
    msg, detail = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap(),
                          tip=(S.TIP_ACCOUNTS[1], S.TIP_LAMPORTS))
    assert msg is not None, detail
    assert S.tip_transfers(msg) == [(S.TIP_ACCOUNTS[1], 5_000)]
    assert S.guard_refusal(msg, 5_000) is None
    assert len(msg.instructions) == len(plain.instructions) + 1


def test_a_treated_crowded_send_prices_the_target_from_this_builds_cu_limit(monkeypatch):
    """Crowded fee on every slot-lane venue (Alpha GO 2026-10-16): LaunchLab takes the treated priority."""
    from sniper.settings import memecoin as cfg
    from test_memecoin_crowded_fee import budget, fee_lamports
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000)
    b = _builder(lambda mint: _leg(100.0) if mint == C.WSOL_MINT else None, SOL_B)
    assert b.accepts_priority_lamports is True
    plain, plain_detail = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap())
    msg, detail = b.build(L.launchlab_slot_buy(_routed(SOL_B)), 100.0, _snap(), priority_lamports=10_000_000)
    limit, price = budget(msg)
    assert limit == detail["cu_limit"] and fee_lamports(limit, price) >= 10_000_000
    assert detail["priority_bid"]["crowded"] == "raised" and "priority_bid" not in plain_detail
    assert budget(plain)[1] < price
