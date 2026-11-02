"""Forward-paper admission for thesis cards.

This is the router-owned half of the thesis-card contract. Producers emit cheap
hypothesis cards with raw p-values and no paper decision; this module applies the
pre-registered family rule:

  q <= 0.20, placebo passed, card-type n floor met, rank-order admission only.

No operator curation happens here. A slot cap may truncate the ranked list, but
it cannot reorder or select by wallet/card name.

Two entry points:
  - apply_paper_admission(cards)   -> admitted cards only (the statistical primitive).
  - explain_paper_admission(cards) -> a decision record for EVERY card (admitted AND
    rejected, each with a reason), so the profile and the router log the true rejection
    cause — not "ineligible_q" for everything. This is the enrollment-log source of
    truth (see docs/superpowers/specs/2026-05-30-forward-paper-enrollment-contract.md).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from sniper.analysis.counterfactual_selection.guards import benjamini_hochberg
from sniper.analysis.thesis_cards.contract import ThesisCard, valid_forward_test_design

PAPER_Q_MAX = 0.20

DEFAULT_CARD_MIN_N: dict[str, int] = {
    "selection_card": 30,
    "exit_card": 15,
    "sizing_card": 20,
    "price_capture_card": 20,
    "price_capture_proxy_card": 20,
    "discovery_card": 20,
    "specialist_card": 20,
    "timing_card": 20,
}

_DEAD_END_STATES = {"underpowered", "contradicted", "descriptive_only"}
_BLOCKED_TIERS = {"BLOCKED", "PROXY"}


@dataclass(frozen=True)
class AdmissionDecision:
    """One per input card. `card` has q/eligible/rank filled iff admitted."""
    card: ThesisCard
    decision: str            # "admitted" | "rejected"
    reason: str              # "admitted" | one of the rejection taxonomy below
    rank: int | None         # 1-based rank among admitted, else None
    q_value: float | None
    n: int
    evidence_state: str
    data_tier: str | None


def _q_by_index(cards: Sequence[ThesisCard]) -> dict[int, float]:
    indexed_p = [(i, float(c.p_value)) for i, c in enumerate(cards) if c.p_value is not None]
    if not indexed_p:
        return {}
    qs = benjamini_hochberg([p for _, p in indexed_p])
    return {i: q for (i, _), q in zip(indexed_p, qs)}


def _statistical_reason(card: ThesisCard, q: float | None, required_n: int, q_max: float) -> str | None:
    """First failing statistical/structural gate, or None if statistically admissible.

    Precedence (most structural first): data_tier -> state -> placebo -> q -> n.
    A BLOCKED/PROXY card is also typically underpowered/descriptive; the data-tier
    reason is reported first because it is the more informative cause."""
    if card.evidence.get("data_tier") in _BLOCKED_TIERS:
        return "ineligible_data_tier"
    if card.evidence_state in _DEAD_END_STATES:
        return "ineligible_state"
    if card.placebo_passed is not True:
        return "ineligible_placebo"
    if q is None or q > q_max:
        return "ineligible_q"
    if card.n < required_n:
        return "ineligible_n"
    return None


def apply_paper_admission(
    cards: Sequence[ThesisCard],
    *,
    card_min_n: Mapping[str, int] | None = None,
    max_slots: int | None = None,
    q_max: float = PAPER_Q_MAX,
) -> list[ThesisCard]:
    """Return paper-admitted cards with router fields filled (statistical gate only).

    The input cards are not mutated. Cards with no p-value remain unranked for paper
    purposes. Ranking is deterministic: higher producer score first, then lower
    q-value, then stable card_id/lens tie-breaks. (forward_test_design is an
    enrollment-layer gate — see explain_paper_admission — not applied here.)"""
    min_n = {**DEFAULT_CARD_MIN_N, **dict(card_min_n or {})}
    q_by_idx = _q_by_index(cards)

    ranked: list[ThesisCard] = []
    for i, card in enumerate(cards):
        q = q_by_idx.get(i)
        required_n = int(min_n.get(card.card_id, 20))
        if _statistical_reason(card, q, required_n, q_max) is not None:
            continue
        ranked.append(replace(card, q_value=q, eligible_for_paper=True, rank_key=float(card.score)))

    ranked.sort(key=lambda c: (-(c.rank_key or 0.0), c.q_value or 1.0, c.card_id, c.lens))
    if max_slots is not None:
        ranked = ranked[:max(0, int(max_slots))]
    return ranked


def explain_paper_admission(
    cards: Sequence[ThesisCard],
    *,
    card_min_n: Mapping[str, int] | None = None,
    max_slots: int | None = None,
    q_max: float = PAPER_Q_MAX,
) -> list[AdmissionDecision]:
    """A decision record for EVERY input card, in input order.

    Enrollment-aware: a statistically-admissible card with no valid
    forward_test_design is rejected `missing_forward_test_design`; the rest are
    ranked and a slot cap rejects the overflow as `slot_cap`. Every card is
    accounted for with its true reason — this is the admission-log source of truth.
    """
    min_n = {**DEFAULT_CARD_MIN_N, **dict(card_min_n or {})}
    q_by_idx = _q_by_index(cards)
    decisions: list[AdmissionDecision | None] = [None] * len(cards)
    candidates: list[tuple[int, ThesisCard, float | None]] = []

    for i, card in enumerate(cards):
        q = q_by_idx.get(i)
        tier = card.evidence.get("data_tier")
        required_n = int(min_n.get(card.card_id, 20))
        reason = _statistical_reason(card, q, required_n, q_max)
        if reason is None and not valid_forward_test_design(card.forward_test_design):
            reason = "missing_forward_test_design"
        if reason is not None:
            decisions[i] = AdmissionDecision(
                card=card, decision="rejected", reason=reason, rank=None, q_value=q,
                n=card.n, evidence_state=card.evidence_state, data_tier=tier)
        else:
            candidates.append((i, card, q))

    # rank candidates by the same deterministic order as apply, then apply the slot cap
    candidates.sort(key=lambda t: (-float(t[1].score), (t[2] if t[2] is not None else 1.0),
                                   t[1].card_id, t[1].lens))
    cap = len(candidates) if max_slots is None else max(0, int(max_slots))
    for rank, (i, card, q) in enumerate(candidates, start=1):
        tier = card.evidence.get("data_tier")
        if rank <= cap:
            admitted = replace(card, q_value=q, eligible_for_paper=True, rank_key=float(card.score))
            decisions[i] = AdmissionDecision(
                card=admitted, decision="admitted", reason="admitted", rank=rank, q_value=q,
                n=card.n, evidence_state=card.evidence_state, data_tier=tier)
        else:
            decisions[i] = AdmissionDecision(
                card=card, decision="rejected", reason="slot_cap", rank=None, q_value=q,
                n=card.n, evidence_state=card.evidence_state, data_tier=tier)

    return [d for d in decisions if d is not None]
