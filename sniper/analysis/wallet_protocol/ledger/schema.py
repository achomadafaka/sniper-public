from __future__ import annotations

from collections.abc import Iterable

import duckdb


def _execute_many(conn: duckdb.DuckDBPyConnection, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def ensure_wallet_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _execute_many(
        conn,
        [
            """
            CREATE TABLE IF NOT EXISTS capture_runs (
                run_id VARCHAR NOT NULL,
                wallet VARCHAR NOT NULL,
                started_at BIGINT NOT NULL,
                finished_at BIGINT,
                status VARCHAR NOT NULL,
                config_hash VARCHAR NOT NULL,
                schema_version VARCHAR NOT NULL,
                source_versions_json JSON NOT NULL,
                surfaces_requested_json JSON NOT NULL,
                surfaces_complete_json JSON NOT NULL,
                error_class VARCHAR,
                error_message VARCHAR,
                CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
                PRIMARY KEY (wallet, run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS capture_shards (
                wallet VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                surface VARCHAR NOT NULL,
                shard_uid VARCHAR NOT NULL,
                cursor_kind VARCHAR NOT NULL,
                cursor_value_json JSON,
                range_start_ts BIGINT,
                range_end_ts BIGINT,
                budget_json JSON,
                status VARCHAR NOT NULL,
                rows_written BIGINT NOT NULL DEFAULT 0,
                next_cursor_json JSON,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                started_at BIGINT NOT NULL,
                finished_at BIGINT,
                error_class VARCHAR,
                error_message VARCHAR,
                content_hash VARCHAR,
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                PRIMARY KEY (wallet, capture_run_id, surface, shard_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_events (
                wallet VARCHAR NOT NULL,
                event_uid VARCHAR NOT NULL,
                source_event_uid VARCHAR NOT NULL,
                canonical_event_hash VARCHAR NOT NULL,
                event_row_ordinal INTEGER NOT NULL,
                source_event_type VARCHAR NOT NULL,
                ts BIGINT NOT NULL,
                block_number BIGINT,
                condition_id VARCHAR,
                token_id VARCHAR,
                outcome_index INTEGER,
                outcome_label VARCHAR,
                market_structure VARCHAR,
                side VARCHAR,
                size_tokens DOUBLE,
                collateral_amount DOUBLE,
                fee_collateral DOUBLE,
                collateral_token VARCHAR,
                collateral_token_address VARCHAR,
                collateral_display_unit VARCHAR,
                usd_equivalent_amount DOUBLE,
                price DOUBLE,
                transaction_hash VARCHAR,
                log_index INTEGER,
                order_hash VARCHAR,
                maker VARCHAR,
                taker VARCHAR,
                ingest_status VARCHAR NOT NULL CHECK (ingest_status IN ('captured', 'normalized', 'rejected')),
                dedup_status VARCHAR NOT NULL DEFAULT 'canonical' CHECK (dedup_status IN ('canonical', 'duplicate', 'ambiguous')),
                normalization_notes_json JSON,
                normalization_version VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (wallet, event_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_event_raw_payloads (
                wallet VARCHAR NOT NULL,
                event_uid VARCHAR NOT NULL,
                raw_json JSON NOT NULL,
                raw_sha256 VARCHAR NOT NULL,
                raw_payload_bytes BIGINT NOT NULL,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (wallet, event_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_positions (
                wallet VARCHAR NOT NULL,
                position_uid VARCHAR NOT NULL,
                condition_id VARCHAR NOT NULL,
                token_id VARCHAR NOT NULL,
                outcome_index INTEGER,
                outcome_label VARCHAR,
                market_structure VARCHAR,
                status VARCHAR NOT NULL,
                is_latest BOOLEAN NOT NULL,
                opened_at BIGINT,
                opened_at_source VARCHAR,
                closed_at BIGINT,
                closed_at_source VARCHAR,
                avg_price DOUBLE,
                size_tokens DOUBLE,
                cost_basis_collateral DOUBLE,
                total_bought_collateral DOUBLE,
                total_sold_collateral DOUBLE,
                realized_pnl_collateral DOUBLE,
                unrealized_pnl_collateral DOUBLE,
                current_price DOUBLE,
                collateral_token VARCHAR,
                collateral_token_address VARCHAR,
                api_total_bought_raw DOUBLE,
                api_initial_value_raw DOUBLE,
                normalization_notes_json JSON,
                raw_position_json JSON,
                is_finalized BOOLEAN NOT NULL DEFAULT false,
                finalized_at BIGINT,
                finalization_reason VARCHAR,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (wallet, position_uid, capture_run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_price_lookup_requests (
                wallet VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                token_id VARCHAR NOT NULL,
                target_ts BIGINT NOT NULL,
                fidelity_minutes INTEGER NOT NULL,
                request_reason VARCHAR NOT NULL,
                lookup_status VARCHAR NOT NULL,
                matched_price_ts BIGINT,
                matched_price DOUBLE,
                match_lag_seconds BIGINT,
                match_source_version VARCHAR,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (lookup_status IN ('matched', 'no_match_in_window', 'pending', 'api_error')),
                PRIMARY KEY (
                    wallet,
                    capture_run_id,
                    token_id,
                    target_ts,
                    fidelity_minutes,
                    request_reason
                )
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_execution_probes (
                wallet VARCHAR NOT NULL,
                fill_event_uid VARCHAR NOT NULL,
                probe_config_hash VARCHAR NOT NULL,
                probe_uid VARCHAR NOT NULL,
                probe_version VARCHAR NOT NULL,
                horizons_seconds_json JSON NOT NULL,
                reference_rule VARCHAR NOT NULL,
                condition_id VARCHAR,
                token_id VARCHAR,
                fill_ts BIGINT NOT NULL,
                fill_price DOUBLE,
                side VARCHAR,
                size_tokens DOUBLE,
                reference_price DOUBLE,
                reference_ts BIGINT,
                probe_status VARCHAR NOT NULL,
                probe_error_msg VARCHAR,
                ref_tape DOUBLE,
                tape_n INTEGER,
                in_play BOOLEAN,
                thin_market BOOLEAN,
                resolution_ts_unknown BOOLEAN,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (probe_status IN ('success', 'no_history', 'truncated', 'error')),
                PRIMARY KEY (wallet, fill_event_uid, probe_config_hash)
            )
            """,
            # Slice 5 Task 0: migrate older warehouses created before the
            # auxiliary gating fields were part of the table. Idempotent on
            # fresh schemas where the columns already exist via the CREATE.
            "ALTER TABLE wallet_execution_probes ADD COLUMN IF NOT EXISTS ref_tape DOUBLE",
            "ALTER TABLE wallet_execution_probes ADD COLUMN IF NOT EXISTS tape_n INTEGER",
            "ALTER TABLE wallet_execution_probes ADD COLUMN IF NOT EXISTS in_play BOOLEAN",
            "ALTER TABLE wallet_execution_probes ADD COLUMN IF NOT EXISTS thin_market BOOLEAN",
            "ALTER TABLE wallet_execution_probes ADD COLUMN IF NOT EXISTS resolution_ts_unknown BOOLEAN",
            """
            CREATE TABLE IF NOT EXISTS wallet_execution_probe_horizons (
                wallet VARCHAR NOT NULL,
                fill_event_uid VARCHAR NOT NULL,
                probe_config_hash VARCHAR NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                reference_price DOUBLE,
                reference_ts BIGINT,
                target_ts BIGINT NOT NULL,
                target_price DOUBLE,
                drift DOUBLE,
                return_pct DOUBLE,
                horizon_status VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (horizon_status IN ('success', 'no_history', 'invalid_reference', 'error')),
                PRIMARY KEY (wallet, fill_event_uid, probe_config_hash, horizon_seconds)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wallet_reconstructed_flips (
                wallet VARCHAR NOT NULL,
                flip_uid VARCHAR NOT NULL,
                condition_id VARCHAR,
                token_id VARCHAR,
                outcome_index INTEGER,
                outcome_label VARCHAR,
                buy_event_uid VARCHAR NOT NULL,
                sell_event_uid VARCHAR NOT NULL,
                buy_ts BIGINT NOT NULL,
                sell_ts BIGINT NOT NULL,
                buy_price DOUBLE,
                sell_price DOUBLE,
                size_tokens DOUBLE,
                buy_notional_collateral DOUBLE,
                sell_notional_collateral DOUBLE,
                buy_collateral_token VARCHAR,
                sell_collateral_token VARCHAR,
                flip_roi DOUBLE,
                hold_duration_seconds BIGINT,
                flip_pnl_collateral DOUBLE,
                flip_pnl_usd_equivalent DOUBLE,
                collateral_conversion_assumption VARCHAR,
                derivation_check_status VARCHAR NOT NULL,
                reconstruction_method VARCHAR NOT NULL,
                reconstruction_version VARCHAR NOT NULL,
                input_event_uids_json JSON NOT NULL,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (derivation_check_status IN ('ok', 'drift', 'unverified')),
                PRIMARY KEY (wallet, flip_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analysis_windows (
                wallet VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                window_id VARCHAR NOT NULL,
                window_type VARCHAR NOT NULL,
                from_ts BIGINT,
                to_ts BIGINT,
                materialization_status VARCHAR NOT NULL,
                manifest_hash VARCHAR,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (wallet, capture_run_id, window_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analysis_price_manifest (
                wallet VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                window_id VARCHAR NOT NULL,
                token_id VARCHAR NOT NULL,
                fidelity_minutes INTEGER NOT NULL,
                range_start_ts BIGINT NOT NULL,
                range_end_ts BIGINT NOT NULL,
                source_version VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (
                    wallet,
                    capture_run_id,
                    window_id,
                    token_id,
                    range_start_ts,
                    range_end_ts
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_ts
            ON wallet_events(wallet, ts DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_condition_token_ts
            ON wallet_events(wallet, condition_id, token_id, ts)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_condition
            ON wallet_events(wallet, condition_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_token
            ON wallet_events(wallet, token_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_transaction_hash
            ON wallet_events(wallet, transaction_hash)
            """,
        ],
    )


def ensure_market_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _execute_many(
        conn,
        [
            """
            CREATE TABLE IF NOT EXISTS market_outcome_dim_snapshot (
                capture_run_id VARCHAR NOT NULL,
                condition_id VARCHAR NOT NULL,
                token_id VARCHAR NOT NULL,
                outcome_index INTEGER,
                outcome_label VARCHAR,
                outcome_count INTEGER,
                is_binary BOOLEAN NOT NULL,
                market_structure VARCHAR NOT NULL,
                negative_risk BOOLEAN,
                dim_quality_flags_json JSON,
                market_slug VARCHAR,
                event_slug VARCHAR,
                title VARCHAR,
                end_date BIGINT,
                resolved BOOLEAN NOT NULL,
                resolved_at BIGINT,
                resolution_price DOUBLE,
                resolution_source VARCHAR,
                raw_market_json JSON,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                snapshot_at BIGINT NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (capture_run_id, token_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_metadata_snapshot (
                capture_run_id VARCHAR NOT NULL,
                condition_id VARCHAR NOT NULL,
                market_slug VARCHAR,
                event_slug VARCHAR,
                title VARCHAR,
                end_date BIGINT,
                raw_market_json JSON,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                PRIMARY KEY (capture_run_id, condition_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_price_points (
                token_id VARCHAR NOT NULL,
                fidelity_minutes INTEGER NOT NULL,
                ts BIGINT NOT NULL,
                source_version VARCHAR NOT NULL,
                price DOUBLE,
                point_status VARCHAR NOT NULL,
                point_source VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                first_capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (point_status IN ('captured', 'requested_no_data')),
                PRIMARY KEY (token_id, fidelity_minutes, ts, source_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_price_coverage_ranges (
                token_id VARCHAR NOT NULL,
                fidelity_minutes INTEGER NOT NULL,
                range_start_ts BIGINT NOT NULL,
                range_end_ts BIGINT NOT NULL,
                source_version VARCHAR NOT NULL,
                coverage_status VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                api_error_msg VARCHAR,
                content_hash VARCHAR NOT NULL,
                first_capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (coverage_status IN ('fully_captured', 'partial', 'failed')),
                PRIMARY KEY (
                    token_id,
                    fidelity_minutes,
                    range_start_ts,
                    range_end_ts,
                    source_version
                )
            )
            """,
        ],
    )


def ensure_index_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _execute_many(
        conn,
        [
            """
            CREATE TABLE IF NOT EXISTS wallets_index (
                wallet VARCHAR NOT NULL,
                wallet_path VARCHAR NOT NULL,
                latest_run_id VARCHAR,
                status VARCHAR NOT NULL,
                schema_version VARCHAR NOT NULL,
                last_successful_capture_at BIGINT,
                latest_finding_summary_json JSON,
                capture_completeness_json JSON,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (wallet)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS shared_warehouse_lock (
                lock_name VARCHAR NOT NULL,
                writer_pid BIGINT NOT NULL,
                acquired_at BIGINT NOT NULL,
                expires_at BIGINT,
                PRIMARY KEY (lock_name)
            )
            """,
        ],
    )
