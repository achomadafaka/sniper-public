"""Bounded, episode-safe maintenance for the opportunity hot ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from .directional import EXTENDED_HORIZON_RULES
from .insights import project_matured_insights, prune_insight_samples
from .store import retention_delete_authority, transaction


@dataclass(frozen=True)
class RetentionResult:
    projected_launches: int
    deleted_launches: int
    deleted_rows: int
    cutoff_iso: str


@dataclass(frozen=True)
class StorageMaintenance:
    status: str
    blocker_code: str | None
    pause_ingestion: bool
    hot_bytes: int
    insight_bytes: int
    hot_soft_cap_bytes: int
    hot_hard_cap_bytes: int
    retention_s_used: int
    projected_launches: int
    deleted_launches: int
    deleted_rows: int
    pruned_detail_rows: int

    @property
    def pruned_samples(self) -> int:
        """Backward-compatible alias for the pre-v7 maintenance receipt."""
        return self.pruned_detail_rows

    def detail(self) -> dict[str, int | bool | str | None]:
        return {
            "hot_bytes": self.hot_bytes,
            "insight_bytes": self.insight_bytes,
            "hot_soft_cap_bytes": self.hot_soft_cap_bytes,
            "hot_hard_cap_bytes": self.hot_hard_cap_bytes,
            "retention_s_used": self.retention_s_used,
            "projected_launches": self.projected_launches,
            "deleted_launches": self.deleted_launches,
            "deleted_rows": self.deleted_rows,
            "pruned_detail_rows": self.pruned_detail_rows,
            "pruned_samples": self.pruned_detail_rows,
            "pause_ingestion": self.pause_ingestion,
        }


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("retention timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def database_storage_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return 0
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    live_page_bytes = max(0, page_count - free_pages) * page_size
    # WAL files are preallocated and reused after a PASSIVE checkpoint. Count only
    # frames a reader currently prevents SQLite from checkpointing, not the file's
    # physical length or frames already present in `page_count`.
    wal_debt_bytes = 0
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if checkpoint is not None:
            log_frames = int(checkpoint[1])
            checkpointed_frames = int(checkpoint[2])
            pending_frames = max(0, log_frames - checkpointed_frames)
            if pending_frames:
                wal_debt_bytes = 32 + pending_frames * (page_size + 24)
    except sqlite3.OperationalError:
        # Read-only diagnostics may not be allowed to checkpoint. The logical
        # database size remains useful and must not break the health endpoint.
        pass
    return live_page_bytes + wal_debt_bytes


def eligible_launch_ids(
    conn: sqlite3.Connection,
    *,
    cutoff_iso: str,
    max_launches: int,
    candidate_horizon_ms: int = 300_000,
) -> list[str]:
    extended_rules = EXTENDED_HORIZON_RULES
    rule_placeholders = ",".join("?" for _ in extended_rules)
    rows = conn.execute(
        "SELECT l.launch_id FROM op_launches l WHERE l.launched_at<? "
        "AND NOT EXISTS (SELECT 1 FROM op_counterfactual_entries e "
        "JOIN op_landmark_states lm ON lm.landmark_id=e.landmark_id "
        "WHERE lm.launch_id=l.launch_id AND NOT EXISTS ("
        "SELECT 1 FROM op_competing_outcomes o WHERE o.entry_id=e.entry_id)) "
        "AND NOT EXISTS (SELECT 1 FROM op_forward_alerts a "
        "JOIN op_forward_alert_entries link ON link.alert_id=a.alert_id "
        f"WHERE a.launch_id=l.launch_id AND a.rule_version IN ({rule_placeholders}) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM op_executable_observations x WHERE x.entry_id=link.entry_id "
        "AND x.source_healthy=1 AND x.elapsed_ms>=?) AND NOT EXISTS ("
        "SELECT 1 FROM op_competing_outcomes o WHERE o.entry_id=link.entry_id "
        "AND lower(o.outcome_kind)<>'timeout')) "
        "AND NOT EXISTS (SELECT 1 FROM op_canonical_events ce "
        "WHERE ce.launch_id=l.launch_id AND ce.confirmation='tentative' "
        "AND NOT EXISTS (SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=ce.canonical_event_id "
        "AND correction.confirmation IN ('finalized','orphaned'))) "
        "AND NOT EXISTS (SELECT 1 FROM op_source_frontiers sf "
        "WHERE sf.has_open_gap=1 AND sf.source_key LIKE l.venue_id || ':%' "
        "AND julianday(l.launched_at) + (120.0 / 86400.0) >= julianday(COALESCE(("
        "SELECT MIN(g.opened_at) FROM op_data_gaps g WHERE g.source_key=sf.source_key "
        "AND g.resolved_at IS NULL "
        "AND g.previous_durable_cursor IS sf.durable_cursor), sf.updated_at))) "
        "ORDER BY l.launched_at,l.launch_id LIMIT ?",
        (cutoff_iso, *extended_rules, candidate_horizon_ms, max_launches),
    ).fetchall()
    return [row[0] for row in rows]


def _episode_row_count(conn: sqlite3.Connection, launch_id: str) -> int:
    tables = (
        "op_launches", "op_canonical_events", "op_landmark_states",
        "op_state_snapshots", "op_entity_edges",
    )
    total = sum(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE launch_id=?", (launch_id,)).fetchone()[0]
        for table in tables
    )
    total += conn.execute(
        "SELECT COUNT(*) FROM op_venue_event_facts f JOIN op_canonical_events e "
        "ON e.canonical_event_id=f.canonical_event_id WHERE e.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_predictions p JOIN op_state_snapshots s "
        "ON s.snapshot_id=p.snapshot_id WHERE s.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_quote_observations q JOIN op_state_snapshots s "
        "ON s.snapshot_id=q.snapshot_id WHERE s.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_counterfactual_entries e JOIN op_landmark_states l "
        "ON l.landmark_id=e.landmark_id WHERE l.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_competing_outcomes o JOIN op_counterfactual_entries e ON e.entry_id=o.entry_id "
        "JOIN op_landmark_states l ON l.landmark_id=e.landmark_id WHERE l.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_executable_observations x JOIN op_counterfactual_entries e ON e.entry_id=x.entry_id "
        "JOIN op_landmark_states l ON l.landmark_id=e.landmark_id WHERE l.launch_id=?", (launch_id,)
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_forward_alert_entries a "
        "JOIN op_counterfactual_entries e ON e.entry_id=a.entry_id "
        "JOIN op_landmark_states l ON l.landmark_id=e.landmark_id "
        "WHERE l.launch_id=?",
        (launch_id,),
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(*) FROM op_forward_alerts WHERE launch_id=?",
        (launch_id,),
    ).fetchone()[0]
    total += conn.execute(
        "SELECT COUNT(DISTINCT r.raw_event_id) FROM op_raw_events r JOIN op_canonical_events e "
        "ON e.raw_event_id=r.raw_event_id WHERE e.launch_id=?", (launch_id,)
    ).fetchone()[0]
    return total


def _protected_raw_event_ids(conn: sqlite3.Connection) -> set[str]:
    """Raw facts required to prove or resume the durable source frontier."""
    return {
        str(row[0])
        for row in conn.execute(
            "WITH protected(source_key,source_cursor) AS ("
            "SELECT source_key,durable_cursor FROM op_source_frontiers "
            "WHERE durable_cursor IS NOT NULL UNION "
            "SELECT source_key,resume_cursor FROM op_source_frontiers "
            "WHERE resume_cursor IS NOT NULL UNION "
            "SELECT source_key,latest_observed_cursor FROM op_source_frontiers "
            "WHERE latest_observed_cursor IS NOT NULL UNION "
            "SELECT source_key,previous_durable_cursor FROM op_data_gaps "
            "WHERE resolved_at IS NULL AND previous_durable_cursor IS NOT NULL UNION "
            "SELECT source_key,resume_cursor FROM op_data_gaps "
            "WHERE resolved_at IS NULL AND resume_cursor IS NOT NULL UNION "
            "SELECT source_key,newest_observed_cursor FROM op_data_gaps "
            "WHERE resolved_at IS NULL AND newest_observed_cursor IS NOT NULL) "
            "SELECT r.raw_event_id FROM protected p JOIN op_raw_events r "
            "ON r.source_key=p.source_key AND r.source_cursor=p.source_cursor"
        ).fetchall()
    }


def _delete_episode(
    conn: sqlite3.Connection,
    launch_id: str,
    *,
    protected_raw_ids: set[str],
) -> int:
    before = conn.total_changes
    snapshot_ids = [row[0] for row in conn.execute(
        "SELECT snapshot_id FROM op_state_snapshots WHERE launch_id=?", (launch_id,)
    )]
    landmark_ids = [row[0] for row in conn.execute(
        "SELECT landmark_id FROM op_landmark_states WHERE launch_id=?", (launch_id,)
    )]
    entry_ids = [row[0] for row in conn.execute(
        "SELECT e.entry_id FROM op_counterfactual_entries e JOIN op_landmark_states l "
        "ON l.landmark_id=e.landmark_id WHERE l.launch_id=?", (launch_id,)
    )]
    canonical_ids = [row[0] for row in conn.execute(
        "SELECT canonical_event_id FROM op_canonical_events WHERE launch_id=?", (launch_id,)
    )]
    raw_ids = [row[0] for row in conn.execute(
        "SELECT DISTINCT raw_event_id FROM op_canonical_events WHERE launch_id=?", (launch_id,)
    )]

    conn.executemany(
        "DELETE FROM op_forward_alert_entries WHERE entry_id=?",
        [(value,) for value in entry_ids],
    )
    conn.execute(
        "DELETE FROM op_forward_alert_features WHERE alert_id IN ("
        "SELECT alert_id FROM op_forward_alerts WHERE launch_id=?)",
        (launch_id,),
    )
    conn.execute("DELETE FROM op_forward_alerts WHERE launch_id=?", (launch_id,))
    for table in ("op_competing_outcomes", "op_executable_observations"):
        conn.executemany(f"DELETE FROM {table} WHERE entry_id=?", [(value,) for value in entry_ids])
    conn.executemany("DELETE FROM op_counterfactual_entries WHERE entry_id=?", [(value,) for value in entry_ids])
    for table in ("op_predictions", "op_quote_observations"):
        conn.executemany(f"DELETE FROM {table} WHERE snapshot_id=?", [(value,) for value in snapshot_ids])
    conn.execute("DELETE FROM op_entity_edges WHERE launch_id=?", (launch_id,))
    conn.execute("DELETE FROM op_state_snapshots WHERE launch_id=?", (launch_id,))
    # A single statement keeps self-referential supersession constraints consistent.
    conn.execute("DELETE FROM op_landmark_states WHERE launch_id=?", (launch_id,))
    conn.executemany(
        "DELETE FROM op_venue_event_facts WHERE canonical_event_id=?",
        [(value,) for value in canonical_ids],
    )
    conn.execute("DELETE FROM op_launches WHERE launch_id=?", (launch_id,))
    conn.execute("DELETE FROM op_canonical_events WHERE launch_id=?", (launch_id,))
    conn.executemany(
        "DELETE FROM op_raw_events WHERE raw_event_id=? AND NOT EXISTS ("
        "SELECT 1 FROM op_canonical_events WHERE raw_event_id=?)",
        [(value, value) for value in raw_ids if value not in protected_raw_ids],
    )
    return conn.total_changes - before


def _eligible_launchless_event_groups(
    conn: sqlite3.Connection,
    *,
    cutoff_iso: str,
    limit: int,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return old event groups that can no longer form a launch episode.

    A WebSocket can attach mid-launch and deliver trades for a token whose
    launch event predates capture. Those events are useful briefly for source
    continuity, but they can never produce landmarks without an ``op_launches``
    row. Unresolved tentative events remain protected until reconciliation.
    """
    if limit <= 0:
        return []
    bases = conn.execute(
        "SELECT event.canonical_event_id FROM op_canonical_events event "
        "WHERE event.received_at<? AND event.correction_of IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM op_launches launch "
        "WHERE launch.launch_id=event.launch_id) "
        "AND (event.confirmation!='tentative' OR EXISTS ("
        "SELECT 1 FROM op_canonical_events terminal "
        "WHERE terminal.correction_of=event.canonical_event_id "
        "AND terminal.confirmation IN ('finalized','orphaned') "
        "AND terminal.received_at<?)) "
        "AND NOT EXISTS (SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=event.canonical_event_id "
        "AND correction.received_at>=?) "
        "ORDER BY event.received_at,event.canonical_event_id LIMIT ?",
        (cutoff_iso, cutoff_iso, cutoff_iso, limit),
    ).fetchall()
    groups: list[tuple[str, tuple[str, ...]]] = []
    for row in bases:
        base_id = str(row[0])
        correction_ids = tuple(
            str(correction[0])
            for correction in conn.execute(
                "SELECT canonical_event_id FROM op_canonical_events "
                "WHERE correction_of=? ORDER BY received_at,canonical_event_id",
                (base_id,),
            ).fetchall()
        )
        groups.append((base_id, correction_ids))
    return groups


