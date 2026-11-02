"""Exit Attribution v1 — unit + integration tests.

Hard invariant: attribution surfaces are display-only — verdict / gate /
primary_edge_type must NEVER change as a function of attribution data.

Coverage:
- classify_position decision tree across all six categories.
- Coverage rules: empty activity, position outside activity window,
  truncated history without subgraph fallback.
- compute_attribution_summary aggregate shape.
- Per-bucket attribution slice in outcome_side_breakdown.
- Render: per-bucket attribution lines, top-level coverage block,
  conservative caveat strings.
- Verdict invariance: attribution does not perturb (overall_verdict,
  gate_result, primary_edge_type, copyability_note).
"""
from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.exit_attribution import (
    ALL_CATEGORIES,
    attribute_subset,
    classify_position,
    compute_attribution_summary,
)
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


def _activity_row(cid="cid_a", outcome="Yes", side="BUY", ts=None):
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
    return {
        "conditionId": cid,
        "outcome": outcome,
        "side": side,
        "timestamp": ts,
        "type": "TRADE",
    }


# ---------------------------------------------------------------------------
# classify_position decision tree
# ---------------------------------------------------------------------------


def test_classify_held_to_resolution_win():
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1000)]
    cat = classify_position(
        pos, activity, activity_min_ts=1000, has_full_history=True,
    )
    assert cat == "held_to_resolution_win"


def test_classify_held_to_resolution_loss():
    pos = _pos(curPrice=0.0, outcome="Yes", realizedPnl=-50.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1000)]
    cat = classify_position(
        pos, activity, activity_min_ts=1000, has_full_history=True,
    )
    assert cat == "held_to_resolution_loss"


def test_classify_pre_resolution_profitable_exit():
    pos = _pos(curPrice=0.55, realizedPnl=20.0, ts=2000)
    activity = [
        _activity_row(side="BUY", ts=1000),
        _activity_row(side="SELL", ts=1500),
    ]
    cat = classify_position(
        pos, activity, activity_min_ts=1000, has_full_history=True,
    )
    assert cat == "pre_resolution_profitable_exit"


def test_classify_pre_resolution_loss_cut():
    pos = _pos(curPrice=0.30, realizedPnl=-15.0, ts=2000)
    activity = [
        _activity_row(side="BUY", ts=1000),
        _activity_row(side="SELL", ts=1400),
    ]
    cat = classify_position(
        pos, activity, activity_min_ts=1000, has_full_history=True,
    )
    assert cat == "pre_resolution_loss_cut"


def test_classify_unresolved_or_unknown_when_full_history_and_no_sell():
    """No SELL captured + curPrice not at resolution + history is full →
    genuine ambiguity (early redeem, etc.) → `unresolved_or_unknown`."""
    pos = _pos(curPrice=0.50, realizedPnl=5.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1000)]
    cat = classify_position(
        pos, activity, activity_min_ts=1000, has_full_history=True,
    )
    assert cat == "unresolved_or_unknown"


def test_classify_insufficient_when_truncated_history_and_no_sell_match():
    """When history may be truncated (cap hit, no subgraph fallback) and
    no SELL is captured for a non-at-resolution position, the SELL might
    have been outside the cap window. Honest answer: insufficient_data."""
    pos = _pos(curPrice=0.30, realizedPnl=-12.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1500)]
    cat = classify_position(
        pos, activity, activity_min_ts=1500, has_full_history=False,
    )
    assert cat == "insufficient_data"


def test_classify_insufficient_when_position_predates_activity_window():
    # Use absolute unix timestamps so _pos doesn't rewrite via the
    # "small ts → now-1d" convenience.
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=1_500_000_000)
    activity = [_activity_row(side="BUY", ts=1_600_000_000)]
    cat = classify_position(
        pos, activity, activity_min_ts=1_600_000_000, has_full_history=True,
    )
    assert cat == "insufficient_data"


def test_classify_insufficient_when_no_activity_rows():
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=2000)
    cat = classify_position(
        pos, activity_rows=[], activity_min_ts=None, has_full_history=False,
    )
    assert cat == "insufficient_data"


