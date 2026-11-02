"""Forward PnL snapshots: tile daily realized_24h into 7d/30d windows the ≤100-swap
ledger can't cover, plus a cadence-independent fomo_total_pnl delta for leaderboard traders."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_windowed_realized_sums_daily_tiles():
    c = _conn()
    # 3 daily snapshots, each +10 realized -> 7d window = 30
    for d, ts in enumerate(["2026-07-05T00:00:00Z", "2026-07-06T00:00:00Z", "2026-07-07T00:00:00Z"]):
        m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 10.0, "n_closed_24h": 3,
                                        "unrealized_pnl_usd": 5.0, "live_holdings_value_usd": 50.0,
                                        "fomo_total_pnl": None}, ts)
    w = m.windowed_realized(c, "u1", 7, "2026-07-07T12:00:00Z")
    assert abs(w["realized"] - 30.0) < 1e-6 and w["n_days"] == 3 and w["saturated"] == 0


def test_windowed_realized_dedups_multiple_snapshots_same_day():
    c = _conn()
    # two snapshots on the SAME calendar day -> only the latest counts (no double tile)
    m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 8.0, "n_closed_24h": 2}, "2026-07-07T06:00:00Z")
    m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 10.0, "n_closed_24h": 3}, "2026-07-07T18:00:00Z")
    w = m.windowed_realized(c, "u1", 7, "2026-07-07T23:00:00Z")
    assert abs(w["realized"] - 10.0) < 1e-6 and w["n_days"] == 1


def test_windowed_realized_does_not_count_missing_tiles_as_zero_days():
    c = _conn()
    for ts in ("2026-07-05T00:00:00Z", "2026-07-06T00:00:00Z", "2026-07-07T00:00:00Z"):
        m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1_000.0}, ts)

    assert m.windowed_realized(c, "u1", 7, "2026-07-07T12:00:00Z") == {
        "realized": 0.0, "n_days": 0, "saturated": 0, "days": 7,
    }


def test_record_snapshot_is_daily_idempotent_and_preserves_realized_tile():
    c = _conn()
    m.record_pnl_snapshot(
        c, "u1", {"realized_pnl_24h": 12.0, "n_closed_24h": 2},
        "2026-07-07T08:00:00Z",
    )
    m.record_pnl_snapshot(
        c, "u1", {"fomo_total_pnl": 1_500.0}, "2026-07-07T12:00:00Z",
    )

    rows = c.execute(
        "SELECT snapshot_ts, realized_24h, n_closed_24h, fomo_total_pnl "
        "FROM memecoin_trader_pnl_snapshots WHERE source_user_id='u1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["snapshot_ts"] == "2026-07-07T12:00:00Z"
    assert rows[0]["realized_24h"] == 12.0
    assert rows[0]["n_closed_24h"] == 2
    assert rows[0]["fomo_total_pnl"] == 1_500.0
    assert m.latest_snapshot_day(c) == "2026-07-07"


def test_windowed_realized_saturated_when_day_exceeds_window():
    # REACHABLE threshold (SWAP_LIMIT//2 = 50 round-trips ≈ 100 swap legs = the whole fetch
    # window). The old >=100-round-trip threshold could never fire under the 100-swap cap.
    c = _conn()
    m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 10.0, "n_closed_24h": 60}, "2026-07-07T00:00:00Z")
    assert m.windowed_realized(c, "u1", 7, "2026-07-07T12:00:00Z")["saturated"] == 1
    c2 = _conn()
    m.record_pnl_snapshot(c2, "u2", {"realized_pnl_24h": 10.0, "n_closed_24h": 40}, "2026-07-07T00:00:00Z")
    assert m.windowed_realized(c2, "u2", 7, "2026-07-07T12:00:00Z")["saturated"] == 0


def test_windowed_realized_excludes_out_of_window_days():
    c = _conn()
    m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 99.0, "n_closed_24h": 1}, "2026-05-01T00:00:00Z")
    m.record_pnl_snapshot(c, "u1", {"realized_pnl_24h": 10.0, "n_closed_24h": 1}, "2026-07-07T00:00:00Z")
    w = m.windowed_realized(c, "u1", 7, "2026-07-07T12:00:00Z")
    assert abs(w["realized"] - 10.0) < 1e-6    # May snapshot is outside the 7d window


def test_fomo_pnl_delta_is_cadence_independent():
    c = _conn()
    m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1000.0}, "2026-06-30T00:00:00Z")
    m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1500.0}, "2026-07-07T00:00:00Z")
    d = m.fomo_pnl_delta(c, "u1", 7, "2026-07-07T12:00:00Z")
    assert abs(d["delta"] - 500.0) < 1e-6 and d["from_pnl"] == 1000.0 and d["to_pnl"] == 1500.0
    assert abs(d["span_days"] - 7.0) < 0.1        # actual span ~= requested 7d


def test_fomo_pnl_delta_reports_short_span_honestly():
    # only 3 days of history but queried at days=30 -> span_days must reveal the ~3d truth,
    # not silently present a 3-day delta as a 30-day figure.
    c = _conn()
    m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1000.0}, "2026-07-04T00:00:00Z")
    m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1200.0}, "2026-07-07T00:00:00Z")
    d = m.fomo_pnl_delta(c, "u1", 30, "2026-07-07T12:00:00Z")
    assert d["days"] == 30 and abs(d["span_days"] - 3.0) < 0.1


def test_fomo_pnl_delta_none_when_insufficient_history():
    c = _conn()
    m.record_pnl_snapshot(c, "u1", {"fomo_total_pnl": 1500.0}, "2026-07-07T00:00:00Z")
    assert m.fomo_pnl_delta(c, "u1", 7, "2026-07-07T12:00:00Z")["delta"] is None
