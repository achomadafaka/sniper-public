"""Recoverable source-frontier transitions that never skip known gaps."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from .identity import content_fingerprint, require_utc_iso
from .store import ImmutableConflict, transaction


class GapKind(str, Enum):
    ENUMERATION_BOUND = "enumeration_bound"
    FETCH_FAILURE = "fetch_failure"
    DECODE_FAILURE = "decode_failure"
    ORDERING_DISCONTINUITY = "ordering_discontinuity"
    REORG = "reorg"
    RECOVERY_DISCARDED = "recovery_discarded"


class FrontierConflict(RuntimeError):
    """A frontier batch was computed from stale or inconsistent recovery state."""


def _optional_cursor(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string or None")
    return value.strip()


def _required(value: str, field_name: str) -> str:
    normalized = _optional_cursor(value, field_name)
    if normalized is None:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


@dataclass(frozen=True)
class SourceFrontier:
    source_key: str
    durable_cursor: str | None
    resume_cursor: str | None
    latest_observed_cursor: str | None
    has_open_gap: bool
    version: int
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _required(self.source_key, "source_key"))
        for field_name in ("durable_cursor", "resume_cursor", "latest_observed_cursor"):
            object.__setattr__(
                self,
                field_name,
                _optional_cursor(getattr(self, field_name), field_name),
            )
        if not isinstance(self.has_open_gap, bool):
            raise ValueError("has_open_gap must be boolean")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        object.__setattr__(self, "updated_at", require_utc_iso(self.updated_at))


@dataclass(frozen=True)
class FrontierBatch:
    source_key: str
    expected_version: int
    previous_durable_cursor: str | None
    newest_observed_cursor: str | None
    contiguous_durable_cursor: str | None
    resume_cursor: str | None
    reached_previous_frontier: bool
    enumeration_complete: bool
    decode_complete: bool
    observed_at: str
    gap_kind: GapKind | None = None
    gap_detail: str | None = None
    resolved_gap_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _required(self.source_key, "source_key"))
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative integer")
        for field_name in (
            "previous_durable_cursor",
            "newest_observed_cursor",
            "contiguous_durable_cursor",
            "resume_cursor",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_cursor(getattr(self, field_name), field_name),
            )
        for field_name in (
            "reached_previous_frontier",
            "enumeration_complete",
            "decode_complete",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        object.__setattr__(self, "observed_at", require_utc_iso(self.observed_at))
        if self.gap_kind is not None:
            object.__setattr__(self, "gap_kind", GapKind(self.gap_kind))
        normalized_ids = tuple(_required(value, "resolved_gap_id") for value in self.resolved_gap_ids)
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("resolved_gap_ids must be unique")
        object.__setattr__(self, "resolved_gap_ids", normalized_ids)


@dataclass(frozen=True)
class FrontierDecision:
    frontier: SourceFrontier
    open_gap: bool
    gap_id: str | None
    gap_kind: GapKind | None
    gap_detail: str | None


def _gap_position(batch: FrontierBatch) -> tuple[str | None, str | None, str | None]:
    """Where a gap sits, independent of when it happened. Dedups a still-open gap."""
    return (
        batch.gap_kind.value if batch.gap_kind is not None else None,
        batch.previous_durable_cursor,
        batch.resume_cursor,
    )


def _gap_identity(batch: FrontierBatch) -> str:
    # `observed_at` is part of the identity because two failures at an unchanged
    # frontier are two facts, not one. Keyed on position alone, the second failure of
    # a quiet source recomputed the first gap's id, found it already resolved in this
    # append-only ledger, and `_persist_gap` raised ImmutableConflict -- from inside
    # the capture lane's own `except` handler, which killed the lane outright.
    return content_fingerprint(
        {
            "source_key": batch.source_key,
            "gap_kind": batch.gap_kind,
            "previous_durable_cursor": batch.previous_durable_cursor,
            "resume_cursor": batch.resume_cursor,
            "observed_at": batch.observed_at,
        }
    )


def decide_frontier(
    current: SourceFrontier | None,
    batch: FrontierBatch,
    *,
    prior_gaps_resolved: bool = False,
) -> FrontierDecision:
    expected_version = current.version if current is not None else 0
    expected_cursor = current.durable_cursor if current is not None else None
    if batch.expected_version != expected_version:
        raise FrontierConflict(
            f"frontier version changed: expected {batch.expected_version}, current {expected_version}"
        )
    if batch.previous_durable_cursor != expected_cursor:
        raise FrontierConflict(
            "previous_durable_cursor does not match current durable frontier"
        )

    complete = (
        batch.enumeration_complete
        and batch.decode_complete
        and batch.reached_previous_frontier
    )
    blocked_by_prior_gap = bool(current and current.has_open_gap and not prior_gaps_resolved)
    if complete and not blocked_by_prior_gap:
        if batch.contiguous_durable_cursor is None and (
            expected_cursor is not None or batch.newest_observed_cursor is not None
        ):
            raise ValueError("complete batch requires contiguous_durable_cursor")
        frontier = SourceFrontier(
            source_key=batch.source_key,
            durable_cursor=batch.contiguous_durable_cursor,
            resume_cursor=None,
            latest_observed_cursor=batch.newest_observed_cursor,
            has_open_gap=False,
            version=expected_version + 1,
            updated_at=batch.observed_at,
        )
        return FrontierDecision(frontier, False, None, None, None)

    if complete:
        frontier = SourceFrontier(
            source_key=batch.source_key,
            durable_cursor=expected_cursor,
            resume_cursor=current.resume_cursor if current is not None else batch.resume_cursor,
            latest_observed_cursor=batch.newest_observed_cursor,
            has_open_gap=True,
            version=expected_version + 1,
            updated_at=batch.observed_at,
        )
        return FrontierDecision(frontier, False, None, None, None)

    if batch.gap_kind is None:
        raise ValueError("incomplete batch requires gap_kind")
    if not batch.gap_detail or not batch.gap_detail.strip():
        raise ValueError("incomplete batch requires gap_detail")
    if batch.resume_cursor is None:
        raise ValueError("incomplete batch requires resume_cursor")
    resume_cursor = (
        current.resume_cursor
        if (
            current is not None
            and current.has_open_gap
            and current.resume_cursor is not None
            and not prior_gaps_resolved
        )
        else batch.resume_cursor
    )
    frontier = SourceFrontier(
        source_key=batch.source_key,
        durable_cursor=expected_cursor,
        resume_cursor=resume_cursor,
        latest_observed_cursor=batch.newest_observed_cursor,
        has_open_gap=True,
        version=expected_version + 1,
        updated_at=batch.observed_at,
    )
    return FrontierDecision(
        frontier=frontier,
        open_gap=True,
        gap_id=_gap_identity(batch),
        gap_kind=batch.gap_kind,
        gap_detail=batch.gap_detail.strip(),
    )


def load_frontier(conn: sqlite3.Connection, source_key: str) -> SourceFrontier | None:
    row = conn.execute(
        "SELECT source_key, durable_cursor, resume_cursor, latest_observed_cursor, "
        "has_open_gap, version, updated_at FROM op_source_frontiers WHERE source_key=?",
        (_required(source_key, "source_key"),),
    ).fetchone()
    if row is None:
        return None
    return SourceFrontier(
        source_key=row["source_key"],
        durable_cursor=row["durable_cursor"],
        resume_cursor=row["resume_cursor"],
        latest_observed_cursor=row["latest_observed_cursor"],
        has_open_gap=bool(row["has_open_gap"]),
        version=row["version"],
        updated_at=row["updated_at"],
    )


def list_open_gaps(conn: sqlite3.Connection, source_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM op_data_gaps WHERE source_key=? AND resolved_at IS NULL "
        "ORDER BY opened_at, gap_id",
        (_required(source_key, "source_key"),),
    ).fetchall()


def _persist_frontier(
    conn: sqlite3.Connection,
    current: SourceFrontier | None,
    frontier: SourceFrontier,
) -> None:
    if current is None:
        conn.execute(
            "INSERT INTO op_source_frontiers "
            "(source_key,durable_cursor,resume_cursor,latest_observed_cursor,has_open_gap,version,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                frontier.source_key,
                frontier.durable_cursor,
                frontier.resume_cursor,
                frontier.latest_observed_cursor,
                int(frontier.has_open_gap),
                frontier.version,
                frontier.updated_at,
            ),
        )
        return
    cursor = conn.execute(
        "UPDATE op_source_frontiers SET durable_cursor=?, resume_cursor=?, "
        "latest_observed_cursor=?, has_open_gap=?, version=?, updated_at=? "
        "WHERE source_key=? AND version=?",
        (
            frontier.durable_cursor,
            frontier.resume_cursor,
            frontier.latest_observed_cursor,
            int(frontier.has_open_gap),
            frontier.version,
            frontier.updated_at,
            frontier.source_key,
            current.version,
        ),
    )
    if cursor.rowcount != 1:
        raise FrontierConflict("frontier version changed during write")


def _persist_gap(conn: sqlite3.Connection, batch: FrontierBatch, decision: FrontierDecision) -> None:
    values = {
        "gap_id": decision.gap_id,
        "source_key": batch.source_key,
        "gap_kind": decision.gap_kind.value if decision.gap_kind is not None else None,
        "previous_durable_cursor": batch.previous_durable_cursor,
        "resume_cursor": batch.resume_cursor,
        "newest_observed_cursor": batch.newest_observed_cursor,
        "detail": decision.gap_detail,
        "opened_at": batch.observed_at,
    }
    fingerprint = content_fingerprint(values)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO op_data_gaps "
        "(gap_id,source_key,gap_kind,previous_durable_cursor,resume_cursor,newest_observed_cursor,"
        "detail,opened_at,resolved_at,row_fingerprint) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
        (*values.values(), fingerprint),
    )
    if cursor.rowcount == 1:
        return
    row = conn.execute(
        "SELECT row_fingerprint FROM op_data_gaps WHERE gap_id=?", (decision.gap_id,)
    ).fetchone()
    if row is None or row[0] != fingerprint:
        raise ImmutableConflict(f"op_data_gaps.gap_id={decision.gap_id!r} conflicts")


def apply_frontier_batch(conn: sqlite3.Connection, batch: FrontierBatch) -> SourceFrontier:
    with transaction(conn):
        current = load_frontier(conn, batch.source_key)
        open_gaps = list_open_gaps(conn, batch.source_key) if current is not None else []
        open_gap_ids = {row["gap_id"] for row in open_gaps}
        resolved_ids = set(batch.resolved_gap_ids)
        unknown_resolutions = resolved_ids - open_gap_ids
        if unknown_resolutions:
            raise FrontierConflict(
                f"resolved_gap_ids are not open for source: {sorted(unknown_resolutions)!r}"
            )
        prior_gaps_resolved = bool(
            current
            and current.has_open_gap
            and (not open_gap_ids or open_gap_ids <= resolved_ids)
        )
        decision = decide_frontier(
            current,
            batch,
            prior_gaps_resolved=prior_gaps_resolved,
        )
        durable_cursor = decision.frontier.durable_cursor
        if durable_cursor is not None and conn.execute(
            "SELECT 1 FROM op_raw_events WHERE source_key=? AND source_cursor=?",
            (batch.source_key, durable_cursor),
        ).fetchone() is None:
            raise ValueError(
                f"durable cursor {durable_cursor!r} is absent from source raw ledger"
            )
        for gap_id in batch.resolved_gap_ids:
            conn.execute(
                "UPDATE op_data_gaps SET resolved_at=? WHERE gap_id=? AND resolved_at IS NULL",
                (batch.observed_at, gap_id),
            )
        _persist_frontier(conn, current, decision.frontier)
        open_positions = {
            (row["gap_kind"], row["previous_durable_cursor"], row["resume_cursor"])
            for row in open_gaps
        }
        if decision.open_gap and _gap_position(batch) not in open_positions:
            _persist_gap(conn, batch, decision)
        return decision.frontier


def resolve_gap(conn: sqlite3.Connection, gap_id: str, *, resolved_at: str) -> None:
    normalized_id = _required(gap_id, "gap_id")
    normalized_at = require_utc_iso(resolved_at)
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE op_data_gaps SET resolved_at=? WHERE gap_id=? AND resolved_at IS NULL",
            (normalized_at, normalized_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"gap_id is missing or already resolved: {normalized_id}")


def frontier_gap_is_discardable(
    conn: sqlite3.Connection,
    source_key: str,
) -> bool:
    """Return whether a durable gap satisfies every destructive rebase precondition."""
    source = _required(source_key, "source_key")
    current = load_frontier(conn, source)
    if current is None or not current.has_open_gap:
        return False
    open_gaps = list_open_gaps(conn, source)
    if not open_gaps or any(
        row["gap_kind"] == GapKind.DECODE_FAILURE.value for row in open_gaps
    ):
        return False
    target_cursor = current.latest_observed_cursor or current.durable_cursor
    if target_cursor is None:
        return False
    return conn.execute(
        "SELECT 1 FROM op_raw_events WHERE source_key=? AND source_cursor=?",
        (source, target_cursor),
    ).fetchone() is not None


def rebase_frontier_after_discard(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    observed_at: str,
    detail: str,
) -> SourceFrontier:
    """Start a clean current epoch after quarantining an unrecoverable span."""
    source = _required(source_key, "source_key")
    at = require_utc_iso(observed_at)
    reason = _required(detail, "detail")
    with transaction(conn):
        current = load_frontier(conn, source)
        if current is None or not current.has_open_gap:
            raise ValueError("source must have an open gap before rebasing")
        open_gaps = list_open_gaps(conn, source)
        if any(row["gap_kind"] == GapKind.DECODE_FAILURE.value for row in open_gaps):
            raise ValueError("decode failure cannot be discarded by source rebasing")
        target_cursor = current.latest_observed_cursor or current.durable_cursor
        if target_cursor is None:
            raise ValueError("cannot rebase without an observed source cursor")
        if conn.execute(
            "SELECT 1 FROM op_raw_events WHERE source_key=? AND source_cursor=?",
            (source, target_cursor),
        ).fetchone() is None:
            raise ValueError("rebase cursor is absent from source raw ledger")
        conn.execute(
            "UPDATE op_data_gaps SET resolved_at=? "
            "WHERE source_key=? AND resolved_at IS NULL",
            (at, source),
        )
        audit_values = {
            "source_key": source,
            "gap_kind": GapKind.RECOVERY_DISCARDED.value,
            "previous_durable_cursor": current.durable_cursor,
            "resume_cursor": current.resume_cursor,
            "newest_observed_cursor": target_cursor,
            "detail": reason,
            "opened_at": at,
        }
        gap_id = content_fingerprint(audit_values)
        conn.execute(
            "INSERT INTO op_data_gaps "
            "(gap_id,source_key,gap_kind,previous_durable_cursor,resume_cursor,"
            "newest_observed_cursor,detail,opened_at,resolved_at,row_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gap_id, *audit_values.values(), at, content_fingerprint(audit_values)),
        )
        rebased = SourceFrontier(
            source_key=source,
            durable_cursor=target_cursor,
            resume_cursor=None,
            latest_observed_cursor=target_cursor,
            has_open_gap=False,
            version=current.version + 1,
            updated_at=at,
        )
        _persist_frontier(conn, current, rebased)
        return rebased
