"""SPA tag-grain-only evidence caveat integration — failing acceptance suite.

Spec: docs/superpowers/specs/2026-05-12-tag-grain-rescue-honesty-floor.md
Section: SPA caveat integration (test matrix rows #29–31)

These tests encode:

  29. A report carrying BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY (any branch) cannot
      transition to MATCHES in _evaluate_rule. Returns INCONCLUSIVE with
      reason="tag_grain_only_evidence" and the flag surfaced in coverage_caveats.

  30. A report with NEGATIVE_PNL_EDGE_UNEXPLAINED (no tag-grain flag) follows
      pre-existing behavior — no new semantics added in this PR.

  31. A report carrying BOTH flags → INCONCLUSIVE with both flags in
      coverage_caveats; rule is not promoted to MATCHES and not silently
      REJECTED with reason="no_match".

All tests MUST fail in Phase 1 because core.py has not yet been extended
to check for BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY in _evaluate_rule.

The BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY constant is imported from analyze_wallet
(or checked as a string prefix) so a rename in Phase 2 forces an explicit fix,
not a silent green.
"""
from __future__ import annotations

import pytest

from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit
from sniper.analysis.strategy_pattern_audit.rules import INCONCLUSIVE, MATCHES, REJECTED
from sniper.analysis.wallet_protocol import analyze_wallet as aw_mod
from sniper.analysis.wallet_protocol.types import ProtocolReport


# ---------------------------------------------------------------------------
# Constant reference — pin so Phase 2 rename forces a test fix, not silence.
# ---------------------------------------------------------------------------


def _tag_grain_const() -> str:
    obj = getattr(aw_mod, "BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY", None)
    if obj is None:
        pytest.fail(
            "expected `sniper.analysis.wallet_protocol.analyze_wallet"
            ".BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` to exist; "
            "implementation has not landed yet"
        )
    return obj


# ---------------------------------------------------------------------------
# Fixture builders (mirror test_strategy_pattern_audit_core.py style)
# ---------------------------------------------------------------------------


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
    quality: str = "partial",
    archetype: str = "directional",
) -> ProtocolReport:
    report = _report(wallet)
    report.n_closed_positions = 10
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
    report.exit_attribution = {"coverage": {"n_with_resolved_attribution": 10}}
    report.outcome_side_breakdown = {"n": 10}
    return report


def _rule_matching():
    """A ThesisRule whose pattern_predicate always returns True (would match clean reports)."""
    from sniper.analysis.strategy_pattern_audit.rules import ThesisRule

    return ThesisRule(
        rule_id="tag_grain_caveat_test_rule_v1",
        title="Tag-grain caveat test rule",
        mechanism_category="category_specialist",
        description="Rule used to test that BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY blocks MATCHES",
        surfaces_consumed=["data_completeness"],
        operator_priors_used=["min_population_evaluated"],
        forbidden_archetypes=[],
        minimum_n_per_wallet=0,
        needs_data=[],
        false_positive_risks=["test"],
        simulation_dataset_requirements=["test_dataset"],
        coverage_predicate=lambda report, priors: True,
        anti_pattern_predicate=lambda report, priors: False,
        pattern_predicate=lambda report, priors: True,  # always would match
        evidence_builder=lambda report: [],
        promotable=True,
    )


# ---------------------------------------------------------------------------
# Test #29 — Report with BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY → INCONCLUSIVE
# ---------------------------------------------------------------------------


