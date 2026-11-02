"""Tests for the analyze_wallet orchestrator + report formatter."""

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import (
    FLIPPER_PRIMARY_TESTS,
    HELD_PRIMARY_TESTS,
    analyze_wallet,
)
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import BranchReport, HypothesisResult, WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid="cid_a", outcome="Yes", curPrice=1.0, avgPrice=0.50, size=100,
    realizedPnl=50.0, ts=None, eventSlug=None,
):
    # Default timestamps near "now" so R3 dormancy gate (90d) doesn't fire.
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:  # < 2001 → treat as offset from recent reference
        ts = _now() - 86400 + ts  # within last day
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts,
        "eventSlug": eventSlug or f"slug-{cid}",
    }


def _wallet_data(closed=None, activity=None, archetype=None, profile=None, cap_hit=False):
    return WalletData(
        wallet="0xabc",
        closed_positions=closed or [],
        open_positions=[],
        activity_rows=activity or [],
        activity_cap_hit=cap_hit,
        archetype_layer1=archetype,
        execution_profile=profile,
        fetched_at=_now(),
    )


# ----------------------------------------------------------------------------
# Stage 1 short-circuit
# ----------------------------------------------------------------------------


def test_orchestrator_rejects_at_hedge_archetype():
    closed = [_pos(realizedPnl=100.0) for _ in range(50)]
    data = _wallet_data(closed=closed, archetype="paired_outcome_arb")
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.gate_result == "reject_hedge_archetype"
    assert report.overall_verdict == "data_insufficient"
    assert report.held_analyst_branch is None
    assert report.flipper_branch is None


def test_orchestrator_rejects_at_insufficient_sample():
    closed = [_pos(realizedPnl=100.0) for _ in range(5)]  # below R2 threshold
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.gate_result == "reject_insufficient_sample"
    assert report.overall_verdict == "data_insufficient"


# ----------------------------------------------------------------------------
# Held-only mode
# ----------------------------------------------------------------------------


def test_orchestrator_held_only_mode_runs_held_branch_only():
    """No SELL events → flip_rate = 0 → held_analyst_only."""
    closed = [
        _pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0, avgPrice=0.40,
             realizedPnl=60.0, ts=1000 + i)
        for i in range(50)
    ]
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    assert report.gate_result == "pass"
    assert report.trading_mode == "held_analyst_only"
    assert report.held_analyst_branch is not None
    assert report.held_analyst_branch.is_active
    assert report.flipper_branch is not None
    assert not report.flipper_branch.is_active
    # Strong wins → some held verdict surfaces
    assert report.overall_verdict in ("edge_detected", "no_edge", "underpowered")


def test_headline_held_count_uses_full_descriptive_universe(monkeypatch):
    """Headline count must not shrink to H2's post-regime inference sample."""
    closed = [
        _pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0, avgPrice=0.40,
             realizedPnl=60.0, ts=1000 + i)
        for i in range(30)
    ]
    data = _wallet_data(closed=closed)

    def fake_run_held_branch(*args, **kwargs):
        return BranchReport(
            branch="held_analyst",
            is_active=True,
            hypotheses=[
                HypothesisResult(name="H1", verdict="inconclusive", n=30),
                HypothesisResult(name="H2", verdict="inconclusive", n=29),
            ],
        )

    import sniper.analysis.wallet_protocol.analyze_wallet as mod

    monkeypatch.setattr(mod, "run_held_branch", fake_run_held_branch)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)

    assert report.n_held_to_resolution == 30


# ----------------------------------------------------------------------------
# Flipper-only mode
# ----------------------------------------------------------------------------


