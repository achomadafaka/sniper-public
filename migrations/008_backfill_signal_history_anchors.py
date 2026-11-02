"""
Migration 008: Backfill entry_anchor_ts for legacy signal_history rows.

Migration 006 backfilled entry_anchor_ts from position_signals, but ~75% of
rows remained at 0 because the corresponding position_signals had already been
closed/removed.

This migration uses MIN(analyzed_ts) per (wallet, condition_id, outcome) group
as a reasonable proxy for when the position was first detected.  Not as precise
as the true entry_anchor_ts from position_signals, but sufficient to make the
replay engine include these rows instead of discarding them entirely.

Safe to run multiple times (only touches rows where entry_anchor_ts = 0).
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    # Count before
    before = conn.execute(
        "SELECT COUNT(*) FROM signal_history WHERE entry_anchor_ts = 0"
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM signal_history"
    ).fetchone()[0]
    print(f"Migration 008: {before}/{total} rows have entry_anchor_ts = 0")

    if before == 0:
        print("Migration 008: nothing to backfill, skipping.")
        return

    # For each (wallet, condition_id, outcome) group with entry_anchor_ts=0,
    # set entry_anchor_ts = the earliest analyzed_ts for that group.
    conn.execute("""
        UPDATE signal_history
        SET entry_anchor_ts = (
            SELECT MIN(sh2.analyzed_ts)
            FROM signal_history sh2
            WHERE sh2.wallet = signal_history.wallet
              AND sh2.condition_id = signal_history.condition_id
              AND LOWER(sh2.outcome) = LOWER(signal_history.outcome)
        )
        WHERE entry_anchor_ts = 0
    """)
    backfilled = conn.execute("SELECT changes()").fetchone()[0]
    print(f"Migration 008: backfilled {backfilled} rows using MIN(analyzed_ts) as anchor proxy.")

    # Verify
    remaining = conn.execute(
        "SELECT COUNT(*) FROM signal_history WHERE entry_anchor_ts = 0"
    ).fetchone()[0]
    print(f"Migration 008: {remaining} rows still have entry_anchor_ts = 0 after backfill.")

    conn.commit()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 008 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
