"""Meteora DAMM v2 on the slot lane, B2 (spec 2026-10-01-memecoin-slot-lane-routed-venues-design.md).

The rows are REAL mainnet reads (tests/fixtures/memecoin/damm_v2_sol_pools.json): two SOL-quoted pools
from the routed-handoff census, one classic coin and one Token-2022 coin. A swap built by
`swap_instruction` on each simulated clean on mainnet on 2026-10-01 (err None).

DAMM v2 emits its swap event by self-CPI, not in the logs, so the lane learns a coin's pool from a
first sighting's confirmed transaction and prices from the source's own average (USDC spent / tokens
received). MEMECOIN_SLOT_LANE_DAMM_V2: off (default) | shadow | live.
"""
import base64
import contextlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from solders.transaction import VersionedTransaction
from solders.null_signer import NullSigner

from sniper.execution import meteora_damm_v2_buy as D
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as s
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/damm_v2_sol_pools.json").read_text())
CLASSIC, T22 = FX["pools"]
USER = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
BLOCKHASH = "11111111111111111111111111111111"


class FakeRpc:
    """`rpc(method, params)` -> the fixture's answer; `tx_answers` lets a first read be unconfirmed."""

    def __init__(self, row, tx_answers=None):
        self.row, self.calls = row, []
        self.tx_answers = list(tx_answers) if tx_answers is not None else None

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getTransaction":
            if self.tx_answers:
                return self.tx_answers.pop(0)
            return self.row["tx"]
        if method == "getAccountInfo":
            return {"value": self.row["pool_account"]}
        if method == "getMultipleAccounts":
            return {"value": self.row["mints"]}
        raise AssertionError(method)


def _learned(row):
    cache = D.DammV2PoolCache(rpc=FakeRpc(row))
    assert cache.learn_from_signature(row["signature"], row["coin"]) is True
    return cache


def _routed(row, *, usdc=100_000_000, tokens=None, events=(D.DammV2Seen(),)):
    # $100 for 10M whole tokens (6 dp) = $0.00001 a token; ~1e9 supply -> ~$10k mcap
    tokens = 10_000_000 * 10 ** 6 if tokens is None else tokens
    return s.RoutedBuy(source_user_id="w1", handle="h1", owner="Owner1", signature=row["signature"],
                       slot=row["slot"], mint=row["coin"], token_delta_atomic=tokens, token_decimals=6,
                       usdc_spent_atomic=usdc, recv_monotonic=time.monotonic(),
                       recv_utc="2026-10-01T20:30:00+00:00", paired_monotonic=time.monotonic(),
                       venue_events=tuple(events))


# --- the chain facts ------------------------------------------------------------------------------

def test_a_routed_swap2_source_teaches_its_pool():
    # 2026-10-02 08:00:01Z and 08:01:18Z: LingardSol bought 9txCZaqY through DFlow, whose CPI into
    # cp-amm is `swap2` (same 14 accounts as `swap`). The learner matched `swap` only, so both routed
    # buys logged pool_unknown:queued and the pool was never learned (B2 read, Lambda).
    row = json.loads((Path(__file__).parent / "fixtures/memecoin/damm_v2_swap2_routed.json").read_text())
    assert D.pool_from_transaction(row["tx"], row["coin"]) == row["pool"]


def test_a_source_with_no_cp_amm_swap_on_the_coin_is_counted():
    # A learn that finds no swap used to leave every counter alone: queued 2, learned 0, failed 0.
    cache = D.DammV2PoolCache(rpc=FakeRpc(CLASSIC))
    assert cache.queue(CLASSIC["signature"], "OtherCoin1111111111111111111111111111111111") == "queued"
    cache.learn_pending()
    assert cache.stats["not_learnable"] == 1 and cache.stats["learned"] == 0 and not cache._pending


