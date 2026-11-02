#!/usr/bin/env python3
"""Backfill pnl_usdc, current_price, current_value for strategy_positions.

- Adds the new columns if they don't exist yet (idempotent).
- Backfills pnl_usdc for all CLOSED positions where pnl_usdc IS NULL.
- Backfills pnl_usdc = 0 and current_price = avg_entry_price for OPEN positions
  where pnl_usdc IS NULL (so the column is never NULL for active positions).
- Handles both YES and NO outcome positions with the correct formula:
    pnl_usdc = shares * (exit_price - entry_price)
  For NO positions, entry_price and exit_price are already stored as NO prices,
  so the formula is identical.
"""
from __future__ import annotations

import os
import sys
import sqlite3

# Resolve project root (one level up from scripts/)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

DB_PATH = os.getenv("DB_PATH", os.path.join(_ROOT, "sniper_tracker.db"))


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add the three new columns if they don't exist (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(strategy_positions)").fetchall()}
    for col, definition in [
        ("pnl_usdc", "REAL"),
        ("current_price", "REAL"),
        ("current_value", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE strategy_positions ADD COLUMN {col} {definition}")
            print(f"  Added column strategy_positions.{col}")
        else:
            print(f"  Column strategy_positions.{col} already exists")
    conn.commit()


def backfill_closed(conn: sqlite3.Connection) -> int:
    """Fill pnl_usdc for closed positions where it is NULL."""
    rows = conn.execute(
        """
        SELECT position_id, outcome, avg_entry_price, avg_exit_price, filled_entry_shares
        FROM strategy_positions
        WHERE state = 'closed'
          AND pnl_usdc IS NULL
        """
    ).fetchall()

    updated = 0
    for pos_id, outcome, entry_price, exit_price, shares in rows:
        entry_price = float(entry_price or 0.0)
        exit_price = float(exit_price or entry_price)
        shares = float(shares or 0.0)
        # Uniform formula: pnl = shares * (exit - entry)
        # Works for YES and NO since both store prices in the same token space.
        pnl = shares * (exit_price - entry_price)
        conn.execute(
            """
            UPDATE strategy_positions
            SET pnl_usdc = ?,
                current_price = ?,
                current_value = ?
            WHERE position_id = ?
            """,
            (pnl, exit_price, shares * exit_price, pos_id),
        )
        updated += 1

    conn.commit()
    return updated


def backfill_open(conn: sqlite3.Connection) -> int:
    """Seed pnl_usdc=0, current_price=entry_price for open positions where pnl_usdc IS NULL."""
    rows = conn.execute(
        """
        SELECT position_id, avg_entry_price, entry_price, filled_entry_shares, entry_shares
        FROM strategy_positions
        WHERE state IN ('open', 'pending_entry', 'pending_exit')
          AND pnl_usdc IS NULL
        """
    ).fetchall()

    updated = 0
    for pos_id, avg_entry, entry, filled_shares, entry_shares in rows:
        ep = float(avg_entry or entry or 0.0)
        sh = float(filled_shares or entry_shares or 0.0)
        conn.execute(
            """
            UPDATE strategy_positions
            SET pnl_usdc = 0,
                current_price = ?,
                current_value = ?
            WHERE position_id = ?
            """,
            (ep, sh * ep, pos_id),
        )
        updated += 1

    conn.commit()
    return updated


def main() -> None:
    print(f"Backfilling strategy_positions P&L in {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    print("\n[1] Ensuring columns exist...")
    ensure_columns(conn)

    print("\n[2] Backfilling closed positions...")
    closed_updated = backfill_closed(conn)
    print(f"  Updated {closed_updated} closed positions")

    print("\n[3] Seeding open positions...")
    open_updated = backfill_open(conn)
    print(f"  Updated {open_updated} open positions")

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    still_null = conn.execute("SELECT COUNT(*) FROM strategy_positions WHERE pnl_usdc IS NULL").fetchone()[0]
    print(f"\nDone. Total positions: {total} | Still NULL pnl_usdc: {still_null}")

    if still_null > 0:
        print("  (NULL remaining likely means state='error' or edge cases — not critical)")

    conn.close()


if __name__ == "__main__":
    main()