def test_classify_insufficient_when_at_resolution_but_history_truncated():
    """At resolution + no SELL captured + history truncated:
    Codex PR #9 review item 1. Previously this returned
    `held_to_resolution_win` on the assumption that curPrice is
    observable regardless of history coverage. That's true for the
    curPrice — but a wallet may have BOUGHT, SOLD inside the missed
    REST window, and then watched the market resolve in its favor.
    The absent SELL is not reliable evidence of holding when history
    is truncated. Refuse to attribute. The conservative move is
    `insufficient_data`."""
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1500)]
    cat = classify_position(
        pos, activity, activity_min_ts=1500, has_full_history=False,
    )
    assert cat == "insufficient_data"


def test_classify_held_to_resolution_when_subgraph_fallback_used():
    """Cap hit BUT subgraph fallback ran → has_full_history=True →
    SELL absence is reliable → held_to_resolution_win.
    Required by Codex PR #9 review item 1: existing fallback paths
    must keep producing the held label when history is reconstructed.
    """
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1500)]
    cat = classify_position(
        pos, activity, activity_min_ts=1500, has_full_history=True,
    )
    assert cat == "held_to_resolution_win"


def test_classify_held_to_resolution_when_walk_full_history_used():
    """Cap hit BUT walk_full_history ran → has_full_history=True →
    SELL absence is reliable → held_to_resolution_win.
    Same intent as the subgraph_fallback test, different branch.
    Required by Codex PR #9 review item 1."""
    pos = _pos(curPrice=1.0, realizedPnl=50.0, ts=2000)
    activity = [_activity_row(side="BUY", ts=1500)]
    cat = classify_position(
        pos, activity, activity_min_ts=1500, has_full_history=True,
    )
    assert cat == "held_to_resolution_win"


# ---------------------------------------------------------------------------
# compute_attribution_summary aggregate shape
# ---------------------------------------------------------------------------


def test_compute_attribution_summary_returns_none_for_empty_closed():
    out = compute_attribution_summary(
        closed_positions=[], activity_rows=[],
        activity_cap_hit=False, subgraph_fallback_used=False,
    )
    assert out is None


def test_compute_attribution_summary_aggregates_categories():
    closed = [
        _pos(cid="cid_h_w", curPrice=1.0, realizedPnl=100.0, ts=2000),
        _pos(cid="cid_h_l", curPrice=0.0, outcome="Yes", realizedPnl=-50.0, ts=2100),
        _pos(cid="cid_pre_w", curPrice=0.55, realizedPnl=30.0, ts=2200),
        _pos(cid="cid_pre_l", curPrice=0.30, realizedPnl=-20.0, ts=2300),
        _pos(cid="cid_unk", curPrice=0.40, realizedPnl=0.0, ts=2400),
    ]
    activity = [
        _activity_row(cid="cid_h_w", side="BUY", ts=1000),
        _activity_row(cid="cid_h_l", side="BUY", ts=1100),
        _activity_row(cid="cid_pre_w", side="BUY", ts=1200),
        _activity_row(cid="cid_pre_w", side="SELL", ts=2150),
        _activity_row(cid="cid_pre_l", side="BUY", ts=1300),
        _activity_row(cid="cid_pre_l", side="SELL", ts=2250),
        _activity_row(cid="cid_unk", side="BUY", ts=1400),
    ]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
    )
    s = out["summary"]
    assert s["held_to_resolution_win"]["n"] == 1
    assert s["held_to_resolution_win"]["total_pnl"] == pytest.approx(100.0)
    assert s["held_to_resolution_loss"]["n"] == 1
    assert s["held_to_resolution_loss"]["total_pnl"] == pytest.approx(-50.0)
    assert s["pre_resolution_profitable_exit"]["n"] == 1
    assert s["pre_resolution_profitable_exit"]["total_pnl"] == pytest.approx(30.0)
    assert s["pre_resolution_loss_cut"]["n"] == 1
    assert s["pre_resolution_loss_cut"]["total_pnl"] == pytest.approx(-20.0)
    assert s["unresolved_or_unknown"]["n"] == 1
    cov = out["coverage"]
    assert cov["n_total"] == 5
    assert cov["history_assumed_full"] is True


