from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.prices import (
    MarketPricePoint,
    acquire_shared_warehouse_lock,
    upsert_price_points,
)
from sniper.analysis.wallet_protocol.ledger.schema import ensure_index_schema, ensure_market_schema


def _connect_market(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "market_data.duckdb"))
    ensure_market_schema(conn)
    with duckdb.connect(str(tmp_path / "index.duckdb")) as index_conn:
        ensure_index_schema(index_conn)
    return conn


def _rows(conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def test_upsert_price_points_is_idempotent_and_preserves_first_capture_run_id(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        point = MarketPricePoint("token-a", 1, 1700000000, 0.42)

        assert upsert_price_points(conn, [point], source_version="source-v1", capture_run_id="run-1", captured_at=1) == 1
        assert upsert_price_points(conn, [point], source_version="source-v1", capture_run_id="run-2", captured_at=2) == 0

        rows = _rows(
            conn,
            """
            SELECT token_id, fidelity_minutes, ts, source_version, price, point_status,
                   first_capture_run_id, captured_at
            FROM market_price_points
            """,
        )
        assert rows == [
            {
                "token_id": "token-a",
                "fidelity_minutes": 1,
                "ts": 1700000000,
                "source_version": "source-v1",
                "price": 0.42,
                "point_status": "captured",
                "first_capture_run_id": "run-1",
                "captured_at": 1,
            }
        ]


def test_upsert_price_points_records_requested_no_data_rows(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        point = MarketPricePoint("token-a", 1, 1700000000, None, point_status="requested_no_data")

        written = upsert_price_points(conn, [point], source_version="source-v1", capture_run_id="run-1", captured_at=1)

        assert written == 1
        rows = _rows(conn, "SELECT price, point_status FROM market_price_points")
        assert rows == [{"price": None, "point_status": "requested_no_data"}]


def test_market_price_points_check_rejects_invalid_status_when_bypassed(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
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
                VALUES ('token-a', 1, 1700000000, 'source-v1', 0.42, 'api_error',
                        'clob_prices_history_batch', 'hash', 'polymarket_clob_v2', 'run-1', 1)
                """
            )


def test_upsert_price_points_requires_exclusive_shared_lock(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        with duckdb.connect(str(tmp_path / "index.duckdb")) as index_conn:
            ensure_index_schema(index_conn)
            acquire_shared_warehouse_lock(index_conn, "market_data")

        with pytest.raises(RuntimeError, match="shared warehouse lock"):
            upsert_price_points(
                conn,
                [MarketPricePoint("token-a", 1, 1700000000, 0.42)],
                source_version="source-v1",
                capture_run_id="run-1",
                captured_at=1,
            )


def test_upsert_price_points_releases_lock_on_failure(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        with pytest.raises(ValueError):
            upsert_price_points(
                conn,
                [MarketPricePoint("token-a", 1, 1700000000, 0.42, point_status="bad")],
                source_version="source-v1",
                capture_run_id="run-1",
                captured_at=1,
            )

        written = upsert_price_points(
            conn,
            [MarketPricePoint("token-a", 1, 1700000000, 0.42)],
            source_version="source-v1",
            capture_run_id="run-1",
            captured_at=1,
        )
        assert written == 1