@pytest.mark.parametrize("row", FX["pools"], ids=["classic", "token2022"])
def test_the_pool_is_found_in_the_source_transaction_and_its_account_parses(row):
    assert D.pool_from_transaction(row["tx"], row["coin"]) == row["pool"]
    assert D.pool_from_transaction(row["tx"], "OtherCoin1111111111111111111111111111111111") is None
    st = D.parse_pool(base64.b64decode(row["pool_account"]["data"][0]))
    assert st["mint_a"] == row["coin"] and st["mint_b"] == D.WSOL_MINT
    with pytest.raises(ValueError):
        D.parse_pool(b"\x00" * D.POOL_LEN)


def test_the_logs_show_only_that_cp_amm_ran():
    logs = CLASSIC["tx"]["meta"]["logMessages"]
    assert D.damm_v2_invoked(logs)
    assert not any(line.startswith("Program data:") for line in logs
                   if "cpamdp" in line), "no event in the logs: emit_cpi"
    frame = {"params": {"result": {"value": {"logs": logs}}}}
    assert s._damm_v2_seen(frame, True) == (D.DammV2Seen(),)
    assert s._damm_v2_seen(frame, False) == ()
    assert s._damm_v2_seen({"params": {"result": {"value": {"logs": ["Program x invoke [1]"]}}}}, True) == ()


@pytest.mark.parametrize("row,program", [(CLASSIC, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                                         (T22, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")],
                         ids=["classic", "token2022"])
def test_a_first_sighting_learns_the_coins_pool_off_the_signal_path(row, program):
    cache = _learned(row)
    facts = cache.get(row["coin"])
    assert facts["pool"] == row["pool"] and facts["quote"] == D.WSOL_MINT
    assert facts["program_a"] == program and facts["coin_facts"][1] == 6
    assert facts["coin_refusal"] is None


def test_an_unconfirmed_first_sighting_is_retried_then_dropped():
    rpc = FakeRpc(CLASSIC, tx_answers=[None] * D.LEARN_TRIES)
    cache = D.DammV2PoolCache(rpc=rpc)
    cache.queue(CLASSIC["signature"], CLASSIC["coin"])
    for _ in range(D.LEARN_TRIES - 1):
        cache.learn_pending()
        assert CLASSIC["coin"] in cache._pending and cache.get(CLASSIC["coin"]) is None
    cache.learn_pending()
    assert CLASSIC["coin"] not in cache._pending


def test_a_late_confirmation_is_learned_on_a_retry():
    cache = D.DammV2PoolCache(rpc=FakeRpc(CLASSIC, tx_answers=[None]))
    cache.queue(CLASSIC["signature"], CLASSIC["coin"])
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"]) is None
    cache.learn_pending()
    assert cache.get(CLASSIC["coin"])["pool"] == CLASSIC["pool"]


# --- conversion and admission -----------------------------------------------------------------------

def test_conversion_needs_the_marker_and_a_learned_pool():
    cache = D.DammV2PoolCache(rpc=FakeRpc(CLASSIC))
    assert D.damm_slot_buy(_routed(CLASSIC, events=()), cache) == (None, None)
    assert D.damm_slot_buy(_routed(CLASSIC), cache) == (None, "pool_unknown:queued")
    assert CLASSIC["coin"] in cache._pending, "the first sighting is queued for learning"
    cache = _learned(CLASSIC)
    buy, why = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert why is None and buy.venue == "meteora_damm_v2" and buy.pool == CLASSIC["pool"]
    assert buy.quote_mint == D.WSOL_MINT and buy.routed is not None and buy.mint == CLASSIC["coin"]


def test_market_facts_price_the_source_average_and_refuse_another_quote():
    cache = _learned(CLASSIC)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    reason, facts = D.market_facts(buy, cache, 150.0)
    assert reason is None and facts["price_usd"] == pytest.approx(1e-5)
    assert facts["their_usd"] == pytest.approx(100.0)
    supply = int(CLASSIC["mints"][0]["data"]["parsed"]["info"]["supply"])
    assert facts["mcap_usd"] == pytest.approx(1e-5 * supply / 1e6)
    cache.get(CLASSIC["coin"])["quote"] = "METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL"   # LILMET's
    assert D.market_facts(buy, cache, 150.0)[0] == "slot_lane_damm_v2_quote_not_supported"


# --- the build --------------------------------------------------------------------------------------

def _builder(row, *, leg_age=0.0):
    b = D.SlotLaneDammV2Builder(user=USER, rpc=FakeRpc(row), leg_fn=None, clock=lambda: 1000.0,
                                start_learner=False)
    b.pools = _learned(row)
    leg = {"ixs": [], "tables": [], "usdc_atomic": 100_000_000, "min_out_lamports": 600_000_000,
           "out_lamports": 610_000_000, "fetched_at": 1000.0 - leg_age, "quote": {}}
    b.leg_fn = lambda: leg
    return b, leg


@pytest.mark.parametrize("row", FX["pools"], ids=["classic", "token2022"])
def test_the_build_signs_the_leg_amount_at_the_price_floor_in_the_verified_layout(row, monkeypatch):
    from sniper.execution.pump_curve_buy import source_price_floor_atomic
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b, leg = _builder(row)
    buy, _ = D.damm_slot_buy(_routed(row), b.pools)
    message, validity = b.build(buy, 100.0, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1,
                                                            sol_usd=150.0))
    assert message is not None, validity
    keys = [str(k) for k in message.account_keys]
    swaps = [ix for ix in message.instructions if keys[ix.program_id_index] == D.PROGRAM]
    (swap,) = swaps
    data = bytes(swap.data)
    import struct
    amount_in, min_out = struct.unpack("<QQ", data[8:24])
    floor = source_price_floor_atomic(100_000_000, 1e-5, 30.0, base_decimals=6)
    assert data[:8] == D.SWAP_DISCRIMINATOR and amount_in == leg["min_out_lamports"] and min_out == floor
    facts = b.pools.get(row["coin"])
    accounts = [keys[i] for i in bytes(swap.accounts)]
    assert accounts[:2] == [D.POOL_AUTHORITY, row["pool"]]
    assert accounts[4:8] == [facts["vault_a"], facts["vault_b"], facts["mint_a"], facts["mint_b"]]
    assert accounts[8] == USER and accounts[11:] == [D.PROGRAM, D.EVENT_AUTHORITY, D.PROGRAM]
    assert validity["min_tokens_out"] == floor and validity["target_mint"] == row["coin"]
    assert validity["pump_price_limit"]["cap_pct"] == 30.0, "the chase cap goes to the trace"
    wire = base64.b64decode(validity["unsigned_b64"])
    assert len(wire) <= 1232


def test_a_stale_leg_or_native_funding_refuses_the_build():
    b, _ = _builder(CLASSIC, leg_age=60.0)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), b.pools)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    assert b.build(buy, 100.0, snap) == (None, "leg_stale")
    b, _ = _builder(CLASSIC)
    assert b.build(buy, 100.0, snap, funding_lamports=1) == (
        None, "native_funding_not_supported_on_damm_v2")


