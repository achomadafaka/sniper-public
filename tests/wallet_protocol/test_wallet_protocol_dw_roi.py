"""I1 — top-level dollar-weighted vs equal-weighted ROI on ProtocolReport.

Spec: docs/superpowers/specs/wallet-protocol-bulletproofing.md §I1.

Bootstrap method (per advisor): standard ratio bootstrap — resample
(pnl_i, cost_i) PAIRS, recompute sum(pnl)/sum(cost) per iteration. This is
correct for a ratio statistic (the original task wording would have held the
denominator fixed and was wrong).

Universe: closed_positions (post-CUSUM-trim is irrelevant here; the field is
populated from ALL closed positions so the metric is comparable to H1's
winsorized mean which lives on the same universe).
"""

from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.held_analyst import compute_dw_ew_roi
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import WalletData


def _pos(cid: str, avg_price: float, size: float, real_pnl: float, *, cur_price: float = 1.0, outcome: str = "yes", ts: int = 1700000000):
    """Synthetic closed-position row matching Polymarket /closed-positions schema."""
    return {
        "conditionId": cid,
        "avgPrice": avg_price,
        "size": size,
        "realizedPnl": real_pnl,
        "curPrice": cur_price,
        "outcome": outcome,
        "timestamp": ts,
        "eventSlug": f"evt-{cid}",
    }


# ---------------------------------------------------------------------------
# compute_dw_ew_roi — pure-function tests
# ---------------------------------------------------------------------------


def test_dw_roi_diverges_from_ew_for_eatpraylove_archetype():
    """One big losing position + nine tiny winners → DW negative, EW positive.

    Cost basis advisor's exact construction:
      - 1 position: cost = $1000, pnl = -$500   (ROI = -50%)
      - 9 positions: cost = $10,   pnl = +$5    (ROI = +50% each)
    EW = mean ROI = (-0.50 + 9*0.50) / 10 = +0.40
    DW = sum(pnl) / sum(cost) = (-500 + 45) / (1000 + 90) = -455/1090 ≈ -0.4174
    Sign flip; ratio not meaningful (DW negative); divergence flag fires.
    """
    big_loser = _pos("c0", avg_price=1.0, size=1000.0, real_pnl=-500.0)
    small_winners = [
        _pos(f"c{i}", avg_price=1.0, size=10.0, real_pnl=5.0)
        for i in range(1, 10)
    ]
    positions = [big_loser] + small_winners

    result = compute_dw_ew_roi(positions, n_iter=2000, seed=1)
    assert result is not None
    dw_point, dw_lo, dw_hi, ew_point, n = result

    assert n == 10
    # Equal-weighted ROI ~ +40%
    assert ew_point == pytest.approx(0.40, abs=1e-9)
    # Dollar-weighted ROI ~ -41.7%
    assert dw_point == pytest.approx(-455.0 / 1090.0, abs=1e-9)
    # Bootstrap CI must bracket the point estimate
    assert dw_lo <= dw_point <= dw_hi


def test_dw_roi_matches_ew_when_positions_are_same_size():
    """When every position has identical cost basis, DW == EW exactly."""
    positions = [
        _pos("c0", avg_price=0.5, size=200.0, real_pnl=20.0),   # cost=100, ROI=+20%
        _pos("c1", avg_price=0.5, size=200.0, real_pnl=-10.0),  # cost=100, ROI=-10%
        _pos("c2", avg_price=0.5, size=200.0, real_pnl=30.0),   # cost=100, ROI=+30%
    ]
    result = compute_dw_ew_roi(positions, n_iter=2000, seed=1)
    assert result is not None
    dw_point, _, _, ew_point, n = result
    assert n == 3
    assert dw_point == pytest.approx(ew_point, abs=1e-9)


def test_dw_roi_returns_none_when_no_valid_cost_basis():
    """All positions have zero size → no valid cost basis → return None."""
    positions = [
        _pos("c0", avg_price=0.5, size=0.0, real_pnl=10.0),
        _pos("c1", avg_price=0.0, size=100.0, real_pnl=10.0),
    ]
    result = compute_dw_ew_roi(positions, n_iter=200, seed=1)
    assert result is None


