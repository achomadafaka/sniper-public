from datetime import datetime, timezone
from unittest.mock import patch

from scripts.wallet_archetype_extractor import (
    _build_edge_hypotheses,
    _event_family_metrics,
    _build_outcome_attribution,
    _fetch_wallet_tenure_metadata,
    _per_position_pnl_metrics,
    _timing_metrics,
    _trade_shape_metrics,
    classify_archetype,
    classify_archetype_layer2,
    extract_wallet_archetype,
)


# Minimal Layer 1 classification shape — Layer 2 only reads the archetype name from it.
_L1_NON_TERMINAL = {"classification": {"archetype": "sports_specialist_accumulator"}}
_L1_TERMINAL_ARB = {"classification": {"archetype": "paired_outcome_arb"}}
_L1_TERMINAL_MM = {"classification": {"archetype": "two_sided_inventory_trader"}}


def _layer2_features(l1_archetype="tag_specialist_accumulator", **overrides):
    """Build a feature dict with every Layer 2 input block present.

    Stage 5 migration: Layer 2 reads domain_tags (not legacy domain).
    Test fixtures accept a `domain` override as shorthand — we auto-map
    its `distinct_titles` + `concentration` to the equivalent domain_tags
    fields so existing tests keep their semantics without rewriting every
    fixture.
    """
    base = {
        "classification": {"archetype": l1_archetype},
        "timing": {
            "wallet_lifetime_days": 60,
            "lifespan_days": 60,
            "inter_trade_interval_cv": 0.5,
            "us_hours_share": 0.5,
        },
        "domain_tags": {
            "distinct_titles": 10,
            "primary_tag_share": 0.4,
            "primary_tag": "test-slug",
            "unclassified_share": 0.0,
            "sample_size": 20,
            "distinct_tag_count": 10,
        },
        "trade_shape": {
            "top_condition_fill_share": 0.2, "price_repetition_ratio": 0.1,
            "sell_ratio": 0.05, "usdc_size_cv": 1.0,
        },
        "redeems": {"redeem_ratio": 0.08},
        "returns": {"resolved_position_count": 8, "pnl_cv_abs": 0.4, "positive_share": 0.5},
    }
    # Back-compat shim: if a test passes `domain={...}` (legacy field),
    # rewrite its distinct_titles + concentration into domain_tags.
    if "domain" in overrides:
        legacy_domain = overrides.pop("domain")
        base["domain_tags"] = {
            **base["domain_tags"],
            "distinct_titles": legacy_domain.get("distinct_titles", base["domain_tags"]["distinct_titles"]),
            "primary_tag_share": legacy_domain.get("concentration", base["domain_tags"]["primary_tag_share"]),
        }
    for key, patch in overrides.items():
        base[key] = {**base.get(key, {}), **patch}
    return base


def _layer3_features(l1_archetype="sports_specialist_accumulator", l2_archetype=None, **overrides):
    base = _layer2_features(l1_archetype=l1_archetype)
    base.update(
        {
            "counts": {"activity_records": 100, "trade_records": 80, "live_positions": 12},
            "trade_pairing": {"paired_condition_ratio": 0.05},
            "position_pairing": {"paired_condition_ratio": 0.04, "negative_risk_condition_ratio": 0.0},
            "position_event_families": {"multi_condition_family_ratio": 0.0, "negative_risk_family_value_share": 0.0, "max_family_value_share": 0.1},
        }
    )
    if l2_archetype is not None:
        base["classification_layer2"] = {"archetype": l2_archetype}
    for key, patch in overrides.items():
        if key == "classification_layer2":
            base[key] = {**base.get(key, {}), **patch}
        else:
            base[key] = {**base.get(key, {}), **patch}
    return base


def test_classify_paired_outcome_arb():
    features = {
        "position_pairing": {
            "paired_condition_ratio": 0.72,
            "paired_value_share": 0.81,
            "binary_paired_condition_ratio": 0.72,
            "binary_secondary_leg_value_share": 0.81,
        },
        "trade_pairing": {"paired_condition_ratio": 0.18},
        "position_event_families": {"multi_condition_family_ratio": 0.0, "multi_condition_family_value_share": 0.0, "negative_risk_family_value_share": 0.0},
        "trade_shape": {"sell_ratio": 0.05, "midpoint_buy_ratio": 0.42, "price_repetition_ratio": 0.31},
        "domain": {"primary_domain": "political", "concentration": 0.61},
        "redeems": {"redeem_ratio": 0.01},
    }
    result = classify_archetype(features)
    assert result["archetype"] == "paired_outcome_arb"
    assert result["copy_policy"] == "exclude"


