"""Chainlink RTDS frame parsing + price-to-beat reconstruction/calibration. Recorded fixtures only."""
from __future__ import annotations

import json
import os
from decimal import Decimal

from sniper.algos.polymarket_crypto5m import chainlink_rtds as RTDS
from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as P2B
from sniper.algos.polymarket_crypto5m.models import ChainlinkTick, PriceToBeatQuality

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _frames():
    with open(os.path.join(FX, "rtds_chainlink_ticks.jsonl")) as f:
        return [line for line in f.read().splitlines() if line.strip()]


# --- RTDS frame parsing ---------------------------------------------------------------------

def test_parse_update_frame_uses_full_accuracy_decimal():
    raw = json.dumps({
        "connection_id": "abc",
        "payload": {"full_accuracy_value": "63610238708214345000000", "symbol": "btc/usd",
                    "timestamp": 1781948758000, "value": 63610.23870821435},
        "timestamp": 1781948758835, "topic": "crypto_prices_chainlink", "type": "update"})
    ticks = RTDS.parse_frame(raw, recv_ts=1781948759)
    assert len(ticks) == 1
    t = ticks[0]
    assert t.symbol == "btc/usd"
    assert t.source_ts_ms == 1781948758000
    assert t.server_ts_ms == 1781948758835
    assert t.recv_ts == 1781948759
    assert t.is_snapshot is False
    # exact Decimal, NOT the lossy float
    assert t.price == Decimal("63610238708214345000000") / (Decimal(10) ** 18)
    assert t.price == Decimal("63610.238708214345")


def test_parse_snapshot_frame_expands_data_array():
    raw = json.dumps({"payload": {"data": [
        {"timestamp": 1781948698000, "value": 63638.13747611466},
        {"timestamp": 1781948699000, "value": 63637.9930505619}]},
        "timestamp": 1781948700000, "topic": "crypto_prices_chainlink", "type": "snapshot"})
    ticks = RTDS.parse_frame(raw)
    assert len(ticks) == 2
    assert all(t.is_snapshot for t in ticks)
    assert ticks[0].source_ts_ms == 1781948698000
    # snapshot items have no full_accuracy_value -> price falls back to the float value
    assert ticks[0].price == Decimal("63638.13747611466")


def test_parse_control_frames_are_ignored():
    assert RTDS.parse_frame("PONG") == []
    assert RTDS.parse_frame('{"type":"subscribed"}') == []
    assert RTDS.parse_frame("not json") == []


def test_malformed_numeric_update_is_skipped_not_crashed():
    raw = json.dumps({"payload": {"symbol": "btc/usd", "timestamp": "notanint",
                                  "value": "notafloat"}, "timestamp": 1, "type": "update"})
    assert RTDS.parse_frame(raw) == []   # never raises, never a fake tick


def test_snapshot_item_missing_value_is_skipped_not_zero():
    # a value-less snapshot item must NOT become a fake 0.0 Chainlink observation
    raw = json.dumps({"payload": {"data": [
        {"timestamp": 1781948698000},                     # no value -> skip
        {"timestamp": 1781948699000, "value": 63637.99}]},  # good -> keep
        "timestamp": 1781948700000, "type": "snapshot"})
    ticks = RTDS.parse_frame(raw)
    assert len(ticks) == 1
    assert all(t.value != 0.0 for t in ticks)
    assert ticks[0].source_ts_ms == 1781948699000


def test_real_fixture_frames_parse_without_error():
    ticks = []
    for raw in _frames():
        ticks.extend(RTDS.parse_frame(raw, recv_ts=1781948800))
    assert len(ticks) >= 10
    assert any(not t.is_snapshot for t in ticks)
    assert all(t.symbol == "btc/usd" for t in ticks if not t.is_snapshot)
    assert all(t.price > 0 for t in ticks)


# --- price-to-beat boundary reconstruction --------------------------------------------------

def _ticks_around(start_s):
    start_ms = start_s * 1000
    # one tick per second from start-3s..start+3s
    return [ChainlinkTick("btc/usd", start_ms + d * 1000, None, start_s + d,
                          float(1000 + d), str((1000 + d) * 10**18), False)
            for d in range(-3, 4)]


def test_reconstruct_three_candidate_rules():
    start = 1781947200
    cands = P2B.reconstruct(_ticks_around(start), window_start_ts=start)
    # last tick at-or-before start == the start-second tick (value 1000)
    assert cands["last_at_or_before"].price == Decimal("1000")
    assert cands["first_at_or_after"].price == Decimal("1000")   # exact tick at start
    assert cands["nearest"].price == Decimal("1000")


