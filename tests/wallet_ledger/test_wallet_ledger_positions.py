from pathlib import Path
import json

import duckdb

from sniper.analysis.wallet_protocol.ledger.positions import upsert_position_snapshots
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


def test_position_snapshots_keep_history_and_move_latest_flag(tmp_path: Path) -> None:
    row = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.40,
        "size": 100,
        "totalBought": 9999,
        "realizedPnl": 0,
        "curPrice": 0.50,
    }
    with _connect(tmp_path) as conn:
        first = upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[row],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000000,
        )
        second = upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-2",
            position_rows=[{**row, "curPrice": 0.60}],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )

        assert first == 1
        assert second == 1
        rows = _rows(
            conn,
            """
            SELECT wallet, capture_run_id, is_latest, current_price, cost_basis_collateral,
                   total_bought_collateral, api_total_bought_raw
            FROM wallet_positions
            ORDER BY capture_run_id
            """,
        )
        assert len(rows) == 2
        assert rows[0]["wallet"] == NORMALIZED_WALLET
        assert rows[0]["capture_run_id"] == "run-1"
        assert rows[0]["is_latest"] is False
        assert rows[1]["capture_run_id"] == "run-2"
        assert rows[1]["is_latest"] is True
        assert rows[1]["current_price"] == 0.60
        assert rows[1]["cost_basis_collateral"] == 40.0
        assert rows[1]["total_bought_collateral"] is None
        assert rows[1]["api_total_bought_raw"] == 9999


def test_position_snapshot_retry_keeps_existing_latest_flag(tmp_path: Path) -> None:
    row = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.40,
        "size": 100,
        "curPrice": 0.50,
    }
    with _connect(tmp_path) as conn:
        first = upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[row],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000000,
        )
        second = upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[row],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000001,
        )

        assert first == 1
        assert second == 0
        rows = _rows(conn, "SELECT capture_run_id, is_latest FROM wallet_positions")
        assert rows == [{"capture_run_id": "run-1", "is_latest": True}]


def test_position_uid_includes_token_id_for_same_condition_hedges(tmp_path: Path) -> None:
    rows = [
        {
            "conditionId": "0xcid",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "avgPrice": 0.40,
            "size": 10,
        },
        {
            "conditionId": "0xcid",
            "asset": "asset_no",
            "outcomeIndex": 1,
            "outcome": "No",
            "avgPrice": 0.60,
            "size": 10,
        },
    ]
    with _connect(tmp_path) as conn:
        written = upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=rows,
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000000,
        )

        assert written == 2
        got = _rows(conn, "SELECT position_uid, token_id, outcome_index FROM wallet_positions ORDER BY token_id")
        assert got[0]["token_id"] == "asset_no"
        assert got[1]["token_id"] == "asset_yes"
        assert got[0]["position_uid"] != got[1]["position_uid"]


def test_position_snapshot_persists_resolved_collateral_provenance(tmp_path: Path) -> None:
    row = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.40,
        "size": 10,
        "collateralToken": "pUSD",
    }
    with _connect(tmp_path) as conn:
        upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[row],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1777334400,
        )

        got = _rows(conn, "SELECT collateral_token, normalization_notes_json FROM wallet_positions")
        assert got[0]["collateral_token"] == "pUSD"
        assert json.loads(got[0]["normalization_notes_json"])["collateral_source"] == "row_explicit"


def test_closed_position_snapshot_uses_timestamp_as_closed_at(tmp_path: Path) -> None:
    row = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.20,
        "size": 50,
        "realizedPnl": 10,
        "curPrice": 1,
        "timestamp": 1700000000,
    }
    with _connect(tmp_path) as conn:
        upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[row],
            status="closed_resolved",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000100,
        )

        got = _rows(conn, "SELECT status, closed_at, closed_at_source, realized_pnl_collateral FROM wallet_positions")
        assert got == [
            {
                "status": "closed_resolved",
                "closed_at": 1700000000,
                "closed_at_source": "position.timestamp",
                "realized_pnl_collateral": 10.0,
            }
        ]


