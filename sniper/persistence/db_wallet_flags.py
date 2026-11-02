"""Wallet flags — favorite (pin to top + boosted re-analyze) and soft-delete (blacklist from
intake + hide from surfaces). Analyzer-owned, wallet-keyed (lowercased), reversible. Separate
from the legacy `watchlist_wallets` table (different concept). Lazy-ensure pattern."""
from __future__ import annotations

import sqlite3
import time

_DDL = """
CREATE TABLE IF NOT EXISTS wallet_flags (
    wallet      TEXT PRIMARY KEY,
    favorite_ts INTEGER NOT NULL DEFAULT 0,
    deleted_ts  INTEGER NOT NULL DEFAULT 0,
    updated_ts  INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_wallet_flags_tables(conn: sqlite3.Connection) -> None:
    from db import with_db_init_lock
    with with_db_init_lock(conn):
        conn.executescript(_DDL)


def _norm(w: str) -> str:
    return (w or "").strip().lower()


def _now(now: int | None) -> int:
    return int(now if now is not None else time.time())


def _upsert(conn: sqlite3.Connection, wallet: str, col: str, value: int, now: int) -> None:
    ensure_wallet_flags_tables(conn)
    w = _norm(wallet)
    conn.execute(
        f"INSERT INTO wallet_flags (wallet, {col}, updated_ts) VALUES (?,?,?) "
        f"ON CONFLICT(wallet) DO UPDATE SET {col}=excluded.{col}, updated_ts=excluded.updated_ts",
        (w, int(value), now))
    conn.commit()


def set_favorite(conn, wallet, *, now=None) -> None:
    _upsert(conn, wallet, "favorite_ts", _now(now), _now(now))


def clear_favorite(conn, wallet, *, now=None) -> None:
    _upsert(conn, wallet, "favorite_ts", 0, _now(now))


def soft_delete(conn, wallet, *, now=None) -> None:
    _upsert(conn, wallet, "deleted_ts", _now(now), _now(now))


def restore(conn, wallet, *, now=None) -> None:
    _upsert(conn, wallet, "deleted_ts", 0, _now(now))


def is_favorite(conn, wallet) -> bool:
    try:
        row = conn.execute("SELECT favorite_ts FROM wallet_flags WHERE wallet=?",
                           (_norm(wallet),)).fetchone()
    except sqlite3.OperationalError:
        return False  # flags table not ensured yet -> default "not flagged" (safe)
    return bool(row and int(row[0]) > 0)


def is_deleted(conn, wallet) -> bool:
    try:
        row = conn.execute("SELECT deleted_ts FROM wallet_flags WHERE wallet=?",
                           (_norm(wallet),)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and int(row[0]) > 0)


def favorites(conn) -> list[str]:
    try:
        return [r[0] for r in conn.execute(
            "SELECT wallet FROM wallet_flags WHERE favorite_ts > 0 AND deleted_ts = 0 "
            "ORDER BY favorite_ts DESC")]
    except sqlite3.OperationalError:
        return []  # flags table not ensured yet -> no favorites (safe)


def flags_for(conn, wallets) -> dict[str, dict]:
    """Return {wallet -> {favorite, deleted, favorite_ts, deleted_ts}} for wallets with a row."""
    norm = {_norm(w) for w in (wallets or [])}
    if not norm:
        return {}
    qmarks = ",".join("?" * len(norm))
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
                f"SELECT wallet, favorite_ts, deleted_ts FROM wallet_flags WHERE wallet IN ({qmarks})",
                tuple(norm))
    except sqlite3.OperationalError:
        return {}
    for r in rows:
        out[r["wallet"]] = {"favorite": int(r["favorite_ts"]) > 0, "deleted": int(r["deleted_ts"]) > 0,
                            "favorite_ts": int(r["favorite_ts"]), "deleted_ts": int(r["deleted_ts"])}
    return out
