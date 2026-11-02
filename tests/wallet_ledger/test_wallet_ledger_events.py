import json
from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.ledger.events import upsert_activity_events
from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


def _connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    ensure_wallet_schema(conn)
    return conn


def _rows(conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def test_upsert_activity_events_writes_events_and_raw_payloads(tmp_path: Path) -> None:
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "0xcid",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "timestamp": 1700000000,
            "price": 0.45,
            "size": 50,
            "usdcSize": 22.5,
            "transactionHash": "0xtx",
        },
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "0xcid",
            "asset": "asset_no",
            "outcomeIndex": 1,
            "outcome": "No",
            "timestamp": 1700000000,
            "price": 0.55,
            "size": 50,
            "usdcSize": 27.5,
            "transactionHash": "0xtx",
        },
    ]
    with _connect(tmp_path) as conn:
        written = upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )

        assert written == 2
        events = _rows(conn, "SELECT * FROM wallet_events ORDER BY event_row_ordinal")
        assert [row["event_row_ordinal"] for row in events] == [0, 1]
        assert [row["token_id"] for row in events] == ["asset_yes", "asset_no"]
        assert events[0]["event_uid"] != events[1]["event_uid"]
        assert events[0]["source_event_uid"] == "polymarket_data_api:0xtx:0"
        assert events[0]["wallet"] == NORMALIZED_WALLET
        assert events[0]["source_event_type"] == "TRADE"
        assert events[0]["side"] == "BUY"
        assert events[0]["collateral_amount"] == 22.5
        assert events[0]["collateral_token"] == "USDC.e"
        assert json.loads(events[0]["normalization_notes_json"])["collateral_source"] == "cutoff_inferred"
        assert events[0]["ingest_status"] == "normalized"
        assert events[0]["dedup_status"] == "canonical"

        payloads = _rows(conn, "SELECT * FROM wallet_event_raw_payloads ORDER BY event_uid")
        assert len(payloads) == 2
        assert payloads[0]["raw_sha256"]
        assert payloads[0]["raw_payload_bytes"] > 0
        assert json.loads(payloads[0]["raw_json"])["transactionHash"] == "0xtx"


def test_upsert_activity_events_is_idempotent_for_same_source_rows(tmp_path: Path) -> None:
    activity = [
        {
            "type": "TRADE",
            "side": "SELL",
            "conditionId": "0xcid",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "timestamp": 1700000000,
            "price": 0.50,
            "size": 10,
            "usdcSize": 5,
            "transactionHash": "0xtx",
        }
    ]
    with _connect(tmp_path) as conn:
        first = upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )
        second = upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )

        assert first == 1
        assert second == 0
        assert _rows(conn, "SELECT COUNT(*) AS n FROM wallet_events")[0]["n"] == 1
        assert _rows(conn, "SELECT COUNT(*) AS n FROM wallet_event_raw_payloads")[0]["n"] == 1


def test_same_canonical_event_from_different_source_is_retained_as_ambiguous(tmp_path: Path) -> None:
    activity = {
        "type": "TRADE",
        "side": "BUY",
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "timestamp": 1700000000,
        "price": 0.45,
        "size": 50,
        "usdcSize": 22.5,
        "transactionHash": "0xtx",
    }
    with _connect(tmp_path) as conn:
        upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=[activity],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )
        upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=[{**activity, "transactionHash": "0xgoldsky"}],
            source="goldsky_subgraph",
            source_version="2026-05-02",
            captured_at=1700000101,
        )

        rows = _rows(conn, "SELECT source, dedup_status FROM wallet_events ORDER BY source")
        assert rows == [
            {"source": "goldsky_subgraph", "dedup_status": "ambiguous"},
            {"source": "polymarket_data_api", "dedup_status": "ambiguous"},
        ]


def test_reingest_noop_then_new_ambiguous_sibling_flips_both_rows(tmp_path: Path) -> None:
    activity = {
        "type": "TRADE",
        "side": "BUY",
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "timestamp": 1700000000,
        "price": 0.45,
        "size": 50,
        "usdcSize": 22.5,
        "transactionHash": "0xtx",
    }
    with _connect(tmp_path) as conn:
        upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=[activity],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )
        assert (
            upsert_activity_events(
                conn,
                wallet=WALLET,
                capture_run_id="run-1",
                activity_rows=[activity],
                source="polymarket_data_api",
                source_version="2026-05-02",
                captured_at=1700000100,
            )
            == 0
        )
        upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=[{**activity, "transactionHash": "0xgoldsky"}],
            source="goldsky_subgraph",
            source_version="2026-05-02",
            captured_at=1700000101,
        )

        rows = _rows(conn, "SELECT dedup_status FROM wallet_events ORDER BY source")
        assert rows == [{"dedup_status": "ambiguous"}, {"dedup_status": "ambiguous"}]


def test_activity_event_missing_timestamp_is_rejected_not_normalized(tmp_path: Path) -> None:
    activity = {
        "type": "TRADE",
        "side": "BUY",
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "price": 0.45,
        "size": 50,
        "usdcSize": 22.5,
        "transactionHash": "0xtx",
    }
    with _connect(tmp_path) as conn:
        written = upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=[activity],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )

        assert written == 1
        rows = _rows(conn, "SELECT ts, ingest_status FROM wallet_events")
        assert rows == [{"ts": 0, "ingest_status": "rejected"}]
