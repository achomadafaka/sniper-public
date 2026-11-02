"""Seed Strategy Pattern Audit rules."""
from __future__ import annotations

from typing import Any

from sniper.analysis.strategy_pattern_audit.rules import ThesisRule, evidence_ref


def default_seed_rules() -> list[ThesisRule]:
    return [
        _category_specialist_rule(),
        _held_prediction_rule(),
        _pre_resolution_exit_rule(),
        _gradual_accumulation_rule(),
        _front_loaded_conviction_rule(),
        _liquidity_needs_data_rule(),
        _timing_capture_rule(),
        _noise_or_luck_rule(),
    ]


def _category_specialist_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="category_specialist_v1",
        title="Category specialist",
        mechanism_category="category_specialist",
        description="Returns are concentrated in one market category.",
        surfaces_consumed=["outcome_side_breakdown", "data_completeness"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["category concentration can be sample noise"],
        simulation_dataset_requirements=["market_metadata", "category_labels"],
        coverage_predicate=lambda report, priors: bool(
            getattr(report, "outcome_side_breakdown", None)
            and getattr(report, "data_completeness", None)
        ),
        anti_pattern_predicate=lambda report, priors: _category_concentration(report)
        is not None
        and _category_concentration(report) < 0.60,
        pattern_predicate=lambda report, priors: (
            (_first_category_cluster(report) or {}).get("n", 0) >= 5
            and (_category_concentration(report) or 0.0) >= 0.60
        ),
        evidence_builder=lambda report: [
            evidence_ref(
                surface="outcome_side_breakdown",
                path="category_clusters[0].category",
                value=(_first_category_cluster(report) or {}).get("category"),
                predicate="share_gte",
                threshold=0.60,
            )
        ],
    )


def _held_prediction_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="held_prediction_edge_v1",
        title="Held prediction edge",
        mechanism_category="holding_prediction_edge",
        description="Resolved-position profit is concentrated in held outcomes.",
        surfaces_consumed=["exit_attribution", "timing_surface", "data_completeness"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["held winners can be concentrated in few markets"],
        simulation_dataset_requirements=["resolution_outcomes", "market_metadata"],
        coverage_predicate=lambda report, priors: _exit_n(report) >= 5
        and bool(getattr(report, "timing_surface", None)),
        anti_pattern_predicate=lambda report, priors: _held_share(report) is not None
        and (
            _held_share(report) < 0.25
            or _timing_classification(report) == "trading_driven"
        ),
        pattern_predicate=lambda report, priors: (_held_share(report) or 0.0) >= 0.50
        and (_held_pnl(report) or 0.0) > 0
        and _timing_classification(report) == "holding_driven"
        and _timing_quality(report) not in {"underpowered", "unavailable"},
        evidence_builder=lambda report: [
            evidence_ref(
                surface="exit_attribution",
                path="held_to_resolution.share_of_total_pnl",
                value=_held_share(report),
                predicate="gte",
                threshold=0.50,
            ),
            evidence_ref(
                surface="timing_surface",
                path="classification",
                value=_timing_classification(report),
                predicate="eq",
                threshold="holding_driven",
            ),
        ],
    )


def _pre_resolution_exit_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="pre_resolution_exit_discipline_v1",
        title="Pre-resolution exit discipline",
        mechanism_category="exit_discipline_edge",
        description="Profit is concentrated in profitable pre-resolution exits.",
        surfaces_consumed=["exit_attribution", "timing_surface"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["exit profit can reflect one-off event timing"],
        simulation_dataset_requirements=["position_exits", "market_timestamps"],
        coverage_predicate=lambda report, priors: _exit_n(report) >= 5,
        anti_pattern_predicate=lambda report, priors: _pre_exit_share(report)
        is not None
        and _pre_exit_share(report) < 0.25,
        pattern_predicate=lambda report, priors: (_pre_exit_share(report) or 0.0)
        >= 0.50
        and (_pre_exit_pnl(report) or 0.0) > 0,
        evidence_builder=lambda report: [
            evidence_ref(
                surface="exit_attribution",
                path="profitable_pre_resolution_exit.share_of_total_pnl",
                value=_pre_exit_share(report),
                predicate="gte",
                threshold=0.50,
            )
        ],
    )