def _delete_launchless_event_groups(
    conn: sqlite3.Connection,
    groups: list[tuple[str, tuple[str, ...]]],
    *,
    protected_raw_ids: set[str],
) -> int:
    if not groups:
        return 0
    before = conn.total_changes
    correction_ids = [value for _, values in groups for value in values]
    base_ids = [base_id for base_id, _ in groups]
    canonical_ids = correction_ids + base_ids
    raw_ids = [
        str(row[0])
        for canonical_id in canonical_ids
        for row in conn.execute(
            "SELECT raw_event_id FROM op_canonical_events WHERE canonical_event_id=?",
            (canonical_id,),
        ).fetchall()
    ]
    conn.executemany(
        "DELETE FROM op_venue_event_facts WHERE canonical_event_id=?",
        [(value,) for value in canonical_ids],
    )
    # Corrections reference their base event, so remove them first.
    conn.executemany(
        "DELETE FROM op_canonical_events WHERE canonical_event_id=?",
        [(value,) for value in correction_ids],
    )
    conn.executemany(
        "DELETE FROM op_canonical_events WHERE canonical_event_id=?",
        [(value,) for value in base_ids],
    )
    conn.executemany(
        "DELETE FROM op_raw_events WHERE raw_event_id=? AND NOT EXISTS ("
        "SELECT 1 FROM op_canonical_events WHERE raw_event_id=?)",
        [(value, value) for value in raw_ids if value not in protected_raw_ids],
    )
    return conn.total_changes - before


