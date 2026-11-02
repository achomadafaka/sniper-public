"""Daily leaderboard + PnL snapshot pass. fomo lifetime delta accrues every day (cheap
leaderboard call); realized-24h tiles are recorded ONLY on days a trader was freshly
screened, else the stale value would repeat and double-count the 7d sum."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_snapshot import daily_snapshot_pass


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _lb(*entries):
    return lambda auth: list(entries)


def _e(uid, pnl, hpnl=(0.0,)):
    return {"id": uid, "userHandle": uid, "totalPnL": pnl,
            "topHoldings": [{"pnl": p} for p in hpnl]}


def test_pass_ingests_leaderboard_and_snapshots_lifetime():
    c = _conn()
    res = daily_snapshot_pass(c, None, "2026-07-07T00:00:00Z",
                              lb_fn=_lb(_e("u1", 1000.0), _e("u2", 2000.0)))
    assert res["leaderboard"] == 2 and res["snapshots"] == 2
    # fomo lifetime delta will accrue: the snapshot carries fomo_total_pnl
    rows = c.execute("SELECT source_user_id, fomo_total_pnl, realized_24h "
                     "FROM memecoin_trader_pnl_snapshots ORDER BY source_user_id").fetchall()
    assert rows[0]["fomo_total_pnl"] == 1000.0 and rows[0]["realized_24h"] is None


def test_realized_tile_only_when_analytics_fresh_today():
    c = _conn()
    # a stale analytics row (computed days ago) must NOT contribute its 24h tile
    m.upsert_trader_analytics(c, {"source_user_id": "u1", "handle": "u1",
                                  "computed_at": "2026-07-01T00:00:00Z",
                                  "realized_pnl_24h": 50.0, "n_closed_24h": 3})
    daily_snapshot_pass(c, None, "2026-07-07T00:00:00Z", lb_fn=_lb(_e("u1", 1000.0)))
    r = c.execute("SELECT realized_24h FROM memecoin_trader_pnl_snapshots").fetchone()
    assert r["realized_24h"] is None            # stale screen -> no tile

    # a fresh analytics row (computed today) DOES contribute
    m.upsert_trader_analytics(c, {"source_user_id": "u2", "handle": "u2",
                                  "computed_at": "2026-07-07T09:00:00Z",
                                  "realized_pnl_24h": 42.0, "n_closed_24h": 2})
    daily_snapshot_pass(c, None, "2026-07-07T12:00:00Z", lb_fn=_lb())
    r2 = c.execute("SELECT realized_24h FROM memecoin_trader_pnl_snapshots "
                   "WHERE source_user_id='u2'").fetchone()
    assert abs(r2["realized_24h"] - 42.0) < 1e-6


def test_pass_snapshots_screened_trader_not_on_leaderboard():
    c = _conn()
    m.upsert_trader_analytics(c, {"source_user_id": "u9", "handle": "u9",
                                  "computed_at": "2026-07-07T00:00:00Z",
                                  "realized_pnl_24h": 12.0, "n_closed_24h": 1,
                                  "live_holdings_value_usd": 500.0})
    res = daily_snapshot_pass(c, None, "2026-07-07T00:00:00Z", lb_fn=_lb())
    assert res["snapshots"] == 1
    r = c.execute("SELECT realized_24h, fomo_total_pnl FROM memecoin_trader_pnl_snapshots "
                  "WHERE source_user_id='u9'").fetchone()
    assert abs(r["realized_24h"] - 12.0) < 1e-6 and r["fomo_total_pnl"] is None


def test_repeated_same_day_pass_updates_in_place():
    c = _conn()
    daily_snapshot_pass(c, None, "2026-07-07T08:00:00Z", lb_fn=_lb(_e("u1", 1000.0)))
    daily_snapshot_pass(c, None, "2026-07-07T12:00:00Z", lb_fn=_lb(_e("u1", 1200.0)))

    rows = c.execute(
        "SELECT snapshot_ts, fomo_total_pnl FROM memecoin_trader_pnl_snapshots "
        "WHERE source_user_id='u1'"
    ).fetchall()
    assert [(r["snapshot_ts"], r["fomo_total_pnl"]) for r in rows] == [
        ("2026-07-07T12:00:00Z", 1200.0),
    ]
