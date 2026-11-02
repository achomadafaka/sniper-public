from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import duckdb

from sniper.analysis.wallet_protocol.ledger.schema import ensure_index_schema


POINT_SOURCE = "clob_prices_history_batch"
SOURCE = "polymarket_clob_v2"
LOCK_NAME = "market_data"
_VALID_POINT_STATUSES = {"captured", "requested_no_data"}


@dataclass(frozen=True)
class MarketPricePoint:
    token_id: str
    fidelity_minutes: int
    ts: int
    price: float | None
    point_status: str = "captured"


def _json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def price_point_content_hash(point: MarketPricePoint, source_version: str) -> str:
    payload = {
        "token_id": point.token_id,
        "fidelity_minutes": point.fidelity_minutes,
        "ts": point.ts,
        "price": point.price,
        "source_version": source_version,
    }
    return _sha256_text(_json_dumps(payload))


def _market_db_path(conn: duckdb.DuckDBPyConnection) -> Path | None:
    for _seq, _name, db_path in conn.execute("PRAGMA database_list").fetchall():
        if db_path:
            return Path(db_path)
    return None


def _index_db_path(conn: duckdb.DuckDBPyConnection) -> Path | None:
    market_path = _market_db_path(conn)
    if market_path is None:
        return None
    return market_path.parent / "index.duckdb"


def acquire_shared_warehouse_lock(
    conn: duckdb.DuckDBPyConnection,
    lock_name: str = LOCK_NAME,
    *,
    acquired_at: int | None = None,
) -> None:
    ensure_index_schema(conn)
    now = int(time.time()) if acquired_at is None else acquired_at
    try:
        conn.execute(
            """
            INSERT INTO shared_warehouse_lock (lock_name, writer_pid, acquired_at, expires_at)
            VALUES (?, ?, ?, NULL)
            """,
            [lock_name, os.getpid(), now],
        )
    except duckdb.ConstraintException as exc:
        raise RuntimeError(f"shared warehouse lock already held: {lock_name}") from exc


def release_shared_warehouse_lock(conn: duckdb.DuckDBPyConnection, lock_name: str = LOCK_NAME) -> None:
    ensure_index_schema(conn)
    conn.execute("DELETE FROM shared_warehouse_lock WHERE lock_name = ?", [lock_name])


@contextmanager
def _shared_warehouse_lock(conn: duckdb.DuckDBPyConnection) -> Iterator[None]:
    index_path = _index_db_path(conn)
    if index_path is None:
        ensure_index_schema(conn)
        acquire_shared_warehouse_lock(conn)
        try:
            yield
        finally:
            release_shared_warehouse_lock(conn)
        return

    with duckdb.connect(str(index_path)) as index_conn:
        ensure_index_schema(index_conn)
        acquire_shared_warehouse_lock(index_conn)
        try:
            yield
        finally:
            release_shared_warehouse_lock(index_conn)


def _validate_point(point: MarketPricePoint) -> None:
    if point.point_status not in _VALID_POINT_STATUSES:
        raise ValueError(f"invalid point_status: {point.point_status}")
    if point.point_status == "captured" and point.price is None:
        raise ValueError("captured price point requires price")
    if not point.token_id:
        raise ValueError("token_id is required")


def upsert_price_points(
    conn: duckdb.DuckDBPyConnection,
    points: list[MarketPricePoint],
    *,
    source_version: str,
    capture_run_id: str,
    captured_at: int,
) -> int:
    if not source_version:
        raise ValueError("source_version is required")

    with _shared_warehouse_lock(conn):
        written = 0
        for point in points:
            _validate_point(point)
            result = conn.execute(
                """
                INSERT INTO market_price_points (
                    token_id,
                    fidelity_minutes,
                    ts,
                    source_version,
                    price,
                    point_status,
                    point_source,
                    content_hash,
                    source,
                    first_capture_run_id,
                    captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (token_id, fidelity_minutes, ts, source_version) DO NOTHING
                RETURNING token_id
                """,
                [
                    point.token_id,
                    point.fidelity_minutes,
                    point.ts,
                    source_version,
                    point.price,
                    point.point_status,
                    POINT_SOURCE,
                    price_point_content_hash(point, source_version),
                    SOURCE,
                    capture_run_id,
                    captured_at,
                ],
            ).fetchone()
            if result is not None:
                written += 1
        return written