def _reclaim_hot_pages(conn: sqlite3.Connection, *, max_pages: int = 8_192) -> None:
    """Bound physical file reclamation so maintenance stays laptop-friendly."""
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    if free_pages:
        # incremental_vacuum yields once per reclaimed page. Python's sqlite
        # driver steps only the first row unless the result cursor is consumed.
        conn.execute(
            f"PRAGMA incremental_vacuum({min(free_pages, max_pages)})"
        ).fetchall()


def discard_launch_episodes(
    conn: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    launch_ids: tuple[str, ...],
    observed_at: str,
) -> int:
    """Project then remove quarantined hot episodes from a discarded span."""
    if not launch_ids:
        return 0
    project_matured_insights(
        conn,
        insights,
        now_iso=observed_at,
        launch_ids=launch_ids,
        allow_incomplete=True,
    )
    missing_receipts = [
        launch_id
        for launch_id in launch_ids
        if insights.execute(
            "SELECT 1 FROM op_projected_launches WHERE launch_id=?",
            (launch_id,),
        ).fetchone() is None
    ]
    if missing_receipts:
        raise RuntimeError(
            "quarantined launch evidence is not fully projected: "
            + ",".join(missing_receipts[:3])
        )
    protected_raw_ids = _protected_raw_event_ids(conn)
    deleted = 0
    with retention_delete_authority(conn):
        with transaction(conn):
            for launch_id in launch_ids:
                if conn.execute(
                    "SELECT 1 FROM op_launches WHERE launch_id=?",
                    (launch_id,),
                ).fetchone() is not None:
                    deleted += _delete_episode(
                        conn,
                        launch_id,
                        protected_raw_ids=protected_raw_ids,
                    )
    return deleted


