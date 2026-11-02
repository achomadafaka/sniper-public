"""Tests for wallet-taxonomy migration Stage 3 — shadow classifier + edge augmentation."""
from __future__ import annotations

import pytest

from scripts.wallet_archetype_extractor import (
    _append_tag_rationale_to_edges,
    _tag_specialist_horizon_passes,
    classify_archetype_tag_shadow,
)


# ---------------------------------------------------------------------------
# _tag_specialist_horizon_passes
# ---------------------------------------------------------------------------


def _horizon(
    sample_size=100,
    primary_tag_share=0.55,
    distinct_tag_count=5,
    unclassified_share=0.05,
):
    return {
        "sample_size": sample_size,
        "primary_tag_share": primary_tag_share,
        "distinct_tag_count": distinct_tag_count,
        "unclassified_share": unclassified_share,
        "primary_tag": "some-slug",
    }


def test_horizon_passes_on_strong_concentration():
    assert _tag_specialist_horizon_passes(_horizon()) is True


def test_horizon_fails_on_low_primary_share():
    assert _tag_specialist_horizon_passes(_horizon(primary_tag_share=0.40)) is False


def test_horizon_passes_despite_high_distinct_tag_count():
    """Polymarket tags markets granularly — a focused politics-only wallet
    can have 100+ distinct tags. distinct_tag_count is NOT a gate; the
    primary_tag_share is what discriminates specialist from diffuse."""
    h = _horizon(primary_tag_share=0.50, distinct_tag_count=150)
    assert _tag_specialist_horizon_passes(h) is True


def test_horizon_fails_on_high_unclassified():
    assert _tag_specialist_horizon_passes(_horizon(unclassified_share=0.30)) is False


def test_horizon_fails_on_small_sample():
    assert _tag_specialist_horizon_passes(_horizon(sample_size=5)) is False


def test_horizon_boundary_primary_share_exactly_45pct():
    # >=0.45 is spec
    assert _tag_specialist_horizon_passes(_horizon(primary_tag_share=0.45)) is True


def test_horizon_boundary_unclassified_exactly_15pct():
    # >=0.15 is spec fail
    assert _tag_specialist_horizon_passes(_horizon(unclassified_share=0.15)) is False
    assert _tag_specialist_horizon_passes(_horizon(unclassified_share=0.149)) is True


def test_horizon_handles_malformed_types():
    assert _tag_specialist_horizon_passes({"primary_tag_share": "not-a-number"}) is False
    assert _tag_specialist_horizon_passes({}) is False


# ---------------------------------------------------------------------------
# classify_archetype_tag_shadow
# ---------------------------------------------------------------------------


def _features(live=None, recent=None, trade_pairs=0.05, sell_ratio=0.05):
    return {
        "trade_pairing": {"paired_condition_ratio": trade_pairs},
        "trade_shape": {"sell_ratio": sell_ratio},
        "domain_tags_horizons": {
            "live_book": live or _horizon(),
            "recent_trade": recent or _horizon(),
        },
    }


def test_shadow_fires_when_live_book_passes():
    out = classify_archetype_tag_shadow(_features(live=_horizon()))
    assert out is not None
    assert out["archetype"] == "tag_specialist_accumulator"
    assert out["chosen_horizon"] == "live_book"
    assert out["primary_tag"] == "some-slug"


def test_shadow_prefers_live_book_when_both_pass():
    live = _horizon(primary_tag_share=0.50)
    recent = _horizon(primary_tag_share=0.60)
    out = classify_archetype_tag_shadow(_features(live=live, recent=recent))
    assert out["chosen_horizon"] == "live_book"
    # confidence elevated because both pass
    assert out["confidence"] == "medium"  # both pass but no strong share


def test_shadow_falls_back_to_recent_when_only_recent_passes():
    live = _horizon(unclassified_share=0.50)   # fails
    recent = _horizon()                          # passes
    out = classify_archetype_tag_shadow(_features(live=live, recent=recent))
    assert out["chosen_horizon"] == "recent_trade"


def test_shadow_returns_none_when_neither_passes():
    live = _horizon(primary_tag_share=0.10)
    recent = _horizon(primary_tag_share=0.10)
    assert classify_archetype_tag_shadow(_features(live=live, recent=recent)) is None


def test_shadow_returns_none_when_trade_pairs_high():
    """A wallet that sells (inventory-like) isn't a specialist_accumulator."""
    out = classify_archetype_tag_shadow(_features(trade_pairs=0.30))
    assert out is None


def test_shadow_returns_none_when_sell_ratio_high():
    out = classify_archetype_tag_shadow(_features(sell_ratio=0.25))
    assert out is None


def test_shadow_confidence_high_when_both_pass_with_strong_share():
    live = _horizon(primary_tag_share=0.75)
    recent = _horizon(primary_tag_share=0.80)
    out = classify_archetype_tag_shadow(_features(live=live, recent=recent))
    assert out["confidence"] == "high"


def test_shadow_returns_none_when_horizons_missing():
    assert classify_archetype_tag_shadow({}) is None
    assert classify_archetype_tag_shadow({"domain_tags_horizons": {}}) is None


def test_shadow_never_hardcodes_tag_slug():
    """Rule must fire regardless of what the primary_tag value is."""
    exotic = _horizon()
    exotic["primary_tag"] = "completely-made-up-future-2030-slug"
    out = classify_archetype_tag_shadow(_features(live=exotic))
    assert out is not None
    assert out["primary_tag"] == "completely-made-up-future-2030-slug"


# ---------------------------------------------------------------------------
# _append_tag_rationale_to_edges
# ---------------------------------------------------------------------------


def test_rationale_appended_to_market_selection_and_portfolio():
    features = {
        "domain_tags": {
            "primary_tag": "some-slug",
            "primary_tag_share": 0.50,
            "distinct_tag_count": 7,
            "unclassified_share": 0.05,
        }
    }
    ms = {"level": "medium", "rationale": ["orig"]}
    pc = {"level": "unknown", "rationale": []}
    new_ms, new_pc = _append_tag_rationale_to_edges(features, ms, pc)
    assert new_ms["level"] == "medium"  # unchanged
    assert new_pc["level"] == "unknown"
    assert any("tag-based" in r for r in new_ms["rationale"])
    assert any("primary_tag=some-slug" in r for r in new_pc["rationale"])


def test_rationale_unchanged_when_domain_tags_missing():
    ms = {"level": "medium", "rationale": ["orig"]}
    pc = {"level": "unknown", "rationale": []}
    new_ms, new_pc = _append_tag_rationale_to_edges({}, ms, pc)
    assert new_ms == ms
    assert new_pc == pc


def test_rationale_unchanged_when_no_primary_tag():
    features = {"domain_tags": {"primary_tag": None, "distinct_tag_count": 0}}
    ms = {"level": "medium", "rationale": ["orig"]}
    new_ms, _ = _append_tag_rationale_to_edges(features, ms, {"level": "unknown"})
    assert new_ms == ms


def test_rationale_does_not_mutate_original_edge_dict():
    features = {
        "domain_tags": {
            "primary_tag": "slug", "primary_tag_share": 0.5,
            "distinct_tag_count": 5, "unclassified_share": 0.05,
        }
    }
    original_rationale = ["orig"]
    ms = {"level": "medium", "rationale": original_rationale}
    _append_tag_rationale_to_edges(features, ms, {"level": "unknown"})
    # Original list must not have been mutated
    assert original_rationale == ["orig"]
