"""Wallet SIGNAL PROFILE builder: synthesize the archetype classification (headline) + the thesis
in-sample edge read (secondary) into an explainable profile (NOT a grade/score). No insider_score."""
from __future__ import annotations

from sniper.analysis.wallet_grade import grade as G

ARCH = {"classification": {"archetype": "directional_discretionary", "confidence": "high",
                           "copy_policy": "validated_copy", "rationale": ["x"]},
        "classification_layer2": {"archetype": "thesis_specialist", "confidence": "medium",
                                  "copy_policy": "reverse_engineer_candidate"},
        "domain_tags": {"primary_tag": "NBA", "primary_tag_share": 0.6}}
THESIS = {"shuffle_baseline": {"obs_over_expected": 2.4},
          "edge_clusters": [{"label": "big NBA bets", "pnl": 50000, "is_loss_center": 0, "edge_pp": 4},
                            {"label": "tennis", "pnl": -2000, "is_loss_center": 1, "edge_pp": -2}]}


def test_profile_archetype_led_directional():
    g = G.build_profile(THESIS, ARCH, q75_usdc=30000.0)
    assert g["archetype"] == "directional_discretionary" and g["archetype_confidence"] == "high"
    assert g["intent"] == "thesis_specialist"
    assert g["directional_profile"] is True
    assert g["copy_policy"] == "validated_copy"
    assert g["in_sample_edge_read"] == "profitable_unproven"   # bare ratio is in-sample -> never "provable"
    assert g["edge_read_strength"] == "high"
    assert "big NBA bets" in g["edge_domains"] and "tennis" not in g["edge_domains"]
    assert g["primary_tag"] == "NBA" and g["q75_usdc"] == 30000.0
    assert g["description"] and "insider" not in g["description"].lower()
    assert "validation required" in g["description"].lower()   # never reads as a certification


def test_profile_non_directional_arb():
    arch = {**ARCH, "classification": {"archetype": "paired_outcome_arb", "confidence": "high",
                                       "copy_policy": "exclude", "rationale": []},
            "classification_layer2": None}
    g = G.build_profile({"shuffle_baseline": {}, "edge_clusters": []}, arch, q75_usdc=0.0)
    assert g["directional_profile"] is False
    assert g["in_sample_edge_read"] in ("mixed", "net_loser", "profitable_unproven")


def test_edge_read_strength_bands():
    assert G.build_profile({"shuffle_baseline": {"obs_over_expected": 2.0}}, ARCH)["edge_read_strength"] == "high"
    assert G.build_profile({"shuffle_baseline": {"obs_over_expected": 2.0}}, ARCH)["in_sample_edge_read"] == "profitable_unproven"
    assert G.build_profile({"shuffle_baseline": {"obs_over_expected": 1.5}}, ARCH)["in_sample_edge_read"] == "profitable_unproven"
    assert G.build_profile({"shuffle_baseline": {"obs_over_expected": 0.9}}, ARCH)["in_sample_edge_read"] == "mixed"


def test_profile_uses_authoritative_thesis_verdict_when_present():
    # the analyzer's own edge_verdict (proper stats) wins over the bare ratio; only it can say "provable"
    g = G.build_profile({"edge_verdict": "profitable_provable", "shuffle_baseline": {"obs_over_expected": 0.5}}, ARCH)
    assert g["in_sample_edge_read"] == "profitable_provable" and g["edge_read_strength"] == "high"


def test_manual_review_is_not_directional():
    # manual_review = extractor's unclassifiable fallback (basket/mixed) -> NOT a directional profile
    for pol in ("manual_review", "study_only", "exclude"):
        arch = {"classification": {"archetype": "multi_outcome_basket_builder", "confidence": "low",
                                   "copy_policy": pol}, "classification_layer2": None}
        assert G.build_profile({}, arch)["directional_profile"] is False, pol
    for pol in ("validated_copy", "reverse_engineer_candidate"):
        arch = {"classification": {"archetype": "directional_discretionary", "confidence": "high",
                                   "copy_policy": pol}, "classification_layer2": None}
        assert G.build_profile({}, arch)["directional_profile"] is True, pol


def test_profile_handles_empty():
    g = G.build_profile({}, {}, q75_usdc=0.0)
    assert isinstance(g["archetype"], str)
    assert g["directional_profile"] in (True, False) and g["description"]
