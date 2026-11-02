"""Negative-PnL edge-detected honesty floor — failing acceptance suite.

Spec: docs/superpowers/specs/2026-05-11-negative-pnl-edge-honesty-floor.md
Issue: #24 (ANALYZER TRUTH BUG / Pestle)

These tests encode the spec's "Test matrix" + "Caveat-content tests" +
"Doctrine guards" sections verbatim. They MUST all fail in Phase 1 because
the floor helpers (and the caveat-key constant) do not exist yet.

The implementation under test (Phase 2) is expected to export, from
`sniper.analysis.wallet_protocol.analyze_wallet`:

  - NEGATIVE_PNL_EDGE_UNEXPLAINED              : str caveat-key constant
  - attribution_coverage_nontrivial(d)         : pure predicate, bool
  - timing_coverage_nontrivial(d)              : pure predicate, bool
  - position_coverage_nontrivial(d)            : pure predicate, bool
  - _apply_negative_pnl_honesty_floor(report)  : in-place mutator, returns report

The floor runs after the verdict assembler in analyze_wallet around line 919.
The floor only mutates `overall_verdict`, `copyability_note`, and appends to
`data_quality_flags`. Nothing else (per spec "Hard scope guards").
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from sniper.analysis.wallet_protocol import analyze_wallet as aw_mod
from sniper.analysis.wallet_protocol.types import ProtocolReport


# ---------------------------------------------------------------------------
# Module-symbol helpers — single-source resolution so individual tests give
# a meaningful failure message when the implementation hasn't landed yet.
# ---------------------------------------------------------------------------


def _require(symbol_name: str):
    obj = getattr(aw_mod, symbol_name, None)
    if obj is None:
        pytest.fail(
            f"expected `sniper.analysis.wallet_protocol.analyze_wallet.{symbol_name}` "
            f"to exist; implementation has not landed yet"
        )
    return obj


def _floor():
    return _require("_apply_negative_pnl_honesty_floor")


def _caveat_const():
    return _require("NEGATIVE_PNL_EDGE_UNEXPLAINED")


def _attribution_nontrivial():
    return _require("attribution_coverage_nontrivial")


def _timing_nontrivial():
    return _require("timing_coverage_nontrivial")


def _position_nontrivial():
    return _require("position_coverage_nontrivial")


# ---------------------------------------------------------------------------
# Fixture builders — mirror the shapes produced by analyze_wallet for real
# wallets so the test inputs aren't unrealistic. Only the keys the floor
# inspects are populated; everything else stays at ProtocolReport defaults.
# ---------------------------------------------------------------------------


def _empty_data_completeness() -> dict:
    """Shape produced by analyze_wallet around lines 739-761 when there are
    no closed positions with attribution. `n_with_resolved_attribution == 0`."""
    return {
        "activity_capture": {
            "activity_rows_fetched": 0,
            "activity_cap_hit": False,
            "walk_full_history": False,
            "subgraph_fallback_used": False,
            "n_unmapped_fills": 0,
        },
        "attribution_coverage": {
            "n_total": 5000,
            "n_outside_window": 0,
            "n_insufficient_data": 5000,
            "n_attributed": 0,
            "n_with_resolved_attribution": 0,
            "history_assumed_full": True,
        },
        "caveats": {
            "activity_truncated": False,
            "subgraph_unmapped_fills": False,
            "attribution_coverage_low": True,
            "limitation_present": True,
        },
    }


def _data_completeness_with_resolved(n_with_resolved: int, n_total: int) -> dict:
    dc = _empty_data_completeness()
    dc["attribution_coverage"]["n_with_resolved_attribution"] = n_with_resolved
    dc["attribution_coverage"]["n_total"] = n_total
    dc["attribution_coverage"]["n_attributed"] = n_with_resolved
    dc["attribution_coverage"]["n_insufficient_data"] = max(0, n_total - n_with_resolved)
    return dc


def _timing_surface(confidence_band: str | None) -> dict:
    """Minimal timing surface dict. Only `confidence_band` matters for the floor."""
    surf = {
        "classification": "underpowered",
        "classification_reason": "test_fixture",
        "confidence_band": confidence_band,
        "confidence_downgraded": False,
        "coverage_downgraded": False,
        "coverage_caveats": [],
        "n_attributed": 0,
        "n_total_closed": 0,
        "held_to_resolution": {},
        "pre_resolution": {},
        "future_repricing_available": False,
        "future_repricing_signal": None,
        "future_repricing_window": None,
        "price_history_source": None,
    }
    return surf


def _position_building_surface(coverage: float | None) -> dict:
    """Minimal position-building surface dict. Only
    `wallet_summary.observation_window_coverage` matters for the floor."""
    return {
        "wallet": "0xtest",
        "surface_version": "position_building_v1",
        "generated_from": {
            "activity_cap_hit": False,
            "subgraph_fallback_used": False,
            "walk_full_history": False,
        },
        "source_quality_distribution": {},
        "identity_quality_distribution": {},
        "row_quality_summary": {},
        "wallet_summary": {
            "observation_window_coverage": coverage,
        },
        "positions": [],
        "top_level_caveats": [],
    }


def _make_report(
    *,
    overall_verdict: str = "edge_detected",
    net_pnl: float = -100_000.0,
    data_completeness: dict | None = None,
    timing_surface: dict | None = None,
    position_building_surface: dict | None = None,
    data_quality_flags: list[str] | None = None,
    copyability_note: str | None = None,
    primary_edge_type: str | None = "entry_timing",
) -> ProtocolReport:
    """Build a synthetic ProtocolReport with only the floor-relevant fields set."""
    return ProtocolReport(
        wallet="0xtest",
        overall_verdict=overall_verdict,                       # type: ignore[arg-type]
        net_pnl=net_pnl,
        data_completeness=data_completeness,
        timing_surface=timing_surface,
        position_building_surface=position_building_surface,
        data_quality_flags=list(data_quality_flags or []),
        copyability_note=copyability_note,
        primary_edge_type=primary_edge_type,
    )


# ---------------------------------------------------------------------------
# Test matrix — rows 1-12 from the spec.
# ---------------------------------------------------------------------------


def test_floor_fires_on_negative_pnl_zero_coverage():
    """#1 — edge_detected + net_pnl < 0 + no surface coverage → downgrade."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-727_729.04,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat in report.data_quality_flags


