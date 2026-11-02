"""Acceptance tests — Position Reconciliation Surface (`position_reconciliation_surface`).

Spec: docs/superpowers/specs/2026-05-23-position-reconciliation-surface-design.md

Display-only surface: reconciles /closed-positions against CTF Redemption events
to detect API undercounting. Reports a gap ratio, acquisition-rail breakdown, and
sets downstream_lower_bound when the gap exceeds a threshold. Hard wall identical
to sibling surfaces — never feeds verdict / gate / primary_edge_type /
copyability_note / data_quality_flags.

RED until `compute_position_reconciliation_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.position_reconciliation_surface`.

Excluded from Phase 1 (live in later tasks):
- Test 12b: None survives replay round-trip (Task 2)
- Test 14:  scope-guard field-set updates (Task 7)
- Test 15:  calibration v4 (Task 8)
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

import pytest

from sniper.analysis.wallet_protocol.position_reconciliation import (
    ACQUISITION_GAP_NOTABLE_THRESHOLD,
    SURFACE_VERSION,
    UNDERCOUNT_PROXY_ONLY_THRESHOLD,
    compute_position_reconciliation_surface,
)
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


# ---------------------------------------------------------------------------
# Helpers / builders
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _closed(condition_id: str, **kw) -> dict:
    """A minimal closed-position row."""
    row: dict = {"conditionId": condition_id}
    row.update(kw)
    return row


def _red(condition_id: str, **kw) -> dict:
    """A CTF Redemption event row. Spec §5.1 uses field 'condition', not 'conditionId'."""
    row: dict = {"condition": condition_id}
    row.update(kw)
    return row


def _split(condition_id: str, **kw) -> dict:
    """A CTF Split event row (same 'condition' field as Redemption)."""
    row: dict = {"condition": condition_id}
    row.update(kw)
    return row


def _merge(condition_id: str, **kw) -> dict:
    """A CTF Merge event row (same 'condition' field as Redemption)."""
    row: dict = {"condition": condition_id}
    row.update(kw)
    return row


def _act(asset: str, condition_id: str, **kw) -> dict:
    """An activity_rows TRADE row — the orderbook-rail source per §5.1."""
    row: dict = {"type": "TRADE", "asset": asset, "conditionId": condition_id}
    row.update(kw)
    return row


def _data(
    closed_positions=None,
    redemptions=None,
    splits=None,
    merges=None,
    activity_rows=None,
    activity_cap_hit: bool = False,
    n_unmapped_fills: int = 0,
) -> WalletData:
    return WalletData(
        wallet="0xtest",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=activity_cap_hit,
        n_unmapped_fills=n_unmapped_fills,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        redemptions=redemptions,  # None means fetch failure; [] means empty
        splits=splits,
        merges=merges,
        replay_surface_meta={
            "archetype_layer1": {"status": "skipped"},
            "execution_profile": {"status": "skipped"},
            "price_history_lookups": {"status": "skipped"},
            "leafs_by_cid": {"status": "skipped"},
            "nav_lookup": {"status": "skipped"},
            "peer_baselines": {"status": "skipped"},
        },
    )


def _signature(report: ProtocolReport) -> tuple:
    """Everything that must not change when position_reconciliation_surface is stubbed."""
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
        tuple(report.data_quality_flags),
        json.dumps(report.capital_risk_surface, sort_keys=True, default=str),
        json.dumps(report.sizing_outcome_surface, sort_keys=True, default=str),
        json.dumps(report.null_baseline_surface, sort_keys=True, default=str),
        json.dumps(report.copy_latency_surface, sort_keys=True, default=str),
        json.dumps(report.survivorship_surface, sort_keys=True, default=str),
        json.dumps(report.timing_surface, sort_keys=True, default=str),
        json.dumps(report.liquidity_surface, sort_keys=True, default=str),
        json.dumps(report.position_building_surface, sort_keys=True, default=str),
    )


# ---------------------------------------------------------------------------
# Test 1 — Clean wallet (no gap)
# §10.1: closed_positions and redemptions conditionId sets identical
# ---------------------------------------------------------------------------
def test_clean_wallet_no_gap():
    """#1 — identical closed + redeemed sets → ratio 0.0, ok, no lower-bound."""
    cids = [f"0x{i:064x}" for i in range(20)]
    closed = [_closed(c) for c in cids]
    reds = [_red(c) for c in cids]
    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["undercount_ratio"] == 0.0
    assert s["data_state"] == "ok"
    assert s["downstream_lower_bound"] is False
    assert s["surface_version"] == SURFACE_VERSION


