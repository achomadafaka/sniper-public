# Strategy Pattern Audit Phase 1 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the core Strategy Pattern Audit as pure compute over already-produced `ProtocolReport`s, producing thesis intelligence while rejecting weak/noisy patterns.

**Architecture:** Create a new core-only package under `sniper/analysis/strategy_pattern_audit/`. The package defines `ThesisRule`, deterministic rule evaluation, honesty-bar aggregation, eight seed rules from the merged spec, and output helpers. It does not wire into `ProtocolReport`, `analyze_wallet.py`, report rendering, persistence, APIs, or simulators.

**Tech Stack:** Python dataclasses, plain dictionaries, existing `ProtocolReport` dataclass as read-only input, pytest.

---

## Hard Scope Guards

This Phase 1 plan must not include any implementation step that:

- wires `ProtocolReport`,
- mutates `ProtocolReport`,
- touches `sniper/analysis/wallet_protocol/analyze_wallet.py`,
- touches `sniper/analysis/wallet_protocol/report.py`,
- changes verdict logic,
- changes gates,
- changes `primary_edge_type`,
- changes `copyability_note`,
- adds simulator logic,
- outputs expected PnL,
- outputs deployable recommendations,
- outputs copy-trigger language,
- calls price-history endpoints,
- calls live APIs,
- runs analyzer batches,
- mutates DB,
- restarts services,
- touches legacy low-buy streams.

Forbidden output keys:

```python
FORBIDDEN_OUTPUT_KEYS = {
    "recommended_trigger",
    "trigger_threshold",
    "copy_after_first_buy",
    "copy_after_n_buys",
    "expected_copy_pnl",
    "simulated_copy_result",
    "copy_strategy",
    "copy_signal",
    "trade_signal",
    "deploy_recommendation",
    "recommended_trade",
    "recommended_position_size",
    "expected_pnl_for_thesis",
    "simulated_thesis_result",
    "forward_prediction",
    "buy_signal",
    "sell_signal",
}
```

Forbidden output phrases:

```python
FORBIDDEN_OUTPUT_PHRASES = (
    "copy this wallet",
    "copy these wallets",
    "expected return",
    "trigger on first buy",
    "buy when pattern matches",
    "predicts forward returns",
    "deploy this strategy",
    "trade this market",
)
```

Allowed output concepts:

- candidate thesis,
- mechanism category,
- evidence strength,
- missing evidence,
- false-positive risks,
- simulation-worthiness,
- required dataset for future testing,
- plain-language explanation.

---

## File Structure

Create:

- `sniper/analysis/strategy_pattern_audit/__init__.py`
  - Re-export public API: `ThesisRule`, `RuleEvaluation`, `compute_strategy_pattern_audit`, `default_seed_rules`.
- `sniper/analysis/strategy_pattern_audit/rules.py`
  - `ThesisRule` dataclass, `RuleEvaluation` dataclass, evidence-ref helpers, quality helpers, operator-prior normalization.
- `sniper/analysis/strategy_pattern_audit/core.py`
  - `compute_strategy_pattern_audit(...)`, rule evaluation loop, honesty bar, population-quality block, data-gap consolidation, forbidden-output constants.
- `sniper/analysis/strategy_pattern_audit/seed_rules.py`
  - Eight deterministic seed rules from the merged spec.
- `tests/test_strategy_pattern_audit_core.py`
  - Core evaluator, honesty bar, forbidden outputs, no-HTTP, no mutation, output shape.
- `tests/test_strategy_pattern_audit_seed_rules.py`
  - One focused behavior test per seed rule plus rule metadata contract tests.

Do not modify:

- `sniper/analysis/wallet_protocol/types.py`
- `sniper/analysis/wallet_protocol/analyze_wallet.py`
- `sniper/analysis/wallet_protocol/report.py`
- any ledger, DB, migration, simulator, or legacy low-buy file.

---

## Data Model Decisions

Use exact Phase 1 API from the merged spec:

```python
def compute_strategy_pattern_audit(
    reports: Sequence[ProtocolReport],
    *,
    rules: Sequence[ThesisRule] | None = None,
    audit_run_id: str | None = None,
    operator_priors: Mapping[str, Any] | None = None,
) -> dict | None:
    ...
```

Rule-result states:

```python
MATCHES = "matches"
INCONCLUSIVE = "inconclusive"
REJECTED = "rejected"
```

Denominators:

- `n_supporting`: rule result `matches`
- `n_inconclusive`: rule result `inconclusive`
- `n_rejected`: rule result `rejected`
- `n_evaluable = n_supporting + n_rejected`
- `support_share = n_supporting / n_evaluable`, or `None` when `n_evaluable == 0`

Operator-prior lower bounds:

```python
MIN_PRIOR_BOUNDS = {
    "min_population_evaluated": 5,
    "min_supporting": 2,
    "min_observation_quality_share": 0.20,
    "min_archetype_diversity": 1,
    "min_n_closed_positions_per_wallet": 1,
}
```

Default priors:

```python
DEFAULT_OPERATOR_PRIORS = {
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
```

## Spec Gap Resolutions

The merged spec is authoritative, but this implementation plan locks these
review-time decisions so the implementer does not guess:

1. Effective forbidden archetypes are computed at evaluation time from both:
   - `rule.forbidden_archetypes`
   - `operator_priors["forbidden_archetypes_for_directional_theses"]` when
     `rule.mechanism_category` is directional.

2. Phase 1 must include a class-level `ProtocolReport` boundary lock:
   `dataclasses.fields(ProtocolReport)` must not contain any field whose name
   starts with `strategy_pattern_audit`.

3. Quality labels accepted in Phase 1 are locked:
   - partial-or-better: `partial`, `medium`, `good`, `high`
   - below-partial: `poor`, `low`, `underpowered`, `insufficient`,
     `unavailable`
   - unknown labels are not silently mapped to unavailable. They produce
     `inconclusive (coverage)` with an `UNKNOWN_SURFACE_QUALITY:<label>`
     caveat.

4. Anti-pattern symmetry counts only rejected evaluations with
   `reason == "anti-pattern"`. Rejections from forbidden archetypes,
   no-match, or sample/coverage causes do not satisfy the anti-pattern floor.

5. `would_have_been_promoted_at.data_fields` must include blocking
   `data_needs[*].field_name` values from per-evaluation results.

---

## Task 1: Public Package Skeleton

**Files:**
- Create: `sniper/analysis/strategy_pattern_audit/__init__.py`
- Create: `sniper/analysis/strategy_pattern_audit/rules.py`
- Create: `sniper/analysis/strategy_pattern_audit/core.py`
- Test: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing import/API test**

Add to `tests/test_strategy_pattern_audit_core.py`:

```python
"""Strategy Pattern Audit Phase 1 core tests."""
from __future__ import annotations


def test_public_api_imports() -> None:
    from sniper.analysis.strategy_pattern_audit import (
        RuleEvaluation,
        ThesisRule,
        compute_strategy_pattern_audit,
        default_seed_rules,
    )

    assert ThesisRule.__name__ == "ThesisRule"
    assert RuleEvaluation.__name__ == "RuleEvaluation"
    assert callable(compute_strategy_pattern_audit)
    assert callable(default_seed_rules)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_public_api_imports -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.analysis.strategy_pattern_audit'`.

- [ ] **Step 3: Create minimal package skeleton**