def _gradual_accumulation_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="gradual_accumulation_pattern_v1",
        title="Gradual accumulation pattern",
        mechanism_category="accumulation_pattern_edge",
        description="Positions are often built through gradual accumulation.",
        surfaces_consumed=["position_building_surface", "data_completeness"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["gradual builds can be caused by incomplete capture"],
        simulation_dataset_requirements=["fill_sequence", "market_metadata"],
        coverage_predicate=lambda report, priors: _observation_coverage(report) >= 0.50,
        anti_pattern_predicate=lambda report, priors: _dominant_pattern(report)
        not in {None, "gradual_accumulation"},
        pattern_predicate=lambda report, priors: _dominant_pattern(report)
        == "gradual_accumulation",
        evidence_builder=lambda report: [
            evidence_ref(
                surface="position_building_surface",
                path="wallet_summary.dominant_accumulation_pattern",
                value=_dominant_pattern(report),
                predicate="eq",
                threshold="gradual_accumulation",
            )
        ],
    )


def _front_loaded_conviction_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="front_loaded_conviction_v1",
        title="Front-loaded conviction",
        mechanism_category="sizing_conviction_edge",
        description="Positions are often built quickly near first entry.",
        surfaces_consumed=["position_building_surface", "outcome_side_breakdown"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["front-loaded sizing can be bankroll noise"],
        simulation_dataset_requirements=["fill_sequence", "position_notional"],
        coverage_predicate=lambda report, priors: _observation_coverage(report) >= 0.50
        and bool(getattr(report, "outcome_side_breakdown", None)),
        anti_pattern_predicate=lambda report, priors: _dominant_pattern(report)
        not in {None, "front_loaded"},
        pattern_predicate=lambda report, priors: _dominant_pattern(report)
        == "front_loaded",
        evidence_builder=lambda report: [
            evidence_ref(
                surface="position_building_surface",
                path="wallet_summary.dominant_accumulation_pattern",
                value=_dominant_pattern(report),
                predicate="eq",
                threshold="front_loaded",
            )
        ],
    )


def _liquidity_needs_data_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="liquidity_opportunist_needs_data_v1",
        title="Liquidity opportunity needs data",
        mechanism_category="liquidity_opportunist",
        description="Liquidity opportunity cannot be assessed from current surfaces alone.",
        surfaces_consumed=["position_building_surface"],
        operator_priors_used=["min_population_evaluated", "min_supporting"],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=["order_book_depth", "slippage_model"],
        false_positive_risks=["liquidity behavior is invisible without order-book data"],
        simulation_dataset_requirements=["order_book_depth", "slippage_model"],
        coverage_predicate=lambda report, priors: bool(
            getattr(report, "position_building_surface", None)
        ),
        anti_pattern_predicate=lambda report, priors: False,
        pattern_predicate=lambda report, priors: False,
        evidence_builder=lambda report: [
            evidence_ref(
                surface="position_building_surface",
                path="wallet_summary.observation_window_coverage",
                value=_observation_coverage(report),
                predicate="observed",
            )
        ],
    )


def _timing_capture_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="timing_capture_edge_v1",
        title="Timing capture edge",
        mechanism_category="timing_capture_edge",
        description="Captured travel is consistent with trading-driven timing.",
        surfaces_consumed=["timing_surface", "data_completeness"],
        operator_priors_used=[
            "min_population_evaluated",
            "min_supporting",
            "min_observation_quality_share",
            "min_archetype_diversity",
        ],
        forbidden_archetypes=[],
        minimum_n_per_wallet=5,
        needs_data=[],
        false_positive_risks=["captured timing can be underpowered"],
        simulation_dataset_requirements=["price_history", "fill_sequence"],
        coverage_predicate=lambda report, priors: bool(
            getattr(report, "timing_surface", None)
        ),
        anti_pattern_predicate=lambda report, priors: _timing_classification(report)
        in {"holding_driven", "underpowered"},
        pattern_predicate=lambda report, priors: _timing_classification(report)
        in {"trading_driven", "mixed"}
        and _timing_quality(report) not in {"underpowered", "unavailable"},
        evidence_builder=lambda report: [
            evidence_ref(
                surface="timing_surface",
                path="classification",
                value=_timing_classification(report),
                predicate="in",
                threshold=["trading_driven", "mixed"],
            )
        ],
    )


