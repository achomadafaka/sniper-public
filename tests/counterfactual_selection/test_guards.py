"""Tests for CSE-F0 methodology guards."""
from __future__ import annotations

import pytest

from sniper.analysis.counterfactual_selection.guards import (
    classify_feature, leakage_violations, assert_rule_features_clean,
    benjamini_hochberg, promotion_decision, candidate_universe_quality,
    standardized_mean_diff, matching_balance, placebo_pass,
)


# ===== G3 leakage =====

def test_feature_time_scope_known():
    assert classify_feature("drift_6h") == "pre_engagement"
    assert classify_feature("entry_vwap") == "at_engagement"
    assert classify_feature("roi") == "post_engagement_label_only"


def test_unknown_feature_fails_closed():
    assert classify_feature("some_new_feature") == "forbidden_lookahead"
    assert classify_feature("future_volume") == "forbidden_lookahead"


def test_leakage_violations_flags_post_and_unknown():
    bad = leakage_violations(["drift_6h", "roi", "future_price", "entry_vwap"])
    assert set(bad) == {"roi", "future_price"}
    assert_rule_features_clean(["drift_6h", "entry_vwap"])  # clean → no raise
    with pytest.raises(ValueError, match="leakage"):
        assert_rule_features_clean(["drift_6h", "roi"])


# ===== G4 FDR + promotion =====

def test_bh_uniform_case():
    q = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(abs(v - 0.05) < 1e-9 for v in q)


def test_bh_separates_strong_from_weak():
    q = benjamini_hochberg([0.001, 0.5])
    assert q[0] < 0.01 and abs(q[1] - 0.5) < 1e-9


def test_promotion_requires_all_gates():
    ok, reasons = promotion_decision(n=100, effect_size=0.4, q_value=0.01, oos_confirmed=True)
    assert ok and reasons == []
    bad, reasons = promotion_decision(n=5, effect_size=0.05, q_value=0.2, oos_confirmed=False)
    assert not bad
    assert any("sample" in r for r in reasons) and any("effect" in r for r in reasons)
    assert any("fdr" in r for r in reasons) and any("oos" in r for r in reasons)


# ===== G1 candidate quality =====

def test_candidate_quality_states():
    assert candidate_universe_quality(n_picked=50, n_unselected=200, point_in_time_verified=True,
                                      price_available_share=0.9, resolution_available_share=0.9)["scope_quality"] == "ok"
    assert candidate_universe_quality(n_picked=50, n_unselected=200, point_in_time_verified=True,
                                      price_available_share=0.3, resolution_available_share=0.9)["scope_quality"] == "sparse"
    inv = candidate_universe_quality(n_picked=3, n_unselected=200, point_in_time_verified=True,
                                     price_available_share=0.9, resolution_available_share=0.9)
    assert inv["scope_quality"] == "invalid" and inv["underpowered"]
    assert candidate_universe_quality(n_picked=50, n_unselected=200, point_in_time_verified=False,
                                      price_available_share=0.9, resolution_available_share=0.9)["scope_quality"] == "invalid"


# ===== G2 matching balance =====

def test_smd_balanced_vs_imbalanced():
    assert abs(standardized_mean_diff([1, 2, 3, 4], [1, 2, 3, 4])) < 1e-9
    assert standardized_mean_diff([10, 11, 12], [1, 2, 3]) > 1.0


def test_matching_balance_caliper():
    good = matching_balance({"price": [0.4, 0.41, 0.42]}, {"price": [0.4, 0.41, 0.42]})
    assert good["caliper_pass"] and good["verdict"] == "ok"
    bad = matching_balance({"price": [0.8, 0.81, 0.82]}, {"price": [0.2, 0.21, 0.22]})
    assert not bad["caliper_pass"] and bad["verdict"] == "nearest_neighbor_scope_invalid"


# ===== G5 negative control / placebo =====

def test_placebo_pass_when_beyond_null():
    # need enough shuffles to reach p<=0.05: 20 null draws, all below observed
    res = placebo_pass(0.9, [0.1 + 0.01 * i for i in range(20)])  # max null 0.29 < 0.9
    assert res["passed"] and not res["battery_too_permissive"]
    assert res["empirical_p"] <= 0.05


def test_placebo_fail_when_within_null():
    res = placebo_pass(0.2, [0.3, 0.25, 0.4, 0.22, 0.5])  # observed not beyond shuffled
    assert not res["passed"] and res["battery_too_permissive"]