Create `sniper/analysis/strategy_pattern_audit/rules.py`:

```python
"""Rule contracts for Strategy Pattern Audit v1."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


MATCHES = "matches"
INCONCLUSIVE = "inconclusive"
REJECTED = "rejected"


@dataclass(frozen=True)
class RuleEvaluation:
    wallet: str
    rule_id: str
    state: str
    reason: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    coverage_caveats: list[str] = field(default_factory=list)
    data_needs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ThesisRule:
    rule_id: str
    title: str
    mechanism_category: str
    description: str
    surfaces_consumed: list[str]
    operator_priors_used: list[str]
    forbidden_archetypes: list[str]
    minimum_n_per_wallet: int
    needs_data: list[str]
    false_positive_risks: list[str]
    simulation_dataset_requirements: list[str]
    coverage_predicate: Callable[[Any, Mapping[str, Any]], bool]
    anti_pattern_predicate: Callable[[Any, Mapping[str, Any]], bool]
    pattern_predicate: Callable[[Any, Mapping[str, Any]], bool]
    evidence_builder: Callable[[Any], list[dict[str, Any]]]
```

Create `sniper/analysis/strategy_pattern_audit/core.py`:

```python
"""Core Strategy Pattern Audit v1 evaluator."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sniper.analysis.strategy_pattern_audit.rules import ThesisRule

SURFACE_VERSION = "strategy_pattern_audit_v1"


def compute_strategy_pattern_audit(
    reports: Sequence[Any],
    *,
    rules: Sequence[ThesisRule] | None = None,
    audit_run_id: str | None = None,
    operator_priors: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not reports:
        return None
    return {
        "audit_run_id": audit_run_id or "strategy-pattern-audit-v1",
        "surface_version": SURFACE_VERSION,
        "generated_from": {
            "n_reports": len(reports),
            "n_reports_excluded_by_priors": 0,
            "operator_priors_applied": dict(operator_priors or {}),
            "report_protocol_versions": {},
        },
        "population_quality": {},
        "theses": [],
        "rejected_theses": [],
        "data_gaps": [],
        "top_level_caveats": [],
    }
```

Create `sniper/analysis/strategy_pattern_audit/__init__.py`:

```python
"""Strategy Pattern Audit v1 public API."""
from sniper.analysis.strategy_pattern_audit.core import compute_strategy_pattern_audit
from sniper.analysis.strategy_pattern_audit.rules import RuleEvaluation, ThesisRule
from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

__all__ = [
    "RuleEvaluation",
    "ThesisRule",
    "compute_strategy_pattern_audit",
    "default_seed_rules",
]
```

Create `sniper/analysis/strategy_pattern_audit/seed_rules.py`:

```python
"""Seed thesis rules for Strategy Pattern Audit v1."""
from __future__ import annotations

from sniper.analysis.strategy_pattern_audit.rules import ThesisRule


def default_seed_rules() -> list[ThesisRule]:
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_public_api_imports -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): scaffold strategy pattern audit core"
```

---

## Task 2: Output Shape and Empty/No-Rule Behavior

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing output-shape tests**

Append to `tests/test_strategy_pattern_audit_core.py`:

```python
from dataclasses import replace

from sniper.analysis.wallet_protocol.types import ProtocolReport


def _report(wallet: str = "0xabc") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        overall_verdict="data_insufficient",
        gate_result="reject_insufficient_sample",
        trading_mode="n/a_routing",
    )


def test_returns_none_for_zero_reports() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    assert compute_strategy_pattern_audit([]) is None


def test_returns_catalog_with_empty_theses_when_no_rules() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit([_report()], rules=[], audit_run_id="audit-1")

    assert result == {
        "audit_run_id": "audit-1",
        "surface_version": "strategy_pattern_audit_v1",
        "generated_from": {
            "n_reports": 1,
            "n_reports_excluded_by_priors": 0,
            "operator_priors_applied": {},
            "report_protocol_versions": {"unknown": 1},
        },
        "population_quality": {
            "share_with_partial_or_better": None,
            "share_with_activity_cap_hit": 0.0,
            "share_with_subgraph_fallback_used": 0.0,
            "share_with_walk_full_history": 0.0,
            "median_n_closed_positions": 0,
            "median_n_open_positions": 0,
            "archetype_layer1_distribution": {},
            "data_quality_flag_distribution": {},
            "n_excluded_by_forbidden_archetypes": 0,
            "n_excluded_by_zero_n_closed_positions": 1,
            "surface_quality_distribution": {
                "data_completeness": {},
                "timing_surface": {},
                "position_building_surface": {},
                "exit_attribution": {},
                "edge_surface": {},
            },
        },
        "theses": [],
        "rejected_theses": [],
        "data_gaps": [],
        "top_level_caveats": ["NO_RULES_EVALUATED"],
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_returns_catalog_with_empty_theses_when_no_rules -q
```

Expected: FAIL because `population_quality` is currently `{}` and `report_protocol_versions` is empty.

- [ ] **Step 3: Implement population-quality skeleton**

In `core.py`, add helpers:

```python
from statistics import median


def _get(report: Any, name: str, default: Any = None) -> Any:
    return getattr(report, name, default)


def _protocol_version(report: Any) -> str:
    return str(_get(report, "protocol_version", None) or "unknown")


def _distribution(values: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        if value is None:
            continue
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    return int(median(values))


def _population_quality(reports: Sequence[Any]) -> dict[str, Any]:
    n = len(reports)
    n_closed = [int(_get(r, "n_closed_positions", 0) or 0) for r in reports]
    n_open = [int(_get(r, "n_open_positions", 0) or 0) for r in reports]
    data_flags: list[str] = []
    for report in reports:
        data_flags.extend(list(_get(report, "data_quality_flags", []) or []))
    return {
        "share_with_partial_or_better": None,
        "share_with_activity_cap_hit": sum(bool(_get(r, "activity_cap_hit", False)) for r in reports) / n,
        "share_with_subgraph_fallback_used": sum(bool(_get(r, "subgraph_fallback_used", False)) for r in reports) / n,
        "share_with_walk_full_history": sum(bool(_get(r, "walk_full_history", False)) for r in reports) / n,
        "median_n_closed_positions": _median_int(n_closed),
        "median_n_open_positions": _median_int(n_open),
        "archetype_layer1_distribution": _distribution([_get(r, "archetype_layer1") for r in reports]),
        "data_quality_flag_distribution": _distribution(data_flags),
        "n_excluded_by_forbidden_archetypes": 0,
        "n_excluded_by_zero_n_closed_positions": sum(v == 0 for v in n_closed),
        "surface_quality_distribution": {
            "data_completeness": {},
            "timing_surface": {},
            "position_building_surface": {},
            "exit_attribution": {},
            "edge_surface": {},
        },
    }
```

Update `compute_strategy_pattern_audit(...)` to set:

```python
rules = list(rules if rules is not None else [])
protocol_versions = _distribution([_protocol_version(report) for report in reports])
return {
    ...
    "generated_from": {
        ...
        "operator_priors_applied": dict(operator_priors or {}),
        "report_protocol_versions": protocol_versions,
    },
    "population_quality": _population_quality(reports),
    ...
    "top_level_caveats": ["NO_RULES_EVALUATED"] if not rules else [],
}
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_returns_none_for_zero_reports tests/test_strategy_pattern_audit_core.py::test_returns_catalog_with_empty_theses_when_no_rules -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/core.py tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): return strategy audit catalog skeleton"
```

