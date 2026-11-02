"""RTDS delivery-mode classification, known-at-time discipline, and clock integrity."""
from __future__ import annotations

import os

from sniper.algos.polymarket_crypto5m import chainlink_rtds as RTDS
from sniper.algos.polymarket_crypto5m import clock as CLK
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as P2B
from sniper.algos.polymarket_crypto5m.models import ChainlinkTick, ClockSource, RtdsDeliveryMode


def _tick(src_ms, *, snapshot):
    return ChainlinkTick("btc/usd", src_ms, None, src_ms // 1000, 100.0, str(100 * 10**18), snapshot)


# --- RTDS delivery mode (#2) ----------------------------------------------------------------

def test_delivery_mode_live_vs_snapshot_vs_unavailable():
    live = [_tick(1000, snapshot=True), _tick(2000, snapshot=False)]   # an update present
    assert RTDS.classify_delivery(live) is RtdsDeliveryMode.LIVE_UPDATES
    snap = [_tick(1000, snapshot=True), _tick(2000, snapshot=True)]
    assert RTDS.classify_delivery(snap) is RtdsDeliveryMode.SNAPSHOT_RECONSTRUCTION
    assert RTDS.classify_delivery([]) is RtdsDeliveryMode.UNAVAILABLE


def test_staleness_ms_from_latest_source_ts():
    ticks = [_tick(1000_000, snapshot=True), _tick(1005_000, snapshot=True)]
    assert RTDS.staleness_ms(ticks, now_ms=1010_000) == 5000   # 1010000 - latest 1005000
    assert RTDS.staleness_ms([], now_ms=1) is None


# --- known-at-time discipline (#5) ----------------------------------------------------------

def test_decision_not_before_known_ts():
    assert P2B.decision_not_before(known_ts=100, decision_ts=100) is True
    assert P2B.decision_not_before(known_ts=100, decision_ts=101) is True
    assert P2B.decision_not_before(known_ts=100, decision_ts=99) is False   # lookahead -> blocked


def test_known_at_time_latency():
    out = P2B.known_at_time(source_ts_ms=1781947200000, recv_ts=1781947201)   # ~1s late
    assert out["known_ts"] == 1781947201
    assert out["known_latency_ms"] == 1000


# --- clock integrity (#8) -------------------------------------------------------------------

def test_excessive_skew_threshold():
    assert CLK.excessive_skew(offset_ms=500) is False
    assert CLK.excessive_skew(offset_ms=7 * 3600 * 1000) is True            # the +7h env skew
    assert CLK.excessive_skew(offset_ms=-7 * 3600 * 1000) is True


def test_record_clock_persists_source_and_offset(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "ck.sqlite"))
    P.ensure_schema(conn)
    P.record_clock(conn, clock_source=ClockSource.BINANCE_SERVER, clock_offset_ms=-25_200_000,
                   clock_last_verified_ts=1781947200, now_ts=1781947201)
    row = conn.execute("SELECT * FROM crypto5m_clock").fetchone()
    assert row["clock_source"] == ClockSource.BINANCE_SERVER.value
    assert row["clock_offset_ms"] == -25_200_000