def test_classify_tag_specialist_accumulator_from_live_book():
    """Stage 4 cutover: rule fires on tag-concentrated live book, any tag slug."""
    features = {
        "position_pairing": {"paired_condition_ratio": 0.04, "paired_value_share": 0.03},
        "trade_pairing": {"paired_condition_ratio": 0.06},
        "position_event_families": {"multi_condition_family_ratio": 0.0, "multi_condition_family_value_share": 0.0, "negative_risk_family_value_share": 0.0},
        "trade_shape": {"sell_ratio": 0.04, "midpoint_buy_ratio": 0.28, "price_repetition_ratio": 0.41},
        # Legacy `domain` still populated (Stage 5 removes it) but no longer
        # consulted by the classifier. Tag-based signal is authoritative.
        "domain": {"primary_domain": "sports", "concentration": 0.58},
        "domain_tags_horizons": {
            "live_book": {
                "sample_size": 30,
                "primary_tag": "some-opaque-slug",
                "primary_tag_share": 0.60,
                "unclassified_share": 0.05,
                "distinct_tag_count": 40,
            },
            "recent_trade": {
                "sample_size": 30,
                "primary_tag": "some-opaque-slug",
                "primary_tag_share": 0.58,
                "unclassified_share": 0.10,
                "distinct_tag_count": 35,
            },
        },
        "redeems": {"redeem_ratio": 0.12},
    }
    result = classify_archetype(features)
    assert result["archetype"] == "tag_specialist_accumulator"
    assert result["copy_policy"] == "study_only"
    assert result["primary_tag"] == "some-opaque-slug"


def test_classify_tag_specialist_requires_low_sell_ratio():
    features = {
        "position_pairing": {"paired_condition_ratio": 0.04, "paired_value_share": 0.03},
        "trade_pairing": {"paired_condition_ratio": 0.06},
        "position_event_families": {"multi_condition_family_ratio": 0.0, "multi_condition_family_value_share": 0.0, "negative_risk_family_value_share": 0.0},
        "trade_shape": {"sell_ratio": 0.22, "midpoint_buy_ratio": 0.02, "price_repetition_ratio": 0.41},
        "domain": {"primary_domain": "sports", "concentration": 0.58},
        "domain_tags_horizons": {
            "live_book": {
                "sample_size": 30,
                "primary_tag": "slug",
                "primary_tag_share": 0.60,
                "unclassified_share": 0.05,
                "distinct_tag_count": 40,
            },
        },
        "redeems": {"redeem_ratio": 0.12},
    }
    result = classify_archetype(features)
    assert result["archetype"] != "tag_specialist_accumulator"


# Stage 5 deleted tests that relied on the legacy keyword classifier:
# - test_classify_legacy_keyword_path_preserved_for_rollback
#   (classify_archetype_legacy is gone)
# - test_classify_title_detects_generic_head_to_head_sports_titles
#   (_classify_title / _classify_title_subcategory are gone)
#
# Coverage of the tag-based specialist path lives in
# tests/test_wallet_taxonomy_stage3.py.


def test_event_family_metrics_surface_multi_condition_negative_risk_baskets():
    rows = [
        {
            "eventSlug": "presidential-election-winner-2028",
            "conditionId": "c1",
            "title": "Will Dwayne 'The Rock' Johnson win the 2028 US Presidential Election?",
            "negativeRisk": True,
            "currentValue": 400_000,
        },
        {
            "eventSlug": "presidential-election-winner-2028",
            "conditionId": "c2",
            "title": "Will Gavin Newsom win the 2028 US Presidential Election?",
            "negativeRisk": True,
            "currentValue": 20_000,
        },
        {
            "eventSlug": "next-prime-minister-of-hungary",
            "conditionId": "c3",
            "title": "Will the next Prime Minister of Hungary be Viktor Orbán?",
            "negativeRisk": True,
            "currentValue": 15_000,
        },
        {
            "eventSlug": "next-prime-minister-of-hungary",
            "conditionId": "c4",
            "title": "Will the next Prime Minister of Hungary be Péter Magyar?",
            "negativeRisk": True,
            "currentValue": 10_000,
        },
        {
            "eventSlug": "single-binary-event",
            "conditionId": "c5",
            "title": "Will candidate X win the election?",
            "negativeRisk": False,
            "currentValue": 5_000,
        },
    ]
    result = _event_family_metrics(rows)
    assert result["multi_condition_families"] == 2
    assert result["multi_condition_family_ratio"] == 0.667
    assert result["negative_risk_family_ratio"] == 0.667
    assert result["negative_risk_family_value_share"] == 0.989
    assert "Will Dwayne 'The Rock' Johnson win the 2028 US Presidential Election?" in result["family_examples"]


def test_classify_multi_outcome_basket_builder():
    features = {
        "position_pairing": {"paired_condition_ratio": 0.02, "paired_value_share": 0.03},
        "trade_pairing": {"paired_condition_ratio": 0.04},
        "position_event_families": {
            "multi_condition_family_ratio": 0.14,
            "multi_condition_family_value_share": 0.41,
            "negative_risk_family_value_share": 0.35,
        },
        "trade_shape": {"sell_ratio": 0.03, "midpoint_buy_ratio": 0.18, "price_repetition_ratio": 0.08},
        "domain": {"primary_domain": "sports", "concentration": 0.56},
        "redeems": {"redeem_ratio": 0.04},
    }
    result = classify_archetype(features)
    assert result["archetype"] == "multi_outcome_basket_builder"
    assert result["copy_policy"] == "manual_review"


# ---------------------------------------------------------------------------
# New features added in the merge (timing, sizing CV, per-position PnL)
# ---------------------------------------------------------------------------


def test_usdc_size_cv_low_for_consistent_sizes():
    """Algo signature: buys at ~$100 every time → CV near 0."""
    trades = [
        {"side": "BUY", "usdcSize": 100, "price": 0.5, "conditionId": f"c{i}"}
        for i in range(10)
    ]
    result = _trade_shape_metrics(trades)
    assert result["usdc_size_cv"] < 0.01, f"consistent sizes should yield low CV, got {result['usdc_size_cv']}"


