import json
from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.runs import (
    CaptureShard,
    complete_capture_run,
    complete_capture_shard,
    create_capture_run,
    next_pending_shard,
    upsert_capture_shard,
)
from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


def _connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    ensure_wallet_schema(conn)
    return conn


def _row(conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> dict:
    columns = [col[0] for col in conn.execute(query, params or []).description]
    values = conn.fetchone()
    assert values is not None
    return dict(zip(columns, values))


def test_create_capture_run_normalizes_wallet_and_serializes_config(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        create_capture_run(
            conn,
            wallet=WALLET,
            run_id="run-1",
            started_at=1700000000,
            config={"surfaces": ["positions", "activity"], "budget": {"rows": 500}},
            schema_version="ledger-v1",
            source_versions={"data_api": "2026-05-02"},
            surfaces_requested=["activity", "positions"],
        )

        row = _row(conn, "SELECT * FROM capture_runs WHERE wallet=? AND run_id=?", [NORMALIZED_WALLET, "run-1"])
        assert row["status"] == "running"
        assert row["finished_at"] is None
        assert json.loads(row["source_versions_json"]) == {"data_api": "2026-05-02"}
        assert json.loads(row["surfaces_requested_json"]) == ["activity", "positions"]
        assert json.loads(row["surfaces_complete_json"]) == {}
        assert len(row["config_hash"]) == 64


def test_complete_capture_run_records_surface_completion_and_error_fields(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        create_capture_run(
            conn,
            wallet=WALLET,
            run_id="run-1",
            started_at=1700000000,
            config={},
            schema_version="ledger-v1",
            source_versions={},
            surfaces_requested=["activity"],
        )

        complete_capture_run(
            conn,
            wallet=WALLET,
            run_id="run-1",
            finished_at=1700000100,
            status="failed",
            surfaces_complete={"activity": False},
            error_class="api_error",
            error_message="upstream failed",
        )

        row = _row(conn, "SELECT * FROM capture_runs WHERE wallet=? AND run_id=?", [NORMALIZED_WALLET, "run-1"])
        assert row["status"] == "failed"
        assert row["finished_at"] == 1700000100
        assert json.loads(row["surfaces_complete_json"]) == {"activity": False}
        assert row["error_class"] == "api_error"
        assert row["error_message"] == "upstream failed"


def test_upsert_capture_shard_is_idempotent_and_next_pending_skips_completed(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        pending = CaptureShard(
            wallet=WALLET,
            capture_run_id="run-1",
            surface="activity",
            shard_uid="activity-001",
            cursor_kind="offset",
            cursor_value={"offset": 0},
            range_start_ts=1700000000,
            range_end_ts=1700003600,
            budget={"rows": 500},
            status="pending",
            rows_written=0,
            next_cursor=None,
            source="polymarket_data_api",
            source_version="2026-05-02",
            started_at=1700000001,
            finished_at=None,
            error_class=None,
            error_message=None,
            content_hash=None,
        )
        upsert_capture_shard(conn, pending)
        upsert_capture_shard(conn, pending)

        got = next_pending_shard(conn, wallet=WALLET, run_id="run-1", surface="activity")
        assert got is not None
        assert got.shard_uid == "activity-001"
        assert got.cursor_value == {"offset": 0}
        assert got.budget == {"rows": 500}

        complete_capture_shard(
            conn,
            wallet=WALLET,
            run_id="run-1",
            surface="activity",
            shard_uid="activity-001",
            finished_at=1700000020,
            rows_written=100,
            next_cursor={"offset": 100},
            content_hash="hash-001",
        )

        assert next_pending_shard(conn, wallet=WALLET, run_id="run-1", surface="activity") is None
        row = _row(
            conn,
            "SELECT status, rows_written, next_cursor_json, content_hash FROM capture_shards WHERE wallet=?",
            [NORMALIZED_WALLET],
        )
        assert row["status"] == "completed"
        assert row["rows_written"] == 100
        assert json.loads(row["next_cursor_json"]) == {"offset": 100}
        assert row["content_hash"] == "hash-001"


def test_upsert_capture_shard_preserves_original_started_at_on_retry(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        first = CaptureShard(
            wallet=WALLET,
            capture_run_id="run-1",
            surface="activity",
            shard_uid="activity-001",
            cursor_kind="offset",
            cursor_value={"offset": 0},
            range_start_ts=None,
            range_end_ts=None,
            budget={"rows": 500},
            status="pending",
            rows_written=0,
            next_cursor=None,
            source="polymarket_data_api",
            source_version="2026-05-02",
            started_at=1700000001,
            finished_at=None,
            error_class=None,
            error_message=None,
            content_hash=None,
        )
        retry = CaptureShard(
            wallet=WALLET,
            capture_run_id="run-1",
            surface="activity",
            shard_uid="activity-001",
            cursor_kind="offset",
            cursor_value={"offset": 0},
            range_start_ts=None,
            range_end_ts=None,
            budget={"rows": 500},
            status="running",
            rows_written=0,
            next_cursor=None,
            source="polymarket_data_api",
            source_version="2026-05-02",
            started_at=1700000500,
            finished_at=None,
            error_class=None,
            error_message=None,
            content_hash=None,
        )

        upsert_capture_shard(conn, first)
        upsert_capture_shard(conn, retry)

        row = _row(conn, "SELECT status, started_at FROM capture_shards WHERE wallet=?", [NORMALIZED_WALLET])
        assert row == {"status": "running", "started_at": 1700000001}


def test_invalid_run_and_shard_statuses_are_rejected(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                """
                INSERT INTO capture_runs (
                    run_id, wallet, started_at, finished_at, status, config_hash,
                    schema_version, source_versions_json, surfaces_requested_json,
                    surfaces_complete_json, error_class, error_message
                )
                VALUES ('run-1', ?, 1700000000, NULL, 'bogus', 'hash', 'ledger-v1',
                        '{}', '[]', '{}', NULL, NULL)
                """,
                [NORMALIZED_WALLET],
            )

        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                """
                INSERT INTO capture_shards (
                    wallet, capture_run_id, surface, shard_uid, cursor_kind, cursor_value_json,
                    range_start_ts, range_end_ts, budget_json, status, rows_written,
                    next_cursor_json, source, source_version, started_at, finished_at,
                    error_class, error_message, content_hash
                )
                VALUES (?, 'run-1', 'activity', 'activity-001', 'offset', '{}',
                        NULL, NULL, '{}', 'bogus', 0, NULL, 'polymarket_data_api',
                        '2026-05-02', 1700000000, NULL, NULL, NULL, NULL)
                """,
                [NORMALIZED_WALLET],
            )
