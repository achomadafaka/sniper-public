"""Acceptance tests — Copy-Latency Decay surface (`copy_latency_surface`).

Spec: docs/superpowers/specs/2026-05-22-copy-latency-surface-design.md

Display-only surface: how much of a wallet's entry edge survives to a
copier entering 5/15/60 min late. Pure function of closed_positions +
activity_rows + copy_latency_prices (the fidelity=1 post-entry grid).
Hard wall identical to the sibling surfaces — never feeds verdict / gate
/ primary_edge_type / copyability_note / data_quality_flags.

RED until `compute_copy_latency_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.copy_latency_surface`.
"""
from __future__ import annotations

import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.copy_latency import (
    compute_copy_latency_surface,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData

_TS = 1_700_000_000


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# builders — a "covered" position is a closed position + its first BUY +
# price_history entries at entry and the post-entry grid (+5m/+15m/+60m).
# --------------------------------------------------------------------------
def _buy(cid: str, asset: str, ts: int) -> dict:
    return {"type": "TRADE", "side": "BUY", "conditionId": cid,
            "asset": asset, "timestamp": ts}


def _pos(cid: str, asset: str, avg_price: float, cur_price: float) -> dict:
    return {"conditionId": cid, "asset": asset, "avgPrice": avg_price,
            "curPrice": cur_price, "outcome": "Yes", "realizedPnl": 1.0}


def _covered(i: int, *, won: bool, p_entry: float, p5: float, p15: float,
             p60: float, with5=True, with15=True, with60=True,
             avg_price: float = 0.40):
    """One eligible position with its BUY and price-history grid entries."""
    cid, asset, ts = f"c{i}", f"tok-{i}", _TS + i * 100_000
    pos = _pos(cid, asset, avg_price, 1.0 if won else 0.0)
    buy = _buy(cid, asset, ts)
    ph: dict[str, float] = {f"{asset}|{ts}": p_entry}
    if with5:
        ph[f"{asset}|{ts + 300}"] = p5
    if with15:
        ph[f"{asset}|{ts + 900}"] = p15
    if with60:
        ph[f"{asset}|{ts + 3600}"] = p60
    return pos, buy, ph


def _many(n: int, *, won: bool, p_entry: float, p5: float, p15: float,
          p60: float, drop15_from: int | None = None):
    closed, activity, ph = [], [], {}
    for i in range(n):
        w15 = drop15_from is None or i < drop15_from
        pos, buy, entries = _covered(i, won=won, p_entry=p_entry, p5=p5,
                                     p15=p15, p60=p60, with15=w15)
        closed.append(pos)
        activity.append(buy)
        ph.update(entries)
    return closed, activity, ph


def _data(closed_positions, activity_rows, copy_latency_prices) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        copy_latency_prices=dict(copy_latency_prices or {}),
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


def _h(surface: dict, horizon_min: int) -> dict:
    return next(x for x in surface["horizons"]
               if x["horizon_min"] == horizon_min)


# ==========================================================================
# §10 — acceptance tests
# ==========================================================================
def test_edge_decays():
    """#1 — winners whose post-entry spot ran up sharply → positive decay,
    edge_retained < 0.5, copyability edge_decays."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.55,
                                 p15=0.70, p60=0.75)
    s = compute_copy_latency_surface(closed, activity, ph)
    h15 = _h(s, 15)
    assert h15["mean_decay"] > 0.0
    assert h15["edge_retained"] < 0.5
    assert s["copyability"] == "edge_decays"


def test_copyable():
    """#2 — winners whose post-entry spot barely moved → edge_retained
    >= 0.5, copyability copyable."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.405,
                                 p15=0.41, p60=0.42)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert _h(s, 15)["edge_retained"] >= 0.5
    assert s["copyability"] == "copyable"


def test_favorable_drift_negative_decay():
    """#3 — post-entry spot fell below entry → latency HELPED the copier →
    mean_decay < 0; no crash."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.35,
                                 p15=0.30, p60=0.28)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert _h(s, 15)["mean_decay"] < 0.0
    assert _h(s, 15)["mean_drift"] < 0.0


def test_losers_contribute_zero_decay():
    """#4 — lost positions → roi is -1 at entry and every horizon →
    decay == 0 regardless of price drift."""
    closed, activity, ph = _many(25, won=False, p_entry=0.40, p5=0.60,
                                 p15=0.70, p60=0.80)
    s = compute_copy_latency_surface(closed, activity, ph)
    for hm in (5, 15, 60):
        assert abs(_h(s, hm)["mean_decay"]) < 1e-9


def test_dormant_pregrid_fixture_is_data_unavailable():
    """#5 — price_history has entry keys but NO post-entry-grid keys (a
    pre-grid fixture) → data_unavailable, copyability inconclusive."""
    closed, activity, ph = [], [], {}
    for i in range(25):
        pos, buy, entries = _covered(i, won=True, p_entry=0.40, p5=0.0,
                                     p15=0.0, p60=0.0, with5=False,
                                     with15=False, with60=False)
        closed.append(pos); activity.append(buy); ph.update(entries)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert s["data_state"] == "data_unavailable"
    assert s["copyability"] == "inconclusive"


def test_partial_horizon_coverage_is_proxy_only():
    """#6 — +5m/+60m present for all, +15m for only 12 of 30 → +15m matched
    n in [MIN_PER_HORIZON, 0.5*n_eligible) → proxy_only + caveat."""
    closed, activity, ph = _many(30, won=True, p_entry=0.40, p5=0.45,
                                 p15=0.50, p60=0.55, drop15_from=12)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert s["data_state"] == "proxy_only"
    assert _h(s, 15)["n"] == 12
    assert s["caveats"]


def test_eligibility_excludes_unresolved_and_out_of_band():
    """#7 — an unresolved (curPrice mid) position and an out-of-band
    avgPrice position are excluded from n_eligible."""
    closed, activity, ph = _many(22, won=True, p_entry=0.40, p5=0.42,
                                 p15=0.44, p60=0.46)
    # unresolved
    pu, bu, eu = _covered(900, won=True, p_entry=0.4, p5=0.4, p15=0.4,
                          p60=0.4)
    pu["curPrice"] = 0.50
    # out-of-band avgPrice
    pb, bb, eb = _covered(901, won=True, p_entry=0.4, p5=0.4, p15=0.4,
                          p60=0.4, avg_price=0.995)
    closed += [pu, pb]; activity += [bu, bb]; ph.update(eu); ph.update(eb)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert s["n_eligible"] == 22
    assert s["n_total_closed"] == 24


def test_edge_retained_guard_on_nonpositive_entry_roi():
    """#8 — a horizon whose entry_ew_roi <= 0 (all losers) → edge_retained
    is None and copyability is not 'copyable'."""
    closed, activity, ph = _many(25, won=False, p_entry=0.40, p5=0.45,
                                 p15=0.50, p60=0.55)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert _h(s, 15)["entry_ew_roi"] <= 0
    assert _h(s, 15)["edge_retained"] is None
    assert s["copyability"] != "copyable"


def test_division_guards_no_crash():
    """#9 — band-edge prices and empty input do not crash / divide by 0."""
    closed, activity, ph = _many(20, won=True, p_entry=0.021, p5=0.03,
                                 p15=0.04, p60=0.05)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert isinstance(s["horizons"], list)
    empty = compute_copy_latency_surface([], [], {})
    assert empty["data_state"] == "data_unavailable"