def test_usdc_size_cv_high_for_conviction_sized():
    """Discretionary signature: sizes range from $10 to $1000 → CV > 1."""
    sizes = [10, 25, 100, 500, 1000, 50, 200, 5, 2000, 300]
    trades = [
        {"side": "BUY", "usdcSize": s, "price": 0.5, "conditionId": f"c{i}"}
        for i, s in enumerate(sizes)
    ]
    result = _trade_shape_metrics(trades)
    assert result["usdc_size_cv"] > 1.0, f"variable sizes should yield high CV, got {result['usdc_size_cv']}"


def test_timing_metrics_on_regular_cadence():
    """Algo trades every 3600s — CV near 0."""
    base_ts = 1_700_000_000
    trades = [{"timestamp": base_ts + i * 3600, "side": "BUY", "conditionId": "x"} for i in range(20)]
    result = _timing_metrics(trades)
    assert result["inter_trade_interval_cv"] < 0.05
    assert result["lifespan_days"] == round(19 * 3600 / 86400, 2)
    assert sum(result["time_of_day_histogram"]) == 20


def test_timing_metrics_on_bursty_cadence():
    """Human bursts — many trades in minutes, then silence for days. High CV."""
    base_ts = 1_700_000_000
    burst1 = [base_ts + i * 60 for i in range(5)]
    burst2 = [base_ts + 5 * 86400 + i * 60 for i in range(5)]
    trades = [{"timestamp": ts, "side": "BUY", "conditionId": "x"} for ts in burst1 + burst2]
    result = _timing_metrics(trades)
    assert result["inter_trade_interval_cv"] > 2.0


def test_timing_metrics_empty_on_tiny_sample():
    """Fewer than 5 trades → CV is unreliable, safely returns 0."""
    trades = [{"timestamp": 1_700_000_000 + i, "side": "BUY", "conditionId": "x"} for i in range(3)]
    result = _timing_metrics(trades)
    assert result["inter_trade_interval_cv"] == 0.0
    # But lifespan + histogram still populate.
    assert result["lifespan_days"] >= 0


def test_per_position_pnl_matches_buys_to_redeems():
    """Winner: $100 buy, $200 redeem → +$100 PnL. Loser: $50 buy, no redeem → excluded from resolved set."""
    activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "winner"},
        {"type": "REDEEM", "size": 200, "conditionId": "winner"},
        {"type": "TRADE", "side": "BUY", "usdcSize": 50, "conditionId": "openpos"},  # still open
        {"type": "TRADE", "side": "BUY", "usdcSize": 80, "conditionId": "loser"},
        # No REDEEM for "loser" — lost positions don't redeem; excluded from realized set.
    ]
    result = _per_position_pnl_metrics(activity)
    assert result["resolved_position_count"] == 1
    assert result["median_pnl"] == 100.0
    assert result["positive_share"] == 1.0


def test_per_position_pnl_handles_multiple_buys_per_condition():
    """Scaled in with 3 buys totaling $300; redeemed $450 → +$150 PnL."""
    activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "c1"},
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "c1"},
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "c1"},
        {"type": "REDEEM", "size": 450, "conditionId": "c1"},
    ]
    result = _per_position_pnl_metrics(activity)
    assert result["resolved_position_count"] == 1
    assert result["median_pnl"] == 150.0


def test_per_position_pnl_counts_fully_sold_positions_as_realized():
    """BUY then fully SELL should count as realized, even without REDEEM."""
    activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "size": 200, "price": 0.5, "conditionId": "c1"},
        {"type": "TRADE", "side": "SELL", "usdcSize": 160, "size": 200, "price": 0.8, "conditionId": "c1"},
    ]
    result = _per_position_pnl_metrics(activity)
    assert result["resolved_position_count"] == 1
    assert result["median_pnl"] == 60.0
    assert result["positive_share"] == 1.0


# ---------------------------------------------------------------------------
# Layer 2 — intent classification (runs only on Layer 1 survivors)
# ---------------------------------------------------------------------------


def test_layer2_skips_when_layer1_terminal_arb():
    assert classify_archetype_layer2(_L1_TERMINAL_ARB) is None


def test_layer2_skips_when_layer1_terminal_mm():
    assert classify_archetype_layer2(_L1_TERMINAL_MM) is None


