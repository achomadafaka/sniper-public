"""Acceptance tests — Null-Strategy Baseline surface (`null_baseline_surface`).

Spec: docs/superpowers/specs/2026-05-22-null-baseline-surface-design.md

Display-only surface: did the wallet's side selection beat the chalk?
Compares the wallet's hold-to-resolution side return against always-favorite
/ always-underdog / random baselines on the markets it traded. Hard wall
identical to capital_risk_surface — never feeds verdict / gate /
primary_edge_type / copyability_note / data_quality_flags.

RED until `compute_null_baseline_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.null_baseline_surface`.
"""
from __future__ import annotations

import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.null_baseline import (
    compute_null_baseline_surface,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# builders — a closed position carries conditionId, avgPrice, curPrice
# --------------------------------------------------------------------------
_CTR = [0]


def _pos(avg_price, cur_price, *, neg_risk: bool = False,
         cid: str | None = None) -> dict:
    _CTR[0] += 1
    row: dict = {
        "conditionId": cid or f"c{_CTR[0]}",
        "outcome": "Yes",
        "realizedPnl": 1.0,
    }
    if avg_price is not None:
        row["avgPrice"] = avg_price
    if cur_price is not None:
        row["curPrice"] = cur_price
    if neg_risk:
        row["negRisk"] = True
    return row


def _fav_win(n: int) -> list[dict]:
    return [_pos(0.65, 1.0) for _ in range(n)]


def _fav_loss(n: int) -> list[dict]:
    return [_pos(0.65, 0.0) for _ in range(n)]


def _dog_win(n: int) -> list[dict]:
    return [_pos(0.35, 1.0) for _ in range(n)]


def _dog_loss(n: int) -> list[dict]:
    return [_pos(0.35, 0.0) for _ in range(n)]


def _data(closed_positions) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
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


# ==========================================================================
# §9 — acceptance tests
# ==========================================================================
def test_eligibility_excludes_unresolved_and_near_certain():
    """#1 — an unresolved position (curPrice mid-range) and an avgPrice=0.99
    position are excluded from n_eligible but counted in n_total/coverage."""
    closed = (_fav_win(20)
              + [_pos(0.60, 0.50)]           # unresolved
              + [_pos(0.99, 1.0)])           # near-certain, out of band
    s = compute_null_baseline_surface(closed)
    assert s["n_eligible"] == 20
    assert s["n_total_closed"] == 22
    assert abs(s["coverage"] - round(20 / 22, 4)) < 1e-9


def test_roi_math_favorite_won():
    """#2 — favorite bought at 0.60 that won: wallet_roi == favorite_roi ==
    (1-0.6)/0.6; underdog_roi == -1."""
    closed = [_pos(0.60, 1.0) for _ in range(20)]
    s = compute_null_baseline_surface(closed)
    assert abs(s["wallet_ew_roi"] - (0.4 / 0.6)) < 1e-6
    assert abs(s["favorite_ew_roi"] - (0.4 / 0.6)) < 1e-6
    assert abs(s["underdog_ew_roi"] - (-1.0)) < 1e-6


def test_wallet_on_underdog_lost():
    """#3 — wallet bought the underdog at 0.30 and lost → wallet_roi == -1,
    equals underdog_roi; the favorite won."""
    closed = [_pos(0.30, 0.0) for _ in range(20)]
    s = compute_null_baseline_surface(closed)
    assert abs(s["wallet_ew_roi"] - (-1.0)) < 1e-6
    assert abs(s["underdog_ew_roi"] - (-1.0)) < 1e-6
    assert s["favorite_hit_rate"] == 1.0          # favorite won every market


def test_always_favorite_wallet_is_inconclusive_proxy_only():
    """#4 — every eligible position is a favorite bet → edge_vs_favorite ≈ 0,
    data_state proxy_only (favorite dominance), comparison inconclusive."""
    closed = _fav_win(15) + _fav_loss(15)
    s = compute_null_baseline_surface(closed)
    assert abs(s["edge_vs_favorite"]) < 1e-6
    assert s["data_state"] == "proxy_only"
    assert s["comparison"] == "inconclusive"


def test_underdog_edge_beats_chalk():
    """#5 — a clean wallet that bet underdogs that mostly hit beats the
    favorite line → edge_vs_favorite > 0, comparison beats_chalk."""
    closed = _dog_win(24) + _dog_loss(6)
    s = compute_null_baseline_surface(closed)
    assert s["data_state"] == "ok"
    assert s["edge_vs_favorite"] > 0.0
    assert s["comparison"] == "beats_chalk"


def test_comparison_underperforms_chalk():
    """#6a — a clean wallet that bet underdogs that mostly missed
    underperforms the favorite line."""
    closed = _dog_loss(24) + _dog_win(6)
    s = compute_null_baseline_surface(closed)
    assert s["data_state"] == "ok"
    assert s["edge_vs_favorite"] < 0.0
    assert s["comparison"] == "underperforms_chalk"


def test_comparison_inconclusive_when_not_ok():
    """#6b — a degraded (non-ok) surface never gets a beats/underperforms
    verdict, regardless of edge sign."""
    closed = _dog_win(10)                      # < 20 → underpowered
    s = compute_null_baseline_surface(closed)
    assert s["data_state"] != "ok"
    assert s["comparison"] == "inconclusive"


def test_data_state_transitions():
    """#7 — data_unavailable / underpowered / ok."""
    assert compute_null_baseline_surface([])["data_state"] == "data_unavailable"
    assert compute_null_baseline_surface(
        _dog_win(8) + _dog_loss(4))["data_state"] == "underpowered"
    assert compute_null_baseline_surface(
        _dog_win(20) + _dog_loss(10))["data_state"] == "ok"


def test_missing_field_position_is_excluded_not_a_loss():
    """#8 — a position with a missing curPrice is excluded, NOT silently
    scored as a resolved loss."""
    closed = _dog_win(20) + [_pos(0.40, None)]   # curPrice missing
    s = compute_null_baseline_surface(closed)
    assert s["n_eligible"] == 20
    assert s["n_total_closed"] == 21


def test_negative_risk_position_excluded():
    """#9 — a position flagged negative-risk is excluded from n_eligible."""
    closed = _dog_win(20) + [_pos(0.45, 1.0, neg_risk=True)]
    s = compute_null_baseline_surface(closed)
    assert s["n_eligible"] == 20


def test_division_guards_no_crash():
    """#10 — band-edge prices and empty input do not crash / divide by 0."""
    edge = compute_null_baseline_surface(
        [_pos(0.021, 1.0) for _ in range(20)])
    assert edge["wallet_ew_roi"] is not None
    empty = compute_null_baseline_surface([])
    assert empty["data_state"] == "data_unavailable"


def test_favorite_hit_rate_matches_base_rate():
    """#11 — favorite_hit_rate equals the constructed base rate."""
    # 20 favorite bets that won, 10 favorite bets that lost → fav won 20/30.
    closed = _fav_win(20) + _fav_loss(10)
    s = compute_null_baseline_surface(closed)
    assert abs(s["favorite_hit_rate"] - round(20 / 30, 4)) < 1e-9


def test_hard_wall_surface_never_mutates_verdict(monkeypatch):
    """#12 — a garbage-stub producer leaves verdict / gate / primary_edge_type
    / copyability_note / data_quality_flags byte-identical."""
    closed = _dog_win(20) + _dog_loss(10)
    data = _data(closed)
    baseline = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)

    import sniper.analysis.wallet_protocol.null_baseline as nb

    monkeypatch.setattr(
        nb, "compute_null_baseline_surface",
        lambda *a, **k: {"data_state": "ok", "GARBAGE": 9.99e9},
    )
    poisoned = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1)
    assert poisoned.null_baseline_surface.get("GARBAGE") == 9.99e9
    assert _signature(poisoned) == _signature(baseline)
    assert "GARBAGE" not in str(poisoned.data_quality_flags)


