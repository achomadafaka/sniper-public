"""
Migration 004: Slim flagged_users table — remove 24 duplicated scoring columns.

All scoring data now lives exclusively in user_analysis and is joined at query
time. flagged_users keeps only: wallet, flagged_ts, source_note, updated_at.

Safe to run multiple times (idempotent via column-existence check).
"""

import sqlite3
import sys


_COLUMNS_TO_DROP = [
    "position_count",
    "pnl",
    "max_position_value",
    "top_open_position_title",
    "top_open_position_value",
    "total_open_value",
    "trade_count",
    "low_buy_hits_14d",
    "suspicion_score",
    "insider_score",
    "speculator_score",
    "classification",
    "win_rate",
    "closed_sample_size",
    "high_winrate_qualified",
    "account_age_days",
    "is_fresh_account",
    "position_signal_count",
    "position_insider_candidate_count",
    "position_speculator_count",
    "max_position_insider_score",
    "max_position_speculator_score",
    "tier",
    "strategy_group",
    "strategy_confidence",
    "strategy_note",
]


def run(conn: sqlite3.Connection) -> None:
    """Rebuild flagged_users keeping only the 4 slim columns."""
    # Check current columns
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(flagged_users)").fetchall()
    }
    if not existing.intersection(_COLUMNS_TO_DROP):
        print("Migration 004: flagged_users already slim — nothing to do.")
        return

    print("Migration 004: rebuilding flagged_users (slim schema)…")

    conn.executescript(
        """
        BEGIN;

        -- Create the new slim table
        CREATE TABLE IF NOT EXISTS flagged_users_new (
            wallet      TEXT PRIMARY KEY,
            flagged_ts  INTEGER NOT NULL,
            source_note TEXT,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Copy only the columns that survive
        INSERT OR REPLACE INTO flagged_users_new (wallet, flagged_ts, source_note, updated_at)
        SELECT wallet, flagged_ts, source_note, updated_at
        FROM flagged_users;

        -- Swap
        DROP TABLE flagged_users;
        ALTER TABLE flagged_users_new RENAME TO flagged_users;

        COMMIT;
        """
    )
    print("Migration 004: done.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 004 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
