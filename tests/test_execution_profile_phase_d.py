"""Tests for Layer 3 mapping of execution_profile — spec §6.6."""
from __future__ import annotations

import pytest

from scripts.wallet_archetype_extractor import _apply_execution_profile_to_edges


def _edge(level: str, rationale=None, notes="") -> dict:
    rationale = rationale or []
    payload = {"level": level, "rationale": list(rationale)}
    if notes:
        payload["notes"] = notes
    return payload


def _features(status="populated", *, drift_mean=0.0, drift_median=0.0, n=30, tier="medium", n_skew=30) -> dict:
    return {
        "execution_profile": {
            "status": status,
            "confidence_tier": tier,
            "skew_vs_preceding_bar": {"n": n_skew},
            "drift_1h": {
                "n": n,
                "mean": drift_mean,
                "median": drift_median,
            },
        }
    }


# ---------------------------------------------------------------------------
# Promote to medium on positive drift
# ---------------------------------------------------------------------------


def test_entry_timing_promotes_to_medium_on_positive_drift():
    features = _features(drift_mean=0.025, drift_median=0.015, n=35, tier="medium")
    entry = _edge("unknown", ["stub"], notes="needs path-aware replay")
    exit_ = _edge("unknown", ["stub"])
    new_entry, _new_exit = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry["level"] == "medium"
    # path evidence exists now → notes key is absent (falsy → omitted by _edge_signal)
    assert new_entry.get("notes", "") == ""
    assert any("drift_1h_mean=+0.025" in r for r in new_entry["rationale"])


def test_entry_timing_promotes_to_high_requires_high_tier():
    features = _features(drift_mean=0.06, drift_median=0.04, n=120, tier="high", n_skew=150)
    entry = _edge("unknown", ["stub"])
    exit_ = _edge("unknown", ["stub"])
    new_entry, _ = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry["level"] == "high"
    assert "confidence_tier=high" in " | ".join(new_entry["rationale"])


def test_entry_timing_stays_medium_when_tier_is_medium_even_with_high_drift():
    features = _features(drift_mean=0.06, drift_median=0.04, n=40, tier="medium", n_skew=40)
    entry = _edge("unknown", ["stub"])
    exit_ = _edge("unknown", ["stub"])
    new_entry, _ = _apply_execution_profile_to_edges(features, entry, exit_)
    # Drift is strong but tier is only medium → upgrade stops at medium
    assert new_entry["level"] == "medium"


# ---------------------------------------------------------------------------
# Demote to low on negative drift
# ---------------------------------------------------------------------------


def test_entry_timing_downgrades_to_low_on_negative_drift():
    features = _features(drift_mean=-0.03, drift_median=-0.02, n=30, tier="medium")
    entry = _edge("unknown", ["stub"])
    exit_ = _edge("unknown", ["stub"])
    new_entry, _ = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry["level"] == "low"
    assert any("fading" in r for r in new_entry["rationale"])


# ---------------------------------------------------------------------------
# Near-zero drift: keep heuristic, append rationale
# ---------------------------------------------------------------------------


def test_entry_timing_near_zero_drift_keeps_heuristic_level():
    features = _features(drift_mean=0.005, drift_median=0.003, n=30, tier="medium")
    entry = _edge("medium", ["midpoint_buy_ratio=10.0%"], notes="suggestive only")
    exit_ = _edge("unknown", ["stub"])
    new_entry, _ = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry["level"] == "medium"  # preserved
    # Notes are NOT cleared (heuristic-only result, no execution promotion)
    assert new_entry.get("notes") == "suggestive only"
    assert any("near-zero" in r for r in new_entry["rationale"])


# ---------------------------------------------------------------------------
# Guard: low drift sample n → no change
# ---------------------------------------------------------------------------


def test_entry_timing_unchanged_when_drift_sample_too_small():
    features = _features(drift_mean=0.10, drift_median=0.08, n=9)
    entry = _edge("unknown", ["stub"], notes="needs path replay")
    exit_ = _edge("unknown", ["stub"])
    new_entry, new_exit = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry["level"] == "unknown"
    assert new_entry["notes"] == "needs path replay"  # untouched
    assert new_exit == exit_


# ---------------------------------------------------------------------------
# Guard: execution_profile not populated → no change
# ---------------------------------------------------------------------------


def test_entry_timing_unchanged_when_execution_profile_stub():
    features = {"execution_profile": {"status": "not_run", "reason": "gate"}}
    entry = _edge("unknown", ["stub"], notes="needs path replay")
    exit_ = _edge("unknown", ["stub"])
    new_entry, new_exit = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry == entry
    assert new_exit == exit_


def test_entry_timing_unchanged_when_no_execution_profile_key():
    features: dict = {}
    entry = _edge("unknown", ["stub"])
    exit_ = _edge("unknown", ["stub"])
    new_entry, new_exit = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_entry == entry
    assert new_exit == exit_


# ---------------------------------------------------------------------------
# Exit management cross-ref (v1)
# ---------------------------------------------------------------------------


def test_exit_management_gets_drift_rationale_appended_in_v1():
    features = _features(drift_mean=0.015, drift_median=0.01, n=30, tier="medium")
    entry = _edge("unknown", ["stub"])
    exit_ = _edge("medium", ["sell_ratio=10%"], notes="heuristic")
    _new_entry, new_exit = _apply_execution_profile_to_edges(features, entry, exit_)
    assert new_exit["level"] == "medium"  # unchanged level
    assert new_exit["notes"] == "heuristic"  # unchanged notes
    assert any("execution_profile.drift_1h_mean=+0.015" in r for r in new_exit["rationale"])


# ---------------------------------------------------------------------------
# Original edge dict is not mutated
# ---------------------------------------------------------------------------


def test_entry_edge_dict_not_mutated_in_place():
    features = _features(drift_mean=0.025, drift_median=0.015, n=30)
    entry = _edge("unknown", ["stub"], notes="orig")
    original_copy = {"level": entry["level"], "rationale": list(entry["rationale"]), "notes": entry["notes"]}
    exit_ = _edge("unknown", ["stub"])
    _ = _apply_execution_profile_to_edges(features, entry, exit_)
    # Original still reads as before
    assert entry["level"] == original_copy["level"]
    assert entry["rationale"] == original_copy["rationale"]
    assert entry["notes"] == original_copy["notes"]
