"""Acceptance tests for scripts/phase_b_shift_diff.py.

Phase B B4 — the verdict/surface shift-diff harness. Compares a v3-fixture
replay against a v4-fixture replay (both under current analyzer code) so
the diff isolates DATA changes from analyzer-code changes. Pure functions
are unit-tested here; the replay integration is exercised against one real
v3 fixture.

Spec: docs/superpowers/specs/2026-05-17-phase-b-fixture-recapture.md §5
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.phase_b_shift_diff import (
    diff_shift_fields,
    extract_shift_fields,
    load_expected_verdict,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic report dicts (asdict(ProtocolReport) shape)
# ---------------------------------------------------------------------------

def _report(
    *,
    overall_verdict="edge_detected",
    gate_result="pass",
    primary_edge_type="entry_timing",
    held_branch=None,
    flipper_branch=None,
    tag_lookup_state="ok",
    closed_cap_hit=False,
    category_clusters=None,
    data_quality_flags=None,
) -> dict:
    return {
        "overall_verdict": overall_verdict,
        "gate_result": gate_result,
        "primary_edge_type": primary_edge_type,
        "held_analyst_branch": held_branch,
        "flipper_branch": flipper_branch,
        "data_completeness": {
            "tag_lookup_state": tag_lookup_state,
            "closed_positions_capture": {
                "closed_positions_fetched": 100,
                "closed_positions_cap_hit": closed_cap_hit,
            },
        },
        "outcome_side_breakdown": (
            {"category_clusters": category_clusters}
            if category_clusters is not None
            else {}
        ),
        "data_quality_flags": data_quality_flags or [],
    }


def _branch(*, is_active=True, hypotheses=None) -> dict:
    return {
        "branch": "held_analyst",
        "is_active": is_active,
        "skip_reason": None,
        "hypotheses": hypotheses or [],
    }


# ===========================================================================
# extract_shift_fields
# ===========================================================================

def test_extract_pulls_all_top_level_fields():
    r = _report(
        overall_verdict="underpowered",
        gate_result="reject_insufficient_sample",
        primary_edge_type=None,
    )
    fields = extract_shift_fields(r)
    assert fields["overall_verdict"] == "underpowered"
    assert fields["gate_result"] == "reject_insufficient_sample"
    assert fields["primary_edge_type"] is None


def test_extract_handles_none_branches():
    """held/flipper branch None → active False, hypotheses empty dict."""
    fields = extract_shift_fields(_report(held_branch=None, flipper_branch=None))
    assert fields["held_active"] is False
    assert fields["flipper_active"] is False
    assert fields["held_hypotheses"] == {}
    assert fields["flipper_hypotheses"] == {}


def test_extract_pulls_branch_hypothesis_verdicts():
    held = _branch(
        is_active=True,
        hypotheses=[
            {"name": "H1", "verdict": "supported"},
            {"name": "H3", "verdict": "inconclusive"},
        ],
    )
    fields = extract_shift_fields(_report(held_branch=held))
    assert fields["held_active"] is True
    assert fields["held_hypotheses"] == {"H1": "supported", "H3": "inconclusive"}


def test_extract_tag_lookup_state_and_closed_cap():
    fields = extract_shift_fields(
        _report(tag_lookup_state="unavailable", closed_cap_hit=True)
    )
    assert fields["tag_lookup_state"] == "unavailable"
    assert fields["closed_positions_cap_hit"] is True


def test_extract_category_cluster_count():
    clusters = [
        {"category": "politics", "n": 6, "pnl": 600.0, "total_pnl": 640.0},
        {"category": "sports", "n": 2, "pnl": -40.0, "total_pnl": 640.0},
    ]
    fields = extract_shift_fields(_report(category_clusters=clusters))
    assert fields["n_category_clusters"] == 2
    assert fields["primary_category"] == "politics"


def test_extract_category_cluster_count_zero_when_absent():
    fields = extract_shift_fields(_report(category_clusters=None))
    assert fields["n_category_clusters"] == 0
    assert fields["primary_category"] is None


def test_extract_data_quality_flags_sorted():
    fields = extract_shift_fields(
        _report(data_quality_flags=["TAG_LOOKUP_UNAVAILABLE", "ACCOUNTING_TIE"])
    )
    assert fields["data_quality_flags"] == ["ACCOUNTING_TIE", "TAG_LOOKUP_UNAVAILABLE"]


def test_extract_tolerates_missing_data_completeness():
    """A bare report (no data_completeness) must not crash extraction."""
    bare = {"overall_verdict": "data_insufficient"}
    fields = extract_shift_fields(bare)
    assert fields["overall_verdict"] == "data_insufficient"
    assert fields["tag_lookup_state"] is None
    assert fields["closed_positions_cap_hit"] is None


# ===========================================================================
# diff_shift_fields
# ===========================================================================

def test_diff_flags_changed_verdict():
    v3 = extract_shift_fields(_report(overall_verdict="underpowered"))
    v4 = extract_shift_fields(_report(overall_verdict="edge_detected"))
    diff = diff_shift_fields(v3, v4)
    assert diff["overall_verdict"]["changed"] is True
    assert diff["overall_verdict"]["v3"] == "underpowered"
    assert diff["overall_verdict"]["v4"] == "edge_detected"


def test_diff_marks_unchanged_field():
    v3 = extract_shift_fields(_report(gate_result="pass"))
    v4 = extract_shift_fields(_report(gate_result="pass"))
    diff = diff_shift_fields(v3, v4)
    assert diff["gate_result"]["changed"] is False


def test_diff_any_changed_summary():
    """diff carries an `any_changed` rollup so the caller can quickly see
    whether a wallet shifted at all."""
    v3 = extract_shift_fields(_report(overall_verdict="underpowered"))
    v4 = extract_shift_fields(_report(overall_verdict="underpowered"))
    assert diff_shift_fields(v3, v4)["any_changed"] is False

    v3b = extract_shift_fields(_report(tag_lookup_state="ok"))
    v4b = extract_shift_fields(_report(tag_lookup_state="unavailable"))
    assert diff_shift_fields(v3b, v4b)["any_changed"] is True


def test_diff_data_quality_flags_reports_added_removed():
    v3 = extract_shift_fields(_report(data_quality_flags=["A", "B"]))
    v4 = extract_shift_fields(_report(data_quality_flags=["B", "C"]))
    diff = diff_shift_fields(v3, v4)
    dq = diff["data_quality_flags"]
    assert dq["changed"] is True
    assert dq["added"] == ["C"]
    assert dq["removed"] == ["A"]


# ===========================================================================
# load_expected_verdict
# ===========================================================================

def test_load_expected_verdict_known_wallet():
    labels = {
        "labels": [
            {"wallet": "0xAAA", "expected": "edge_detected"},
            {"wallet": "0xBBB", "expected": "underpowered"},
        ]
    }
    assert load_expected_verdict(labels, "0xAAA") == "edge_detected"
    assert load_expected_verdict(labels, "0xBBB") == "underpowered"


def test_load_expected_verdict_unknown_wallet_returns_none():
    labels = {"labels": [{"wallet": "0xAAA", "expected": "edge_detected"}]}
    assert load_expected_verdict(labels, "0xZZZ") is None


def test_load_expected_verdict_case_insensitive():
    """Wallet addresses compare case-insensitively."""
    labels = {"labels": [{"wallet": "0xabc", "expected": "edge_detected"}]}
    assert load_expected_verdict(labels, "0xABC") == "edge_detected"


# ===========================================================================
# Integration — replay one real v3 fixture
# ===========================================================================

@pytest.mark.integration
def test_replay_fixture_returns_parseable_report():
    """replay_fixture against a real v3 fixture returns a report dict with
    an overall_verdict. Skipped if no v3 fixture is checked out.

    Marked `integration` because it executes a full ProtocolReport replay
    on a real ~992-position fixture and takes ~54s. Run via
    `pytest -m integration tests/test_phase_b_shift_diff.py`.
    """
    from scripts.phase_b_shift_diff import replay_fixture

    fixture_dir = Path(__file__).resolve().parent / "calibration" / "fixtures"
    v3 = sorted(fixture_dir.glob("*_2026-05-13.json"))
    if not v3:
        pytest.skip("no v3 calibration fixtures checked out")

    report = replay_fixture(str(v3[0]))
    assert isinstance(report, dict)
    assert "overall_verdict" in report
    assert report["overall_verdict"] in {
        "edge_detected", "underpowered", "data_insufficient",
    }


# ---------------------------------------------------------------------------
# Value-bearing flag normalization (2026-05-22 — Phase B B4 finding)
# ---------------------------------------------------------------------------

from scripts.phase_b_shift_diff import _flag_type


def test_flag_type_strips_value_payloads():
    assert _flag_type("ACCOUNTING_TIE_UNSUPPORTED: lifecycle_estimate=$384,945") \
        == "ACCOUNTING_TIE_UNSUPPORTED"
    assert _flag_type("F3_recent_regime (last_90d_share=0.91)") == "F3_recent_regime"
    assert _flag_type("DW_EW_DIVERGENCE: dw_roi=0.36% vs ew_roi=2.46%") \
        == "DW_EW_DIVERGENCE"
    # Payload-free flags are returned unchanged.
    assert _flag_type("TAG_LOOKUP_UNAVAILABLE") == "TAG_LOOKUP_UNAVAILABLE"
    # csv-payload flag: type is still the prefix.
    assert _flag_type("ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution") \
        == "ANALYST_FAST_MODE_OMITTED"


def test_diff_ignores_value_bearing_flag_drift():
    """Two captures with the SAME flag types but drifted embedded numbers
    (different capture dates → different $/%/ts) must NOT register as a
    data_quality_flags shift. Only a flag TYPE appearing/disappearing is a
    real shift."""
    v3 = extract_shift_fields(_report(data_quality_flags=[
        "ACCOUNTING_TIE_UNSUPPORTED: lifecycle_estimate=$306,680 delta_pct=66.47%",
        "F3_recent_regime (last_90d_share=0.90)",
    ]))
    v4 = extract_shift_fields(_report(data_quality_flags=[
        "ACCOUNTING_TIE_UNSUPPORTED: lifecycle_estimate=$704,834 delta_pct=250.88%",
        "F3_recent_regime (last_90d_share=0.91)",
    ]))
    diff = diff_shift_fields(v3, v4)
    assert diff["data_quality_flags"]["changed"] is False
    assert diff["data_quality_flags"]["added"] == []
    assert diff["data_quality_flags"]["removed"] == []
    assert diff["any_changed"] is False


def test_diff_still_catches_real_flag_type_change():
    """A genuine flag TYPE appearing IS still a shift."""
    v3 = extract_shift_fields(_report(data_quality_flags=[
        "ACCOUNTING_TIE_UNSUPPORTED: delta_pct=66%",
    ]))
    v4 = extract_shift_fields(_report(data_quality_flags=[
        "ACCOUNTING_TIE_UNSUPPORTED: delta_pct=70%",
        "TAG_LOOKUP_UNAVAILABLE",
    ]))
    diff = diff_shift_fields(v3, v4)
    assert diff["data_quality_flags"]["changed"] is True
    assert diff["data_quality_flags"]["added"] == ["TAG_LOOKUP_UNAVAILABLE"]


def test_shift_row_surfaces_edge_type_and_hypothesis_shifts():
    """Regression (B5 adversarial check, 2026-05-22): the shift table row
    must surface primary_edge_type and hypothesis-level shifts, not only
    verdict/gate/flags. The first table draft hid a primary_edge_type flip."""
    from scripts.phase_b_shift_diff import _shift_row

    v3 = extract_shift_fields(_report(
        primary_edge_type="flipper_skill",
        held_branch=_branch(hypotheses=[{"name": "H2", "verdict": "inconclusive"}]),
    ))
    v4 = extract_shift_fields(_report(
        primary_edge_type="entry_timing",
        held_branch=_branch(hypotheses=[{"name": "H2", "verdict": "supported"}]),
    ))
    row = _shift_row("0xabc0000000", diff_shift_fields(v3, v4), "edge_detected",
                     "edge_detected")
    assert "edge_type" in row
    assert "held_hyp" in row
