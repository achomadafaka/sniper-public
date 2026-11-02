"""Core SQLite connection and schema helper functions."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from threading import local


DEFAULT_DB_PATH = "sniper_tracker.db"
CORE_SCHEMA_VERSION = 2
_INIT_LOCK_STATE = local()


def get_db_path() -> str:
    return os.getenv("DB_PATH", DEFAULT_DB_PATH)


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or get_db_path(), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA wal_autocheckpoint=500;")
    # Cap the WAL. Without this (default -1 = unlimited) passive checkpoints recycle the WAL in
    # place but never shrink the file, so a one-off write burst leaves it stuck at its high-water
    # mark forever while a long-lived reader (the web server) keeps the DB open. That is what
    # tripped the ops_health `wal_bloat` warning (276MB, only ~28MB live).
    # journal_size_limit truncates the WAL back down when a checkpoint resets it.
    #
    # 16MiB, lowered from 64MiB on 2026-07-29. The limit is a floor SQLite trims *down to*, so at
    # 64MiB this file simply parked at 64MiB: measured 67,108,864 bytes on disk holding 174 live
    # frames (717KB) — 66MB of dead space that never came back. 16MiB is 8x the 500-page
    # autocheckpoint watermark above and 23x the observed live content, so the steady state never
    # reaches the truncate path and there is no truncate/regrow churn on the funded ledger.
    conn.execute("PRAGMA journal_size_limit=16777216;")
    conn.execute("PRAGMA cache_size=-8000;")
    return conn


@contextmanager
def connection_scope(db_path: str | None = None):
    """Open a writable SQLite connection for a single unit of work and close it deterministically."""
    conn = get_connection(db_path)
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def get_readonly_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-only SQLite connection. Raises on any write attempt.

    WAL-pinning fix (WO-10, root-cause reproduced 2026-07-25): a long-lived reader that sits with an
    OPEN transaction pins the WAL — a TRUNCATE checkpoint returns busy=1 and the WAL grows unbounded
    (the funded runner / web server / discovery worker each did this, each "fixed" by a bounce).
    Python's default `isolation_level=""` leaves a read transaction open across statements once one is
    begun; `isolation_level=None` (autocommit) ends each read's snapshot the moment its statement
    finishes, so an idle reader holds nothing. `query_only=1` is belt-and-braces: this handle can
    never even attempt a write that would auto-begin a transaction. Consumed reads on this connection
    no longer pin the WAL (verified: TRUNCATE completes, WAL -> 0, vs busy=1 / WAL-growth before)."""
    path = db_path or get_db_path()
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA query_only=1;")
    return conn


def _connection_db_path(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        path = str(row[2] or "").strip()
        if not path or path == ":memory:" or path.startswith("file::memory:"):
            continue
        return path
    return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _pragma_user_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _core_schema_is_current(conn: sqlite3.Connection) -> bool:
    if _pragma_user_version(conn) < CORE_SCHEMA_VERSION:
        return False
    return all(
        _table_exists(conn, table)
        for table in (
            "market_metadata",
            "positions",
            "alerts_outbox",
            "agent_mutes",
            "mcp_audit_log",
            "replay_configs",
            "replay_runs",
            "wallet_registry",
            "wallet_memberships",
            "wallet_tags",
        )
    )


@contextmanager
def with_db_init_lock(conn: sqlite3.Connection):
    """Serialize schema/bootstrap writes across processes for a shared SQLite DB."""
    db_path = _connection_db_path(conn)
    if not db_path:
        yield
        return
    held_paths = getattr(_INIT_LOCK_STATE, "held_paths", {})
    held_depth = int(held_paths.get(db_path, 0))
    if held_depth > 0:
        held_paths[db_path] = held_depth + 1
        _INIT_LOCK_STATE.held_paths = held_paths
        try:
            yield
        finally:
            remaining = int(_INIT_LOCK_STATE.held_paths.get(db_path, 1)) - 1
            if remaining > 0:
                _INIT_LOCK_STATE.held_paths[db_path] = remaining
            else:
                _INIT_LOCK_STATE.held_paths.pop(db_path, None)
        return
    try:
        import fcntl
    except ImportError:
        yield
        return

    lock_path = f"{db_path}.init.lock"
    lock_dir = os.path.dirname(lock_path) or "."
    os.makedirs(lock_dir, exist_ok=True)
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        held_paths[db_path] = 1
        _INIT_LOCK_STATE.held_paths = held_paths
        yield
    finally:
        _INIT_LOCK_STATE.held_paths.pop(db_path, None)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    # Skip migration entirely if the table doesn't exist yet — it will be created
    # fresh with all columns when the owning module initialises its schema.
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not table_exists:
        return
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