def test_layer2_identifies_insider_shape():
    # Fresh wallet (<30d), narrow markets, heavy concentration in one market.
    features = _layer2_features(
        timing={"wallet_lifetime_days": 12, "lifespan_days": 12},
        domain={"distinct_titles": 2, "concentration": 0.9, "primary_domain": "political"},
        trade_shape={"top_condition_fill_share": 0.85, "price_repetition_ratio": 0.3,
                     "sell_ratio": 0.0, "usdc_size_cv": 2.0},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "insider_shape"
    assert result["copy_policy"] == "exclude"  # Conservative — no auto-copy on insider-shape
    assert result["confidence"] == "low"  # Always low until proper pre-event timing check exists


def test_layer2_identifies_thesis_specialist():
    # Tenured, domain-focused, holds to resolution, conviction-sized (high PnL CV).
    features = _layer2_features(
        timing={"wallet_lifetime_days": 180, "lifespan_days": 9},  # sample is short; true tenure is 6 months
        domain={"distinct_titles": 8, "concentration": 0.7, "primary_domain": "political"},
        trade_shape={"top_condition_fill_share": 0.2, "price_repetition_ratio": 0.1,
                     "sell_ratio": 0.05, "usdc_size_cv": 1.5},
        redeems={"redeem_ratio": 0.12},
        returns={"resolved_position_count": 12, "pnl_cv_abs": 0.6, "positive_share": 0.65},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "thesis_specialist"
    # Per constraint: no auto-upgrade above validated_copy on first pass
    assert result["copy_policy"] == "validated_copy"
    assert "human review" in result["notes"].lower()


def test_layer2_uses_wallet_lifetime_days_not_sample_lifespan():
    """Regression: active wallet with 9-day sample but 180-day lifetime → thesis_specialist.

    Before the lifetime_days fix, an active wallet like SwissMiss (450 trades
    in a 9-day sample but months-long true history) would fall through to
    directional_unknown because `lifespan_days` was sample-scoped. This test
    pins the fix: when `wallet_lifetime_days` says tenured, Layer 2 respects it.
    """
    features = _layer2_features(
        timing={"wallet_lifetime_days": 180, "lifespan_days": 9},
        domain={"distinct_titles": 8, "concentration": 0.7, "primary_domain": "political"},
        trade_shape={"top_condition_fill_share": 0.2, "price_repetition_ratio": 0.1,
                     "sell_ratio": 0.05, "usdc_size_cv": 1.5},
        redeems={"redeem_ratio": 0.12},
        returns={"resolved_position_count": 12, "pnl_cv_abs": 0.6},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "thesis_specialist"


def test_layer2_falls_back_to_sample_lifespan_when_lifetime_missing():
    """If wallet_lifetime_days is 0/missing (pagination failed), use sample lifespan."""
    features = _layer2_features(
        timing={"wallet_lifetime_days": 0, "lifespan_days": 120},  # wallet-level lookup missing
        domain={"distinct_titles": 8, "concentration": 0.7, "primary_domain": "political"},
        trade_shape={"top_condition_fill_share": 0.2, "price_repetition_ratio": 0.1,
                     "sell_ratio": 0.05, "usdc_size_cv": 1.5},
        redeems={"redeem_ratio": 0.12},
        returns={"resolved_position_count": 12, "pnl_cv_abs": 0.4},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "thesis_specialist"


def test_layer2_identifies_structural_bias_exploiter():
    # Consistent sizing (low CV) + high price repetition + concentrated category + long sample.
    features = _layer2_features(
        timing={"wallet_lifetime_days": 300, "lifespan_days": 300},
        domain={"distinct_titles": 30, "concentration": 0.75, "primary_domain": "political"},
        trade_shape={"top_condition_fill_share": 0.1, "price_repetition_ratio": 0.35,
                     "sell_ratio": 0.08, "usdc_size_cv": 0.3},
        redeems={"redeem_ratio": 0.08},
        returns={"resolved_position_count": 25, "pnl_cv_abs": 0.4, "positive_share": 0.6},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "structural_bias_exploiter"
    assert result["copy_policy"] == "reverse_engineer_candidate"


def test_layer2_identifies_noise():
    # Scattered markets, no concentration, tiny resolved sample.
    features = _layer2_features(
        timing={"wallet_lifetime_days": 45, "lifespan_days": 45},
        domain={"distinct_titles": 40, "concentration": 0.1, "primary_domain": "other"},
        trade_shape={"top_condition_fill_share": 0.05, "price_repetition_ratio": 0.05,
                     "sell_ratio": 0.2, "usdc_size_cv": 1.5},
        returns={"resolved_position_count": 2, "pnl_cv_abs": 0.0, "positive_share": 0.5},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "noise"
    assert result["copy_policy"] == "exclude"


def test_layer2_falls_through_to_directional_unknown():
    # Doesn't match any named Layer 2 archetype — survivor-but-unclear.
    features = _layer2_features(
        timing={"wallet_lifetime_days": 60, "lifespan_days": 60},
        domain={"distinct_titles": 10, "concentration": 0.45, "primary_domain": "sports"},
        trade_shape={"top_condition_fill_share": 0.2, "price_repetition_ratio": 0.1,
                     "sell_ratio": 0.05, "usdc_size_cv": 1.0},
        redeems={"redeem_ratio": 0.03},
        returns={"resolved_position_count": 4, "pnl_cv_abs": 0.2},
    )
    result = classify_archetype_layer2(features)
    assert result["archetype"] == "directional_unknown"
    assert result["copy_policy"] == "manual_review"


# ---------------------------------------------------------------------------
# Layer 3 — edge hypotheses (first-pass heuristic vector)
# ---------------------------------------------------------------------------


def test_edge_hypotheses_flag_structural_exploitation_for_paired_outcome_arb():
    features = _layer3_features(
        l1_archetype="paired_outcome_arb",
        position_pairing={"paired_condition_ratio": 0.7, "negative_risk_condition_ratio": 0.2},
        trade_shape={"price_repetition_ratio": 0.3, "usdc_size_cv": 0.2},
        timing={"inter_trade_interval_cv": 0.2},
    )
    result = _build_edge_hypotheses(features)
    assert result["structural_exploitation_edge"]["level"] == "high"
    assert any("paired" in reason.lower() for reason in result["structural_exploitation_edge"]["rationale"])


def test_edge_hypotheses_identify_conviction_sizing_when_variance_and_outcomes_align():
    features = _layer3_features(
        trade_shape={"trade_count": 120, "usdc_size_cv": 4.8, "top_condition_fill_share": 0.22},
        returns={"resolved_position_count": 14, "pnl_cv_abs": 0.9, "positive_share": 0.71},
    )
    result = _build_edge_hypotheses(features)
    assert result["sizing_edge"]["level"] == "high"


def test_edge_hypotheses_keep_entry_timing_unknown_without_path_level_evidence():
    features = _layer3_features(
        trade_shape={"midpoint_buy_ratio": 0.08},
        returns={"resolved_position_count": 4, "positive_share": 0.75},
    )
    result = _build_edge_hypotheses(features)
    assert result["entry_timing_edge"]["level"] == "unknown"
    assert "path" in result["entry_timing_edge"]["notes"].lower()


def test_edge_hypotheses_surface_informational_signature_conservatively():
    features = _layer3_features(
        l2_archetype="insider_shape",
        timing={"wallet_lifetime_days": 12},
        domain={"distinct_titles": 2, "concentration": 0.9},
        trade_shape={"top_condition_fill_share": 0.8},
    )
    result = _build_edge_hypotheses(features)
    assert result["informational_edge_signature"]["level"] == "medium"
    assert "not proof" in result["informational_edge_signature"]["notes"].lower()


# ---------------------------------------------------------------------------
# Layer 4 — outcome attribution
# ---------------------------------------------------------------------------


def test_outcome_attribution_breaks_realized_pnl_by_category_bucket_and_path():
    features = _layer3_features(
        returns={"resolved_position_count": 2, "positive_share": 1.0, "pnl_cv_abs": 0.76},
    )
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "usdcSize": 100,
            "price": 0.2,
            "conditionId": "tennis1",
            "title": "Miami Open: Player A vs Player B",
            "outcome": "Yes",
        },
        {
            "type": "REDEEM",
            "size": 300,
            "conditionId": "tennis1",
            "title": "Miami Open: Player A vs Player B",
            "outcome": "Yes",
        },
        {
            "type": "TRADE",
            "side": "BUY",
            "usdcSize": 100,
            "size": 200,
            "price": 0.5,
            "conditionId": "nba1",
            "title": "NBA: Lakers win",
            "outcome": "Yes",
        },
        {
            "type": "TRADE",
            "side": "SELL",
            "usdcSize": 160,
            "size": 200,
            "price": 0.8,
            "conditionId": "nba1",
            "title": "NBA: Lakers win",
            "outcome": "Yes",
        },
        {
            "type": "TRADE",
            "side": "BUY",
            "usdcSize": 50,
            "price": 0.8,
            "conditionId": "pol1",
            "title": "Will candidate X win the election?",
            "outcome": "Yes",
        },
    ]
    positions = [
        {"conditionId": "open1", "currentValue": 75, "title": "Will candidate X win the election?"},
    ]

    # Stage 5: category comes from market_tags (alphabetically first slug).
    # Inject a fake tag_map so test asserts the tag-based grouping works.
    tag_map = {
        "tennis1": frozenset({"sports-tag", "tennis-tag"}),
        "nba1":    frozenset({"nba-tag", "sports-tag"}),
        "pol1":    frozenset({"politics-tag"}),
        "open1":   frozenset({"politics-tag"}),
    }
    result = _build_outcome_attribution(features, activity, positions, tag_map=tag_map)

    assert result["status"] == "insufficient"
    assert result["resolved_position_count"] == 2
    assert result["realized_pnl_total"] == 260.0
    # Category is the alphabetically first tag per condition.
    # tennis1 → "sports-tag", nba1 → "nba-tag", open1 → "politics-tag"
    assert result["realized_pnl_by_category"]["sports-tag"]["realized_pnl"] == 200.0
    assert result["realized_pnl_by_category"]["nba-tag"]["realized_pnl"] == 60.0
    assert result["realized_pnl_by_entry_price_bucket"]["0.00-0.25"]["realized_pnl"] == 200.0
    assert result["realized_pnl_by_entry_price_bucket"]["0.40-0.60"]["realized_pnl"] == 60.0
    assert result["realized_pnl_by_hold_path"]["held_to_resolution"]["realized_pnl"] == 200.0
    assert result["realized_pnl_by_hold_path"]["sold_before_resolution"]["realized_pnl"] == 60.0
    assert result["gain_concentration"]["top_position_gain_share"] == 0.769
    assert result["outcome_structure"]["breadth"] == "insufficient"
    assert result["live_current_value_by_category"]["politics-tag"]["current_value"] == 75.0
    assert "open_book_present" in result["flags"]


def test_outcome_attribution_falls_back_when_redeem_has_blank_outcome():
    features = _layer3_features(
        returns={"resolved_position_count": 1, "positive_share": 1.0, "pnl_cv_abs": 0.0},
    )
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "usdcSize": 100,
            "price": 0.2,
            "conditionId": "tennis1",
            "title": "Savannah: Andrew Johnson vs Nishesh Basavareddy",
            "outcome": "Andrew Johnson",
        },
        {
            "type": "REDEEM",
            "size": 300,
            "usdcSize": 300,
            "conditionId": "tennis1",
            "title": "Savannah: Andrew Johnson vs Nishesh Basavareddy",
            "outcome": "",
            "asset": "",
        },
    ]

    # Stage 5: without tag_map the fill groups under "unclassified".
    result = _build_outcome_attribution(features, activity, [])

    assert result["resolved_position_count"] == 1
    assert result["realized_pnl_total"] == 200.0
    assert result["realized_pnl_by_category"]["unclassified"]["realized_pnl"] == 200.0


def test_outcome_attribution_can_label_broad_and_stable_when_gains_are_distributed():
    features = _layer3_features(
        returns={"resolved_position_count": 5, "positive_share": 0.8, "pnl_cv_abs": 0.37},
    )
    activity = []
    markets = [
        ("m1", "NBA: Lakers win", 0.42, 130),
        ("m2", "Miami Open: Player A vs Player B", 0.18, 125),
        ("m3", "Will candidate X win the election?", 0.57, 120),
        ("m4", "Bitcoin above $100k by June?", 0.61, 115),
        ("m5", "Real Madrid vs Barcelona", 0.53, 110),
    ]
    for condition_id, title, price, redeem in markets:
        activity.extend(
            [
                {
                    "type": "TRADE",
                    "side": "BUY",
                    "usdcSize": 100,
                    "price": price,
                    "conditionId": condition_id,
                    "title": title,
                    "outcome": "Yes",
                },
                {
                    "type": "REDEEM",
                    "size": redeem,
                    "conditionId": condition_id,
                    "title": title,
                    "outcome": "Yes",
                },
            ]
        )

    # Stage 5: inject tag_map so category grouping has structure to assert.
    tag_map = {
        "m1": frozenset({"sports-tag", "nba"}),
        "m2": frozenset({"sports-tag", "tennis"}),
        "m3": frozenset({"politics-tag"}),
        "m4": frozenset({"crypto-tag"}),
        "m5": frozenset({"sports-tag", "soccer"}),
    }
    result = _build_outcome_attribution(features, activity, [], tag_map=tag_map)

    assert result["status"] == "ok"
    assert result["resolved_position_count"] == 5
    assert result["outcome_structure"]["breadth"] == "broad"
    assert result["outcome_structure"]["stability"] == "stable"
    assert result["gain_concentration"]["top_position_gain_share"] < 0.35
    # 3 of the 5 markets have "sports-tag" as their alphabetically-first tag
    # (m1: nba, m2: sports-tag [sic — "sports-tag" < "tennis"], m5: soccer).
    # Actually sort: m1 [nba, sports-tag] → first=nba. m2 [sports-tag, tennis] → first=sports-tag.
    # m5 [soccer, sports-tag] → first=soccer. So groups split 1/1/1/1/1. Check sum instead.
    assert sum(b["positions"] for b in result["realized_pnl_by_category"].values()) == 5


def test_layer2_not_added_to_features_when_layer1_terminal():
    """extract_wallet_archetype should skip Layer 2 when Layer 1 excludes."""
    fake_activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "price": 0.5, "conditionId": "c1",
         "title": "political event A", "timestamp": 1_700_000_000, "outcome": "Yes"},
        # Make Layer 1 trigger paired_outcome_arb via high position pairing.
    ]
    fake_positions = [
        {"conditionId": "p1", "outcome": "Yes", "currentValue": 500, "title": "Market 1"},
        {"conditionId": "p1", "outcome": "No", "currentValue": 500, "title": "Market 1"},
        {"conditionId": "p2", "outcome": "Yes", "currentValue": 500, "title": "Market 2"},
        {"conditionId": "p2", "outcome": "No", "currentValue": 500, "title": "Market 2"},
        {"conditionId": "p3", "outcome": "Yes", "currentValue": 100, "title": "Market 3"},
    ]

    with patch("scripts.wallet_archetype_extractor._pm_iter_activity") as m_activity, \
         patch("scripts.wallet_archetype_extractor._pm_get_positions", return_value=fake_positions), \
         patch("scripts.wallet_archetype_extractor._fetch_wallet_tenure_metadata",
               return_value={"wallet_first_activity_ts": 0, "wallet_last_activity_ts": 0,
                             "wallet_lifetime_days": 0.0, "pagination_complete": True,
                             "pages_fetched": 1, "profile_joined_ts": 0,
                             "profile_lifetime_days": 0.0, "profile_tenure_source": "",
                             "wallet_lifetime_source": "unknown"}), \
         patch("scripts.wallet_archetype_extractor.fetch_wallet_lifetime_stats", return_value={}):
        m_activity.return_value = iter([type("R", (), {"raw": r})() for r in fake_activity])
        features = extract_wallet_archetype("0xtest", skip_gate0=True)

    # Layer 1 should have classified as paired_outcome_arb (high live paired ratio)
    assert features["classification"]["archetype"] == "paired_outcome_arb"
    # Layer 2 should NOT be added when Layer 1 is terminal
    assert "classification_layer2" not in features
    assert features["behavior_pattern"]["primary_pattern"]["terminal"] is True
    assert features["behavior_pattern"]["secondary_pattern"] is None
    assert features["data_quality"]["layer2_evaluated"] is False