def test_compute_attribution_summary_coverage_flags_truncated_history():
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)]
    activity = [_activity_row(side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True, subgraph_fallback_used=False,
    )
    assert out["coverage"]["history_assumed_full"] is False
    # Position has no SELL match + history truncated → insufficient_data.
    assert out["summary"]["insufficient_data"]["n"] == 1


def test_compute_attribution_summary_at_resolution_truncated_demoted_to_insufficient():
    """Codex PR #9 review item 1, aggregate-level check.
    A position with curPrice ≈ 1.0, realizedPnl > 0, activity_cap_hit=True,
    no fallback, no walk → must be `insufficient_data`, not
    `held_to_resolution_win`. SELL might just be outside the captured window."""
    closed = [_pos(curPrice=1.0, realizedPnl=50.0, ts=2000)]
    activity = [_activity_row(side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    assert out["summary"]["insufficient_data"]["n"] == 1
    assert out["summary"]["held_to_resolution_win"]["n"] == 0


def test_compute_attribution_summary_at_resolution_subgraph_fallback_held():
    """Codex PR #9 review item 1, aggregate-level positive case.
    Same fixture but subgraph_fallback_used=True → held label stands."""
    closed = [_pos(curPrice=1.0, realizedPnl=50.0, ts=2000)]
    activity = [_activity_row(side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=False,
    )
    assert out["summary"]["held_to_resolution_win"]["n"] == 1
    assert out["summary"]["insufficient_data"]["n"] == 0


def test_compute_attribution_summary_at_resolution_walk_full_history_held():
    """Codex PR #9 review item 1, aggregate-level positive case.
    Same fixture but walk_full_history=True → held label stands."""
    closed = [_pos(curPrice=1.0, realizedPnl=50.0, ts=2000)]
    activity = [_activity_row(side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=True,
    )
    assert out["summary"]["held_to_resolution_win"]["n"] == 1
    assert out["summary"]["insufficient_data"]["n"] == 0


def test_compute_attribution_summary_treats_walk_full_history_as_full_history():
    closed = [_pos(curPrice=0.50, realizedPnl=0.0, ts=2000)]
    activity = [_activity_row(cid="cid_other", side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=True,
    )
    assert out["coverage"]["history_assumed_full"] is True
    assert out["coverage"]["walk_full_history"] is True
    assert out["summary"]["unresolved_or_unknown"]["n"] == 1
    assert out["summary"]["insufficient_data"]["n"] == 0


def test_attribute_subset_treats_walk_full_history_as_full_history():
    rows = [_pos(curPrice=0.50, realizedPnl=0.0, ts=2000)]
    activity = [_activity_row(cid="cid_other", side="BUY", ts=1500)]
    summary = attribute_subset(
        rows, activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=True,
    )
    assert summary["unresolved_or_unknown"]["n"] == 1
    assert summary["insufficient_data"]["n"] == 0


def test_compute_attribution_summary_outside_window_count():
    closed = [
        _pos(cid="cid_old", curPrice=1.0, realizedPnl=50.0, ts=500),
        _pos(cid="cid_new", curPrice=1.0, realizedPnl=50.0, ts=2000),
    ]
    activity = [_activity_row(cid="cid_new", side="BUY", ts=1500)]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
    )
    assert out["coverage"]["n_outside_window"] == 1
    assert out["summary"]["insufficient_data"]["n"] == 1
    assert out["summary"]["held_to_resolution_win"]["n"] == 1


def test_compute_attribution_summary_includes_n_with_resolved_attribution():
    """R10 Option A: coverage dict carries n_with_resolved_attribution
    (= held_w + held_l + pre_w + pre_l). Excludes unresolved + insufficient."""
    closed = (
        # 5 held wins (curPrice=1.0, no SELL captured, history full)
        [_pos(cid=f"h_w_{i}", outcome="Yes", avgPrice=0.30,
              curPrice=1.0, realizedPnl=70.0, ts=2000) for i in range(5)]
        # 5 held losses (curPrice=0.0)
        + [_pos(cid=f"h_l_{i}", outcome="Yes", avgPrice=0.30,
                curPrice=0.0, realizedPnl=-30.0, ts=2000) for i in range(5)]
        # 5 unresolved (curPrice mid-band, no SELL match, history full
        # → unresolved_or_unknown, NOT insufficient_data)
        + [_pos(cid=f"unres_{i}", outcome="Yes", avgPrice=0.30,
                curPrice=0.50, realizedPnl=0.0, ts=2000) for i in range(5)]
    )
    # Sentinel BUY for an unrelated cid before all closed-position
    # timestamps — gives a non-empty activity stream / activity_min_ts
    # so the held + unresolved positions don't short-circuit to
    # insufficient_data.
    activity = [{
        "side": "BUY", "conditionId": "sentinel", "outcome": "Yes",
        "price": 0.50, "timestamp": 1500, "size": 100, "asset": "tok_x",
    }]
    out = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    cov = out["coverage"]
    assert cov["n_total"] == 15
    # New field: held_w + held_l + pre_w + pre_l = 5 + 5 + 0 + 0 = 10.
    # Excludes the 5 unresolved_or_unknown.
    assert cov["n_with_resolved_attribution"] == 10
    # Sample-size floor (n_attributed = n_total - insufficient_data) is
    # still 15 - 0 = 15 — preserved for Timing Surface.
    n_insuf = out["summary"]["insufficient_data"]["n"]
    assert n_insuf == 0
    assert cov["n_total"] - n_insuf == 15


# ---------------------------------------------------------------------------
# Per-bucket attribution slice (in outcome_side_breakdown)
# ---------------------------------------------------------------------------


def _wallet_data_with_attribution() -> WalletData:
    """30 closed positions with a mix of attribution categories."""
    closed = []
    activity = []
    # 10 NO held-to-resolution wins. Under held-token semantics (R2,
    # cba241e) the held NO token redeems at $1 when NO wins; we use
    # curPrice=1.0 here. realizedPnl=60 is consistent (NO bought at
    # avgPrice=0.40, held to settlement at 1.0).
    for i in range(10):
        closed.append(_pos(cid=f"cid_no_w_{i}", outcome="No",
                           curPrice=1.0, avgPrice=0.40,
                           realizedPnl=60.0, ts=3000 + i))
        activity.append(_activity_row(cid=f"cid_no_w_{i}",
                                      outcome="No", side="BUY",
                                      ts=1000 + i))
    # 5 NO pre-resolution profitable exits
    for i in range(5):
        closed.append(_pos(cid=f"cid_no_pre_w_{i}", outcome="No",
                           curPrice=0.45, avgPrice=0.40,
                           realizedPnl=15.0, ts=3100 + i))
        activity.append(_activity_row(cid=f"cid_no_pre_w_{i}",
                                      outcome="No", side="BUY", ts=1100 + i))
        activity.append(_activity_row(cid=f"cid_no_pre_w_{i}",
                                      outcome="No", side="SELL", ts=3050 + i))
    # 15 YES held-to-resolution losses
    for i in range(15):
        closed.append(_pos(cid=f"cid_yes_l_{i}", outcome="Yes",
                           curPrice=0.0, avgPrice=0.40,
                           realizedPnl=-40.0, ts=3200 + i))
        activity.append(_activity_row(cid=f"cid_yes_l_{i}",
                                      outcome="Yes", side="BUY",
                                      ts=1200 + i))
    return WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=activity,
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )


def test_per_bucket_attribution_populated_in_breakdown():
    data = _wallet_data_with_attribution()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    osb = report.outcome_side_breakdown
    assert osb is not None
    no_bucket = osb["no"]
    attr = no_bucket["by_attribution"]
    assert attr["held_to_resolution_win"]["n"] == 10
    assert attr["held_to_resolution_win"]["total_pnl"] == pytest.approx(600.0)
    assert attr["pre_resolution_profitable_exit"]["n"] == 5
    assert attr["pre_resolution_profitable_exit"]["total_pnl"] == pytest.approx(75.0)
    yes_bucket = osb["yes"]
    yes_attr = yes_bucket["by_attribution"]
    assert yes_attr["held_to_resolution_loss"]["n"] == 15


def test_top_level_exit_attribution_populated():
    data = _wallet_data_with_attribution()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    ea = report.exit_attribution
    assert ea is not None
    assert ea["coverage"]["n_total"] == 30
    assert ea["coverage"]["history_assumed_full"] is True
    s = ea["summary"]
    assert s["held_to_resolution_win"]["n"] == 10
    assert s["held_to_resolution_loss"]["n"] == 15
    assert s["pre_resolution_profitable_exit"]["n"] == 5


def test_analyze_wallet_walk_full_history_does_not_false_low_coverage_timing():
    closed = [
        _pos(cid=f"cid_mid_{i}", curPrice=0.50, realizedPnl=0.0, ts=2000 + i)
        for i in range(30)
    ]
    activity = [_activity_row(cid="cid_other", side="BUY", ts=1500)]
    data = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=activity,
        activity_cap_hit=True,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        walk_full_history=True,
    )

    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)

    ea = report.exit_attribution
    assert ea is not None
    assert ea["summary"]["unresolved_or_unknown"]["n"] == 30
    assert ea["summary"]["insufficient_data"]["n"] == 0
    assert report.timing_surface is not None
    assert report.timing_surface["n_attributed"] == 30
    assert "ATTRIBUTION_COVERAGE_LOW" not in report.timing_surface["coverage_caveats"]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_includes_per_bucket_exit_attribution():
    data = _wallet_data_with_attribution()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "Exit attribution:" in md
    assert "Held to resolution:" in md
    assert "Pre-resolution exit:" in md
    assert "WR_resolution=" in md
    assert "WR_exit=" in md
    # Top-level coverage now lives in `## Data completeness`.
    assert "## Data completeness" in md
    assert "### Attribution coverage" in md
    assert "n_attributed:" in md


def test_render_emits_caveat_when_history_truncated_and_insufficient_present():
    closed = [_pos(curPrice=0.30, outcome="Yes", realizedPnl=-10.0, ts=2000)]
    activity = [_activity_row(side="BUY", ts=1500)]
    data = WalletData(
        wallet="0xabc",
        closed_positions=closed * 40,  # bring R2 above 30
        open_positions=[],
        activity_rows=activity,
        activity_cap_hit=True,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    # New tiered caveats live inside `## Data completeness`.
    assert "## Data completeness" in md
    assert "ACTIVITY_TRUNCATED:" in md
    # < 50% of closed positions reliably attributed → low-coverage caveat
    assert "ATTRIBUTION_COVERAGE_LOW:" in md
    # Partial-fill / multi-SELL limitation is always surfaced when there
    # are closed positions in the universe.
    assert "LIMITATION: exit attribution treats each position" in md


def test_render_omits_attribution_when_no_closed_positions():
    data = WalletData(
        wallet="0xabc",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.exit_attribution is None
    md = render_report(report)
    # Per-bucket attribution requires closed positions; nothing to render.
    assert "Exit attribution:" not in md
    # The Completeness section is always rendered; with zero closed
    # positions it explicitly states there's nothing to attribute.
    assert "## Data completeness" in md
    assert "n_closed_positions: 0 (no closed positions to attribute)" in md
    # No caveats when there's no closed-position universe.
    assert "LIMITATION: exit attribution" not in md


# ---------------------------------------------------------------------------
# Verdict invariance — attribution must NEVER alter verdict signature
# ---------------------------------------------------------------------------


def _verdict_signature(report: ProtocolReport) -> tuple:
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
    )


def test_attribution_does_not_alter_verdict_signature():
    """Two matched runs — one with activity_rows that enable rich
    attribution, one with empty activity_rows — must produce identical
    verdict signatures on the same closed_positions."""
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    base = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],  # → attribution all `insufficient_data`
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    enriched = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[
            _activity_row(side="BUY", ts=500 + i) for i in range(40)
        ],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_enr = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=200, seed=1)
    assert _verdict_signature(r_base) == _verdict_signature(r_enr)
    # And attribution is genuinely populated only for the enriched run.
    assert r_base.exit_attribution["summary"]["insufficient_data"]["n"] == 40
    assert r_enr.exit_attribution["summary"]["insufficient_data"]["n"] == 0


def test_categories_constant_locked():
    """Lock the public AttributionCategory tuple — downstream code may
    iterate it; reordering or renaming is a breaking change."""
    assert ALL_CATEGORIES == (
        "held_to_resolution_win",
        "held_to_resolution_loss",
        "pre_resolution_profitable_exit",
        "pre_resolution_loss_cut",
        "unresolved_or_unknown",
        "insufficient_data",
    )
