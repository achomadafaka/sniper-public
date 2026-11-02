"""Core rule contracts for Strategy Pattern Audit v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


MATCHES = "matches"
INCONCLUSIVE = "inconclusive"
REJECTED = "rejected"


DEFAULT_OPERATOR_PRIORS: dict[str, Any] = {
    "min_population_evaluated": 30,
    "min_supporting": 10,
    "min_observation_quality_share": 0.50,
    "min_archetype_diversity": 2,
    "forbidden_archetypes_for_directional_theses": ["arb_fund", "market_maker"],
    "exclude_wallets": [],
    "exclude_known_phantom_wallets": True,
    "min_n_closed_positions_per_wallet": 5,
    "max_share_with_activity_cap_hit": 0.40,
    "min_share_with_partial_or_better": 0.30,
}

MIN_PRIOR_BOUNDS: dict[str, Any] = {
    "min_population_evaluated": 5,
    "min_supporting": 2,
    "min_observation_quality_share": 0.20,
    "min_archetype_diversity": 1,
    "min_n_closed_positions_per_wallet": 1,
}


@dataclass(frozen=True)
class RuleEvaluation:
    """Per-wallet result for one thesis rule."""

    wallet: str
    rule_id: str
    state: str
    reason: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    coverage_caveats: list[str] = field(default_factory=list)
    data_needs: list[dict[str, Any]] = field(default_factory=list)
    archetype_layer1: str | None = None
    surface_quality: str | None = None


@dataclass(frozen=True)
class ThesisRule:
    """Deterministic strategy-thesis rule contract."""

    rule_id: str
    title: str
    mechanism_category: str
    description: str
    surfaces_consumed: list[str]
    operator_priors_used: list[str]
    forbidden_archetypes: list[str]
    minimum_n_per_wallet: int
    needs_data: list[Any]
    false_positive_risks: list[str]
    simulation_dataset_requirements: list[str]
    coverage_predicate: Callable[[Any, dict[str, Any]], bool]
    anti_pattern_predicate: Callable[[Any, dict[str, Any]], bool]
    pattern_predicate: Callable[[Any, dict[str, Any]], bool]
    evidence_builder: Callable[[Any], list[dict[str, Any]]]
    promotable: bool = True


def normalize_operator_priors(
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Merge operator priors and keep an audit trail for defaults/clamps."""

    raw = dict(DEFAULT_OPERATOR_PRIORS)
    overrides = dict(overrides or {})
    raw.update(overrides)

    normalized: dict[str, Any] = {}
    applied: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_OPERATOR_PRIORS.items():
        value = raw[key]
        entry: dict[str, Any] = {"value": value, "default": default}
        if key in MIN_PRIOR_BOUNDS and value < MIN_PRIOR_BOUNDS[key]:
            entry = {
                "value": MIN_PRIOR_BOUNDS[key],
                "default": default,
                "source": "clamped",
                "clamped_from": value,
            }
            value = MIN_PRIOR_BOUNDS[key]
        elif key in overrides:
            entry["source"] = "operator"
        else:
            entry["source"] = "default"
        normalized[key] = value
        applied[key] = entry

    for key, value in overrides.items():
        if key in applied:
            continue
        normalized[key] = value
        applied[key] = {"value": value, "default": None, "source": "operator"}

    return normalized, applied


def data_need(
    field_name: str,
    *,
    blocking: bool = True,
    rationale: str | None = None,
    candidate_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a normalized data-need row."""

    return {
        "field_name": field_name,
        "blocking": blocking,
        "rationale": rationale
        or f"{field_name} is required before this thesis can be tested.",
        "candidate_sources": list(candidate_sources or []),
    }


def evidence_ref(
    *,
    surface: str,
    path: str,
    value: Any,
    predicate: str,
    threshold: Any = None,
    provenance: str = "factual",
) -> dict[str, Any]:
    return {
        "surface": surface,
        "path": path,
        "value": value,
        "predicate": predicate,
        "threshold": threshold,
        "provenance": provenance,
    }


def unique_sorted(values: Sequence[str]) -> list[str]:
    return sorted({value for value in values if value})
