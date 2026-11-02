"""Slice 6 TI-2 — `closed_at` → `timestamp` alias boundary.

Pins the contract that the alias is added at READ time by the
materializer, not at WRITE time by the position writer. A future
writer-side migration would be a conscious change that flips this
test, not a silent drift.

See spec §8 "closed_at → timestamp alias boundary (Slice 6 TI-2)"
for the rationale.
"""
from __future__ import annotations

import time
from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.ledger.materialize import (
    materialize_wallet_data,
)
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet
from sniper.analysis.wallet_protocol.ledger.positions import (
    upsert_position_snapshots,
)
from sniper.analysis.wallet_protocol.ledger.schema import (
    ensure_index_schema,
    ensure_market_schema,
    ensure_wallet_schema,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = normalize_wallet(WALLET)


def _seed(tmp_path: Path, *, position_ts: int, captured_at: int) -> None:
    wallet_path = tmp_path / f"{NORMALIZED_WALLET}.duckdb"
    market_path = tmp_path / "market_data.duckdb"
    index_path = tmp_path / "index.duckdb"
    with duckdb.connect(str(wallet_path)) as wallet_conn, duckdb.connect(
        str(market_path)
    ) as market_conn, duckdb.connect(str(index_path)) as index_conn:
        ensure_wallet_schema(wallet_conn)
        ensure_market_schema(market_conn)
        ensure_index_schema(index_conn)
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-alias",
            position_rows=[
                {
                    "conditionId": "0xcid_alias",
                    "asset": "asset_alias",
                    "outcomeIndex": 0,
                    "outcome": "Yes",
                    "marketStructure": "binary",
                    "avgPrice": 0.40,
                    "size": 100,
                    "totalBought": 40,
                    "realizedPnl": 60,
                    "curPrice": 1.0,
                    "timestamp": position_ts,
                }
            ],
            status="closed_resolution",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=captured_at,
        )


def test_warehouse_row_has_closed_at_only_no_timestamp_column(
    tmp_path: Path,
) -> None:
    """The DuckDB surface is the canonical store: `closed_at`. There is
    NO `timestamp` column on `wallet_positions`."""
    base_ts = int(time.time()) - 1_000
    _seed(tmp_path, position_ts=base_ts, captured_at=base_ts + 100)
    with duckdb.connect(
        str(tmp_path / f"{NORMALIZED_WALLET}.duckdb"), read_only=True
    ) as conn:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'wallet_positions'"
            ).fetchall()
        }
        assert "closed_at" in cols
        assert "timestamp" not in cols, (
            "wallet_positions must not carry a `timestamp` column — the "
            "alias is read-side only per Slice 6 TI-2 (spec §8). Lifting "
            "it into the schema requires a dedicated migration and a "
            "conscious change to this test."
        )

        # And the row's persisted `closed_at` is the value we passed in
        # via the writer's `timestamp` field.
        rows = conn.execute(
            "SELECT closed_at FROM wallet_positions WHERE wallet = ?",
            [NORMALIZED_WALLET],
        ).fetchall()
        assert rows == [(base_ts,)]


def test_materialized_closed_position_carries_both_aliases_equal(
    tmp_path: Path,
) -> None:
    """The materializer adds the read-side `timestamp` alias on closed
    positions. Both keys are populated and equal."""
    base_ts = int(time.time()) - 1_000
    _seed(tmp_path, position_ts=base_ts, captured_at=base_ts + 100)
    window = AnalysisWindow(
        window_id="ti2",
        capture_run_ids=("run-alias",),
        start_ts=base_ts - 10_000,
        end_ts=base_ts + 10_000,
        source_versions=(("polymarket_data_api", "2026-05-02"),),
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=window, ledger_root=tmp_path
    )
    closed = bundle.wallet_data.closed_positions
    assert len(closed) == 1
    only = closed[0]
    assert "closed_at" in only and only["closed_at"] == base_ts
    assert "timestamp" in only and only["timestamp"] == base_ts
    assert only["closed_at"] == only["timestamp"]
