"""Deterministic point-in-time landmark crossing and persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from .identity import canonical_json, content_fingerprint, require_utc_iso
from .store import _insert_immutable, transaction
from .types import ConfirmationState


class OutOfOrderObservation(ValueError):
    """A tentative event arrived behind the already-applied launch state."""


TIME_LANDMARKS_MS = (1_000, 2_000, 5_000, 10_000, 20_000, 30_000, 60_000)
PROGRESS_LANDMARKS_BPS = (200, 500, 1_000, 2_000, 4_000, 6_000, 8_000)


class LandmarkStatus(str, Enum):
    TENTATIVE = "tentative"
    CANONICAL = "canonical"
    ORPHANED = "orphaned"
    INELIGIBLE = "ineligible"


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _non_negative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class LaunchSnapshot:
    launch_id: str
    observed_event_id: str
    observed_at: str
    age_ms: int
    progress_bps: int
    confirmation: ConfirmationState
    state_fingerprint: str
    source_gap_open: bool
    supersedes_landmark_id: str | None = None
    ineligible_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("launch_id", "observed_event_id", "state_fingerprint"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "observed_at", require_utc_iso(self.observed_at))
        object.__setattr__(self, "age_ms", _non_negative_int(self.age_ms, "age_ms"))
        if (
            isinstance(self.progress_bps, bool)
            or not isinstance(self.progress_bps, int)
            or not 0 <= self.progress_bps <= 10_000
        ):
            raise ValueError("progress_bps must be an integer from 0 through 10000")
        object.__setattr__(self, "confirmation", ConfirmationState(self.confirmation))
        if not isinstance(self.source_gap_open, bool):
            raise ValueError("source_gap_open must be boolean")
        if self.supersedes_landmark_id is not None:
            object.__setattr__(
                self,
                "supersedes_landmark_id",
                _required(self.supersedes_landmark_id, "supersedes_landmark_id"),
            )
        if self.ineligible_reason is not None:
            object.__setattr__(
                self,
                "ineligible_reason",
                _required(self.ineligible_reason, "ineligible_reason"),
            )


@dataclass(frozen=True)
class LandmarkState:
    landmark_id: str
    launch_id: str
    observed_event_id: str
    observed_at: str
    age_ms: int
    progress_bps: int
    crossed_time_ms: tuple[int, ...]
    crossed_progress_bps: tuple[int, ...]
    status: LandmarkStatus
    state_fingerprint: str
    supersedes_landmark_id: str | None = None
    ineligible_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "landmark_id",
            "launch_id",
            "observed_event_id",
            "state_fingerprint",
        ):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "observed_at", require_utc_iso(self.observed_at))
        object.__setattr__(self, "age_ms", _non_negative_int(self.age_ms, "age_ms"))
        if (
            isinstance(self.progress_bps, bool)
            or not isinstance(self.progress_bps, int)
            or not 0 <= self.progress_bps <= 10_000
        ):
            raise ValueError("progress_bps must be an integer from 0 through 10000")
        time_tags = tuple(self.crossed_time_ms)
        progress_tags = tuple(self.crossed_progress_bps)
        if time_tags != tuple(sorted(set(time_tags))) or any(
            value not in TIME_LANDMARKS_MS for value in time_tags
        ):
            raise ValueError("crossed_time_ms must contain unique ordered registered thresholds")
        if progress_tags != tuple(sorted(set(progress_tags))) or any(
            value not in PROGRESS_LANDMARKS_BPS for value in progress_tags
        ):
            raise ValueError(
                "crossed_progress_bps must contain unique ordered registered thresholds"
            )
        object.__setattr__(self, "crossed_time_ms", time_tags)
        object.__setattr__(self, "crossed_progress_bps", progress_tags)
        object.__setattr__(self, "status", LandmarkStatus(self.status))
        if self.supersedes_landmark_id is not None:
            object.__setattr__(
                self,
                "supersedes_landmark_id",
                _required(self.supersedes_landmark_id, "supersedes_landmark_id"),
            )
        if not time_tags and not progress_tags and self.supersedes_landmark_id is None:
            raise ValueError("landmark state needs a crossed threshold or superseded state")
        if self.status == LandmarkStatus.INELIGIBLE:
            object.__setattr__(
                self,
                "ineligible_reason",
                _required(self.ineligible_reason, "ineligible_reason"),
            )
        elif self.ineligible_reason is not None:
            raise ValueError("ineligible_reason is only valid for ineligible landmarks")


def crossed_landmarks(
    previous: LaunchSnapshot | None,
    current: LaunchSnapshot,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if previous is not None:
        if previous.launch_id != current.launch_id:
            raise ValueError("previous and current launch_id must match")
        if current.age_ms < previous.age_ms:
            raise ValueError("age_ms cannot regress")
        if (
            current.progress_bps < previous.progress_bps
            and current.confirmation != ConfirmationState.ORPHANED
        ):
            raise OutOfOrderObservation(
                "progress_bps cannot regress without reorg correction"
            )
    prior_age = previous.age_ms if previous is not None else -1
    prior_progress = previous.progress_bps if previous is not None else -1
    time_tags = tuple(
        threshold
        for threshold in TIME_LANDMARKS_MS
        if prior_age < threshold <= current.age_ms
    )
    progress_tags = tuple(
        threshold
        for threshold in PROGRESS_LANDMARKS_BPS
        if prior_progress < threshold <= current.progress_bps
    )
    return time_tags, progress_tags


def _status(snapshot: LaunchSnapshot) -> tuple[LandmarkStatus, str | None]:
    if snapshot.confirmation == ConfirmationState.ORPHANED:
        return LandmarkStatus.ORPHANED, None
    if snapshot.source_gap_open or snapshot.ineligible_reason is not None:
        return (
            LandmarkStatus.INELIGIBLE,
            snapshot.ineligible_reason or "source_gap_open",
        )
    if snapshot.confirmation == ConfirmationState.TENTATIVE:
        return LandmarkStatus.TENTATIVE, None
    return LandmarkStatus.CANONICAL, None


def materialize_landmarks(
    previous: LaunchSnapshot | None,
    current: LaunchSnapshot,
) -> LandmarkState | None:
    time_tags, progress_tags = crossed_landmarks(previous, current)
    if not time_tags and not progress_tags and current.supersedes_landmark_id is None:
        return None
    status, reason = _status(current)
    identity = {
        "launch_id": current.launch_id,
        "observed_event_id": current.observed_event_id,
        "crossed_time_ms": time_tags,
        "crossed_progress_bps": progress_tags,
        "status": status,
        "state_fingerprint": current.state_fingerprint,
        "supersedes_landmark_id": current.supersedes_landmark_id,
    }
    return LandmarkState(
        landmark_id=content_fingerprint(identity),
        launch_id=current.launch_id,
        observed_event_id=current.observed_event_id,
        observed_at=current.observed_at,
        age_ms=current.age_ms,
        progress_bps=current.progress_bps,
        crossed_time_ms=time_tags,
        crossed_progress_bps=progress_tags,
        status=status,
        state_fingerprint=current.state_fingerprint,
        supersedes_landmark_id=current.supersedes_landmark_id,
        ineligible_reason=reason,
    )


def append_landmark(conn: sqlite3.Connection, landmark: LandmarkState) -> bool:
    values = {
        "landmark_id": landmark.landmark_id,
        "launch_id": landmark.launch_id,
        "observed_event_id": landmark.observed_event_id,
        "observed_at": landmark.observed_at,
        "age_ms": landmark.age_ms,
        "progress_bps": landmark.progress_bps,
        "crossed_time_ms_json": canonical_json(landmark.crossed_time_ms),
        "crossed_progress_bps_json": canonical_json(landmark.crossed_progress_bps),
        "status": landmark.status.value,
        "state_fingerprint": landmark.state_fingerprint,
        "supersedes_landmark_id": landmark.supersedes_landmark_id,
        "ineligible_reason": landmark.ineligible_reason,
    }
    with transaction(conn):
        event = conn.execute(
            "SELECT launch_id, confirmation FROM op_canonical_events WHERE canonical_event_id=?",
            (landmark.observed_event_id,),
        ).fetchone()
        if event is None or event["launch_id"] != landmark.launch_id:
            raise ValueError("observed_event_id does not belong to landmark launch")
        allowed_confirmations = {
            LandmarkStatus.TENTATIVE: {ConfirmationState.TENTATIVE.value},
            LandmarkStatus.CANONICAL: {
                ConfirmationState.CONFIRMED.value,
                ConfirmationState.FINALIZED.value,
            },
            LandmarkStatus.ORPHANED: {ConfirmationState.ORPHANED.value},
            LandmarkStatus.INELIGIBLE: {
                ConfirmationState.TENTATIVE.value,
                ConfirmationState.CONFIRMED.value,
                ConfirmationState.FINALIZED.value,
            },
        }
        if event["confirmation"] not in allowed_confirmations[landmark.status]:
            raise ValueError("landmark status is incompatible with observed event confirmation")
        if landmark.supersedes_landmark_id is not None:
            prior = conn.execute(
                "SELECT launch_id, status FROM op_landmark_states WHERE landmark_id=?",
                (landmark.supersedes_landmark_id,),
            ).fetchone()
            if prior is None or prior["launch_id"] != landmark.launch_id:
                raise ValueError("supersedes_landmark_id does not belong to landmark launch")
            if prior["status"] != LandmarkStatus.TENTATIVE.value:
                raise ValueError("only a tentative landmark can be superseded")
        return _insert_immutable(
            conn,
            table="op_landmark_states",
            identity_column="landmark_id",
            identity_value=landmark.landmark_id,
            values=values,
        )


def list_entry_eligible_landmarks(
    conn: sqlite3.Connection,
    launch_id: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT lm.* FROM op_landmark_states lm "
        "JOIN op_canonical_events ce ON ce.canonical_event_id=lm.observed_event_id "
        "WHERE lm.launch_id=? AND lm.status IN ('tentative','canonical') "
        "AND lm.supersedes_landmark_id IS NULL "
        "ORDER BY ce.block_or_slot, COALESCE(ce.transaction_index,-1), ce.event_index, lm.landmark_id",
        (_required(launch_id, "launch_id"),),
    ).fetchall()