def test_reconstruct_when_no_tick_exactly_on_boundary():
    start = 1781947200
    start_ms = start * 1000
    ticks = [ChainlinkTick("btc/usd", start_ms - 2000, None, start - 2, 998.0, str(998 * 10**18), False),
             ChainlinkTick("btc/usd", start_ms + 3000, None, start + 3, 1003.0, str(1003 * 10**18), False)]
    cands = P2B.reconstruct(ticks, window_start_ts=start)
    assert cands["last_at_or_before"].price == Decimal("998")
    assert cands["first_at_or_after"].price == Decimal("1003")
    assert cands["nearest"].price == Decimal("998")              # 2s away beats 3s away


def test_reconstruct_missing_side_returns_none_candidate():
    start = 1781947200
    only_after = [ChainlinkTick("btc/usd", start * 1000 + 5000, None, start + 5, 1005.0,
                               str(1005 * 10**18), False)]
    cands = P2B.reconstruct(only_after, window_start_ts=start)
    assert cands["last_at_or_before"] is None
    assert cands["first_at_or_after"].price == Decimal("1005")


# --- quality + fail-closed ------------------------------------------------------------------

def test_quality_is_unavailable_without_locked_rule_or_exact_report():
    # v0: boundary rule not yet locked -> RTDS reconstruction is NOT decision-grade.
    q = P2B.resolve_quality(has_exact_report=False, locked_rule=None, has_rtds_candidate=True,
                            has_proxy=True)
    assert q is PriceToBeatQuality.UNAVAILABLE


def test_quality_validated_when_rule_locked_and_candidate_present():
    q = P2B.resolve_quality(has_exact_report=False, locked_rule="last_at_or_before",
                            has_rtds_candidate=True, has_proxy=True)
    assert q is PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION


def test_quality_rejects_bogus_locked_rule():
    # a non-candidate "locked" rule must NOT be treated as decision-grade
    q = P2B.resolve_quality(has_exact_report=False, locked_rule="bogus", has_rtds_candidate=True,
                            has_proxy=True)
    assert q is PriceToBeatQuality.UNAVAILABLE


def test_quality_exact_report_wins():
    q = P2B.resolve_quality(has_exact_report=True, locked_rule=None, has_rtds_candidate=False,
                            has_proxy=False)
    assert q is PriceToBeatQuality.EXACT_CHAINLINK_REPORT


def test_proxy_only_and_decision_gate():
    q = P2B.resolve_quality(has_exact_report=False, locked_rule="last_at_or_before",
                            has_rtds_candidate=False, has_proxy=True)
    assert q is PriceToBeatQuality.PROXY_ONLY
    # fail-closed: only exact / validated-rtds may drive a paper decision
    assert P2B.decision_allowed(PriceToBeatQuality.PROXY_ONLY) is False
    assert P2B.decision_allowed(PriceToBeatQuality.UNAVAILABLE) is False
    assert P2B.decision_allowed(PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION) is True
    assert P2B.decision_allowed(PriceToBeatQuality.EXACT_CHAINLINK_REPORT) is True


# --- calibration vs the Gamma oracle --------------------------------------------------------

def test_calibration_matches_oracle_within_precision():
    start = 1781947200
    cands = P2B.reconstruct(_ticks_around(start), window_start_ts=start)
    # oracle equals the at-or-before candidate exactly
    rows = [P2B.compare_to_oracle(cands, oracle_value=Decimal("1000"), precision=Decimal("0.01"))]
    agg = P2B.calibrate(rows)
    assert agg["last_at_or_before"]["match_rate"] == 1.0
    assert agg["last_at_or_before"]["max_abs_error"] == Decimal("0")
    assert agg["n"] == 1


def test_calibration_counts_mismatch_and_missing():
    start = 1781947200
    cands = P2B.reconstruct(_ticks_around(start), window_start_ts=start)  # all candidates == 1000
    rows = [
        P2B.compare_to_oracle(cands, oracle_value=Decimal("1000"), precision=Decimal("0.01")),
        P2B.compare_to_oracle(cands, oracle_value=Decimal("1005"), precision=Decimal("0.01")),  # miss by 5
        P2B.compare_to_oracle({"last_at_or_before": None, "first_at_or_after": None, "nearest": None},
                              oracle_value=Decimal("1000"), precision=Decimal("0.01")),           # missing
    ]
    agg = P2B.calibrate(rows)
    assert agg["last_at_or_before"]["match_rate"] == 0.5      # 1 of 2 non-missing matched
    assert agg["last_at_or_before"]["missing"] == 1
    assert agg["last_at_or_before"]["max_abs_error"] == Decimal("5")


