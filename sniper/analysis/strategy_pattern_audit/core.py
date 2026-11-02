"""Pure Strategy Pattern Audit evaluator."""
from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Mapping, Sequence

from sniper.analysis.strategy_pattern_audit.rules import (
    INCONCLUSIVE,
    MATCHES,
    REJECTED,
    RuleEvaluation,
    ThesisRule,
    data_need,
    normalize_operator_priors,
    unique_sorted,
)


SURFACE_VERSION = "strategy_pattern_audit_v1"

QUALITY_ORDER = {
    "unavailable": 0,
    "insufficient": 0,
    "poor": 1,
    "low": 1,
    "underpowered": 1,
    "partial": 2,
    "medium": 2,
    "good": 3,
    "high": 3,
}
PARTIAL_OR_BETTER = {"partial", "medium", "good", "high"}
DIRECTIONAL_MECHANISMS = {
    "market_selection_edge",
    "category_specialist",
    "holding_prediction_edge",
    "exit_discipline_edge",
    "timing_capture_edge",
    "accumulation_pattern_edge",
    "sizing_conviction_edge",
}
SURFACE_NAMES = [
    "data_completeness",
    "timing_surface",
    "position_building_surface",
    "exit_attribution",
    "edge_surface",
]


def compute_strategy_pattern_audit(
    reports: Sequence[Any],
    *,
    rules: Sequence[ThesisRule] | None = None,
    audit_run_id: str | None = None,
    operator_priors: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Evaluate strategy-thesis rules over already-computed reports."""

    reports = list(reports)
    if not reports:
        return None

    priors, applied_priors = normalize_operator_priors(operator_priors)
    excluded_wallets = set(priors.get("exclude_wallets") or [])
    included_reports = [r for r in reports if _wallet(r) not in excluded_wallets]

    if rules is None:
        from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

        rules = default_seed_rules()
    rules = list(rules)

    population_quality = _population_quality(included_reports)
    result: dict[str, Any] = {
        "audit_run_id": audit_run_id,
        "surface_version": SURFACE_VERSION,
        "generated_from": {
            "n_reports": len(reports),
            "n_reports_excluded_by_priors": len(reports) - len(included_reports),
            "operator_priors_applied": applied_priors,
            "report_protocol_versions": dict(
                sorted(Counter(_protocol_version(r) for r in reports).items())
            ),
        },
        "population_quality": population_quality,
        "theses": [],
        "rejected_theses": [],
        "data_gaps": [],
        "top_level_caveats": [],
    }

    if not rules:
        result["top_level_caveats"] = ["NO_RULES_EVALUATED"]
        return result

    gap_rows: list[tuple[str, str, bool, dict[str, Any]]] = []
    for rule in rules:
        evaluations = [_evaluate_rule(report, rule, priors) for report in included_reports]
        row = _aggregate_rule(rule, evaluations, priors, applied_priors, population_quality)
        for need in row.get("data_needs", []):
            gap_rows.append((need["field_name"], row["rule_id"], need["blocking"], need))
        if row["honesty_bar"]["passed"]:
            result["theses"].append(row)
        else:
            result["rejected_theses"].append(row)

    result["data_gaps"] = _consolidate_data_gaps(gap_rows)
    return result


def _honesty_floor_caveats(report: Any) -> list[str]:
    """Return honesty-floor flags that must block MATCHES for this report.

    Only fires when at least one ``BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY:`` entry
    is present. When it does, returns that entry (or entries) PLUS any
    ``NEGATIVE_PNL_EDGE_UNEXPLAINED`` flag in the same ``data_quality_flags``
    list, so both surface together in ``coverage_caveats`` (spec §31).

    Returns ``[]`` when no tag-grain flag is present — preserving pre-existing
    behavior for reports that carry only ``NEGATIVE_PNL_EDGE_UNEXPLAINED``
    (spec §30: that flag alone adds no SPA semantics).
    """
    flags = list(getattr(report, "data_quality_flags", None) or [])
    tag_grain_flags = [f for f in flags if f.startswith("BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY:")]
    if not tag_grain_flags:
        return []
    neg_pnl_flags = [f for f in flags if f == "NEGATIVE_PNL_EDGE_UNEXPLAINED"]
    return tag_grain_flags + neg_pnl_flags


def _evaluate_rule(report: Any, rule: ThesisRule, priors: dict[str, Any]) -> RuleEvaluation:
    wallet = _wallet(report)
    archetype = getattr(report, "archetype_layer1", None)
    surface_caveats = _surface_quality_caveats(report, rule.surfaces_consumed)
    honesty_floor_caveats = _honesty_floor_caveats(report)
    caveats = surface_caveats + honesty_floor_caveats
    if caveats:
        reason = "tag_grain_only_evidence" if honesty_floor_caveats else "coverage"
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=INCONCLUSIVE,
            reason=reason,
            coverage_caveats=caveats,
            archetype_layer1=archetype,
            surface_quality="unavailable" if surface_caveats else _overall_surface_quality(report, rule.surfaces_consumed),
        )

    if not rule.coverage_predicate(report, priors):
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=INCONCLUSIVE,
            reason="coverage",
            evidence_refs=_safe_evidence(rule, report),
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    if (getattr(report, "n_closed_positions", 0) or 0) < max(
        rule.minimum_n_per_wallet, priors["min_n_closed_positions_per_wallet"]
    ):
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=INCONCLUSIVE,
            reason="sample_size",
            evidence_refs=_safe_evidence(rule, report),
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    needs = _rule_data_needs(rule)
    blocking_needs = [need for need in needs if need["blocking"]]
    if rule.anti_pattern_predicate(report, priors):
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=REJECTED,
            reason="anti-pattern",
            evidence_refs=_safe_evidence(rule, report),
            data_needs=needs,
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    forbidden = set(rule.forbidden_archetypes)
    if rule.mechanism_category in DIRECTIONAL_MECHANISMS:
        forbidden.update(priors.get("forbidden_archetypes_for_directional_theses") or [])
    if archetype in forbidden:
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=REJECTED,
            reason="forbidden_archetype",
            evidence_refs=_safe_evidence(rule, report),
            data_needs=needs,
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    if blocking_needs:
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=INCONCLUSIVE,
            reason="data_gap",
            evidence_refs=_safe_evidence(rule, report),
            data_needs=needs,
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    if rule.pattern_predicate(report, priors):
        return RuleEvaluation(
            wallet=wallet,
            rule_id=rule.rule_id,
            state=MATCHES,
            reason="match",
            evidence_refs=_safe_evidence(rule, report),
            data_needs=needs,
            archetype_layer1=archetype,
            surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
        )

    return RuleEvaluation(
        wallet=wallet,
        rule_id=rule.rule_id,
        state=REJECTED,
        reason="no_match",
        evidence_refs=_safe_evidence(rule, report),
        archetype_layer1=archetype,
        surface_quality=_overall_surface_quality(report, rule.surfaces_consumed),
    )


def _aggregate_rule(
    rule: ThesisRule,
    evaluations: list[RuleEvaluation],
    priors: dict[str, Any],
    applied_priors: dict[str, dict[str, Any]],
    population_quality: dict[str, Any],
) -> dict[str, Any]:
    supporting = [e for e in evaluations if e.state == MATCHES]
    rejected = [e for e in evaluations if e.state == REJECTED]
    inconclusive = [e for e in evaluations if e.state == INCONCLUSIVE]
    anti_examples = [e for e in rejected if e.reason == "anti-pattern"]
    n_evaluable = len(supporting) + len(rejected)
    support_share = len(supporting) / n_evaluable if n_evaluable else None

    data_needs = _unique_data_needs(
        [need for e in evaluations for need in e.data_needs] or _rule_data_needs(rule)
    )
    blocking_data_fields = [need["field_name"] for need in data_needs if need["blocking"]]

    quality_supporters = [
        e for e in supporting if e.surface_quality in PARTIAL_OR_BETTER
    ]
    quality_share = (
        len(quality_supporters) / len(supporting) if supporting else None
    )
    archetypes = {e.archetype_layer1 for e in supporting if e.archetype_layer1}

    fail_reasons: list[str] = []
    if not rule.promotable:
        fail_reasons.append("descriptive_rejection_rule")
    elif blocking_data_fields:
        fail_reasons.append("blocking_data_gap")
    else:
        if n_evaluable < priors["min_population_evaluated"]:
            fail_reasons.append("population_floor")
        if len(supporting) < priors["min_supporting"]:
            fail_reasons.append("support_floor")
        if quality_share is None or quality_share < priors["min_observation_quality_share"]:
            fail_reasons.append("quality_floor")
        if len(supporting) >= 5 and not anti_examples:
            fail_reasons.append("anti_pattern_symmetry")
        if len(archetypes) < priors["min_archetype_diversity"]:
            fail_reasons.append("archetype_independence")
        share = population_quality.get("share_with_partial_or_better")
        if share is not None and share < priors["min_share_with_partial_or_better"]:
            fail_reasons.append("population_quality_too_low")

    honesty_bar = {
        "n_min_supporting_required": priors["min_supporting"],
        "min_observation_quality_share_required": priors[
            "min_observation_quality_share"
        ],
        "min_population_evaluated": priors["min_population_evaluated"],
        "passed": not fail_reasons,
        "fail_reasons": fail_reasons,
    }
    base = {
        "rule_id": rule.rule_id,
        "title": rule.title,
        "description": rule.description,
        "mechanism_category": rule.mechanism_category,
        "n_supporting": len(supporting),
        "n_inconclusive": len(inconclusive),
        "n_rejected": len(rejected),
        "n_evaluable": n_evaluable,
        "support_share": support_share,
        "minimum_n_per_wallet": rule.minimum_n_per_wallet,
        "operator_priors_applied": {
            key: applied_priors[key] for key in rule.operator_priors_used if key in applied_priors
        },
        "honesty_bar": honesty_bar,
        "data_needs": data_needs,
        "fields_provenance": {
            "supporting_wallets": "factual",
            "anti_pattern_examples": "factual",
            "support_share": "factual" if support_share is not None else "unavailable",
        },
        "caveats": unique_sorted(
            [c for e in evaluations for c in e.coverage_caveats]
        ),
        "anti_pattern_examples": [
            {
                "wallet": e.wallet,
                "reason": e.reason,
                "evidence_refs": e.evidence_refs,
            }
            for e in anti_examples
        ],
    }
    if honesty_bar["passed"]:
        base["supporting_wallets"] = [
            {
                "wallet": e.wallet,
                "evidence_refs": e.evidence_refs,
                "coverage_caveats": e.coverage_caveats,
            }
            for e in supporting
        ]
    else:
        base["fail_reasons"] = fail_reasons
        base["would_have_been_promoted_at"] = {
            "n_supporting": priors["min_supporting"],
            "data_fields": blocking_data_fields,
        } if rule.promotable else None
    return base


def _population_quality(reports: Sequence[Any]) -> dict[str, Any]:
    n = len(reports)
    n_closed = [getattr(r, "n_closed_positions", 0) or 0 for r in reports]
    n_open = [getattr(r, "n_open_positions", 0) or 0 for r in reports]
    partial_or_better = []
    activity_cap_hit = []
    subgraph = []
    walk = []
    surface_dist: dict[str, dict[str, int]] = {name: {} for name in SURFACE_NAMES}
    for report in reports:
        data_quality = _data_completeness_quality_label(report) or "unavailable"
        partial_or_better.append(data_quality in PARTIAL_OR_BETTER)
        if hasattr(report, "activity_cap_hit"):
            activity_cap_hit.append(bool(getattr(report, "activity_cap_hit")))
        if hasattr(report, "subgraph_fallback_used"):
            subgraph.append(bool(getattr(report, "subgraph_fallback_used")))
        if hasattr(report, "walk_full_history"):
            walk.append(bool(getattr(report, "walk_full_history")))
        for surface in SURFACE_NAMES:
            quality = _surface_quality_label(report, surface) or "unavailable"
            surface_dist[surface][quality] = surface_dist[surface].get(quality, 0) + 1

    return {
        "share_with_partial_or_better": _share(partial_or_better),
        "share_with_activity_cap_hit": _share(activity_cap_hit),
        "share_with_subgraph_fallback_used": _share(subgraph),
        "share_with_walk_full_history": _share(walk),
        "median_n_closed_positions": int(median(n_closed)) if n_closed else None,
        "median_n_open_positions": int(median(n_open)) if n_open else None,
        "archetype_layer1_distribution": dict(
            sorted(Counter(getattr(r, "archetype_layer1", None) or "unknown" for r in reports).items())
        ),
        "data_quality_flag_distribution": dict(
            sorted(Counter(flag for r in reports for flag in (getattr(r, "data_quality_flags", None) or [])).items())
        ),
        "n_excluded_by_forbidden_archetypes": 0,
        "n_excluded_by_zero_n_closed_positions": sum(1 for value in n_closed if value == 0),
        "surface_quality_distribution": surface_dist if n else {name: {} for name in SURFACE_NAMES},
    }


def _consolidate_data_gaps(
    rows: list[tuple[str, str, bool, dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_field: dict[str, dict[str, Any]] = {}
    for field_name, rule_id, blocking, need in rows:
        row = by_field.setdefault(
            field_name,
            {
                "field_name": field_name,
                "blocking_n_theses": 0,
                "partial_n_theses": 0,
                "blocked_thesis_ids": [],
                "partial_thesis_ids": [],
                "candidate_sources": [],
                "operator_priority_hint": None,
            },
        )
        if blocking:
            row["blocking_n_theses"] += 1
            row["blocked_thesis_ids"].append(rule_id)
        else:
            row["partial_n_theses"] += 1
            row["partial_thesis_ids"].append(rule_id)
        for source in need.get("candidate_sources") or []:
            if source not in row["candidate_sources"]:
                row["candidate_sources"].append(source)
    return sorted(
        by_field.values(),
        key=lambda row: (
            -row["blocking_n_theses"],
            -int(row["blocking_n_theses"] > 0),
            row["field_name"],
        ),
    )


def _surface_quality_caveats(report: Any, surfaces: Sequence[str]) -> list[str]:
    caveats: list[str] = []
    for surface in surfaces:
        quality = _surface_quality_label(report, surface)
        if quality and quality not in QUALITY_ORDER:
            caveats.append(f"UNKNOWN_SURFACE_QUALITY:{quality}")
    return caveats


def _overall_surface_quality(report: Any, surfaces: Sequence[str]) -> str:
    labels = [
        _surface_quality_label(report, surface) or "unavailable" for surface in surfaces
    ]
    known = [label for label in labels if label in QUALITY_ORDER]
    if not known:
        return "unavailable"
    return min(known, key=lambda label: QUALITY_ORDER[label])


def _surface_quality_label(report: Any, surface: str) -> str | None:
    if surface == "data_completeness":
        return _data_completeness_quality_label(report)
    if surface == "timing_surface":
        return (getattr(report, "timing_surface", None) or {}).get("confidence_band")
    if surface == "position_building_surface":
        return _position_quality_label(report)
    if surface == "exit_attribution":
        return _exit_quality_label(report)
    if surface in {"edge_surface", "outcome_side_breakdown"}:
        return _edge_quality_label(report)
    return None


def _position_quality_label(report: Any) -> str | None:
    surface = getattr(report, "position_building_surface", None) or {}
    distribution = surface.get("observation_window_quality_distribution") or (
        surface.get("wallet_summary") or {}
    ).get("observation_window_quality_distribution") or {}
    if not distribution:
        return None
    return max(
        distribution,
        key=lambda label: distribution.get(label, {}).get("share", 0),
    )


def _data_completeness_quality_label(report: Any) -> str | None:
    """Return explicit quality; unknown caveats stay visible as unknown_caveat."""

    surface = getattr(report, "data_completeness", None) or {}
    explicit = surface.get("quality")
    if explicit:
        return explicit
    if not surface:
        return None
    caveats = surface.get("caveats") or {}
    known_caveats = {
        "activity_truncated",
        "subgraph_unmapped_fills",
        "attribution_coverage_low",
        "limitation_present",
        "closed_positions_truncated",  # G2 — closed-position fetch cap signal
    }
    if any(value and key not in known_caveats for key, value in caveats.items()):
        return "unknown_caveat"
    if (
        caveats.get("activity_truncated")
        or caveats.get("subgraph_unmapped_fills")
        or caveats.get("closed_positions_truncated")
    ):
        return "poor"
    if caveats.get("attribution_coverage_low") or caveats.get("limitation_present"):
        return "partial"
    return "good"


def _exit_quality_label(report: Any) -> str | None:
    coverage = (getattr(report, "exit_attribution", None) or {}).get("coverage") or {}
    resolved = coverage.get("n_with_resolved_attribution") or coverage.get("n_attributed") or 0
    total = coverage.get("n_total") or resolved
    if resolved <= 0:
        return None
    ratio = resolved / max(total, 1)
    if ratio >= 0.80:
        return "good"
    if ratio >= 0.50:
        return "partial"
    return "poor"


def _edge_quality_label(report: Any) -> str | None:
    edge = getattr(report, "outcome_side_breakdown", None) or {}
    n = edge.get("n")
    if n is None and edge.get("category_clusters"):
        n = sum(int(cluster.get("n") or 0) for cluster in edge["category_clusters"])
    if n is None:
        n = sum(
            int((edge.get(bucket) or {}).get("n") or 0)
            for bucket in ("yes", "no", "other")
        )
    n = int(n or 0)
    if n >= 30:
        return "good"
    if n >= 5:
        return "partial"
    if n > 0:
        return "poor"
    return None


def _rule_data_needs(rule: ThesisRule) -> list[dict[str, Any]]:
    needs: list[dict[str, Any]] = []
    for raw in rule.needs_data:
        if isinstance(raw, Mapping):
            field_name = raw.get("field_name") or raw.get("name")
            if not field_name:
                continue
            needs.append(
                data_need(
                    str(field_name),
                    blocking=bool(raw.get("blocking", True)),
                    rationale=raw.get("rationale"),
                    candidate_sources=raw.get("candidate_sources") or [],
                )
            )
        else:
            needs.append(data_need(str(raw)))
    return needs


def _safe_evidence(rule: ThesisRule, report: Any) -> list[dict[str, Any]]:
    try:
        return rule.evidence_builder(report)
    except (AttributeError, KeyError, TypeError, ValueError):
        return []


def _unique_data_needs(needs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_field: dict[str, dict[str, Any]] = {}
    for need in needs:
        by_field.setdefault(need["field_name"], dict(need))
    return [by_field[key] for key in sorted(by_field)]


def _wallet(report: Any) -> str:
    return str(getattr(report, "wallet", "unknown"))


def _protocol_version(report: Any) -> str:
    return str(getattr(report, "protocol_version", "unknown"))


def _share(values: Sequence[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)
