"""Slice 4 Task 4 — end-to-end integration of probes + flips.

Walks the full pipeline once: capture activity → write wallet_events →
join event_uids back into activity → iter_fill_probes → write
wallet_execution_probes / wallet_execution_probe_horizons → reconstruct
and write wallet_reconstructed_flips.

Asserts cross-table foreign-key invariants the writers do not enforce on
their own (DuckDB has no FK constraints in the schema).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import duckdb

from scripts.execution_profile import iter_fill_probes
from sniper.analysis.wallet_protocol.ledger.events import upsert_activity_events
from sniper.analysis.wallet_protocol.ledger.flips import (
    reconstruct_and_upsert_flips,
)
from sniper.analysis.wallet_protocol.ledger.probes import upsert_execution_probes
from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


def _connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    ensure_wallet_schema(conn)
    return conn


def _rows(
    conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None
) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _build_activity(base: int) -> list[dict]:
    """Five buys spaced 60 s apart to clear the >=30 buys threshold? No — the
    gate runs with `force=True` to keep the fixture small."""
    return [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "0xcid",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "timestamp": base + i * 60,
            "price": 0.40,
            "size": 100,
            "usdcSize": 40,
            "transactionHash": f"0xtx_buy_{i}",
        }
        for i in range(5)
    ] + [
        {
            "type": "TRADE",
            "side": "SELL",
            "conditionId": "0xcid",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "timestamp": base + (5 + i) * 60,
            "price": 0.60,
            "size": 100,
            "usdcSize": 60,
            "transactionHash": f"0xtx_sell_{i}",
        }
        for i in range(5)
    ]


def _stub_client(base: int):
    bars = [
        {
            "timestamp": base + dt,
            "price": 0.40 + (0.05 if dt >= 3600 else 0.0),
        }
        for dt in range(-300, 3600 + 1800, 60)
    ]
    client = MagicMock()
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset_yes", "price": 0.40, "side": "BUY"}
    ] * 5
    return client


def _attach_event_uids(
    activity: list[dict], conn: duckdb.DuckDBPyConnection
) -> list[dict]:
    """Pull event_uids from wallet_events and stamp them onto matching activity
    rows by (condition_id, token_id, ts, side, transaction_hash).
    """
    rows = _rows(
        conn,
        """
        SELECT event_uid, condition_id, token_id, ts, side, transaction_hash
        FROM wallet_events
        WHERE wallet = ? AND ingest_status = 'normalized'
        """,
        [NORMALIZED_WALLET],
    )
    index: dict[tuple, str] = {
        (r["condition_id"], r["token_id"], r["ts"], r["side"], r["transaction_hash"]): r[
            "event_uid"
        ]
        for r in rows
    }
    enriched: list[dict] = []
    for row in activity:
        key = (
            row["conditionId"],
            row["asset"],
            int(row["timestamp"]),
            row["side"],
            row["transactionHash"],
        )
        ev_uid = index.get(key)
        enriched.append({**row, "event_uid": ev_uid} if ev_uid else dict(row))
    return enriched


def test_slice4_pipeline_populates_all_three_tables(
    tmp_path: Path, monkeypatch
) -> None:
    base = 1_700_000_000
    activity = _build_activity(base)
    client = _stub_client(base)

    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    with _connect(tmp_path) as conn:
        # 1. Persist activity into wallet_events.
        upsert_activity_events(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )

        events = _rows(conn, "SELECT COUNT(*) AS n FROM wallet_events")
        assert events[0]["n"] == 10

        # 2. Join event_uids back to activity, then iterate probes.
        enriched = _attach_event_uids(activity, conn)
        records = list(
            iter_fill_probes(
                WALLET,
                enriched,
                client=client,
                classification={"archetype": "directional_discretionary"},
                counts={"trade_records": 30},
                returns={"resolved_position_count": 10},
                force=True,  # below default min_buys threshold; force the gate
                db_path="/tmp/nonexistent/__no__.db",
            )
        )
        assert len(records) == 5
        assert all(r.fill_event_uid for r in records)

        # 3. Persist probes.
        n_probes = upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=records,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )
        assert n_probes == 5

        # 4. Reconstruct flips.
        n_flips = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )
        assert n_flips == 5

        # 5. Cross-table invariants.
        # 5a. Every probe row's fill_event_uid exists in wallet_events.
        orphan_probes = _rows(
            conn,
            """
            SELECT p.fill_event_uid
            FROM wallet_execution_probes p
            LEFT JOIN wallet_events e ON e.wallet = p.wallet AND e.event_uid = p.fill_event_uid
            WHERE e.event_uid IS NULL
            """,
        )
        assert orphan_probes == []

        # 5b. Every horizon row matches a probe row.
        orphan_horizons = _rows(
            conn,
            """
            SELECT h.fill_event_uid, h.horizon_seconds
            FROM wallet_execution_probe_horizons h
            LEFT JOIN wallet_execution_probes p
              ON h.wallet = p.wallet
             AND h.fill_event_uid = p.fill_event_uid
             AND h.probe_config_hash = p.probe_config_hash
            WHERE p.fill_event_uid IS NULL
            """,
        )
        assert orphan_horizons == []

        # 5c. Every flip row's buy_event_uid and sell_event_uid exist in wallet_events.
        orphan_flips = _rows(
            conn,
            """
            SELECT f.buy_event_uid, f.sell_event_uid
            FROM wallet_reconstructed_flips f
            LEFT JOIN wallet_events b ON b.wallet = f.wallet AND b.event_uid = f.buy_event_uid
            LEFT JOIN wallet_events s ON s.wallet = f.wallet AND s.event_uid = f.sell_event_uid
            WHERE b.event_uid IS NULL OR s.event_uid IS NULL
            """,
        )
        assert orphan_flips == []

        # 5d. At least one row each in the four tables.
        for table in (
            "wallet_events",
            "wallet_execution_probes",
            "wallet_execution_probe_horizons",
            "wallet_reconstructed_flips",
        ):
            assert (
                _rows(conn, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] >= 1
            ), f"{table} is empty"

        # 5e. Every flip's input_event_uids_json contains uids that resolve.
        flip_rows = _rows(
            conn,
            "SELECT input_event_uids_json FROM wallet_reconstructed_flips",
        )
        for row in flip_rows:
            uids = json.loads(row["input_event_uids_json"])
            assert len(uids) >= 2
            for uid in uids:
                exists = conn.execute(
                    "SELECT 1 FROM wallet_events WHERE wallet = ? AND event_uid = ?",
                    [NORMALIZED_WALLET, uid],
                ).fetchone()
                assert exists is not None, f"orphan flip contributor {uid!r}"


def test_slice4_pipeline_idempotent_under_repeat(
    tmp_path: Path, monkeypatch
) -> None:
    """Running the full pipeline twice produces no duplicate rows in any
    Slice 4 table."""
    base = 1_700_000_000
    activity = _build_activity(base)
    client = _stub_client(base)

    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    with _connect(tmp_path) as conn:
        for _ in range(2):
            upsert_activity_events(
                conn,
                wallet=WALLET,
                capture_run_id="run-1",
                activity_rows=activity,
                source="polymarket_data_api",
                source_version="2026-05-02",
                captured_at=1_700_100_000,
            )
            enriched = _attach_event_uids(activity, conn)
            records = list(
                iter_fill_probes(
                    WALLET,
                    enriched,
                    client=client,
                    classification={"archetype": "directional_discretionary"},
                    counts={"trade_records": 30},
                    returns={"resolved_position_count": 10},
                    force=True,
                    db_path="/tmp/nonexistent/__no__.db",
                )
            )
            upsert_execution_probes(
                conn,
                wallet=WALLET,
                capture_run_id="run-1",
                fill_probes=records,
                source="polymarket_data_api",
                source_version="2026-05-02",
                captured_at=1_700_100_000,
            )
            reconstruct_and_upsert_flips(
                conn,
                wallet=WALLET,
                capture_run_id="run-1",
                source="polymarket_data_api",
                source_version="2026-05-02",
                captured_at=1_700_100_000,
            )

        counts = {
            t: _rows(conn, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            for t in (
                "wallet_events",
                "wallet_execution_probes",
                "wallet_execution_probe_horizons",
                "wallet_reconstructed_flips",
            )
        }
        assert counts["wallet_events"] == 10
        assert counts["wallet_execution_probes"] == 5
        assert counts["wallet_execution_probe_horizons"] == 15
        assert counts["wallet_reconstructed_flips"] == 5
