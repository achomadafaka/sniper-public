"""Strategy Pattern Audit Phase 1 core tests."""
from __future__ import annotations

import ast
import copy
import dataclasses
import http.client
import json
import urllib.request
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.types import ProtocolReport


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "sniper"
    / "analysis"
    / "strategy_pattern_audit"
)


def _report(wallet: str = "0xabc") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        overall_verdict="data_insufficient",
        gate_result="reject_insufficient_sample",
        trading_mode="n/a_routing",
    )


def _quality_report(
    wallet: str,
    *,
    quality: str,
    archetype: str = "directional",
) -> ProtocolReport:
    report = _report(wallet)
    report.n_closed_positions = 5
    report.archetype_layer1 = archetype
    report.data_completeness = {
        "quality": quality,
        "coverage": {
            "share_with_partial_or_better": 1.0
            if quality in {"partial", "medium", "good", "high"}
            else 0.0
        },
        "caveats": [],
    }
    report.timing_surface = {"confidence_band": quality, "coverage_caveats": []}
    report.position_building_surface = {
        "wallet_summary": {"observation_window_coverage": 1.0},
        "observation_window_quality_distribution": {
            quality: {"n": 1, "share": 1.0}
        },
    }
    report.exit_attribution = {"coverage": {"n_with_resolved_attribution": 5}}
    report.outcome_side_breakdown = {"n": 5}
    return report


def _rule(
    *,
    rule_id: str = "test_rule_v1",
    mechanism_category: str = "category_specialist",
    coverage=True,
    anti=False,
    pattern=True,
    needs_data=None,
    minimum_n_per_wallet=0,
    promotable=True,
):
    from sniper.analysis.strategy_pattern_audit.rules import ThesisRule

    return ThesisRule(
        rule_id=rule_id,
        title="Test rule",
        mechanism_category=mechanism_category,
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
                "path": "data_completeness.quality",
                "value": (report.data_completeness or {}).get("quality"),
                "predicate": "eq",
                "threshold": "partial",
                "provenance": "factual",
            }
        ],
        promotable=promotable,
    )


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


def test_returns_none_for_zero_reports() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    assert compute_strategy_pattern_audit([]) is None


def test_returns_catalog_with_empty_theses_when_no_rules() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit(
        [_report()], rules=[], audit_run_id="audit-1"
    )

    assert result["audit_run_id"] == "audit-1"
    assert result["surface_version"] == "strategy_pattern_audit_v1"
    assert result["generated_from"]["n_reports"] == 1
    assert result["generated_from"]["report_protocol_versions"] == {"v2": 1}
    assert result["population_quality"]["median_n_closed_positions"] == 0
    assert result["population_quality"]["n_excluded_by_zero_n_closed_positions"] == 1
    assert result["population_quality"]["surface_quality_distribution"] == {
        "data_completeness": {"unavailable": 1},
        "timing_surface": {"unavailable": 1},
        "position_building_surface": {"unavailable": 1},
        "exit_attribution": {"unavailable": 1},
        "edge_surface": {"unavailable": 1},
    }
    assert result["theses"] == []
    assert result["rejected_theses"] == []
    assert result["data_gaps"] == []
    assert result["top_level_caveats"] == ["NO_RULES_EVALUATED"]


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


def test_evaluation_counts_exclude_inconclusive_from_evaluable() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [_quality_report(f"0x{i}", quality="partial") for i in range(3)]
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
    assert rejected["inconclusive_rule"]["fields_provenance"]["support_share"] == "unavailable"
    assert rejected["rejected_rule"]["n_evaluable"] == 3
    assert rejected["rejected_rule"]["n_rejected"] == 3