def test_calibration_counts_malformed_row_as_missing():
    # a malformed/empty per-window row (no rule key at all) must count toward missing, not vanish
    good = P2B.compare_to_oracle(
        P2B.reconstruct(_ticks_around(1781947200), window_start_ts=1781947200),
        oracle_value=Decimal("1000"), precision=Decimal("0.01"))
    agg = P2B.calibrate([good, {}])                          # {} = malformed window
    assert agg["n"] == 2
    assert agg["last_at_or_before"]["missing"] == 1          # the {} row counted as missing
    assert agg["last_at_or_before"]["n_present"] == 1


# --- re-subscribe collector (RTDS backfills on subscribe, never streams in-window; verified 2026-06-21) ---

class _FakeWS:
    """Yields its frames on recv(), then raises (a recv timeout between batches — NOT a reconnect)."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False
        self.subscribed = False

    def send(self, _msg):
        self.subscribed = True

    def recv(self):
        if self.frames:
            return self.frames.pop(0)
        raise TimeoutError("no more frames this batch")

    def close(self):
        self.closed = True


def test_collect_resubscribing_pulls_fresh_batches_after_interval():
    # batch A (one subscribe) then, after the interval elapses, batch B (a re-subscribe) with NEWER ticks.
    a = '{"payload":{"data":[{"timestamp":1000,"value":"100.0"},{"timestamp":1005,"value":"101.0"}]}}'
    b = '{"payload":{"data":[{"timestamp":1015,"value":"102.0"},{"timestamp":1020,"value":"103.0"}]}}'
    sockets = [_FakeWS([a]), _FakeWS([b])]
    made = []

    def connect_fn():
        ws = sockets[len(made)] if len(made) < len(sockets) else _FakeWS([])
        made.append(ws)
        return ws

    clock = {"t": 0.0}
    iters = {"n": 0}

    def clock_fn():
        return clock["t"]

    def stop_fn():
        iters["n"] += 1
        if iters["n"] == 3:                       # after batch A drained, jump the clock past the interval
            clock["t"] = RTDS.RESUBSCRIBE_INTERVAL_S + 1
        return len(made) >= 2 and not sockets[1].frames   # stop once batch B is drained

    ticks = []
    RTDS.collect_resubscribing(connect_fn, stop_fn=stop_fn, on_tick=ticks.append,
                               now_fn=lambda: 9999, clock_fn=clock_fn)
    vals = sorted(t.value for t in ticks)
    assert vals == [100.0, 101.0, 102.0, 103.0]   # ticks from BOTH the first and the re-subscribed batch
    assert len(made) == 2 and sockets[0].closed    # re-subscribed via a fresh connection; old one closed
    assert sockets[0].subscribed and sockets[1].subscribed


def test_collect_resubscribing_backs_off_on_persistent_connect_failure():
    # an RTDS outage must NOT busy-loop: connect_fn keeps raising -> the loop backs off via sleep_fn.
    attempts = {"n": 0}
    sleeps = []

    def connect_fn():
        attempts["n"] += 1
        raise ConnectionError("RTDS down")

    RTDS.collect_resubscribing(connect_fn, stop_fn=lambda: attempts["n"] >= 3,
                               on_tick=lambda t: None, now_fn=lambda: 0, clock_fn=lambda: 0,
                               sleep_fn=sleeps.append)
    assert attempts["n"] >= 3                       # it kept trying
    assert len(sleeps) >= 2 and all(s > 0 for s in sleeps)   # ...but backed off each time (no busy-loop)


def test_append_chainlink_tick_dedups_overlapping_batches(tmp_path):
    from sniper.algos.polymarket_crypto5m import persistence as P
    conn = P.connect(os.path.join(str(tmp_path), "dd.sqlite"))
    P.ensure_schema(conn)
    t = ChainlinkTick(symbol="BTC/USD", source_ts_ms=1000, server_ts_ms=1, recv_ts=2,
                      value=100.0, full_accuracy_value=None, is_snapshot=True)
    first = P.append_chainlink_tick(conn, t, window_start_ts=900)
    again = P.append_chainlink_tick(conn, t, window_start_ts=900)   # same tick from a re-subscribe overlap
    assert first is True and again is False                          # second insert ignored
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_chainlink_ticks").fetchone()[0] == 1
