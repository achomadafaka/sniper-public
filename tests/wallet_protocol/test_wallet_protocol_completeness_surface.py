"""Completeness Surface v1 — render + plumbing + verdict-invariance tests.

The Completeness Surface is the top-of-report block that exposes how much of
the wallet's history was actually inspected before any performance numbers
are read. It owns:

  ### Activity capture
    - activity_rows_fetched (with CAP HIT marker)
    - walk_full_history (enabled / disabled)
    - subgraph_fallback_used (true / false)
    - n_unmapped_fills

  ### Attribution coverage
    - n_closed_positions
    - n_outside_activity_window (n + share)
    - n_insufficient_data (n + share)
    - n_attributed (n + share)

  ### Caveats (tiered, conditional)
    - ACTIVITY_TRUNCATED   — cap hit AND not walk_full_history AND not
      subgraph_fallback_used.
    - SUBGRAPH_UNMAPPED_FILLS — n_unmapped_fills > 0.
    - ATTRIBUTION_COVERAGE_LOW — < 50% of closed positions reliably attributed.
    - LIMITATION (always when closed positions exist) — partial-fill /
      multi-SELL exit ladders not yet modelled.

Hard invariant: this surface is display-only. The same closed-position
universe must produce identical (overall_verdict, gate_result,
primary_edge_type, copyability_note) regardless of which capture-state
flags fired.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.replay import (
    deserialize_wallet_data,
    save_fetch_snapshot,
    serialize_wallet_data,
)
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid="cid_a",
    outcome="Yes",
    curPrice=1.0,
    avgPrice=0.50,
    size=100,
    realizedPnl=50.0,
    ts=None,
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
        "eventSlug": f"slug-{cid}",
    }


def _activity(cid="cid_a", outcome="Yes", side="BUY", ts=None):
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


def _wallet_data(
    *,
    closed_positions,
    activity_rows,
    activity_cap_hit=False,
    subgraph_fallback_used=False,
    walk_full_history=False,
    n_unmapped_fills=0,
    fetch_termination=None,
):
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions),
        open_positions=[],
        activity_rows=list(activity_rows),
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        n_unmapped_fills=n_unmapped_fills,
        fetch_termination=fetch_termination,
    )


# ---------------------------------------------------------------------------
# Plumbing: WalletData → ProtocolReport (analyze_wallet copies both fields)
# ---------------------------------------------------------------------------


def test_walk_full_history_and_n_unmapped_propagate_to_report():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 30,
        activity_rows=[_activity(side="BUY", ts=1000)],
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=True,
        n_unmapped_fills=7,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert isinstance(report, ProtocolReport)
    assert report.walk_full_history is True
    assert report.n_unmapped_fills == 7
    assert report.activity_cap_hit is True
    assert report.subgraph_fallback_used is True
    assert report.activity_rows_fetched == 1


# ---------------------------------------------------------------------------
# Replay round-trip: serialize / deserialize preserves capture-state
# ---------------------------------------------------------------------------


def test_capture_state_round_trips_through_v2_fixture(tmp_path: Path):
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=5.0)],
        activity_rows=[_activity()],
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=True,
        n_unmapped_fills=4,
    )
    # The replay layer requires v2 fixtures to carry replay_surface_meta;
    # provide a minimal non-null dict so the round-trip exercises the new
    # fields under the same load path used in production.
    data.replay_surface_meta = {"meta": {"recorded": True}}

    target = tmp_path / "snap.json"
    save_fetch_snapshot(data, target)
    payload = json.loads(target.read_text())
    assert payload["walk_full_history"] is True
    assert payload["n_unmapped_fills"] == 4

    restored = deserialize_wallet_data(payload)
    assert restored.walk_full_history is True
    assert restored.n_unmapped_fills == 4


def test_legacy_v1_fixture_defaults_capture_state_to_safe_values():
    """Pre-Completeness fixtures didn't track these fields. They must
    deserialize to (False, 0) — truthful, since the capture state was
    not recorded at fetch time."""
    legacy = {
        "schema_version": 1,
        "wallet": "0xabc",
        "fetched_at": 1_700_000_000,
        "closed_positions": [],
        "open_positions": [],
        "activity_rows": [],
        "activity_cap_hit": False,
        "archetype_layer1": None,
        "execution_profile": None,
    }
    restored = deserialize_wallet_data(legacy)
    assert restored.walk_full_history is False
    assert restored.n_unmapped_fills == 0


# ---------------------------------------------------------------------------
# Render: section is always present and reports the right state
# ---------------------------------------------------------------------------


def test_render_always_emits_data_completeness_section_even_without_closed():
    """A wallet with no closed positions still gets a Completeness section
    so the reader can see whether the capture itself was complete."""
    data = _wallet_data(closed_positions=[], activity_rows=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "## Data completeness" in md
    assert "### Activity capture" in md
    assert "### Attribution coverage" in md
    assert "n_closed_positions: 0 (no closed positions to attribute)" in md
    # No closed-position universe → no LIMITATION line.
    assert "LIMITATION: exit attribution" not in md


def test_render_activity_capture_lines_reflect_fetch_state():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=False,
        n_unmapped_fills=3,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "activity_rows_fetched: 1 (CAP HIT)" in md
    assert "walk_full_history: disabled" in md
    assert "subgraph_fallback_used: true" in md
    assert "n_unmapped_fills: 3" in md


def test_render_closed_positions_capture_line_reflects_cap_state():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
        fetch_termination={"closed_positions": "api_refused"},
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)

    assert "closed_positions_fetched: 5 (CAP HIT — closed history may be truncated)" in md


def test_render_closed_positions_capture_line_reflects_complete_state():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
        fetch_termination={"closed_positions": "empty_page"},
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)

    assert "closed_positions_fetched: 5 (capture complete)" in md


def test_render_clean_capture_marks_no_caveats():
    """Healthy capture (no cap hit, no unmapped fills, full attribution,
    tags found) explicitly states 'None' so the reader knows nothing fired.

    G4 (2026-05-17) widened "clean" to also require tag_lookup_state="ok";
    without it the default-inferred "unavailable" fires the
    TAG_LOOKUP_UNAVAILABLE caveat and the no-warnings line never renders."""
    closed = [
        _pos(curPrice=1.0, realizedPnl=10.0, ts=2000) for _ in range(30)
    ]
    activity = [_activity(side="BUY", ts=1000)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=200, seed=1,
        leafs_by_cid={"cid_a": ["election"]},
        nav_lookup={"election": "politics"},
        tag_lookup_state="ok",
    )
    md = render_report(report)
    assert "### Caveats" in md
    assert "None — capture window covers the closed-position universe." in md
    # Only the LIMITATION caveat should NOT fire here, because the all-clear
    # branch doesn't render LIMITATION (it only renders the no-warnings line).
    assert "ACTIVITY_TRUNCATED:" not in md
    assert "SUBGRAPH_UNMAPPED_FILLS:" not in md
    assert "ATTRIBUTION_COVERAGE_LOW:" not in md
    assert "TAG_LOOKUP_UNAVAILABLE:" not in md
    assert "CLOSED_POSITIONS_TRUNCATED:" not in md


def test_caveat_closed_positions_truncated_renders_when_cap_hit():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 30,
        activity_rows=[_activity(side="BUY", ts=1500)],
        fetch_termination={"closed_positions": "api_refused"},
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)

    assert "CLOSED_POSITIONS_TRUNCATED:" in md


# ---------------------------------------------------------------------------
# Phase A render symmetry — G3 / G4 / G6
# Spec: docs/superpowers/specs/2026-05-17-phase-a-render-symmetry.md
# Codex closed G2 render gap (c4ec26d); these close the same gap for
# tag_lookup_state (G4), triage_context (G3), and category_clusters (G6).
# ---------------------------------------------------------------------------


def test_render_tag_lookup_state_ok_line():
    """G4 — tag_lookup_state="ok" shows the state in the Activity capture block."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        leafs_by_cid={"cid_a": ["election"]},
        nav_lookup={"election": "politics"},
        tag_lookup_state="ok",
    )
    md = render_report(report)
    assert "tag_lookup_state: ok" in md
    assert "TAG_LOOKUP_UNAVAILABLE:" not in md


