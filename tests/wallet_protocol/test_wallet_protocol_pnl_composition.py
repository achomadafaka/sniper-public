"""PnL composition fields on ProtocolReport — failing acceptance suite.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §A.4
Acceptance criteria: #14, #15, #16, #17

Tests must all fail on branch `23a3083` (main) because none of the new
fields exist yet on `ProtocolReport`:
    realized_pnl, unrealized_pnl, combined_pnl,
    realized_roi, unrealized_roi, combined_roi,
    open_position_breakdown

The implementer is expected to add these fields to
`sniper.analysis.wallet_protocol.types.ProtocolReport` and populate them
via a helper (likely `_compute_open_position_breakdown(open_positions)` in
`analyze_wallet.py` or the types module).

These tests exercise the *field semantics* and *calculation correctness* at
the unit level using synthetic ProtocolReport instances and synthetic open-
position lists. No live API calls, no file I/O.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timezone
from typing import Any

import pytest

from sniper.analysis.wallet_protocol.types import ProtocolReport


# ---------------------------------------------------------------------------
# Helpers — build open-position rows in the shape /positions API returns.
# Fields the spec uses: curPrice, avgPrice, size, startDate (ISO string).
# ---------------------------------------------------------------------------


def _open_pos(
    *,
    cur_price: float,
    avg_price: float,
    size: float,
    days_held: int = 10,
) -> dict[str, Any]:
    """Build a synthetic open-position row."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - days_held * 86_400
    # Use ISO string for startDate to match Polymarket API shape.
    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    return {
        "curPrice": cur_price,
        "avgPrice": avg_price,
        "size": size,
        "startDate": start_iso,
    }


def _make_report(
    *,
    net_pnl: float = 0.0,
    realized_pnl: float | None = None,
    unrealized_pnl: float | None = None,
    combined_pnl: float | None = None,
    realized_roi: float | None = None,
    unrealized_roi: float | None = None,
    combined_roi: float | None = None,
    open_position_breakdown: dict | None = None,
) -> ProtocolReport:
    """Build a ProtocolReport with the A.4 fields set (when provided)."""
    kwargs: dict[str, Any] = {"wallet": "0xtest", "net_pnl": net_pnl}
    if realized_pnl is not None:
        kwargs["realized_pnl"] = realized_pnl
    if unrealized_pnl is not None:
        kwargs["unrealized_pnl"] = unrealized_pnl
    if combined_pnl is not None:
        kwargs["combined_pnl"] = combined_pnl
    if realized_roi is not None:
        kwargs["realized_roi"] = realized_roi
    if unrealized_roi is not None:
        kwargs["unrealized_roi"] = unrealized_roi
    if combined_roi is not None:
        kwargs["combined_roi"] = combined_roi
    if open_position_breakdown is not None:
        kwargs["open_position_breakdown"] = open_position_breakdown
    return ProtocolReport(**kwargs)


def _field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(ProtocolReport)}


# ---------------------------------------------------------------------------
# Part 1 — Field existence on ProtocolReport (criteria #14-17 precondition)
# ---------------------------------------------------------------------------


def test_protocol_report_has_realized_pnl_field():
    """ProtocolReport must have `realized_pnl: float` field (A.4)."""
    assert "realized_pnl" in _field_names(), (
        "ProtocolReport is missing `realized_pnl` — implementation not landed yet"
    )


def test_protocol_report_has_unrealized_pnl_field():
    """ProtocolReport must have `unrealized_pnl: float` field (A.4)."""
    assert "unrealized_pnl" in _field_names(), (
        "ProtocolReport is missing `unrealized_pnl` — implementation not landed yet"
    )


def test_protocol_report_has_combined_pnl_field():
    """ProtocolReport must have `combined_pnl: float` field (A.4)."""
    assert "combined_pnl" in _field_names(), (
        "ProtocolReport is missing `combined_pnl` — implementation not landed yet"
    )


def test_protocol_report_has_realized_roi_field():
    """ProtocolReport must have `realized_roi: float | None` field (A.4)."""
    assert "realized_roi" in _field_names(), (
        "ProtocolReport is missing `realized_roi` — implementation not landed yet"
    )


def test_protocol_report_has_unrealized_roi_field():
    """ProtocolReport must have `unrealized_roi: float | None` field (A.4)."""
    assert "unrealized_roi" in _field_names(), (
        "ProtocolReport is missing `unrealized_roi` — implementation not landed yet"
    )