# ---------------------------------------------------------------------------
# Test 1b — Row vs condition count
# §10.1b: 100 rows reducing to 80 unique conditions
# ---------------------------------------------------------------------------
def test_row_vs_condition_count():
    """#1b — duplicate conditionId rows: closed_positions_count != closed_condition_count."""
    # 80 unique conditions, 20 appear twice → 100 rows total
    unique_cids = [f"0x{i:064x}" for i in range(80)]
    dupe_rows = [_closed(unique_cids[i % 20]) for i in range(20)]   # 20 dupes
    all_closed = [_closed(c) for c in unique_cids] + dupe_rows      # 100 rows

    # all 80 unique conditions are also redeemed
    reds = [_red(c) for c in unique_cids]

    s = compute_position_reconciliation_surface(
        closed_positions=all_closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["closed_positions_count"] == 100
    assert s["closed_condition_count"] == 80
    # set arithmetic uses 80, not 100
    assert s["condition_set_summary"]["both"] == 80
    assert s["condition_set_summary"]["closed_only"] == 0
    assert s["condition_set_summary"]["redeemed_only"] == 0


# ---------------------------------------------------------------------------
# Test 2 — Hidden-redemption wallet (0x20ad75 shape)
# §10.2: 10608 rows / 10580 unique closed + 12258 unique redeemed / 1678 gap
# ---------------------------------------------------------------------------
def test_hidden_redemption_wallet_0x20ad75():
    """#2 — synthesised 0x20ad75 inputs: undercount_ratio ≈ 0.137, proxy_only."""
    # 10,580 unique conditionIds for closed_positions
    base_cids = [f"0x{i:064x}" for i in range(10_580)]
    # 10,608 rows: 10,580 unique + 28 extra rows on first 28 conditions
    extra_rows = [_closed(base_cids[i]) for i in range(28)]
    closed = [_closed(c) for c in base_cids] + extra_rows   # 10,608 total

    # 12,258 unique redeemed conditions:
    #   10,580 that overlap with closed + 1,678 that don't
    redeemed_extra = [f"0x{i:064x}" for i in range(10_580, 12_258)]
    all_redeemed_cids = base_cids + redeemed_extra
    reds = [_red(c) for c in all_redeemed_cids]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["closed_positions_count"] == 10_608
    assert s["closed_condition_count"] == 10_580
    assert s["redeemed_condition_count"] == 12_258
    assert s["redeemed_not_in_closed_count"] == 1_678
    assert s["undercount_ratio"] == pytest.approx(0.137, abs=0.001)
    assert s["data_state"] == "proxy_only"
    assert s["downstream_lower_bound"] is True
    assert any("CLOSED_POSITIONS_UNDERCOUNT" in c for c in s["caveats"])


# ---------------------------------------------------------------------------
# Test 3 — Untraced-acquisition caveat
# §10.3: acquisition_rail_gap_share > ACQUISITION_GAP_NOTABLE_THRESHOLD
# ---------------------------------------------------------------------------
def test_untraced_acquisition_caveat():
    """#3 — when acquisition_rail_gap_share > 0.10, UNTRACED_ACQUISITION_RAIL caveat fires."""
    # 10 closed, 15 redeemed (5 gap), no orderbook/split/merge trace for gap
    closed_cids = [f"0xa{i:04x}" for i in range(10)]
    gap_cids = [f"0xb{i:04x}" for i in range(5)]
    all_redeemed_cids = closed_cids + gap_cids  # 15 unique
    closed = [_closed(c) for c in closed_cids]
    reds = [_red(c) for c in all_redeemed_cids]

    # acquisition_rail_gap_share = 5 / 15 ≈ 0.333 > 0.10
    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],   # no orderbook traces
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["acquisition_rail_gap_share"] > ACQUISITION_GAP_NOTABLE_THRESHOLD
    assert any("UNTRACED_ACQUISITION_RAIL" in c for c in s["caveats"])