---

## Task 3: Operator Priors and Relaxation Floors

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/rules.py`
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing operator-prior tests**

Append:

```python
def test_operator_priors_apply_defaults_and_clamp_relaxations() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit(
        [_report()],
        rules=[],
        operator_priors={
            "min_population_evaluated": 1,
            "min_supporting": 1,
            "min_observation_quality_share": 0.0,
            "min_archetype_diversity": 0,
            "min_n_closed_positions_per_wallet": 0,
        },
    )

    priors = result["generated_from"]["operator_priors_applied"]
    assert priors["min_population_evaluated"] == {
        "value": 5,
        "default": 30,
        "source": "clamped",
        "clamped_from": 1,
    }
    assert priors["min_supporting"]["value"] == 2
    assert priors["min_observation_quality_share"]["value"] == 0.20
    assert priors["min_archetype_diversity"]["value"] == 1
    assert priors["min_n_closed_positions_per_wallet"]["value"] == 1
    assert priors["exclude_known_phantom_wallets"]["value"] is True


def test_operator_priors_preserve_non_clamped_overrides() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit(
        [_report()],
        rules=[],
        operator_priors={
            "min_population_evaluated": 12,
            "min_supporting": 4,
            "min_observation_quality_share": 0.35,
        },
    )

    priors = result["generated_from"]["operator_priors_applied"]
    assert priors["min_population_evaluated"] == {
        "value": 12,
        "default": 30,
        "source": "operator",
    }
    assert priors["min_supporting"]["value"] == 4
    assert priors["min_supporting"]["source"] == "operator"
    assert priors["min_observation_quality_share"]["value"] == 0.35
    assert priors["min_observation_quality_share"]["source"] == "operator"
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_operator_priors_apply_defaults_and_clamp_relaxations tests/test_strategy_pattern_audit_core.py::test_operator_priors_preserve_non_clamped_overrides -q
```

Expected: FAIL because priors are raw dicts.

- [ ] **Step 3: Implement prior normalization**

Add to `rules.py`:

```python
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