def test_field_present_on_live_path():
    """#13 — null_baseline_surface is a dict on the analyze_wallet path."""
    data = _data(_dog_win(20) + _dog_loss(10))
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                            seed=1)
    assert isinstance(report.null_baseline_surface, dict)
    assert "data_state" in report.null_baseline_surface


def test_replay_determinism():
    """#14 — same input → byte-identical surface JSON across two runs."""
    closed = _dog_win(20) + _dog_loss(10)
    a = compute_null_baseline_surface(closed)
    b = compute_null_baseline_surface(closed)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_backward_compat_protocolreport_without_field():
    """#15 — a ProtocolReport built without the field defaults to None."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.null_baseline_surface is None


# ==========================================================================
# Reviewer fixes (platform-reviewer pass, 2026-05-22) — test-coverage gaps
# ==========================================================================
def test_low_coverage_degrades_to_proxy_only():
    """Reviewer #1: coverage below the floor (many ineligible positions) →
    proxy_only with a LOW_COVERAGE caveat; comparison inconclusive."""
    closed = _dog_win(20) + [_pos(0.60, 0.50) for _ in range(25)]   # 20 / 45
    s = compute_null_baseline_surface(closed)
    assert s["data_state"] == "proxy_only"
    assert s["comparison"] == "inconclusive"
    assert any("LOW_COVERAGE" in c for c in s["caveats"])


def test_missing_and_out_of_range_avgprice_excluded():
    """Reviewer #2: a missing avgPrice, and negative / >1 avgPrice, are all
    excluded — no crash, no _roi divide-by-near-zero."""
    closed = (_dog_win(20)
              + [_pos(None, 1.0)]        # avgPrice missing
              + [_pos(-0.10, 1.0)]       # negative
              + [_pos(1.50, 1.0)])       # > 1
    s = compute_null_baseline_surface(closed)
    assert s["n_eligible"] == 20
    assert s["n_total_closed"] == 23
