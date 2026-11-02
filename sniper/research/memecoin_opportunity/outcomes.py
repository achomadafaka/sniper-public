"""Executable, self-impact-aware competing first-passage outcomes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

from .identity import content_fingerprint, require_utc_iso
from .store import _insert_immutable, credit_creator_outcome, transaction


EVALUATOR_VERSION = "opportunity-outcome-v3"


class ExitObservationKind(str, Enum):
    EXECUTABLE = "executable"
    DETERMINISTIC_FAILURE = "deterministic_failure"
    TRANSIENT_FAILURE = "transient_failure"


class OutcomeKind(str, Enum):
    PROFIT_FIRST = "profit_first"
    LOSS_FIRST = "loss_first"
    PERSISTENT_EXIT_FAILURE_FIRST = "persistent_exit_failure_first"
    MIGRATION_FIRST = "migration_first"
    TIMEOUT = "timeout"
    CENSORED_DATA_GAP = "censored_data_gap"
    CHAIN_REORG = "chain_reorg"


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class CounterfactualEntry:
    entry_id: str
    landmark_id: str
    clip_usd_atomic: int
    entry_latency_ms: int
    entry_observed_at: str
    entry_effective_at: str
    acquired_token_atomic: int
    entry_cost_usd_atomic: int
    entry_price_impact_bps: int
    quote_provider: str
    quote_version: str

    def __post_init__(self) -> None:
        for field_name in ("entry_id", "landmark_id", "quote_provider", "quote_version"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        for field_name in ("clip_usd_atomic", "acquired_token_atomic"):
            value = _integer(getattr(self, field_name), field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        cost = _integer(self.entry_cost_usd_atomic, "entry_cost_usd_atomic")
        if cost < 0:
            raise ValueError("entry_cost_usd_atomic must be non-negative")
        latency = _integer(self.entry_latency_ms, "entry_latency_ms")
        if latency < 0:
            raise ValueError("entry_latency_ms must be non-negative")
        _integer(self.entry_price_impact_bps, "entry_price_impact_bps")
        object.__setattr__(
            self,
            "entry_observed_at",
            require_utc_iso(self.entry_observed_at),
        )
        object.__setattr__(
            self,
            "entry_effective_at",
            require_utc_iso(self.entry_effective_at),
        )
        if _parse_timestamp(self.entry_effective_at) < _parse_timestamp(self.entry_observed_at):
            raise ValueError("entry_effective_at must not precede entry_observed_at")


@dataclass(frozen=True)
class ExecutableObservation:
    observation_id: str
    entry_id: str
    elapsed_ms: int
    canonical_state_id: str
    observed_at: str
    exit_kind: ExitObservationKind
    net_return_bps: int | None
    net_proceeds_usd_atomic: int | None
    source_healthy: bool
    migration: bool = False
    orphaned: bool = False

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "entry_id", "canonical_state_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        elapsed = _integer(self.elapsed_ms, "elapsed_ms")
        if elapsed < 0:
            raise ValueError("elapsed_ms must be non-negative")
        object.__setattr__(self, "observed_at", require_utc_iso(self.observed_at))
        kind = ExitObservationKind(self.exit_kind)
        object.__setattr__(self, "exit_kind", kind)
        for field_name in ("source_healthy", "migration", "orphaned"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        if kind == ExitObservationKind.EXECUTABLE:
            if self.net_return_bps is None or self.net_proceeds_usd_atomic is None:
                raise ValueError("executable observation requires net return and proceeds")
            _integer(self.net_return_bps, "net_return_bps")
            proceeds = _integer(self.net_proceeds_usd_atomic, "net_proceeds_usd_atomic")
            if proceeds < 0:
                raise ValueError("net_proceeds_usd_atomic must be non-negative")
        elif self.net_return_bps is not None or self.net_proceeds_usd_atomic is not None:
            raise ValueError("failure observation cannot carry return or proceeds")


@dataclass(frozen=True)
class CompetingOutcome:
    outcome_id: str
    entry_id: str
    outcome_kind: OutcomeKind
    elapsed_ms: int
    terminal_observation_id: str
    terminal_net_return_bps: int | None
    profit_barrier_bps: int
    loss_barrier_bps: int
    horizon_ms: int
    last_executable_observation_id: str | None
    last_executable_return_bps: int | None
    evaluator_version: str = EVALUATOR_VERSION

    def __post_init__(self) -> None:
        for field_name in ("outcome_id", "entry_id", "terminal_observation_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(
            self, "evaluator_version", _required(self.evaluator_version, "evaluator_version")
        )
        object.__setattr__(self, "outcome_kind", OutcomeKind(self.outcome_kind))
        if _integer(self.elapsed_ms, "elapsed_ms") < 0:
            raise ValueError("elapsed_ms must be non-negative")
        if _integer(self.horizon_ms, "horizon_ms") <= 0:
            raise ValueError("horizon_ms must be positive")
        profit = _integer(self.profit_barrier_bps, "profit_barrier_bps")
        loss = _integer(self.loss_barrier_bps, "loss_barrier_bps")
        if profit <= 0 or loss >= 0 or loss >= profit:
            raise ValueError("barriers must satisfy loss < 0 < profit")
        if self.terminal_net_return_bps is not None:
            _integer(self.terminal_net_return_bps, "terminal_net_return_bps")
        if self.last_executable_return_bps is not None:
            _integer(self.last_executable_return_bps, "last_executable_return_bps")
        if (self.last_executable_observation_id is None) != (
            self.last_executable_return_bps is None
        ):
            raise ValueError("last executable observation and return must be present together")


def _make_outcome(
    *,
    entry: CounterfactualEntry,
    kind: OutcomeKind,
    elapsed_ms: int,
    terminal: ExecutableObservation,
    terminal_return: int | None,
    profit_barrier_bps: int,
    loss_barrier_bps: int,
    horizon_ms: int,
    last_executable: ExecutableObservation | None,
) -> CompetingOutcome:
    identity = {
        "entry_id": entry.entry_id,
        "outcome_kind": kind,
        "elapsed_ms": elapsed_ms,
        "terminal_observation_id": terminal.observation_id,
        "terminal_net_return_bps": terminal_return,
        "profit_barrier_bps": profit_barrier_bps,
        "loss_barrier_bps": loss_barrier_bps,
        "horizon_ms": horizon_ms,
        "evaluator_version": EVALUATOR_VERSION,
    }
    return CompetingOutcome(
        outcome_id=content_fingerprint(identity),
        entry_id=entry.entry_id,
        outcome_kind=kind,
        elapsed_ms=elapsed_ms,
        terminal_observation_id=terminal.observation_id,
        terminal_net_return_bps=terminal_return,
        profit_barrier_bps=profit_barrier_bps,
        loss_barrier_bps=loss_barrier_bps,
        horizon_ms=horizon_ms,
        last_executable_observation_id=(
            last_executable.observation_id if last_executable is not None else None
        ),
        last_executable_return_bps=(
            last_executable.net_return_bps if last_executable is not None else None
        ),
        evaluator_version=EVALUATOR_VERSION,
    )


def evaluate_competing_outcome(
    entry: CounterfactualEntry,
    observations: Sequence[ExecutableObservation],
    *,
    profit_barrier_bps: int = 1_200,
    loss_barrier_bps: int = -800,
    horizon_ms: int = 60_000,
    transient_failure_states: int = 2,
) -> CompetingOutcome:
    profit = _integer(profit_barrier_bps, "profit_barrier_bps")
    loss = _integer(loss_barrier_bps, "loss_barrier_bps")
    horizon = _integer(horizon_ms, "horizon_ms")
    failure_states = _integer(transient_failure_states, "transient_failure_states")
    if profit <= 0 or loss >= 0 or loss >= profit:
        raise ValueError("barriers must satisfy loss < 0 < profit")
    if horizon <= 0:
        raise ValueError("horizon_ms must be positive")
    if failure_states < 2:
        raise ValueError("transient_failure_states must be at least 2")
    if not observations:
        raise ValueError("observations must be non-empty")
    ordered = sorted(
        observations,
        key=lambda item: (item.elapsed_ms, item.observed_at, item.canonical_state_id),
    )
    state_ids: set[str] = set()
    for observation in ordered:
        if observation.entry_id != entry.entry_id:
            raise ValueError("observation entry_id does not match entry")
        if observation.canonical_state_id in state_ids:
            raise ValueError("ambiguous duplicate canonical state")
        state_ids.add(observation.canonical_state_id)

    last_executable: ExecutableObservation | None = None
    transient_streak = 0
    for observation in ordered:
        if observation.orphaned:
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.CHAIN_REORG,
                elapsed_ms=observation.elapsed_ms,
                terminal=observation,
                terminal_return=None,
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )
        if not observation.source_healthy:
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.CENSORED_DATA_GAP,
                elapsed_ms=observation.elapsed_ms,
                terminal=observation,
                terminal_return=None,
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )
        if observation.elapsed_ms > horizon:
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.TIMEOUT,
                elapsed_ms=horizon,
                terminal=observation,
                terminal_return=(
                    last_executable.net_return_bps if last_executable is not None else None
                ),
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )
        if observation.migration:
            if observation.exit_kind == ExitObservationKind.EXECUTABLE:
                last_executable = observation
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.MIGRATION_FIRST,
                elapsed_ms=observation.elapsed_ms,
                terminal=observation,
                terminal_return=(
                    last_executable.net_return_bps if last_executable is not None else None
                ),
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )
        if observation.exit_kind == ExitObservationKind.DETERMINISTIC_FAILURE:
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.PERSISTENT_EXIT_FAILURE_FIRST,
                elapsed_ms=observation.elapsed_ms,
                terminal=observation,
                terminal_return=None,
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )
        if observation.exit_kind == ExitObservationKind.TRANSIENT_FAILURE:
            transient_streak += 1
            if transient_streak >= failure_states:
                return _make_outcome(
                    entry=entry,
                    kind=OutcomeKind.PERSISTENT_EXIT_FAILURE_FIRST,
                    elapsed_ms=observation.elapsed_ms,
                    terminal=observation,
                    terminal_return=None,
                    profit_barrier_bps=profit,
                    loss_barrier_bps=loss,
                    horizon_ms=horizon,
                    last_executable=last_executable,
                )
        else:
            transient_streak = 0
            last_executable = observation
            if observation.net_return_bps >= profit:
                return _make_outcome(
                    entry=entry,
                    kind=OutcomeKind.PROFIT_FIRST,
                    elapsed_ms=observation.elapsed_ms,
                    terminal=observation,
                    terminal_return=observation.net_return_bps,
                    profit_barrier_bps=profit,
                    loss_barrier_bps=loss,
                    horizon_ms=horizon,
                    last_executable=last_executable,
                )
            if observation.net_return_bps <= loss:
                return _make_outcome(
                    entry=entry,
                    kind=OutcomeKind.LOSS_FIRST,
                    elapsed_ms=observation.elapsed_ms,
                    terminal=observation,
                    terminal_return=observation.net_return_bps,
                    profit_barrier_bps=profit,
                    loss_barrier_bps=loss,
                    horizon_ms=horizon,
                    last_executable=last_executable,
                )
        if observation.elapsed_ms == horizon:
            return _make_outcome(
                entry=entry,
                kind=OutcomeKind.TIMEOUT,
                elapsed_ms=horizon,
                terminal=observation,
                terminal_return=(
                    last_executable.net_return_bps if last_executable is not None else None
                ),
                profit_barrier_bps=profit,
                loss_barrier_bps=loss,
                horizon_ms=horizon,
                last_executable=last_executable,
            )

    terminal = ordered[-1]
    return _make_outcome(
        entry=entry,
        kind=OutcomeKind.CENSORED_DATA_GAP,
        elapsed_ms=terminal.elapsed_ms,
        terminal=terminal,
        terminal_return=None,
        profit_barrier_bps=profit,
        loss_barrier_bps=loss,
        horizon_ms=horizon,
        last_executable=last_executable,
    )


def append_counterfactual_entry(
    conn: sqlite3.Connection,
    entry: CounterfactualEntry,
) -> bool:
    values = {
        "entry_id": entry.entry_id,
        "landmark_id": entry.landmark_id,
        "clip_usd_atomic": str(entry.clip_usd_atomic),
        "entry_latency_ms": entry.entry_latency_ms,
        "entry_observed_at": entry.entry_observed_at,
        "entry_effective_at": entry.entry_effective_at,
        "acquired_token_atomic": str(entry.acquired_token_atomic),
        "entry_cost_usd_atomic": str(entry.entry_cost_usd_atomic),
        "entry_price_impact_bps": entry.entry_price_impact_bps,
        "quote_provider": entry.quote_provider,
        "quote_version": entry.quote_version,
    }
    with transaction(conn):
        landmark = conn.execute(
            "SELECT status, observed_at FROM op_landmark_states WHERE landmark_id=?",
            (entry.landmark_id,),
        ).fetchone()
        if landmark is None or landmark["status"] not in ("tentative", "canonical"):
            raise ValueError("landmark is not entry-eligible")
        if _parse_timestamp(entry.entry_observed_at) < _parse_timestamp(landmark["observed_at"]):
            raise ValueError("entry_observed_at precedes landmark observation")
        return _insert_immutable(
            conn,
            table="op_counterfactual_entries",
            identity_column="entry_id",
            identity_value=entry.entry_id,
            values=values,
        )


def append_executable_observation(
    conn: sqlite3.Connection,
    observation: ExecutableObservation,
    *,
    provider: str,
    blocker_code: str | None = None,
    latency_ms: int = 0,
) -> bool:
    provider = _required(provider, "provider")
    latency = _integer(latency_ms, "latency_ms")
    if latency < 0:
        raise ValueError("latency_ms must be non-negative")
    values = {
        "observation_id": observation.observation_id,
        "entry_id": observation.entry_id,
        "elapsed_ms": observation.elapsed_ms,
        "canonical_state_id": observation.canonical_state_id,
        "observed_at": observation.observed_at,
        "exit_kind": observation.exit_kind.value,
        "net_return_bps": observation.net_return_bps,
        "net_proceeds_usd_atomic": (
            str(observation.net_proceeds_usd_atomic)
            if observation.net_proceeds_usd_atomic is not None
            else None
        ),
        "source_healthy": int(observation.source_healthy),
        "migration": int(observation.migration),
        "orphaned": int(observation.orphaned),
        "provider": provider,
        "blocker_code": blocker_code,
        "latency_ms": latency,
    }
    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM op_counterfactual_entries WHERE entry_id=?",
            (observation.entry_id,),
        ).fetchone() is None:
            raise ValueError("entry_id does not exist")
        return _insert_immutable(
            conn,
            table="op_executable_observations",
            identity_column="observation_id",
            identity_value=observation.observation_id,
            values=values,
        )


def load_executable_observations(
    conn: sqlite3.Connection,
    entry_id: str,
) -> list[ExecutableObservation]:
    rows = conn.execute(
        "SELECT * FROM op_executable_observations WHERE entry_id=? "
        "ORDER BY elapsed_ms,observed_at,canonical_state_id",
        (_required(entry_id, "entry_id"),),
    ).fetchall()
    return [
        ExecutableObservation(
            observation_id=row["observation_id"],
            entry_id=row["entry_id"],
            elapsed_ms=row["elapsed_ms"],
            canonical_state_id=row["canonical_state_id"],
            observed_at=row["observed_at"],
            exit_kind=ExitObservationKind(row["exit_kind"]),
            net_return_bps=row["net_return_bps"],
            net_proceeds_usd_atomic=(
                int(row["net_proceeds_usd_atomic"])
                if row["net_proceeds_usd_atomic"] is not None
                else None
            ),
            source_healthy=bool(row["source_healthy"]),
            migration=bool(row["migration"]),
            orphaned=bool(row["orphaned"]),
        )
        for row in rows
    ]


def _reconciliation_statuses(conn: sqlite3.Connection, landmark_id: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT status FROM op_landmark_states WHERE supersedes_landmark_id=? "
            "AND status IN ('canonical','orphaned')",
            (landmark_id,),
        )
    }


def append_competing_outcome(
    conn: sqlite3.Connection,
    outcome: CompetingOutcome,
) -> bool:
    values = {
        "outcome_id": outcome.outcome_id,
        "entry_id": outcome.entry_id,
        "outcome_kind": outcome.outcome_kind.value,
        "elapsed_ms": outcome.elapsed_ms,
        "terminal_observation_id": outcome.terminal_observation_id,
        "terminal_net_return_bps": outcome.terminal_net_return_bps,
        "profit_barrier_bps": outcome.profit_barrier_bps,
        "loss_barrier_bps": outcome.loss_barrier_bps,
        "horizon_ms": outcome.horizon_ms,
        "last_executable_observation_id": outcome.last_executable_observation_id,
        "last_executable_return_bps": outcome.last_executable_return_bps,
        "evaluator_version": outcome.evaluator_version,
    }
    with transaction(conn):
        entry_row = conn.execute(
            "SELECT ce.landmark_id, ce.entry_effective_at, lm.status, "
            "l.network_id, l.venue_id, l.creator_id "
            "FROM op_counterfactual_entries ce "
            "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
            "JOIN op_launches l ON l.launch_id=lm.launch_id "
            "WHERE ce.entry_id=?",
            (outcome.entry_id,),
        ).fetchone()
        if entry_row is None:
            raise ValueError("entry_id does not exist")
        if entry_row["status"] == "tentative":
            statuses = _reconciliation_statuses(conn, entry_row["landmark_id"])
            if not statuses:
                raise ValueError("tentative entry requires canonical or orphaned reconciliation")
            if statuses == {"orphaned"} and outcome.outcome_kind != OutcomeKind.CHAIN_REORG:
                raise ValueError("orphaned tentative entry accepts only CHAIN_REORG outcome")
            if "canonical" in statuses and outcome.outcome_kind == OutcomeKind.CHAIN_REORG:
                raise ValueError("canonical reconciliation cannot use CHAIN_REORG outcome")
            if len(statuses) > 1:
                raise ValueError("tentative entry has conflicting reconciliation successors")
        # Fold the result into the creator's running record. Structurally causal: an
        # outcome can only finalize after the decision that created it, so a launch can
        # never contribute to its own prior. `last_outcome_at` carries the entry time of
        # the latest scored outcome -- telemetry only; the reader uses just the counts.
        credit_creator_outcome(
            conn,
            network_id=entry_row["network_id"],
            venue_id=entry_row["venue_id"],
            creator_id=entry_row["creator_id"],
            outcome_kind=outcome.outcome_kind.value,
            terminal_net_return_bps=outcome.terminal_net_return_bps,
            observed_at=entry_row["entry_effective_at"],
        )
        return _insert_immutable(
            conn,
            table="op_competing_outcomes",
            identity_column="outcome_id",
            identity_value=outcome.outcome_id,
            values=values,
        )


def list_outcome_ready_entries(
    conn: sqlite3.Connection,
    launch_id: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ce.* FROM op_counterfactual_entries ce "
        "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
        "JOIN op_canonical_events ev ON ev.canonical_event_id=lm.observed_event_id "
        "WHERE lm.launch_id=? AND (lm.status='canonical' OR EXISTS ("
        "SELECT 1 FROM op_landmark_states successor "
        "WHERE successor.supersedes_landmark_id=lm.landmark_id "
        "AND successor.status IN ('canonical','orphaned'))) "
        "ORDER BY ev.block_or_slot, COALESCE(ev.transaction_index,-1), ev.event_index, ce.entry_id",
        (_required(launch_id, "launch_id"),),
    ).fetchall()