def test_render_tag_lookup_state_unavailable_line_and_caveat():
    """G4 — 'unavailable' renders BOTH the state line and the caveat."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        tag_lookup_state="unavailable",
    )
    md = render_report(report)
    assert "tag_lookup_state: unavailable" in md
    assert "TAG_LOOKUP_UNAVAILABLE:" in md


def test_render_tag_lookup_state_skipped_line_no_caveat():
    """G4 — 'skipped' shows the state but no caveat."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        tag_lookup_state="skipped",
    )
    md = render_report(report)
    assert "tag_lookup_state: skipped" in md
    assert "TAG_LOOKUP_UNAVAILABLE:" not in md


def test_render_tag_lookup_state_empty_line_no_caveat():
    """G4 — 'empty' shows the state but no caveat."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        leafs_by_cid={},
        tag_lookup_state="empty",
    )
    md = render_report(report)
    assert "tag_lookup_state: empty" in md
    assert "TAG_LOOKUP_UNAVAILABLE:" not in md


def test_render_triage_context_block_when_present():
    """G3 — triage_context dict renders as `## Triage context` block
    with one line per key:value."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        triage_context={"source": "lb", "period": "MONTH", "rank": 3},
    )
    md = render_report(report)
    assert "## Triage context" in md
    assert "- source: lb" in md
    assert "- period: MONTH" in md
    assert "- rank: 3" in md


