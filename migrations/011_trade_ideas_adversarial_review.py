"""
Migration 011: Add adversarial review columns to trade_ideas.

Adds:
  - adversarial_score INTEGER
  - adversarial_reasoning TEXT
  - adversarial_approved INTEGER DEFAULT NULL

Safe to run multiple times.
"""

import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    additions = [
        ("adversarial_score", "INTEGER"),
        ("adversarial_reasoning", "TEXT"),
        ("adversarial_approved", "INTEGER DEFAULT NULL"),
    ]
    for column, definition in additions:
        try:
            conn.execute(f"ALTER TABLE trade_ideas ADD COLUMN {column} {definition}")
            conn.commit()
            print(f"Migration 011: added {column} to trade_ideas.")
        except Exception:
            print(f"Migration 011: {column} already exists, skipping ALTER.")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ti_review_queue ON trade_ideas(status, adversarial_approved, created_at DESC)"
    )
    conn.commit()
    print("Migration 011: idx_ti_review_queue index ensured.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 011 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
