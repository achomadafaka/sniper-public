"""Universal analyzer intake queue — `analyzer_read_queue`.

Whatever enters the system (discovery / followed / watchlist / manual / favorite / later:
stream) lands here and is leased by the worker for a full read. Analyzer-owned; NOT the
legacy `analysis_runs` / discovery queues. One live (pending|leased) row per wallet
(partial unique index). Atomic lease with heartbeat + expiry reclaim so a dead worker's
lease is recoverable; retry-with-backoff then terminal failure. Lazy-ensure pattern.
"""
from __future__ import annotations

import sqlite3
import time

_DDL = """
CREATE TABLE IF NOT EXISTS analyzer_read_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT    NOT NULL,
    source            TEXT    NOT NULL,
    source_ref        TEXT    NOT NULL DEFAULT '',
    reason            TEXT    NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 100,
    status            TEXT    NOT NULL DEFAULT 'pending',
    lease_owner       TEXT    NOT NULL DEFAULT '',
    lease_expires_ts  INTEGER NOT NULL DEFAULT 0,
    heartbeat_ts      INTEGER NOT NULL DEFAULT 0,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_ts     INTEGER NOT NULL DEFAULT 0,
    analyzer_version  INTEGER NOT NULL DEFAULT 0,
    data_as_of        INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT    NOT NULL DEFAULT '',
    enqueued_ts       INTEGER NOT NULL,
    updated_ts        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_waq_wallet_live
    ON analyzer_read_queue(wallet) WHERE status IN ('pending', 'leased');
CREATE INDEX IF NOT EXISTS idx_waq_status_priority
    ON analyzer_read_queue(status, priority, next_retry_ts);
CREATE TABLE IF NOT EXISTS analyzer_fdr_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_test_index INTEGER NOT NULL
);
"""


def ensure_queue_tables(conn: sqlite3.Connection) -> None:
    from db import with_db_init_lock
    with with_db_init_lock(conn):
        conn.executescript(_DDL)
    # Serialize lease contention across worker processes: the guarded UPDATE...WHERE status='pending'
    # is a correct compare-and-swap (the loser retries), but without a busy timeout concurrent writers
    # raise "database is locked". 30s matches the project's connection timeout (ADR-001). Multi-worker
    # BEGIN IMMEDIATE serialization is a deferred hardening; v1 runs a single worker.
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass


def _norm(wallet: str) -> str:
    return (wallet or "").strip().lower()


def enqueue(conn: sqlite3.Connection, wallet: str, source: str, *, source_ref: str = "",
            reason: str = "", priority: int = 100, now: int | None = None) -> bool:
    """Add a wallet to the queue. Idempotent: returns False (no-op) if a live row exists."""
    ts = int(now if now is not None else time.time())
    w = _norm(wallet)
    existing = conn.execute(
        "SELECT id FROM analyzer_read_queue WHERE LOWER(wallet)=? AND status IN ('pending', 'leased') "
        "LIMIT 1", (w,)).fetchone()
    if existing:
        return False
    try:
        conn.execute(
            """INSERT INTO analyzer_read_queue
               (wallet, source, source_ref, reason, priority, status, enqueued_ts, updated_ts)
               VALUES (?,?,?,?,?, 'pending', ?, ?)""",
            (w, source, source_ref, reason, int(priority), ts, ts))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def allocate_test_index(conn: sqlite3.Connection) -> int:
    """Atomically allocate a monotone global test index for online PFER spending."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO analyzer_fdr_state (id, next_test_index) VALUES (1, 1)")
        row = conn.execute("SELECT next_test_index FROM analyzer_fdr_state WHERE id=1").fetchone()
        idx = int(row[0])
        conn.execute("UPDATE analyzer_fdr_state SET next_test_index=? WHERE id=1", (idx + 1,))
        conn.commit()
        return idx
    except Exception:
        conn.rollback()
        raise


def lease(conn: sqlite3.Connection, owner: str, *, now: int | None = None,
          ttl_s: int = 600) -> dict | None:
    """Atomically claim the highest-priority due pending row. Reclaims expired leases first.
    Returns the leased row as a dict, or None when nothing is due."""
    ts = int(now if now is not None else time.time())
    # reclaim leases whose holder went dark
    conn.execute("UPDATE analyzer_read_queue SET status='pending', updated_ts=? "
                 "WHERE status='leased' AND lease_expires_ts <= ?", (ts, ts))
    for _ in range(8):  # retry on lost race (multi-worker); single-conn tests take one pass
        row = conn.execute(
            "SELECT id FROM analyzer_read_queue WHERE status='pending' AND next_retry_ts <= ? "
            "ORDER BY priority, enqueued_ts LIMIT 1", (ts,)).fetchone()
        if not row:
            return None
        rid = int(row[0])
        cur = conn.execute(
            "UPDATE analyzer_read_queue SET status='leased', lease_owner=?, lease_expires_ts=?, "
            "heartbeat_ts=?, updated_ts=? WHERE id=? AND status='pending'",
            (owner, ts + ttl_s, ts, ts, rid))
        conn.commit()
        if cur.rowcount == 1:
            return dict(conn.execute("SELECT * FROM analyzer_read_queue WHERE id=?", (rid,)).fetchone())
    return None


def heartbeat(conn: sqlite3.Connection, qid: int, *, now: int | None = None, ttl_s: int = 600) -> None:
    ts = int(now if now is not None else time.time())
    conn.execute("UPDATE analyzer_read_queue SET heartbeat_ts=?, lease_expires_ts=?, updated_ts=? "
                 "WHERE id=? AND status='leased'", (ts, ts + ttl_s, ts, int(qid)))
    conn.commit()


def complete(conn: sqlite3.Connection, qid: int, *, now: int | None = None,
             analyzer_version: int = 0, data_as_of: int = 0) -> None:
    ts = int(now if now is not None else time.time())
    conn.execute("UPDATE analyzer_read_queue SET status='done', analyzer_version=?, data_as_of=?, "
                 "updated_ts=? WHERE id=?", (int(analyzer_version), int(data_as_of), ts, int(qid)))
    conn.commit()


def fail(conn: sqlite3.Connection, qid: int, err: str, *, now: int | None = None,
         backoff_s: int = 300, max_attempts: int = 5) -> None:
    """Bump attempt_count; reschedule (pending + next_retry) until max_attempts, then 'failed'."""
    ts = int(now if now is not None else time.time())
    row = conn.execute("SELECT attempt_count FROM analyzer_read_queue WHERE id=?", (int(qid),)).fetchone()
    attempts = (int(row[0]) if row else 0) + 1
    if attempts < max_attempts:
        conn.execute("UPDATE analyzer_read_queue SET status='pending', attempt_count=?, "
                     "next_retry_ts=?, last_error=?, lease_owner='', updated_ts=? WHERE id=?",
                     (attempts, ts + backoff_s, str(err)[:500], ts, int(qid)))
    else:
        conn.execute("UPDATE analyzer_read_queue SET status='failed', attempt_count=?, "
                     "last_error=?, updated_ts=? WHERE id=?", (attempts, str(err)[:500], ts, int(qid)))
    conn.commit()