def normalize_operator_priors(
    overrides: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    overrides = dict(overrides or {})
    normalized = dict(DEFAULT_OPERATOR_PRIORS)
    applied: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_OPERATOR_PRIORS.items():
        raw = overrides.get(key, default)
        source = "operator" if key in overrides else "default"
        value = raw
        entry: dict[str, Any] = {"value": value, "default": default, "source": source}
        if key in MIN_PRIOR_BOUNDS and value < MIN_PRIOR_BOUNDS[key]:
            entry["clamped_from"] = value
            entry["value"] = MIN_PRIOR_BOUNDS[key]
            entry["source"] = "clamped"
            value = entry["value"]
        normalized[key] = value
        applied[key] = entry
    if normalized["exclude_known_phantom_wallets"]:
        applied["exclude_known_phantom_wallets"]["status"] = "not_applicable"
    return normalized, applied
```

Use `normalize_operator_priors()` in `core.py`.

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_operator_priors_apply_defaults_and_clamp_relaxations tests/test_strategy_pattern_audit_core.py::test_operator_priors_preserve_non_clamped_overrides -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): normalize strategy audit operator priors"
```

---

## Task 4: Rule Evaluation Order and Denominators

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `sniper/analysis/strategy_pattern_audit/rules.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing evaluation-order test**

Append:

```python
from sniper.analysis.strategy_pattern_audit.rules import ThesisRule


def _rule(
    *,
    rule_id: str = "test_rule_v1",
    coverage=True,
    anti=False,
    pattern=True,
    needs_data=None,
    minimum_n_per_wallet=0,
) -> ThesisRule:
    return ThesisRule(
        rule_id=rule_id,
        title="Test rule",
        mechanism_category="test_mechanism",
        description="Pattern observed in test wallets",
        surfaces_consumed=["data_completeness"],
        operator_priors_used=["min_population_evaluated"],
        forbidden_archetypes=[],
        minimum_n_per_wallet=minimum_n_per_wallet,
        needs_data=list(needs_data or []),
        false_positive_risks=["synthetic test risk"],
        simulation_dataset_requirements=["test_dataset"],
        coverage_predicate=lambda report, priors: coverage,
        anti_pattern_predicate=lambda report, priors: anti,
        pattern_predicate=lambda report, priors: pattern,
        evidence_builder=lambda report: [
            {
                "surface": "data_completeness",
                "path": "data_completeness.capture_state",
                "value": "complete",
                "predicate": "eq",
                "threshold": "complete",
                "provenance": "factual",
            }
        ],
    )


def test_evaluation_counts_exclude_inconclusive_from_evaluable() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [
        _report("0x1"),
        _report("0x2"),
        _report("0x3"),
    ]
    rules = [
        _rule(rule_id="support_rule", pattern=True),
        _rule(rule_id="inconclusive_rule", coverage=False),
        _rule(rule_id="rejected_rule", pattern=False),
    ]

    result = compute_strategy_pattern_audit(
        reports,
        rules=rules,
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    rejected = {row["rule_id"]: row for row in result["rejected_theses"]}
    assert rejected["inconclusive_rule"]["n_supporting"] == 0
    assert rejected["inconclusive_rule"]["n_evaluable"] == 0
    assert rejected["inconclusive_rule"]["n_inconclusive"] == 3
    assert rejected["rejected_rule"]["n_evaluable"] == 3
    assert rejected["rejected_rule"]["n_rejected"] == 3


def test_directional_rules_apply_operator_forbidden_archetypes() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _report("0xarb")
    report.archetype_layer1 = "arb_fund"
    report.n_closed_positions = 10

    result = compute_strategy_pattern_audit(
        [report],
        rules=[_rule(rule_id="directional_rule", pattern=True)],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
            "forbidden_archetypes_for_directional_theses": ["arb_fund"],
        },
    )

    row = result["rejected_theses"][0]
    assert row["n_rejected"] == 1
    assert row["anti_pattern_examples"] == []
    assert "support_floor" in row["fail_reasons"]
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_evaluation_counts_exclude_inconclusive_from_evaluable tests/test_strategy_pattern_audit_core.py::test_directional_rules_apply_operator_forbidden_archetypes -q
```

Expected: FAIL because rules are not evaluated.

- [ ] **Step 3: Implement rule evaluation**

Add to `core.py`:

```python
from sniper.analysis.strategy_pattern_audit.rules import (
    INCONCLUSIVE,
    MATCHES,
    REJECTED,
    RuleEvaluation,
    normalize_operator_priors,
)


def _wallet(report: Any) -> str:
    return str(_get(report, "wallet", "unknown"))


def _evaluate_rule_for_report(
    rule: ThesisRule,
    report: Any,
    priors: Mapping[str, Any],
) -> RuleEvaluation:
    if not rule.coverage_predicate(report, priors):
        return RuleEvaluation(_wallet(report), rule.rule_id, INCONCLUSIVE, "coverage")
    if rule.anti_pattern_predicate(report, priors):
        return RuleEvaluation(_wallet(report), rule.rule_id, REJECTED, "anti-pattern", rule.evidence_builder(report))
    effective_forbidden = set(rule.forbidden_archetypes)
    if rule.mechanism_category in {
        "market_selection_edge",
        "category_specialist",
        "holding_prediction_edge",
        "exit_discipline_edge",
        "timing_capture_edge",
        "accumulation_pattern_edge",
        "sizing_conviction_edge",
    }:
        effective_forbidden.update(
            priors.get("forbidden_archetypes_for_directional_theses", [])
        )
    if _get(report, "archetype_layer1") in effective_forbidden:
        return RuleEvaluation(_wallet(report), rule.rule_id, REJECTED, "forbidden_archetype")
    n_closed = int(_get(report, "n_closed_positions", 0) or 0)
    if n_closed < rule.minimum_n_per_wallet:
        return RuleEvaluation(_wallet(report), rule.rule_id, INCONCLUSIVE, "n")
    if rule.needs_data:
        needs = [
            {
                "field_name": name,
                "blocking": True,
                "rationale": f"{rule.rule_id} requires {name}",
                "candidate_sources": [],
            }
            for name in rule.needs_data
        ]
        return RuleEvaluation(_wallet(report), rule.rule_id, INCONCLUSIVE, "data", data_needs=needs)
    if rule.pattern_predicate(report, priors):
        return RuleEvaluation(_wallet(report), rule.rule_id, MATCHES, "match", rule.evidence_builder(report))
    return RuleEvaluation(_wallet(report), rule.rule_id, REJECTED, "no_match", rule.evidence_builder(report))
```

Add a rule aggregation helper that returns one rejected thesis when the honesty bar fails:

```python
def _aggregate_rule(
    rule: ThesisRule,
    evaluations: list[RuleEvaluation],
    priors: Mapping[str, Any],
    applied_priors: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    supporting = [e for e in evaluations if e.state == MATCHES]
    inconclusive = [e for e in evaluations if e.state == INCONCLUSIVE]
    rejected = [e for e in evaluations if e.state == REJECTED]
    n_evaluable = len(supporting) + len(rejected)
    support_share = len(supporting) / n_evaluable if n_evaluable else None
    fail_reasons: list[str] = []
    if n_evaluable < priors["min_population_evaluated"]:
        fail_reasons.append("population_floor")
    if len(supporting) < priors["min_supporting"]:
        fail_reasons.append("support_floor")
    row = {
        "rule_id": rule.rule_id,
        "title": rule.title,
        "fail_reasons": fail_reasons,
        "n_supporting": len(supporting),
        "n_inconclusive": len(inconclusive),
        "n_rejected": len(rejected),
        "n_evaluable": n_evaluable,
        "support_share": support_share,
        "honesty_bar": {
            "n_min_supporting_required": priors["min_supporting"],
            "min_observation_quality_share_required": priors["min_observation_quality_share"],
            "min_population_evaluated": priors["min_population_evaluated"],
            "passed": not fail_reasons,
            "fail_reasons": fail_reasons,
        },
        "data_needs": [],
        "anti_pattern_examples": [],
        "would_have_been_promoted_at": {
            "n_supporting": priors["min_supporting"],
            "data_fields": sorted(
                {
                    need["field_name"]
                    for evaluation in evaluations
                    for need in evaluation.data_needs
                    if need.get("blocking", False)
                }
            ),
        },
        "caveats": [],
    }
    if fail_reasons:
        return None, row
    thesis = {
        "rule_id": rule.rule_id,
        "title": rule.title,
        "description": rule.description,
        "mechanism_category": rule.mechanism_category,
        "n_supporting": len(supporting),
        "n_inconclusive": len(inconclusive),
        "n_rejected": len(rejected),
        "support_share": support_share,
        "supporting_wallets": [
            {
                "wallet": e.wallet,
                "evidence_refs": e.evidence_refs,
                "coverage_caveats": e.coverage_caveats,
            }
            for e in supporting
        ],
        "anti_pattern_examples": [
            {"wallet": e.wallet, "reason": e.reason, "evidence_refs": e.evidence_refs}
            for e in rejected
            if e.reason == "anti-pattern"
        ],
        "minimum_n_per_wallet": rule.minimum_n_per_wallet,
        "operator_priors_applied": dict(applied_priors),
        "honesty_bar": row["honesty_bar"],
        "data_needs": [],
        "fields_provenance": {
            "supporting_wallets": "factual",
            "anti_pattern_examples": "factual",
            "support_share": "factual",
        },
        "false_positive_risks": list(rule.false_positive_risks),
        "simulation_dataset_requirements": list(rule.simulation_dataset_requirements),
        "caveats": [],
    }
    return thesis, None
```

Update `compute_strategy_pattern_audit()` to normalize priors, evaluate each rule against each report, and fill `theses` / `rejected_theses`.

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_evaluation_counts_exclude_inconclusive_from_evaluable tests/test_strategy_pattern_audit_core.py::test_directional_rules_apply_operator_forbidden_archetypes -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): evaluate thesis rules with honesty counters"
```

---

## Task 5: Full Honesty Bar

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing honesty-bar tests**

Append:

```python
def _quality_report(wallet: str, *, quality: str, archetype: str = "directional") -> ProtocolReport:
    report = _report(wallet)
    report.data_completeness = {
        "quality": quality,
        "coverage": {"share_with_partial_or_better": 1.0 if quality in {"partial", "good"} else 0.0},
        "caveats": [],
    }
    report.timing_surface = {"confidence_band": quality, "coverage_caveats": []}
    report.position_building_surface = {
        "wallet_summary": {"observation_window_coverage": 1.0},
        "observation_window_quality_distribution": {quality: {"n": 1, "share": 1.0}},
    }
    report.exit_attribution = {"coverage": {"n_with_resolved_attribution": 5}}
    report.outcome_side_breakdown = {"n": 5}
    report.archetype_layer1 = archetype
    report.n_closed_positions = 5
    return report


def test_honesty_bar_rejects_single_archetype_support() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [_quality_report(f"0x{i}", quality="partial", archetype="same") for i in range(6)]
    result = compute_strategy_pattern_audit(
        reports,
        rules=[_rule()],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 2,
        },
    )

    rejected = result["rejected_theses"][0]
    assert "archetype_independence" in rejected["fail_reasons"]


def test_honesty_bar_rejects_low_observation_quality_share() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [
        _quality_report("0x1", quality="unavailable", archetype="a"),
        _quality_report("0x2", quality="unavailable", archetype="b"),
        _quality_report("0x3", quality="partial", archetype="a"),
        _quality_report("0x4", quality="unavailable", archetype="b"),
        _quality_report("0x5", quality="unavailable", archetype="a"),
    ]
    result = compute_strategy_pattern_audit(
        reports,
        rules=[_rule()],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.50,
            "min_archetype_diversity": 1,
        },
    )

    assert "quality_floor" in result["rejected_theses"][0]["fail_reasons"]


def test_anti_pattern_symmetry_requires_real_anti_pattern_examples() -> None:
    """Floor 4 (anti-pattern symmetry) must filter `rejected` by
    `reason == "anti-pattern"` only — `forbidden_archetype` rejections
    are coverage exclusions, not anti-pattern examples. Without this
    test, a future regression that drops the filter slips past CI.
    Spec Gap §4 anchor at lines 196-198 of this plan.
    """
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    supporters = [
        _quality_report(f"0xs{i}", quality="partial", archetype="directional")
        for i in range(5)
    ]
    forbidden_reject = _quality_report("0xarb", quality="partial", archetype="arb_fund")

    result = compute_strategy_pattern_audit(
        supporters + [forbidden_reject],
        rules=[_rule()],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
            "forbidden_archetypes_for_directional_theses": ["arb_fund"],
        },
    )

    row = result["rejected_theses"][0]
    assert "anti_pattern_symmetry" in row["fail_reasons"]
```

- [ ] **Step 2: Run tests to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_honesty_bar_rejects_single_archetype_support tests/test_strategy_pattern_audit_core.py::test_honesty_bar_rejects_low_observation_quality_share tests/test_strategy_pattern_audit_core.py::test_anti_pattern_symmetry_requires_real_anti_pattern_examples -q
```

Expected: FAIL because only population/support floors are implemented.

- [ ] **Step 3: Implement quality and honesty helpers**

Add:

```python
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

KNOWN_QUALITY_LABELS = set(QUALITY_ORDER)


def _quality_at_least_partial(value: Any) -> tuple[bool, str | None]:
    label = str(value or "").lower()
    if label not in KNOWN_QUALITY_LABELS:
        return False, f"UNKNOWN_SURFACE_QUALITY:{label}"
    return QUALITY_ORDER[label] >= QUALITY_ORDER["partial"], None


def _surface_quality(report: Any, surface: str) -> str:
    if surface == "data_completeness":
        dc = _get(report, "data_completeness") or {}
        return str(dc.get("quality") or dc.get("capture_state") or "unavailable")
    if surface == "timing_surface":
        ts = _get(report, "timing_surface") or {}
        return str(ts.get("confidence_band") or "unavailable")
    if surface == "position_building_surface":
        pb = _get(report, "position_building_surface") or {}
        dist = pb.get("observation_window_quality_distribution") or {}
        if dist.get("good", {}).get("share", 0) > 0:
            return "good"
        if dist.get("partial", {}).get("share", 0) > 0:
            return "partial"
        if dist.get("poor", {}).get("share", 0) > 0:
            return "poor"
        return "unavailable"
    if surface == "exit_attribution":
        ea = _get(report, "exit_attribution") or {}
        coverage = ea.get("coverage") or {}
        return "partial" if coverage.get("n_with_resolved_attribution", 0) else "unavailable"
    if surface == "edge_surface":
        return "partial" if _get(report, "outcome_side_breakdown") else "unavailable"
    return "unavailable"


def _report_has_partial_quality(report: Any, surfaces: Sequence[str]) -> bool:
    return all(_quality_at_least_partial(_surface_quality(report, surface))[0] for surface in surfaces)
```

Extend honesty bar:

```python
quality_caveats = []
quality_ok_n = 0
for evaluation in supporting:
    report = report_by_wallet[evaluation.wallet]
    surface_ok = True
    for surface in rule.surfaces_consumed:
        ok, caveat = _quality_at_least_partial(_surface_quality(report, surface))
        if caveat:
            quality_caveats.append(caveat)
            surface_ok = False
        elif not ok:
            surface_ok = False
    if surface_ok:
        quality_ok_n += 1
quality_share = quality_ok_n / len(supporting) if supporting else 0.0
if quality_share < priors["min_observation_quality_share"]:
    fail_reasons.append("quality_floor")

archetypes = {str(_get(report_by_wallet[e.wallet], "archetype_layer1", "")) for e in supporting}
archetypes.discard("")
if len(archetypes) < priors["min_archetype_diversity"]:
    fail_reasons.append("archetype_independence")

anti_pattern_examples = [e for e in rejected if e.reason == "anti-pattern"]
if len(supporting) >= 5 and not anti_pattern_examples:
    fail_reasons.append("anti_pattern_symmetry")
```

Pass a `report_by_wallet` mapping into `_aggregate_rule`.

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_honesty_bar_rejects_single_archetype_support tests/test_strategy_pattern_audit_core.py::test_honesty_bar_rejects_low_observation_quality_share tests/test_strategy_pattern_audit_core.py::test_anti_pattern_symmetry_requires_real_anti_pattern_examples -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): enforce strategy audit honesty bar"
```

---

## Task 6: Data Gaps and Blocking Needs

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write failing data-gap test**

Append:

```python
def test_blocking_data_need_rejects_thesis_and_rolls_up_gap() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [_quality_report(f"0x{i}", quality="partial", archetype=str(i % 2)) for i in range(5)]
    result = compute_strategy_pattern_audit(
        reports,
        rules=[_rule(rule_id="needs_liquidity", needs_data=["order_book_depth"])],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    assert result["theses"] == []
    assert result["rejected_theses"][0]["fail_reasons"] == ["blocking_data_gap"]
    assert result["rejected_theses"][0]["would_have_been_promoted_at"]["data_fields"] == [
        "order_book_depth"
    ]
    assert result["data_gaps"] == [
        {
            "field_name": "order_book_depth",
            "blocking_n_theses": 1,
            "partial_n_theses": 0,
            "blocked_thesis_ids": ["needs_liquidity"],
            "partial_thesis_ids": [],
            "candidate_sources": [],
            "operator_priority_hint": None,
        }
    ]
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_blocking_data_need_rejects_thesis_and_rolls_up_gap -q
```

Expected: FAIL because data needs are not consolidated.

- [ ] **Step 3: Implement data-gap consolidation**

Add:

```python
def _consolidate_data_gaps(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_field: dict[str, dict[str, Any]] = {}
    for row in rows:
        rule_id = row["rule_id"]
        for need in row.get("data_needs", []):
            field = need["field_name"]
            entry = by_field.setdefault(
                field,
                {
                    "field_name": field,
                    "blocking_n_theses": 0,
                    "partial_n_theses": 0,
                    "blocked_thesis_ids": [],
                    "partial_thesis_ids": [],
                    "candidate_sources": need.get("candidate_sources", []),
                    "operator_priority_hint": None,
                },
            )
            if need.get("blocking", False):
                entry["blocking_n_theses"] += 1
                entry["blocked_thesis_ids"].append(rule_id)
            else:
                entry["partial_n_theses"] += 1
                entry["partial_thesis_ids"].append(rule_id)
    return sorted(
        by_field.values(),
        key=lambda x: (-x["blocking_n_theses"], -x["partial_n_theses"], x["field_name"]),
    )
```

When any rule evaluation has `data_needs`, aggregate into a rejected-thesis row with `fail_reasons=["blocking_data_gap"]`.

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_blocking_data_need_rejects_thesis_and_rolls_up_gap -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): surface blocking data gaps"
```

---

## Task 7: Forbidden Output Keys and Phrases

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write recursive forbidden-output test**

Append:

```python
FORBIDDEN_KEYS = {
    "recommended_trigger",
    "trigger_threshold",
    "copy_after_first_buy",
    "copy_after_n_buys",
    "expected_copy_pnl",
    "simulated_copy_result",
    "copy_strategy",
    "copy_signal",
    "trade_signal",
    "deploy_recommendation",
    "recommended_trade",
    "recommended_position_size",
    "expected_pnl_for_thesis",
    "simulated_thesis_result",
    "forward_prediction",
    "buy_signal",
    "sell_signal",
}

FORBIDDEN_PHRASES = (
    "copy this wallet",
    "copy these wallets",
    "expected return",
    "trigger on first buy",
    "buy when pattern matches",
    "predicts forward returns",
    "deploy this strategy",
    "trade this market",
)


def _walk_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)
    elif isinstance(value, str):
        yield value