def _noise_or_luck_rule() -> ThesisRule:
    return ThesisRule(
        rule_id="noise_or_luck_rejection_v1",
        title="Noise or luck rejection",
        mechanism_category="noise_or_luck",
        description="Evidence is too concentrated or underpowered to form a thesis.",
        surfaces_consumed=["timing_surface", "outcome_side_breakdown"],
        operator_priors_used=["min_population_evaluated", "min_supporting"],
        forbidden_archetypes=[],
        minimum_n_per_wallet=1,
        needs_data=[],
        false_positive_risks=["weak evidence can look like a pattern"],
        simulation_dataset_requirements=["larger_wallet_population"],
        coverage_predicate=lambda report, priors: True,
        anti_pattern_predicate=lambda report, priors: (
            (getattr(report, "n_closed_positions", 0) or 0) < 5
            or _timing_quality(report) == "underpowered"
            or _top_market_share(report) >= 0.80
        ),
        pattern_predicate=lambda report, priors: False,
        evidence_builder=lambda report: [
            evidence_ref(
                surface="outcome_side_breakdown",
                path="top_market_pnl_share",
                value=_top_market_share(report),
                predicate="lt",
                threshold=0.80,
            )
        ],
        promotable=False,
    )


def _first_category_cluster(report: Any) -> dict[str, Any] | None:
    clusters = (getattr(report, "outcome_side_breakdown", None) or {}).get(
        "category_clusters"
    ) or []
    return clusters[0] if clusters else None


def _category_concentration(report: Any) -> float | None:
    cluster = _first_category_cluster(report)
    if not cluster:
        return None
    total = abs(float(cluster.get("total_pnl") or 0.0))
    return abs(float(cluster.get("pnl") or 0.0)) / max(total, 1.0)


def _exit_n(report: Any) -> int:
    coverage = (getattr(report, "exit_attribution", None) or {}).get("coverage") or {}
    return int(coverage.get("n_with_resolved_attribution") or 0)


def _held_share(report: Any) -> float | None:
    held = (getattr(report, "exit_attribution", None) or {}).get(
        "held_to_resolution"
    ) or {}
    return held.get("share_of_total_pnl")


def _held_pnl(report: Any) -> float | None:
    held = (getattr(report, "exit_attribution", None) or {}).get(
        "held_to_resolution"
    ) or {}
    return held.get("pnl")


def _pre_exit_share(report: Any) -> float | None:
    row = (getattr(report, "exit_attribution", None) or {}).get(
        "profitable_pre_resolution_exit"
    ) or {}
    return row.get("share_of_total_pnl")


def _pre_exit_pnl(report: Any) -> float | None:
    row = (getattr(report, "exit_attribution", None) or {}).get(
        "profitable_pre_resolution_exit"
    ) or {}
    return row.get("pnl")


def _timing_quality(report: Any) -> str | None:
    return (getattr(report, "timing_surface", None) or {}).get("confidence_band")


def _timing_classification(report: Any) -> str | None:
    return (getattr(report, "timing_surface", None) or {}).get("classification")


def _position_summary(report: Any) -> dict[str, Any]:
    return (getattr(report, "position_building_surface", None) or {}).get(
        "wallet_summary"
    ) or {}


def _dominant_pattern(report: Any) -> str | None:
    return _position_summary(report).get("dominant_accumulation_pattern")


def _observation_coverage(report: Any) -> float:
    return float(_position_summary(report).get("observation_window_coverage") or 0.0)


def _top_market_share(report: Any) -> float:
    return float(
        (getattr(report, "outcome_side_breakdown", None) or {}).get(
            "top_market_pnl_share"
        )
        or 0.0
    )
