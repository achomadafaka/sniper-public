"""Paper-admission rule for thesis cards.

Router-owned behavior only: producers leave q/rank/eligibility unset; admission
computes family q-values and admits every eligible card in deterministic rank order.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.admission import apply_paper_admission
from sniper.analysis.thesis_cards.contract import producer_card


def _card(card_id: str, *, n: int = 30, score: float = 1.0,
          p_value: float | None = 0.01, placebo: bool | None = True):
    return producer_card(
        card_id=card_id,
        lens=card_id,
        n=n,
        score=score,
        effect=score,
        p_value=p_value,
        placebo_passed=placebo,
        evidence_state="supported",
    )


def test_admission_fills_q_rank_and_eligibility_without_mutating_producer_cards():
    cards = [_card("exit_card", score=2.0), _card("sizing_card", score=1.0)]

    admitted = apply_paper_admission(cards)

    assert [c.card_id for c in admitted] == ["exit_card", "sizing_card"]
    assert all(c.q_value is not None for c in admitted)
    assert all(c.eligible_for_paper is True for c in admitted)
    assert admitted[0].rank_key > admitted[1].rank_key
    assert all(c.q_value is None and c.eligible_for_paper is None for c in cards)


def test_admission_requires_q_placebo_and_card_minimum_n():
    cards = [
        _card("exit_card", n=14, score=100.0),          # below exit min
        _card("sizing_card", placebo=False, score=99.0),
        _card("timing_card", p_value=0.90, score=98.0),
        _card("discovery_card", n=30, p_value=0.001, score=1.0),
    ]

    admitted = apply_paper_admission(cards)

    assert [c.card_id for c in admitted] == ["discovery_card"]
    assert admitted[0].eligible_for_paper is True


def test_admission_cap_uses_rank_order_not_input_order():
    cards = [
        _card("exit_card", score=1.0, p_value=0.001),
        _card("sizing_card", score=5.0, p_value=0.001),
        _card("timing_card", score=3.0, p_value=0.001),
    ]

    admitted = apply_paper_admission(cards, max_slots=2)

    assert [c.card_id for c in admitted] == ["sizing_card", "timing_card"]