def test_layer2_is_added_to_features_for_survivors():
    """extract_wallet_archetype should include Layer 2 when Layer 1 lets through."""
    # Build a synthetic wallet that looks like a sports specialist (Layer 1 = sports_specialist_accumulator).
    fake_activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "price": 0.58, "conditionId": "c1",
         "title": "NBA: Lakers win", "timestamp": 1_700_000_000, "outcome": "Yes"},
        {"type": "TRADE", "side": "BUY", "usdcSize": 100, "price": 0.58, "conditionId": "c2",
         "title": "NBA: Warriors win", "timestamp": 1_700_003_600, "outcome": "Yes"},
        {"type": "REDEEM", "size": 200, "conditionId": "c1", "title": "NBA: Lakers win",
         "timestamp": 1_700_007_200, "outcome": "Yes"},
    ]
    fake_positions = [
        {"conditionId": "c3", "outcome": "Yes", "currentValue": 100, "title": "NBA: Celtics win"},
    ]

    with patch("scripts.wallet_archetype_extractor._pm_iter_activity") as m_activity, \
         patch("scripts.wallet_archetype_extractor._pm_get_positions", return_value=fake_positions), \
         patch("scripts.wallet_archetype_extractor._fetch_wallet_tenure_metadata",
               return_value={"wallet_first_activity_ts": 1_700_000_000,
                             "wallet_last_activity_ts": 1_708_640_000,
                             "wallet_lifetime_days": 100.0,
                             "pagination_complete": True, "pages_fetched": 1,
                             "profile_joined_ts": 0, "profile_lifetime_days": 0.0,
                             "profile_tenure_source": "", "wallet_lifetime_source": "activity_span"}), \
         patch("scripts.wallet_archetype_extractor.fetch_wallet_lifetime_stats", return_value={}):
        m_activity.return_value = iter([type("R", (), {"raw": r})() for r in fake_activity])
        features = extract_wallet_archetype("0xtest", skip_gate0=True)

    # Layer 1 non-terminal → Layer 2 must be present
    assert features["classification"]["archetype"] not in {
        "paired_outcome_arb", "two_sided_inventory_trader"
    }
    assert "classification_layer2" in features
    assert features["behavior_pattern"]["primary_pattern"]["terminal"] is False
    assert features["behavior_pattern"]["secondary_pattern"]["provisional"] is True