def test_spa_tag_grain_flag_blocks_matches():
    """#29 — A ThesisRule whose pattern_predicate returns True on a clean report
    returns INCONCLUSIVE (not MATCHES) when the report carries
    BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY. The reason is 'tag_grain_only_evidence'
    and the flag string appears in coverage_caveats."""
    const = _tag_grain_const()
    flag_string = f"{const}: held_analyst (grain=nav/politics, primaries_failed_oos=['H2'])"

    # Build a quality report that would normally match.
    report = _quality_report("0xswissmiss", quality="partial", archetype="directional")
    report.data_quality_flags = [flag_string]

    rule = _rule_matching()

    result = compute_strategy_pattern_audit(
        [report],
        rules=[rule],
        operator_priors={
            "min_population_evaluated": 1,
            "min_supporting": 1,
            "min_observation_quality_share": 0.0,
            "min_archetype_diversity": 1,
            "min_n_closed_positions_per_wallet": 0,
        },
    )

    assert result is not None
    # Rule must appear in rejected_theses (not theses), because no wallet can MATCH.
    row = next(
        (r for r in result["rejected_theses"] if r["rule_id"] == rule.rule_id), None
    )
    assert row is not None, (
        f"Rule should be in rejected_theses but was in theses: {[t['rule_id'] for t in result['theses']]}"
    )
    assert row["n_supporting"] == 0, (
        f"n_supporting should be 0 but got {row['n_supporting']}"
    )

    # Verify per-wallet evaluation — check the individual evaluation states.
    # We need to confirm the individual evaluation for 0xswissmiss is INCONCLUSIVE.
    # The aggregate row should show n_inconclusive >= 1 and n_supporting == 0.
    assert row.get("n_inconclusive", 0) >= 1, (
        f"n_inconclusive should be >=1 but row is: {row}"
    )

    # The fail_reasons or caveats should reference tag_grain_only_evidence.
    row_caveats = row.get("caveats", [])
    fail_reasons = row.get("fail_reasons", [])
    all_text = " ".join(str(x) for x in row_caveats + fail_reasons)
    assert "tag_grain_only_evidence" in all_text or any(
        const in str(c) for c in row_caveats
    ), (
        f"tag_grain_only_evidence or flag not found in caveats/fail_reasons. "
        f"caveats={row_caveats}, fail_reasons={fail_reasons}"
    )


def test_spa_tag_grain_evaluation_reason_is_tag_grain_only():
    """#29 (per-wallet detail) — The individual RuleEvaluation for a report carrying
    BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY has state=INCONCLUSIVE and
    reason='tag_grain_only_evidence', and the flag string appears in coverage_caveats.

    This test bypasses compute_strategy_pattern_audit and calls _evaluate_rule
    directly to pin the per-wallet contract precisely."""
    from sniper.analysis.strategy_pattern_audit.core import _evaluate_rule
    from sniper.analysis.strategy_pattern_audit.rules import normalize_operator_priors

    const = _tag_grain_const()
    flag_string = f"{const}: held_analyst (grain=nav/politics, primaries_failed_oos=['H2'])"

    report = _quality_report("0xswissmiss", quality="partial", archetype="directional")
    report.data_quality_flags = [flag_string]

    rule = _rule_matching()
    priors, _ = normalize_operator_priors({
        "min_n_closed_positions_per_wallet": 0,
    })

    evaluation = _evaluate_rule(report, rule, priors)

    assert evaluation.state == INCONCLUSIVE, (
        f"Expected INCONCLUSIVE, got {evaluation.state!r} (reason={evaluation.reason!r})"
    )
    assert evaluation.reason == "tag_grain_only_evidence", (
        f"Expected reason='tag_grain_only_evidence', got {evaluation.reason!r}"
    )
    # Flag string must appear in coverage_caveats.
    assert any(const in c for c in evaluation.coverage_caveats), (
        f"Flag not in coverage_caveats: {evaluation.coverage_caveats}"
    )


# ---------------------------------------------------------------------------
# Test #30 — NEGATIVE_PNL_EDGE_UNEXPLAINED alone follows pre-existing behavior
# ---------------------------------------------------------------------------