# ---------------------------------------------------------------------------
# Slice 6 TI-1 — numeric-zero persistence
# Pre-existing bug: `_float_or_none(row.get("curPrice") or row.get("current_price"))`
# short-circuits a literal `0.0` to `None` because Python treats `0.0` as
# falsy. Same pattern affects every numeric alias chain. The fix replaces
# the truthiness fallback with explicit `is not None` selection so a
# resolved-NO outcome (curPrice == 0.0) round-trips intact.
# ---------------------------------------------------------------------------


def _seed_one(
    tmp_path: Path,
    *,
    overrides: dict,
    base: dict | None = None,
) -> dict:
    base_row = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.40,
        "size": 100,
        "totalBought": 40,
        "realizedPnl": 0,
        "curPrice": 0.50,
    }
    if base is not None:
        base_row = base
    base_row.update(overrides)
    with _connect(tmp_path) as conn:
        upsert_position_snapshots(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            position_rows=[base_row],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1700000000,
        )
        rows = _rows(
            conn,
            """
            SELECT current_price, avg_price, size_tokens, realized_pnl_collateral,
                   total_bought_collateral, api_total_bought_raw,
                   api_initial_value_raw, opened_at, closed_at,
                   unrealized_pnl_collateral
            FROM wallet_positions
            """,
        )
    assert len(rows) == 1
    return rows[0]


def test_curPrice_zero_persists_as_zero(tmp_path: Path) -> None:
    """Regression: curPrice=0.0 must NOT short-circuit to None.

    A resolved-NO position carries curPrice == 0.0 in real Polymarket
    data; the writer dropping it to None pushed every NO winner out of
    the held-to-resolution filter (Slice 5 Task 8 worked around this
    with 0.005). Slice 6 TI-1 fixes the writer.
    """
    got = _seed_one(tmp_path, overrides={"curPrice": 0.0})
    assert got["current_price"] == 0.0


def test_curPrice_falls_back_to_current_price_when_curPrice_absent(
    tmp_path: Path,
) -> None:
    """When the camelCase alias is missing, the snake_case form wins —
    even when its value is 0.42 (truthy) or 0.0 (zero)."""
    base = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avgPrice": 0.40,
        "size": 100,
        "current_price": 0.42,  # snake_case only
        "realizedPnl": 0,
    }
    got = _seed_one(tmp_path, base=base, overrides={})
    assert got["current_price"] == 0.42


def test_avgPrice_zero_persists_as_zero(tmp_path: Path) -> None:
    """Defensive: avgPrice=0.0 is degenerate but legal data; persist it."""
    got = _seed_one(tmp_path, overrides={"avgPrice": 0.0})
    assert got["avg_price"] == 0.0


def test_size_zero_persists_as_zero(tmp_path: Path) -> None:
    got = _seed_one(tmp_path, overrides={"size": 0})
    assert got["size_tokens"] == 0.0


def test_realizedPnl_zero_persists_as_zero(tmp_path: Path) -> None:
    got = _seed_one(tmp_path, overrides={"realizedPnl": 0})
    assert got["realized_pnl_collateral"] == 0.0


def test_zero_falls_through_to_snake_case_alias(tmp_path: Path) -> None:
    """Both camelCase and snake_case can carry numeric zero independently;
    the first present alias wins, regardless of truthiness."""
    base = {
        "conditionId": "0xcid",
        "asset": "asset_yes",
        "outcomeIndex": 0,
        "outcome": "Yes",
        "avg_price": 0.0,           # snake_case zero
        "size_tokens": 0.0,         # snake_case zero
        "realized_pnl": 0.0,
        "curPrice": 0.50,
    }
    got = _seed_one(tmp_path, base=base, overrides={})
    assert got["avg_price"] == 0.0
    assert got["size_tokens"] == 0.0
    assert got["realized_pnl_collateral"] == 0.0