# ---------------------------------------------------------------------------
# Test 4 — Rails observed
# §10.4: 50 orderbook conditions + 60 redemptions (50 shared + 10 not in orderbook)
# ---------------------------------------------------------------------------
def test_rails_observed():
    """#4 — rails_observed counts per-rail distinct conditions."""
    shared_cids = [f"0xc{i:04x}" for i in range(50)]
    redemption_only_cids = [f"0xd{i:04x}" for i in range(10)]

    closed = [_closed(c) for c in shared_cids]
    reds = [_red(c) for c in shared_cids + redemption_only_cids]
    # orderbook: all 50 shared conditions
    acts = [_act(f"tok-{i}", c) for i, c in enumerate(shared_cids)]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=acts,
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["rails_observed"] == {
        "orderbook": 50,
        "redemption": 60,
        "split": 0,
        "merge": 0,
    }


# ---------------------------------------------------------------------------
# Test 4b — Orderbook rail incompleteness caveat
# §10.4b + §5.3: activity_cap_hit=True + n_unmapped_fills=7
# ---------------------------------------------------------------------------
def test_orderbook_rail_incomplete_caveat():
    """#4b — activity_cap_hit=True, n_unmapped_fills=7 → caveat ORDERBOOK_RAIL_INCOMPLETE_n=7."""
    cids = [f"0xe{i:04x}" for i in range(5)]
    closed = [_closed(c) for c in cids]
    reds = [_red(c) for c in cids]
    acts = [_act(f"tok-{i}", c) for i, c in enumerate(cids)]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=acts,
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=True,
        n_unmapped_fills=7,
    )
    matching = [c for c in s["caveats"] if "ORDERBOOK_RAIL_INCOMPLETE_n=7" in c]
    assert len(matching) == 1, f"Expected exactly one ORDERBOOK_RAIL_INCOMPLETE_n=7 caveat, got: {s['caveats']}"
    # rails_observed["orderbook"] is NOT inflated by the unmapped count
    assert s["rails_observed"]["orderbook"] == len(cids)


# ---------------------------------------------------------------------------
# Test 5 — condition_set_summary exact set arithmetic
# ---------------------------------------------------------------------------
def test_condition_set_summary_exact():
    """#5 — set arithmetic on a tiny constructed case is exact."""
    closed_only_cids = [f"0xf{i:04x}" for i in range(3)]
    both_cids = [f"0x1{i:04x}" for i in range(4)]
    redeemed_only_cids = [f"0x2{i:04x}" for i in range(2)]

    closed = [_closed(c) for c in closed_only_cids + both_cids]
    reds = [_red(c) for c in both_cids + redeemed_only_cids]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["condition_set_summary"] == {
        "closed_only": 3,
        "redeemed_only": 2,
        "both": 4,
    }


