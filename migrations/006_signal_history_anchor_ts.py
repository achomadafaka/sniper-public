"""
Migration 006: Add entry_anchor_ts to signal_history.

Captures the immutable first-detection timestamp from position_signals so that
convergence timing queries can use the true anchor time instead of analyzed_ts.

Safe to run multiple times (uses ADD COLUMN IF NOT EXISTS pattern via try/except).
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    # Add column (SQLite has no IF NOT EXISTS for ALTER TABLE ADD COLUMN)
    try:
        conn.execute(
            "ALTER TABLE signal_history ADD COLUMN entry_anchor_ts INTEGER NOT NULL DEFAULT 0"
        )
        print("Migration 006: added entry_anchor_ts column to signal_history.")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("Migration 006: entry_anchor_ts column already exists, skipping.")
        else:
            raise

    # Backfill from position_signals where possible
    conn.execute(
        """UPDATE signal_history
           SET entry_anchor_ts = COALESCE(
               (SELECT ps.entry_anchor_ts
                FROM position_signals ps
                WHERE ps.wallet = signal_history.wallet
                  AND ps.condition_id = signal_history.condition_id
                  AND ps.outcome = signal_history.outcome),
               0
           )
           WHERE entry_anchor_ts = 0"""
    )
    backfilled = conn.execute(
        "SELECT changes()"
    ).fetchone()[0]
    print(f"Migration 006: backfilled {backfilled} rows with entry_anchor_ts from position_signals.")

    # Add index for convergence queries
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_signal_history_anchor
           ON signal_history(condition_id, entry_anchor_ts)"""
    )
    print("Migration 006: convergence index created.")
    conn.commit()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 006 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