# --- the lane --------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _lane(conn, tmp_path, monkeypatch, *, mode, sent):
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0),
                        ("MEMECOIN_REAL_MAX_MCAP_USD", 1_000_000.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {}),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0), ("MEMECOIN_SLOT_LANE_DAMM_V2", mode)):
        monkeypatch.setattr(cfg, name, value, raising=False)
    b, _ = _builder(CLASSIC)

    def slot_execute_fn(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None,
                        boundary_fn=None, entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        sent.append({"pending": dict(pending), "validity": validity})
        return {"status": "filled", "position_id": 1}
    snap = lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5,
                                 blockhash=BLOCKHASH, last_valid_height=1,
                                 observed_mono={k: time.monotonic() for k in
                                                ("sol_usd", "balances", "blockhash")})
    return lane.SlotLane(conn=conn, executor={"slot_execute_fn": slot_execute_fn},
                         build_fn=lane.dispatch_build({"meteora_damm_v2": b}),
                         snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"w1"},
                         kill_tripped_fn=lambda: False,
                         now_iso_fn=lambda: "2026-10-01T20:30:00+00:00", lock_key="sol-test",
                         mode="live", shadow_clip_fn=lambda: 100.0, damm_pools=b.pools), b


def test_shadow_builds_and_hands_the_bytes_to_the_verifier_without_reserving(conn, tmp_path,
                                                                             monkeypatch):
    sent = []
    ln, b = _lane(conn, tmp_path, monkeypatch, mode="shadow", sent=sent)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), b.pools)
    assert lane.shadow_only(buy)
    result = ln.handle(buy)
    assert result["status"] == "shadow" and result["reason"].startswith("built min=")
    assert result["unsigned_b64"] and result["min_tokens_out"]
    assert sent == [] and conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_missing_output_quote_remains_a_mechanical_backup_class(conn, tmp_path, monkeypatch):
    from sniper.execution import pump_curve_buy as pcb
    from sniper.services.memecoin_onchain_lane import slot_lane_verdict
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(pcb, "leg_clip_refusal", lambda leg, clip: None)  # the shared lane fixture
    sent = []
    ln, b = _lane(conn, tmp_path, monkeypatch, mode="live", sent=sent)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), b.pools)
    result = ln.handle(buy)
    assert result["status"] == "aborted", result
    assert result["reason"] == "slot lane build refused: output_quote_unavailable"
    assert sent == []
    assert slot_lane_verdict(conn, buy.signature, "2026-10-01T20:30:00+00:00") == ("failed", "build_refused")


