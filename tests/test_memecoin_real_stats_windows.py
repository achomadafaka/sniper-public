"""The funded-authority board shows 24h / 7d / 30d beside all-time (operator, 2026-09-22), so a
wallet that was good and started to degrade reads as such. Same robot-only, per-chain, closed-row
definition as the all-time columns. No network."""
import sqlite3
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _close(c, uid, pnl, age, *, mode="real", source="global_feed", net="solana", notional=None):
    now = datetime.now(timezone.utc)
    closed = (now - age).isoformat()
    opened = (now - age - timedelta(minutes=2)).isoformat()
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,is_open,"
        "realized_pnl,opened_at,closed_at,strategy,signal_source,entry_notional)"
        " VALUES (?,?,?,?,0,?,?,?,'alpha_b',?,?)",
        (uid, "mint-%s-%s" % (uid, age.total_seconds()), net, mode, pnl, opened, closed, source, notional))


def _pnl(window):
    return {"closed": window["closed"], "pnl": window["pnl"]}


def test_windows_bucket_closes_by_closed_at_and_keep_all_time():
    c = _conn()
    _close(c, "u1", 10.0, timedelta(hours=1))
    _close(c, "u1", -4.0, timedelta(days=3))
    _close(c, "u1", 7.0, timedelta(days=20))
    _close(c, "u1", -30.0, timedelta(days=60))
    c.commit()
    w = m.real_stats_by_wallet(c, "solana")["u1"]
    assert (w["closed_count"], round(w["realized_pnl"], 2)) == (4, -17.0)
    assert _pnl(w["windows"]["24h"]) == {"closed": 1, "pnl": 10.0}
    assert _pnl(w["windows"]["7d"]) == {"closed": 2, "pnl": 6.0}
    assert _pnl(w["windows"]["30d"]) == {"closed": 3, "pnl": 13.0}
    # no notional recorded -> no percentage, never a fabricated one
    assert w["windows"]["24h"]["roi_pct"] is None and w["roi_pct"] is None


def test_return_on_notional_per_window_and_all_time():
    """The operator reads a percent, not dollars: the clip differs on almost every entry."""
    c = _conn()
    _close(c, "u1", 10.0, timedelta(hours=1), notional=100.0)     # +10% on $100
    _close(c, "u1", -30.0, timedelta(days=3), notional=200.0)     # -15% on $200
    _close(c, "u1", 6.0, timedelta(days=60), notional=50.0)       # +12% on $50, outside 30d
    c.commit()
    w = m.real_stats_by_wallet(c, "solana")["u1"]
    assert w["windows"]["24h"]["roi_pct"] == 10.0 and w["windows"]["24h"]["notional"] == 100.0
    assert round(w["windows"]["7d"]["roi_pct"], 4) == round(-20.0 / 300.0 * 100.0, 4)
    assert round(w["windows"]["30d"]["roi_pct"], 4) == round(-20.0 / 300.0 * 100.0, 4)
    assert round(w["roi_pct"], 4) == round(-14.0 / 350.0 * 100.0, 4) and w["notional"] == 350.0


def test_windows_are_robot_only_and_chain_scoped_like_all_time():
    c = _conn()
    _close(c, "u1", 50.0, timedelta(hours=2), source="manual")     # operator pick: never graded
    _close(c, "u1", 5.0, timedelta(hours=2), net="4663")            # another chain
    _close(c, "u1", 2.0, timedelta(hours=2), mode="paper")          # paper: never funded evidence
    _close(c, "u1", 1.0, timedelta(hours=2))
    c.commit()
    w = m.real_stats_by_wallet(c, "solana")["u1"]
    assert _pnl(w["windows"]["24h"]) == {"closed": 1, "pnl": 1.0}
    assert w["closed_count"] == 1
