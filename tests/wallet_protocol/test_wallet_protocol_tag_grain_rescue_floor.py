"""Tag-grain rescue honesty floor — failing acceptance suite.

Spec: docs/superpowers/specs/2026-05-12-tag-grain-rescue-honesty-floor.md

These tests encode spec test matrix rows 1–28:
  - Predicate-direct tests (held branch): #1–12b
  - Predicate-direct tests (flipper branch): #13–14
  - Emission shape tests: #15–18
  - Doctrine + scope guards: #19–25
  - Stacking with negative-PnL floor: #26–28

Tests MUST all fail in Phase 1 because the helpers do not exist yet:
  - BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY : str constant in analyze_wallet
  - _branch_promoted_on_tag_grain_only(branch) : predicate helper
  - _apply_tag_grain_rescue_caveat(report) : report-level emitter

Rows 32–33 (live-cohort fixture replay) are skipped — the JSON captures
under /tmp/calibration-pass-2026-05-12/ are not checked into the repo.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from sniper.analysis.wallet_protocol import analyze_wallet as aw_mod
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
    TagGrainResult,
    TimeSplitResult,
)


# ---------------------------------------------------------------------------
# Module-symbol helpers — fail at call-site when implementation hasn't landed.
# ---------------------------------------------------------------------------


def _require(symbol_name: str):
    obj = getattr(aw_mod, symbol_name, None)
    if obj is None:
        pytest.fail(
            f"expected `sniper.analysis.wallet_protocol.analyze_wallet.{symbol_name}` "
            f"to exist; implementation has not landed yet"
        )
    return obj


def _const():
    return _require("BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY")


def _predicate():
    return _require("_branch_promoted_on_tag_grain_only")


def _caveat_floor():
    return _require("_apply_tag_grain_rescue_caveat")


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _hypothesis(name: str, verdict: str) -> HypothesisResult:
    return HypothesisResult(name=name, verdict=verdict)  # type: ignore[arg-type]


def _time_split(holdout_status: str) -> TimeSplitResult:
    return TimeSplitResult(
        n_total=20,
        n_train=14,
        n_test=6,
        train_p=0.05,
        test_p=0.12,
        holdout_status=holdout_status,  # type: ignore[arg-type]
    )


def _tag_grain(grain: str, tag: str, verdict: str) -> TagGrainResult:
    return TagGrainResult(
        grain=grain,  # type: ignore[arg-type]
        tag=tag,
        peer_count=15,
        confidence_flag="ok",
        n_at_grain=8,
        metric_value=0.65,
        pop_baseline=0.55,
        excess_pp=10.0,
        verdict=verdict,  # type: ignore[arg-type]
    )


def _held_branch(
    *,
    is_active: bool = True,
    hypotheses: list[HypothesisResult] | None = None,
    tag_grains: list[TagGrainResult] | None = None,
    time_split: dict[str, TimeSplitResult] | None = None,
) -> BranchReport:
    return BranchReport(
        branch="held_analyst",
        is_active=is_active,
        hypotheses=list(hypotheses or []),
        tag_grains=list(tag_grains or []),
        time_split=dict(time_split or {}),
    )


def _flipper_branch(
    *,
    is_active: bool = True,
    hypotheses: list[HypothesisResult] | None = None,
    tag_grains: list[TagGrainResult] | None = None,
    time_split: dict[str, TimeSplitResult] | None = None,
) -> BranchReport:
    return BranchReport(
        branch="flipper",
        is_active=is_active,
        hypotheses=list(hypotheses or []),
        tag_grains=list(tag_grains or []),
        time_split=dict(time_split or {}),
    )


def _make_report(
    *,
    overall_verdict: str = "edge_detected",
    net_pnl: float = 50_000.0,
    held_branch: BranchReport | None = None,
    flipper_branch: BranchReport | None = None,
    data_quality_flags: list[str] | None = None,
    applicable_limitations: list[str] | None = None,
    copyability_note: str | None = "prior note",
    primary_edge_type: str | None = "entry_timing",
    gate_result: str = "pass",
    gate_reason: str | None = None,
) -> ProtocolReport:
    r = ProtocolReport(
        wallet="0xtest",
        overall_verdict=overall_verdict,  # type: ignore[arg-type]
        net_pnl=net_pnl,
        data_quality_flags=list(data_quality_flags or []),
        applicable_limitations=list(applicable_limitations or []),
        copyability_note=copyability_note,
        primary_edge_type=primary_edge_type,
        gate_result=gate_result,  # type: ignore[arg-type]
        gate_reason=gate_reason,
    )
    r.held_analyst_branch = held_branch
    r.flipper_branch = flipper_branch
    return r


# ---------------------------------------------------------------------------
# Predicate-direct tests — held branch (#1–12b)
# ---------------------------------------------------------------------------


def test_predicate_swissmiss_shape_true():
    """#1 — SwissMiss-shape: H2=sup, H3=inc; H2 time-split failed; grain supported.
    Predicate must return TRUE."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "inconclusive"),
        ],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is True


