"""Persist executable counterfactual paths and finalize competing outcomes."""

from __future__ import annotations

from datetime import datetime

from .curve_math import constant_product_exit_quote, quote_atomic_for_usd
from .directional import EXTENDED_HORIZON_RULES
from .identity import content_fingerprint, require_utc_iso
from .outcomes import (
    CounterfactualEntry,
    ExecutableObservation,
    ExitObservationKind,
    OutcomeKind,
    append_competing_outcome,
    append_executable_observation,
    evaluate_competing_outcome,
    list_outcome_ready_entries,
    load_executable_observations,
)
from .state import LaunchState, launch_state_from_json


PATH_EVALUATOR_VERSION = "opportunity-path-v4"
EXTENDED_CANDIDATE_HORIZON_MS = 300_000
_EXTENDED_RULES = EXTENDED_HORIZON_RULES


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def _entry(row) -> CounterfactualEntry:
    return CounterfactualEntry(
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


def _entry_quote_in(conn, entry: CounterfactualEntry) -> int | None:
    row = conn.execute(
        "SELECT q.quote_in_atomic FROM op_landmark_states lm "
        "JOIN op_state_snapshots s ON s.launch_id=lm.launch_id "
        "AND s.observed_event_id=lm.observed_event_id "
        "JOIN op_quote_observations q ON q.snapshot_id=s.snapshot_id "
        "AND q.clip_usd_micros=? AND q.is_remote=0 "
        "WHERE lm.landmark_id=? ORDER BY q.observed_at DESC LIMIT 1",
        (entry.clip_usd_atomic, entry.landmark_id),
    ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _terminal_or_pending(entry, observations, *, horizon_ms: int):
    outcome = evaluate_competing_outcome(entry, observations, horizon_ms=horizon_ms)
    latest = observations[-1]
    if (
        outcome.outcome_kind is OutcomeKind.CENSORED_DATA_GAP
        and latest.source_healthy
        and latest.elapsed_ms < horizon_ms
    ):
        return None
    return outcome


def record_launch_outcomes(
    conn,
    *,
    state: LaunchState,
    canonical_state_id: str,
    observed_at: str,
    quote_usd_price_micros: int | None,
    quote_decimals: int | None,
    fee_bps: int | None,
    network_cost_quote_atomic: int = 0,
    source_healthy: bool = True,
    horizon_ms: int = 60_000,
    only_due: bool = False,
) -> tuple[str, ...]:
    """Record one current-state exit observation for each unresolved entry.

    The fixed-world replay overlays the modeled entry reserve changes on every later
    observed state. Missing math or source gaps censor instead of inventing returns.
    """
    observed = require_utc_iso(observed_at)
    completed: list[str] = []
    for row in list_outcome_ready_entries(conn, state.launch_id):
        entry = _entry(row)
        primary_outcome_exists = conn.execute(
            "SELECT 1 FROM op_competing_outcomes WHERE entry_id=?",
            (entry.entry_id,),
        ).fetchone() is not None
        extended_candidate = conn.execute(
            "SELECT 1 FROM op_forward_alert_entries link "
            "JOIN op_forward_alerts alert USING(alert_id) "
            f"WHERE link.entry_id=? AND alert.rule_version IN ({','.join('?' for _ in _EXTENDED_RULES)})",
            (entry.entry_id, *_EXTENDED_RULES),
        ).fetchone() is not None
        existing_observations = load_executable_observations(conn, entry.entry_id)
        extended_complete = any(
            observation.elapsed_ms >= EXTENDED_CANDIDATE_HORIZON_MS
            for observation in existing_observations
        )
        if primary_outcome_exists and (not extended_candidate or extended_complete):
            continue
        if any(
            observation.canonical_state_id == canonical_state_id
            for observation in existing_observations
        ):
            if not primary_outcome_exists:
                outcome = _terminal_or_pending(
                    entry,
                    existing_observations,
                    horizon_ms=horizon_ms,
                )
                if outcome is not None:
                    append_competing_outcome(conn, outcome)
                    completed.append(entry.entry_id)
            continue
        elapsed_ms = int((_parse(observed) - _parse(entry.entry_effective_at)).total_seconds() * 1000)
        due_ms = (
            EXTENDED_CANDIDATE_HORIZON_MS
            if primary_outcome_exists and extended_candidate
            else horizon_ms
        )
        if elapsed_ms < 0 or (only_due and elapsed_ms < due_ms):
            continue
        # A quiet-launch sweep can arrive after the exact policy deadline. Its
        # state is the last finalized state known at that deadline, so record the
        # receipt at the policy boundary instead of fabricating a late timeout.
        if only_due:
            elapsed_ms = due_ms
        quote_in = _entry_quote_in(conn, entry)
        if quote_in is None and quote_usd_price_micros is not None and quote_decimals is not None:
            quote_in = quote_atomic_for_usd(
                entry.clip_usd_atomic,
                quote_usd_price_micros,
                quote_decimals,
            )
        can_quote = all(
            value is not None
            for value in (
                quote_in,
                quote_usd_price_micros,
                quote_decimals,
                fee_bps,
                state.virtual_token_reserves,
                state.virtual_quote_reserves,
            )
        )
        migration = state.lifecycle.value == "migrated"
        healthy = bool(source_healthy and (can_quote or migration))
        blocker = None
        proceeds_usd = None
        return_bps = None
        kind = ExitObservationKind.TRANSIENT_FAILURE
        if can_quote and not migration:
            quote = constant_product_exit_quote(
                current_virtual_token_reserves=int(state.virtual_token_reserves),
                current_virtual_quote_reserves=int(state.virtual_quote_reserves),
                acquired_token_atomic=entry.acquired_token_atomic,
                hypothetical_quote_in_atomic=int(quote_in),
                fee_bps=int(fee_bps),
                network_cost_quote_atomic=network_cost_quote_atomic,
            )
            if quote.executable:
                proceeds_quote = quote.net_quote_out_atomic - quote.network_cost_quote_atomic
                proceeds_usd = max(0, proceeds_quote) * int(quote_usd_price_micros) // (10 ** int(quote_decimals))
                return_bps = (proceeds_usd - entry.entry_cost_usd_atomic) * 10_000 // entry.entry_cost_usd_atomic
                kind = ExitObservationKind.EXECUTABLE
            else:
                kind = ExitObservationKind.DETERMINISTIC_FAILURE
                blocker = quote.blocker
        elif not source_healthy:
            blocker = "DATA_GAP"
        elif not migration:
            blocker = "NET_PNL_INCOMPLETE"
        observation_id = content_fingerprint(
            {
                "entry_id": entry.entry_id,
                "canonical_state_id": canonical_state_id,
                "observed_at": observed,
                "evaluator": PATH_EVALUATOR_VERSION,
            }
        )
        observation = ExecutableObservation(
            observation_id=observation_id,
            entry_id=entry.entry_id,
            elapsed_ms=elapsed_ms,
            canonical_state_id=canonical_state_id,
            observed_at=observed,
            exit_kind=kind,
            net_return_bps=return_bps,
            net_proceeds_usd_atomic=proceeds_usd,
            source_healthy=healthy,
            migration=migration,
        )
        append_executable_observation(
            conn,
            observation,
            provider=PATH_EVALUATOR_VERSION,
            blocker_code=blocker,
        )
        observations = load_executable_observations(conn, entry.entry_id)
        if primary_outcome_exists:
            continue
        outcome = _terminal_or_pending(entry, observations, horizon_ms=horizon_ms)
        if outcome is not None:
            append_competing_outcome(conn, outcome)
            completed.append(entry.entry_id)
    return tuple(completed)


def backfill_finalized_outcomes(
    conn,
    *,
    launch_id: str,
    quote_usd_price_micros: int | None,
    quote_decimals: int | None,
    fee_bps: int | None,
    network_cost_quote_atomic: int = 0,
    source_healthy: bool = True,
    limit: int = 512,
) -> tuple[str, ...]:
    """Replay newly finalized snapshots through unresolved point-in-time paths."""
    rows = conn.execute(
        "SELECT snapshot.snapshot_id,snapshot.observed_at,snapshot.state_json "
        "FROM op_state_snapshots snapshot "
        "JOIN op_canonical_events event "
        "ON event.canonical_event_id=snapshot.observed_event_id "
        "WHERE snapshot.launch_id=? AND (event.confirmation='finalized' OR EXISTS ("
        "SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=event.canonical_event_id "
        "AND correction.confirmation='finalized')) AND EXISTS ("
        "SELECT 1 FROM op_counterfactual_entries entry "
        "JOIN op_landmark_states landmark USING(landmark_id) "
        "WHERE landmark.launch_id=snapshot.launch_id "
        "AND entry.entry_effective_at<=snapshot.observed_at "
        "AND NOT EXISTS (SELECT 1 FROM op_executable_observations observation "
        "WHERE observation.entry_id=entry.entry_id "
        "AND observation.canonical_state_id=snapshot.snapshot_id) "
        "AND (NOT EXISTS (SELECT 1 FROM op_competing_outcomes outcome "
        "WHERE outcome.entry_id=entry.entry_id) OR (EXISTS ("
        "SELECT 1 FROM op_forward_alert_entries link "
        "JOIN op_forward_alerts alert USING(alert_id) "
        "WHERE link.entry_id=entry.entry_id "
        f"AND alert.rule_version IN ({','.join('?' for _ in _EXTENDED_RULES)})) "
        "AND NOT EXISTS (SELECT 1 FROM op_executable_observations extended "
        "WHERE extended.entry_id=entry.entry_id AND extended.elapsed_ms>=?)))) "
        "ORDER BY event.block_or_slot,COALESCE(event.transaction_index,-1),"
        "event.event_index,snapshot.observed_at,snapshot.inserted_at LIMIT ?",
        (launch_id, *_EXTENDED_RULES, EXTENDED_CANDIDATE_HORIZON_MS, int(limit)),
    ).fetchall()
    completed: list[str] = []
    for row in rows:
        state = launch_state_from_json(row["state_json"])
        completed.extend(
            record_launch_outcomes(
                conn,
                state=state,
                canonical_state_id=row["snapshot_id"],
                observed_at=row["observed_at"],
                quote_usd_price_micros=quote_usd_price_micros,
                quote_decimals=quote_decimals,
                fee_bps=fee_bps,
                network_cost_quote_atomic=network_cost_quote_atomic,
                source_healthy=source_healthy,
            )
        )
    return tuple(completed)


def active_forward_launch_ids(conn) -> tuple[str, ...]:
    """Return only launches whose immutable forward path still needs evidence."""
    return tuple(
        row["launch_id"]
        for row in conn.execute(
            "SELECT DISTINCT alert.launch_id FROM op_forward_alerts alert "
            "JOIN op_forward_alert_entries link USING(alert_id) "
            "WHERE NOT EXISTS (SELECT 1 FROM op_competing_outcomes outcome "
            "WHERE outcome.entry_id=link.entry_id) OR (alert.rule_version IN ("
            + ",".join("?" for _ in _EXTENDED_RULES)
            + ") AND NOT EXISTS ("
            "SELECT 1 FROM op_executable_observations observation "
            "WHERE observation.entry_id=link.entry_id AND observation.elapsed_ms>=?)) "
            "ORDER BY alert.launch_id",
            (*_EXTENDED_RULES, EXTENDED_CANDIDATE_HORIZON_MS),
        ).fetchall()
    )