def test_output_contains_no_forbidden_keys_or_phrases() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit([_report()], rules=[])
    tokens = [str(x).lower() for x in _walk_json(result)]

    for forbidden in FORBIDDEN_KEYS:
        assert forbidden.lower() not in tokens
    for phrase in FORBIDDEN_PHRASES:
        assert all(phrase not in token for token in tokens)
```

- [ ] **Step 2: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_output_contains_no_forbidden_keys_or_phrases -q
```

Expected: PASS if previous tasks avoided forbidden output. If FAIL, rename offending output key/phrase without weakening the test.

- [ ] **Step 3: Add constants to core for implementation reuse**

Add `FORBIDDEN_OUTPUT_KEYS` and `FORBIDDEN_OUTPUT_PHRASES` constants to `core.py` with the exact sets from the test. Do not make production code filter output after the fact; the constants are for test/import reuse and documentation.

- [ ] **Step 4: Run full core tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/core.py tests/test_strategy_pattern_audit_core.py
git commit -m "test(strategy): lock forbidden strategy audit outputs"
```

---

## Task 8: No-HTTP and Import Boundary Guards

**Files:**
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write no-HTTP and AST import tests**

Append:

```python
import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "sniper" / "analysis" / "strategy_pattern_audit"


def test_compute_strategy_pattern_audit_makes_no_http_calls(monkeypatch) -> None:
    import requests
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    calls = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("HTTP call attempted")

    monkeypatch.setattr(requests, "get", _record, raising=False)
    monkeypatch.setattr(requests, "post", _record, raising=False)
    monkeypatch.setattr(requests.Session, "get", _record, raising=False)
    monkeypatch.setattr(requests.Session, "post", _record, raising=False)

    compute_strategy_pattern_audit([_report()], rules=[])

    assert calls == []


