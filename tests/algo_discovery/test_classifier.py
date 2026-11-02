"""Tests for the behavioral classifier (algorithmic vs discretionary → route)."""
from __future__ import annotations

from sniper.analysis.algo_discovery.classifier import (
    classify_wallet_behavior,
    BehaviorClassification,
)


def _tables(*, burst, inter_s, cv, n_hours):
    return {
        "descriptor_cadence": [
            {"side": "BUY", "burst_share": burst, "median_seconds_between_trades": inter_s, "n_intervals": 100},
        ],
        "descriptor_sizing": [
            {"side": "BUY", "coeff_of_variation": cv, "n_fills": 100},
        ],
        "descriptor_hour_distribution": [
            {"side": "BUY", "hour_utc": h, "n_trades": 5} for h in range(n_hours)
        ],
    }


def test_algorithmic_wallet_routes_to_rebuild():
    # fast bursts, 24/7 coverage, uniform sizing → all 4 signals say algorithmic
    c = classify_wallet_behavior(_tables(burst=0.7, inter_s=20, cv=0.05, n_hours=20))
    assert c.label == "algorithmic"
    assert c.route_hint == "rebuild_rule"
    assert c.net_score == 4
    assert c.confidence == 1.0


def test_discretionary_wallet_routes_to_copy_mirror():
    # not bursty, slow, clustered hours, highly variable sizing → all 4 discretionary
    c = classify_wallet_behavior(_tables(burst=0.05, inter_s=200_000, cv=1.5, n_hours=5))
    assert c.label == "discretionary"
    assert c.route_hint == "copy_mirror"
    assert c.net_score == -4


def test_mixed_signals_route_to_human_review():
    # bursty (+1) but highly variable sizing (-1), mid-speed (neutral), no hours → net 0
    tables = {
        "descriptor_cadence": [{"side": "BUY", "burst_share": 0.7, "median_seconds_between_trades": 5000, "n_intervals": 100}],
        "descriptor_sizing": [{"side": "BUY", "coeff_of_variation": 1.5, "n_fills": 100}],
    }
    c = classify_wallet_behavior(tables)
    assert c.label == "ambiguous"
    assert c.route_hint == "human_review"
    assert c.net_score == 0


def test_no_descriptors_is_insufficient():
    c = classify_wallet_behavior({})
    assert c.label == "insufficient_data"
    assert c.route_hint == "human_review"
    assert c.confidence == 0.0


def test_weighted_aggregation_across_categories():
    # two cadence rows: a big-weight non-bursty one should dominate a small bursty one
    tables = {
        "descriptor_cadence": [
            {"side": "BUY", "burst_share": 0.9, "median_seconds_between_trades": 10, "n_intervals": 5},
            {"side": "BUY", "burst_share": 0.02, "median_seconds_between_trades": 200_000, "n_intervals": 500},
        ],
        "descriptor_sizing": [{"side": "BUY", "coeff_of_variation": 1.5, "n_fills": 100}],
    }
    c = classify_wallet_behavior(tables)
    # weighted burst ≈ (0.9*5 + 0.02*500)/505 ≈ 0.028 → not bursty (-1); slow (-1); cv high (-1)
    assert c.signals["burst_share"] < 0.10
    assert c.label == "discretionary"


def test_shape_and_version():
    c = classify_wallet_behavior(_tables(burst=0.7, inter_s=20, cv=0.05, n_hours=20))
    assert isinstance(c, BehaviorClassification)
    assert c.classifier_version == "algo_discovery_behavior_classifier_v1"
    assert set(c.signals) >= {"burst_share", "median_seconds_between_trades", "hour_coverage", "sizing_cv", "votes"}
