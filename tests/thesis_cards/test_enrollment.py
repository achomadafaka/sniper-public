"""Forward-paper enrollment router.

Router scope: consume ThesisProfile.decisions + eligible cards, apply a rank-only
slot cap, emit queued paper enrollments and a full admission log. No DB writes,
no live trades, no forward tracking.
"""
from __future__ import annotations

import pytest

from sniper.analysis.thesis_cards.admission import explain_paper_admission
from sniper.analysis.thesis_cards.contract import producer_card
from sniper.analysis.thesis_cards.enrollment import (
    InMemoryPaperEnrollmentStore,
    enroll_profile,
    transition_paper_state,
)
from sniper.analysis.thesis_cards.profile import ThesisProfile

_FTD = {
    "metric": "calibration_edge",
    "horizon": "30d",
    "success_threshold": 0.03,
    "kill_threshold": 0.0,
    "min_forward_n": 20,
    "stake_convention": "uniform_paper_unit",
}


def _card(card_id: str, *, score: float = 1.0, p: float = 0.001, n: int = 40,
          state: str = "supported", placebo: bool | None = True,
          evidence: dict | None = None, ftd: dict | None = _FTD):
    return producer_card(
        card_id=card_id,
        lens=card_id,
        n=n,
        score=score,
        effect=score,
        p_value=p,
        placebo_passed=placebo,
        evidence_state=state,
        evidence=evidence or {},
        forward_test_design=ftd,
    )


def _profile(cards, *, run_id: str = "run-1") -> ThesisProfile:
    decisions = explain_paper_admission(cards)
    eligible = [d.card for d in decisions if d.decision == "admitted"]
    eligible.sort(key=lambda c: (-(c.rank_key or 0.0), c.q_value or 1.0, c.card_id, c.lens))
    return ThesisProfile(
        wallet="0xwallet",
        n_positions=100,
        cards=list(cards),
        headline="test",
        profile_run_id=run_id,
        decisions=decisions,
        eligible=eligible,
    )


def test_no_operator_picked_enrollment_uses_ranked_eligible_set():
    profile = _profile([
        _card("exit_card", score=1.0),
        _card("sizing_card", score=5.0),
        _card("timing_card", score=3.0),
    ])

    result = enroll_profile(profile, max_slots=2)

    assert [e.card_id for e in result.enrollments] == ["sizing_card", "timing_card"]
    assert all(e.state == "queued" for e in result.enrollments)
    assert not any(e.card_id == "exit_card" for e in result.enrollments)


def test_rejected_cards_log_reason_and_all_cards_are_accounted_for():
    profile = _profile([
        _card("sizing_card", score=5.0),
        _card("discovery_card", state="underpowered", evidence={"data_tier": "BLOCKED"},
              placebo=None, ftd=None),
        _card("exit_card", state="descriptive_only", evidence={"data_tier": "PROXY"},
              placebo=None, ftd=None),
    ])

    result = enroll_profile(profile)

    assert len(result.admission_log) == len(profile.cards)
    by_id = {row.card_id: row for row in result.admission_log}
    assert by_id["sizing_card"].decision == "admitted"
    assert by_id["discovery_card"].reason == "ineligible_data_tier"
    assert by_id["exit_card"].reason == "ineligible_data_tier"


def test_cap_truncates_by_rank_only_independent_of_input_order():
    cards_a = [_card("exit_card", score=1.0), _card("sizing_card", score=5.0),
               _card("timing_card", score=3.0)]
    cards_b = list(reversed(cards_a))

    a = enroll_profile(_profile(cards_a), max_slots=2)
    b = enroll_profile(_profile(cards_b), max_slots=2)

    assert [e.card_id for e in a.enrollments] == ["sizing_card", "timing_card"]
    assert [e.card_id for e in b.enrollments] == ["sizing_card", "timing_card"]
    assert {row.card_id: row.reason for row in a.admission_log}["exit_card"] == "slot_cap"


def test_rerun_is_idempotent_and_does_not_reset_state():
    store = InMemoryPaperEnrollmentStore()
    profile = _profile([_card("sizing_card")], run_id="same-run")

    first = enroll_profile(profile, store=store)
    key = first.enrollments[0].idempotency_key
    store.records[key].state = "active"
    second = enroll_profile(profile, store=store)

    assert len(store.records) == 1
    assert second.enrollments[0].state == "active"


def test_blocked_descriptive_and_missing_design_cannot_enroll():
    profile = _profile([
        _card("discovery_card", state="underpowered", evidence={"data_tier": "BLOCKED"},
              placebo=None, ftd=None),
        _card("exit_card", state="descriptive_only", evidence={"data_tier": "PROXY"},
              placebo=None, ftd=None),
        _card("selection_card", ftd=None),
    ])

    result = enroll_profile(profile)

    assert result.enrollments == []
    reasons = {row.card_id: row.reason for row in result.admission_log}
    assert reasons["discovery_card"] == "ineligible_data_tier"
    assert reasons["exit_card"] == "ineligible_data_tier"
    assert reasons["selection_card"] == "missing_forward_test_design"


def test_partial_tier_may_enroll_with_valid_forward_design():
    profile = _profile([_card("timing_card", evidence={"data_tier": "PARTIAL"})])

    result = enroll_profile(profile)

    assert [e.card_id for e in result.enrollments] == ["timing_card"]


def test_state_transitions_legal_only():
    assert transition_paper_state("queued", "active") == "active"
    assert transition_paper_state("active", "killed") == "killed"
    assert transition_paper_state("completed", "promoted") == "promoted"
    with pytest.raises(ValueError):
        transition_paper_state("queued", "completed")
    with pytest.raises(ValueError):
        transition_paper_state("promoted", "active")
