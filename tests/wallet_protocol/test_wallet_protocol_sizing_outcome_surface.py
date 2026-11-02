"""Acceptance tests — Sizing & Outcome surface (`sizing_outcome_surface`).

Spec: docs/superpowers/specs/2026-05-22-sizing-outcome-surface-design.md

Display-only surface: does a wallet's entry sizing associate with outcome
quality? Stratified Spearman rho (market-mix control), leave-one-out
robustness (one-big-winner control), entry-vs-total cost (late conviction).
Hard wall identical to capital_risk_surface — never feeds verdict / gate /
primary_edge_type / copyability_note / data_quality_flags.

RED until `compute_sizing_outcome_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.sizing_outcome_surface`.
"""
from __future__ import annotations

import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.sizing_outcome import (
    compute_sizing_outcome_surface,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# builders — a datapoint is a matched (BUY activity row, closed position)
# --------------------------------------------------------------------------
def _buy(cid: str, ts: int, *, asset: str, outcome: str = "Yes",
         usdc: float, price: float = 0.5, size: float | None = None) -> dict:
    row = {"type": "TRADE", "side": "BUY", "conditionId": cid, "asset": asset,
           "outcome": outcome, "price": price, "timestamp": ts,
           "usdcSize": usdc}
    if size is not None:
        row["size"] = size
    return row


def _pos(cid: str, realized_pnl: float, *, asset: str, outcome: str = "Yes",
         event: str | None = None, ts: int = 1_700_000_000) -> dict:
    row = {"conditionId": cid, "asset": asset, "outcome": outcome,
           "realizedPnl": realized_pnl, "timestamp": ts,
           "avgPrice": 0.5, "curPrice": 1.0}
    if event is not None:
        row["eventSlug"] = event
    return row


def _dp(cid: str, entry: float, roi: float, i: int, *,
        leaf: str | list[str] | None = None, event: str | None = None,
        asset: str | None = None, outcome: str = "Yes",
        extra_buys: list[float] | None = None):
    """One datapoint: a single BUY of `entry`, a closed position whose
    realizedPnl yields the requested ROI on total cost. `extra_buys` are
    post-entry adds (late conviction) — they raise total cost, not entry."""
    asset = asset or f"tok-{cid}"
    ts = 1_700_000_000 + i
    buys = [_buy(cid, ts, asset=asset, outcome=outcome, usdc=entry)]
    total = entry
    for j, add in enumerate(extra_buys or []):
        buys.append(_buy(cid, ts + 100 + j, asset=asset, outcome=outcome,
                         usdc=add))
        total += add
    pos = _pos(cid, roi * total, asset=asset, outcome=outcome, event=event,
               ts=ts)
    leafs = {}
    if leaf is not None:
        leafs[cid] = list(leaf) if isinstance(leaf, list) else [leaf]
    return buys, pos, leafs


def _assemble(datapoints, *, nav=None):
    """datapoints: list of (buys, pos, leafs) tuples from _dp."""
    closed, activity, leafs_by_cid = [], [], {}
    for buys, pos, leafs in datapoints:
        activity.extend(buys)
        closed.append(pos)
        leafs_by_cid.update(leafs)
    return closed, activity, leafs_by_cid, dict(nav or {})


def _data(*, closed_positions=None, activity_rows=None, leafs_by_cid=None,
          nav_lookup=None) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
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
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
        tuple(report.data_quality_flags),
    )


# 25 baseline datapoints: entry sizes ascending, ROI rank-uncorrelated with
# size (alternating sign) — global rho ~ 0 before any confound is injected.
def _uncorrelated(n: int = 25, *, leaf: str = "politics", event_each=False):
    pts = []
    for i in range(n):
        roi = 0.20 if i % 2 == 0 else -0.20
        pts.append(_dp(f"c{i}", entry=10.0 + i, roi=roi, i=i, leaf=leaf,
                       event=(f"e{i}" if event_each else None)))
    return pts


def _paired_base(n_levels: int = 10, *, leaf: str = "politics"):
    """Datapoints whose entry↔ROI Spearman rho is exactly 0: each entry
    level appears twice — once +ROI, once −ROI — so every entry-rank tie is
    roi-balanced and contributes nothing to the covariance."""
    pts = []
    for k in range(n_levels):
        e = 10.0 + k
        pts.append(_dp(f"p{k}a", entry=e, roi=0.20, i=2 * k, leaf=leaf))
        pts.append(_dp(f"p{k}b", entry=e, roi=-0.20, i=2 * k + 1, leaf=leaf))
    return pts