def cleanup_hot_ledger(
    conn: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    now_iso: str,
    retention_s: int,
    max_launches: int = 25,
    max_rows: int = 2_500,
) -> RetentionResult:
    cutoff_iso = _iso(_utc(now_iso) - timedelta(seconds=retention_s))
    candidates = eligible_launch_ids(conn, cutoff_iso=cutoff_iso, max_launches=max_launches)
    projected = project_matured_insights(
        conn, insights, now_iso=now_iso, launch_ids=candidates
    ) if candidates else 0
    selected: list[str] = []
    row_budget = 0
    for launch_id in candidates:
        if insights.execute(
            "SELECT 1 FROM op_projected_launches WHERE launch_id=?", (launch_id,)
        ).fetchone() is None:
            continue
        count = _episode_row_count(conn, launch_id)
        if selected and row_budget + count > max_rows:
            break
        if count > max_rows:
            selected.append(launch_id)
            row_budget += count
            break
        selected.append(launch_id)
        row_budget += count

    protected_raw_ids = _protected_raw_event_ids(conn)
    launchless_limit = max(0, max_rows - row_budget)
    launchless_groups = _eligible_launchless_event_groups(
        conn,
        cutoff_iso=cutoff_iso,
        limit=launchless_limit,
    )
    launchless_rows = sum(1 + len(corrections) for _, corrections in launchless_groups)
    orphan_limit = max(0, max_rows - row_budget - launchless_rows)
    orphan_raw_ids = [
        row[0] for row in conn.execute(
            "SELECT r.raw_event_id FROM op_raw_events r WHERE r.received_at<? "
            "AND NOT EXISTS (SELECT 1 FROM op_canonical_events e "
            "WHERE e.raw_event_id=r.raw_event_id) "
            "AND NOT EXISTS (SELECT 1 FROM op_source_frontiers f "
            "WHERE f.source_key=r.source_key AND r.source_cursor IN ("
            "f.durable_cursor,f.resume_cursor,f.latest_observed_cursor)) "
            "AND NOT EXISTS (SELECT 1 FROM op_data_gaps g "
            "WHERE g.source_key=r.source_key AND g.resolved_at IS NULL "
            "AND r.source_cursor IN (g.previous_durable_cursor,g.resume_cursor,g.newest_observed_cursor)) "
            "ORDER BY r.received_at,r.raw_event_id LIMIT ?",
            (cutoff_iso, orphan_limit),
        ).fetchall()
    ]

    deleted_rows = 0
    deleted_anything = bool(selected or launchless_groups or orphan_raw_ids)
    if deleted_anything:
        with retention_delete_authority(conn):
            with transaction(conn):
                for launch_id in selected:
                    deleted_rows += _delete_episode(
                        conn,
                        launch_id,
                        protected_raw_ids=protected_raw_ids,
                    )
                deleted_rows += _delete_launchless_event_groups(
                    conn,
                    launchless_groups,
                    protected_raw_ids=protected_raw_ids,
                )
                before = conn.total_changes
                conn.executemany(
                    "DELETE FROM op_raw_events WHERE raw_event_id=?",
                    [(raw_event_id,) for raw_event_id in orphan_raw_ids],
                )
                deleted_rows += conn.total_changes - before
        # Routine cleanup must not wait on or evict observation-only readers.
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    if int(conn.execute("PRAGMA freelist_count").fetchone()[0]):
        if not deleted_anything:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        _reclaim_hot_pages(conn)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return RetentionResult(projected, len(selected), deleted_rows, cutoff_iso)