# ---------------------------------------------------------------------------
# Test 6 — data_unavailable from fetch failure
# §10.6: redemptions=None (capture failure)
# ---------------------------------------------------------------------------
def test_data_unavailable_fetch_failure():
    """#6 — redemptions=None → data_unavailable + RECONCILIATION_INPUTS_MISSING caveat."""
    closed = [_closed("0xaaa")]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=None,   # fetch failed — distinct from empty list
        splits=None,
        merges=None,
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["data_state"] == "data_unavailable"
    assert any("RECONCILIATION_INPUTS_MISSING" in c for c in s["caveats"])
    assert not any("NO_CLOSED_OR_REDEEMED_CONDITIONS" in c for c in s["caveats"])
    # count fields must be None (unknown), not 0 (measured zero)
    assert s["closed_positions_count"] is None
    assert s["redeemed_condition_count"] is None


# ---------------------------------------------------------------------------
# Test 7 — data_unavailable from empty wallet
# §10.7: redemptions=[] AND closed_positions=[]
# ---------------------------------------------------------------------------
def test_data_unavailable_empty_wallet():
    """#7 — empty wallet (nothing to reconcile) → data_unavailable + NO_CLOSED_OR_REDEEMED_CONDITIONS."""
    s = compute_position_reconciliation_surface(
        closed_positions=[],
        activity_rows=[],
        redemptions=[],   # fetch succeeded, wallet simply has none
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["data_state"] == "data_unavailable"
    assert any("NO_CLOSED_OR_REDEEMED_CONDITIONS" in c for c in s["caveats"])
    assert not any("RECONCILIATION_INPUTS_MISSING" in c for c in s["caveats"])
    # count fields are 0 (we measured zero), not None (couldn't measure)
    assert s["closed_positions_count"] == 0
    assert s["redeemed_condition_count"] == 0


# ---------------------------------------------------------------------------
# Test 8 — No orderbook trace for gap condition
# §10.8: redeemed but not closed + no activity fill → untraced_redemption_count
# ---------------------------------------------------------------------------
def test_no_orderbook_trace_goes_to_untraced():
    """#8 — gap condition with no orderbook/split/merge trace → untraced_redemption_count."""
    closed_cid = "0x" + "a" * 64
    gap_cid = "0x" + "b" * 64

    closed = [_closed(closed_cid)]
    reds = [_red(closed_cid), _red(gap_cid)]
    # no activity rows → gap_cid has no orderbook trace
    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["redeemed_orderbook_traced_count"] == 0
    assert s["untraced_redemption_count"] == 1


# ---------------------------------------------------------------------------
# Test 9 — Activity row without conditionId is silently excluded
# §10.9: TRADE row with empty/missing conditionId → no caveat, no crash
# ---------------------------------------------------------------------------
def test_activity_row_without_conditionid_excluded():
    """#9 — TRADE row with missing conditionId is excluded from orderbook_cond; no crash."""
    cid = "0x" + "c" * 64
    closed = [_closed(cid)]
    reds = [_red(cid)]
    acts = [
        _act("tok-1", cid),                     # valid row
        {"type": "TRADE", "asset": "tok-2"},     # no conditionId — excluded
        {"type": "TRADE", "asset": "tok-3", "conditionId": ""},  # empty — excluded
    ]
    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=acts,
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    # no crash, and orderbook_cond contains exactly cid (from the one valid row)
    assert s["rails_observed"]["orderbook"] == 1
    assert s["data_state"] in {"ok", "proxy_only", "data_unavailable"}


# ---------------------------------------------------------------------------
# Test 10 — Hard wall: mutation-proof
# §10.10 + §7: garbage-stub producer must not change verdict/gate/other surfaces
# ---------------------------------------------------------------------------
def test_hard_wall_mutation_proof(monkeypatch):
    """#10 — garbage-stub compute_position_reconciliation_surface leaves all
    verdict/gate/other-surface fields byte-identical."""
    cids = [f"0xd{i:04x}" for i in range(20)]
    data = _data(
        closed_positions=[_closed(c) for c in cids],
        redemptions=[_red(c) for c in cids],
        splits=[],
        merges=[],
    )
    reference = analyze_wallet("0xtest", pre_fetched_data=data, n_iter=100, seed=1)

    import sniper.analysis.wallet_protocol.position_reconciliation as prs_mod

    monkeypatch.setattr(
        prs_mod,
        "compute_position_reconciliation_surface",
        lambda *a, **k: {
            "closed_positions_count": -1,
            "downstream_lower_bound": True,   # sentinel — wrong value
            "GARBAGE_SENTINEL": 9.99e9,
        },
    )
    poisoned = analyze_wallet("0xtest", pre_fetched_data=data, n_iter=100, seed=1)

    assert poisoned.position_reconciliation_surface.get("GARBAGE_SENTINEL") == 9.99e9
    assert _signature(poisoned) == _signature(reference)
    assert "GARBAGE_SENTINEL" not in str(poisoned.data_quality_flags)


# ---------------------------------------------------------------------------
# Test 11 — Hard wall: grep guard
# §10.11 + §7: no producer reads position_reconciliation_surface
# ---------------------------------------------------------------------------
def test_hard_wall_grep_guard():
    """#11 — position_reconciliation_surface appears ONLY in its allowed files."""
    allowed = {
        "sniper/analysis/wallet_protocol/types.py",
        "sniper/analysis/wallet_protocol/analyze_wallet.py",
        "sniper/analysis/wallet_protocol/report.py",
        "sniper/analysis/wallet_protocol/position_reconciliation.py",
        "sniper/analysis/wallet_protocol/fetch.py",
        # Wallet dossier §6.8 surfaces table mirrors all ProtocolReport surfaces
        # by name (allowlisted in builder._SURFACE_FIELDS). Display-only mirror;
        # never mutates the source surface. See PR #57 commits 5ffd9f5 / 51d9e6c.
        "sniper/analysis/wallet_dossier/builder.py",
    }
    result = subprocess.run(
        ["grep", "-rn", "position_reconciliation_surface", "sniper/"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    hits = result.stdout.strip().splitlines()
    assert hits, (
        "position_reconciliation_surface not found anywhere in sniper/ — impl is missing"
    )
    unexpected = []
    for line in hits:
        # line format: "path/to/file.py:N:..."
        path = line.split(":")[0]
        if path not in allowed:
            unexpected.append(path)
    assert not unexpected, (
        f"position_reconciliation_surface referenced outside allowed files: {unexpected}"
    )


# ---------------------------------------------------------------------------
# Test 12 — Replay determinism
# §10.12: same input → byte-identical surface JSON
# ---------------------------------------------------------------------------
def test_replay_determinism():
    """#12 — same input → byte-identical surface JSON."""
    cids = [f"0xe{i:04x}" for i in range(10)]
    gap_cids = [f"0xf{i:04x}" for i in range(3)]
    closed = [_closed(c) for c in cids]
    reds = [_red(c) for c in cids + gap_cids]

    a = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    b = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# Test 13 — Backward compat: ProtocolReport without the field → None
# §10.13
# ---------------------------------------------------------------------------
def test_backward_compat_field_defaults_none():
    """#13 — ProtocolReport constructed without field → None, no crash."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.position_reconciliation_surface is None


# ---------------------------------------------------------------------------
# Test 16 — Note rendering
# §10.16: downstream_lower_bound True/False → note content
# ---------------------------------------------------------------------------
def test_note_rendering_lower_bound_true():
    """#16a — when downstream_lower_bound=True, note contains 'should be read as lower bounds'."""
    # Enough gap to trigger proxy_only (> 5% of redeemed)
    closed_cids = [f"0xaa{i:04x}" for i in range(10)]
    gap_cids = [f"0xbb{i:04x}" for i in range(2)]   # 2/12 ≈ 0.167 > 0.05
    closed = [_closed(c) for c in closed_cids]
    reds = [_red(c) for c in closed_cids + gap_cids]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["downstream_lower_bound"] is True
    assert "should be read as lower bounds" in s["note"]


def test_note_rendering_lower_bound_false():
    """#16b — when downstream_lower_bound=False, note does NOT contain 'should be read as lower bounds'."""
    cids = [f"0xcc{i:04x}" for i in range(10)]
    closed = [_closed(c) for c in cids]
    reds = [_red(c) for c in cids]

    s = compute_position_reconciliation_surface(
        closed_positions=closed,
        activity_rows=[],
        redemptions=reds,
        splits=[],
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["downstream_lower_bound"] is False
    assert "should be read as lower bounds" not in s["note"]


# ---------------------------------------------------------------------------
# Test 17 — No mutation of other surfaces (paired-run)
# §10.17: only position_reconciliation_surface differs between runs
# ---------------------------------------------------------------------------
def test_no_mutation_of_other_surfaces():
    """#17 — only position_reconciliation_surface differs between empty-vs-populated redemptions runs."""
    cids = [f"0xdd{i:04x}" for i in range(20)]
    extra_cids = [f"0xee{i:04x}" for i in range(5)]   # redeemed but not in closed

    closed = [_closed(c) for c in cids]

    data_empty = _data(
        closed_positions=closed,
        redemptions=[],
        splits=[],
        merges=[],
    )
    data_populated = _data(
        closed_positions=closed,
        redemptions=[_red(c) for c in extra_cids],   # conditions not in closed_positions
        splits=[],
        merges=[],
    )

    report_empty = analyze_wallet("0xtest", pre_fetched_data=data_empty, n_iter=100, seed=42)
    report_populated = analyze_wallet("0xtest", pre_fetched_data=data_populated, n_iter=100, seed=42)

    # position_reconciliation_surface must differ
    assert (
        json.dumps(report_empty.position_reconciliation_surface, sort_keys=True, default=str)
        != json.dumps(report_populated.position_reconciliation_surface, sort_keys=True, default=str)
    ), "Expected position_reconciliation_surface to differ between runs"

    # every other surface must be identical
    def _other_surfaces(r: ProtocolReport) -> dict:
        return {
            "capital_risk_surface": json.dumps(r.capital_risk_surface, sort_keys=True, default=str),
            "sizing_outcome_surface": json.dumps(r.sizing_outcome_surface, sort_keys=True, default=str),
            "null_baseline_surface": json.dumps(r.null_baseline_surface, sort_keys=True, default=str),
            "copy_latency_surface": json.dumps(r.copy_latency_surface, sort_keys=True, default=str),
            "survivorship_surface": json.dumps(r.survivorship_surface, sort_keys=True, default=str),
            "timing_surface": json.dumps(r.timing_surface, sort_keys=True, default=str),
            "liquidity_surface": json.dumps(r.liquidity_surface, sort_keys=True, default=str),
            "position_building_surface": json.dumps(r.position_building_surface, sort_keys=True, default=str),
            "overall_verdict": r.overall_verdict,
            "gate_result": r.gate_result,
            "primary_edge_type": r.primary_edge_type,
            "copyability_note": r.copyability_note,
            "data_quality_flags": tuple(r.data_quality_flags),
        }

    assert _other_surfaces(report_empty) == _other_surfaces(report_populated), (
        "Other surfaces changed between empty/populated redemptions runs — hard wall violated"
    )


# ---------------------------------------------------------------------------
# Test 6b — Partial aux-stream failure (splits or merges is None)
# Spec §10 test 6b: operator BLOCK finding (commit 4364d81).
# splits/merges=None must not produce measured-zero rail claim or
# UNTRACED_ACQUISITION_RAIL. acquisition_rail_gap_share must be None, and
# untraced_redemption_count must be described as an upper bound: a missing rail
# could later trace the gap condition and lower the true untraced count.
# ---------------------------------------------------------------------------
def test_partial_aux_stream_failure_splits_none():
    """#6b-splits — splits=None: rail count unknown, not 0; no UNTRACED_ACQUISITION_RAIL."""
    s = compute_position_reconciliation_surface(
        closed_positions=[{"conditionId": "0xa"}],
        activity_rows=[],
        redemptions=[{"condition": "0xa"}, {"condition": "0xb"}],
        splits=None,
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["data_state"] == "proxy_only", (
        f"Expected proxy_only (undercount stands), got {s['data_state']}"
    )
    assert s["rails_observed"]["split"] is None, (
        "split rail must be None (unknown), not measured-zero"
    )
    assert s["rails_observed"]["merge"] == 0
    assert s["acquisition_rail_gap_share"] is None, (
        "acquisition_rail_gap_share must be None when an aux rail is unknown"
    )
    assert s["untraced_redemption_count"] == 1
    assert "untraced-acquisition figures are an upper bound" in s["note"]
    assert "untraced-acquisition figures are a lower bound" not in s["note"]
    assert "ACQUISITION_RAIL_FETCH_PARTIAL_splits" in s["caveats"], (
        f"Expected ACQUISITION_RAIL_FETCH_PARTIAL_splits caveat; got {s['caveats']}"
    )
    assert "UNTRACED_ACQUISITION_RAIL" not in s["caveats"], (
        "UNTRACED_ACQUISITION_RAIL must be suppressed when an acquisition rail is unknown"
    )


def test_partial_aux_stream_failure_merges_none():
    """#6b-merges — merges=None: symmetric to splits=None case."""
    s = compute_position_reconciliation_surface(
        closed_positions=[{"conditionId": "0xa"}],
        activity_rows=[],
        redemptions=[{"condition": "0xa"}, {"condition": "0xb"}],
        splits=[],
        merges=None,
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["data_state"] == "proxy_only"
    assert s["rails_observed"]["merge"] is None, (
        "merge rail must be None (unknown), not measured-zero"
    )
    assert s["rails_observed"]["split"] == 0
    assert s["acquisition_rail_gap_share"] is None
    assert s["untraced_redemption_count"] == 1
    assert "untraced-acquisition figures are an upper bound" in s["note"]
    assert "ACQUISITION_RAIL_FETCH_PARTIAL_merges" in s["caveats"]
    assert "UNTRACED_ACQUISITION_RAIL" not in s["caveats"]


def test_partial_aux_stream_failure_both_none():
    """#6b-both — splits=None AND merges=None: both rail counts unknown."""
    s = compute_position_reconciliation_surface(
        closed_positions=[{"conditionId": "0xa"}],
        activity_rows=[],
        redemptions=[{"condition": "0xa"}, {"condition": "0xb"}],
        splits=None,
        merges=None,
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    assert s["data_state"] == "proxy_only"
    assert s["rails_observed"]["split"] is None
    assert s["rails_observed"]["merge"] is None
    assert s["acquisition_rail_gap_share"] is None
    assert s["untraced_redemption_count"] == 1
    assert "untraced-acquisition figures are an upper bound" in s["note"]
    assert "ACQUISITION_RAIL_FETCH_PARTIAL_splits" in s["caveats"]
    assert "ACQUISITION_RAIL_FETCH_PARTIAL_merges" in s["caveats"]
    assert "UNTRACED_ACQUISITION_RAIL" not in s["caveats"]


def test_partial_aux_stream_failure_render_does_not_claim_all_sources_read():
    """Renderer must not say 'no acquisition rail observed in any source' when
    an aux stream is unknown."""
    surface = compute_position_reconciliation_surface(
        closed_positions=[{"conditionId": "0xa"}],
        activity_rows=[],
        redemptions=[{"condition": "0xa"}, {"condition": "0xb"}],
        splits=None,
        merges=[],
        activity_cap_hit=False,
        n_unmapped_fills=0,
    )
    report = ProtocolReport(
        wallet="0xtest",
        position_reconciliation_surface=surface,
    )

    md = render_report(report)

    assert "upper bound; auxiliary acquisition rail unavailable" in md
    assert "no acquisition rail observed in any source" not in md