def test_render_triage_context_block_absent_when_none():
    """G3 — when triage_context is None, the section header is NOT rendered."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        triage_context=None,
    )
    md = render_report(report)
    assert "## Triage context" not in md


def test_render_triage_context_fetched_at_int_renders_as_iso8601():
    """G3 — fetched_at as unix epoch int renders as ISO-8601 UTC string."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    # 1747700000 == 2025-05-19T23:33:20Z; just confirm the year+T format.
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        triage_context={"fetched_at": 1747700000},
    )
    md = render_report(report)
    assert "## Triage context" in md
    # ISO-8601 substring: yyyy-mm-ddTHH:MM:SSZ
    import re as _re
    assert _re.search(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b", md), (
        f"Expected ISO-8601 timestamp in markdown; got: {md[:500]}..."
    )


def test_render_category_clusters_when_populated():
    """G6 — category_clusters list renders as `### Category clusters` with
    one line per cluster and (primary) annotation on the first."""
    closed = [_pos(f"cid_p{i}", realizedPnl=100.0) for i in range(6)]
    closed += [_pos(f"cid_s{i}", realizedPnl=-20.0) for i in range(2)]
    leafs_by_cid = {**{f"cid_p{i}": ["election"] for i in range(6)},
                    **{f"cid_s{i}": ["nba"] for i in range(2)}}
    nav_lookup = {"election": "politics", "nba": "sports"}

    data = _wallet_data(
        closed_positions=closed,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        tag_lookup_state="ok",
    )
    md = render_report(report)
    assert "### Category clusters" in md
    assert "politics" in md
    assert "sports" in md
    assert "(primary)" in md


def test_render_category_clusters_absent_when_no_data():
    """G6 — without leafs_by_cid + nav_lookup, the producer omits
    category_clusters and the renderer omits the section."""
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
    )
    md = render_report(report)
    assert "### Category clusters" not in md


def test_render_category_clusters_handles_zero_total_pnl():
    """G6 — when total_pnl is 0 (all positions had realizedPnl=0), share
    renders as '—' to avoid implying a numeric share."""
    closed = [_pos(f"cid_z{i}", realizedPnl=0.0) for i in range(5)]
    leafs_by_cid = {f"cid_z{i}": ["election"] for i in range(5)}
    nav_lookup = {"election": "politics"}

    data = _wallet_data(
        closed_positions=closed,
        activity_rows=[_activity(side="BUY", ts=1500)],
    )
    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=50, seed=1,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        tag_lookup_state="ok",
    )
    md = render_report(report)
    # The cluster line must render without crashing and show "—" for share.
    assert "### Category clusters" in md
    assert "share=—" in md


# ---------------------------------------------------------------------------
# Caveat firing matrix
# ---------------------------------------------------------------------------


def test_caveat_activity_truncated_fires_when_capped_without_backfill():
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "ACTIVITY_TRUNCATED:" in md


def test_caveat_activity_truncated_suppressed_when_subgraph_backfilled():
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "ACTIVITY_TRUNCATED:" not in md


def test_caveat_activity_truncated_suppressed_when_walk_full_history():
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=True,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "ACTIVITY_TRUNCATED:" not in md