def maintain_storage(
    conn: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    now_iso: str,
    hot_retention_s: int,
    emergency_retention_s: int,
    insight_retention_s: int,
    alert_retention_s: int | None = None,
    hot_soft_cap_bytes: int,
    hot_hard_cap_bytes: int,
    insight_soft_cap_bytes: int,
    insight_hard_cap_bytes: int,
    max_launches: int = 25,
    max_rows: int = 7_500,
) -> StorageMaintenance:
    initial_hot = database_storage_bytes(conn)
    retention_s = (
        emergency_retention_s if initial_hot >= hot_soft_cap_bytes else hot_retention_s
    )
    cleanup = cleanup_hot_ledger(
        conn, insights, now_iso=now_iso, retention_s=retention_s,
        max_launches=max_launches, max_rows=max_rows,
    )
    insight_cutoff = _iso(_utc(now_iso) - timedelta(seconds=insight_retention_s))
    alert_cutoff = _iso(
        _utc(now_iso)
        - timedelta(
            seconds=insight_retention_s if alert_retention_s is None else alert_retention_s
        )
    )
    pruned = prune_insight_samples(
        insights,
        cutoff_iso=insight_cutoff,
        alert_cutoff_iso=alert_cutoff,
        max_rows=max_rows,
    )
    if pruned:
        insights.execute("PRAGMA wal_checkpoint(PASSIVE)")
        insights.execute("PRAGMA incremental_vacuum(256)")
    hot_bytes = database_storage_bytes(conn)
    insight_bytes = database_storage_bytes(insights)
    hard = hot_bytes >= hot_hard_cap_bytes or insight_bytes >= insight_hard_cap_bytes
    soft = hot_bytes >= hot_soft_cap_bytes or insight_bytes >= insight_soft_cap_bytes
    return StorageMaintenance(
        status="blocked" if hard else "degraded" if soft else "healthy",
        blocker_code="STORAGE_CAP_REACHED" if hard else "STORAGE_PRESSURE" if soft else None,
        pause_ingestion=hard,
        hot_bytes=hot_bytes,
        insight_bytes=insight_bytes,
        hot_soft_cap_bytes=hot_soft_cap_bytes,
        hot_hard_cap_bytes=hot_hard_cap_bytes,
        retention_s_used=retention_s,
        projected_launches=cleanup.projected_launches,
        deleted_launches=cleanup.deleted_launches,
        deleted_rows=cleanup.deleted_rows,
        pruned_detail_rows=pruned,
    )
