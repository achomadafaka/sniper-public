"""
Migration 005: Create signal_history and score_history tables.

Append-only tables that capture point-in-time scores every time the analyzer
runs, enabling true backtesting with historical gate replay.

Safe to run multiple times (uses CREATE TABLE IF NOT EXISTS).
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            is_open INTEGER NOT NULL DEFAULT 1,
            entry_price REAL NOT NULL DEFAULT 0,
            entry_notional REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            position_insider_score REAL NOT NULL DEFAULT 0,
            position_speculator_score REAL NOT NULL DEFAULT 0,
            position_classification TEXT NOT NULL DEFAULT '',
            activity_state TEXT NOT NULL DEFAULT 'holding',
            hold_hours REAL NOT NULL DEFAULT 0,
            entry_anchor_ts INTEGER NOT NULL DEFAULT 0,
            analyzed_ts INTEGER NOT NULL,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_signal_history_wallet_cond
            ON signal_history(wallet, condition_id, outcome, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_history_ts
            ON signal_history(analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_history_anchor
            ON signal_history(condition_id, entry_anchor_ts);

        CREATE TABLE IF NOT EXISTS score_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            insider_score REAL NOT NULL DEFAULT 0,
            speculator_score REAL NOT NULL DEFAULT 0,
            classification TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT '',
            strategy_group TEXT NOT NULL DEFAULT '',
            win_rate REAL NOT NULL DEFAULT 0,
            closed_sample_size INTEGER NOT NULL DEFAULT 0,
            qualifying_roi REAL,
            qualifying_capital REAL NOT NULL DEFAULT 0,
            pnl REAL NOT NULL DEFAULT 0,
            position_count INTEGER NOT NULL DEFAULT 0,
            wallet_edge_tier TEXT NOT NULL DEFAULT '',
            followability_cached REAL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            analyzed_ts INTEGER NOT NULL,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_score_history_wallet_ts
            ON score_history(wallet, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_score_history_ts
            ON score_history(analyzed_ts DESC);
        """
    )
    print("Migration 005: signal_history + score_history tables created.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 005 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
