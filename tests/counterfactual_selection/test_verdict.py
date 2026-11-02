"""Tests for CSE-F12 verdict synthesizer."""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.verdict import synthesize_verdict


def test_algorithmic_self_contained_routes_rebuild():
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.2},
        rule={"best": {"determinism": 0.9, "oos_confirmed": True, "feature": "price", "op": "<=", "threshold": 0.35}},
        automation={"coverage": 0.9, "dead_hours": 0},
        n_decisions=100,
    )
    assert v["classification"] == "algorithmic_self_contained"
    assert v["route"] == "rebuild_rule"
    assert v["dimensions"]["rule_extractability"] == "high"
    assert v["learned_rule"]["feature"] == "price"


def test_edge_without_rule_is_unobserved_signal_not_human():
    """G8: a real edge with no observed rule → unobserved signal (flag), NOT human."""
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.18},
        rule={"best": None, "rule_found": False},
        automation={"coverage": 0.5, "dead_hours": 5},
        n_decisions=100,
    )
    assert v["classification"] == "algorithmic_unobserved_signal"
    assert v["route"] == "flag_human_ai"
    assert "NOT necessarily human" in v["qualitative_note"]


def test_selective_no_rule_no_edge_leans_human():
    v = synthesize_verdict(
        edge={"supported": False, "edge": 0.01},
        rule={"best": None},
        automation={"coverage": 0.2, "dead_hours": 8},
        n_decisions=100,
    )
    assert v["classification"] == "human_or_noise"
    assert v["route"] == "copy_mirror"
    assert v["dimensions"]["human_discretion_evidence"] == "high"


def test_high_automation_no_clean_rule_is_mixed():
    # human selection + bot execution: automation high but no extractable selection rule
    v = synthesize_verdict(
        edge={"supported": False, "edge": 0.01},
        rule={"best": None},
        automation={"coverage": 0.9, "dead_hours": 0},
        n_decisions=100,
    )
    assert v["classification"] == "mixed"
    assert v["route"] == "copy_mirror"


def test_underpowered_collects_more():
    v = synthesize_verdict(edge={"supported": None}, rule={"best": None},
                           automation={}, n_decisions=10)
    assert v["classification"] == "undetermined"
    assert v["route"] == "collect_more"


def test_underpowered_edge_blocks_high_rule_verdict():
    v = synthesize_verdict(
        edge={"supported": None, "reason": "underpowered"},
        rule={"best": {"determinism": 0.95, "oos_confirmed": True}},
        automation={"coverage": 0.95, "dead_hours": 0},
        n_decisions=100,
    )

    assert v["classification"] == "undetermined"
    assert v["route"] == "collect_more"


def test_matched_edge_confound_downgrades_raw_supported_edge():
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.5},
        rule={"best": None, "rule_found": False},
        automation={"coverage": 0.95, "dead_hours": 0},
        n_decisions=100,
        edge_matched={"supported": False, "edge": 0.0},
        matching_balance={"scope_valid": True, "worst_abs_smd": 0.05},
        confound_flag=True,
    )

    assert v["classification"] == "undetermined"
    assert v["route"] == "collect_more"
    assert v["dimensions"]["selection_edge"] == "confounded"


def test_confound_diagnostic_does_not_override_high_oos_rule():
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.5},
        rule={"best": {"determinism": 0.95, "oos_confirmed": True}},
        automation={"coverage": 0.95, "dead_hours": 0},
        n_decisions=100,
        edge_matched={"supported": False, "edge": 0.0},
        matching_balance={"scope_valid": False, "worst_abs_smd": 0.43},
        confound_flag=True,
    )

    assert v["classification"] == "algorithmic_self_contained"
    assert v["route"] == "rebuild_rule"
    assert v["dimensions"]["selection_edge"] == "supported"


def test_g9_language_guard_and_confidence_cap():
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.3},
        rule={"best": {"determinism": 0.95, "oos_confirmed": True}},
        automation={"coverage": 0.95, "dead_hours": 0},
        n_decisions=500,
    )
    assert v["confidence"] <= 0.8                      # never overclaims
    assert "cannot prove intent" in v["honest_caveat"]
    assert "proved" not in v["qualitative_note"].lower()
    assert set(v["dimensions"]) == {"selection_edge", "rule_extractability",
                                    "automation_evidence", "human_discretion_evidence",
                                    "calibration_edge", "placebo_control"}


def test_calibration_does_not_rescue_global_underpower():
    # < MIN_DECISIONS: even a supported calibration edge must NOT route — too few
    # decisions overall (Codex finding #2).
    v = synthesize_verdict(
        edge={"supported": None, "reason": "underpowered"},
        rule={"best": None}, automation={}, n_decisions=20,
        calibration={"supported": True, "edge": 0.1})
    assert v["classification"] == "undetermined" and v["route"] == "collect_more"


def test_calibration_rescues_confounded_when_powered():
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.5}, rule={"best": None}, automation={},
        n_decisions=60, confound_flag=True,
        calibration={"supported": True, "edge": 0.1})
    assert v["dimensions"]["selection_edge"] == "confounded"
    assert v["classification"] == "mixed" and v["route"] == "copy_mirror"


def test_placebo_failure_blocks_algorithmic_self_contained():
    # high OOS rule + automation, but the promotion gate failed (placebo) → must
    # NOT route algorithmic_self_contained (Codex finding #3).
    v = synthesize_verdict(
        edge={"supported": True, "edge": 0.3},
        rule={"best": {"determinism": 0.95, "oos_confirmed": True}},
        automation={"coverage": 0.95, "dead_hours": 0}, n_decisions=100,
        promotion={"promotable": False, "blockers": ["placebo_not_passed"]},
        placebo={"passed": False})
    assert v["classification"] != "algorithmic_self_contained"
    assert v["dimensions"]["placebo_control"] == "failed"