def test_predicate_no_grain_false():
    """#2 — 6a72-shape: H3=sup, H3 time-split failed, no supported grain → FALSE.
    (Branch doesn't promote without grain anyway, but predicate guards on grain too.)"""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[_hypothesis("H3", "supported")],
        time_split={"H3": _time_split("failed")},
        tag_grains=[],  # no grain at all
    )
    assert pred(branch) is False


def test_predicate_pure_grain_promotion_true():
    """#3 — Pure-grain case (3): H2/H3=inconclusive, no time-split, grain=supported.
    Predicate must return TRUE."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "inconclusive"),
            _hypothesis("H3", "inconclusive"),
        ],
        tag_grains=[_tag_grain("leaf", "entertainment", "supported")],
    )
    assert pred(branch) is True


def test_predicate_healthy_edge_primary_survived_false():
    """#4 — Healthy edge: H2=sup with H2 time-split survived, grain present → FALSE.
    Primary survived OOS, so no caveat warranted."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("survived")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is False


def test_predicate_healthy_no_grain_false():
    """#5 — Healthy edge, no grain: H2=sup, H2 survived → FALSE."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("survived")},
        tag_grains=[],
    )
    assert pred(branch) is False


def test_predicate_two_primaries_one_survives_false():
    """#6 — Two primaries, one survives: H2=sup+failed, H3=sup+survived, grain → FALSE.
    H3 survived OOS, so wallet-level signal exists."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "supported"),
        ],
        time_split={
            "H2": _time_split("failed"),
            "H3": _time_split("survived"),
        },
        tag_grains=[_tag_grain("nav", "crypto", "supported")],
    )
    assert pred(branch) is False


def test_predicate_two_primaries_both_fail_grain_supported_true():
    """#7 — Two primaries, both fail OOS, grain supported → TRUE."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "supported"),
        ],
        time_split={
            "H2": _time_split("failed"),
            "H3": _time_split("failed"),
        },
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is True


def test_predicate_time_split_absent_false():
    """#8 — Time-split absent for H2: H2=sup but no time_split entry → FALSE.
    No failure to rescue from — primary is considered not-failed-OOS."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={},  # no entry for H2
        tag_grains=[_tag_grain("leaf", "sports", "supported")],
    )
    assert pred(branch) is False


def test_predicate_adr002_h2sub080_path_true():
    """#9 — ADR-002: H2=refuted, H2_sub080=supported, H2_sub080 time-split failed,
    grain → TRUE. H2_sub080 joins primary set per ADR-002 and also fails OOS."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "refuted"),
            _hypothesis("H2_sub080", "supported"),
        ],
        time_split={"H2_sub080": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "sports", "supported")],
    )
    assert pred(branch) is True


def test_predicate_branch_inactive_false():
    """#10 — Branch inactive (is_active=False) → always FALSE."""
    pred = _predicate()
    branch = _held_branch(
        is_active=False,
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is False


def test_predicate_branch_not_promoted_no_grain_false():
    """#11 — Branch not promoted: H2=sup time-split failed, no other supports,
    no grain → branch verdict is inconclusive, predicate must return FALSE."""
    pred = _predicate()
    # When no grain is supported and H2 is the only supported primary but fails OOS,
    # _branch_verdict returns inconclusive, so predicate guard on branch verdict fires.
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[],  # no supported grain
    )
    assert pred(branch) is False


