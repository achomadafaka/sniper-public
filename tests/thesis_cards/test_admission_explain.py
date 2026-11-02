"""explain_paper_admission — a decision record for EVERY card with its TRUE reason.

This is the fix for the Critical that the router can't recover rejection causes from
producer cards alone (every rejection would mislog as ineligible_q). The reason
precedence is data_tier -> state -> placebo -> q -> n, then missing_forward_test_design,
then slot_cap.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.admission import explain_paper_admission
from sniper.analysis.thesis_cards.contract import producer_card

_FTD = {"metric": "m", "horizon": "h", "success_threshold": 0.0, "kill_threshold": -1.0,
        "min_forward_n": 1, "stake_convention": "uniform_paper_unit"}


def _good(cid="sizing_card", score=1.0, p=0.001, n=40, state="supported",
          placebo=True, evidence=None, ftd=_FTD):
    return producer_card(card_id=cid, lens=cid, n=n, score=score, effect=0.1, p_value=p,
                         placebo_passed=placebo, evidence_state=state, evidence=evidence,
                         forward_test_design=ftd)


def _reason(card):
    return explain_paper_admission([card])[0].reason


def test_explain_accounts_for_every_card():
    cards = [_good(), _good(cid="timing_card", ftd=None),
             _good(cid="discovery_card", state="underpowered", evidence={"data_tier": "BLOCKED"})]
    decs = explain_paper_admission(cards)
    assert len(decs) == 3
    assert {d.card.card_id for d in decs} == {"sizing_card", "timing_card", "discovery_card"}


def test_explain_admitted_has_rank_and_q():
    d = explain_paper_admission([_good()])[0]
    assert d.decision == "admitted" and d.reason == "admitted" and d.rank == 1
    assert d.q_value is not None
    assert d.card.eligible_for_paper is True and d.card.rank_key == 1.0


def test_explain_reason_data_tier_beats_state():
    # BLOCKED tier is reported even though the card is also underpowered
    assert _reason(_good(state="underpowered", placebo=None,
                         evidence={"data_tier": "BLOCKED"}, ftd=None)) == "ineligible_data_tier"


def test_explain_reason_state():
    assert _reason(_good(state="contradicted", placebo=None)) == "ineligible_state"


def test_explain_reason_placebo():
    assert _reason(_good(state="exploratory", placebo=False)) == "ineligible_placebo"


def test_explain_reason_q():
    # not significant: high p alone -> q ~ p > 0.20
    assert _reason(_good(state="exploratory", placebo=True, p=0.9)) == "ineligible_q"


def test_explain_reason_n_below_floor():
    # sizing floor = 20; n=5, otherwise eligible
    assert _reason(_good(state="exploratory", placebo=True, p=0.001, n=5)) == "ineligible_n"


def test_explain_reason_missing_forward_design():
    # statistically eligible but no forward_test_design
    assert _reason(_good(ftd=None)) == "missing_forward_test_design"


def test_explain_slot_cap_rejects_overflow_by_rank():
    cards = [_good(cid="sizing_card", score=1.0, p=0.001),
             _good(cid="timing_card", score=5.0, p=0.001),
             _good(cid="exit_card", score=3.0, p=0.001, n=40)]
    decs = explain_paper_admission(cards, max_slots=2)
    by_id = {d.card.card_id: d for d in decs}
    # top-2 by score (timing=5, exit=3) admitted; sizing (1.0) is slot_cap
    assert by_id["timing_card"].decision == "admitted" and by_id["timing_card"].rank == 1
    assert by_id["exit_card"].decision == "admitted" and by_id["exit_card"].rank == 2
    assert by_id["sizing_card"].decision == "rejected" and by_id["sizing_card"].reason == "slot_cap"
