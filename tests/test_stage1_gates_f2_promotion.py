"""Regression test for v3 amendment B: F2 caution flag → H1_sub_top3 wiring.

Locks the contract introduced by commit 8343eed
("feat(wallet-protocol/v3): F2 structured payload + H1_sub_top3 wiring"):
when ``_top3_pnl_concentration`` exceeds ``F2_TOP3_NET_PNL_FRACTION``,
``evaluate_stage1_gates`` populates ``f2_top3_indices`` + ``f2_top3_share``
so that downstream ``analyze_wallet`` can run the H1_sub_top3 diagnostic
and emit ``EDGE_CONDITIONAL_ON_TOP3_EXCLUSION`` when the excluded subset
has positive CI.

Phase 3 plan § A9.
"""
from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.stage1_gates import (
    F2_TOP3_NET_PNL_FRACTION,
    evaluate_stage1_gates,
)
from sniper.analysis.wallet_protocol.types import WalletData


def _pos(
    *,
    realized_pnl: float,
    avg_price: float = 0.50,
    cur_price: float = 0.0,
    cid: str = "0xcid",
    ts: int | None = None,
) -> dict:
    return {
        "conditionId": cid,
        "outcome": "yes",
        "avgPrice": avg_price,
        "size": 1.0,
        "curPrice": cur_price,
        "realizedPnl": realized_pnl,
        "timestamp": ts if ts is not None else int(time.time()) - 7 * 86400,
    }


def _wallet_data(closed: list[dict]) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
    )


def _concentrated_closed_positions() -> list[dict]:
    """50 positions where the top-3 absolute realized P&L > 70% of total.

    Outliers carry the entire P&L story; the remaining 47 positions are
    small consistent winners (the "real" book). H1 winsorized may or may
    not survive the outlier influence; H1_sub_top3 (excluded outliers)
    should be cleanly positive.
    """
    closed: list[dict] = []
    closed.append(_pos(realized_pnl=-100_000.0, cid="0xouter1"))
    closed.append(_pos(realized_pnl=-50_000.0, cid="0xouter2"))
    closed.append(_pos(realized_pnl=-25_000.0, cid="0xouter3"))
    for i in range(47):
        closed.append(_pos(realized_pnl=200.0, cid=f"0xsmall{i:02d}"))
    return closed


def _balanced_closed_positions(n: int = 50) -> list[dict]:
    """All positions same magnitude; F2 must NOT fire."""
    return [_pos(realized_pnl=200.0, cid=f"0xeven{i:02d}") for i in range(n)]


# ---- Stage 1 unit tests ----------------------------------------------------


def test_f2_populates_top3_indices_when_concentrated():
    """F2 caution flag emission + structured payload (v3-B contract)."""
    data = _wallet_data(_concentrated_closed_positions())
    ev = evaluate_stage1_gates(data)

    assert ev.result == "pass", ev.reason
    # Caution flag string preserved for backcompat.
    assert any(
        s.startswith("F2_single_bet_concentration") for s in ev.caution_flags
    ), ev.caution_flags
    # Structured payload — the v3-B addition.
    assert ev.f2_top3_indices is not None
    assert len(ev.f2_top3_indices) == 3
    assert set(ev.f2_top3_indices) == {0, 1, 2}, (
        f"top-3 by abs(realizedPnl) should be the outlier indices; "
        f"got {ev.f2_top3_indices}"
    )
    assert ev.f2_top3_share is not None
    assert ev.f2_top3_share > F2_TOP3_NET_PNL_FRACTION


def test_f2_indices_none_when_concentration_below_threshold():
    """When top-3 share <= F2_TOP3_NET_PNL_FRACTION, no F2 emission."""
    data = _wallet_data(_balanced_closed_positions())
    ev = evaluate_stage1_gates(data)

    assert ev.result == "pass"
    assert all(
        not s.startswith("F2_single_bet_concentration") for s in ev.caution_flags
    )
    assert ev.f2_top3_indices is None
    assert ev.f2_top3_share is None


def test_f2_indices_none_when_total_pnl_zero():
    """Degenerate case: all realizedPnl = 0 → concentration is undefined."""
    closed = [_pos(realized_pnl=0.0, cid=f"0xz{i:02d}") for i in range(40)]
    ev = evaluate_stage1_gates(_wallet_data(closed))

    assert ev.result == "pass"
    assert ev.f2_top3_indices is None
    assert ev.f2_top3_share is None


def test_f2_threshold_locked_at_70_percent():
    """v3-B locks the threshold; downstream readers depend on it."""
    assert F2_TOP3_NET_PNL_FRACTION == 0.70


# ---- Integration: F2 indices must route into H1_sub_top3 -------------------


def test_f2_indices_drive_h1_sub_top3_diagnostic():
    """When F2 fires, analyze_wallet must pass f2_top3_indices into
    run_h1_net_pnl so H1.details surfaces the sub_top3 diagnostic."""
    data = _wallet_data(_concentrated_closed_positions())
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=500)

    held = report.held_analyst_branch
    assert held is not None and held.is_active
    h1 = next((h for h in held.hypotheses if h.name == "H1"), None)
    assert h1 is not None
    assert isinstance(h1.details, dict)
    assert "sub_top3" in h1.details, (
        f"H1.details missing sub_top3 — F2 → H1 wiring regressed. "
        f"details keys: {list(h1.details.keys())}"
    )
    sub = h1.details["sub_top3"]
    assert isinstance(sub, dict)
    # Either powered (CI fields populated) or explicitly skipped — never silent.
    assert ("ci_lower" in sub and "ci_upper" in sub) or "skipped" in sub, sub


def test_no_h1_sub_top3_when_f2_does_not_fire():
    """F2 dormant → no top3_indices → no sub_top3 diagnostic."""
    data = _wallet_data(_balanced_closed_positions())
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=500)

    held = report.held_analyst_branch
    assert held is not None and held.is_active
    h1 = next((h for h in held.hypotheses if h.name == "H1"), None)
    assert h1 is not None
    # When F2 doesn't fire, sub_top3 must not be computed.
    assert "sub_top3" not in (h1.details or {})
