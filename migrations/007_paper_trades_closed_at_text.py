"""
Migration 007: Normalize legacy integer paper_trades.closed_at values to UTC text.

SQLite does not enforce a strict timestamp type, and older rows may contain unix
epoch integers. The paper-trader cooldown query compares closed_at against
datetime('now', '-1 day'), so mixed formats are unsafe.
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    before = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE typeof(closed_at) = 'integer'"
    ).fetchone()[0]
    print(f"Migration 007: integer closed_at rows before = {before}")

    conn.execute(
        """UPDATE paper_trades
           SET closed_at = datetime(closed_at, 'unixepoch')
           WHERE typeof(closed_at) = 'integer'"""
    )
    changed = conn.execute("SELECT changes()").fetchone()[0]

    after = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE typeof(closed_at) = 'integer'"
    ).fetchone()[0]
    print(f"Migration 007: normalized {changed} rows.")
    print(f"Migration 007: integer closed_at rows after = {after}")
    conn.commit()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 007 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