def test_hard_wall_surface_never_mutates_verdict(monkeypatch):
    """#10 — a garbage-stub producer leaves verdict / gate /
    primary_edge_type / copyability_note / data_quality_flags identical."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.5,
                                 p15=0.6, p60=0.7)
    data = _data(closed, activity, ph)
    baseline = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)

    import sniper.analysis.wallet_protocol.copy_latency as cl

    monkeypatch.setattr(
        cl, "compute_copy_latency_surface",
        lambda *a, **k: {"data_state": "ok", "GARBAGE": 9.99e9},
    )
    poisoned = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)
    assert poisoned.copy_latency_surface.get("GARBAGE") == 9.99e9
    assert _signature(poisoned) == _signature(baseline)
    assert "GARBAGE" not in str(poisoned.data_quality_flags)


def test_field_present_on_live_path():
    """#11 — copy_latency_surface is a dict on the analyze_wallet path."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.5,
                                 p15=0.6, p60=0.7)
    report = analyze_wallet("0xabc", pre_fetched_data=_data(closed,
                            activity, ph), n_iter=100, seed=1)
    assert isinstance(report.copy_latency_surface, dict)
    assert "data_state" in report.copy_latency_surface


def test_replay_determinism():
    """#12 — same input → byte-identical surface JSON across two runs."""
    closed, activity, ph = _many(25, won=True, p_entry=0.40, p5=0.5,
                                 p15=0.6, p60=0.7)
    a = compute_copy_latency_surface(closed, activity, ph)
    b = compute_copy_latency_surface(closed, activity, ph)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_backward_compat_protocolreport_without_field():
    """#13 — a ProtocolReport built without the field defaults to None."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.copy_latency_surface is None


def test_near_certain_entry_spot_excluded():
    """Reviewer suggestion #1 — a position whose ENTRY spot is near-certain
    (out of the 0.02-0.98 band) is excluded from n_eligible (noisy
    roi_entry / edge_retained)."""
    closed, activity, ph = _many(22, won=True, p_entry=0.40, p5=0.42,
                                 p15=0.44, p60=0.46)
    pos, buy, entries = _covered(950, won=True, p_entry=0.99, p5=0.99,
                                 p15=0.99, p60=0.99)
    closed.append(pos); activity.append(buy); ph.update(entries)
    s = compute_copy_latency_surface(closed, activity, ph)
    assert s["n_eligible"] == 22            # the near-1.0 entry spot is out
    assert s["n_total_closed"] == 23


def test_out_of_range_horizon_price_dropped_with_caveat():
    """Review #2 — a horizon spot price > 1.0 (negative-risk / synthetic
    weirdness) is dropped, never fed into _roi, and an OUT_OF_RANGE caveat
    is emitted."""
    closed, activity, clp = _many(25, won=True, p_entry=0.40, p5=0.45,
                                  p15=0.50, p60=0.55)
    # corrupt one position's +15m spot to an impossible 1.7
    bad_asset = "tok-0"
    clp[f"{bad_asset}|{_TS + 0 * 100_000 + 900}"] = 1.7
    s = compute_copy_latency_surface(closed, activity, clp)
    assert _h(s, 15)["n"] == 24            # the corrupted +15m point dropped
    assert any("OUT_OF_RANGE_HORIZON_PRICE" in c for c in s["caveats"])


def test_all_out_of_range_horizon_prices_report_invalid_grid_not_missing_grid():
    """Review #3 — when a grid exists but every horizon price is invalid,
    report captured-but-unusable data instead of the dormant/pre-grid path."""
    closed, activity, clp = _many(20, won=True, p_entry=0.40, p5=1.7,
                                  p15=1.7, p60=1.7)
    s = compute_copy_latency_surface(closed, activity, clp)
    assert s["data_state"] == "data_unavailable"
    assert s["n_eligible"] == 20
    assert any("all_horizon_prices_out_of_range" in c for c in s["caveats"])
    assert any("OUT_OF_RANGE_HORIZON_PRICE" in c for c in s["caveats"])
    assert not any("no_post_entry_grid_captured" in c for c in s["caveats"])
