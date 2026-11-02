"""Migration 014: Create hl_tracked_wallets and hl_whale_positions tables."""
import sqlite3
import sys


def _table_exists(conn, table):
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0] > 0


def run(conn):
    from sniper.persistence.db_hyperliquid import ensure_hl_tracked_wallets_table, ensure_hl_whale_positions_table

    created = []
    if not _table_exists(conn, "hl_tracked_wallets"):
        ensure_hl_tracked_wallets_table(conn)
        created.append("hl_tracked_wallets")

    if not _table_exists(conn, "hl_whale_positions"):
        ensure_hl_whale_positions_table(conn)
        created.append("hl_whale_positions")

    if created:
        print(f"Migration 014: created {', '.join(created)}")
    else:
        print("Migration 014: tables already exist — skipped")


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
        print(f"Migration 014 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
