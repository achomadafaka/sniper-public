"""
Migration 009: Add pause metadata columns to strategies table.

Adds:
  - paused_at TEXT    — ISO timestamp of when the strategy was last paused
  - pause_reason TEXT — machine-readable reason (e.g. 'wallet_degradation', 'manual')

Both columns are nullable; historical rows get NULL (unknown pause origin).
Safe to run while services are active (only adds columns, no data changes).
"""

import sqlite3
import sys


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def run(conn: sqlite3.Connection) -> None:
    added = []

    if not _column_exists(conn, "strategies", "paused_at"):
        conn.execute("ALTER TABLE strategies ADD COLUMN paused_at TEXT")
        added.append("paused_at")

    if not _column_exists(conn, "strategies", "pause_reason"):
        conn.execute("ALTER TABLE strategies ADD COLUMN pause_reason TEXT")
        added.append("pause_reason")

    if added:
        conn.commit()
        print(f"Migration 009: added columns to strategies: {', '.join(added)}")
    else:
        print("Migration 009: columns already exist — skipped")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 009 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