def test_predicate_only_tag_grain_inconclusive_false():
    """#12 — H2/H3=inconclusive, grain verdict=inconclusive → FALSE.
    Grain must be 'supported', not just present."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "inconclusive"),
            _hypothesis("H3", "inconclusive"),
        ],
        tag_grains=[_tag_grain("leaf", "entertainment", "inconclusive")],
    )
    assert pred(branch) is False


def test_predicate_two_primaries_mutual_rescue_no_grain_false():
    """#12a — Two primaries, both sup, both fail OOS, no grain → FALSE.
    Spec: predicate requires at least one grain.verdict=='supported'.
    The two-primary mutual rescue case (out of scope for this PR)."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "supported"),
        ],
        time_split={
            "H2": _time_split("failed"),
            "H3": _time_split("failed"),
        },
        tag_grains=[],  # no grain
    )
    assert pred(branch) is False


def test_predicate_holdout_underpowered_is_not_failed_false():
    """#12b — holdout_status='underpowered' is NOT 'failed': H2=sup, H2=underpowered,
    grain → FALSE. Only 'failed' disqualifies; 'underpowered' means did-not-fail."""
    pred = _predicate()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("underpowered")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is False


# ---------------------------------------------------------------------------
# Predicate-direct tests — flipper branch (#13–14)
# ---------------------------------------------------------------------------


def test_predicate_flipper_mirror_swissmiss_true():
    """#13 — Flipper branch mirror of #1: FH2=sup, FH2 failed, grain → TRUE."""
    pred = _predicate()
    branch = _flipper_branch(
        hypotheses=[
            _hypothesis("FH2", "supported"),
            _hypothesis("FH4", "inconclusive"),
        ],
        time_split={"FH2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    assert pred(branch) is True


def test_predicate_flipper_healthy_edge_false():
    """#14 — Flipper branch mirror of #4: FH1=sup, FH1 survived, grain → FALSE."""
    pred = _predicate()
    branch = _flipper_branch(
        hypotheses=[_hypothesis("FH1", "supported")],
        time_split={"FH1": _time_split("survived")},
        tag_grains=[_tag_grain("leaf", "sports", "supported")],
    )
    assert pred(branch) is False


# ---------------------------------------------------------------------------
# Emission shape tests (#15–18)
# ---------------------------------------------------------------------------


def test_emission_flag_appended_to_data_quality_flags():
    """#15 — When predicate fires, data_quality_flags gains exactly one entry
    starting with 'BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY: held_analyst '.
    Pre-existing flags are preserved."""
    floor = _caveat_floor()
    const = _const()
    preexisting = ["PREEXISTING_FLAG"]
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "inconclusive"),
        ],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(
        held_branch=branch,
        data_quality_flags=list(preexisting),
    )
    floor(report)

    # Pre-existing flag still there.
    assert "PREEXISTING_FLAG" in report.data_quality_flags
    # New flag present.
    new_flags = [f for f in report.data_quality_flags if const in f]
    assert len(new_flags) == 1, f"Expected exactly 1 new flag, got: {report.data_quality_flags}"
    assert new_flags[0].startswith(f"{const}: held_analyst ")


def test_emission_applicable_limitations_appended():
    """#16 — applicable_limitations gains exactly one entry mentioning branch name
    AND the supporting grain identifier. Existing limitations preserved."""
    floor = _caveat_floor()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
        ],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    prior_limitation = "PRIOR_LIMITATION"
    report = _make_report(
        held_branch=branch,
        applicable_limitations=[prior_limitation],
    )
    floor(report)

    assert prior_limitation in report.applicable_limitations
    new_limitations = [
        lim for lim in report.applicable_limitations if lim != prior_limitation
    ]
    assert len(new_limitations) == 1
    # Must mention branch name and grain.
    lim = new_limitations[0]
    assert "held_analyst" in lim
    assert "nav" in lim or "politics" in lim, f"Grain identifier not in limitation: {lim!r}"


