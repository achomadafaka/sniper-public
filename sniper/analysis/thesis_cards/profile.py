"""Thesis-profile assembler — one wallet's positions -> every card -> a ranked,
admission-annotated profile + a full per-card decision log + a deterministic run id.

This is the plumbing that makes the producers a product: run all of them over the
same wallet positions (each card extracts the fields it needs and degrades honestly
when they are absent), apply the router's admission rule, and present the result as a
ranked profile. Dead-ends (descriptive_only / BLOCKED / underpowered / contradicted)
are SHOWN, never hidden — the honest line is the point.

The decision log comes from `explain_paper_admission` (every card, with its TRUE
rejection reason), so downstream logging is correct rather than mislabelling every
rejection as `ineligible_q`. `profile_run_id` is a deterministic content hash of the
producer cards, so re-running an unchanged profile is idempotent (enrollment key).

The assembler does not enroll anything in paper or mutate any verdict; it composes
read-only card producers + the admission rule. Slot capping is a router concern and is
intentionally left out here (the full eligible set is shown).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from sniper.analysis.thesis_cards.admission import AdmissionDecision, explain_paper_admission
from sniper.analysis.thesis_cards.contract import ThesisCard
from sniper.analysis.thesis_cards.discovery_card import discovery_card
from sniper.analysis.thesis_cards.exit_card import exit_card
from sniper.analysis.thesis_cards.sizing_card import sizing_card
from sniper.analysis.thesis_cards.timing_card import timing_card

# Every Wave-1 producer. Each takes (positions) and returns a ThesisCard.
PRODUCERS: tuple[Callable[[Sequence[Mapping[str, Any]]], ThesisCard], ...] = (
    sizing_card, exit_card, timing_card, discovery_card,
)

_DEAD_END_TIERS = ("BLOCKED", "PROXY")


@dataclass
class ThesisProfile:
    wallet: str
    n_positions: int
    cards: list[ThesisCard]                       # eligible-ranked first, then the rest; nothing hidden
    headline: str
    profile_run_id: str                           # deterministic content hash (idempotency key component)
    decisions: list[AdmissionDecision] = field(default_factory=list)  # one per card, true reason
    eligible: list[ThesisCard] = field(default_factory=list)          # admitted subset, rank-sorted


def _fingerprint(cards: Sequence[ThesisCard]) -> str:
    parts = sorted(
        f"{c.card_id}:{c.card_version}:{c.n}:{c.score:.6f}:{c.p_value}:{c.evidence_state}"
        for c in cards
    )
    return "|".join(parts)


def _run_id(wallet: str, cards: Sequence[ThesisCard]) -> str:
    h = hashlib.sha256(f"{wallet}||{_fingerprint(cards)}".encode()).hexdigest()
    return h[:16]


def _state_label(c: ThesisCard) -> str:
    tier = c.evidence.get("data_tier")
    if tier in _DEAD_END_TIERS:
        return f"{c.card_id}={c.evidence_state}/{tier}"
    return f"{c.card_id}={c.evidence_state}"


def _headline(eligible: list[ThesisCard], all_cards: list[ThesisCard]) -> str:
    if eligible:
        lenses = ", ".join(f"{c.card_id} ({c.lens})" for c in eligible)
        return f"Forward-testable edge: {lenses}."
    states = "; ".join(_state_label(c) for c in all_cards)
    return f"No forward-testable edge on current data. {states}."


def assemble_profile(
    wallet: str,
    positions: Sequence[Mapping[str, Any]],
    *,
    extra_cards: Sequence[ThesisCard] | None = None,
) -> ThesisProfile:
    cards: list[ThesisCard] = [prod(positions) for prod in PRODUCERS]
    if extra_cards:
        cards.extend(extra_cards)

    run_id = _run_id(wallet, cards)                # from producer cards, pre-annotation
    decisions = explain_paper_admission(cards)     # full log; no cap at profile level

    eligible = sorted([d.card for d in decisions if d.decision == "admitted"],
                      key=lambda c: (-(c.rank_key or 0.0), c.q_value or 1.0, c.card_id, c.lens))
    rest = [d.card for d in decisions if d.decision != "admitted"]
    rest.sort(key=lambda c: (-(c.score or 0.0), c.card_id, c.lens))

    ranked = eligible + rest
    headline = _headline(eligible, ranked)
    return ThesisProfile(
        wallet=wallet, n_positions=len(positions), cards=ranked, headline=headline,
        profile_run_id=run_id, decisions=decisions, eligible=eligible,
    )
