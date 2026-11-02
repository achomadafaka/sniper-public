#!/usr/bin/env python3
"""
One-off repair script: repair paper trades falsely labeled as 'source_exited'
when the source wallet never actually exited.

Default mode: dry-run (prints what would change).
Use --apply to actually mutate the database.

Criteria for reopening:
1. exit_reason = 'source_exited' (status is typically 'early_exit')
2. No matching sell signal in paper_sell_signals
3. Source wallet still holds position (data-api size > 0.01)
4. Market not resolved/archived (CLOB check)
5. No duplicate open trade for same (wallet, condition_id, outcome)

If a newer open duplicate already exists for the same key, the script does not reopen
the older row. Instead it re-labels that row away from ``source_exited`` so the UI and
history stop claiming the source sold when it did not.
"""
import argparse
import sqlite3
import sys
import os
import requests
from datetime import datetime, timezone

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")


def _normalized_trade_key(trade):
    return (
        str(trade["wallet"] or "").lower(),
        str(trade["condition_id"] or "").lower(),
        str(trade["outcome"] or "").lower(),
    )


def _trade_recency_key(trade):
    return (
        str(trade["closed_at"] or ""),
        int(trade["trade_id"] or 0),
    )


def _dedupe_candidates(candidates):
    """Keep only the newest closed trade per normalized wallet/market/outcome key."""
    newest_by_key = {}
    skipped = []
    for trade in candidates:
        key = _normalized_trade_key(trade)
        current = newest_by_key.get(key)
        if current is None or _trade_recency_key(trade) > _trade_recency_key(current):
            if current is not None:
                skipped.append((current["trade_id"], f"older duplicate candidate for {key[0][:10]}..."))
            newest_by_key[key] = trade
        else:
            skipped.append((trade["trade_id"], f"older duplicate candidate for {key[0][:10]}..."))
    deduped = sorted(newest_by_key.values(), key=lambda trade: _trade_recency_key(trade), reverse=True)
    return deduped, skipped


