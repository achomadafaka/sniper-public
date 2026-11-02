from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace

import pytest

from sniper.research.memecoin_opportunity.store import (
    retention_delete_authority,
    ImmutableConflict,
    LaunchIdentity,
    MissingOpportunityDatabase,
    append_canonical_event,
    append_forward_alert_features,
    append_raw_event,
    connect,
    _RETENTION_DELETE_TABLES,
    connect_existing,
    credit_creator_outcome,
    opportunity_db_path,
    read_creator_prior,
    transaction,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)


NOW = "2026-07-26T14:00:00Z"


def _raw_event(**overrides) -> RawEvent:
    payload = overrides.pop("raw_payload_json", '{"event":"buy"}')
    values = {
        "raw_event_id": "raw-1",
        "network_id": NetworkId.ROBINHOOD_4663,
        "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        "source_key": "4663:bags:provider-a",
        "source_provider": "provider-a",
        "source_cursor": "101:0:2",
        "token_id": "0xabc",
        "transaction_id": "0xdef",
        "chain_order": ChainOrder(101, 0, 2, "0xblock"),
        "chain_timestamp": NOW,
        "received_at": "2026-07-26T14:00:00.100000Z",
        "confirmation": ConfirmationState.CONFIRMED,
        "raw_payload_json": payload,
        "raw_payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }
    values.update(overrides)
    return RawEvent(**values)


def _canonical_event(**overrides) -> CanonicalEvent:
    values = {
        "canonical_event_id": "event-1",
        "raw_event_id": "raw-1",
        "launch_id": "launch-1",
        "network_id": NetworkId.ROBINHOOD_4663,
        "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        "token_id": "0xabc",
        "lifecycle": LifecycleState.PRE_MIGRATION,
        "event_kind": EventKind.LAUNCH,
        "transaction_id": "0xdef",
        "chain_order": ChainOrder(101, 0, 2, "0xblock"),
        "chain_timestamp": NOW,
        "received_at": "2026-07-26T14:00:00.100000Z",
        "decoded_at": "2026-07-26T14:00:00.200000Z",
        "state_applied_at": "2026-07-26T14:00:00.250000Z",
        "confirmation": ConfirmationState.CONFIRMED,
        "actor_id": "0xcreator",
        "quote_delta_atomic": 25_000_000,
        "token_delta_atomic": 9_000_000_000_000_000_000_000,
        "progress_bps": 0,
        "creator_linked": True,
        "decoder_version": "bags-4663-v1",
    }
    values.update(overrides)
    return CanonicalEvent(**values)


def _launch(**overrides) -> LaunchIdentity:
    values = {
        "launch_id": "launch-1",
        "network_id": NetworkId.ROBINHOOD_4663,
        "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        "token_id": "0xabc",
        "creator_id": "0xcreator",
        "launch_event_id": "event-1",
        "launched_at": NOW,
    }
    values.update(overrides)
    return LaunchIdentity(**values)


def _seed(conn):
    append_raw_event(conn, _raw_event())
    append_canonical_event(conn, _canonical_event())
    upsert_launch_identity(conn, _launch())


def test_transaction_batches_append_helpers_without_nested_commits(tmp_path):
    conn = connect(tmp_path / "batched.sqlite")

    with transaction(conn):
        append_raw_event(conn, _raw_event())
        append_canonical_event(conn, _canonical_event())
        assert conn.in_transaction is True

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM op_canonical_events").fetchone()[0] == 1


def test_connect_initializes_only_the_explicit_database(tmp_path):
    path = tmp_path / "opportunity.sqlite"

    conn = connect(path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()

    assert path.exists()
    assert {
        "op_schema_version",
        "op_raw_events",
        "op_canonical_events",
        "op_launches",
        "op_forward_alert_entries",
    } <= tables
    assert not (tmp_path / "data" / "memecoin_opportunity.db").exists()


def test_connect_existing_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite"

    with pytest.raises(MissingOpportunityDatabase):
        connect_existing(path)

    assert not path.exists()


def test_connect_existing_is_read_only(tmp_path):
    path = tmp_path / "opportunity.sqlite"
    connect(path).close()

    conn = connect_existing(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO op_schema_version(version) VALUES (2)")
    conn.close()


def test_append_raw_event_is_exactly_idempotent(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    event = _raw_event()

    assert append_raw_event(conn, event) is True
    assert append_raw_event(conn, event) is False
    assert conn.execute("SELECT count(*) FROM op_raw_events").fetchone()[0] == 1


def test_same_raw_event_id_with_different_content_conflicts(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    event = _raw_event()
    changed = _raw_event(source_cursor="101:0:3")
    append_raw_event(conn, event)

    with pytest.raises(ImmutableConflict, match="raw_event_id"):
        append_raw_event(conn, changed)


def test_canonical_event_requires_existing_raw_event(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    with pytest.raises(ValueError, match="raw_event_id"):
        append_canonical_event(conn, _canonical_event())


def test_launch_identity_requires_existing_launch_event(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    with pytest.raises(ValueError, match="launch_event_id"):
        upsert_launch_identity(conn, _launch())


@pytest.mark.parametrize("statement", [
    "UPDATE op_raw_events SET source_cursor='changed' WHERE raw_event_id='raw-1'",
    "DELETE FROM op_canonical_events WHERE canonical_event_id='event-1'",
    "UPDATE op_launches SET creator_id=NULL WHERE launch_id='launch-1'",
])
def test_update_and_delete_of_immutable_facts_are_rejected(tmp_path, statement):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed(conn)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(statement)


def test_correction_appends_and_preserves_original_event(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event())
    original = _canonical_event()
    append_canonical_event(conn, original)
    correction = replace(
        original,
        canonical_event_id="event-2",
        event_kind=EventKind.CORRECTION,
        correction_of=original.canonical_event_id,
        progress_bps=100,
    )

    assert append_canonical_event(conn, correction) is True
    rows = conn.execute(
        "SELECT canonical_event_id, correction_of FROM op_canonical_events ORDER BY canonical_event_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("event-1", None), ("event-2", "event-1")]


def test_unbounded_atomic_values_are_stored_as_decimal_text(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event())
    append_canonical_event(conn, _canonical_event())

    row = conn.execute(
        "SELECT token_delta_atomic, typeof(token_delta_atomic) FROM op_canonical_events"
    ).fetchone()
    assert tuple(row) == ("9000000000000000000000", "text")


def test_wal_foreign_keys_and_schema_version_are_enabled(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert conn.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_confirmation_corrections_use_indexed_lookup(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    plan = " ".join(
        row[3]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM op_canonical_events "
            "WHERE correction_of=? AND confirmation='orphaned'",
            ("event-1",),
        )
    )

    assert "USING COVERING INDEX idx_op_canonical_correction" in plan


def test_writer_migrates_old_database_without_rewriting_facts(tmp_path):
    path = tmp_path / "opportunity.sqlite"
    conn = connect(path)
    _seed(conn)
    conn.execute("UPDATE op_schema_version SET version=1")
    conn.close()

    migrated = connect(path)
    tables = {
        row[0]
        for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert migrated.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    assert "op_creator_outcomes" in tables
    assert "op_paper_fill_requests" in tables
    assert "op_paper_fills" in tables
    assert migrated.execute("SELECT count(*) FROM op_canonical_events").fetchone()[0] == 1
    assert {
        "op_venue_event_facts",
        "op_state_snapshots",
        "op_entity_edges",
        "op_quote_observations",
        "op_predictions",
        "op_model_registry",
        "op_runtime_status",
        "op_executable_observations",
    } <= tables
    migrated.close()


def test_writer_migrates_legacy_paper_atomic_integer_columns(tmp_path):
    path = tmp_path / "opportunity.sqlite"
    conn = connect(path)
    conn.execute(_paper_request_sql())
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='op_paper_fill_requests'"
    ).fetchone()[0]
    legacy_sql = create_sql.replace("local_qty_atomic TEXT", "local_qty_atomic INTEGER")
    conn.execute("DROP TRIGGER op_paper_fill_requests_no_update")
    conn.execute("DROP TRIGGER op_paper_fill_requests_no_delete")
    conn.execute(
        "ALTER TABLE op_paper_fill_requests "
        "RENAME TO op_paper_fill_requests_v13"
    )
    conn.execute(legacy_sql)
    columns = ",".join(
        row[1] for row in conn.execute("PRAGMA table_info(op_paper_fill_requests_v13)")
    )
    conn.execute(
        f"INSERT INTO op_paper_fill_requests ({columns}) "
        f"SELECT {columns} FROM op_paper_fill_requests_v13"
    )
    conn.execute("DROP TABLE op_paper_fill_requests_v13")
    conn.execute("UPDATE op_schema_version SET version=12")
    conn.close()

    migrated = connect(path)

    declared = {
        row[1]: row[2]
        for row in migrated.execute("PRAGMA table_info(op_paper_fill_requests)")
    }
    assert declared["local_qty_atomic"] == "TEXT"
    assert migrated.execute(
        "SELECT local_qty_atomic FROM op_paper_fill_requests"
    ).fetchone()[0] == "123"
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE op_paper_fill_requests SET local_qty_atomic='124'"
        )


def test_forward_alert_features_are_compact_immutable_and_bounded(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(
        "INSERT INTO op_forward_alerts(alert_id,launch_id,network_id,venue_id,token_id,"
        "snapshot_id,observed_at,clip_usd_micros,rule_version,quote_provider,"
        "round_trip_return_bps,truth_label,row_fingerprint,inserted_at) "
        "VALUES('alert-1','launch-1','solana','pumpfun_solana','mint','snapshot-1',"
        "'2026-07-26T14:00:00Z',25000000,'pump-confirmation-truth-v2','fixture',-400,"
        "'EXPLORATORY','fixture','2026-07-26T14:00:00Z')"
    )

    features = {"effective_fee_bps": 125, "independent_buyer_count": 7}
    assert append_forward_alert_features(conn, "alert-1", features, "confirmation-features-v1") is True
    assert append_forward_alert_features(conn, "alert-1", features, "confirmation-features-v1") is False
    row = conn.execute("SELECT * FROM op_forward_alert_features").fetchone()
    assert row["feature_json"] == '{"effective_fee_bps":125,"independent_buyer_count":7}'

    with pytest.raises(ImmutableConflict):
        append_forward_alert_features(
            conn,
            "alert-1",
            {"effective_fee_bps": 95},
            "confirmation-features-v1",
        )
    with pytest.raises(ValueError, match="4096"):
        append_forward_alert_features(
            conn,
            "alert-2",
            {"oversize": "x" * 4_097},
            "confirmation-features-v1",
        )


def test_alert_entry_relation_has_exact_identity_and_timing_columns(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(op_forward_alert_entries)")
    }

    assert columns == {
        "alert_id": "TEXT",
        "entry_id": "TEXT",
        "snapshot_id": "TEXT",
        "quote_observed_at": "TEXT",
        "quote_completed_at": "TEXT",
        "quote_age_ms": "INTEGER",
        "entry_effective_at": "TEXT",
        "provider": "TEXT",
        "evaluator_version": "TEXT",
        "row_fingerprint": "TEXT",
        "inserted_at": "TEXT",
    }


def test_in_memory_sentinel_is_not_resolved_to_a_shared_file(tmp_path, monkeypatch):
    """`:memory:` is SQLite's sentinel, not a path. Resolving it shared one real file."""
    monkeypatch.chdir(tmp_path)

    assert str(opportunity_db_path(":memory:")) == ":memory:"

    first = connect(":memory:")
    first.execute(
        "INSERT INTO op_runtime_status(source_key,status,heartbeat_at,queue_depth,"
        "dropped_events,detail_json,version) VALUES(?,?,?,0,0,'{}',1)",
        ("probe:ws", "healthy", "2026-07-26T12:00:00Z"),
    )
    second = connect(":memory:")

    assert second.execute("SELECT COUNT(*) FROM op_runtime_status").fetchone()[0] == 0
    assert not (tmp_path / ":memory:").exists()


def test_creator_outcomes_accumulate_only_scored_results(tmp_path):
    """The prior is built from scored outcomes only, per chain and venue."""
    conn = connect(tmp_path / "op.db")

    credit_creator_outcome(
        conn, network_id="solana", venue_id="pumpfun_solana",
        creator_id="Creator1", outcome_kind="profit_first",
        terminal_net_return_bps=1_500, observed_at="2026-07-26T12:00:00Z",
    )
    credit_creator_outcome(
        conn, network_id="solana", venue_id="pumpfun_solana",
        creator_id="Creator1", outcome_kind="loss_first",
        terminal_net_return_bps=-500, observed_at="2026-07-26T12:01:00Z",
    )
    # censored and reorg outcomes carry no information and must not count
    credit_creator_outcome(
        conn, network_id="solana", venue_id="pumpfun_solana",
        creator_id="Creator1", outcome_kind="censored_data_gap",
        terminal_net_return_bps=-9_000, observed_at="2026-07-26T12:02:00Z",
    )

    prior = read_creator_prior(
        conn, network_id="solana", venue_id="pumpfun_solana", creator_id="Creator1"
    )
    assert prior == {"samples": 2, "mean_return_bps": 500}

    # a different chain is a different creator identity
    assert read_creator_prior(
        conn, network_id="robinhood_4663", venue_id="pumpfun_solana", creator_id="Creator1"
    ) == {"samples": 0, "mean_return_bps": None}


def test_creator_outcomes_survive_retention_pruning(tmp_path):
    """Retention prunes launches; the prior would be worthless if it went with them."""
    conn = connect(tmp_path / "op.db")
    credit_creator_outcome(
        conn, network_id="solana", venue_id="pumpfun_solana",
        creator_id="Creator1", outcome_kind="profit_first",
        terminal_net_return_bps=1_200, observed_at="2026-07-26T12:00:00Z",
    )

    # Not in the retention set, and no delete trigger guards it, so the retention
    # sweep that prunes launches and outcomes leaves the record standing.
    assert "op_creator_outcomes" not in _RETENTION_DELETE_TABLES
    triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='op_creator_outcomes'"
        )
    }
    assert triggers == set()
    conn.execute("DELETE FROM op_creator_outcomes WHERE creator_id='absent'")

    assert read_creator_prior(
        conn, network_id="solana", venue_id="pumpfun_solana", creator_id="Creator1"
    )["samples"] == 1


# --- paper adapter ledger -----------------------------------------------------
# Two immutable tables held outside the retention delete set. A receipt whose launch
# pruned minutes later still has to be readable, and a handoff cursor that can lose
# rows to retention is not a cursor.

def _paper_request_sql():
    return (
        "INSERT INTO op_paper_fill_requests (request_id,alert_id,launch_id,network_id,"
        "venue_id,token_id,creator_id,launch_mode,state_fingerprint,rule_version,"
        "evaluator_version,adapter_version,fill_model_version,clip_usd_micros,decision_at,"
        "decision_age_ms,intent_at,expires_at,local_qty_atomic,row_fingerprint,inserted_at) "
        "VALUES ('r1','a1','l1','solana','pumpfun_solana','t1','c1','standard','sf',"
        "'pump-confirmation-truth-v5','alert-outcome-v4','radar-paper-adapter-v1',"
        "'route-floor-v1',25000000,'2026-08-05T00:00:00Z',20000,'2026-08-05T00:00:00Z',"
        "'2026-08-05T00:00:30Z',123,'fp','2026-08-05T00:00:00Z')"
    )


def test_paper_fill_tables_are_immutable_and_retention_excluded(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(_paper_request_sql())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE op_paper_fill_requests SET alert_id='a2' WHERE request_id='r1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM op_paper_fill_requests WHERE request_id='r1'")
    # Retention authority unlocks the pruned tables. It must not unlock these.
    with retention_delete_authority(conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM op_paper_fill_requests WHERE request_id='r1'")


def test_paper_fill_receipt_is_append_only_and_unique_per_request(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(_paper_request_sql())

    def _receipt(request_id):
        conn.execute(
            "INSERT INTO op_paper_fills (request_id,status,receipt_at,row_fingerprint,"
            "inserted_at) VALUES (?,'filled','2026-08-05T00:00:01Z','fp',"
            "'2026-08-05T00:00:01Z')",
            (request_id,),
        )

    _receipt("r1")
    with pytest.raises(sqlite3.IntegrityError):
        _receipt("r1")
    seq = conn.execute(
        "SELECT receipt_seq FROM op_paper_fills WHERE request_id='r1'"
    ).fetchone()[0]
    assert seq >= 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE op_paper_fills SET status='entry_unfilled' WHERE request_id='r1'")
    with retention_delete_authority(conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM op_paper_fills WHERE request_id='r1'")


def test_paper_fill_status_and_reason_agree(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(_paper_request_sql())
    # filled must not carry a reason; entry_unfilled must.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO op_paper_fills (request_id,status,unfilled_reason,receipt_at,"
            "row_fingerprint,inserted_at) VALUES ('r1','filled','no_route','x','fp','x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO op_paper_fills (request_id,status,receipt_at,row_fingerprint,"
            "inserted_at) VALUES ('r1','entry_unfilled','x','fp','x')"
        )


def test_paper_fill_tables_are_not_in_the_retention_delete_set():
    from sniper.research.memecoin_opportunity.store import _RETENTION_DELETE_TABLES

    assert "op_paper_fill_requests" not in _RETENTION_DELETE_TABLES
    assert "op_paper_fills" not in _RETENTION_DELETE_TABLES
