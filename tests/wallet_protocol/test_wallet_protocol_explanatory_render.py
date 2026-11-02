"""Explanatory render surfaces — Layer 2 archetype, drift companion summaries,
Execution quality block. Hard invariant: these are display-only — they must
NEVER alter overall_verdict, gate_result, or primary_edge_type compared to a
matched WalletData with the new fields cleared.
"""
from __future__ import annotations

import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid="cid_a", outcome="Yes", curPrice=1.0, avgPrice=0.50, size=100,
    realizedPnl=50.0, ts=None, eventSlug=None,
):
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
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


def _wallet_data(**overrides) -> WalletData:
    base = dict(
        wallet="0xabc",
        closed_positions=[_pos(realizedPnl=50.0, ts=1000 + i) for i in range(40)],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    base.update(overrides)
    return WalletData(**base)


# ---------------------------------------------------------------------------
# Layer 2 render
# ---------------------------------------------------------------------------


def test_layer2_renders_in_headline_when_present():
    data = _wallet_data(
        archetype_layer1="directional_discretionary",
        archetype_layer2="thesis_specialist",
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.archetype_layer2 == "thesis_specialist"
    md = render_report(report)
    assert "**archetype (Layer 1):** directional_discretionary" in md
    assert "**archetype (Layer 2):** thesis_specialist" in md


def test_layer2_renders_em_dash_when_absent():
    data = _wallet_data(archetype_layer1="directional_discretionary")  # layer2=None
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.archetype_layer2 is None
    md = render_report(report)
    assert "**archetype (Layer 2):** —" in md


# ---------------------------------------------------------------------------
# Execution quality block
# ---------------------------------------------------------------------------


def _populated_execution_profile() -> dict:
    return {
        "status": "populated",
        "sample_tier": "ok",
        "drifts_6h": [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
        "skew_vs_preceding_bar": {"mean": -0.005, "median": -0.003, "n": 12},
        "skew_vs_tape_5min": {"mean": -0.002, "median": -0.001, "n": 10},
        "drift_1h": {"mean": 0.011, "median": 0.010, "n": 12},
        "drift_6h": {"mean": 0.020, "median": 0.018, "n": 12},
        "drift_24h": {"mean": 0.045, "median": 0.040, "n": 11},
    }


def test_execution_quality_block_renders_when_profile_populated():
    data = _wallet_data(execution_profile=_populated_execution_profile())
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.execution_quality_summary is not None
    assert "skew_vs_preceding_bar" in report.execution_quality_summary
    md = render_report(report)
    assert "## Execution quality" in md
    assert "skew_vs_preceding_bar:" in md
    assert "skew_vs_tape_5min:" in md
    assert "drift_1h:" in md
    assert "drift_6h:" in md
    assert "drift_24h:" in md


def test_execution_quality_block_omitted_when_no_profile():
    data = _wallet_data(execution_profile=None)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.execution_quality_summary is None
    md = render_report(report)
    assert "## Execution quality" not in md


def test_execution_quality_block_omitted_when_profile_unpopulated():
    data = _wallet_data(execution_profile={"status": "insufficient_sample"})
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.execution_quality_summary is None
    md = render_report(report)
    assert "## Execution quality" not in md


# ---------------------------------------------------------------------------
# H4 drift companion (drift_1h_summary / drift_24h_summary inside H4 block)
# ---------------------------------------------------------------------------


def test_h4_block_renders_drift_companion_summaries():
    data = _wallet_data(execution_profile=_populated_execution_profile())
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    # Drift companion lines appear after the H4 hypothesis header.
    assert "drift_1h_summary:" in md
    assert "drift_24h_summary:" in md
    # And specifically after the H4 marker, not somewhere unrelated.
    h4_section = md.split("#### H4 — verdict")[-1]
    assert "drift_1h_summary:" in h4_section
    assert "drift_24h_summary:" in h4_section


def test_h4_drift_companion_omitted_when_profile_unpopulated():
    data = _wallet_data(execution_profile=None)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    assert "drift_1h_summary" not in md
    assert "drift_24h_summary" not in md


# ---------------------------------------------------------------------------
# Hard invariant: explanatory surfaces NEVER change verdict / gate /
# primary_edge_type compared to a matched run with these fields cleared.
# ---------------------------------------------------------------------------


def _verdict_signature(report: ProtocolReport) -> tuple:
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
    )


def test_layer2_does_not_alter_verdict_signature():
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    base = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="directional_discretionary",
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    enriched = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="directional_discretionary",
        archetype_layer2="thesis_specialist",
        execution_profile=None,
        fetched_at=_now(),
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_enr = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=200, seed=1)
    assert _verdict_signature(r_base) == _verdict_signature(r_enr)


def test_execution_quality_summary_does_not_alter_verdict_signature():
    """An execution_profile that ONLY adds populated execution-quality
    summary fields (without changing the H4-relevant drifts_6h list) must
    not change overall_verdict / gate_result / primary_edge_type."""
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    profile_with_quality = _populated_execution_profile()
    profile_without_quality = {
        "status": "populated",
        "sample_tier": "ok",
        "drifts_6h": list(profile_with_quality["drifts_6h"]),
    }
    base = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=profile_without_quality,
        fetched_at=_now(),
    )
    enriched = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=profile_with_quality,
        fetched_at=_now(),
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_enr = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=200, seed=1)
    assert _verdict_signature(r_base) == _verdict_signature(r_enr)
