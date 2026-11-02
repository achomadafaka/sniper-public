#!/usr/bin/env python3
"""Live-positions poller for the Live Wallet Signals roster. For each graded wallet it pulls the
wallet's current OPEN positions from Polymarket /positions (WHOLE-position aggregates — never
trade-level), classifies each position's coarse league from its slug/title (tag-canonical names via
leagues.league_from_slug), upserts into live_positions, and marks vanished positions closed. Fast
cron (~90s). Read-only on Polymarket; per-wallet errors are isolated. Cron: `*/2 * * * * python3
scripts/poll_live_positions.py`."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.analysis.market_taxonomy import league_from_slug  # noqa: E402


def _first(row: dict, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalize_positions(wallet: str, rows: list) -> tuple[list, set]:
    """Polymarket /positions rows -> (upsert dicts, open_keys). Pure: league via league_from_slug
    over the slug+title blob (tag-canonical coarse names). open_keys = {(condition_id, oidx)}."""
    out, keys = [], set()
    for r in rows or []:
        # Polymarket /positions returns RESOLVED-but-unredeemed positions (redeemable=True, curPrice=0
        # — e.g. a game that ended weeks ago) alongside genuinely-open ones. They are NOT live/copyable
        # bets; skip them entirely (and leave them out of open_keys so the close-inference sweep retires
        # any already in live_positions). Genuinely-live positions carry redeemable=False + curPrice>0.
        if r.get("redeemable"):
            continue
        cid = _first(r, "conditionId", "condition_id", "condition")
        oidx = _first(r, "outcomeIndex", "outcome_index")
        if cid is None or oidx is None:
            continue
        try:
            oidx = int(oidx)
        except (TypeError, ValueError):
            continue
        slug = _first(r, "eventSlug", "event_slug", "slug") or ""
        title = _first(r, "title", "marketTitle", "question") or ""
        league = league_from_slug(f"{slug} {title}")
        out.append({
            "wallet": wallet.lower(), "condition_id": str(cid), "outcome_index": oidx,
            "outcome": _first(r, "outcome", "outcomeName") or "", "title": title, "event_slug": slug,
            "league": league, "size": _f(_first(r, "size", "quantity")),
            "avg_price": _f(_first(r, "avgPrice", "averagePrice", "avg_price")),
            "cur_price": _f(_first(r, "curPrice", "currentPrice", "current_price")),
            "current_value": _f(_first(r, "currentValue", "current_value")),
            "cash_pnl": _f(_first(r, "cashPnl", "cash_pnl", "pnl")),
        })
        keys.add((str(cid), oidx))
    return out, keys


def should_close_from(term) -> bool:
    """Only INFER closures (mark vanished positions closed) when the fetch reached a NATURAL end.
    On api_refused/request_failed the position list is INCOMPLETE — e.g. a first-page 429 returns
    rows=[] with reason='api_refused', so treating "not in this run's keys" as closed would
    false-close EVERY open position for the wallet. Natural ends: empty_page / partial_page."""
    return getattr(term, "reason", None) in ("empty_page", "partial_page")


def main(argv=None) -> int:
    import db
    from sniper.markets import polymarket_api as P
    from sniper.persistence.db_wallet_signals import all_profiles, upsert_live_position, mark_closed
    conn = db.get_connection()
    wallets = [w.lower() for w in (argv or sys.argv[1:])] or sorted(all_profiles(conn).keys())
    sess = P.build_session()
    total = 0
    for w in wallets:
        try:
            rows, term = P.fetch_user_positions(sess, w, "positions")
        except Exception as e:  # noqa: BLE001 — isolate one wallet's failure
            print(f"  [{w[:10]}] positions fetch failed: {type(e).__name__}", file=sys.stderr)
            continue
        norm, keys = normalize_positions(w, rows)
        for row in norm:
            upsert_live_position(conn, row)
        if should_close_from(term):
            mark_closed(conn, w, keys)              # complete fetch -> safe to infer closures
        else:
            print(f"  [{w[:10]}] fetch incomplete ({getattr(term, 'reason', '?')}) — "
                  f"keeping prior open positions (no close-inference)", file=sys.stderr)
        total += len(norm)
        print(f"  [{w[:10]}] {len(norm)} open positions", file=sys.stderr)
    print(f"polled {len(wallets)} wallets, {total} open positions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