def test_live_reserves_builds_and_sends_under_the_venue_cap(conn, tmp_path, monkeypatch):
    from sniper.execution import pump_curve_buy as pcb
    monkeypatch.setattr(pcb, "leg_clip_refusal", lambda leg, clip: None)   # the leg is a fixture
    sent = []
    ln, b = _lane(conn, tmp_path, monkeypatch, mode="live", sent=sent)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), b.pools)
    assert not lane.shadow_only(buy)
    result = ln.handle(buy)
    assert result["status"] == "filled", result
    (call,) = sent
    assert call["pending"]["mint"] == CLASSIC["coin"] and call["pending"]["signal_source"] == "slot_lane"
    assert call["validity"]["pool"] == CLASSIC["pool"]
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 1, raising=False)
    other = D.damm_slot_buy(_routed(CLASSIC), b.pools)[0]
    assert ln.handle(other)["reason"] == "slot_lane_venue_live_cap"


class RoutedRows:
    def __init__(self):
        self.written = []

    def write(self, buy):
        self.written.append(buy.signature)
        return "inserted"


@pytest.mark.parametrize("mode,known,expect_lane", [
    ("live", True, True), ("live", False, False), ("shadow", True, False)])
def test_one_routed_buy_one_funded_path(monkeypatch, mode, known, expect_lane):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", mode, raising=False)
    pools = _learned(CLASSIC) if known else D.DammV2PoolCache(rpc=FakeRpc(CLASSIC))
    puts, lines, rows = [], [], RoutedRows()
    lane.route_routed_buy(_routed(CLASSIC), handoff=rows, log=lines.append, put=puts.append,
                          amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                          damm_on=True, damm_pools=pools)
    if expect_lane:
        assert [p.venue for p in puts] == ["meteora_damm_v2"] and rows.written == []
    else:
        assert rows.written == [CLASSIC["signature"]]
        assert all(lane.shadow_only(p) for p in puts), "a shadow build is never a funded path"
    if not known:
        assert CLASSIC["coin"] in pools._pending


def test_the_shadow_verifier_simulates_once_and_logs_whether_it_would_land():
    calls, lines = [], []

    def sim_rpc(method, params):
        calls.append((method, params[1]))
        return {"context": {"slot": 9}, "value": {"err": None, "unitsConsumed": 41365, "logs": []}}
    v = D.ShadowVerifier(log=lines.append, sim_rpc=sim_rpc, start=False)
    buy = SimpleNamespace(handle="h1", mint=CLASSIC["coin"], pool=CLASSIC["pool"], slot=1,
                          signature="Sig", recv_utc="t")
    v.verify_once(buy, {"reason": "built", "unsigned_b64": "AAAA", "min_tokens_out": 5})
    (method, opts), = calls
    assert method == "simulateTransaction" and opts["sigVerify"] is False
    assert "sim_ok=True" in lines[0] and "sim_units=41365" in lines[0] and v.stats["sim_ok"] == 1