def test_dw_roi_skips_zero_cost_positions_but_includes_others():
    """Zero-cost positions filtered out; rest computed normally."""
    positions = [
        _pos("c0", avg_price=0.0, size=100.0, real_pnl=5.0),  # zero cost — skip
        _pos("c1", avg_price=0.5, size=200.0, real_pnl=20.0),  # cost=100, +20%
        _pos("c2", avg_price=0.5, size=200.0, real_pnl=-10.0),  # cost=100, -10%
    ]
    result = compute_dw_ew_roi(positions, n_iter=2000, seed=1)
    assert result is not None
    dw_point, _, _, ew_point, n = result
    assert n == 2
    assert dw_point == pytest.approx(0.05, abs=1e-9)
    assert ew_point == pytest.approx(0.05, abs=1e-9)


def test_dw_roi_returns_none_for_empty_positions():
    assert compute_dw_ew_roi([], n_iter=200, seed=1) is None


def test_dw_roi_falls_back_to_totalBought_when_size_absent():
    """Polymarket /closed-positions has no `size` field — fall back to totalBought.

    For held-to-resolution positions totalBought == avgPrice * size exactly,
    so this fallback is exact. For early-exit closed positions totalBought
    overstates entry cost (gross dollars deployed) and conservatively
    dampens DW ROI — documented behaviour, not a bug.
    """
    # Closed positions in /closed-positions schema: avgPrice + totalBought + realizedPnl
    positions = [
        {"conditionId": "c0", "avgPrice": 0.5, "totalBought": 100.0, "realizedPnl": 20.0,
         "curPrice": 1.0, "outcome": "Yes", "timestamp": 1700000000, "eventSlug": "e0"},
        {"conditionId": "c1", "avgPrice": 0.5, "totalBought": 100.0, "realizedPnl": -10.0,
         "curPrice": 0.0, "outcome": "Yes", "timestamp": 1700000001, "eventSlug": "e1"},
    ]
    result = compute_dw_ew_roi(positions, n_iter=2000, seed=1)
    assert result is not None
    dw_point, _, _, ew_point, n = result
    assert n == 2
    # Same per-position cost — DW == EW
    assert dw_point == pytest.approx(0.05, abs=1e-9)
    assert ew_point == pytest.approx(0.05, abs=1e-9)


# ---------------------------------------------------------------------------
# Integration — analyze_wallet populates new fields + flag
# ---------------------------------------------------------------------------


def _wallet_data(positions, wallet="0xabc"):
    return WalletData(
        wallet=wallet,
        closed_positions=list(positions),
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
    )


def test_analyze_wallet_populates_dw_ew_roi_fields():
    """Report carries dw_roi_point, dw_roi_ci_*, ew_roi_point."""
    big_loser = _pos("c0", avg_price=1.0, size=1000.0, real_pnl=-500.0)
    small_winners = [
        _pos(f"c{i}", avg_price=1.0, size=10.0, real_pnl=5.0)
        for i in range(1, 10)
    ]
    data = _wallet_data([big_loser] + small_winners)

    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=2000,
        seed=1,
    )

    assert report.dw_roi_point is not None
    assert report.dw_roi_ci_lower is not None
    assert report.dw_roi_ci_upper is not None
    assert report.ew_roi_point is not None
    assert report.dw_roi_point == pytest.approx(-455.0 / 1090.0, abs=1e-9)
    assert report.ew_roi_point == pytest.approx(0.40, abs=1e-9)


def test_analyze_wallet_emits_dw_ew_divergence_flag():
    """When DW and EW disagree by >2× AND |EW| > 1%, DW_EW_DIVERGENCE fires."""
    big_loser = _pos("c0", avg_price=1.0, size=1000.0, real_pnl=-500.0)
    small_winners = [
        _pos(f"c{i}", avg_price=1.0, size=10.0, real_pnl=5.0)
        for i in range(1, 10)
    ]
    data = _wallet_data([big_loser] + small_winners)

    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=2000,
        seed=1,
    )

    assert any("DW_EW_DIVERGENCE" in f for f in report.data_quality_flags)


def test_analyze_wallet_no_divergence_flag_when_ratio_under_2x():
    """DW ≈ EW → no flag."""
    positions = [
        _pos("c0", avg_price=0.5, size=200.0, real_pnl=20.0),
        _pos("c1", avg_price=0.5, size=200.0, real_pnl=-10.0),
        _pos("c2", avg_price=0.5, size=200.0, real_pnl=30.0),
    ]
    data = _wallet_data(positions)
    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=2000,
        seed=1,
    )
    assert not any("DW_EW_DIVERGENCE" in f for f in report.data_quality_flags)