def test_extract_wallet_archetype_uses_activity_client():
    """Confirm the extractor's fetch path wires through the typed activity client,
    not the legacy wallet_profiler.fetch_activity."""
    fake_activity = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 50, "price": 0.5, "conditionId": "c1",
         "title": "Tennis market", "timestamp": 1_700_000_000},
        {"type": "TRADE", "side": "BUY", "usdcSize": 60, "price": 0.5, "conditionId": "c2",
         "title": "Tennis market 2", "timestamp": 1_700_000_100},
    ]
    fake_positions = []

    with patch("scripts.wallet_archetype_extractor._pm_iter_activity") as m_activity, \
         patch("scripts.wallet_archetype_extractor._pm_get_positions", return_value=fake_positions), \
         patch("scripts.wallet_archetype_extractor._fetch_wallet_tenure_metadata",
               return_value={"wallet_first_activity_ts": 0, "wallet_last_activity_ts": 0,
                             "wallet_lifetime_days": 0.0, "pagination_complete": True,
                             "pages_fetched": 1, "profile_joined_ts": 0,
                             "profile_lifetime_days": 0.0, "profile_tenure_source": "",
                             "wallet_lifetime_source": "unknown"}), \
         patch("scripts.wallet_archetype_extractor.fetch_wallet_lifetime_stats", return_value={}):
        # iter_activity yields ActivityRow-like objects with .raw = dict.
        m_activity.return_value = iter([type("R", (), {"raw": r})() for r in fake_activity])
        features = extract_wallet_archetype("0xtest", skip_gate0=True)

    # All new feature blocks populated
    assert "timing" in features
    assert "returns" in features
    assert "usdc_size_cv" in features["trade_shape"]
    assert features["observations"]["trade_shape"]["usdc_size_cv"] == features["trade_shape"]["usdc_size_cv"]
    assert features["behavior_pattern"]["primary_pattern"]["label"] == features["classification"]["archetype"]
    assert "copy_policy" not in features["behavior_pattern"]["primary_pattern"]
    assert features["edge_hypotheses"]["market_selection_edge"]["level"] in {"low", "medium", "high", "unknown"}
    assert features["edge_hypotheses"]["entry_timing_edge"]["level"] in {"low", "medium", "high", "unknown"}
    assert "copy_policy" not in features["edge_hypotheses"]["market_selection_edge"]
    assert features["outcome_attribution"]["status"] == "insufficient"
    assert "resolved_position_count" in features["outcome_attribution"]
    assert features["operational_implications"] == {}
    assert features["data_quality"]["pagination_complete"] is True
    # Activity client was called — legacy fetch_activity was NOT
    assert m_activity.called


