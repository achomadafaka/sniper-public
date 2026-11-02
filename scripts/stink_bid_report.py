#!/usr/bin/env python3
"""Stink Bid evaluation report — run after data collection.

Queries paper_limit_orders, paper_limit_fills, paper_limit_events,
and strategy_positions to compute the 8 metrics from the spec.

Usage:
  python3 scripts/stink_bid_report.py [db_path] [strategy_id]
"""
from __future__ import annotations

import json
import sqlite3
import sys


def report(db_path: str, strategy_id: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()["n"]

    strict = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ? AND state = 'filled'",
        (strategy_id,),
    ).fetchone()["n"]

    cancelled = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ? AND state = 'cancelled'",
        (strategy_id,),
    ).fetchone()["n"]

    expired = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ? AND state = 'expired'",
        (strategy_id,),
    ).fetchone()["n"]

    resting = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ? AND state = 'resting'",
        (strategy_id,),
    ).fetchone()["n"]

    # Loose-only fills (sensitivity gap)
    loose_only = conn.execute("""
        SELECT COUNT(DISTINCT plf.order_id) AS n
        FROM paper_limit_fills plf
        JOIN paper_limit_orders plo ON plf.order_id = plo.order_id
        WHERE plo.strategy_id = ? AND plo.state != 'filled'
        AND plf.fill_label = 'loose'
    """, (strategy_id,)).fetchone()["n"]

    # Skipped orders
    skipped = conn.execute("""
        SELECT COUNT(*) AS n FROM paper_limit_events
        WHERE event_type = 'order_skipped'
    """).fetchone()["n"]

    print(f"{'=' * 50}")
    print(f"  STINK BID REPORT (strategy_id={strategy_id})")
    print(f"{'=' * 50}")
    print(f"Total orders:     {total}")
    print(f"  resting:        {resting}")
    print(f"  filled (strict):{strict}")
    print(f"  cancelled:      {cancelled}")
    print(f"  expired:        {expired}")
    print(f"Loose-only gaps:  {loose_only}")
    print(f"Skipped matches:  {skipped}")
    print(f"Fill rate:        {100 * strict / max(total, 1):.1f}%")

    # Opportunity rate
    opportunity = conn.execute("""
        SELECT COUNT(DISTINCT plo.order_id) AS n
        FROM paper_limit_orders plo
        JOIN paper_limit_fills plf ON plo.order_id = plf.order_id
        WHERE plo.strategy_id = ?
    """, (strategy_id,)).fetchone()["n"]
    print(f"Opportunity rate: {100 * opportunity / max(total, 1):.1f}% "
          f"({opportunity}/{total} orders saw trade at bid zone)")

    # PnL from strategy_positions
    wallet_prefix = f"paper:stink_bid:{strategy_id}"
    positions = conn.execute("""
        SELECT state, entry_price, current_price, size_usdc, realized_pnl
        FROM strategy_positions
        WHERE wallet = ? AND strategy_id = ?
    """, (wallet_prefix, str(strategy_id))).fetchall()

    if positions:
        open_pos = [p for p in positions if p["state"] == "open"]
        closed_pos = [p for p in positions if p["state"] == "closed"]
        total_pnl = sum(p["realized_pnl"] or 0 for p in closed_pos)
        wins = sum(1 for p in closed_pos if (p["realized_pnl"] or 0) > 0)

        print(f"\nPositions: {len(positions)} total ({len(open_pos)} open, {len(closed_pos)} closed)")
        if closed_pos:
            print(f"Win rate:         {100 * wins / len(closed_pos):.1f}%")
            print(f"Realized PnL:     ${total_pnl:.2f}")
    else:
        print("\nNo positions yet.")

    # Fill quality
    strict_fills = conn.execute("""
        SELECT trade_price, bid_price, slippage
        FROM paper_limit_fills
        WHERE fill_label = 'strict'
        ORDER BY created_at
    """).fetchall()
    if strict_fills:
        avg_slippage = sum(f["slippage"] for f in strict_fills) / len(strict_fills)
        print(f"\nStrict fills:     {len(strict_fills)}")
        print(f"Avg slippage:     {avg_slippage:.4f} (positive = better than bid)")

    print(f"\n{'=' * 50}")
    if total < 50:
        print(f"  {50 - total} more qualified matches needed for exit criteria")
    else:
        print("  Exit criteria data threshold met (50+ orders)")

    conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    sid = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    report(db, sid)