def test_strategy_pattern_audit_core_import_boundaries() -> None:
    forbidden_roots = {
        "requests",
        "httpx",
        "urllib",
        "duckdb",
        "supabase",
        "db",
    }
    forbidden_modules = {
        "sniper.analysis.wallet_protocol.analyze_wallet",
        "sniper.analysis.wallet_protocol.report",
        "sniper.analysis.wallet_protocol.ledger",
    }
    for path in PACKAGE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".", 1)[0] not in forbidden_roots
                    assert alias.name not in forbidden_modules
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".", 1)[0] not in forbidden_roots
                assert node.module not in forbidden_modules
                assert not node.module.startswith("sniper.analysis.wallet_protocol.ledger")
```

- [ ] **Step 2: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_compute_strategy_pattern_audit_makes_no_http_calls tests/test_strategy_pattern_audit_core.py::test_strategy_pattern_audit_core_import_boundaries -q
```

Expected: PASS. If `requests` is unavailable in the environment, adjust the test to monkeypatch `urllib.request.urlopen` too and skip only the requests-specific monkeypatch, not the no-I/O assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_strategy_pattern_audit_core.py
git commit -m "test(strategy): guard strategy audit IO boundaries"
```

---

## Task 9: No `ProtocolReport` Mutation Boundary

**Files:**
- Modify: `tests/test_strategy_pattern_audit_core.py`

This task locks Phase 1 only. The merged spec says Phase 2 `StrategyPatternAuditReport`
wiring is optional and population-level; it must not become a per-wallet
`ProtocolReport` field.

- [ ] **Step 1: Write mutation-boundary test**

Append:

```python
import copy
import dataclasses
from sniper.analysis.wallet_protocol.types import ProtocolReport


def test_compute_strategy_pattern_audit_does_not_mutate_protocol_reports() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _quality_report("0xabc", quality="partial", archetype="a")
    before = copy.deepcopy(dataclasses.asdict(report))

    compute_strategy_pattern_audit([report], rules=[])

    after = dataclasses.asdict(report)
    assert after == before
    assert not hasattr(report, "strategy_pattern_audit")


def test_phase1_does_not_add_protocol_report_field() -> None:
    field_names = {field.name for field in dataclasses.fields(ProtocolReport)}
    assert not any(name.startswith("strategy_pattern_audit") for name in field_names)
```

- [ ] **Step 2: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_compute_strategy_pattern_audit_does_not_mutate_protocol_reports tests/test_strategy_pattern_audit_core.py::test_phase1_does_not_add_protocol_report_field -q
```

Expected: PASS. If it fails, remove any assignment to input reports or any
Phase 1 `ProtocolReport` field addition.

- [ ] **Step 3: Commit**

```bash
git add tests/test_strategy_pattern_audit_core.py
git commit -m "test(strategy): lock ProtocolReport mutation boundary"
```

---

## Task 10: Seed Rule Metadata Contract

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Create: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Write failing metadata test for eight seed rules**

Create `tests/test_strategy_pattern_audit_seed_rules.py`:

```python
"""Seed-rule contract tests for Strategy Pattern Audit v1."""
from __future__ import annotations


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
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_default_seed_rules_are_exact_spec_set -q
```

Expected: FAIL because `default_seed_rules()` returns `[]`.

- [ ] **Step 3: Add seed rule factories with conservative metadata**

Implement `seed_rules.py` with eight `ThesisRule` objects. Use minimal predicates returning `False` for pattern checks for now; behavior-specific tests in later tasks will drive actual predicates.

```python
from typing import Any

from sniper.analysis.strategy_pattern_audit.rules import ThesisRule


def _false(_report: Any, _priors: dict) -> bool:
    return False


def _true(_report: Any, _priors: dict) -> bool:
    return True


def _evidence(surface: str, path: str, value: Any) -> list[dict[str, Any]]:
    return [
        {
            "surface": surface,
            "path": path,
            "value": value,
            "predicate": "observed",
            "threshold": None,
            "provenance": "factual",
        }
    ]
```

Then define each rule with exact `rule_id`, mechanism, consumed surfaces, risks, and dataset requirements from the spec.

- [ ] **Step 4: Run metadata test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_default_seed_rules_are_exact_spec_set -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): define strategy audit seed rule set"
```

---

## Task 11: Category Specialist Rule

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Modify: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 0: Verify `ProtocolReport` constructor fields**

Before writing the test fixture below, read
`sniper/analysis/wallet_protocol/types.py` and confirm the
`ProtocolReport(...)` constructor accepts `wallet`, `overall_verdict`,
`gate_result`, `trading_mode`, `n_closed_positions`, and
`archetype_layer1`. If any field changed, update the fixture to set that value
after construction rather than guessing.

- [ ] **Step 1: Write failing rule behavior test**

Append:

```python
from sniper.analysis.wallet_protocol.types import ProtocolReport


def _base_report(wallet: str = "0x1") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        overall_verdict="data_insufficient",
        gate_result="reject_insufficient_sample",
        trading_mode="n/a_routing",
        n_closed_positions=12,
        archetype_layer1="directional",
    )


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
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_category_specialist_rule_matches_concentrated_category_edge -q
```

Expected: FAIL because predicate still returns False.

- [ ] **Step 3: Implement rule predicate**

Add helper:

```python
def _first_category_cluster(report: Any) -> dict[str, Any] | None:
    breakdown = getattr(report, "outcome_side_breakdown", None) or {}
    clusters = breakdown.get("category_clusters") or []
    return clusters[0] if clusters else None