def test_floor_skips_on_positive_pnl():
    """#2 — edge_detected + net_pnl > 0 → unchanged (positive PnL is enough)."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    prior_note = report.copyability_note
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert caveat not in report.data_quality_flags
    assert report.copyability_note == prior_note


def test_floor_skips_on_attribution_coverage():
    """#3 — edge_detected + neg PnL but attribution n_with_resolved>0 → unchanged."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_data_completeness_with_resolved(n_with_resolved=5, n_total=10),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert caveat not in report.data_quality_flags


def test_floor_skips_on_timing_partial_coverage():
    """#4 — edge_detected + neg PnL + timing band == 'partial' → unchanged."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=_timing_surface("partial"),
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert caveat not in report.data_quality_flags


def test_floor_skips_on_position_coverage_above_threshold():
    """#5 — edge_detected + neg PnL + position coverage 0.50 → unchanged."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=_position_building_surface(0.50),
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert caveat not in report.data_quality_flags


def test_floor_fires_on_position_coverage_below_threshold():
    """#6 — edge_detected + neg PnL + position coverage 0.25 → downgrade."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=_position_building_surface(0.25),
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat in report.data_quality_flags


def test_floor_fires_on_zero_pnl():
    """#7 — edge_detected + net_pnl == 0 + no surface coverage → downgrade.
    Zero PnL is not "positive". Spec: `pnl_floor_satisfied = report.net_pnl > 0`."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=0.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat in report.data_quality_flags


def test_floor_skips_on_underpowered_verdict():
    """#8 — verdict already 'underpowered' → floor passes through untouched."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="underpowered",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat not in report.data_quality_flags


def test_floor_skips_on_no_edge_verdict():
    """#9 — verdict 'no_edge' → floor passes through untouched."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="no_edge",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "no_edge"
    assert caveat not in report.data_quality_flags