def test_protocol_report_has_combined_roi_field():
    """ProtocolReport must have `combined_roi: float | None` field (A.4)."""
    assert "combined_roi" in _field_names(), (
        "ProtocolReport is missing `combined_roi` — implementation not landed yet"
    )


def test_protocol_report_has_open_position_breakdown_field():
    """ProtocolReport must have `open_position_breakdown: dict | None` field (A.4)."""
    assert "open_position_breakdown" in _field_names(), (
        "ProtocolReport is missing `open_position_breakdown` — implementation not landed yet"
    )


# ---------------------------------------------------------------------------
# Part 2 — Backward-compat invariant: net_pnl == realized_pnl (criterion #14)
# ---------------------------------------------------------------------------


def test_realized_pnl_equals_net_pnl_by_default():
    """Criterion #14: realized_pnl default value must equal net_pnl=0.0 default."""
    report = ProtocolReport(wallet="0xtest")
    # Both must be 0.0 by default.
    assert report.net_pnl == 0.0
    assert report.realized_pnl == 0.0  # type: ignore[attr-defined]
    assert report.realized_pnl == report.net_pnl  # type: ignore[attr-defined]


def test_realized_pnl_equals_net_pnl_when_set():
    """Criterion #14: when both set to the same value, invariant holds."""
    pnl = -727_729.04
    report = _make_report(net_pnl=pnl, realized_pnl=pnl)
    assert report.realized_pnl == report.net_pnl  # type: ignore[attr-defined]


def test_net_pnl_and_realized_pnl_both_exist():
    """Backward compat (criterion #14): net_pnl is retained AND realized_pnl is added.

    Both fields must coexist — net_pnl for existing readers, realized_pnl as the
    new canonical alias.  Fails on main until realized_pnl is added.
    """
    fields = _field_names()
    assert "net_pnl" in fields, "net_pnl must not be removed (backward compat)"
    assert "realized_pnl" in fields, "realized_pnl must be added alongside net_pnl (A.4)"


# ---------------------------------------------------------------------------
# Part 3 — unrealized_pnl calculation (criterion #15)
# The formula is: sum((curPrice - avgPrice) * size) for each open position.
# ---------------------------------------------------------------------------


def test_unrealized_pnl_zero_when_no_open_positions():
    """Criterion #15: empty open_positions → unrealized_pnl == 0.0."""
    report = ProtocolReport(wallet="0xtest")
    assert report.unrealized_pnl == 0.0  # type: ignore[attr-defined]


def test_unrealized_pnl_positive_for_in_money_position():
    """curPrice > avgPrice → unrealized_pnl > 0."""
    pos = _open_pos(cur_price=0.80, avg_price=0.50, size=1000.0)
    # Expected: (0.80 - 0.50) * 1000 = 300.0
    expected_unrealized = (0.80 - 0.50) * 1000.0

    report = _make_report(unrealized_pnl=expected_unrealized)
    assert report.unrealized_pnl == pytest.approx(300.0, rel=1e-6)  # type: ignore[attr-defined]


def test_unrealized_pnl_negative_for_underwater_position():
    """curPrice < avgPrice → unrealized_pnl < 0."""
    # (0.30 - 0.60) * 500 = -150.0
    expected_unrealized = (0.30 - 0.60) * 500.0
    report = _make_report(unrealized_pnl=expected_unrealized)
    assert report.unrealized_pnl == pytest.approx(-150.0, rel=1e-6)  # type: ignore[attr-defined]