```

Rule logic:

```python
coverage = bool(report.outcome_side_breakdown and report.data_completeness)
pattern = cluster["n"] >= 5 and abs(cluster["pnl"]) / max(abs(cluster["total_pnl"]), 1.0) >= 0.60
```

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_category_specialist_rule_matches_concentrated_category_edge -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): add category specialist seed rule"
```

---

## Task 12: Held Prediction Edge Rule

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Modify: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Write failing test**

Append:

```python
def test_held_prediction_edge_rule_requires_held_profit_and_timing_quality() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["held_prediction_edge_v1"]
    report = _base_report()
    report.exit_attribution = {
        "coverage": {"n_with_resolved_attribution": 8},
        "held_to_resolution": {"pnl": 700.0, "share_of_total_pnl": 0.70},
    }
    report.held_analyst_branch = {"hypotheses": [{"id": "H2", "verdict": "supported"}]}
    report.timing_surface = {"confidence_band": "partial", "classification": "holding_driven"}
    report.data_completeness = {"quality": "partial", "caveats": []}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True
```

- [ ] **Step 2: Run test to verify failure**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_held_prediction_edge_rule_requires_held_profit_and_timing_quality -q
```

Expected: FAIL.

- [ ] **Step 3: Implement predicate**

Rule logic:

```python
held = exit_attribution["held_to_resolution"]
coverage_ok = coverage["n_with_resolved_attribution"] >= 5
timing_ok = timing_surface["confidence_band"] not in {"underpowered", "unavailable"}
pattern = held["share_of_total_pnl"] >= 0.50 and held["pnl"] > 0 and timing_ok
```

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_held_prediction_edge_rule_requires_held_profit_and_timing_quality -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): add held prediction seed rule"
```

---

## Task 13: Pre-Resolution Exit Discipline Rule

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Modify: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Write failing test**

```python
def test_pre_resolution_exit_discipline_rule_matches_profitable_exits() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["pre_resolution_exit_discipline_v1"]
    report = _base_report()
    report.exit_attribution = {
        "coverage": {"n_with_resolved_attribution": 7},
        "profitable_pre_resolution_exit": {"pnl": 650.0, "share_of_total_pnl": 0.65},
    }
    report.execution_quality_summary = {"n_exits": 7}
    report.timing_surface = {"confidence_band": "partial", "classification": "trading_driven"}

    assert rule.coverage_predicate(report, {}) is True
    assert rule.pattern_predicate(report, {}) is True
```

- [ ] **Step 2: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_pre_resolution_exit_discipline_rule_matches_profitable_exits -q
```

Expected: FAIL.

- [ ] **Step 3: Implement predicate**

Use `profitable_pre_resolution_exit.share_of_total_pnl >= 0.50`, positive PnL, and `n_with_resolved_attribution >= 5`.

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_pre_resolution_exit_discipline_rule_matches_profitable_exits -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): add pre-resolution exit seed rule"
```

---

## Task 14: Position-Building Seed Rules

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Modify: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Write failing tests for gradual/front-loaded rules**

```python
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

    rule = {r.rule_id: r for r in default_seed_rules()}["gradual_accumulation_pattern_v1"]
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
```

- [ ] **Step 2: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_gradual_accumulation_rule_matches_position_builder_pattern tests/test_strategy_pattern_audit_seed_rules.py::test_front_loaded_conviction_rule_matches_front_loaded_pattern -q
```

Expected: FAIL.

- [ ] **Step 3: Implement predicates**

Gradual rule:

```python
pattern = wallet_summary["dominant_accumulation_pattern"] == "gradual_accumulation"
coverage = observation_window_coverage >= 0.50
```

Front-loaded rule:

```python
pattern = wallet_summary["dominant_accumulation_pattern"] == "front_loaded"
coverage = observation_window_coverage >= 0.50 and outcome_side_breakdown exists
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_gradual_accumulation_rule_matches_position_builder_pattern tests/test_strategy_pattern_audit_seed_rules.py::test_front_loaded_conviction_rule_matches_front_loaded_pattern -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): add position-building seed rules"
```

---

## Task 15: Needs-Data, Timing Capture, and Noise Rules

**Files:**
- Modify: `sniper/analysis/strategy_pattern_audit/seed_rules.py`
- Modify: `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Write failing tests**

```python
def test_liquidity_rule_declares_blocking_data_needs() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["liquidity_opportunist_needs_data_v1"]

    assert rule.needs_data == ["order_book_depth", "slippage_model"]
    assert "order_book_depth" in rule.simulation_dataset_requirements


def test_timing_capture_rule_matches_trading_driven_timing() -> None:
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    rule = {r.rule_id: r for r in default_seed_rules()}["timing_capture_edge_v1"]
    report = _base_report()
    report.timing_surface = {"confidence_band": "partial", "classification": "trading_driven"}
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
```

- [ ] **Step 2: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_liquidity_rule_declares_blocking_data_needs tests/test_strategy_pattern_audit_seed_rules.py::test_timing_capture_rule_matches_trading_driven_timing tests/test_strategy_pattern_audit_seed_rules.py::test_noise_or_luck_rule_rejects_concentrated_underpowered_wallet -q
```

Expected: FAIL for timing/noise behavior if predicates are still false.

- [ ] **Step 3: Implement predicates**

Timing capture:

```python
classification in {"trading_driven", "mixed"} and confidence_band not in {"underpowered", "unavailable"}
```

Noise/luck anti-pattern:

```python
n_closed_positions < 5 or timing_surface.confidence_band == "underpowered" or top_market_pnl_share >= 0.80
```

Liquidity rule:

- Leave `pattern_predicate` conservative.
- Keep `needs_data=["order_book_depth", "slippage_model"]`.

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_seed_rules.py::test_liquidity_rule_declares_blocking_data_needs tests/test_strategy_pattern_audit_seed_rules.py::test_timing_capture_rule_matches_trading_driven_timing tests/test_strategy_pattern_audit_seed_rules.py::test_noise_or_luck_rule_rejects_concentrated_underpowered_wallet -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit/seed_rules.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "feat(strategy): add timing and data-gap seed rules"
```

---

## Task 16: End-to-End Thesis Promotion and Rejection

**Files:**
- Modify: `tests/test_strategy_pattern_audit_core.py`
- Modify: `sniper/analysis/strategy_pattern_audit/core.py`

- [ ] **Step 1: Write failing end-to-end test**

Append:

```python
def test_end_to_end_promotes_clean_thesis_and_keeps_rejections() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    reports = []
    for idx in range(6):
        archetype = "directional" if idx % 2 == 0 else "specialist"
        report = _quality_report(f"0x{idx}", quality="partial", archetype=archetype)
        report.outcome_side_breakdown = {
            "category_clusters": [
                {"category": "politics", "pnl": 900.0, "total_pnl": 1000.0, "n": 10}
            ]
        }
        report.extractor_features_summary = {"top_category": "politics"}
        reports.append(report)

    # One anti-pattern example for symmetry.
    anti = _quality_report("0xanti", quality="partial", archetype="other")
    anti.outcome_side_breakdown = {
        "category_clusters": [
            {"category": "sports", "pnl": 100.0, "total_pnl": 1000.0, "n": 10}
        ]
    }
    anti.extractor_features_summary = {"top_category": "sports"}
    reports.append(anti)

    result = compute_strategy_pattern_audit(
        reports,
        rules=[r for r in default_seed_rules() if r.rule_id == "category_specialist_v1"],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 2,
        },
    )

    assert [t["rule_id"] for t in result["theses"]] == ["category_specialist_v1"]
    thesis = result["theses"][0]
    assert thesis["n_supporting"] == 6
    assert thesis["n_rejected"] == 1
    assert thesis["support_share"] == 6 / 7
    assert thesis["honesty_bar"]["passed"] is True
    assert thesis["anti_pattern_examples"][0]["wallet"] == "0xanti"
```

