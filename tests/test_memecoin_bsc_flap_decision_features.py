"""BSC Flap lane decision features (spec 2026-10-14-fast-lane-decision-features-design.md section
4.3, Beta's mirror of Lambda's decision_features_v1). Measurement only: the source TokenBought
words the watcher already decodes, the hook's own stats read, and the curve state the Flap safety
check already reads. No gate, classification or funded contract reads any of it.

Gap (loss-pattern universe 10-01..10-14): 3 of 3 bsc_flap_fast_lane fills had none of pair age,
m5 volume or lifecycle, and nothing of the curve.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from eth_abi import decode

from sniper.execution import evm_flap_curve as C
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_leg as leg
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from test_memecoin_bsc_flap_fast_handoff import BOUGHT, EPOCH, FRAME, MINT, _enabled, _payload  # noqa: F401
from test_memecoin_evm_flap_curve import (NATIVE, TAKER, YEE, _fallback_recording,
                                          _matching_simulate_fn, _overrides_fn, _state, _wired)

AT = "2026-10-10T12:49:16+00:00"
WORDS = ["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"]
CURVE = ("curve_real_quote_atomic", "curve_real_token_atomic", "curve_virtual_quote_atomic",
         "curve_virtual_token_atomic", "curve_progress", "quote_mint", "quote_decimals")


def _words():
    trade = next(log for log in FRAME["receipt"]["logs"] if log["topics"][0] == BOUGHT
                 and decode(WORDS, bytes.fromhex(log["data"][2:]))[1].lower() == MINT)
    return decode(WORDS, bytes.fromhex(trade["data"][2:]))


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    return conn


def _lane_event(stats=({"volume_m5": 0.0, "liquidity": 9000.0}, EPOCH - 4)):
    """The event _handle_v3 hands the hook, after the hook's own stats read."""
    seen = []

    class Cache:
        def stats(self, mint, now):
            return stats

        def put_market_stats(self, mint, got, now):
            pass

    def factory(contexts, stats_fn):
        def hook(conn, ev):
            stats_fn(ev.mint, network_id="56")
            seen.append(ev)
            return {"status": "rejected", "reason": "fixture"}
        return hook
    live = object.__new__(bsc.BscLive)
    live.ctx, live.contexts, live.conn, live.cache = {}, {"56": {}}, None, Cache()
    live.host_clock, live.v3_hook_factory, live.quotes = (lambda: EPOCH + .1), factory, {}
    live.stats_fn, live.stopped = None, False
    live.handle(_payload(), recv_epoch=EPOCH + .2)
    return seen[0]


def _stamp(ev):
    conn = _conn()
    trace = EntryAttemptTrace.start(conn, ev, AT, SimpleNamespace(perf_counter=lambda: 1.0))
    trace.finish("rejected", "fixture")
    return json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])


def _curve_record(state, seconds_after):
    at = (datetime.fromisoformat(AT) + timedelta(seconds=seconds_after)).isoformat()
    return dict({k: state[k] for k in C.CURVE_FACT_FIELDS}, observed_at=at)


def test_the_watcher_forwards_the_source_token_bought_words_and_the_runner_accepts_them():
    payload, words = _payload(), _words()
    assert payload["source_quote_paid_atomic"] == words[4]
    assert payload["source_fee_atomic"] == words[5]
    assert payload["source_post_price_atomic"] == words[6]
    assert bsc.check_sender(payload, 7, os.getuid(), watcher_pid=7) is None


def test_a_flap_lane_attempt_stamps_decision_features_at_trace_finish():
    ev, state, words = _lane_event(), _state(), _words()
    ev.decision_features_sink["flap_curve_state"] = _curve_record(state, 2.0)
    got = _stamp(ev)["decision_features"]
    assert got["schema"] == "decision_features_v1" and got["lane"] == "bsc_flap_fast_lane"
    assert got["decision_at"] == AT and got["stats_source"] == "lane_cache"
    assert got["volume_m5_usd"] == 0.0 and got["liquidity_usd"] == 9000.0      # a known zero
    assert got["source_post_price_atomic"] == words[6] and got["source_fee_atomic"] == words[5]
    assert got["source_quote_paid_atomic"] == words[4]
    assert got["curve_real_quote_atomic"] == state["reserve"]
    assert got["curve_real_token_atomic"] == state["coin_reserve"]
    assert got["curve_virtual_quote_atomic"] == state["reserve"] + state["r"]
    assert got["curve_virtual_token_atomic"] == state["coin_reserve"] + state["h"]
    assert got["curve_progress"] == state["circulating_supply"] / state["dex_supply_thresh"]
    assert got["curve_progress_basis"] == "flap_curve_state_at_safety"
    assert got["curve_state_age_s"] == 2.0
    assert got["quote_mint"] == NATIVE and got["quote_decimals"] == 18


def test_without_a_safety_read_the_curve_is_unknown_never_zero():
    got = _stamp(_lane_event(stats=None))["decision_features"]
    for field in CURVE:
        assert got[field] is None and got[field + "_reason"] == "curve_state_not_held", field
    assert got["volume_m5_usd"] is None and got["volume_m5_usd_reason"] == "stats_not_cached"


def test_a_non_native_quote_curve_keeps_its_quote_but_not_a_guessed_decimals():
    ev = _lane_event()
    ev.decision_features_sink["flap_curve_state"] = _curve_record(
        _state(quote_token="0x" + "3f" * 20), 1.0)
    got = _stamp(ev)["decision_features"]
    assert got["quote_mint"] == "0x" + "3f" * 20
    assert got["quote_decimals"] is None and got["quote_decimals_reason"] == "not_held"


def test_the_flap_safety_check_keeps_the_curve_state_it_read_and_its_verdict():
    fallback, _calls = _fallback_recording()
    state, timings = _state(), {}
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state),
        simulate_fn=_matching_simulate_fn(state, override_allowed=True), overrides_fn=_overrides_fn())
    verdict = wrapped(YEE, bought_amount_atomic=10 ** 18, timings=timings)
    assert verdict == wrapped(YEE, bought_amount_atomic=10 ** 18)
    assert timings["flap_curve_state"] == {k: state[k] for k in C.CURVE_FACT_FIELDS}


def test_the_leg_hands_the_curve_state_to_the_event_and_keeps_it_out_of_the_safety_mark(
        monkeypatch, tmp_path):
    from test_evm_early_cached_sizing import Marks
    from test_memecoin_evm_leg import _c, _Ev
    c, kill = _c(monkeypatch, tmp_path)
    record = {k: v for k, v in _state().items() if k in C.CURVE_FACT_FIELDS}

    def safety_fn(mint, *, timings=None):
        timings["flap_curve_state"] = record
        timings["code_ms"] = 3.0
        return True, "safe"

    def execute_fn(conn, pid, mint, size, now_iso, safety_gate_fn=None):
        return {"status": "filled", "position_id": pid} if safety_gate_fn()[0] else {
            "status": "unsafe_gate", "reason": "x"}
    trace = Marks()
    trace.mark = lambda *a, **k: None
    trace.reconciled_now = lambda: AT
    ev = _Ev()
    ev.decision_features_sink = {}
    leg.evm_real_entry(c, ev, route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                       native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                       token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill, trace=trace)
    assert ev.decision_features_sink["flap_curve_state"] == dict(record, observed_at=AT)
    mark = trace.last("evm.token_safety")
    assert mark["code_ms"] == 3.0 and "flap_curve_state" not in mark