def test_fetch_wallet_lifetime_span_computes_true_tenure():
    """Regression for the sample-scoped lifespan bug: walking full history
    should produce a wallet_lifetime_days number based on oldest→newest
    timestamp across ALL pages, not just the first 500 rows."""
    # Simulate a wallet with 2 pages of activity spanning ~100 days.
    oldest_ts = 1_700_000_000       # 2023-11-14
    middle_ts = 1_705_000_000       # 2024-01-11
    newest_ts = 1_708_640_000       # 2024-02-22 (~100d later)

    page1 = [
        type("R", (), {"timestamp": newest_ts})(),
        type("R", (), {"timestamp": newest_ts - 3600})(),
    ] + [type("R", (), {"timestamp": middle_ts + i})() for i in range(498)]
    page2 = [type("R", (), {"timestamp": oldest_ts + i * 3600})() for i in range(50)]

    call_count = {"n": 0}
    def fake_get_activity_page(wallet, limit=500, offset=0):
        call_count["n"] += 1
        if offset == 0:
            return page1
        if offset == 500:
            return page2
        return []

    from scripts.wallet_archetype_extractor import _fetch_wallet_lifetime_span
    with patch("scripts.wallet_archetype_extractor.get_activity_page", side_effect=fake_get_activity_page):
        result = _fetch_wallet_lifetime_span("0xtest")

    assert result["wallet_first_activity_ts"] == oldest_ts
    assert result["wallet_last_activity_ts"] == newest_ts
    # ~100 days of span, not the 9 days that sample-scoped computation would produce.
    assert result["wallet_lifetime_days"] > 99
    assert result["pagination_complete"] is True
    assert call_count["n"] == 2  # full-span pagination hit 2 pages then stopped on partial


