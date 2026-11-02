from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.manifest import (
    analysis_price_manifest_hash,
    record_analysis_price_manifest,
)
from sniper.analysis.wallet_protocol.ledger.prices import MarketPricePoint, upsert_price_points
from sniper.analysis.wallet_protocol.ledger.schema import ensure_index_schema, ensure_market_schema, ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


def _connect_pair(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]:
    wallet_conn = duckdb.connect(str(tmp_path / f"{NORMALIZED_WALLET}.duckdb"))
    market_conn = duckdb.connect(str(tmp_path / "market_data.duckdb"))
    ensure_wallet_schema(wallet_conn)
    ensure_market_schema(market_conn)
    with duckdb.connect(str(tmp_path / "index.duckdb")) as index_conn:
        ensure_index_schema(index_conn)
    return wallet_conn, market_conn


def _rows(conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _seed_points(market_conn: duckdb.DuckDBPyConnection, token_id: str = "token-a") -> None:
    upsert_price_points(
        market_conn,
        [
            MarketPricePoint(token_id, 1, 100, 0.40),
            MarketPricePoint(token_id, 1, 160, 0.41),
            MarketPricePoint(token_id, 1, 220, 0.42),
        ],
        source_version="source-v1",
        capture_run_id="run-1",
        captured_at=1,
    )


def test_record_analysis_price_manifest_writes_one_row_per_window_token_range(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _seed_points(market_conn)

        written = record_analysis_price_manifest(
            wallet_conn,
            market_conn,
            wallet=WALLET,
            capture_run_id="run-1",
            window_id="window-1",
            token_id="token-a",
            fidelity_minutes=1,
            range_start_ts=100,
            range_end_ts=220,
            source_version="source-v1",
            source="wallet_ledger",
            captured_at=2,
        )

        assert written == 1
        rows = _rows(
            wallet_conn,
            """
            SELECT wallet, capture_run_id, window_id, token_id, fidelity_minutes,
                   range_start_ts, range_end_ts, source_version, source, captured_at
            FROM analysis_price_manifest
            """,
        )
        assert rows == [
            {
                "wallet": NORMALIZED_WALLET,
                "capture_run_id": "run-1",
                "window_id": "window-1",
                "token_id": "token-a",
                "fidelity_minutes": 1,
                "range_start_ts": 100,
                "range_end_ts": 220,
                "source_version": "source-v1",
                "source": "wallet_ledger",
                "captured_at": 2,
            }
        ]


def test_record_analysis_price_manifest_hash_replays_from_market_points(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _seed_points(market_conn)
        record_analysis_price_manifest(
            wallet_conn,
            market_conn,
            wallet=WALLET,
            capture_run_id="run-1",
            window_id="window-1",
            token_id="token-a",
            fidelity_minutes=1,
            range_start_ts=100,
            range_end_ts=220,
            source_version="source-v1",
            source="wallet_ledger",
            captured_at=2,
        )

        manifest_hash = _rows(wallet_conn, "SELECT content_hash FROM analysis_price_manifest")[0]["content_hash"]
        replay_rows = _rows(
            market_conn,
            """
            SELECT token_id, fidelity_minutes, ts, source_version
            FROM market_price_points
            WHERE token_id = 'token-a'
              AND fidelity_minutes = 1
              AND source_version = 'source-v1'
              AND ts BETWEEN 100 AND 220
            ORDER BY ts
            """,
        )
        assert manifest_hash == analysis_price_manifest_hash(replay_rows)


def test_record_analysis_price_manifest_refuses_partial_underlying_points(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        upsert_price_points(
            market_conn,
            [MarketPricePoint("token-a", 1, 100, 0.40), MarketPricePoint("token-a", 1, 220, 0.42)],
            source_version="source-v1",
            capture_run_id="run-1",
            captured_at=1,
        )

        with pytest.raises(ValueError, match="missing market_price_points"):
            record_analysis_price_manifest(
                wallet_conn,
                market_conn,
                wallet=WALLET,
                capture_run_id="run-1",
                window_id="window-1",
                token_id="token-a",
                fidelity_minutes=1,
                range_start_ts=100,
                range_end_ts=220,
                source_version="source-v1",
                source="wallet_ledger",
                captured_at=2,
            )


def test_record_analysis_price_manifest_is_idempotent(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _seed_points(market_conn)
        kwargs = dict(
            wallet=WALLET,
            capture_run_id="run-1",
            window_id="window-1",
            token_id="token-a",
            fidelity_minutes=1,
            range_start_ts=100,
            range_end_ts=220,
            source_version="source-v1",
            source="wallet_ledger",
            captured_at=2,
        )

        assert record_analysis_price_manifest(wallet_conn, market_conn, **kwargs) == 1
        assert record_analysis_price_manifest(wallet_conn, market_conn, **kwargs) == 0