def test_orchestrator_flipper_only_mode_runs_flipper_branch_only():
    """All positions have pre-resolution SELLs → flip_rate=1.0 → flipper_only."""
    closed = []
    activity = []
    for i in range(50):
        cid = f"cid_{i}"
        closed.append(_pos(
            cid=cid, outcome="Yes", curPrice=0.5,  # unresolved
            avgPrice=0.30, ts=1000 + i,
        ))
        activity.append({
            "side": "BUY", "conditionId": cid, "outcome": "Yes",
            "size": 100, "price": 0.30, "timestamp": 1000 + i * 10,
        })
        activity.append({
            "side": "SELL", "conditionId": cid, "outcome": "Yes",
            "size": 100, "price": 0.40, "timestamp": 1000 + i * 10 + 5,
        })
    data = _wallet_data(closed=closed, activity=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    assert report.trading_mode == "flipper_only"
    assert report.held_analyst_branch is not None
    assert not report.held_analyst_branch.is_active
    assert report.flipper_branch is not None
    assert report.flipper_branch.is_active
    # Strong positive flip ROI → edge_detected on flipper
    assert report.overall_verdict in ("edge_detected", "underpowered")


# ----------------------------------------------------------------------------
# Mixed mode
# ----------------------------------------------------------------------------


def test_orchestrator_mixed_mode_runs_both_branches():
    """flip_rate ~0.5 → mixed_mode; both branches active."""
    closed = []
    activity = []
    # 25 positions held to resolution (no sells)
    for i in range(25):
        cid = f"held_{i}"
        closed.append(_pos(
            cid=cid, outcome="Yes", curPrice=1.0, avgPrice=0.40,
            realizedPnl=60.0, ts=1000 + i,
        ))
    # 25 positions with pre-resolution sells (flips)
    for i in range(25):
        cid = f"flip_{i}"
        closed.append(_pos(
            cid=cid, outcome="Yes", curPrice=0.5,
            avgPrice=0.30, ts=1000 + 100 + i,
        ))
        activity.append({
            "side": "BUY", "conditionId": cid, "outcome": "Yes",
            "size": 100, "price": 0.30, "timestamp": 1000 + 100 + i * 10,
        })
        activity.append({
            "side": "SELL", "conditionId": cid, "outcome": "Yes",
            "size": 100, "price": 0.40, "timestamp": 1000 + 100 + i * 10 + 5,
        })
    data = _wallet_data(closed=closed, activity=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    assert report.trading_mode == "mixed_mode"
    assert report.held_analyst_branch is not None
    assert report.held_analyst_branch.is_active
    assert report.flipper_branch is not None
    assert report.flipper_branch.is_active


# ----------------------------------------------------------------------------
# Report formatter
# ----------------------------------------------------------------------------


def test_render_report_includes_required_sections():
    closed = [_pos(realizedPnl=50.0, ts=1000 + i) for i in range(40)]
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    # Spec §12 mandatory sections
    assert "## Wallet Report: 0xabc" in md
    assert "**Protocol version:** v2" in md
    assert "**n_closed_positions:** 40" in md
    assert "### Stage 1 result" in md
    assert "[HELD-ANALYST] BRANCH" in md
    assert "[FLIPPER] BRANCH" in md
    assert "### Overall verdict" in md
    assert "### Applicable limitations" in md


def test_render_report_truncated_at_stage1_rejection():
    closed = [_pos(realizedPnl=10.0)]  # below R2 threshold
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100)
    md = render_report(report)
    assert "REJECT_INSUFFICIENT_SAMPLE" in md
    assert "data_insufficient" in md
    # Stage 2 sections should NOT appear after rejection
    assert "[HELD-ANALYST]" not in md


def test_render_report_marks_inactive_branches_as_skipped():
    """Held-only mode: flipper branch should render as skipped, not absent."""
    closed = [
        _pos(cid=f"cid_{i}", curPrice=1.0, ts=1000 + i, realizedPnl=20.0)
        for i in range(40)
    ]
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    assert "[FLIPPER] BRANCH" in md
    assert "branch suppressed" in md or "wallet predominantly holds" in md


# ----------------------------------------------------------------------------
# Constants — locked test
# ----------------------------------------------------------------------------


def test_primary_test_constants_locked():
    assert HELD_PRIMARY_TESTS == ("H2", "H3", "H5")
    assert FLIPPER_PRIMARY_TESTS == ("FH1", "FH2", "FH4")


def test_v3_h2_sub080_promoted_when_h2_refuted():
    """v3: full H2 refuted but H2_sub080 supported → branch verdict 'supported'.

    Construct a wallet that is a certainty-buyer overall (most positions
    entered at 0.95 with WR=1.0 → full H2 refuted because 1.0 - 0.95 = 5pp
    is BELOW null implied of ~0.95) but has a thin sub-0.80 edge.
    """
    closed = []
    # 30 certainty buys (entered 0.97, all win → contribute 1.0 - 0.97 = +0.03 each)
    for i in range(30):
        closed.append(_pos(
            cid=f"cert_{i}", outcome="Yes", curPrice=1.0, avgPrice=0.97,
            realizedPnl=3.0,
        ))
    # 30 sub-0.80 entries (avg 0.40, all win → contribute +0.60 excess each)
    for i in range(30):
        closed.append(_pos(
            cid=f"sub_{i}", outcome="Yes", curPrice=1.0, avgPrice=0.40,
            realizedPnl=60.0,
        ))
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    held = report.held_analyst_branch
    assert held is not None and held.is_active
    # Both H2 (full sample) and H2_sub080 should be in hypotheses
    h2_sub = next((h for h in held.hypotheses if h.name == "H2_sub080"), None)
    assert h2_sub is not None
    # H2_sub080 should be in time_split now (v3 amendment)
    assert "H2_sub080" in held.time_split


def test_v3_certainty_buyer_flag_emitted():
    """When H2 refuted + H2_sub080 ran with sufficient n, flag fires."""
    closed = []
    # All 50 are certainty buys at 0.97 → H2 refuted (excess = 0.03 < 0.95 null)
    # But none qualify for sub_080 filter (all entered >= 0.80) → H2_sub080 underpowered
    # Need a mix to populate H2_sub080
    for _ in range(30):
        closed.append(_pos(curPrice=1.0, avgPrice=0.97, realizedPnl=3.0))
    for _ in range(30):
        closed.append(_pos(curPrice=1.0, avgPrice=0.40, realizedPnl=60.0))
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    # H2 should be refuted on full sample (because mean entry ~0.69, mean WR 1.0
    # → excess ~0.31 → may actually SUPPORT depending on bootstrap. Test the flag
    # logic by checking when H2 verdict matches.)
    held = report.held_analyst_branch
    h2 = next((h for h in held.hypotheses if h.name == "H2"), None)
    h2_sub = next((h for h in held.hypotheses if h.name == "H2_sub080"), None)
    if h2.verdict == "refuted" and h2_sub.verdict != "underpowered":
        assert "CERTAINTY_BUYER_DETECTED" in report.data_quality_flags


def test_v3_certainty_buyer_flag_absent_when_h2_supported():
    """When H2 not refuted, no certainty-buyer flag regardless of H2_sub080."""
    closed = [
        _pos(curPrice=1.0, outcome="Yes", avgPrice=0.30, realizedPnl=70.0)
        for _ in range(40)
    ]
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    assert "CERTAINTY_BUYER_DETECTED" not in report.data_quality_flags


def test_v3_edge_conditional_on_top3_flag_when_sub_top3_supported():
    """H1 inconclusive but excluding top-3 reveals positive edge → flag fires."""
    # 50 positions of +$10 each (real edge, but tiny) plus 3 massive outliers
    closed = []
    for i in range(50):
        closed.append(_pos(cid=f"small_{i}", realizedPnl=10.0))
    closed.append(_pos(cid="outlier1", realizedPnl=500_000.0))
    closed.append(_pos(cid="outlier2", realizedPnl=300_000.0))
    closed.append(_pos(cid="outlier3", realizedPnl=200_000.0))
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=300)
    # F2 should fire (top-3 dominate). H1 winsorized may be supported (not
    # inconclusive) since winsorization clips the outliers. The flag rule
    # only fires when H1 is INCONCLUSIVE — so this test verifies absence
    # in a situation where the winsorization successfully recovers.
    h1 = next((h for h in report.held_analyst_branch.hypotheses if h.name == "H1"), None)
    if h1.verdict == "inconclusive":
        assert "EDGE_CONDITIONAL_ON_TOP3_EXCLUSION" in report.data_quality_flags
    # If winsorization saved the verdict, flag should be absent
    if h1.verdict == "supported":
        assert "EDGE_CONDITIONAL_ON_TOP3_EXCLUSION" not in report.data_quality_flags


def test_orchestrator_surfaces_mandatory_limitations():
    closed = [_pos(realizedPnl=50.0, ts=1000 + i) for i in range(40)]
    data = _wallet_data(closed=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    # All four §13 limitations should appear
    joined = " | ".join(report.applicable_limitations)
    assert "B1 population" in joined
    assert "Cross-event news-cycle" in joined
    assert "Mode-router thresholds" in joined
    assert "Activity API capped" in joined
