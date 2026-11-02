"""Thesis-card contract: producers leave router fields None; evidence_state validated."""
from __future__ import annotations

import pytest

from sniper.analysis.thesis_cards.contract import (
    ThesisCard, producer_card, valid_forward_test_design,
)

_FTD = {"metric": "calibration_edge", "horizon": "to_resolution", "success_threshold": 0.0,
        "kill_threshold": -0.05, "min_forward_n": 20, "stake_convention": "uniform_paper_unit"}


def test_producer_card_leaves_router_fields_none():
    c = producer_card(card_id="exit_card", lens="exit timing", n=40, score=1.2,
                      effect=0.03, p_value=0.01, placebo_passed=True,
                      evidence_state="supported", evidence={"x": 1})
    assert c.card_id == "exit_card" and c.n == 40
    assert c.q_value is None and c.eligible_for_paper is None and c.rank_key is None


def test_producer_card_carries_version_and_forward_design():
    c = producer_card(card_id="sizing_card", lens="sizing", n=40, score=1.0, effect=0.1,
                      p_value=0.01, placebo_passed=True, evidence_state="supported",
                      forward_test_design=_FTD)
    assert c.card_version == "1"                     # default
    assert c.forward_test_design == _FTD
    # default when not supplied
    d = producer_card(card_id="exit_card", lens="exit", n=1, score=0.0, effect=None,
                      p_value=None, placebo_passed=None, evidence_state="underpowered")
    assert d.card_version == "1" and d.forward_test_design is None


def test_valid_forward_test_design():
    assert valid_forward_test_design(_FTD) is True
    assert valid_forward_test_design(None) is False
    assert valid_forward_test_design({"metric": "x"}) is False        # missing keys
    assert valid_forward_test_design({**_FTD, "extra": 1}) is True    # extras ok


def test_invalid_evidence_state_rejected():
    with pytest.raises(ValueError):
        producer_card(card_id="x", lens="y", n=1, score=0.0, effect=None,
                      p_value=None, placebo_passed=None, evidence_state="totally_real")


def test_negative_score_rejected():
    with pytest.raises(ValueError):
        ThesisCard(card_id="x", lens="y", n=1, score=-1.0, effect=None,
                   p_value=None, placebo_passed=None, evidence_state="underpowered")
