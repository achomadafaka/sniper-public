"""The entry feature capture's own database (spec 2026-09-29-memecoin-entry-feature-capture-design.md).

Never the funded ledger: `data/memecoin_entry_features.db`, written only by the capture process.
One row per terminal entry attempt, inserted once (a re-read inserts nothing), plus the cursor.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_features (
    attempt_id TEXT PRIMARY KEY,
    network_id TEXT,
    mint TEXT,
    source_user_id TEXT,
    signal_source TEXT,
    delivery_lane TEXT,
    decision_at TEXT,
    completed_at TEXT,
    outcome TEXT,
    outcome_reason TEXT,
    tier INTEGER,
    feature_version INTEGER NOT NULL,
    features_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    read_lag_json TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_features_decision ON entry_features (decision_at);
CREATE TABLE IF NOT EXISTS capture_cursor (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
"""

COLUMNS = ("attempt_id", "network_id", "mint", "source_user_id", "signal_source",
           "delivery_lane", "decision_at", "completed_at",
           "outcome", "outcome_reason", "tier", "feature_version", "features_json",
           "coverage_json", "sources_json", "read_lag_json", "computed_at")


def connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert(conn, row):
    """True when the row was new. An attempt already captured is left exactly as it was."""
    cur = conn.execute("INSERT OR IGNORE INTO entry_features (%s) VALUES (%s)"
                       % (",".join(COLUMNS), ",".join("?" * len(COLUMNS))),
                       tuple(row.get(c) for c in COLUMNS))
    conn.commit()
    return cur.rowcount == 1


def captured(conn, attempt_ids):
    ids = list(attempt_ids)
    if not ids:
        return set()
    return {r[0] for r in conn.execute(
        "SELECT attempt_id FROM entry_features WHERE attempt_id IN (%s)" % ",".join("?" * len(ids)),
        ids)}


def get_cursor(conn, name="attempts"):
    row = conn.execute("SELECT completed_at FROM capture_cursor WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def set_cursor(conn, completed_at, name="attempts"):
    conn.execute("INSERT INTO capture_cursor (name, completed_at) VALUES (?, ?) "
                 "ON CONFLICT(name) DO UPDATE SET completed_at=excluded.completed_at "
                 "WHERE excluded.completed_at > capture_cursor.completed_at", (name, completed_at))
    conn.commit()