def test_fetch_wallet_lifetime_span_handles_polymarket_offset_cap():
    """Polymarket API caps offset at 3000; returns 400 'max historical activity
    offset exceeded'. The helper treats this as end-of-reachable-history, not
    a fatal error — marks pagination_complete=False but still returns valid
    lifetime_days for the rows it did see."""
    from data.polymarket_activity_client import PolymarketActivityError
    rows = [type("R", (), {"timestamp": 1_700_000_000 + i * 3600})() for i in range(500)]
    call_count = {"n": 0}
    def fake_page(wallet, limit=500, offset=0):
        call_count["n"] += 1
        # First 2 pages return full batches; offset=1000 raises the 400.
        if offset >= 1000:
            raise PolymarketActivityError(
                "HTTP 400 on https://...: max historical activity offset of 3000 exceeded"
            )
        return rows

    with patch("scripts.wallet_archetype_extractor.get_activity_page", side_effect=fake_page):
        from scripts.wallet_archetype_extractor import _fetch_wallet_lifetime_span
        result = _fetch_wallet_lifetime_span("0xtest", max_pages=10)

    assert result["pagination_complete"] is False  # hit API cap, history extends further
    assert result["wallet_lifetime_days"] > 0       # but we still computed what we saw
    assert call_count["n"] == 3                     # 2 successful pages + 1 that raised


def test_fetch_wallet_lifetime_span_flags_incomplete_on_max_pages():
    """If pagination hits max_pages without seeing a partial/empty page,
    returned lifetime_days is a LOWER bound and pagination_complete=False."""
    # Every page returns a full 500-row batch — simulate a wallet with more
    # history than our max_pages limit can walk.
    rows = [type("R", (), {"timestamp": 1_700_000_000 + i})() for i in range(500)]
    with patch("scripts.wallet_archetype_extractor.get_activity_page", return_value=rows):
        from scripts.wallet_archetype_extractor import _fetch_wallet_lifetime_span
        result = _fetch_wallet_lifetime_span("0xtest", max_pages=3)

    assert result["pagination_complete"] is False
    assert result["pages_fetched"] == 3


def test_fetch_wallet_tenure_metadata_uses_profile_joined_when_older_than_activity_span():
    """Hyper-active wallets can hit the activity API cap; profile join date should win."""
    with patch(
        "scripts.wallet_archetype_extractor._fetch_wallet_lifetime_span",
        return_value={
            "wallet_first_activity_ts": 1_700_000_000,
            "wallet_last_activity_ts": 1_701_800_000,
            "wallet_lifetime_days": 20.75,
            "pagination_complete": False,
            "pages_fetched": 6,
        },
    ), patch(
        "scripts.wallet_archetype_extractor._fetch_wallet_profile_tenure",
        return_value={
            "profile_joined_ts": 1_706_000_000,
            "profile_lifetime_days": 400.0,
            "profile_tenure_source": "polymarket_profile_next_data",
        },
    ):
        result = _fetch_wallet_tenure_metadata("0xtest")

    assert result["wallet_lifetime_days"] == 400.0
    assert result["wallet_lifetime_source"] == "profile_joined"
    assert result["pagination_complete"] is False
    assert result["profile_joined_ts"] == 1_706_000_000


def test_fetch_wallet_tenure_metadata_falls_back_to_activity_span_when_profile_missing():
    with patch(
        "scripts.wallet_archetype_extractor._fetch_wallet_lifetime_span",
        return_value={
            "wallet_first_activity_ts": 1_700_000_000,
            "wallet_last_activity_ts": 1_708_640_000,
            "wallet_lifetime_days": 100.0,
            "pagination_complete": True,
            "pages_fetched": 2,
        },
    ), patch(
        "scripts.wallet_archetype_extractor._fetch_wallet_profile_tenure",
        return_value={
            "profile_joined_ts": 0,
            "profile_lifetime_days": 0.0,
            "profile_tenure_source": "",
        },
    ):
        result = _fetch_wallet_tenure_metadata("0xtest")

    assert result["wallet_lifetime_days"] == 100.0
    assert result["wallet_lifetime_source"] == "activity_span"
    assert result["pagination_complete"] is True


# Stage 5 removed the keyword-based _domain_horizon_metrics /
# _realized_pnl_horizon_view helpers and the `domain_horizons` output
# field. The tag-based replacements (_domain_tags_horizon_metrics,
# domain_tags_horizons) are covered by tests/test_wallet_taxonomy_stage2.py.
