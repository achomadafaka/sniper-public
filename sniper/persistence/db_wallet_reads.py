"""Persistence for the analyzer's canonical reads — `wallet_analysis_reads` (immutable,
append-only) + a `wallet_analysis_latest` view.

Analyzer-owned. No legacy reuse, no downstream routing eligibility fields.
A re-analysis NEVER updates a prior read; it appends a new row whose `supersedes_read_id`
points at the previous latest. Idempotent per data snapshot `(wallet, analyzer_version,
data_as_of)` so the worker can't double-write the same evidence. Lazy-ensure pattern.
"""
from __future__ import annotations

import json
import sqlite3
import time

_DDL = """
CREATE TABLE IF NOT EXISTS wallet_analysis_reads (
    read_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet               TEXT    NOT NULL,
    analyzer_version     INTEGER NOT NULL,
    rule_version         INTEGER NOT NULL,
    data_as_of           INTEGER NOT NULL,
    primary_verdict      TEXT    NOT NULL,
    operational_route    TEXT    NOT NULL,
    history_depth        TEXT    NOT NULL DEFAULT 'thin',
    ex_ante_level        TEXT    NOT NULL DEFAULT 'none',
    ex_post_level        TEXT    NOT NULL DEFAULT 'none',
    coordination_level   TEXT    NOT NULL DEFAULT 'none',
    recent_anomaly_level TEXT    NOT NULL DEFAULT 'none',
    archetype            TEXT    NOT NULL DEFAULT 'unknown',
    intent               TEXT    NOT NULL DEFAULT '',
    headline             TEXT    NOT NULL DEFAULT '',
    evidence_json        TEXT    NOT NULL DEFAULT '{}',
    edge_location_json   TEXT    NOT NULL DEFAULT '{}',
    warnings_json        TEXT    NOT NULL DEFAULT '[]',
    source_snapshot_hash TEXT    NOT NULL DEFAULT '',
    supersedes_read_id   INTEGER,
    created_ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_war_wallet_created
    ON wallet_analysis_reads(wallet, created_ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_war_snapshot
    ON wallet_analysis_reads(wallet, analyzer_version, rule_version, data_as_of);
CREATE VIEW IF NOT EXISTS wallet_analysis_latest AS
    SELECT r.* FROM wallet_analysis_reads r
    WHERE r.read_id = (
        SELECT r2.read_id FROM wallet_analysis_reads r2
        WHERE r2.wallet = r.wallet
        ORDER BY r2.created_ts DESC, r2.read_id DESC LIMIT 1
    );
"""


def ensure_wallet_reads_tables(conn: sqlite3.Connection) -> None:
    from db import with_db_init_lock
    with with_db_init_lock(conn):
        conn.executescript(_DDL)


def _norm(wallet: str) -> str:
    return (wallet or "").strip().lower()


def append_read(conn: sqlite3.Connection, wallet: str, read, *, now_ts: int | None = None) -> int:
    """Append an immutable read for `wallet`. Idempotent on (wallet, analyzer_version,
    data_as_of) — returns the existing read_id if the same snapshot was already written.
    Sets supersedes_read_id to the prior latest read."""
    prov = read.provenance or {}
    av = int(prov.get("analyzer_version", 0))
    rv = int(prov.get("rule_version", 0))
    da = int(prov.get("data_as_of", 0))
    w = _norm(wallet)
    existing = conn.execute(
        "SELECT read_id FROM wallet_analysis_reads WHERE LOWER(wallet)=? AND analyzer_version=? "
        "AND rule_version=? AND data_as_of=?", (w, av, rv, da)).fetchone()
    if existing:
        return int(existing[0])

    prior = conn.execute(
        "SELECT read_id FROM wallet_analysis_reads WHERE LOWER(wallet)=? "
        "ORDER BY created_ts DESC, read_id DESC LIMIT 1",
        (w,)).fetchone()
    supersedes = int(prior[0]) if prior else None

    ev = read.evidence_profile or {}
    fresh = ev.get("fresh", {}) or {}
    ts = int(now_ts if now_ts is not None else time.time())
    cur = conn.execute(
        """INSERT INTO wallet_analysis_reads
           (wallet, analyzer_version, rule_version, data_as_of, primary_verdict, operational_route,
            history_depth, ex_ante_level, ex_post_level, coordination_level, recent_anomaly_level,
            archetype, intent, headline, evidence_json, edge_location_json, warnings_json,
            source_snapshot_hash, supersedes_read_id, created_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (w, av, rv, da, read.primary_verdict, read.operational_route,
         ev.get("history_depth", "thin"), fresh.get("ex_ante_level", "none"),
         fresh.get("ex_post_level", "none"), fresh.get("coordination_level", "none"),
         ev.get("recent_anomaly_level", "none"), ev.get("archetype", "unknown"),
         ev.get("intent", ""), read.headline, json.dumps(ev), json.dumps(read.edge_location or {}),
         json.dumps(read.warnings or []), str(prov.get("source_snapshot_hash", "")),
         supersedes, ts))
    conn.commit()
    return int(cur.lastrowid)


def latest_read(conn: sqlite3.Connection, wallet: str) -> dict | None:
    try:
        row = conn.execute(
            "SELECT * FROM wallet_analysis_reads WHERE LOWER(wallet)=? "
            "ORDER BY created_ts DESC, read_id DESC LIMIT 1",
            (_norm(wallet),)).fetchone()
    except sqlite3.OperationalError:
        return None  # reads table/view not ensured yet -> no read (callers degrade gracefully)
    return dict(row) if row else None


def all_latest(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every wallet's latest read, keyed by wallet. Empty if the table isn't ensured yet."""
    try:
        rows = conn.execute("SELECT * FROM wallet_analysis_reads ORDER BY created_ts, read_id").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["wallet"] = _norm(d.get("wallet", ""))
        out[d["wallet"]] = d
    return out
