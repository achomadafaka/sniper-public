"""Seed-rule contract tests for Strategy Pattern Audit v1."""
from __future__ import annotations

from sniper.analysis.wallet_protocol.types import ProtocolReport


EXPECTED_RULE_IDS = [
    "category_specialist_v1",
    "held_prediction_edge_v1",
    "pre_resolution_exit_discipline_v1",
    "gradual_accumulation_pattern_v1",
    "front_loaded_conviction_v1",
    "liquidity_opportunist_needs_data_v1",
    "timing_capture_edge_v1",
    "noise_or_luck_rejection_v1",
]


def _base_report(wallet: str = "0x1") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        overall_verdict="data_insufficient",
        gate_result="reject_insufficient_sample",
        trading_mode="n/a_routing",
        n_closed_positions=12,
        archetype_layer1="directional",
    )


def test_default_seed_rules_are_exact_spec_set() -> None:
    from sniper.analysis.strategy_pattern_audit import default_seed_rules

    rules = default_seed_rules()

    assert [rule.rule_id for rule in rules] == EXPECTED_RULE_IDS
    assert len({rule.rule_id for rule in rules}) == 8
    for rule in rules:
        assert rule.title
        assert rule.description
        assert rule.mechanism_category
        assert rule.surfaces_consumed
        assert rule.false_positive_risks
        assert rule.simulation_dataset_requirements
        assert callable(rule.coverage_predicate)
        assert callable(rule.anti_pattern_predicate)
        assert callable(rule.pattern_predicate)
        assert callable(rule.evidence_builder)


def test_category_specialist_rule_matches_concentrated_category_edge() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["category_specialist_v1"]
    report = _base_report()
    report.outcome_side_breakdown = {
        "category_clusters": [
            {"category": "politics", "pnl": 900.0, "total_pnl": 1000.0, "n": 10}
        ]
    }
    report.extractor_features_summary = {"top_category": "politics"}
    report.data_completeness = {"quality": "partial", "caveats": []}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True
    evidence = rule.evidence_builder(report)
    assert evidence[0]["surface"] == "outcome_side_breakdown"
    assert evidence[0]["path"] == "category_clusters[0].category"


def test_held_prediction_edge_rule_requires_held_profit_and_timing_quality() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["held_prediction_edge_v1"]
    report = _base_report()
    report.exit_attribution = {
        "coverage": {"n_with_resolved_attribution": 8},
        "held_to_resolution": {"pnl": 700.0, "share_of_total_pnl": 0.70},
    }
    report.held_analyst_branch = {"hypotheses": [{"id": "H2", "verdict": "supported"}]}
    report.timing_surface = {
        "confidence_band": "partial",
        "classification": "holding_driven",
    }
    report.data_completeness = {"quality": "partial", "caveats": []}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True
    assert any(ref["path"] == "classification" for ref in rule.evidence_builder(report))


def test_held_prediction_edge_rule_rejects_trading_driven_held_profit() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["held_prediction_edge_v1"]
    report = _base_report()
    report.exit_attribution = {
        "coverage": {"n_with_resolved_attribution": 8},
        "held_to_resolution": {"pnl": 700.0, "share_of_total_pnl": 0.70},
    }
    report.timing_surface = {
        "confidence_band": "partial",
        "classification": "trading_driven",
    }
    report.data_completeness = {"quality": "partial", "caveats": []}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is False
    assert rule.anti_pattern_predicate(report, {}) is True


def test_pre_resolution_exit_discipline_rule_matches_profitable_exits() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}[
        "pre_resolution_exit_discipline_v1"
    ]
    report = _base_report()
    report.exit_attribution = {
        "coverage": {"n_with_resolved_attribution": 7},
        "profitable_pre_resolution_exit": {
            "pnl": 650.0,
            "share_of_total_pnl": 0.65,
        },
    }
    report.execution_quality_summary = {"n_exits": 7}
    report.timing_surface = {
        "confidence_band": "partial",
        "classification": "trading_driven",
    }

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True


def _with_position_builder(report: ProtocolReport, pattern: str) -> ProtocolReport:
    report.position_building_surface = {
        "wallet_summary": {
            "share_small_buy_accumulation": 0.40,
            "dominant_accumulation_pattern": pattern,
            "observation_window_coverage": 0.80,
        },
        "observation_window_quality_distribution": {"partial": {"n": 8, "share": 0.80}},
        "source_quality_distribution": {"activity_rows_complete": {"n": 8, "share": 0.80}},
        "identity_quality_distribution": {"stable_event_uid": {"n": 8, "share": 0.80}},
    }
    report.data_completeness = {"quality": "partial", "caveats": []}
    return report


def test_gradual_accumulation_rule_matches_position_builder_pattern() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}[
        "gradual_accumulation_pattern_v1"
    ]
    report = _with_position_builder(_base_report(), "gradual_accumulation")

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True


def test_front_loaded_conviction_rule_matches_front_loaded_pattern() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["front_loaded_conviction_v1"]
    report = _with_position_builder(_base_report(), "front_loaded")
    report.outcome_side_breakdown = {"total_pnl": 1000.0}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True


def test_liquidity_rule_declares_blocking_data_needs() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}[
        "liquidity_opportunist_needs_data_v1"
    ]

    assert rule.needs_data == ["order_book_depth", "slippage_model"]
    assert "order_book_depth" in rule.simulation_dataset_requirements


def test_timing_capture_rule_matches_trading_driven_timing() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["timing_capture_edge_v1"]
    report = _base_report()
    report.timing_surface = {
        "confidence_band": "partial",
        "classification": "trading_driven",
    }
    report.execution_quality_summary = {"n_entries": 8}
    report.data_completeness = {"quality": "partial", "caveats": []}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True


def test_noise_or_luck_rule_rejects_concentrated_underpowered_wallet() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["noise_or_luck_rejection_v1"]
    report = _base_report()
    report.n_closed_positions = 2
    report.timing_surface = {"confidence_band": "underpowered"}
    report.outcome_side_breakdown = {"top_market_pnl_share": 0.90}

    assert rule.anti_pattern_predicate(report, {}) is True
    assert rule.promotable is False
