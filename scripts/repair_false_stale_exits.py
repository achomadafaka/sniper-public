#!/usr/bin/env python3
"""
One-off repair script: reopen paper trades falsely closed as 'stale'
on future-dated event markets where the source wallet still holds.

Default mode: dry-run (prints what would change).
Use --apply to actually mutate the database.

Criteria for reopening:
1. status = 'stale'
2. Market has a future end_date (event hasn't occurred yet)
3. Source wallet still holds position (data-api size > 0.01)
4. No duplicate open trade for same (wallet, condition_id, outcome)
"""
import argparse
import sqlite3
import sys
import os
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")


def main():
    parser = argparse.ArgumentParser(description="Repair falsely stale-closed paper trades on future event markets")
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB (default: dry-run)")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument(
        "--trade-ids",
        default="",
        help="Comma-separated explicit trade IDs to consider. Required for --apply.",
    )
    args = parser.parse_args()
    selected_trade_ids = {
        int(part.strip())
        for part in str(args.trade_ids or "").split(",")
        if part.strip()
    }
    if args.apply and not selected_trade_ids:
        parser.error("--apply requires --trade-ids with the audited false-stale trade IDs")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    from db import ensure_paper_trades_disqualification_columns
    try:
        ensure_paper_trades_disqualification_columns(conn)
    except Exception:
        pass

    query = """
        SELECT trade_id, wallet, condition_id, outcome, status, exit_reason,
               entry_price, exit_price, pnl_usdc, market_title, opened_at, closed_at
        FROM paper_trades
        WHERE status = 'stale'
    """
    params = []
    if selected_trade_ids:
        placeholders = ",".join("?" for _ in selected_trade_ids)
        query += f"  AND trade_id IN ({placeholders})\n"
        params.extend(sorted(selected_trade_ids))
    query += "    ORDER BY closed_at DESC"
    candidates = conn.execute(query, params).fetchall()

    if not candidates:
        print("No stale trades found. Nothing to repair.")
        return

    print(f"Found {len(candidates)} stale trade(s)")
    print(f"{'Mode':>8}: {'DRY-RUN' if not args.apply else 'APPLY'}\n")

    now = datetime.now(timezone.utc)
    reopened = []
    skipped = []

    for trade in candidates:
        trade_id = trade["trade_id"]
        wallet = trade["wallet"]
        condition_id = trade["condition_id"]
        outcome = trade["outcome"]
        title = (trade["market_title"] or "")[:50]
        prefix = f"  Trade #{trade_id} ({title})"

        # Check 1: Market has future end date
        meta = conn.execute(
            "SELECT end_date_iso FROM market_metadata WHERE condition_id = ?",
            (condition_id,)
        ).fetchone()
        end_date_str = meta["end_date_iso"] if meta else None
        future = False
        if end_date_str:
            try:
                ed = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                # Make naive datetimes timezone-aware (assume UTC)
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=timezone.utc)
                future = ed > now
            except Exception:
                pass

        if not future:
            skipped.append((trade_id, "market end date is past or unknown"))
            print(f"{prefix}: SKIP — market end date is past or unknown (end={end_date_str})")
            continue

        # Check 2: Source wallet still holds position
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": wallet, "sizeThreshold": 0, "limit": 500},
                timeout=10,
            )
            positions = r.json() if r.ok else []
        except Exception as e:
            skipped.append((trade_id, f"data-api error: {e}"))
            print(f"{prefix}: SKIP — data-api error: {e}")
            continue

        source_size = 0.0
        for p in positions:
            cid = str(p.get("market", "") or p.get("conditionId", "")).lower()
            if cid == condition_id.lower():
                source_size = float(p.get("size", 0))
                break

        if source_size < 0.01:
            skipped.append((trade_id, f"source has exited (size={source_size:.4f})"))
            print(f"{prefix}: SKIP — source has actually exited (size={source_size:.4f})")
            continue

        # Check 3: No duplicate open trade
        dup = conn.execute(
            "SELECT 1 FROM paper_trades WHERE wallet = ? AND condition_id = ? AND LOWER(outcome) = LOWER(?) AND status = 'open' LIMIT 1",
            (wallet, condition_id, outcome)
        ).fetchone()
        if dup:
            skipped.append((trade_id, "duplicate open trade exists"))
            print(f"{prefix}: SKIP — duplicate open trade exists")
            continue

        # All checks passed
        print(f"{prefix}: REOPEN — future market (end={end_date_str[:10]}), source holds {source_size:.1f} shares")
        reopened.append(trade_id)

        if args.apply:
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                UPDATE paper_trades SET
                    status = 'open',
                    exit_price = NULL,
                    exit_reason = NULL,
                    exit_classification = NULL,
                    closed_at = NULL,
                    pnl_usdc = NULL,
                    roi_percent = NULL,
                    signal_disqualified_at = ?,
                    signal_disqualification_reason = ?
                WHERE trade_id = ?
            """, (now_str, "reopened: false stale exit on future event market", trade_id))
            conn.commit()

    print(f"\n{'='*60}")
    print(f"Summary: {len(reopened)} to reopen, {len(skipped)} skipped")
    if reopened:
        print(f"Trade IDs to reopen: {reopened}")
    if skipped:
        print("Skipped:")
        for tid, reason in skipped:
            print(f"  #{tid}: {reason}")
    if reopened and not args.apply:
        ids = ",".join(str(t) for t in reopened)
        print(f"\nRe-run with --apply to execute:")
        print(f"  python3 scripts/repair_false_stale_exits.py --apply --trade-ids {ids}")


if __name__ == "__main__":
    main()
