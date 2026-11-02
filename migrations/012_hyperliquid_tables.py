"""
Migration 012: Create Hyperliquid and signal queue tables.

Creates:
  - hl_trades: trade-level data from HL WebSocket
  - hl_liquidations: enriched liquidation events
  - hl_positions: strategy positions for leveraged perps
  - hl_fills: individual fill/funding events per position
  - signal_queue: unified cross-venue signal instrumentation
  - copy_viability: per-wallet copy scoring

Safe to run while services are active (only creates tables, no data changes).
"""

import sqlite3
import sys


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def run(conn: sqlite3.Connection) -> None:
    from sniper.persistence.db_hyperliquid import ensure_all_hl_tables

    tables_before = set()
    for name in ("hl_trades", "hl_liquidations", "hl_positions", "hl_fills",
                 "signal_queue", "copy_viability"):
        if _table_exists(conn, name):
            tables_before.add(name)

    ensure_all_hl_tables(conn)

    tables_after = set()
    for name in ("hl_trades", "hl_liquidations", "hl_positions", "hl_fills",
                 "signal_queue", "copy_viability"):
        if _table_exists(conn, name):
            tables_after.add(name)

    created = tables_after - tables_before
    if created:
        print(f"Migration 012: created tables: {', '.join(sorted(created))}")
    else:
        print("Migration 012: all tables already exist — skipped")


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 012 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
