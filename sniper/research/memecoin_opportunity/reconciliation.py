"""Append-only confirmation and orphan reconciliation for tentative events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .identity import canonical_json, content_fingerprint, require_utc_iso
from .landmarks import LandmarkState, LandmarkStatus, append_landmark
from .outcomes import (
    CounterfactualEntry,
    ExecutableObservation,
    ExitObservationKind,
    append_competing_outcome,
    append_executable_observation,
    evaluate_competing_outcome,
    load_executable_observations,
)
from .store import append_canonical_event, append_raw_event
from .types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)


RECONCILER_VERSION = "confirmation-reconciler-v1"


@dataclass(frozen=True)
class ReconciliationResult:
    correction_event_id: str
    successor_landmark_ids: tuple[str, ...]


def _append_successor_landmarks(
    conn,
    *,
    canonical_event_id: str,
    correction_id: str,
    target: ConfirmationState,
    reconciled_at: str,
) -> tuple[str, ...]:
    status = (
        LandmarkStatus.ORPHANED
        if target is ConfirmationState.ORPHANED
        else LandmarkStatus.CANONICAL
    )
    successor_ids = []
    for prior in conn.execute(
        "SELECT * FROM op_landmark_states WHERE observed_event_id=? AND status='tentative' "
        "ORDER BY landmark_id",
        (canonical_event_id,),
    ).fetchall():
        successor_id = content_fingerprint(
            {"prior_landmark_id": prior["landmark_id"], "correction_event_id": correction_id, "status": status.value}
        )
        append_landmark(
            conn,
            LandmarkState(
                landmark_id=successor_id,
                launch_id=prior["launch_id"],
                observed_event_id=correction_id,
                observed_at=reconciled_at,
                age_ms=prior["age_ms"],
                progress_bps=prior["progress_bps"],
                crossed_time_ms=(),
                crossed_progress_bps=(),
                status=status,
                state_fingerprint=prior["state_fingerprint"],
                supersedes_landmark_id=prior["landmark_id"],
            ),
        )
        successor_ids.append(successor_id)
    return tuple(successor_ids)


def _append_chain_reorg_outcomes(
    conn,
    *,
    canonical_event_id: str,
    correction_id: str,
    reconciled_at: str,
) -> None:
    rows = conn.execute(
        "SELECT ce.* FROM op_counterfactual_entries ce "
        "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
        "WHERE lm.observed_event_id=?",
        (canonical_event_id,),
    ).fetchall()
    for row in rows:
        if conn.execute(
            "SELECT 1 FROM op_competing_outcomes WHERE entry_id=?", (row["entry_id"],)
        ).fetchone() is not None:
            continue
        entry = CounterfactualEntry(
            entry_id=row["entry_id"],
            landmark_id=row["landmark_id"],
            clip_usd_atomic=int(row["clip_usd_atomic"]),
            entry_latency_ms=row["entry_latency_ms"],
            entry_observed_at=row["entry_observed_at"],
            entry_effective_at=row["entry_effective_at"],
            acquired_token_atomic=int(row["acquired_token_atomic"]),
            entry_cost_usd_atomic=int(row["entry_cost_usd_atomic"]),
            entry_price_impact_bps=row["entry_price_impact_bps"],
            quote_provider=row["quote_provider"],
            quote_version=row["quote_version"],
        )
        elapsed_ms = max(
            0,
            int(
                (
                    datetime.fromisoformat(reconciled_at.replace("Z", "+00:00"))
                    - datetime.fromisoformat(entry.entry_effective_at.replace("Z", "+00:00"))
                ).total_seconds()
                * 1000
            ),
        )
        observation = ExecutableObservation(
            observation_id=content_fingerprint(
                {
                    "entry_id": entry.entry_id,
                    "correction_id": correction_id,
                    "kind": "chain_reorg",
                }
            ),
            entry_id=entry.entry_id,
            elapsed_ms=elapsed_ms,
            canonical_state_id=correction_id,
            observed_at=reconciled_at,
            exit_kind=ExitObservationKind.TRANSIENT_FAILURE,
            net_return_bps=None,
            net_proceeds_usd_atomic=None,
            source_healthy=True,
            orphaned=True,
        )
        append_executable_observation(
            conn, observation, provider=RECONCILER_VERSION, blocker_code="CHAIN_REORG"
        )
        append_competing_outcome(
            conn,
            evaluate_competing_outcome(
                entry, load_executable_observations(conn, entry.entry_id)
            ),
        )


def reconcile_confirmation(
    conn,
    *,
    canonical_event_id: str,
    confirmation: ConfirmationState,
    reconciled_at: str,
) -> ReconciliationResult:
    target = ConfirmationState(confirmation)
    if target not in (
        ConfirmationState.CONFIRMED,
        ConfirmationState.FINALIZED,
        ConfirmationState.ORPHANED,
    ):
        raise ValueError("reconciliation target must be confirmed, finalized, or orphaned")
    at = require_utc_iso(reconciled_at)
    original = conn.execute(
        "SELECT ce.*,re.source_key,re.source_cursor FROM op_canonical_events ce "
        "JOIN op_raw_events re USING(raw_event_id) WHERE ce.canonical_event_id=?",
        (canonical_event_id,),
    ).fetchone()
    if original is None:
        raise ValueError("canonical_event_id does not exist")
    if original["confirmation"] != ConfirmationState.TENTATIVE.value:
        raise ValueError("only tentative events require reconciliation")
    existing = conn.execute(
        "SELECT canonical_event_id,confirmation,state_applied_at FROM op_canonical_events "
        "WHERE correction_of=? AND decoder_version=?",
        (canonical_event_id, RECONCILER_VERSION),
    ).fetchall()
    if existing:
        matching = [row for row in existing if row["confirmation"] == target.value]
        if matching:
            correction_id = matching[0]["canonical_event_id"]
            successor_ids = _append_successor_landmarks(
                conn,
                canonical_event_id=canonical_event_id,
                correction_id=correction_id,
                target=target,
                reconciled_at=matching[0]["state_applied_at"],
            )
            if target is ConfirmationState.ORPHANED:
                _append_chain_reorg_outcomes(
                    conn,
                    canonical_event_id=canonical_event_id,
                    correction_id=correction_id,
                    reconciled_at=matching[0]["state_applied_at"],
                )
            return ReconciliationResult(correction_id, successor_ids)
        terminal = {
            row["confirmation"]
            for row in existing
            if row["confirmation"] in (
                ConfirmationState.FINALIZED.value,
                ConfirmationState.ORPHANED.value,
            )
        }
        if terminal or target is ConfirmationState.CONFIRMED:
            raise ValueError("tentative event already has a conflicting reconciliation")

    payload_json = canonical_json(
        {"confirmation_of": canonical_event_id, "confirmation": target.value}
    )
    raw_id = content_fingerprint(
        {"confirmation_of": canonical_event_id, "confirmation": target.value, "version": RECONCILER_VERSION}
    )
    network = NetworkId(original["network_id"])
    venue = VenueId(original["venue_id"])
    order = ChainOrder(
        original["block_or_slot"],
        original["transaction_index"],
        original["event_index"],
        original["block_hash"],
    )
    append_raw_event(
        conn,
        RawEvent(
            raw_event_id=raw_id,
            network_id=network,
            venue_id=venue,
            source_key=original["source_key"],
            source_provider=RECONCILER_VERSION,
            source_cursor=f"{original['source_cursor']}:confirmation:{target.value}",
            token_id=original["token_id"],
            transaction_id=original["transaction_id"],
            chain_order=order,
            chain_timestamp=original["chain_timestamp"],
            received_at=at,
            confirmation=target,
            raw_payload_json=payload_json,
            raw_payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
        ),
    )
    correction_id = content_fingerprint(
        {"raw_event_id": raw_id, "correction_of": canonical_event_id, "version": RECONCILER_VERSION}
    )
    append_canonical_event(
        conn,
        CanonicalEvent(
            canonical_event_id=correction_id,
            raw_event_id=raw_id,
            launch_id=original["launch_id"],
            network_id=network,
            venue_id=venue,
            token_id=original["token_id"],
            lifecycle=LifecycleState(original["lifecycle"]),
            event_kind=EventKind.CORRECTION,
            transaction_id=original["transaction_id"],
            chain_order=order,
            chain_timestamp=original["chain_timestamp"],
            received_at=at,
            decoded_at=at,
            state_applied_at=at,
            confirmation=target,
            actor_id=None,
            quote_delta_atomic=None,
            token_delta_atomic=None,
            progress_bps=original["progress_bps"],
            creator_linked=None,
            decoder_version=RECONCILER_VERSION,
            correction_of=canonical_event_id,
        ),
    )
    successor_ids = _append_successor_landmarks(
        conn,
        canonical_event_id=canonical_event_id,
        correction_id=correction_id,
        target=target,
        reconciled_at=at,
    )
    if target is ConfirmationState.ORPHANED:
        _append_chain_reorg_outcomes(
            conn,
            canonical_event_id=canonical_event_id,
            correction_id=correction_id,
            reconciled_at=at,
        )
    return ReconciliationResult(correction_id, successor_ids)