def test_analyze_wallet_no_divergence_flag_when_ew_near_zero():
    """|EW| < 1% guard → no flag even if DW differs (ratio undefined)
    WHEN signs match. Sign-flip case is covered separately below.
    """
    # EW ≈ 0.5% — below 1% epsilon. DW could still be larger but ratio is meaningless.
    # All positions positive PnL → DW and EW same sign → magnitude floor applies.
    positions = [
        _pos("c0", avg_price=0.5, size=200.0, real_pnl=1.0),    # cost=100, +1%
        _pos("c1", avg_price=0.5, size=200.0, real_pnl=0.0),    # cost=100, 0%
        _pos("c2", avg_price=0.5, size=200.0, real_pnl=0.5),    # cost=100, +0.5%
    ]
    data = _wallet_data(positions)
    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=500,
        seed=1,
    )
    # EW ≈ 0.5% — under 1% floor AND same sign as DW. Flag must NOT fire.
    assert not any("DW_EW_DIVERGENCE" in f for f in report.data_quality_flags)


def test_analyze_wallet_emits_divergence_flag_on_sign_flip_with_small_ew():
    """Sign flip overrides the |EW| < 1% magnitude floor.

    Empirical anchor: exec-bot 0x6a72f6 v3 fixture has DW=+4.50% / EW=-0.34%
    (|ratio|=13×, sign flip). The original implementation suppressed the
    flag because |EW|=0.34% was under the 1% magnitude floor, even though
    the sign flip alone is a structural divergence finding. Fix
    2026-05-14: magnitude floor applies only when signs match.

    Synthetic construction reproducing the pattern:
      - 1 big winner:  cost=$1000  pnl=+$50   → ROI=+5%
      - 10 small losers: cost=$10  pnl=-$0.07 → ROI=-0.7% each
    EW = mean(+5%, -0.7%*10) / 11 ≈ -0.18%   (|EW| < 1% floor)
    DW = (50 + (-0.7)) / (1000 + 100) ≈ +4.48%
    Signs differ → flag must fire.
    """
    big_winner = _pos("c0", avg_price=1.0, size=1000.0, real_pnl=50.0)
    small_losers = [
        _pos(f"c{i}", avg_price=1.0, size=10.0, real_pnl=-0.07)
        for i in range(1, 11)
    ]
    data = _wallet_data([big_winner] + small_losers)

    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=500,
        seed=1,
    )
    # Sign flip — flag must fire regardless of |EW| magnitude.
    assert any("DW_EW_DIVERGENCE" in f for f in report.data_quality_flags), (
        f"Sign-flip divergence not flagged. "
        f"dw={report.dw_roi_point}, ew={report.ew_roi_point}, "
        f"flags={report.data_quality_flags}"
    )


def test_dw_ew_divergence_flag_pure_function_sign_flip_small_ew():
    """Unit-test the flag helper directly: sign flip + small |EW| must fire.

    Mirrors the integration test above but at the function boundary so the
    fix can be reviewed without re-running analyze_wallet end-to-end.
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _dw_ew_divergence_flag,
    )

    # Sign flip, |EW| under the magnitude floor — should fire.
    flag = _dw_ew_divergence_flag(0.045, -0.0034)
    assert flag is not None
    assert flag.startswith("DW_EW_DIVERGENCE:")
    assert "sign_flip" in flag

    # Same-sign, |EW| under floor — should still NOT fire (existing rule).
    flag_same_sign = _dw_ew_divergence_flag(0.05, 0.005)
    assert flag_same_sign is None

    # EW exactly zero — sign of zero is undefined; no flag.
    flag_zero = _dw_ew_divergence_flag(0.05, 0.0)
    assert flag_zero is None


# ---------------------------------------------------------------------------
# Render — Capital efficiency section appears in markdown
# ---------------------------------------------------------------------------


def test_render_report_includes_capital_efficiency_section():
    big_loser = _pos("c0", avg_price=1.0, size=1000.0, real_pnl=-500.0)
    small_winners = [
        _pos(f"c{i}", avg_price=1.0, size=10.0, real_pnl=5.0)
        for i in range(1, 10)
    ]
    data = _wallet_data([big_loser] + small_winners)
    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=2000,
        seed=1,
    )
    md = render_report(report)
    assert "## Capital efficiency" in md
    assert "Dollar-weighted ROI" in md
    assert "Equal-weighted ROI" in md
    # Divergence flag rendered
    assert "DW vs EW divergence" in md


def test_render_report_omits_capital_efficiency_when_no_data():
    """Wallet with no closed positions → section absent (avoid noise)."""
    data = _wallet_data([])
    report = analyze_wallet(
        wallet="0xabc",
        pre_fetched_data=data,
        n_iter=200,
        seed=1,
    )
    md = render_report(report)
    assert "## Capital efficiency" not in md