def test_caveat_subgraph_unmapped_fills_fires_when_unmapped_count_positive():
    data = _wallet_data(
        closed_positions=[_pos(realizedPnl=10.0, ts=2000)] * 5,
        activity_rows=[_activity()],
        n_unmapped_fills=12,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "SUBGRAPH_UNMAPPED_FILLS:" in md
    assert "12 subgraph fills" in md


def test_caveat_attribution_coverage_low_fires_below_50_percent():
    """40 positions, all outside activity window → all `insufficient_data`
    when capture is truncated → 0% attributed → ATTRIBUTION_COVERAGE_LOW."""
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]  # min_ts after pos ts
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "ATTRIBUTION_COVERAGE_LOW:" in md


def test_attribution_coverage_low_fires_when_resolved_attribution_below_50pct():
    """R10 Option A: ATTRIBUTION_COVERAGE_LOW caveat fires when
    n_with_resolved_attribution / n_total < 0.50, even if n_attributed
    (which includes unresolved) is high. Locks the swap from
    n_attributed-based trigger to n_with_resolved-based trigger.

    Fixture: 5 held wins + 25 unresolved positions.
      - n_total = 30
      - n_insufficient_data = 0    (history is full, sentinel activity present)
      - n_attributed (sample floor) = 30 - 0 = 30 → 100% (would NOT fire under
        the old trigger)
      - n_with_resolved = 5 held + 0 preres = 5 → 5/30 = 17% < 50% → DOES fire
    """
    closed = (
        # 5 held wins (curPrice at resolution)
        [_pos(cid=f"h_w_{i}", curPrice=1.0, realizedPnl=10.0, ts=2000)
         for i in range(5)]
        # 25 unresolved (mid-band curPrice, no SELL match, history full)
        + [_pos(cid=f"u_{i}", curPrice=0.50, realizedPnl=0.0, ts=2000)
           for i in range(25)]
    )
    # Sentinel BUY for an unrelated cid → activity_min_ts set, history
    # treated as full (cap_hit=False, walk_full_history default False
    # but cap_hit=False suffices). Without this, all 30 positions would
    # short-circuit to insufficient_data.
    activity = [{
        "conditionId": "sentinel", "outcome": "Yes", "side": "BUY",
        "timestamp": 1500, "type": "TRADE",
    }]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    # Caveat must fire — the new trigger.
    assert "ATTRIBUTION_COVERAGE_LOW:" in md
    # The narrative cites n_with_resolved/n_total (5/30), not n_attributed/n_total.
    assert ("5/30" in md) or ("5 / 30" in md)
    # Sanity: the rendered Attribution coverage section also shows the
    # n_with_resolved_attribution line (since it differs from n_attributed=30).
    assert "n_with_resolved_attribution: 5" in md