def test_floor_skips_on_data_insufficient_verdict():
    """#10 — verdict 'data_insufficient' → floor passes through untouched."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="data_insufficient",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "data_insufficient"
    assert caveat not in report.data_quality_flags


def test_floor_fires_when_data_completeness_is_none():
    """#11 — edge_detected + neg PnL + data_completeness=None → downgrade.
    None counts as no coverage (per the predicate's None-guard)."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=None,
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat in report.data_quality_flags


def test_floor_fires_on_timing_low_band():
    """#12 — edge_detected + neg PnL + timing band 'low' (not in {partial,good,high})
    → downgrade. Spec: only partial/good/high count as non-trivial."""
    floor = _floor()
    caveat = _caveat_const()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=_timing_surface("low"),
        position_building_surface=None,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert caveat in report.data_quality_flags


# ---------------------------------------------------------------------------
# Caveat-content tests — rows 13-16 from the spec.
# ---------------------------------------------------------------------------


def test_caveat_string_constant_value():
    """#13 — caveat string is EXACTLY 'NEGATIVE_PNL_EDGE_UNEXPLAINED'.

    No version suffix, no parameter substitution. Constant lives in the
    same module as the floor (analyze_wallet)."""
    caveat = _caveat_const()
    assert caveat == "NEGATIVE_PNL_EDGE_UNEXPLAINED"


def test_existing_data_quality_flags_preserved_when_floor_fires():
    """#14 — floor appends to data_quality_flags; existing flags are kept."""
    floor = _floor()
    caveat = _caveat_const()
    preexisting = ["PREEXISTING_FLAG_A", "PREEXISTING_FLAG_B"]
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
        data_quality_flags=list(preexisting),
    )
    floor(report)
    # Preexisting must still be there.
    for f in preexisting:
        assert f in report.data_quality_flags
    # And the new caveat is appended (not replacing).
    assert caveat in report.data_quality_flags
    assert len(report.data_quality_flags) == len(preexisting) + 1


def test_copyability_note_set_when_floor_fires_overwrites_prior():
    """#15 — copyability_note is populated when the floor fires; the previous
    value (set by the verdict assembler) is overwritten."""
    floor = _floor()
    prior = "Prior assembler note: held_analyst branch supported."
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
        copyability_note=prior,
    )
    floor(report)
    assert report.copyability_note is not None
    assert report.copyability_note != prior
    # Per spec the note explains the downgrade and references the caveat key.
    assert "NEGATIVE_PNL_EDGE_UNEXPLAINED" in report.copyability_note


def test_primary_edge_type_not_modified_by_floor():
    """#16 — primary_edge_type is NOT touched by the floor; it stays as set
    by the verdict assembler. Spec 'Hard scope guards': floor only touches
    overall_verdict, copyability_note, data_quality_flags."""
    floor = _floor()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
        primary_edge_type="entry_timing",
    )
    floor(report)
    # Verdict downgraded but primary_edge_type frozen at "entry_timing".
    assert report.overall_verdict == "underpowered"
    assert report.primary_edge_type == "entry_timing"


# ---------------------------------------------------------------------------
# Doctrine guards — rows 17-18 from the spec.
# ---------------------------------------------------------------------------


_FORBIDDEN_SUBSTRINGS = (
    "trigger",
    "simulator",
    "forward_prediction",
    "forward-prediction",
    "copy this",
    "copy-trigger",
)


def test_no_copy_or_trigger_strings_in_floor_path():
    """#17 — the floor path emits no copy/trigger/simulator strings.

    Inspects (a) the caveat-key constant value, (b) the copyability_note
    text produced when the floor fires, and (c) the source text of the
    floor helper function. None of these may contain forbidden phrasing.
    """
    floor = _floor()
    caveat = _caveat_const()

    # (a) Caveat constant text is clean.
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad.lower() not in str(caveat).lower(), (
            f"caveat constant must not contain forbidden phrasing {bad!r}: got {caveat!r}"
        )

    # (b) Note text produced by the floor on a firing case is clean.
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        data_completeness=_empty_data_completeness(),
        timing_surface=None,
        position_building_surface=None,
    )
    floor(report)
    note = report.copyability_note or ""
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad.lower() not in note.lower(), (
            f"floor-produced copyability_note must not contain forbidden phrasing "
            f"{bad!r}: got {note!r}"
        )

    # (c) Source text of the floor function is clean.
    src = inspect.getsource(floor).lower()
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad.lower() not in src, (
            f"floor helper source must not contain forbidden phrasing {bad!r}"
        )