def _relabel_duplicate_reason(
    conn: sqlite3.Connection,
    trade_id: int,
    open_trade_id: int,
) -> None:
    new_reason = f"superseded by newer open trade #{open_trade_id}"
    conn.execute(
        """
        UPDATE paper_trades
        SET exit_reason = ?
        WHERE trade_id = ? AND exit_reason = 'source_exited'
        """,
        (new_reason, trade_id),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Repair falsely closed source_exited paper trades")
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB (default: dry-run)")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument(
        "--trade-ids",
        default="",
        help="Comma-separated explicit trade IDs to consider. Required for --apply.",
    )
    parser.add_argument(
        "--closed-after",
        default="2026-03-10",
        help="Only consider trades closed after this date (YYYY-MM-DD). "
             "Prevents reopening legitimately exited trades where the wallet re-entered later. "
             "Default: 2026-03-10 (the known false-closure batch).",
    )
    parser.add_argument(
        "--closed-before",
        default=None,
        help="Only consider trades closed before this date (YYYY-MM-DD). Optional upper bound.",
    )
    args = parser.parse_args()
    selected_trade_ids = {
        int(part.strip())
        for part in str(args.trade_ids or "").split(",")
        if part.strip()
    }
    if args.apply and not selected_trade_ids:
        parser.error("--apply requires --trade-ids with the audited false-closure trade IDs")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Ensure disqualification columns exist
    from db import ensure_paper_trades_disqualification_columns
    try:
        ensure_paper_trades_disqualification_columns(conn)
    except Exception:
        # Columns might already exist or table might not exist
        pass

    # Find candidates: closed trades with exit_reason='source_exited'
    # Date filters prevent reopening trades where the wallet legitimately exited
    # and later re-entered the same market (current position != original position).
    query = """
        SELECT trade_id, wallet, condition_id, outcome, status, exit_reason,
               entry_price, exit_price, pnl_usdc, opened_at, closed_at
        FROM paper_trades
        WHERE exit_reason = 'source_exited'
          AND closed_at >= ?
    """
    params = [args.closed_after]
    if args.closed_before:
        query += "  AND closed_at < ?\n"
        params.append(args.closed_before)
    if selected_trade_ids:
        placeholders = ",".join("?" for _ in selected_trade_ids)
        query += f"  AND trade_id IN ({placeholders})\n"
        params.extend(sorted(selected_trade_ids))
    query += "    ORDER BY closed_at DESC"
    candidates = conn.execute(query, params).fetchall()

    if not candidates:
        print("No source_exited trades found. Nothing to repair.")
        return

    candidates, dedupe_skipped = _dedupe_candidates(candidates)

    date_range = f"closed_at >= {args.closed_after}"
    if args.closed_before:
        date_range += f" AND closed_at < {args.closed_before}"
    original_count = len(candidates) + len(dedupe_skipped)
    print(f"Found {original_count} trade(s) with exit_reason='source_exited' ({date_range})")
    if dedupe_skipped:
        print(f"Deduped to {len(candidates)} newest candidate(s) by wallet/market/outcome key\n")
    else:
        print()
    print(f"{'Mode':>8}: {'DRY-RUN' if not args.apply else 'APPLY'}\n")

    reopened = []
    relabeled = []
    skipped = list(dedupe_skipped)

    for trade in candidates:
        trade_id = trade["trade_id"]
        wallet = trade["wallet"]
        condition_id = trade["condition_id"]
        outcome = trade["outcome"]
        closed_at = trade["closed_at"] or "?"
        prefix = f"  Trade #{trade_id} ({wallet[:10]}... / {condition_id[:16]}... / closed {closed_at})"

        # Check 1: No matching sell signal
        sell = conn.execute(
            "SELECT 1 FROM paper_sell_signals WHERE wallet = ? AND condition_id = ? LIMIT 1",
            (wallet, condition_id)
        ).fetchone()
        if sell:
            skipped.append((trade_id, "has matching sell signal"))
            print(f"{prefix}: SKIP — has matching sell signal")
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

        # Check 3: Market not resolved/archived
        try:
            r = requests.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=8,
            )
            if r.ok:
                m = r.json()
                if m.get("closed") or m.get("archived"):
                    skipped.append((trade_id, "market closed/archived"))
                    print(f"{prefix}: SKIP — market closed/archived")
                    continue
        except Exception:
            pass  # If CLOB fails, proceed with reopen (conservative)

        # Check 4: No duplicate open trade
        dup = conn.execute(
            """
            SELECT trade_id
            FROM paper_trades
            WHERE wallet = ? AND condition_id = ? AND LOWER(outcome) = LOWER(?) AND status = 'open'
            LIMIT 1
            """,
            (wallet, condition_id, outcome)
        ).fetchone()
        if dup:
            open_trade_id = int(dup["trade_id"])
            relabeled.append((trade_id, open_trade_id))
            print(f"{prefix}: RELABEL — superseded by open trade #{open_trade_id}")
            if args.apply:
                _relabel_duplicate_reason(conn, trade_id, open_trade_id)
            continue

        # All checks passed — this trade was falsely closed
        print(f"{prefix}: REOPEN — source still holds {source_size:.2f} shares")
        reopened.append(trade_id)

        if args.apply:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
            """, (now, "reopened: false source_exited (analyzer reclassification)", trade_id))
            conn.commit()

    print(f"\n{'='*60}")
    print(f"Summary: {len(reopened)} to reopen, {len(relabeled)} to relabel, {len(skipped)} skipped")
    if reopened:
        print(f"Trade IDs to reopen: {reopened}")
    if relabeled:
        print(f"Trade IDs relabeled: {[trade_id for trade_id, _ in relabeled]}")
    if skipped:
        print("Skipped:")
        for tid, reason in skipped:
            print(f"  #{tid}: {reason}")
    if reopened and not args.apply:
        print(f"\nRe-run with --apply to execute: python3 scripts/repair_false_source_exits.py --apply")


if __name__ == "__main__":
    main()
