"""Shared thesis-card contract.

The boundary between the card PRODUCERS (exit/sizing/… — Claude lane) and the paper
ADMISSION/router (Codex lane). A card is a ranked HYPOTHESIS, never a claim — the
`evidence_state` carries the methodology honesty, and the router-owned fields
(`q_value`, `eligible_for_paper`, `rank_key`) are left None by producers and filled
by the family-wide FDR + admission layer.

Spec: docs/superpowers/specs/2026-05-30-thesis-extractor-multi-lens.md (rev 2,
§Architecture paper-admission rule + §Pre-registered card protocols).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Methodology-honesty vocabulary (producer sets one).
EVIDENCE_STATES = (
    "supported",         # effect + own p<=0.05 + placebo passed
    "exploratory",       # effect present but a required gate (e.g. permutation) not cleared
    "descriptive_only",  # reported for context; not a testable lead (e.g. unconditional version)
    "underpowered",      # below the card's minimum n
    "contradicted",      # effect in the wrong direction
)


@dataclass
class ThesisCard:
    """One profit-source lead for one wallet. Producers fill the measurement fields;
    the router fills q_value / eligible_for_paper / rank_key."""
    # --- producer-owned ---
    card_id: str                          # "exit_card" | "sizing_card" | …
    lens: str                             # human-readable edge locus
    n: int                                # effective sample for the test
    score: float                          # ranking score, >= 0 (higher = stronger lead)
    effect: float | None                  # raw effect (pp / dollars), signed
    p_value: float | None                 # own permutation/test p (None if underpowered)
    placebo_passed: bool | None           # negative control (None if not run)
    evidence_state: str                   # one of EVIDENCE_STATES
    evidence: dict = field(default_factory=dict)   # card-specific detail
    # --- producer-owned enrollment contract fields ---
    card_version: str = "1"               # bump when statistic/null/protocol changes (idempotency key)
    forward_test_design: dict | None = None  # how the card is forward-tested (None = no valid test now)
    # --- router/admission-owned (producers MUST leave None) ---
    q_value: float | None = None          # family-wide FDR
    eligible_for_paper: bool | None = None  # admission rule
    rank_key: float | None = None         # paper ordering

    def __post_init__(self) -> None:
        if self.evidence_state not in EVIDENCE_STATES:
            raise ValueError(f"evidence_state must be one of {EVIDENCE_STATES}, got {self.evidence_state!r}")
        if self.score < 0:
            raise ValueError(f"score must be >= 0, got {self.score}")


# Required keys for a valid forward_test_design (the enrollment contract — see
# docs/superpowers/specs/2026-05-30-forward-paper-enrollment-contract.md §5).
FORWARD_TEST_DESIGN_KEYS = (
    "metric", "horizon", "success_threshold", "kill_threshold",
    "min_forward_n", "stake_convention",
)


def valid_forward_test_design(d: Any) -> bool:
    """True iff d is a dict carrying every required forward_test_design key."""
    return isinstance(d, dict) and all(k in d for k in FORWARD_TEST_DESIGN_KEYS)


def producer_card(*, card_id: str, lens: str, n: int, score: float, effect: float | None,
                  p_value: float | None, placebo_passed: bool | None, evidence_state: str,
                  evidence: dict[str, Any] | None = None,
                  card_version: str = "1", forward_test_design: dict | None = None) -> ThesisCard:
    """Build a producer-side card with the router fields left None (enforced)."""
    return ThesisCard(
        card_id=card_id, lens=lens, n=int(n), score=float(score), effect=effect,
        p_value=p_value, placebo_passed=placebo_passed, evidence_state=evidence_state,
        evidence=evidence or {}, card_version=card_version, forward_test_design=forward_test_design,
    )