def test_emission_flag_contains_grain_and_primaries_failed_oos():
    """#17 — Flag string contains grain=<grain>/<tag> and primaries_failed_oos=[...]
    in deterministic order (sorted primary names)."""
    floor = _caveat_floor()
    const = _const()
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "supported"),
        ],
        time_split={
            "H2": _time_split("failed"),
            "H3": _time_split("failed"),
        },
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(held_branch=branch)
    floor(report)

    flag_entries = [f for f in report.data_quality_flags if const in f]
    assert len(flag_entries) == 1
    flag = flag_entries[0]
    assert "grain=nav/politics" in flag
    # Both failed primaries must appear in the sorted list.
    assert "H2" in flag
    assert "H3" in flag
    # The primaries_failed_oos field must be present.
    assert "primaries_failed_oos=" in flag


def test_emission_two_grains_only_first_in_sort_order_named():
    """#18 — With two supported grains, only the first in (grain, tag) sort order
    is named in the flag. Both grains remain verbatim in the branch.tag_grains."""
    floor = _caveat_floor()
    const = _const()
    grain_a = _tag_grain("leaf", "entertainment", "supported")  # sort: (leaf, entertainment)
    grain_b = _tag_grain("nav", "politics", "supported")        # sort: (nav, politics)
    # leaf < nav alphabetically, so grain_a is first.
    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
        ],
        time_split={"H2": _time_split("failed")},
        tag_grains=[grain_b, grain_a],  # deliberately unsorted to test sort stability
    )
    report = _make_report(held_branch=branch)
    floor(report)

    flag_entries = [f for f in report.data_quality_flags if const in f]
    assert len(flag_entries) == 1
    flag = flag_entries[0]
    # grain_a (leaf/entertainment) has lower sort key than grain_b (nav/politics).
    assert "grain=leaf/entertainment" in flag
    assert "grain=nav/politics" not in flag
    # Both grains still in branch (not mutated).
    assert len(report.held_analyst_branch.tag_grains) == 2


# ---------------------------------------------------------------------------
# Doctrine + scope guards (#19–25)
# ---------------------------------------------------------------------------


def test_scope_guard_overall_verdict_never_mutated():
    """#19 — overall_verdict is NEVER mutated by _apply_tag_grain_rescue_caveat."""
    floor = _caveat_floor()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(
        overall_verdict="edge_detected",
        held_branch=branch,
    )
    before_verdict = report.overall_verdict
    floor(report)
    assert report.overall_verdict == before_verdict


def test_scope_guard_primary_edge_type_never_mutated():
    """#20 — primary_edge_type is NEVER mutated."""
    floor = _caveat_floor()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(
        primary_edge_type="tag_specialist",
        held_branch=branch,
    )
    floor(report)
    assert report.primary_edge_type == "tag_specialist"


def test_scope_guard_copyability_note_never_mutated():
    """#21 — copyability_note is NEVER mutated by the tag-grain caveat floor."""
    floor = _caveat_floor()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    original_note = "Prior assembler note: H2 evidence."
    report = _make_report(
        copyability_note=original_note,
        held_branch=branch,
    )
    floor(report)
    assert report.copyability_note == original_note


def test_scope_guard_gate_result_and_reason_never_mutated():
    """#22 — gate_result and gate_reason are NEVER mutated."""
    floor = _caveat_floor()
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(
        gate_result="pass",
        gate_reason=None,
        held_branch=branch,
    )
    floor(report)
    assert report.gate_result == "pass"
    assert report.gate_reason is None


def test_scope_guard_hypothesis_verdicts_not_mutated():
    """#23 — Hypothesis verdicts, time-split outcomes, tag-grain verdicts are
    not mutated by the floor."""
    import copy as _copy
    floor = _caveat_floor()
    h2 = _hypothesis("H2", "supported")
    ts = _time_split("failed")
    grain = _tag_grain("nav", "politics", "supported")
    branch = _held_branch(
        hypotheses=[h2],
        time_split={"H2": ts},
        tag_grains=[grain],
    )
    report = _make_report(held_branch=branch)

    branch_before = _copy.deepcopy(dataclasses.asdict(branch))
    floor(report)
    branch_after = dataclasses.asdict(report.held_analyst_branch)

    # Only data_quality_flags and applicable_limitations on the report should change.
    # The branch itself (hypotheses, tag_grains, time_split) must be unchanged.
    assert branch_after["hypotheses"] == branch_before["hypotheses"]
    assert branch_after["tag_grains"] == branch_before["tag_grains"]
    assert branch_after["time_split"] == branch_before["time_split"]


