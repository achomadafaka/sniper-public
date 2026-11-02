from __future__ import annotations

from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.ledger.coverage import record_coverage_range
from sniper.analysis.wallet_protocol.ledger.lookups import record_lookup_request
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


def _coverage(market_conn: duckdb.DuckDBPyConnection, status: str = "fully_captured") -> None:
    start = 90 if status != "failed" else 80
    end = 120 if status != "failed" else 110
    record_coverage_range(
        market_conn,
        token_id="token-a",
        fidelity_minutes=1,
        source_version="source-v1",
        range_start_ts=start,
        range_end_ts=end,
        coverage_status=status,
        source="polymarket_clob_v2",
        api_error_msg="boom" if status == "failed" else None,
        content_hash=f"hash-{status}",
        first_capture_run_id="run-1",
        captured_at=1 if status != "failed" else 2,
    )


def _lookup(wallet_conn: duckdb.DuckDBPyConnection) -> int:
    return record_lookup_request(
        wallet_conn,
        wallet=WALLET,
        capture_run_id="run-1",
        token_id="token-a",
        target_ts=100,
        fidelity_minutes=1,
        request_reason="fill_reference",
        match_window_seconds=10,
        source="wallet_ledger",
        source_version="source-v1",
        captured_at=3,
    )


def test_record_lookup_request_matches_nearest_price_by_source_version(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _coverage(market_conn)
        upsert_price_points(
            market_conn,
            [
                MarketPricePoint("token-a", 1, 94, 0.40),
                MarketPricePoint("token-a", 1, 103, 0.43),
                MarketPricePoint("token-a", 1, 101, 0.99),
            ],
            source_version="source-v2",
            capture_run_id="run-1",
            captured_at=2,
        )
        upsert_price_points(
            market_conn,
            [MarketPricePoint("token-a", 1, 103, 0.43), MarketPricePoint("token-a", 1, 94, 0.40)],
            source_version="source-v1",
            capture_run_id="run-1",
            captured_at=2,
        )
        market_conn.close()

        assert _lookup(wallet_conn) == 1

        rows = _rows(
            wallet_conn,
            """
            SELECT wallet, lookup_status, matched_price_ts, matched_price,
                   match_lag_seconds, match_source_version
            FROM wallet_price_lookup_requests
            """,
        )
        assert rows == [
            {
                "wallet": NORMALIZED_WALLET,
                "lookup_status": "matched",
                "matched_price_ts": 103,
                "matched_price": 0.43,
                "match_lag_seconds": 3,
                "match_source_version": "source-v1",
            }
        ]


def test_record_lookup_request_is_idempotent_for_same_request_key(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _coverage(market_conn)
        market_conn.close()

        assert _lookup(wallet_conn) == 1
        assert _lookup(wallet_conn) == 0
        assert _rows(wallet_conn, "SELECT COUNT(*) AS n FROM wallet_price_lookup_requests") == [{"n": 1}]


def test_record_lookup_request_marks_no_match_when_covered_but_window_empty(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _coverage(market_conn)
        upsert_price_points(
            market_conn,
            [MarketPricePoint("token-a", 1, 200, 0.40)],
            source_version="source-v1",
            capture_run_id="run-1",
            captured_at=2,
        )
        market_conn.close()

        _lookup(wallet_conn)

        assert _rows(wallet_conn, "SELECT lookup_status FROM wallet_price_lookup_requests") == [
            {"lookup_status": "no_match_in_window"}
        ]


def test_record_lookup_request_marks_pending_without_overlapping_coverage(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        market_conn.close()
        _lookup(wallet_conn)

        assert _rows(wallet_conn, "SELECT lookup_status FROM wallet_price_lookup_requests") == [
            {"lookup_status": "pending"}
        ]


def test_record_lookup_request_marks_api_error_for_latest_failed_coverage(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    with wallet_conn, market_conn:
        _coverage(market_conn)
        _coverage(market_conn, "failed")
        market_conn.close()

        _lookup(wallet_conn)

        assert _rows(wallet_conn, "SELECT lookup_status FROM wallet_price_lookup_requests") == [
            {"lookup_status": "api_error"}
        ]