# ==========================================================================
# Spec §9 — confound-control acceptance tests
# ==========================================================================
def test_one_big_winner_fails_leave_one_out():
    """#1 — a single huge-entry winner lifts global rho above the band, but
    it fails leave-one-out (the exact-zero base reappears) → robustness_holds
    False, association inconclusive."""
    pts = _paired_base(10)  # 20 datapoints, exact-zero base rho
    pts.append(_dp("big", entry=5000.0, roi=8.0, i=99, leaf="politics"))
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["global_rho"] > 0.10
    assert s["robustness_holds"] is False
    assert s["association"] == "inconclusive"


def test_category_mix_neutralized_by_stratified_rho():
    """#2 — category A = big entries + high ROI, B = small + low ROI, size
    uncorrelated WITHIN each. Global rho strongly positive (Simpson mix);
    stratified rho ~ 0 — the honest headline neutralizes the mix."""
    pts = []
    for i in range(14):  # category A: big entries, high ROI, no within-corr
        pts.append(_dp(f"a{i}", entry=900.0 + (i % 3), roi=0.50,
                       i=i, leaf="sports"))
    for i in range(14):  # category B: small entries, low ROI
        pts.append(_dp(f"b{i}", entry=10.0 + (i % 3), roi=-0.30,
                       i=100 + i, leaf="politics"))
    closed, activity, leafs, nav = _assemble(
        pts, nav={"sports": "sports", "politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["global_rho"] > 0.50            # mix-driven false signal
    assert abs(s["stratified_rho"]) < 0.20   # neutralized within strata
    assert s["association"] == "no_association"   # headline = stratified
    assert any("GLOBAL_STRATIFIED_DIVERGENCE" in c for c in s["caveats"])


def test_late_conviction_gap_surfaces():
    """#3 — flat entry sizes, but winners get post-entry adds → total cost
    grows. late_conviction_ratio > 1, added_after_entry_usdc > 0; the
    correlation (on entry size) is not corrupted by the adds."""
    pts = []
    for i in range(25):
        adds = [50.0, 50.0] if i % 2 == 0 else None  # adds on half
        pts.append(_dp(f"c{i}", entry=100.0, roi=0.10, i=i, leaf="politics",
                       extra_buys=adds))
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["late_conviction_ratio"] > 1.0
    assert s["added_after_entry_usdc"] > 0.0


def test_missing_tags_degrades_to_proxy_only():
    """#4 — no leafs_by_cid → cannot stratify → proxy_only + caveat,
    stratified_rho is None."""
    pts = _uncorrelated(25, leaf="politics")
    closed, activity, _leafs, _nav = _assemble(pts)
    s = compute_sizing_outcome_surface(closed, activity, {}, {})
    assert s["data_state"] == "proxy_only"
    assert s["stratified_rho"] is None
    assert s["caveats"]


def test_zero_and_negative_entry_price_dropped():
    """#5 — positions whose entry size resolves to <= 0 are dropped (counted
    in n_positions_dropped), no crash, no divide error."""
    pts = _uncorrelated(22, leaf="politics")
    bad = _dp("zero", entry=0.0, roi=0.0, i=200, leaf="politics")
    closed, activity, leafs, nav = _assemble(
        pts + [bad], nav={"politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["n_positions_dropped"] >= 1
    assert s["n_datapoints"] == 22


def test_hard_wall_surface_never_mutates_verdict(monkeypatch):
    """#6 — a garbage-stub producer leaves verdict / gate / primary_edge_type
    / copyability_note / data_quality_flags byte-identical."""
    pts = _uncorrelated(25)
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    data = _data(closed_positions=closed, activity_rows=activity,
                 leafs_by_cid=leafs, nav_lookup=nav)
    baseline = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)

    import sniper.analysis.wallet_protocol.sizing_outcome as so

    monkeypatch.setattr(
        so, "compute_sizing_outcome_surface",
        lambda *a, **k: {"data_state": "ok", "GARBAGE": 9.99e9},
    )
    poisoned = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)
    assert poisoned.sizing_outcome_surface.get("GARBAGE") == 9.99e9
    assert _signature(poisoned) == _signature(baseline)
    assert "GARBAGE" not in str(poisoned.data_quality_flags)


def test_yes_no_asymmetry_separate_entry_sizes():
    """#7 — same conditionId, different asset (YES vs NO) → two separate
    datapoints with their own entry sizes; never merged."""
    pts = _uncorrelated(23, leaf="politics")
    yes = _dp("mkt", entry=100.0, roi=0.10, i=300, leaf="politics",
              asset="tok-yes", outcome="Yes")
    no = _dp("mkt", entry=900.0, roi=-0.10, i=301, leaf="politics",
             asset="tok-no", outcome="No")
    closed, activity, leafs, nav = _assemble(
        pts + [yes, no], nav={"politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["n_datapoints"] == 25  # 23 + YES + NO, both counted separately
    entries = sorted(b["mean_entry_size_usdc"] for b in s["size_buckets"])
    assert entries[0] != entries[-1]  # the 100 and 900 land in different buckets


def test_category_ambiguity_deterministic_plus_caveat():
    """#8 — one conditionId whose leafs map to two distinct nav categories
    → deterministic sorted-first category choice + CATEGORY_AMBIGUOUS."""
    pts = _uncorrelated(24, leaf="politics")
    # 'ambig' has leafs ['zeta','alpha'] → nav: zeta→sports, alpha→politics.
    ambig = _dp("ambig", entry=100.0, roi=0.10, i=400,
                leaf=["zeta", "alpha"])
    closed, activity, leafs, nav = _assemble(
        pts + [ambig],
        nav={"politics": "politics", "alpha": "politics", "zeta": "sports"})
    s1 = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    s2 = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)
    assert any("CATEGORY_AMBIGUOUS" in c for c in s1["caveats"])


# ==========================================================================
# Spec §9 — wiring / determinism / compat
# ==========================================================================
def test_field_present_on_live_path():
    """#9 — sizing_outcome_surface is a dict on the analyze_wallet path."""
    pts = _uncorrelated(25)
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    data = _data(closed_positions=closed, activity_rows=activity,
                 leafs_by_cid=leafs, nav_lookup=nav)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                            seed=1)
    assert isinstance(report.sizing_outcome_surface, dict)
    assert "data_state" in report.sizing_outcome_surface


def test_replay_determinism():
    """#10 — same input → byte-identical surface JSON across two runs,
    including the seeded permutation p-value."""
    pts = _uncorrelated(25)
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    a = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    b = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_backward_compat_protocolreport_without_field():
    """#11 — a ProtocolReport built without the field defaults to None."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.sizing_outcome_surface is None


# ==========================================================================
# Basic producer coverage
# ==========================================================================
def test_data_unavailable_with_no_closed_positions():
    s = compute_sizing_outcome_surface([], [], {}, {})
    assert s["data_state"] == "data_unavailable"


def test_underpowered_below_datapoint_floor():
    pts = _uncorrelated(10, leaf="politics")
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["data_state"] == "underpowered"
    assert s["association"] == "inconclusive"


def test_clean_positive_association_is_ok():
    """Monotone size→ROI within each of two balanced categories, distinct
    events, broad, robust → ok + larger_entries_better."""
    pts = []
    for i in range(15):
        pts.append(_dp(f"a{i}", entry=10.0 + i * 5, roi=0.01 * i, i=i,
                       leaf="politics", event=f"ea{i}"))
    for i in range(15):
        pts.append(_dp(f"b{i}", entry=12.0 + i * 5, roi=0.01 * i, i=100 + i,
                       leaf="sports", event=f"eb{i}"))
    closed, activity, leafs, nav = _assemble(
        pts, nav={"politics": "politics", "sports": "sports"})
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["data_state"] == "ok"
    assert s["association"] == "larger_entries_better"
    assert s["robustness_holds"] is True


def test_join_fallback_does_not_mis_attribute_other_assets_buys():
    """Reviewer fix (#2): a closed position WITH an `asset` whose
    (cid,asset) group is empty must NOT absorb a same-(cid,outcome) BUY
    that belongs to a DIFFERENT asset — it is dropped, not mis-joined."""
    pts = _uncorrelated(22, leaf="politics")
    closed, activity, leafs, nav = _assemble(pts, nav={"politics": "politics"})
    # closed 'mkt' is asset tok-X; the only same-(cid,outcome) BUY is tok-Y.
    closed.append(_pos("mkt", -10.0, asset="tok-X", outcome="Yes"))
    activity.append(_buy("mkt", 500, asset="tok-Y", outcome="Yes", usdc=999.0))
    s = compute_sizing_outcome_surface(closed, activity, leafs, nav)
    assert s["n_datapoints"] == 22          # the mkt position is not joined
    assert s["n_positions_dropped"] >= 1
