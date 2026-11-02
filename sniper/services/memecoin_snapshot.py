"""Daily leaderboard ingest + forward PnL snapshot.

Runs once per calendar day. Two signals accrue:
  1. fomo lifetime totalPnL delta — cheap (one leaderboard call), recorded for every top-50
     trader every day, so fomo_pnl_delta() has clean anchors regardless of screening.
  2. realized-24h tiles — recorded ONLY when a trader's analytics row was computed TODAY.
     A stale row's realized_24h is days old; tiling it daily would repeat the same value and
     double-count the 7d/30d sum. So realized tiles are gated on a same-day screen.
"""
from __future__ import annotations

from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_account import fetch_leaderboard


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def daily_snapshot_pass(conn, auth, now_iso, lb_fn=None):
    """Ingest the leaderboard and record one PnL snapshot per tracked trader for `now_iso`
    (a single calendar day). Returns {leaderboard, snapshots}."""
    lb = (lb_fn or fetch_leaderboard)(auth)
    m.ingest_leaderboard(conn, lb, now_iso)
    lb_pnl = {e["id"]: _f(e.get("totalPnL")) for e in lb if e.get("id")}
    ana = {r["source_user_id"]: r for r in m.list_trader_analytics(conn)}
    today = now_iso[:10]
    n = 0
    for uid in set(lb_pnl) | set(ana):
        a = ana.get(uid) or {}
        fresh = str(a.get("computed_at") or "")[:10] == today   # analyzer ran today
        row = {
            "fomo_total_pnl": lb_pnl.get(uid, a.get("fomo_total_pnl")),
            "realized_pnl_24h": a.get("realized_pnl_24h") if fresh else None,
            "n_closed_24h": a.get("n_closed_24h") if fresh else None,
            "unrealized_pnl_usd": a.get("unrealized_pnl_usd") if fresh else None,
            "live_holdings_value_usd": a.get("live_holdings_value_usd") if fresh else None,
        }
        if all(v is None for v in row.values()):
            continue
        m.record_pnl_snapshot(conn, uid, row, now_iso)
        n += 1
    return {"leaderboard": len(lb), "snapshots": n}