NO_ANSWER = {"null_result": {"result": None}, "null_value": {"context": {"slot": 9}, "value": None},
             "absent_value": {"context": {"slot": 9}}, "absent_err": {"value": {"unitsConsumed": 7}},
             "value_not_a_dict": {"value": "ok"}, "error_envelope": {"error": {"code": -32005}},
             "raises": RuntimeError("timeout")}


@pytest.mark.parametrize("answer", sorted(NO_ANSWER))
def test_the_shadow_verifier_never_counts_no_answer_as_a_landing(answer):
    """SOL-2 (Codex review 2026-10-06): a null or missing simulation value, or one with no err key,
    is no answer. It counted sim_ok, so shadow evidence overstated success."""
    def sim_rpc(method, params):
        if isinstance(NO_ANSWER[answer], Exception):
            raise NO_ANSWER[answer]
        return NO_ANSWER[answer]
    v = D.ShadowVerifier(log=lambda _line: None, sim_rpc=sim_rpc, start=False)
    assert v._simulate({"unsigned_b64": "AAAA"}) is None
    assert v.stats["sim_ok"] == 0 and v.stats["sim_err"] == 0 and v.stats["sim_failed"] == 1


def test_the_shadow_verifier_counts_an_explicit_program_error():
    err = {"InstructionError": [3, {"Custom": 6004}]}
    v = D.ShadowVerifier(log=lambda _line: None, start=False,
                         sim_rpc=lambda m, p: {"context": {"slot": 9}, "value": {"err": err}})
    assert v._simulate({"unsigned_b64": "AAAA"}) == (False, err, None, 9)
    assert v.stats["sim_err"] == 1 and v.stats["sim_ok"] == 0


# --- the Token-2022 extension guard (Gamma MEDIUM on 87a4a017c) ---------------------------------------
# The slot lane runs no token-safety simulation; the coin's extension check is the only guard. A
# permanent delegate can seize the tokens, a transfer hook gates them, a pausable mint can freeze them.

EXTENSIONS = {
    "permanentDelegate": {"extension": "permanentDelegate",
                          "state": {"delegate": "Dele11111111111111111111111111111111111111"}},
    "transferHook": {"extension": "transferHook",
                     "state": {"authority": None, "programId": "Hook1111111111111111111111111111111111111"}},
    "pausableConfig": {"extension": "pausableConfig",
                       "state": {"authority": None, "paused": False}},
}


def _with_extension(row, name):
    import copy
    row = copy.deepcopy(row)
    info = row["mints"][0]["data"]["parsed"]["info"]
    info["extensions"] = list(info.get("extensions") or []) + [EXTENSIONS[name]]
    return row


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_a_coin_with_an_uncovered_extension_is_handed_off_and_never_built(name, monkeypatch, conn,
                                                                         tmp_path):
    row = _with_extension(T22, name)
    cache = _learned(row)
    assert cache.get(row["coin"])["coin_refusal"] == "extension_" + name
    # the routed buy: handed off as today, never put on the lane, in live mode
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    assert D.damm_slot_buy(_routed(row), cache) == (None, "coin_extension_" + name)
    puts, rows = [], RoutedRows()
    lane.route_routed_buy(_routed(row), handoff=rows, log=lambda line: None, put=puts.append,
                          amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                          damm_on=True, damm_pools=cache)
    assert puts == [] and rows.written == [row["signature"]]
    # the production build path refuses it too, and the lane reserves nothing
    sent = []
    ln, b = _lane(conn, tmp_path, monkeypatch, mode="live", sent=sent)
    b.pools = cache
    ln.damm_pools = cache
    from sniper.services.memecoin_slot_signal import SlotBuy
    forced = SlotBuy(venue="meteora_damm_v2", source_user_id="w1", handle="h1", owner="Owner1",
                     signature=row["signature"], slot=row["slot"], mint=row["coin"],
                     pool=row["pool"], sol_amount=100_000_000, token_amount=1, virtual_sol_reserves=None,
                     virtual_token_reserves=None, real_sol_reserves=0, real_token_reserves=0,
                     token_program=None, trade_event_log="", recv_monotonic=time.monotonic(),
                     recv_utc="2026-10-01T20:30:00+00:00", routed=_routed(row), quote_mint=D.WSOL_MINT)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    assert b.build(forced, 100.0, snap) == (None, "slot_lane_damm_v2_coin_extension_" + name)
    result = ln.handle(forced)
    assert result["status"] == "rejected" and result["reason"] == "slot_lane_damm_v2_coin_extension_" + name
    assert sent == [] and conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