def test_non_promotable_rule_has_no_promotion_threshold() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [
        _quality_report(f"0x{i}", quality="partial", archetype=str(i % 2))
        for i in range(5)
    ]
    result = compute_strategy_pattern_audit(
        reports,
        rules=[
                _rule(
                    rule_id="descriptive_rejection",
                    mechanism_category="noise_or_luck",
                    anti=True,
                    promotable=False,
                )
        ],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    row = result["rejected_theses"][0]
    assert row["honesty_bar"]["passed"] is False
    assert row["fail_reasons"] == ["descriptive_rejection_rule"]
    assert row["would_have_been_promoted_at"] is None


def test_directional_rules_apply_operator_forbidden_archetypes() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _quality_report("0xarb", quality="partial", archetype="arb_fund")

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


def test_honesty_bar_rejects_single_archetype_support() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [
        _quality_report(f"0x{i}", quality="partial", archetype="same")
        for i in range(6)
    ]
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

    assert "archetype_independence" in result["rejected_theses"][0]["fail_reasons"]


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


def test_unknown_surface_quality_is_inconclusive_coverage() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _quality_report("0xweird", quality="strange", archetype="a")
    result = compute_strategy_pattern_audit(
        [report],
        rules=[_rule()],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    row = result["rejected_theses"][0]
    assert row["n_evaluable"] == 0
    assert row["n_inconclusive"] == 1
    assert "UNKNOWN_SURFACE_QUALITY:strange" in row["caveats"]


def test_anti_pattern_symmetry_requires_real_anti_pattern_examples() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    supporters = [
        _quality_report(f"0xs{i}", quality="partial", archetype="directional")
        for i in range(5)
    ]
    forbidden_reject = _quality_report(
        "0xarb", quality="partial", archetype="arb_fund"
    )

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


def test_blocking_data_need_rejects_thesis_and_rolls_up_gap() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    reports = [
        _quality_report(f"0x{i}", quality="partial", archetype=str(i % 2))
        for i in range(5)
    ]
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
    assert result["rejected_theses"][0]["would_have_been_promoted_at"][
        "data_fields"
    ] == ["order_book_depth"]
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


def test_non_blocking_data_need_promotes_thesis_and_rolls_up_partial_gap() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit
    from sniper.analysis.strategy_pattern_audit.rules import ThesisRule

    reports = [
        _quality_report(f"0x{i}", quality="partial", archetype=str(i % 2))
        for i in range(5)
    ]

    def _anti(report, priors):
        return report.wallet == "0x4"

    def _pattern(report, priors):
        return report.wallet != "0x4"

    rule = ThesisRule(
        rule_id="partial_gap_rule",
        title="Partial gap rule",
        mechanism_category="category_specialist",
        description="Pattern observed with a non-blocking data gap",
        surfaces_consumed=["data_completeness"],
        operator_priors_used=["min_population_evaluated"],
        forbidden_archetypes=[],
        minimum_n_per_wallet=0,
        needs_data=[
            {
                "field_name": "liquidity_context",
                "blocking": False,
                "rationale": "Adds interpretation context.",
                "candidate_sources": ["ledger"],
            }
        ],
        false_positive_risks=["synthetic test risk"],
        simulation_dataset_requirements=["test_dataset"],
        coverage_predicate=lambda report, priors: True,
        anti_pattern_predicate=_anti,
        pattern_predicate=_pattern,
        evidence_builder=lambda report: [],
    )

    result = compute_strategy_pattern_audit(
        reports,
        rules=[rule],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    assert [row["rule_id"] for row in result["theses"]] == ["partial_gap_rule"]
    assert result["theses"][0]["data_needs"][0]["blocking"] is False
    assert result["data_gaps"] == [
        {
            "field_name": "liquidity_context",
            "blocking_n_theses": 0,
            "partial_n_theses": 1,
            "blocked_thesis_ids": [],
            "partial_thesis_ids": ["partial_gap_rule"],
            "candidate_sources": ["ledger"],
            "operator_priority_hint": None,
        }
    ]


def test_quality_labels_measure_exit_and_edge_strength() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    high = _quality_report("0xgood", quality="partial", archetype="a")
    high.exit_attribution = {
        "coverage": {"n_total": 10, "n_with_resolved_attribution": 9}
    }
    high.outcome_side_breakdown = {"n": 40}

    low = _quality_report("0xpoor", quality="partial", archetype="b")
    low.exit_attribution = {
        "coverage": {"n_total": 10, "n_with_resolved_attribution": 1}
    }
    low.outcome_side_breakdown = {"n": 2}

    result = compute_strategy_pattern_audit([high, low], rules=[])

    distribution = result["population_quality"]["surface_quality_distribution"]
    assert distribution["exit_attribution"] == {"good": 1, "poor": 1}
    assert distribution["edge_surface"] == {"good": 1, "poor": 1}


def test_edge_quality_reads_real_side_bucket_shape() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _quality_report("0xside", quality="partial", archetype="a")
    report.outcome_side_breakdown = {
        "yes": {"n": 18},
        "no": {"n": 9},
        "other": {"n": 4},
    }

    result = compute_strategy_pattern_audit([report], rules=[])

    assert result["population_quality"]["surface_quality_distribution"][
        "edge_surface"
    ] == {"good": 1}


def test_missing_surface_quality_counts_as_unavailable_for_all_surfaces() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    result = compute_strategy_pattern_audit([_report()], rules=[])

    assert result["population_quality"]["share_with_partial_or_better"] == 0.0
    assert result["population_quality"]["surface_quality_distribution"] == {
        "data_completeness": {"unavailable": 1},
        "timing_surface": {"unavailable": 1},
        "position_building_surface": {"unavailable": 1},
        "exit_attribution": {"unavailable": 1},
        "edge_surface": {"unavailable": 1},
    }


def test_unknown_data_completeness_caveat_is_not_default_good() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    report = _quality_report("0xunknown", quality="partial", archetype="a")
    report.data_completeness = {"caveats": {"new_unrecognized_caveat": True}}

    result = compute_strategy_pattern_audit(
        [report],
        rules=[_rule()],
        operator_priors={
            "min_population_evaluated": 5,
            "min_supporting": 2,
            "min_observation_quality_share": 0.20,
            "min_archetype_diversity": 1,
        },
    )

    row = result["rejected_theses"][0]
    assert row["n_evaluable"] == 0
    assert "UNKNOWN_SURFACE_QUALITY:unknown_caveat" in row["caveats"]


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


def test_compute_strategy_pattern_audit_makes_no_http_calls(monkeypatch) -> None:
    requests = pytest.importorskip("requests")
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit

    calls = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("HTTP call attempted")

    monkeypatch.setattr(requests, "get", _record, raising=False)
    monkeypatch.setattr(requests, "post", _record, raising=False)
    monkeypatch.setattr(requests.Session, "get", _record, raising=False)
    monkeypatch.setattr(requests.Session, "post", _record, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _record, raising=False)
    monkeypatch.setattr(http.client.HTTPConnection, "request", _record, raising=False)
    monkeypatch.setattr(http.client.HTTPSConnection, "request", _record, raising=False)
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None:
        monkeypatch.setattr(httpx, "get", _record, raising=False)
        monkeypatch.setattr(httpx, "post", _record, raising=False)
    try:
        import aiohttp
    except ImportError:
        aiohttp = None
    if aiohttp is not None:
        monkeypatch.setattr(aiohttp.ClientSession, "get", _record, raising=False)
        monkeypatch.setattr(aiohttp.ClientSession, "post", _record, raising=False)

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


def test_end_to_end_promotes_clean_thesis_and_keeps_rejections() -> None:
    from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit
    from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules

    reports = []
    for idx in range(6):
        archetype = "directional" if idx % 2 == 0 else "specialist"
        report = _quality_report(f"0x{idx}", quality="partial", archetype=archetype)
        report.outcome_side_breakdown = {
            "category_clusters": [
                {
                    "category": "politics",
                    "pnl": 900.0,
                    "total_pnl": 1000.0,
                    "n": 10,
                }
            ]
        }
        report.extractor_features_summary = {"top_category": "politics"}
        reports.append(report)

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
        rules=[
            r for r in default_seed_rules() if r.rule_id == "category_specialist_v1"
        ],
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
            gate_result=raw.get("gate_result", "reject_insufficient_sample"),
            trading_mode=raw.get("trading_mode", "n/a_routing"),
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
        pytest.skip("No replayable local baseline reports found")

    result = compute_strategy_pattern_audit(reports)

    assert result is not None
    assert result["generated_from"]["n_reports"] == len(reports)
    assert "theses" in result
    assert "rejected_theses" in result