- [ ] **Step 2: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_end_to_end_promotes_clean_thesis_and_keeps_rejections -q
```

Expected: FAIL if honesty bar lacks anti-pattern handling, support-share precision, or seed-rule aggregation.

- [ ] **Step 3: Fix core aggregation**

Ensure:

- supporting evaluations produce `supporting_wallets`,
- rejected evaluations produce `anti_pattern_examples`,
- promoted theses include `mechanism_category`, `false_positive_risks`, `simulation_dataset_requirements`,
- rejected theses remain when honesty bar fails.

- [ ] **Step 4: Run test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_end_to_end_promotes_clean_thesis_and_keeps_rejections -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py
git commit -m "feat(strategy): promote clean strategy theses"
```

---

## Task 17: Fixture/Report Smoke Without Analyzer Batch

**Files:**
- Modify: `tests/test_strategy_pattern_audit_core.py`

- [ ] **Step 1: Write smoke test over local fixture/report only**

Append:

```python
import json
import pytest


def _load_existing_report_like_fixture() -> list[ProtocolReport]:
    candidates = [
        Path("tests/calibration/baselines/swissmiss_phase0_informational.json"),
        Path("tests/calibration/baselines/yatsen_phase0_informational.json"),
    ]
    reports: list[ProtocolReport] = []
    for path in candidates:
        if not path.exists():
            continue
        raw = json.loads(path.read_text())
        wallet = raw.get("wallet") or raw.get("address") or path.stem
        report = ProtocolReport(
            wallet=wallet,
            overall_verdict=raw.get("overall_verdict", "data_insufficient"),
            gate_result=raw.get("gate_result", "unknown"),
            trading_mode=raw.get("trading_mode", "unknown"),
        )
        for field in [
            "outcome_side_breakdown",
            "exit_attribution",
            "data_completeness",
            "timing_surface",
            "position_building_surface",
            "extractor_features_summary",
            "execution_quality_summary",
            "data_quality_flags",
            "applicable_limitations",
            "open_questions",
            "held_analyst_branch",
            "flipper_branch",
        ]:
            if field in raw:
                setattr(report, field, raw[field])
        reports.append(report)
    return reports


def test_fixture_report_smoke_uses_existing_local_reports_only() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = _load_existing_report_like_fixture()
    if not reports:
        pytest.skip("No replayable local baseline reports found; no live API or analyzer batch allowed")

    result = compute_strategy_pattern_audit(reports)

    assert result is not None
    assert result["generated_from"]["n_reports"] == len(reports)
    assert "theses" in result
    assert "rejected_theses" in result
```

- [ ] **Step 2: Run smoke test**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py::test_fixture_report_smoke_uses_existing_local_reports_only -q
```

Expected: PASS or SKIP with clear reason. It must not call live APIs, run analyzer batches, or generate fixtures.

- [ ] **Step 3: Commit**

```bash
git add tests/test_strategy_pattern_audit_core.py
git commit -m "test(strategy): smoke strategy audit on local reports"
```

---

## Task 18: Full Phase 1 Verification

**Files:**
- Modify only if a verification failure exposes a real Phase 1 bug:
  - `sniper/analysis/strategy_pattern_audit/*.py`
  - `tests/test_strategy_pattern_audit_core.py`
  - `tests/test_strategy_pattern_audit_seed_rules.py`

- [ ] **Step 1: Run strategy audit tests**

```bash
python3 -m pytest tests/test_strategy_pattern_audit_core.py tests/test_strategy_pattern_audit_seed_rules.py -q
```

Expected: all pass.

- [ ] **Step 2: Run schema validator**

```bash
python3 scripts/validate_schema_refs.py
```

Expected:

```text
No schema violations found.
```

- [ ] **Step 3: Run boundary greps**

```bash
rg -n "recommended_trigger|trigger_threshold|copy_after_first_buy|copy_after_n_buys|expected_copy_pnl|simulated_copy_result|copy_strategy|copy_signal|trade_signal|deploy_recommendation" sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py tests/test_strategy_pattern_audit_seed_rules.py
```

Expected: no hits except inside test constants.

```bash
rg -n "price-history|prices-history|requests|httpx|urllib|duckdb|supabase|analyze_wallet|wallet_protocol.report|ledger" sniper/analysis/strategy_pattern_audit
```

Expected: no hits.

- [ ] **Step 4: Check changed files**

```bash
git diff --name-only origin/main..HEAD
```

Expected changed implementation files are limited to:

```text
sniper/analysis/strategy_pattern_audit/__init__.py
sniper/analysis/strategy_pattern_audit/core.py
sniper/analysis/strategy_pattern_audit/rules.py
sniper/analysis/strategy_pattern_audit/seed_rules.py
tests/test_strategy_pattern_audit_core.py
tests/test_strategy_pattern_audit_seed_rules.py
```

- [ ] **Step 5: Final commit if verification-only fixes were needed**

Only if Step 1-4 required fixes:

```bash
git add sniper/analysis/strategy_pattern_audit tests/test_strategy_pattern_audit_core.py tests/test_strategy_pattern_audit_seed_rules.py
git commit -m "fix(strategy): finalize strategy audit phase1 guards"
```

---

## Final Acceptance Checklist

- [ ] `ThesisRule` dataclass exists.
- [ ] `RuleEvaluation` dataclass exists.
- [ ] `compute_strategy_pattern_audit(...)` returns `None` for zero usable reports.
- [ ] Catalog output includes `generated_from`, `population_quality`, `theses`, `rejected_theses`, `data_gaps`, `top_level_caveats`.
- [ ] Operator-prior relaxations clamp to hard lower bounds.
- [ ] Denominator contract is implemented: `n_evaluable = n_supporting + n_rejected`.
- [ ] Inconclusive wallets do not enter `n_evaluable`.
- [ ] Honesty bar rejects weak/noisy theses.
- [ ] Blocking data gaps prevent thesis promotion and roll up to top-level `data_gaps`.
- [ ] All eight seed rules exist with exact spec IDs:
  - `category_specialist_v1`
  - `held_prediction_edge_v1`
  - `pre_resolution_exit_discipline_v1`
  - `gradual_accumulation_pattern_v1`
  - `front_loaded_conviction_v1`
  - `liquidity_opportunist_needs_data_v1`
  - `timing_capture_edge_v1`
  - `noise_or_luck_rejection_v1`
- [ ] Recursive forbidden-key/string test exists.
- [ ] No-HTTP test exists.
- [ ] AST import boundary test exists.
- [ ] Fixture/report smoke uses existing local files only and skips if absent.
- [ ] No `ProtocolReport` mutation test exists.
- [ ] No implementation touches `ProtocolReport`, `analyze_wallet.py`, report rendering, ledger, DB, migrations, services, or legacy low-buy streams.

## Plan-Only Verification

For this plan-only commit, run:

```bash
python3 scripts/validate_schema_refs.py
git status --short
```

Expected:

```text
No schema violations found.
```

Only this plan file should be changed before commit.