# --- startup lines and the heartbeat on the production path (c3966171; Gamma LOW on 3d45455fb) -----

def _start_production_lane(monkeypatch, caplog, raw_caps="meteora_damm_v2=2"):
    """start_slot_lane as the runner calls it, with a real DAMM v2 pool cache and verifier (no
    threads start); returns the log lines, the WARNINGs and the status the heartbeat reads."""
    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "shadow")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 6)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE",
                        cfg._parse_venue_caps(raw_caps))
    damm = SimpleNamespace(pools=D.DammV2PoolCache(rpc=None), sim_rpc=None)
    lines = []
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    with caplog.at_level("WARNING", logger=lane.__name__):
        status = lane.start_slot_lane(
            rc, builders={"pump_curve": object(), "meteora_damm_v2": damm},
            signal_start_fn=lambda *a, **k: None, sol_usd_fn=lambda: None, log=lines.append,
            roster_path="unused", conn_factory=object, readonly_conn_factory=object)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    return lines, warnings, status


def test_the_lane_start_logs_one_line_per_enabled_venue_with_its_mode_and_cap(monkeypatch, caplog):
    lines, warnings, _ = _start_production_lane(monkeypatch, caplog)
    venue_lines = [line for line in lines if line.startswith("[memecoin] slot lane venue ")]
    assert venue_lines == [
        "[memecoin] slot lane venue meteora_damm_v2 mode=shadow lane_mode=live live_cap_per_hour=2",
        "[memecoin] slot lane venue pump_curve mode=live lane_mode=live"], lines
    assert warnings == []


@pytest.mark.parametrize("raw", ["meteora_dammv2=2", "damm_v2=2", "meteora_damm_v2:2", "=2"])
def test_a_cap_override_naming_no_venue_warns_and_the_venue_keeps_the_default(monkeypatch, caplog,
                                                                             raw):
    lines, warnings, _ = _start_production_lane(monkeypatch, caplog, raw)
    assert any(line.endswith("meteora_damm_v2 mode=shadow lane_mode=live live_cap_per_hour=6")
               for line in lines), lines
    assert len(warnings) == 1 and repr(raw.split("=")[0] if raw != "=2" else raw) in warnings[0]
    assert "names no slot-lane venue" in warnings[0], warnings


def test_the_production_heartbeat_carries_the_damm_v2_shadow_counters(monkeypatch, caplog, tmp_path):
    from sniper.services import memecoin_runner as run
    _, _, status = _start_production_lane(monkeypatch, caplog)
    status["damm_v2_shadow"]["sim_ok"] = 3          # the verifier's and the cache's own dicts
    status["damm_v2_pools"]["not_confirmed"] = 2
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status=status)
    assert json.loads(path.read_text())["slot_lane"]["damm_v2_shadow"] == {
        "logged": 0, "dropped": 0, "sim_ok": 3, "sim_err": 0, "sim_failed": 0, "sim_skipped": 0,
        "pools_learned": 0, "pools_learn_failed": 0, "pools_queued": 0, "pools_not_confirmed": 2,
        "pools_not_learnable": 0, "pools_warm_rows": 0, "pools_warm_loaded": 0, "pools_warm_dropped": 0,
        "pools_warm_unread": 0, "pools_rechecked": 0, "pools_repointed": 0, "pools_recheck_failed": 0,
        "pools_store_write_failed": 0, "pools_route_rows": None, "pools_route_scan_ok": None,
        "pools_route_scan_failed": 0, "pools_route_queued": 0, "pools_route_loaded": 0,
        "pools_route_read_failed": 0, "pools_route_dropped": 0,
        "pools_route_refused": 0, "pools_route_suppressed": 0}


