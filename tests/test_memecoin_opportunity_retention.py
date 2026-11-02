import hashlib
import inspect
import sqlite3

import pytest

from sniper.research.memecoin_opportunity.insights import connect_insights
from sniper.research.memecoin_opportunity.continuity import record_source_failure
from sniper.research.memecoin_opportunity.reconciliation import reconcile_confirmation
from sniper.research.memecoin_opportunity.retention import (
    _protected_raw_event_ids,
    _reclaim_hot_pages,
    cleanup_hot_ledger,
    database_storage_bytes,
    discard_launch_episodes,
    maintain_storage,
)
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    connect,
    retention_delete_authority,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent, ChainOrder, ConfirmationState, EventKind, LifecycleState,
    NetworkId, RawEvent, VenueId,
)


def test_runtime_cleanup_budget_keeps_up_with_high_launch_churn():
    assert inspect.signature(maintain_storage).parameters["max_rows"].default == 7_500


def test_protected_raw_lookup_does_not_scan_resolved_gap_history(tmp_path):
    conn = connect(tmp_path / "hot.db")
    for index in range(50):
        _seed_unattached_trade(
            conn,
            str(index),
            at="2026-07-20T00:00:00Z",
        )
    conn.execute(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        (
            "pumpfun_solana:ws",
            None,
            None,
            None,
            0,
            1,
            "2026-07-20T00:00:00Z",
        ),
    )
    conn.executemany(
        "INSERT INTO op_data_gaps VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            (
                f"gap-{index}",
                "pumpfun_solana:ws",
                "fetch_failure",
                f"old-{index}",
                f"resume-{index}",
                f"latest-{index}",
                "resolved fixture",
                "2026-07-20T00:00:00Z",
                "2026-07-20T00:00:01Z",
                f"fingerprint-{index}",
            )
            for index in range(2_000)
        ),
    )
    progress_calls = 0

    def count_progress():
        nonlocal progress_calls
        progress_calls += 1
        return 0

    conn.set_progress_handler(count_progress, 1_000)
    try:
        assert _protected_raw_event_ids(conn) == set()
    finally:
        conn.set_progress_handler(None, 0)

    assert progress_calls < 50


def test_reclaim_hot_pages_consumes_the_full_incremental_vacuum(tmp_path):
    conn = connect(tmp_path / "hot.db")
    conn.execute("CREATE TABLE reclaim_fixture(payload BLOB NOT NULL)")
    conn.execute(
        "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM rows WHERE n<500) "
        "INSERT INTO reclaim_fixture SELECT randomblob(2048) FROM rows"
    )
    conn.execute("DELETE FROM reclaim_fixture")
    pages_before = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])

    _reclaim_hot_pages(conn, max_pages=64)

    pages_after = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert free_before >= 64
    assert pages_before - pages_after >= 64
    assert free_before - free_after >= 64


def test_storage_cap_counts_live_pages_not_reusable_wal_allocation(tmp_path):
    hot_path = tmp_path / "hot.db"
    conn = connect(hot_path)
    conn.execute("CREATE TABLE storage_fixture(payload BLOB NOT NULL)")
    conn.execute(
        "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM rows WHERE n<500) "
        "INSERT INTO storage_fixture SELECT randomblob(2048) FROM rows"
    )
    conn.execute("DELETE FROM storage_fixture")
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    physical_bytes = sum(
        path.stat().st_size for path in (
            hot_path,
            tmp_path / "hot.db-wal",
            tmp_path / "hot.db-shm",
        ) if path.exists()
    )

    assert database_storage_bytes(conn) == (page_count - free_pages) * page_size
    assert database_storage_bytes(conn) < physical_bytes


