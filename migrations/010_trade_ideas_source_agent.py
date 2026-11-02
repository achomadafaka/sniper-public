"""
Migration 010: Add source_agent column to trade_ideas.

Tags each trade idea with the agent that generated it:
  'world_analyst'   — geopolitical / conflict / political
  'finance_analyst' — macro / markets / crypto
  NULL              — legacy rows from the original news_intel agent

Safe to run multiple times (ALTER TABLE is inside a try/except).
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    # Add source_agent column (no-op if already present)
    try:
        conn.execute("ALTER TABLE trade_ideas ADD COLUMN source_agent TEXT DEFAULT NULL")
        conn.commit()
        print("Migration 010: added source_agent column to trade_ideas.")
    except Exception:
        print("Migration 010: source_agent column already exists, skipping ALTER.")

    # Add index for fast per-agent queries
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ti_source_agent ON trade_ideas(source_agent)"
    )
    conn.commit()
    print("Migration 010: idx_ti_source_agent index ensured.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 010 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