def test_spa_negative_pnl_flag_alone_follows_preexisting_behavior():
    """#30 — A report with NEGATIVE_PNL_EDGE_UNEXPLAINED (but NO tag-grain flag)
    follows pre-existing SPA behavior at the per-wallet _evaluate_rule level.
    The negative-PnL flag must NOT trigger INCONCLUSIVE with
    reason='tag_grain_only_evidence'. The rule should evaluate to MATCHES
    (pattern_predicate=True, anti=False, no blocking data, coverage ok)."""
    from sniper.analysis.strategy_pattern_audit.core import _evaluate_rule
    from sniper.analysis.strategy_pattern_audit.rules import normalize_operator_priors

    neg_pnl_const = getattr(aw_mod, "NEGATIVE_PNL_EDGE_UNEXPLAINED", None)
    if neg_pnl_const is None:
        pytest.fail(
            "NEGATIVE_PNL_EDGE_UNEXPLAINED not found in analyze_wallet; "
            "Phase 2 for Issue #24 has not landed yet"
        )
    const_tag = _tag_grain_const()

    # Report has NEGATIVE_PNL_EDGE_UNEXPLAINED but NOT the tag-grain flag.
    report = _quality_report("0xpestle", quality="partial", archetype="directional")
    report.data_quality_flags = [neg_pnl_const]

    rule = _rule_matching()
    priors, _ = normalize_operator_priors({
        "min_n_closed_positions_per_wallet": 0,
    })

    evaluation = _evaluate_rule(report, rule, priors)

    # Negative-PnL flag alone must NOT produce INCONCLUSIVE with tag_grain_only_evidence.
    # Pre-existing behavior: pattern_predicate=True → MATCHES.
    assert evaluation.state == MATCHES, (
        f"NEGATIVE_PNL_EDGE_UNEXPLAINED alone must not block MATCHES. "
        f"state={evaluation.state!r}, reason={evaluation.reason!r}"
    )
    assert evaluation.reason != "tag_grain_only_evidence", (
        f"NEGATIVE_PNL_EDGE_UNEXPLAINED must not produce tag_grain_only_evidence reason"
    )
    # Tag-grain flag must not appear in coverage_caveats.
    assert not any(const_tag in c for c in evaluation.coverage_caveats), (
        f"Tag-grain flag must not be in coverage_caveats for neg-PnL-only report: "
        f"{evaluation.coverage_caveats}"
    )


# ---------------------------------------------------------------------------
# Test #31 — Report with BOTH flags → INCONCLUSIVE (not MATCHES, not silent REJECTED)
# ---------------------------------------------------------------------------


def test_spa_both_flags_gives_inconclusive_not_matches_not_rejected():
    """#31 — A report carrying both NEGATIVE_PNL_EDGE_UNEXPLAINED and
    BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY → per-wallet evaluation is INCONCLUSIVE
    with reason='tag_grain_only_evidence'. The rule is NOT promoted to MATCHES
    and NOT silently REJECTED with reason='no_match'."""
    from sniper.analysis.strategy_pattern_audit.core import _evaluate_rule
    from sniper.analysis.strategy_pattern_audit.rules import normalize_operator_priors

    const_tag = _tag_grain_const()
    const_neg = getattr(aw_mod, "NEGATIVE_PNL_EDGE_UNEXPLAINED", None)
    if const_neg is None:
        pytest.fail("NEGATIVE_PNL_EDGE_UNEXPLAINED not found; Phase 2 for Issue #24 has not landed")

    flag_tag = f"{const_tag}: held_analyst (grain=nav/politics, primaries_failed_oos=['H2'])"
    report = _quality_report("0xbothflags", quality="partial", archetype="directional")
    report.data_quality_flags = [const_neg, flag_tag]

    rule = _rule_matching()
    priors, _ = normalize_operator_priors({
        "min_n_closed_positions_per_wallet": 0,
    })

    evaluation = _evaluate_rule(report, rule, priors)

    assert evaluation.state == INCONCLUSIVE, (
        f"Expected INCONCLUSIVE with both flags, got state={evaluation.state!r} "
        f"(reason={evaluation.reason!r})"
    )
    assert evaluation.reason == "tag_grain_only_evidence", (
        f"Expected reason='tag_grain_only_evidence' (tag-grain takes priority over no_match), "
        f"got {evaluation.reason!r}"
    )
    # Both flags must be surfaced in coverage_caveats — containment, not equality
    # (other caveats may legitimately co-exist).
    assert any(const_tag in c for c in evaluation.coverage_caveats), (
        f"BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY not in coverage_caveats: {evaluation.coverage_caveats}"
    )
    assert any(const_neg in c for c in evaluation.coverage_caveats), (
        f"NEGATIVE_PNL_EDGE_UNEXPLAINED not in coverage_caveats: {evaluation.coverage_caveats}. "
        f"Both honesty-floor flags must surface when a report carries both (spec §31)."
    )
