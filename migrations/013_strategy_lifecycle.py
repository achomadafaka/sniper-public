"""Migration 013: Add lifecycle_stage column to strategies table."""
import sqlite3
import sys


def _column_exists(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def run(conn):
    if not _column_exists(conn, "strategies", "lifecycle_stage"):
        conn.execute("ALTER TABLE strategies ADD COLUMN lifecycle_stage TEXT NOT NULL DEFAULT 'paper'")
        conn.commit()
        print("Migration 013: added lifecycle_stage to strategies")
    else:
        print("Migration 013: lifecycle_stage already exists — skipped")


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
        print(f"Migration 013 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
