"""Forward-paper enrollment router for thesis cards.

Scope is intentionally narrow: consume a ThesisProfile's admission decisions,
apply an optional rank-only slot cap, and emit queued paper enrollment records
plus a complete admission log. No DB writes, no live orders, no forward tracking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from sniper.analysis.thesis_cards.admission import AdmissionDecision
from sniper.analysis.thesis_cards.contract import ThesisCard, valid_forward_test_design
from sniper.analysis.thesis_cards.profile import ThesisProfile

PaperState = Literal["queued", "active", "completed", "killed", "promoted"]


class PaperEnrollmentStore(Protocol):
    """Any store the router can persist to (in-memory for tests, Supabase for D3)."""
    def upsert(self, enrollment: "PaperEnrollment") -> "PaperEnrollment": ...


@dataclass
class PaperEnrollment:
    wallet: str
    card_id: str
    card_version: str
    profile_run_id: str
    rank: int
    q_value: float | None
    evidence_state: str
    data_tier: str | None
    forward_test_design: dict
    state: PaperState = "queued"
    # D4 fields (S2 decay gate + forward tracking). Producer-sourced enrolled_effect is
    # the in-sample ThesisCard.effect; the rest are written by the D4 job, not the router.
    enrolled_effect: float | None = None
    enrolled_at: int | None = None          # unix ts; DB sets via default for Supabase
    forward_metric: dict = field(default_factory=dict)
    verdict: str | None = None

    @property
    def idempotency_key(self) -> str:
        return f"{self.wallet}|{self.card_id}|{self.card_version}|{self.profile_run_id}"


@dataclass(frozen=True)
class AdmissionLogEntry:
    card_id: str
    lens: str
    card_version: str
    decision: str
    reason: str
    rank: int | None
    q_value: float | None
    n: int
    evidence_state: str
    data_tier: str | None


@dataclass(frozen=True)
class EnrollmentResult:
    enrollments: list[PaperEnrollment]
    admission_log: list[AdmissionLogEntry]


class InMemoryPaperEnrollmentStore:
    """Tiny idempotent store for router tests and dry runs.

    Existing enrollment keys are returned unchanged, so reruns do not reset paper
    state. Production storage is a separate D3 schema decision.
    """

    def __init__(self) -> None:
        self.records: dict[str, PaperEnrollment] = {}

    def upsert(self, enrollment: PaperEnrollment) -> PaperEnrollment:
        key = enrollment.idempotency_key
        existing = self.records.get(key)
        if existing is not None:
            return existing
        self.records[key] = enrollment
        return enrollment

    # --- D4 forward-tracking surface ---
    def iter_active(self) -> list[dict]:
        """Rows in state queued/active, as dicts (the D4 evaluation input)."""
        return [_enrollment_row(e) for e in self.records.values()
                if e.state in ("queued", "active")]

    def update_tracking(self, *, key: dict, expected_state: PaperState,
                        new_state: PaperState, forward_metric: dict, verdict: str | None) -> bool:
        """Conditional advance: applies only if the row is still at expected_state.

        Returns False on a state mismatch (concurrency / already advanced); raises on an
        illegal (unreachable) transition. Terminals are immutable (no row at queued/active)."""
        ik = f"{key['wallet']}|{key['card_id']}|{key['card_version']}|{key['profile_run_id']}"
        e = self.records.get(ik)
        if e is None or e.state != expected_state:
            return False
        if not _reachable(expected_state, new_state):
            raise ValueError(f"illegal paper state transition: {expected_state} -> {new_state}")
        e.state = new_state
        e.forward_metric = forward_metric
        e.verdict = verdict
        return True


def _enrollment_row(e: PaperEnrollment) -> dict:
    """PaperEnrollment -> the row dict D4 evaluates (shared by both stores)."""
    return {
        "wallet": e.wallet, "card_id": e.card_id, "card_version": e.card_version,
        "profile_run_id": e.profile_run_id, "state": e.state,
        "enrolled_at": e.enrolled_at, "enrolled_effect": e.enrolled_effect,
        "forward_test_design": e.forward_test_design, "forward_metric": e.forward_metric,
        "verdict": e.verdict,
    }


def _rank_key(card: ThesisCard) -> tuple[float, float, str, str]:
    return (-(card.rank_key if card.rank_key is not None else card.score),
            card.q_value if card.q_value is not None else 1.0,
            card.card_id, card.lens)


def _log_entry(decision: AdmissionDecision, *, decision_text: str | None = None,
               reason: str | None = None, rank: int | None = None) -> AdmissionLogEntry:
    card = decision.card
    return AdmissionLogEntry(
        card_id=card.card_id,
        lens=card.lens,
        card_version=card.card_version,
        decision=decision_text or decision.decision,
        reason=reason or decision.reason,
        rank=rank if rank is not None else decision.rank,
        q_value=decision.q_value,
        n=decision.n,
        evidence_state=decision.evidence_state,
        data_tier=decision.data_tier,
    )


def enroll_profile(
    profile: ThesisProfile,
    *,
    max_slots: int | None = None,
    store: PaperEnrollmentStore | None = None,
    enrolled_at: int | None = None,
) -> EnrollmentResult:
    """Enroll rank-admitted thesis cards into queued paper records.

    Uses `profile.decisions` as the full admission-log source. Slot caps only
    truncate by deterministic rank; they never select by wallet/card name.
    """
    admitted = [d for d in profile.decisions if d.decision == "admitted"]
    admitted.sort(key=lambda d: _rank_key(d.card))
    cap = len(admitted) if max_slots is None else max(0, int(max_slots))
    selected_keys = {(d.card.card_id, d.card.lens, d.card.card_version) for d in admitted[:cap]}

    enrollments: list[PaperEnrollment] = []
    admission_log: list[AdmissionLogEntry] = []

    rank_by_key = {
        (d.card.card_id, d.card.lens, d.card.card_version): i
        for i, d in enumerate(admitted[:cap], start=1)
    }

    for decision in profile.decisions:
        card = decision.card
        key = (card.card_id, card.lens, card.card_version)
        if decision.decision == "admitted" and key not in selected_keys:
            admission_log.append(_log_entry(decision, decision_text="rejected",
                                            reason="slot_cap", rank=None))
            continue
        if decision.decision != "admitted":
            admission_log.append(_log_entry(decision))
            continue
        if not valid_forward_test_design(card.forward_test_design):
            admission_log.append(_log_entry(decision, decision_text="rejected",
                                            reason="missing_forward_test_design", rank=None))
            continue

        rank = rank_by_key[key]
        admission_log.append(_log_entry(decision, rank=rank))
        enrollment = PaperEnrollment(
            wallet=profile.wallet,
            card_id=card.card_id,
            card_version=card.card_version,
            profile_run_id=profile.profile_run_id,
            rank=rank,
            q_value=decision.q_value,
            evidence_state=decision.evidence_state,
            data_tier=decision.data_tier,
            forward_test_design=dict(card.forward_test_design),
            enrolled_effect=card.effect,        # S2 decay-gate baseline (in-sample effect)
            enrolled_at=enrolled_at,            # set for backtest (as-of T); None -> DB default now()
        )
        enrollments.append(store.upsert(enrollment) if store is not None else enrollment)

    enrollments.sort(key=lambda e: e.rank)
    return EnrollmentResult(enrollments=enrollments, admission_log=admission_log)


_LEGAL_TRANSITIONS: dict[PaperState, set[PaperState]] = {
    "queued": {"active"},
    "active": {"completed", "killed"},
    "completed": {"promoted"},
    "killed": set(),
    "promoted": set(),
}


def transition_paper_state(current: PaperState, target: PaperState) -> PaperState:
    """Validate a single-hop paper-state transition and return the target state."""
    if target not in _LEGAL_TRANSITIONS[current]:
        raise ValueError(f"illegal paper state transition: {current} -> {target}")
    return target


def _reachable(expected: PaperState, target: PaperState) -> bool:
    """True if `target` is reachable from `expected` via legal hops (or is `expected`).

    D4 writes the final reachable state atomically (e.g. active -> promoted, skipping the
    `completed` waypoint a promoted card never occupies)."""
    if target == expected:
        return True
    seen: set[PaperState] = set()
    frontier = [expected]
    while frontier:
        nxts = _LEGAL_TRANSITIONS.get(frontier.pop(), set())
        for nxt in nxts:
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False