def test_scope_guard_no_new_protocolreport_fields():
    """#24 — No new field is added to ProtocolReport or BranchReport by the
    tag-grain rescue floor implementation. Only the existing data_quality_flags
    and applicable_limitations lists are appended to.

    This test MUST FAIL in Phase 1 because BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY
    does not exist yet — using _require() here ensures a red test until Phase 2
    lands. Once the constant is present the field-set assertions then verify
    the implementer added no new dataclass fields.
    """
    # Require the new constant so this test is red until Phase 2 lands.
    _require("BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY")
    _require("_branch_promoted_on_tag_grain_only")
    _require("_apply_tag_grain_rescue_caveat")

    expected_report_fields = {
        "wallet", "protocol_version", "analyzed_at",
        "n_closed_positions", "n_held_to_resolution", "n_open_positions",
        "n_open_dead", "net_pnl", "activity_rows_fetched", "activity_cap_hit",
        "subgraph_fallback_used", "archetype_layer1", "archetype_layer2",
        "flip_rate", "trading_mode", "walk_full_history", "n_unmapped_fills",
        "gate_result", "gate_reason", "data_quality_flags",
        "dw_roi_point", "dw_roi_ci_lower", "dw_roi_ci_upper", "ew_roi_point",
        "held_analyst_branch", "flipper_branch",
        "overall_verdict", "primary_edge_type", "copyability_note",
        "open_questions", "applicable_limitations",
        "capital_ledger", "execution_quality_summary",
        "outcome_side_breakdown", "extractor_features_summary",
        "exit_attribution", "timing_surface", "position_building_surface",
        "liquidity_surface", "capital_risk_surface", "sizing_outcome_surface",
        "null_baseline_surface",
        "survivorship_surface",
        "copy_latency_surface",
        "position_reconciliation_surface",
        "data_completeness",
        # Phase A split #2 (2026-05-13) — PnL composition fields
        "realized_pnl", "unrealized_pnl", "combined_pnl",
        "realized_roi", "unrealized_roi", "combined_roi",
        "open_position_breakdown",
        # Phase A split #3 (2026-05-13) — time coverage
        "time_coverage",
        # FH4 Phase 2 wiring fix (2026-05-15) — supersession key
        # stamped by analyze_wallet for the flipper-grain writer.
        "wallet_fixture_hash",
        # Phase A G3 (2026-05-17) — operator-stamped leaderboard context.
        "triage_context",
    }
    expected_branch_fields = {
        "branch", "is_active", "skip_reason", "hypotheses",
        "tag_grains", "stationarity", "time_split", "independence_check",
    }
    actual_report = {f.name for f in dataclasses.fields(ProtocolReport)}
    actual_branch = {f.name for f in dataclasses.fields(BranchReport)}

    assert actual_report == expected_report_fields, (
        f"ProtocolReport field drift. Added: {actual_report - expected_report_fields}. "
        f"Removed: {expected_report_fields - actual_report}."
    )
    assert actual_branch == expected_branch_fields, (
        f"BranchReport field drift. Added: {actual_branch - expected_branch_fields}. "
        f"Removed: {expected_branch_fields - actual_branch}."
    )


_FORBIDDEN_SUBSTRINGS = (
    "trigger",
    "simulator",
    "forward_prediction",
    "forward-prediction",
    "copy this",
    "copy-trigger",
    "recommended_trigger",
    "deploy",
)


def test_scope_guard_no_forbidden_output_language():
    """#25 — The flag string and limitation line emit NO copy/trigger/simulator/
    forward-prediction strings. Also checks constant value and source text."""
    floor = _caveat_floor()
    const = _const()

    # (a) Constant text is clean.
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad.lower() not in str(const).lower(), (
            f"constant must not contain forbidden phrasing {bad!r}: got {const!r}"
        )

    # (b) Emitted strings are clean.
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(held_branch=branch)
    floor(report)

    for flag in report.data_quality_flags:
        if const in flag:
            for bad in _FORBIDDEN_SUBSTRINGS:
                assert bad.lower() not in flag.lower(), (
                    f"flag must not contain {bad!r}: got {flag!r}"
                )
    for lim in report.applicable_limitations:
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad.lower() not in lim.lower(), (
                f"limitation must not contain {bad!r}: got {lim!r}"
            )

    # (c) Source text of floor function is clean.
    src = inspect.getsource(floor).lower()
    # We allow "forward" in non-prediction context but be specific:
    for bad in ("forward_prediction", "forward-prediction", "copy-trigger", "copy this"):
        assert bad.lower() not in src, (
            f"floor source must not contain forbidden phrasing {bad!r}"
        )