def test_floor_does_not_add_protocolreport_field():
    """#18 — the floor reads only existing fields on ProtocolReport.
    No new field is added to types.ProtocolReport (only the caveat-key
    string constant exists in the module). The set of dataclass fields
    matches the canonical set captured in this test."""
    # Trigger module load so the implementation is in scope; the assertion
    # is about ProtocolReport not the floor module.
    _floor()
    expected_fields = {
        "wallet",
        "protocol_version",
        "analyzed_at",
        "n_closed_positions",
        "n_held_to_resolution",
        "n_open_positions",
        "n_open_dead",
        "net_pnl",
        "activity_rows_fetched",
        "activity_cap_hit",
        "subgraph_fallback_used",
        "archetype_layer1",
        "archetype_layer2",
        "flip_rate",
        "trading_mode",
        "walk_full_history",
        "n_unmapped_fills",
        "gate_result",
        "gate_reason",
        "data_quality_flags",
        "dw_roi_point",
        "dw_roi_ci_lower",
        "dw_roi_ci_upper",
        "ew_roi_point",
        "held_analyst_branch",
        "flipper_branch",
        "overall_verdict",
        "primary_edge_type",
        "copyability_note",
        "open_questions",
        "applicable_limitations",
        "capital_ledger",
        "execution_quality_summary",
        "outcome_side_breakdown",
        "extractor_features_summary",
        "exit_attribution",
        "timing_surface",
        "position_building_surface",
        "liquidity_surface",
        "capital_risk_surface",
        "sizing_outcome_surface",
        "null_baseline_surface",
        "survivorship_surface",
        "copy_latency_surface",
        "position_reconciliation_surface",
        "data_completeness",
        # Phase A split #2 (2026-05-13) — PnL composition fields
        "realized_pnl",
        "unrealized_pnl",
        "combined_pnl",
        "realized_roi",
        "unrealized_roi",
        "combined_roi",
        "open_position_breakdown",
        # Phase A split #3 (2026-05-13) — time coverage
        "time_coverage",
        # FH4 Phase 2 wiring fix (2026-05-15) — supersession key
        # stamped by analyze_wallet for the flipper-grain writer.
        "wallet_fixture_hash",
        # Phase A G3 (2026-05-17) — operator-stamped leaderboard context.
        "triage_context",
    }
    actual_fields = {f.name for f in dataclasses.fields(ProtocolReport)}
    assert actual_fields == expected_fields, (
        f"ProtocolReport field set drift detected. "
        f"Added: {actual_fields - expected_fields}. "
        f"Removed: {expected_fields - actual_fields}."
    )


# ---------------------------------------------------------------------------
# Helper-predicate direct tests — exercise the three coverage predicates
# in isolation. They round out the matrix coverage and pin down edge cases
# the matrix references implicitly.
# ---------------------------------------------------------------------------


def test_attribution_coverage_predicate_none_input():
    """attribution_coverage_nontrivial(None) → False."""
    pred = _attribution_nontrivial()
    assert pred(None) is False


def test_attribution_coverage_predicate_zero_resolved_is_false():
    pred = _attribution_nontrivial()
    dc = _empty_data_completeness()
    assert pred(dc) is False


def test_attribution_coverage_predicate_one_or_more_resolved_is_true():
    pred = _attribution_nontrivial()
    dc = _data_completeness_with_resolved(n_with_resolved=1, n_total=10)
    assert pred(dc) is True


def test_timing_coverage_predicate_none_input():
    pred = _timing_nontrivial()
    assert pred(None) is False


def test_timing_coverage_predicate_high_band_is_true():
    pred = _timing_nontrivial()
    assert pred(_timing_surface("high")) is True


def test_timing_coverage_predicate_partial_band_is_true():
    pred = _timing_nontrivial()
    assert pred(_timing_surface("partial")) is True


def test_timing_coverage_predicate_good_band_is_true():
    pred = _timing_nontrivial()
    assert pred(_timing_surface("good")) is True


def test_timing_coverage_predicate_low_band_is_false():
    pred = _timing_nontrivial()
    assert pred(_timing_surface("low")) is False


def test_timing_coverage_predicate_underpowered_band_is_false():
    pred = _timing_nontrivial()
    assert pred(_timing_surface("underpowered")) is False


def test_timing_coverage_predicate_missing_band_is_false():
    pred = _timing_nontrivial()
    # confidence_band key absent entirely
    surf = _timing_surface("high")
    del surf["confidence_band"]
    assert pred(surf) is False


def test_position_coverage_predicate_none_input():
    pred = _position_nontrivial()
    assert pred(None) is False


def test_position_coverage_predicate_above_threshold_is_true():
    pred = _position_nontrivial()
    assert pred(_position_building_surface(0.31)) is True


def test_position_coverage_predicate_at_threshold_is_false():
    """Spec: `cov > 0.30` — strict inequality, 0.30 is NOT non-trivial."""
    pred = _position_nontrivial()
    assert pred(_position_building_surface(0.30)) is False


def test_position_coverage_predicate_below_threshold_is_false():
    pred = _position_nontrivial()
    assert pred(_position_building_surface(0.25)) is False


def test_position_coverage_predicate_missing_wallet_summary_is_false():
    pred = _position_nontrivial()
    surf = _position_building_surface(0.50)
    surf.pop("wallet_summary", None)
    assert pred(surf) is False


def test_position_coverage_predicate_none_coverage_is_false():
    pred = _position_nontrivial()
    assert pred(_position_building_surface(None)) is False
