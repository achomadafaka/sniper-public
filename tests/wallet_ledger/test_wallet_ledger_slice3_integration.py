from __future__ import annotations

from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.ledger.manifest import (
    analysis_price_manifest_hash,
    record_analysis_price_manifest,
)
from sniper.analysis.wallet_protocol.ledger.price_api import BatchPriceRequest
from sniper.analysis.wallet_protocol.ledger.price_capture import capture_missing_price_history
from sniper.analysis.wallet_protocol.ledger.schema import ensure_index_schema, ensure_market_schema, ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, *, json: dict) -> _Response:
        self.calls.append((path, json))
        prices = []
        for request in json["requests"]:
            history = [
                {"t": ts, "p": round(0.10 + idx * 0.01, 2)}
                for idx, ts in enumerate(
                    range(request["range_start_ts"], request["range_end_ts"] + 1, request["fidelity_minutes"] * 60)
                )
            ]
            prices.append({"token_id": request["token_id"], "history": history})
        return _Response({"prices": prices})


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


def _requests(start: int, end: int) -> list[BatchPriceRequest]:
    return [
        BatchPriceRequest(f"token-{idx}", fidelity_minutes=1, range_start_ts=start, range_end_ts=end)
        for idx in range(3)
    ]


def test_wallet_ledger_slice3_full_price_path_with_uncovered_tail_and_manifest_replay(tmp_path: Path) -> None:
    wallet_conn, market_conn = _connect_pair(tmp_path)
    client = _Client()
    with wallet_conn, market_conn:
        first_uncovered = capture_missing_price_history(
            market_conn,
            client,
            _requests(100, 220),
            source_version="source-v1",
            capture_run_id="run-1",
            captured_at=1,
        )

        assert [(request.token_id, request.range_start_ts, request.range_end_ts) for request in first_uncovered] == [
            ("token-0", 100, 220),
            ("token-1", 100, 220),
            ("token-2", 100, 220),
        ]
        assert len(client.calls) == 1
        assert len(client.calls[0][1]["requests"]) == 3
        assert _rows(market_conn, "SELECT COUNT(*) AS n FROM market_price_points") == [{"n": 9}]
        assert _rows(
            market_conn,
            """
            SELECT token_id, coverage_status, range_start_ts, range_end_ts
            FROM market_price_coverage_ranges
            ORDER BY token_id
            """,
        ) == [
            {"token_id": "token-0", "coverage_status": "fully_captured", "range_start_ts": 100, "range_end_ts": 220},
            {"token_id": "token-1", "coverage_status": "fully_captured", "range_start_ts": 100, "range_end_ts": 220},
            {"token_id": "token-2", "coverage_status": "fully_captured", "range_start_ts": 100, "range_end_ts": 220},
        ]

        second_uncovered = capture_missing_price_history(
            market_conn,
            client,
            _requests(160, 280),
            source_version="source-v1",
            capture_run_id="run-2",
            captured_at=2,
        )

        assert [(request.token_id, request.range_start_ts, request.range_end_ts) for request in second_uncovered] == [
            ("token-0", 221, 280),
            ("token-1", 221, 280),
            ("token-2", 221, 280),
        ]
        assert len(client.calls) == 2
        assert [request["range_start_ts"] for request in client.calls[1][1]["requests"]] == [221, 221, 221]

        record_analysis_price_manifest(
            wallet_conn,
            market_conn,
            wallet=WALLET,
            capture_run_id="run-1",
            window_id="window-1",
            token_id="token-0",
            fidelity_minutes=1,
            range_start_ts=100,
            range_end_ts=220,
            source_version="source-v1",
            source="wallet_ledger",
            captured_at=3,
        )
        manifest_hash = _rows(wallet_conn, "SELECT content_hash FROM analysis_price_manifest")[0]["content_hash"]
        replay_rows = _rows(
            market_conn,
            """
            SELECT token_id, fidelity_minutes, ts, source_version
            FROM market_price_points
            WHERE token_id = 'token-0'
              AND fidelity_minutes = 1
              AND source_version = 'source-v1'
              AND ts BETWEEN 100 AND 220
            ORDER BY ts
            """,
        )
        assert manifest_hash == analysis_price_manifest_hash(replay_rows)
        assert manifest_hash == analysis_price_manifest_hash(replay_rows)
