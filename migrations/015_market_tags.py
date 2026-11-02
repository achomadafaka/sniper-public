"""Migration 015: Create market_tags table for raw gamma tag ingestion.

Replaces the coarse `market_metadata.category` with raw gamma tag slugs,
one row per (condition_id, tag) pair. Enables opaque-tag analysis per
wallet-taxonomy-migration spec (docs/superpowers/specs/
2026-04-23-wallet-taxonomy-migration.md Stage 1).
"""
import sqlite3
import sys


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()[0] > 0


def ensure_market_tags_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_tags_tag ON market_tags(tag)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_tags_condition_id ON market_tags(condition_id)"
    )
    conn.commit()


def run(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "market_tags"):
        print("Migration 015: market_tags already exists — skipped")
        return
    ensure_market_tags_table(conn)
    print("Migration 015: created market_tags")


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
        print(f"Migration 015 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