# ---------------------------------------------------------------------------
# Stacking with negative-PnL floor (#26–28)
# ---------------------------------------------------------------------------


def test_stacking_both_floors_fire_when_both_conditions_met():
    """#26 — Pestle-shape with tag-grain-only branch: both floors fire.
    Both NEGATIVE_PNL_EDGE_UNEXPLAINED and BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY
    appear in data_quality_flags."""
    neg_pnl_floor = _require("_apply_negative_pnl_honesty_floor")
    tag_floor = _caveat_floor()
    const_neg = _require("NEGATIVE_PNL_EDGE_UNEXPLAINED")
    const_tag = _const()

    # Build a branch that triggers the tag-grain predicate.
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    # Also has negative PnL + no explanatory coverage (Pestle conditions).
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        held_branch=branch,
        data_quality_flags=[],
    )
    # Floors run in order: negative-PnL first, then tag-grain.
    neg_pnl_floor(report)
    tag_floor(report)

    assert const_neg in report.data_quality_flags, (
        f"NEGATIVE_PNL_EDGE_UNEXPLAINED not found in {report.data_quality_flags}"
    )
    assert any(const_tag in f for f in report.data_quality_flags), (
        f"BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY not found in {report.data_quality_flags}"
    )


def test_stacking_pestle_without_tag_grain_only_neg_pnl_fires():
    """#27 — Pestle without tag-grain-only branch (live case): only
    NEGATIVE_PNL_EDGE_UNEXPLAINED fires. BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY must NOT."""
    neg_pnl_floor = _require("_apply_negative_pnl_honesty_floor")
    tag_floor = _caveat_floor()
    const_neg = _require("NEGATIVE_PNL_EDGE_UNEXPLAINED")
    const_tag = _const()

    # No tag grains, H2 supported + survived → no tag-grain rescue.
    branch = _held_branch(
        hypotheses=[_hypothesis("H2", "supported")],
        time_split={"H2": _time_split("survived")},
        tag_grains=[],
    )
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        held_branch=branch,
        data_quality_flags=[],
    )
    neg_pnl_floor(report)
    tag_floor(report)

    assert const_neg in report.data_quality_flags
    assert not any(const_tag in f for f in report.data_quality_flags), (
        f"BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY should not fire: {report.data_quality_flags}"
    )


def test_stacking_swissmiss_positive_pnl_only_tag_grain_fires():
    """#28 — SwissMiss-shape with positive net_pnl: negative-PnL floor does NOT fire;
    tag-grain caveat DOES fire. overall_verdict stays 'edge_detected'."""
    neg_pnl_floor = _require("_apply_negative_pnl_honesty_floor")
    tag_floor = _caveat_floor()
    const_neg = _require("NEGATIVE_PNL_EDGE_UNEXPLAINED")
    const_tag = _const()

    branch = _held_branch(
        hypotheses=[
            _hypothesis("H2", "supported"),
            _hypothesis("H3", "inconclusive"),
        ],
        time_split={"H2": _time_split("failed")},
        tag_grains=[_tag_grain("nav", "politics", "supported")],
    )
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=+50_000.0,  # positive — negative-PnL floor must not fire
        held_branch=branch,
    )
    neg_pnl_floor(report)
    tag_floor(report)

    assert const_neg not in report.data_quality_flags, (
        f"Negative-PnL floor must not fire with positive PnL: {report.data_quality_flags}"
    )
    assert any(const_tag in f for f in report.data_quality_flags), (
        f"Tag-grain caveat must fire: {report.data_quality_flags}"
    )
    assert report.overall_verdict == "edge_detected", (
        f"overall_verdict must not be mutated by tag-grain floor: {report.overall_verdict}"
    )
