from __future__ import annotations

import inspect
import sqlite3

import pytest

from sniper.persistence import db_memecoin


NOW = "2026-08-22T00:00:00+00:00"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db_memecoin.ensure_memecoin_tables(connection)
    return connection


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _attempt(conn, attempt_id: str = "attempt-1") -> None:
    db_memecoin.start_entry_attempt(
        conn,
        attempt_id=attempt_id,
        decision_started_at=NOW,
        origin_event_id="event-1",
        signal_source="global_feed",
        network_id="solana",
        mint="MINT",
    )


def test_cluster_fields_exist_on_all_funded_entry_rows(conn) -> None:
    assert {
        "cluster_decision_id",
        "cluster_reason",
        "cluster_metrics_json",
        "cluster_completed_at",
        "cluster_ms",
        "cluster_outcome_id",
        "cluster_outcome_status",
    } <= _columns(conn, "memecoin_entry_attempts")
    assert "cluster_decision_id" in _columns(conn, "memecoin_pending_entries")
    assert "cluster_decision_id" in _columns(conn, "memecoin_positions")


def test_entry_attempt_cluster_decision_is_immutable(conn) -> None:
    _attempt(conn)
    assert db_memecoin.set_entry_attempt_cluster_decision(
        conn,
        "attempt-1",
        decision_id="decision-1",
        reason="cluster_share_high",
        metrics_json='{"attempted_supply":100}',
        completed_at=NOW,
        duration_ms=125.0,
    ) is True
    with pytest.raises(ValueError, match="immutable"):
        db_memecoin.set_entry_attempt_cluster_decision(
            conn,
            "attempt-1",
            decision_id="decision-2",
            reason="allowed",
            metrics_json='{"attempted_supply":100}',
            completed_at=NOW,
            duration_ms=130.0,
        )


def test_blocked_decision_opens_fixed_v2_outcome(conn) -> None:
    from sniper.services.memecoin_capture_v2 import create_cluster_block_outcome

    _attempt(conn)
    outcome_id = create_cluster_block_outcome(
        conn,
        attempt_id="attempt-1",
        mint="MINT",
        observed_at=NOW,
        ref_price=0.001,
    )
    row = conn.execute(
        "SELECT upstream_event_id,target_horizons_json FROM memecoin_signal_outcomes_v2 "
        "WHERE outcome_id=?",
        (outcome_id,),
    ).fetchone()
    assert tuple(row) == ("cluster-block:attempt-1", "[15,30,60,300]")


def test_missing_block_reference_stays_typed_and_creates_no_flat_outcome(conn) -> None:
    from sniper.services.memecoin_capture_v2 import record_cluster_block_outcome

    _attempt(conn)
    status = record_cluster_block_outcome(
        conn,
        attempt_id="attempt-1",
        mint="MINT",
        observed_at=NOW,
        ref_price=None,
    )
    row = conn.execute(
        "SELECT cluster_outcome_id,cluster_outcome_status FROM memecoin_entry_attempts "
        "WHERE attempt_id='attempt-1'"
    ).fetchone()
    assert status == "coverage_missing_ref_price"
    assert tuple(row) == (None, "coverage_missing_ref_price")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes_v2").fetchone()[0] == 0


def test_cluster_trace_stage_has_completed_time_and_duration_columns(conn) -> None:
    from sniper.services import memecoin_entry_trace

    assert "cluster" in memecoin_entry_trace._STAGES
    assert {"cluster_completed_at", "cluster_ms"} <= _columns(conn, "memecoin_entry_attempts")


def test_cluster_decision_id_propagates_through_every_funded_write_interface() -> None:
    from sniper.execution.solana_swap_executor import execute_entry
    from sniper.services.memecoin_risk import reserve_entry

    assert "cluster_decision_id" in inspect.signature(reserve_entry).parameters
    assert "cluster_decision_id" in inspect.signature(db_memecoin.reserve_pending).parameters
    assert "cluster_decision_id" in inspect.signature(db_memecoin.open_position).parameters
    assert "cluster_decision_id" in inspect.signature(execute_entry).parameters