def test_storage_cap_includes_wal_frames_pinned_by_a_reader(tmp_path):
    hot_path = tmp_path / "hot.db"
    conn = connect(hot_path)
    conn.execute("CREATE TABLE wal_fixture(payload BLOB NOT NULL)")
    conn.execute("INSERT INTO wal_fixture VALUES(randomblob(2048))")
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    reader = sqlite3.connect(hot_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM wal_fixture").fetchone()
    conn.execute(
        "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM rows WHERE n<200) "
        "INSERT INTO wal_fixture SELECT randomblob(2048) FROM rows"
    )

    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    live_page_bytes = (page_count - free_pages) * page_size

    try:
        assert database_storage_bytes(conn) > live_page_bytes
    finally:
        reader.rollback()
        reader.close()


def test_cleanup_reclaims_pages_left_by_an_earlier_cleanup(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    conn.execute("CREATE TABLE reclaim_fixture(payload BLOB NOT NULL)")
    conn.execute(
        "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM rows WHERE n<500) "
        "INSERT INTO reclaim_fixture SELECT randomblob(2048) FROM rows"
    )
    conn.execute("DELETE FROM reclaim_fixture")
    pages_before = int(conn.execute("PRAGMA page_count").fetchone()[0])

    result = cleanup_hot_ledger(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        retention_s=300, max_launches=25, max_rows=2_500,
    )

    pages_after = int(conn.execute("PRAGMA page_count").fetchone()[0])
    assert result.deleted_rows == 0
    assert pages_before - pages_after >= 64


def test_routine_cleanup_only_uses_nonblocking_passive_checkpoints(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    statements = []
    conn.set_trace_callback(statements.append)

    cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:00:00Z",
        retention_s=300,
        max_launches=25,
        max_rows=2_500,
    )

    checkpoints = [statement.upper() for statement in statements if "WAL_CHECKPOINT" in statement.upper()]
    assert checkpoints
    assert all("PASSIVE" in statement for statement in checkpoints)
    assert all("TRUNCATE" not in statement for statement in checkpoints)


def test_discard_launch_episodes_projects_then_removes_quarantined_state(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")

    deleted = discard_launch_episodes(
        conn,
        insights,
        launch_ids=("l-1",),
        observed_at="2026-07-20T00:00:03Z",
    )

    assert deleted >= 3
    assert conn.execute(
        "SELECT 1 FROM op_launches WHERE launch_id='l-1'"
    ).fetchone() is None
    assert insights.execute(
        "SELECT 1 FROM op_projected_launches WHERE launch_id='l-1'"
    ).fetchone() is not None


def _seed(conn, suffix: str, *, at: str, confirmation=ConfirmationState.FINALIZED):
    payload = '{}'
    raw = RawEvent(
        raw_event_id=f"r-{suffix}", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, source_key="pumpfun_solana:ws",
        source_provider="test", source_cursor=suffix, token_id=f"mint-{suffix}",
        transaction_id=f"tx-{suffix}", chain_order=ChainOrder(int(suffix), 0, 0, f"b-{suffix}"),
        chain_timestamp=at, received_at=at, confirmation=confirmation,
        raw_payload_json=payload, raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        canonical_event_id=f"e-{suffix}", raw_event_id=raw.raw_event_id,
        launch_id=f"l-{suffix}", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, token_id=raw.token_id,
        lifecycle=LifecycleState.PRE_MIGRATION, event_kind=EventKind.LAUNCH,
        transaction_id=raw.transaction_id, chain_order=raw.chain_order,
        chain_timestamp=at, received_at=at, decoded_at=at, state_applied_at=at,
        confirmation=confirmation, actor_id=None, quote_delta_atomic=None,
        token_delta_atomic=None, progress_bps=0, creator_linked=None,
        decoder_version="test-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    upsert_launch_identity(conn, LaunchIdentity(
        launch_id=event.launch_id, network_id=event.network_id, venue_id=event.venue_id,
        token_id=event.token_id, creator_id=None, launch_event_id=event.canonical_event_id,
        launched_at=at,
    ))


def _seed_unattached_trade(
    conn, suffix: str, *, at: str, confirmation=ConfirmationState.FINALIZED
):
    payload = '{}'
    raw = RawEvent(
        raw_event_id=f"orphan-r-{suffix}", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, source_key="pumpfun_solana:ws",
        source_provider="test", source_cursor=f"orphan-{suffix}",
        token_id=f"orphan-mint-{suffix}", transaction_id=f"orphan-tx-{suffix}",
        chain_order=ChainOrder(100 + int(suffix), 0, 0, f"orphan-b-{suffix}"),
        chain_timestamp=at, received_at=at, confirmation=confirmation,
        raw_payload_json=payload,
        raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        canonical_event_id=f"orphan-e-{suffix}", raw_event_id=raw.raw_event_id,
        launch_id=f"missing-launch-{suffix}", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, token_id=raw.token_id,
        lifecycle=LifecycleState.PRE_MIGRATION, event_kind=EventKind.BUY,
        transaction_id=raw.transaction_id, chain_order=raw.chain_order,
        chain_timestamp=at, received_at=at, decoded_at=at, state_applied_at=at,
        confirmation=confirmation, actor_id="buyer", quote_delta_atomic=100,
        token_delta_atomic=-10, progress_bps=10, creator_linked=False,
        decoder_version="test-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    return event


def test_delete_authority_is_connection_local_and_restored(tmp_path):
    conn = connect(tmp_path / "hot.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM op_launches WHERE launch_id='l-1'")
    with pytest.raises(RuntimeError):
        with retention_delete_authority(conn):
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM op_launches WHERE launch_id='l-1'")


def test_cleanup_projects_then_deletes_only_safe_old_episode(tmp_path):
    hot_path = tmp_path / "hot.db"
    conn = connect(hot_path)
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    _seed(conn, "2", at="2026-07-20T00:00:00Z", confirmation=ConfirmationState.TENTATIVE)
    _seed(conn, "3", at="2026-07-26T11:55:00Z")
    orphan = RawEvent(
        raw_event_id="orphan", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, source_key="pumpfun_solana:ws",
        source_provider="test", source_cursor="orphan", token_id="orphan-mint",
        transaction_id="orphan-tx", chain_order=ChainOrder(99, 0, 0, "orphan-block"),
        chain_timestamp="2026-07-20T00:00:00Z", received_at="2026-07-20T00:00:00Z",
        confirmation=ConfirmationState.FINALIZED, raw_payload_json='{}',
        raw_payload_hash=hashlib.sha256(b'{}').hexdigest(),
    )
    append_raw_event(conn, orphan)
    conn.execute(
        "INSERT INTO op_forward_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "alert-l1", "l-1", "solana", "pumpfun_solana", "mint-1", "snapshot-l1",
            "2026-07-20T00:00:00Z", 25_000_000, "level1-v2", "fixture", -200,
            "EXPLORATORY PAPER ALERT", "alert-fingerprint", "2026-07-20T00:00:00Z",
        ),
    )

    result = cleanup_hot_ledger(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        retention_s=1800, max_launches=25, max_rows=2500,
    )

    assert result.deleted_launches == 1
    assert [row[0] for row in conn.execute("SELECT launch_id FROM op_launches ORDER BY launch_id")] == ["l-2", "l-3"]
    assert insights.execute("SELECT launch_id FROM op_projected_launches").fetchone()[0] == "l-1"
    assert conn.execute(
        "SELECT 1 FROM op_forward_alerts WHERE alert_id='alert-l1'"
    ).fetchone() is None
    assert insights.execute(
        "SELECT score_status FROM op_insight_alerts WHERE alert_id='alert-l1'"
    ).fetchone()[0] == "UNSCORED_LEGACY"
    assert conn.execute("SELECT 1 FROM op_raw_events WHERE raw_event_id='orphan'").fetchone() is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    busy, log_frames, checkpointed_frames = conn.execute(
        "PRAGMA wal_checkpoint(PASSIVE)"
    ).fetchone()
    assert busy == 0
    assert checkpointed_frames == log_frames


def test_cleanup_deletes_old_launchless_events_but_keeps_unresolved_tentative(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    finalized = _seed_unattached_trade(
        conn, "1", at="2026-07-20T00:00:00Z"
    )
    corrected = _seed_unattached_trade(
        conn, "2", at="2026-07-20T00:00:01Z",
        confirmation=ConfirmationState.TENTATIVE,
    )
    unresolved = _seed_unattached_trade(
        conn, "3", at="2026-07-20T00:00:02Z",
        confirmation=ConfirmationState.TENTATIVE,
    )
    reconcile_confirmation(
        conn,
        canonical_event_id=corrected.canonical_event_id,
        confirmation=ConfirmationState.FINALIZED,
        reconciled_at="2026-07-20T00:00:03Z",
    )

    result = cleanup_hot_ledger(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        retention_s=300, max_launches=25, max_rows=2_500,
    )

    assert result.deleted_launches == 0
    assert result.deleted_rows >= 6
    assert conn.execute(
        "SELECT 1 FROM op_canonical_events WHERE canonical_event_id=?",
        (finalized.canonical_event_id,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM op_canonical_events WHERE canonical_event_id=?",
        (corrected.canonical_event_id,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM op_canonical_events WHERE canonical_event_id=?",
        (unresolved.canonical_event_id,),
    ).fetchone() is not None


def test_launchless_cleanup_preserves_durable_frontier_raw_fact(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    event = _seed_unattached_trade(conn, "1", at="2026-07-20T00:00:00Z")
    conn.execute(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        (
            "pumpfun_solana:ws", "orphan-1", None, "orphan-1", 0, 1,
            "2026-07-20T00:00:01Z",
        ),
    )

    cleanup_hot_ledger(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        retention_s=300, max_launches=25, max_rows=2_500,
    )

    assert conn.execute(
        "SELECT 1 FROM op_canonical_events WHERE canonical_event_id=?",
        (event.canonical_event_id,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM op_raw_events WHERE raw_event_id=?",
        (event.raw_event_id,),
    ).fetchone() is not None


def test_cleanup_deletes_one_complete_episode_when_it_exceeds_row_budget(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    _seed(conn, "2", at="2026-07-20T00:00:01Z")

    result = cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:00:00Z",
        retention_s=300,
        max_launches=25,
        max_rows=1,
    )

    assert result.deleted_launches == 1
    assert [row[0] for row in conn.execute(
        "SELECT launch_id FROM op_launches ORDER BY launch_id"
    )] == ["l-2"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hard_cap_blocks_ingestion_when_safe_cleanup_cannot_get_under_cap(tmp_path):
    hot_path = tmp_path / "hot.db"
    insight_path = tmp_path / "insights.db"
    conn = connect(hot_path)
    insights = connect_insights(insight_path)
    _seed(conn, "3", at="2026-07-26T11:59:59Z")

    result = maintain_storage(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        hot_retention_s=1800, emergency_retention_s=300,
        insight_retention_s=7 * 86400,
        hot_soft_cap_bytes=1, hot_hard_cap_bytes=2,
        insight_soft_cap_bytes=10**9, insight_hard_cap_bytes=2 * 10**9,
    )

    assert result.status == "blocked"
    assert result.blocker_code == "STORAGE_CAP_REACHED"
    assert result.pause_ingestion is True
    assert result.hot_bytes > result.hot_hard_cap_bytes


def test_gap_protects_only_launches_whose_outcome_window_overlaps_it(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    _seed(conn, "2", at="2026-07-20T00:09:30Z")
    record_source_failure(
        conn, source_key="pumpfun_solana:ws",
        observed_at="2026-07-20T00:10:00Z", detail="fixture gap",
    )

    result = cleanup_hot_ledger(
        conn, insights, now_iso="2026-07-26T12:00:00Z",
        retention_s=1800, max_launches=25, max_rows=2500,
    )

    assert result.deleted_launches == 1
    assert [row[0] for row in conn.execute("SELECT launch_id FROM op_launches")] == ["l-2"]


def test_resolved_gap_with_reused_durable_cursor_does_not_pin_old_launch(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    conn.execute(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        (
            "pumpfun_solana:ws", "1", "current-resume", "current-latest",
            1, 3, "2026-07-20T00:30:00Z",
        ),
    )
    conn.executemany(
        "INSERT INTO op_data_gaps VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            (
                "resolved-gap", "pumpfun_solana:ws", "fetch_failure", "1",
                "old-resume", "old-latest", "old", "2026-07-20T00:01:00Z",
                "2026-07-20T00:02:00Z", "resolved-fingerprint",
            ),
            (
                "current-gap", "pumpfun_solana:ws", "fetch_failure", "1",
                "current-resume", "current-latest", "current",
                "2026-07-20T00:30:00Z", None, "current-fingerprint",
            ),
        ),
    )

    result = cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:00:00Z",
        retention_s=300,
        max_launches=25,
        max_rows=2_500,
    )

    assert result.deleted_launches == 1
    assert conn.execute("SELECT 1 FROM op_launches").fetchone() is None


def test_cleanup_retains_raw_fact_named_by_durable_frontier_cursor(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    _seed(conn, "2", at="2026-07-20T00:00:01Z")
    conn.execute(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        ("pumpfun_solana:ws", "1", None, "1", 0, 1, "2026-07-20T00:00:02Z"),
    )

    result = cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:00:00Z",
        retention_s=300,
        max_launches=25,
        max_rows=2_500,
    )

    assert result.deleted_launches == 2
    assert [row[0] for row in conn.execute(
        "SELECT raw_event_id FROM op_raw_events ORDER BY raw_event_id"
    ).fetchall()] == ["r-1"]


def test_cleanup_retains_orphan_raw_fact_named_by_unresolved_gap_cursor(tmp_path):
    conn = connect(tmp_path / "hot.db")
    insights = connect_insights(tmp_path / "insights.db")
    _seed(conn, "1", at="2026-07-20T00:00:00Z")
    orphan = RawEvent(
        raw_event_id="gap-raw",
        network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA,
        source_key="pumpfun_solana:ws",
        source_provider="test",
        source_cursor="gap-resume",
        token_id="gap-mint",
        transaction_id="gap-tx",
        chain_order=ChainOrder(3, 0, 0, "gap-block"),
        chain_timestamp="2026-07-20T00:00:03Z",
        received_at="2026-07-20T00:00:03Z",
        confirmation=ConfirmationState.FINALIZED,
        raw_payload_json="{}",
        raw_payload_hash=hashlib.sha256(b"{}").hexdigest(),
    )
    append_raw_event(conn, orphan)
    conn.execute(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        (
            "pumpfun_solana:ws",
            "1",
            "gap-resume",
            "gap-resume",
            1,
            1,
            "2026-07-20T00:00:04Z",
        ),
    )
    conn.execute(
        "INSERT INTO op_data_gaps VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "gap-1",
            "pumpfun_solana:ws",
            "enumeration_bound",
            "1",
            "gap-resume",
            "gap-resume",
            "fixture",
            "2026-07-20T00:00:04Z",
            None,
            "gap-fingerprint",
        ),
    )

    cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:00:00Z",
        retention_s=300,
        max_launches=25,
        max_rows=2_500,
    )

    assert [row[0] for row in conn.execute(
        "SELECT raw_event_id FROM op_raw_events WHERE source_cursor='gap-resume'"
    ).fetchall()] == ["gap-raw"]