# --- B2 outcome log (Alpha, 2026-10-02): a quiet shadow must be provably quiet ----------------------
# Rule-5: no money path (one log line on the routed thread, written AFTER every put and hand-off of
# that buy, so it never sits ahead of a fill); one damm_slot_buy per routed buy (it was called twice
# when the lane was live and the pool unknown); a frame without cp-amm logs nothing; rollback: revert.

def _damm_lines(lines):
    return [line for line in lines if "slot lane damm_v2 " in line]


@pytest.mark.parametrize("lane_live", [True, False])
def test_an_unknown_pool_logs_one_queued_line_and_is_converted_once(monkeypatch, lane_live):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "shadow", raising=False)
    pools = D.DammV2PoolCache(rpc=FakeRpc(CLASSIC))
    calls, real = [], D.damm_slot_buy
    monkeypatch.setattr(D, "damm_slot_buy", lambda r, p: calls.append(r.signature) or real(r, p))
    lines = []
    lane.route_routed_buy(_routed(CLASSIC), handoff=RoutedRows(), log=lines.append, put=lambda b: None,
                          amm_pools=None, cpmm_on=False, pump_on=False, lane_live=lane_live,
                          damm_on=True, damm_pools=pools)
    assert _damm_lines(lines) == ["[memecoin] slot lane damm_v2 pool_unknown:queued h1 %s sig=%s"
                                  % (CLASSIC["coin"][:8], CLASSIC["signature"])]
    assert pools.stats["queued"] == 1 and calls == [CLASSIC["signature"]]


def test_a_second_sighting_while_learning_says_pending():
    pools = D.DammV2PoolCache(rpc=FakeRpc(CLASSIC))
    assert D.damm_slot_buy(_routed(CLASSIC), pools) == (None, "pool_unknown:queued")
    assert D.damm_slot_buy(_routed(CLASSIC), pools) == (None, "pool_unknown:pending")