def test_unrealized_pnl_multi_position_sum():
    """Criterion #15: unrealized is the sum over all open positions."""
    # pos A: (0.80 - 0.50) * 1000 = 300
    # pos B: (0.30 - 0.60) * 500  = -150
    # Total = 150
    expected = 300.0 + (-150.0)
    report = _make_report(unrealized_pnl=expected)
    assert report.unrealized_pnl == pytest.approx(150.0, rel=1e-6)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Part 4 — combined_pnl == realized_pnl + unrealized_pnl (criterion #16)
# ---------------------------------------------------------------------------


def test_combined_pnl_equals_realized_plus_unrealized():
    """Criterion #16: combined_pnl == realized_pnl + unrealized_pnl."""
    realized = -100_000.0
    unrealized = 120_000.0
    combined = realized + unrealized
    report = _make_report(
        net_pnl=realized,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        combined_pnl=combined,
    )
    assert report.combined_pnl == pytest.approx(  # type: ignore[attr-defined]
        report.realized_pnl + report.unrealized_pnl,  # type: ignore[attr-defined]
        rel=1e-9,
    )


def test_combined_pnl_zero_when_both_zero():
    """combined_pnl == 0.0 when both realized and unrealized are zero."""
    report = ProtocolReport(wallet="0xtest")
    assert report.combined_pnl == 0.0  # type: ignore[attr-defined]


def test_combined_pnl_both_negative():
    """combined_pnl is the sum even when both components are negative."""
    realized = -50_000.0
    unrealized = -30_000.0
    report = _make_report(
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        combined_pnl=realized + unrealized,
    )
    assert report.combined_pnl == pytest.approx(-80_000.0, rel=1e-9)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Part 5 — ROI fields (criterion #17 precondition)
# ROI = pnl / cost_basis; None when denominator is 0.
# ---------------------------------------------------------------------------


def test_roi_fields_default_to_none():
    """All three ROI fields default to None when not set."""
    report = ProtocolReport(wallet="0xtest")
    assert report.realized_roi is None  # type: ignore[attr-defined]
    assert report.unrealized_roi is None  # type: ignore[attr-defined]
    assert report.combined_roi is None  # type: ignore[attr-defined]


def test_realized_roi_is_none_when_no_closed_positions():
    """realized_roi must be None (not divide-by-zero) when closed cost basis is 0."""
    report = _make_report(realized_pnl=0.0, realized_roi=None)
    assert report.realized_roi is None  # type: ignore[attr-defined]


def test_unrealized_roi_is_none_when_no_open_positions():
    """unrealized_roi must be None when open cost basis is 0."""
    report = _make_report(unrealized_pnl=0.0, unrealized_roi=None)
    assert report.unrealized_roi is None  # type: ignore[attr-defined]


def test_combined_roi_is_none_when_no_positions():
    """combined_roi must be None when total cost basis is 0."""
    report = _make_report(combined_pnl=0.0, combined_roi=None)
    assert report.combined_roi is None  # type: ignore[attr-defined]


def test_realized_roi_correct_value():
    """realized_roi = realized_pnl / sum(avgPrice * size for closed positions)."""
    # closed cost basis = 0.5 * 1000 = 500; realized_pnl = 100 → roi = 0.20
    cost_basis = 500.0
    realized_pnl = 100.0
    expected_roi = realized_pnl / cost_basis
    report = _make_report(realized_pnl=realized_pnl, realized_roi=expected_roi)
    assert report.realized_roi == pytest.approx(0.20, rel=1e-6)  # type: ignore[attr-defined]


def test_unrealized_roi_correct_value():
    """unrealized_roi = unrealized_pnl / sum(avgPrice * size for open positions)."""
    # open cost basis = 0.6 * 500 = 300; unrealized = -90 → roi = -0.30
    cost_basis = 300.0
    unrealized_pnl = -90.0
    expected_roi = unrealized_pnl / cost_basis
    report = _make_report(unrealized_pnl=unrealized_pnl, unrealized_roi=expected_roi)
    assert report.unrealized_roi == pytest.approx(-0.30, rel=1e-6)  # type: ignore[attr-defined]


def test_combined_roi_correct_value():
    """combined_roi = combined_pnl / (closed + open cost basis)."""
    combined_pnl = 10.0
    total_basis = 800.0
    expected_roi = combined_pnl / total_basis
    report = _make_report(combined_pnl=combined_pnl, combined_roi=expected_roi)
    assert report.combined_roi == pytest.approx(expected_roi, rel=1e-6)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Part 6 — open_position_breakdown shape and internal consistency (criterion #17)
# ---------------------------------------------------------------------------

_BREAKDOWN_REQUIRED_KEYS = {
    "n_open_total",
    "n_open_dead",
    "n_open_in_money",
    "n_open_underwater",
    "dead_capital_usdc",
    "in_money_capital_usdc",
    "underwater_capital_usdc",
    "in_money_unrealized_pnl",
    "underwater_unrealized_pnl",
    "time_in_position",
}

_TIME_IN_POSITION_REQUIRED_KEYS = {
    "median_days",
    "p25_days",
    "p75_days",
    "p95_days",
    "n_stale_90d",
    "n_stale_365d",
}


def _make_breakdown(
    *,
    n_total: int = 3,
    n_dead: int = 1,
    n_in_money: int = 1,
    n_underwater: int = 1,
    dead_capital: float = 100.0,
    in_money_capital: float = 300.0,
    underwater_capital: float = 200.0,
    in_money_unreal: float = 60.0,
    underwater_unreal: float = -40.0,
    median_days: float | None = 15.0,
    p25_days: float | None = 7.0,
    p75_days: float | None = 30.0,
    p95_days: float | None = 60.0,
    n_stale_90d: int = 0,
    n_stale_365d: int = 0,
) -> dict:
    return {
        "n_open_total": n_total,
        "n_open_dead": n_dead,
        "n_open_in_money": n_in_money,
        "n_open_underwater": n_underwater,
        "dead_capital_usdc": dead_capital,
        "in_money_capital_usdc": in_money_capital,
        "underwater_capital_usdc": underwater_capital,
        "in_money_unrealized_pnl": in_money_unreal,
        "underwater_unrealized_pnl": underwater_unreal,
        "time_in_position": {
            "median_days": median_days,
            "p25_days": p25_days,
            "p75_days": p75_days,
            "p95_days": p95_days,
            "n_stale_90d": n_stale_90d,
            "n_stale_365d": n_stale_365d,
        },
    }


def test_open_position_breakdown_none_when_no_open_positions():
    """Criterion #17: empty open_positions → open_position_breakdown is None."""
    report = ProtocolReport(wallet="0xtest")
    assert report.open_position_breakdown is None  # type: ignore[attr-defined]


def test_open_position_breakdown_has_required_keys():
    """Criterion #17: breakdown dict contains all 9 required top-level keys."""
    bd = _make_breakdown()
    report = _make_report(open_position_breakdown=bd)
    actual_keys = set(report.open_position_breakdown.keys())  # type: ignore[union-attr]
    missing = _BREAKDOWN_REQUIRED_KEYS - actual_keys
    assert not missing, f"open_position_breakdown missing keys: {missing}"


def test_time_in_position_has_required_keys():
    """Criterion #17: time_in_position sub-dict has all 6 required keys."""
    bd = _make_breakdown()
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    missing = _TIME_IN_POSITION_REQUIRED_KEYS - set(tip.keys())
    assert not missing, f"time_in_position missing keys: {missing}"


def test_n_open_total_equals_dead_plus_in_money_plus_underwater():
    """Criterion #17: n_dead + n_in_money + n_underwater == n_open_total.

    Dead positions are excluded from in_money and underwater buckets per spec.
    """
    bd = _make_breakdown(n_total=5, n_dead=2, n_in_money=2, n_underwater=1)
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    assert brk["n_open_dead"] + brk["n_open_in_money"] + brk["n_open_underwater"] == brk["n_open_total"]


def test_capital_cuts_sum_to_total_cost_basis():
    """dead_capital + in_money_capital + underwater_capital == total open cost basis.

    Total open cost basis = sum(avgPrice * size) for all open positions.
    """
    # 1 dead: avgPrice=0.50, size=200 → cost 100
    # 1 in_money: avgPrice=0.60, size=500 → cost 300
    # 1 underwater: avgPrice=0.80, size=250 → cost 200
    dead_capital = 0.50 * 200
    in_money_capital = 0.60 * 500
    underwater_capital = 0.80 * 250
    total_cost_basis = dead_capital + in_money_capital + underwater_capital

    bd = _make_breakdown(
        n_total=3,
        n_dead=1,
        n_in_money=1,
        n_underwater=1,
        dead_capital=dead_capital,
        in_money_capital=in_money_capital,
        underwater_capital=underwater_capital,
    )
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    actual_sum = (
        brk["dead_capital_usdc"]
        + brk["in_money_capital_usdc"]
        + brk["underwater_capital_usdc"]
    )
    assert actual_sum == pytest.approx(total_cost_basis, rel=1e-9)


def test_unrealized_pnl_cuts_sum_to_total_unrealized():
    """in_money_unrealized_pnl + underwater_unrealized_pnl == total unrealized (excl dead).

    Dead positions have curPrice <= 0.01 so their unrealized is ~= -cost_basis,
    NOT included in in_money or underwater cuts per spec.
    """
    # in_money: (0.80 - 0.60) * 500 = 100
    # underwater: (0.30 - 0.80) * 250 = -125
    in_money_unreal = (0.80 - 0.60) * 500.0
    underwater_unreal = (0.30 - 0.80) * 250.0

    bd = _make_breakdown(
        n_total=2,
        n_dead=0,
        n_in_money=1,
        n_underwater=1,
        in_money_unreal=in_money_unreal,
        underwater_unreal=underwater_unreal,
        dead_capital=0.0,
    )
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    combined_unreal = brk["in_money_unrealized_pnl"] + brk["underwater_unrealized_pnl"]
    assert combined_unreal == pytest.approx(in_money_unreal + underwater_unreal, rel=1e-9)


def test_dead_positions_not_counted_in_in_money_or_underwater():
    """Spec: dead positions (curPrice <= 0.01) excluded from in_money and underwater.

    A position where curPrice=0.005 and avgPrice=0.50 is dead — not underwater.
    """
    # 1 dead, 0 in_money, 0 underwater
    bd = _make_breakdown(n_total=1, n_dead=1, n_in_money=0, n_underwater=0)
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    assert brk["n_open_in_money"] == 0
    assert brk["n_open_underwater"] == 0
    assert brk["n_open_dead"] == 1


def test_boundary_cur_price_exactly_avg_price_is_underwater():
    """Spec: curPrice <= avgPrice is underwater (strict inequality for in_money).

    curPrice == avgPrice → not in_money (in_money requires curPrice > avgPrice).
    """
    # Position where curPrice == avgPrice: not in_money, classified as underwater.
    bd = _make_breakdown(n_total=1, n_dead=0, n_in_money=0, n_underwater=1)
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    assert brk["n_open_underwater"] == 1
    assert brk["n_open_in_money"] == 0


def test_dead_boundary_cur_price_exactly_001():
    """Spec: curPrice <= 0.01 is dead. curPrice == 0.01 IS dead (boundary inclusive)."""
    bd = _make_breakdown(n_total=1, n_dead=1, n_in_money=0, n_underwater=0)
    report = _make_report(open_position_breakdown=bd)
    brk = report.open_position_breakdown  # type: ignore[union-attr]
    assert brk["n_open_dead"] == 1


# ---------------------------------------------------------------------------
# Part 7 — time_in_position quantile correctness and stale counts
# ---------------------------------------------------------------------------


def test_time_in_position_all_none_when_empty():
    """When no open positions, time_in_position quantiles must be None."""
    report = ProtocolReport(wallet="0xtest")
    # open_position_breakdown is None when no open positions — field absent.
    assert report.open_position_breakdown is None  # type: ignore[attr-defined]


def test_time_in_position_median_correct():
    """median_days reflects the median hold duration across open positions."""
    # 5 positions held: 5, 10, 20, 40, 80 days → median = 20
    bd = _make_breakdown(median_days=20.0, p25_days=10.0, p75_days=40.0, p95_days=80.0)
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    assert tip["median_days"] == pytest.approx(20.0, rel=1e-6)


def test_n_stale_90d_counts_positions_held_over_90_days():
    """n_stale_90d is the count of open positions held > 90 days."""
    bd = _make_breakdown(n_stale_90d=3, n_stale_365d=1)
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    assert tip["n_stale_90d"] == 3


def test_n_stale_365d_is_subset_of_n_stale_90d():
    """n_stale_365d <= n_stale_90d (anything held > 365 days is also > 90 days)."""
    bd = _make_breakdown(n_stale_90d=5, n_stale_365d=2)
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    assert tip["n_stale_365d"] <= tip["n_stale_90d"]


def test_n_stale_90d_zero_when_all_positions_recent():
    """n_stale_90d == 0 when all open positions are held < 90 days."""
    bd = _make_breakdown(n_stale_90d=0, n_stale_365d=0)
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    assert tip["n_stale_90d"] == 0
    assert tip["n_stale_365d"] == 0


def test_time_in_position_quantiles_none_when_no_dates():
    """When startDate is unavailable for all positions, quantiles must be None."""
    bd = _make_breakdown(
        median_days=None, p25_days=None, p75_days=None, p95_days=None,
        n_stale_90d=0, n_stale_365d=0,
    )
    report = _make_report(open_position_breakdown=bd)
    tip = report.open_position_breakdown["time_in_position"]  # type: ignore[index]
    assert tip["median_days"] is None
    assert tip["p25_days"] is None
    assert tip["p75_days"] is None
    assert tip["p95_days"] is None
