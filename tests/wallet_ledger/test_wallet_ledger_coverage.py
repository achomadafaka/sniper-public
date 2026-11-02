from __future__ import annotations

import random
from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.coverage import (
    compute_uncovered,
    merged_coverage_ranges,
    record_coverage_range,
)
from sniper.analysis.wallet_protocol.ledger.schema import ensure_market_schema


def _connect_market(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "market_data.duckdb"))
    ensure_market_schema(conn)
    return conn


def _rows(conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _record(
    conn: duckdb.DuckDBPyConnection,
    start: int,
    end: int,
    status: str = "fully_captured",
    *,
    token_id: str = "token-a",
) -> int:
    return record_coverage_range(
        conn,
        token_id=token_id,
        fidelity_minutes=1,
        source_version="source-v1",
        range_start_ts=start,
        range_end_ts=end,
        coverage_status=status,
        source="polymarket_clob_v2",
        api_error_msg="boom" if status == "failed" else None,
        content_hash=f"hash-{token_id}-{start}-{end}-{status}",
        first_capture_run_id="run-1",
        captured_at=1,
    )


def test_record_coverage_range_writes_statuses_idempotently(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        assert _record(conn, 10, 20) == 1
        assert _record(conn, 30, 40, "partial") == 1
        assert _record(conn, 50, 60, "failed") == 1
        assert _record(conn, 10, 20) == 0

        rows = _rows(
            conn,
            """
            SELECT range_start_ts, range_end_ts, coverage_status, api_error_msg
            FROM market_price_coverage_ranges
            ORDER BY range_start_ts
            """,
        )
        assert rows == [
            {"range_start_ts": 10, "range_end_ts": 20, "coverage_status": "fully_captured", "api_error_msg": None},
            {"range_start_ts": 30, "range_end_ts": 40, "coverage_status": "partial", "api_error_msg": None},
            {"range_start_ts": 50, "range_end_ts": 60, "coverage_status": "failed", "api_error_msg": "boom"},
        ]


def test_record_coverage_range_rejects_invalid_status_and_bad_failed_error_shape(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        with pytest.raises(ValueError, match="coverage_status"):
            _record(conn, 10, 20, "unknown")
        with pytest.raises(ValueError, match="api_error_msg"):
            record_coverage_range(
                conn,
                token_id="token-a",
                fidelity_minutes=1,
                source_version="source-v1",
                range_start_ts=10,
                range_end_ts=20,
                coverage_status="failed",
                source="polymarket_clob_v2",
                api_error_msg=None,
                content_hash="hash",
                first_capture_run_id="run-1",
                captured_at=1,
            )


def test_market_price_coverage_check_rejects_invalid_status_when_bypassed(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                """
                INSERT INTO market_price_coverage_ranges (
                    token_id,
                    fidelity_minutes,
                    range_start_ts,
                    range_end_ts,
                    source_version,
                    coverage_status,
                    source,
                    api_error_msg,
                    content_hash,
                    first_capture_run_id,
                    captured_at
                )
                VALUES ('token-a', 1, 10, 20, 'source-v1', 'unknown',
                        'polymarket_clob_v2', NULL, 'hash', 'run-1', 1)
                """
            )


def test_merged_coverage_ranges_merges_overlapping_and_adjacent_fully_captured_rows(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        _record(conn, 10, 20)
        _record(conn, 21, 25)
        _record(conn, 24, 30)
        _record(conn, 40, 45)
        _record(conn, 46, 50, "partial")

        assert merged_coverage_ranges(
            conn,
            token_id="token-a",
            fidelity_minutes=1,
            source_version="source-v1",
        ) == [(10, 30), (40, 45)]


def test_compute_uncovered_subtracts_merged_coverage_from_requested_range(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        _record(conn, 10, 20)
        _record(conn, 25, 30)
        _record(conn, 31, 35)
        _record(conn, 50, 55, "failed")

        assert compute_uncovered(
            conn,
            token_id="token-a",
            fidelity_minutes=1,
            source_version="source-v1",
            requested_start=15,
            requested_end=40,
        ) == [(21, 24), (36, 40)]


def test_failed_batch_records_coverage_error_without_point_rows(tmp_path: Path) -> None:
    with _connect_market(tmp_path) as conn:
        _record(conn, 10, 20, "failed")

        assert _rows(conn, "SELECT COUNT(*) AS n FROM market_price_coverage_ranges") == [{"n": 1}]
        assert _rows(conn, "SELECT COUNT(*) AS n FROM market_price_points") == [{"n": 0}]


def test_compute_uncovered_interval_merge_fuzz(tmp_path: Path) -> None:
    rng = random.Random(1)
    with _connect_market(tmp_path) as conn:
        intervals: list[tuple[int, int]] = []
        for idx in range(40):
            start = rng.randint(0, 120)
            end = start + rng.randint(0, 10)
            intervals.append((start, end))
            _record(conn, start, end, token_id=f"token-{idx % 3}")

        token_intervals = [item for idx, item in enumerate(intervals) if idx % 3 == 0]
        covered = set()
        for start, end in token_intervals:
            covered.update(range(start, end + 1))
        expected_missing = []
        cursor = None
        for ts in range(0, 131):
            if ts not in covered and cursor is None:
                cursor = ts
            if (ts in covered or ts == 130) and cursor is not None:
                end = ts - 1 if ts in covered else ts
                expected_missing.append((cursor, end))
                cursor = None

        assert compute_uncovered(
            conn,
            token_id="token-0",
            fidelity_minutes=1,
            source_version="source-v1",
            requested_start=0,
            requested_end=130,
        ) == expected_missing