def test_a_built_buy_is_logged_after_it_is_put(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    events = []
    lane.route_routed_buy(_routed(CLASSIC), handoff=RoutedRows(), log=lambda line: events.append(("log", line)),
                          put=lambda b: events.append(("put", b.venue)), amm_pools=None, cpmm_on=False,
                          pump_on=False, lane_live=True, damm_on=True, damm_pools=_learned(CLASSIC))
    damm = [e for e in events if e[0] == "put" or "damm_v2 " in e[1]]
    assert damm == [("put", "meteora_damm_v2"),
                    ("log", "[memecoin] slot lane damm_v2 built h1 %s sig=%s"
                     % (CLASSIC["coin"][:8], CLASSIC["signature"]))]


def test_a_frame_without_cp_amm_logs_nothing():
    lines = []
    lane.route_routed_buy(_routed(CLASSIC, events=()), handoff=RoutedRows(), log=lines.append,
                          put=lambda b: None, amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                          damm_on=True, damm_pools=D.DammV2PoolCache(rpc=FakeRpc(CLASSIC)))
    assert _damm_lines(lines) == []


def test_a_coin_refusal_is_logged_with_its_reason():
    pools = _learned(CLASSIC)
    pools._by_coin[CLASSIC["coin"]] = dict(pools.get(CLASSIC["coin"]), coin_refusal="permanent_delegate")
    lines = []
    lane.route_routed_buy(_routed(CLASSIC), handoff=RoutedRows(), log=lines.append, put=lambda b: None,
                          amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                          damm_on=True, damm_pools=pools)
    assert _damm_lines(lines) == ["[memecoin] slot lane damm_v2 coin_permanent_delegate h1 %s sig=%s"
                                  % (CLASSIC["coin"][:8], CLASSIC["signature"])]


def test_a_tip_is_appended_last_and_passes_the_sender_guard():
    from sniper.execution import solana_sender_ab as S
    row = FX["pools"][0]
    b, _leg = _builder(row)
    buy, _ = D.damm_slot_buy(_routed(row), b.pools)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    plain, _ = b.build(buy, 100.0, snap)
    msg, detail = b.build(buy, 100.0, snap, tip=(S.TIP_ACCOUNTS[2], S.TIP_LAMPORTS))
    assert msg is not None, detail
    assert S.tip_transfers(msg) == [(S.TIP_ACCOUNTS[2], 5_000)]
    assert S.guard_refusal(msg, 5_000) is None
    assert len(msg.instructions) == len(plain.instructions) + 1


def _crowded_knobs(monkeypatch):
    for name, value in (("MEMECOIN_SLOT_LANE_CROWDED_RULE", "early"),
                        ("MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS", 10_000_000),
                        ("MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000),
                        ("MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS", 150_000_000)):
        monkeypatch.setattr(cfg, name, value)


def test_a_routed_early_damm_send_is_treated_and_signs_the_crowded_target(monkeypatch):
    """MIDWAY 7de9dad9 (2026-10-16): a routed DAMM v2 buy landed S+1 at index 52 on the 1.52M static bid,
    crowded_fee venue_not_in_scope and no head. Now: the routed pair's head, the treated arm, the build's
    own CU limit priced to the target, and the signer keeps it."""
    import dataclasses
    from sniper.execution.solana_sender_ab import cap_entry_priority_fee
    from sniper.services import memecoin_crowded_fee as CF
    from test_memecoin_crowded_fee import budget, fee_lamports
    _crowded_knobs(monkeypatch)
    monkeypatch.setattr(CF, "holdout", lambda attempt_id: False)
    row = FX["pools"][0]
    b, _leg = _builder(row)
    routed = _routed(row)
    buy, _ = D.damm_slot_buy(dataclasses.replace(routed, paired_head_slot=routed.slot - 1), b.pools)
    head = lane.receipt_head(buy)
    assert head["head_at_recv_minus_s"] == -1 and head["head_at_recv_basis"] == "routed_pair_complete"
    arm = CF.CrowdedFee(now_iso_fn=lambda: "2026-10-16T19:00:00+00:00", alert_fn=lambda *a: None).arm(
        buy, "attempt-1", head_minus_s=head["head_at_recv_minus_s"])
    assert arm["arm"] == "treated" and arm["target_lamports"] == 10_000_000
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    msg, detail = b.build(buy, 100.0, snap, priority_lamports=arm["target_lamports"])
    limit, price = budget(msg)
    assert fee_lamports(limit, price) >= 10_000_000 and detail["priority_bid"]["crowded"] == "raised"
    _signed, info = cap_entry_priority_fee(msg, tip_lamports=0, crowded_lamports=arm["target_lamports"])
    assert info["priority_fee_lamports"] >= 10_000_000 - 1 and info["cu_limit"] == limit


def test_a_routed_damm_buy_without_a_paired_head_bids_normally(monkeypatch):
    from sniper.services import memecoin_crowded_fee as CF
    _crowded_knobs(monkeypatch)
    row = FX["pools"][0]
    b, _leg = _builder(row)
    buy, _ = D.damm_slot_buy(_routed(row), b.pools)
    head = lane.receipt_head(buy)["head_at_recv_minus_s"]
    arm = CF.CrowdedFee(now_iso_fn=lambda: "2026-10-16T19:00:00+00:00", alert_fn=lambda *a: None).arm(
        buy, "attempt-1", head_minus_s=head)
    assert head is None and arm["arm"] is None and arm["reason"] == "head_unknown"


@pytest.mark.parametrize("tip_lamports", [5_000, 5_000_000])
def test_a_crowded_damm_build_never_passes_max_send_with_its_tip(monkeypatch, tip_lamports):
    from sniper.execution import solana_sender_ab as S
    from test_memecoin_crowded_fee import budget, fee_lamports
    _crowded_knobs(monkeypatch)
    row = FX["pools"][0]
    b, _leg = _builder(row)
    buy, _ = D.damm_slot_buy(_routed(row), b.pools)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    msg, detail = b.build(buy, 100.0, snap, tip=(S.TIP_ACCOUNTS[2], tip_lamports), priority_lamports=10_000_000)
    assert msg is not None, detail
    limit, price = budget(msg)
    assert fee_lamports(limit, price) + tip_lamports <= 12_000_000