def test_limitation_caveat_always_present_when_closed_positions_exist():
    """Even on a clean capture with all closed positions attributed, the
    partial-fill / multi-SELL limitation is a property of the model and
    must NOT silently disappear. (It moves into the Caveats list whenever
    other caveats fire; otherwise the all-clear line replaces it.)"""
    closed = [_pos(curPrice=0.40, realizedPnl=-5.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    # ACTIVITY_TRUNCATED + ATTRIBUTION_COVERAGE_LOW already fire here, so
    # LIMITATION must be in the same Caveats block.
    assert "LIMITATION: exit attribution treats each position" in md


# ---------------------------------------------------------------------------
# Verdict invariance — capture-state flags must NEVER alter the verdict
# ---------------------------------------------------------------------------


def _verdict_signature(r: ProtocolReport) -> tuple:
    return (
        r.overall_verdict,
        r.gate_result,
        r.primary_edge_type,
        r.copyability_note,
    )


def test_capture_state_flags_do_not_alter_verdict_signature():
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    activity = [_activity(side="BUY", ts=900)]

    base = _wallet_data(closed_positions=closed, activity_rows=activity)
    decorated = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=True,
        n_unmapped_fills=11,
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_dec = analyze_wallet("0xabc", pre_fetched_data=decorated, n_iter=200, seed=1)
    assert _verdict_signature(r_base) == _verdict_signature(r_dec)


# ---------------------------------------------------------------------------
# Section ordering: Completeness must precede Capital efficiency / Edge surface
# ---------------------------------------------------------------------------


def test_data_completeness_section_renders_before_capital_efficiency():
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=[_activity(side="BUY", ts=900)],
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    completeness_pos = md.find("## Data completeness")
    capeff_pos = md.find("## Capital efficiency")
    edge_pos = md.find("## Edge surface")
    assert completeness_pos != -1
    # Capital efficiency may be omitted (no valid cost basis) in some setups,
    # but when both render the order must hold.
    if capeff_pos != -1:
        assert completeness_pos < capeff_pos
    if edge_pos != -1:
        assert completeness_pos < edge_pos


# ---------------------------------------------------------------------------
# data_completeness typed JSON field — Phase 3 wiring
# ---------------------------------------------------------------------------


def test_data_completeness_field_default_is_none_on_bare_protocolreport():
    """An ad-hoc ProtocolReport that hasn't been through analyze_wallet
    must default to None — keeps legacy callers / fixtures unaffected."""
    bare = ProtocolReport(wallet="0xnone")
    assert bare.data_completeness is None


def test_data_completeness_field_populated_after_analyze_wallet():
    """analyze_wallet must produce a typed dict carrying the same numbers
    rendered in the `## Data completeness` Markdown block."""
    closed = [_pos(realizedPnl=10.0, ts=2000)] * 5
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=False,
        n_unmapped_fills=3,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    dc = report.data_completeness
    assert isinstance(dc, dict)
    assert set(dc.keys()) == {
        "activity_capture",
        "attribution_coverage",
        "closed_positions_capture",  # G2 — closed-position cap signal
        "tag_lookup_state",           # G4 — tag-lookup availability
        "caveats",
    }
    ac = dc["activity_capture"]
    assert ac["activity_rows_fetched"] == 1
    assert ac["activity_cap_hit"] is True
    assert ac["walk_full_history"] is False
    assert ac["subgraph_fallback_used"] is True
    assert ac["n_unmapped_fills"] == 3
    cov = dc["attribution_coverage"]
    assert isinstance(cov["n_total"], int)
    assert isinstance(cov["n_outside_window"], int)
    assert isinstance(cov["n_insufficient_data"], int)
    assert isinstance(cov["n_attributed"], int)
    assert isinstance(cov["n_with_resolved_attribution"], int)
    assert isinstance(cov["history_assumed_full"], bool)
    cv = dc["caveats"]
    # SUBGRAPH_UNMAPPED_FILLS must fire (n_unmapped_fills > 0).
    assert cv["subgraph_unmapped_fills"] is True
    # Cap hit but subgraph backfilled → activity_truncated suppressed.
    assert cv["activity_truncated"] is False


def test_data_completeness_caveats_match_rendered_block():
    """The typed caveats dict must agree with the rendered Markdown block.
    Locks the contract that the JSON field and the Markdown source the
    same numbers."""
    closed = [_pos(curPrice=0.30, realizedPnl=-10.0, ts=2000)] * 40
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(
        closed_positions=closed,
        activity_rows=activity,
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    dc = report.data_completeness
    md = render_report(report)
    assert dc is not None
    cv = dc["caveats"]
    # ACTIVITY_TRUNCATED triggers when cap_hit and not backfilled.
    assert cv["activity_truncated"] is True
    assert "ACTIVITY_TRUNCATED:" in md
    # ATTRIBUTION_COVERAGE_LOW: capture truncated → no resolved attribution.
    assert cv["attribution_coverage_low"] is True
    assert "ATTRIBUTION_COVERAGE_LOW:" in md
    # LIMITATION fires whenever a closed-position universe exists.
    assert cv["limitation_present"] is True
    assert "LIMITATION: exit attribution" in md


def test_data_completeness_field_does_not_alter_verdict_signature():
    """Typed-field exposure must NEVER alter the verdict signature relative
    to a hand-crafted ProtocolReport that omits the field."""
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    activity = [_activity(side="BUY", ts=900)]
    data = _wallet_data(closed_positions=closed, activity_rows=activity)
    report_a = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    report_b = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    # Mutate the typed field on one copy — must not change the verdict.
    report_b.data_completeness = None
    assert _verdict_signature(report_a) == _verdict_signature(report_b)


def test_render_falls_back_when_data_completeness_field_is_none():
    """Hand-constructed ProtocolReport (legacy callers) without the typed
    field must still render an Activity capture / Attribution coverage
    block via the inline fallback path."""
    closed = [_pos(realizedPnl=10.0, ts=2000)] * 5
    activity = [_activity(side="BUY", ts=1500)]
    data = _wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    # Drop the typed field; the renderer must still produce the block from
    # the underlying capture-state flags + exit_attribution.
    report.data_completeness = None
    md = render_report(report)
    assert "## Data completeness" in md
    assert "### Activity capture" in md
    assert "### Attribution coverage" in md
    assert "activity_rows_fetched: 1" in md
