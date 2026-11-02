from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.paths import ledger_paths
from sniper.analysis.wallet_protocol.ledger.schema import (
    ensure_index_schema,
    ensure_market_schema,
    ensure_wallet_schema,
)


WALLET_TABLES = {
    "capture_runs",
    "capture_shards",
    "wallet_events",
    "wallet_event_raw_payloads",
    "wallet_positions",
    "wallet_price_lookup_requests",
    "wallet_execution_probes",
    "wallet_execution_probe_horizons",
    "wallet_reconstructed_flips",
    "analysis_windows",
    "analysis_price_manifest",
}

MARKET_TABLES = {
    "market_outcome_dim_snapshot",
    "market_metadata_snapshot",
    "market_price_points",
    "market_price_coverage_ranges",
}

INDEX_TABLES = {
    "wallets_index",
    "shared_warehouse_lock",
}


def _tables(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main'
        """
    ).fetchall()
    return {row[0] for row in rows}


def _primary_key_columns(conn: duckdb.DuckDBPyConnection, table_name: str) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_name = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_name = ?
        ORDER BY kcu.ordinal_position
        """,
        [table_name],
    ).fetchall()
    return tuple(row[0] for row in rows)


def _columns(conn: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'main'
          AND table_name = ?
        """,
        [table_name],
    ).fetchall()
    return {row[0] for row in rows}


def _index_names(conn: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT index_name
        FROM duckdb_indexes()
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchall()
    return {row[0] for row in rows}


def test_ledger_paths_normalize_wallet_and_keep_shared_files_under_root(tmp_path: Path) -> None:
    paths = ledger_paths(tmp_path, "0xABCDEF1234567890abcdef1234567890ABCDEF12")

    assert paths.root == tmp_path
    assert paths.wallet_path == tmp_path / "0xabcdef1234567890abcdef1234567890abcdef12.duckdb"
    assert paths.market_data_path == tmp_path / "market_data.duckdb"
    assert paths.index_path == tmp_path / "index.duckdb"


@pytest.mark.parametrize(
    "wallet",
    [
        "",
        "abcdef1234567890abcdef1234567890abcdef12",
        "0xabc",
        "0xabcdef1234567890abcdef1234567890abcdef1z",
        "../0xabcdef1234567890abcdef1234567890abcdef12",
    ],
)
def test_ledger_paths_reject_unsafe_wallet_strings(tmp_path: Path, wallet: str) -> None:
    with pytest.raises(ValueError):
        ledger_paths(tmp_path, wallet)


def test_ensure_wallet_schema_creates_expected_tables_and_primary_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "wallet.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_wallet_schema(conn)
        ensure_wallet_schema(conn)

        assert _tables(conn) == WALLET_TABLES
        assert _primary_key_columns(conn, "wallet_events") == ("wallet", "event_uid")
        assert _primary_key_columns(conn, "wallet_positions") == (
            "wallet",
            "position_uid",
            "capture_run_id",
        )
        assert _primary_key_columns(conn, "wallet_price_lookup_requests") == (
            "wallet",
            "capture_run_id",
            "token_id",
            "target_ts",
            "fidelity_minutes",
            "request_reason",
        )


def test_wallet_schema_matches_spec_required_columns_and_names(tmp_path: Path) -> None:
    db_path = tmp_path / "wallet.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_wallet_schema(conn)

        assert {
            "run_id",
            "wallet",
            "started_at",
            "finished_at",
            "status",
            "config_hash",
            "schema_version",
            "source_versions_json",
            "surfaces_requested_json",
            "surfaces_complete_json",
            "error_class",
            "error_message",
        }.issubset(_columns(conn, "capture_runs"))
        assert {"capture_run_id", "run_status", "completed_at", "source_version", "error_msg"}.isdisjoint(
            _columns(conn, "capture_runs")
        )

        assert {
            "wallet",
            "capture_run_id",
            "surface",
            "shard_uid",
            "cursor_kind",
            "cursor_value_json",
            "range_start_ts",
            "range_end_ts",
            "budget_json",
            "status",
            "rows_written",
            "next_cursor_json",
            "source",
            "source_version",
            "started_at",
            "finished_at",
            "error_class",
            "error_message",
            "content_hash",
        }.issubset(_columns(conn, "capture_shards"))
        assert {"cursor_json", "shard_status", "captured_at", "error_msg"}.isdisjoint(
            _columns(conn, "capture_shards")
        )

        assert {
            "source_event_uid",
            "canonical_event_hash",
            "event_row_ordinal",
            "ingest_status",
            "normalization_notes_json",
            "normalization_version",
        }.issubset(_columns(conn, "wallet_events"))


def test_wallet_events_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "wallet.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_wallet_schema(conn)

        assert {
            "idx_wallet_events_wallet_ts",
            "idx_wallet_events_wallet_condition_token_ts",
            "idx_wallet_events_wallet_condition",
            "idx_wallet_events_wallet_token",
            "idx_wallet_events_wallet_transaction_hash",
        }.issubset(_index_names(conn, "wallet_events"))


def test_market_status_check_constraints_reject_invalid_values(tmp_path: Path) -> None:
    db_path = tmp_path / "market_data.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_market_schema(conn)

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
                VALUES ('token', 1, 1700000000, 'clob-test', 0.42, 'api_error',
                        'clob_prices_history_batch', 'hash', 'polymarket_clob_v2', 'run-1', 1700000001)
                """
            )

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
                VALUES ('token', 1, 1700000000, 1700003600, 'clob-test', 'unknown',
                        'polymarket_clob_v2', NULL, 'hash', 'run-1', 1700000001)
                """
            )


def test_ensure_market_schema_creates_expected_tables_and_primary_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "market_data.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_market_schema(conn)
        ensure_market_schema(conn)

        assert _tables(conn) == MARKET_TABLES
        assert _primary_key_columns(conn, "market_price_points") == (
            "token_id",
            "fidelity_minutes",
            "ts",
            "source_version",
        )
        assert "snapshot_at" in _columns(conn, "market_outcome_dim_snapshot")


def test_ensure_index_schema_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "index.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        ensure_index_schema(conn)
        ensure_index_schema(conn)

        assert _tables(conn) == INDEX_TABLES
