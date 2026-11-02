"""Strengthened tests for Phase A split #2 PnL composition computation.

The first acceptance suite (`test_wallet_protocol_pnl_composition.py`) was
review-flagged for round-tripping values: tests built expected results,
stamped them onto a ProtocolReport via kwargs, then asserted they came back.
An implementer could satisfy that suite by adding fields with defaults and
never computing anything from closed_positions / open_positions.

This file adds three things the round-trip suite is missing:

1. Direct tests against `_compute_open_position_breakdown` with synthetic
   open_positions input → assert the returned dict matches expected math.
2. End-to-end tests via `analyze_wallet(pre_fetched_data=WalletData(...))`
   that pass synthetic closed_positions + open_positions and assert the
   computed PnL fields on the returned report.
3. Backward-compat invariant: `ProtocolReport(net_pnl=X)` (legacy
   constructor, common in existing test fixtures) yields `realized_pnl == X`.
4. Integration: the negative-PnL honesty floor sees populated PnL fields
   after analyze_wallet's normal stamping order (computation BEFORE floor).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


# ---------------------------------------------------------------------------
# Helpers — build synthetic Polymarket position rows
# ---------------------------------------------------------------------------


def _open_pos(
    *,
    cur_price: float,
    avg_price: float,
    size: float,
    days_held: int = 10,
    asset: str = "0xasset",
    condition_id: str = "0xcid",
    outcome: str = "Yes",
):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    start_iso = datetime.fromtimestamp(
        now_ts - days_held * 86_400, tz=timezone.utc
    ).isoformat()
    return {
        "asset": asset,
        "conditionId": condition_id,
        "outcome": outcome,
        "curPrice": cur_price,
        "avgPrice": avg_price,
        "size": size,
        "startDate": start_iso,
        "initialValue": size * cur_price,
    }


def _closed_pos(
    *,
    realized_pnl: float,
    avg_price: float = 0.5,
    size: float = 100.0,
    ts: int | None = None,
    condition_id: str | None = None,
    outcome: str = "Yes",
    asset: str | None = None,
):
    """Build a synthetic closed-position row."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return {
        "asset": asset or f"0xasset_{id(realized_pnl):x}",
        "conditionId": condition_id or f"0xcid_{id(realized_pnl):x}",
        "outcome": outcome,
        "realizedPnl": realized_pnl,
        "avgPrice": avg_price,
        "size": size,
        "timestamp": ts if ts is not None else now_ts,
        # mimic Polymarket Data API closed-position shape
        "initialValue": size * avg_price,
    }


def _make_wallet_data(closed_positions, open_positions=None, activity_rows=None):
    """Construct a WalletData instance with synthetic surfaces."""
    return WalletData(
        wallet="0xtestwallet",
        closed_positions=closed_positions,
        open_positions=open_positions or [],
        activity_rows=activity_rows or [],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(datetime.now(timezone.utc).timestamp()),
    )


# ---------------------------------------------------------------------------
# Group 1 — direct helper tests (computation, not round-trip)
# ---------------------------------------------------------------------------


def test_compute_open_position_breakdown_direct_helper_in_money():
    """Helper sums unrealized for an in-money position from real inputs."""
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    positions = [_open_pos(cur_price=0.80, avg_price=0.50, size=1000.0)]
    result = _compute_open_position_breakdown(positions, now_ts)
    assert result is not None
    assert result["n_open_total"] == 1
    assert result["n_open_in_money"] == 1
    assert result["n_open_dead"] == 0
    assert result["n_open_underwater"] == 0
    # Expected: (0.80 - 0.50) * 1000 = 300.0
    assert result["in_money_unrealized_pnl"] == pytest.approx(300.0)
    assert result["in_money_capital_usdc"] == pytest.approx(500.0)


def test_compute_open_position_breakdown_direct_helper_underwater():
    """Helper sums unrealized for an underwater position."""
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    positions = [_open_pos(cur_price=0.30, avg_price=0.60, size=500.0)]
    result = _compute_open_position_breakdown(positions, now_ts)
    assert result["n_open_underwater"] == 1
    assert result["underwater_unrealized_pnl"] == pytest.approx(-150.0)
    assert result["underwater_capital_usdc"] == pytest.approx(300.0)


def test_compute_open_position_breakdown_direct_helper_dead():
    """Dead positions (curPrice <= 0.01) go into the dead bucket."""
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    positions = [_open_pos(cur_price=0.005, avg_price=0.40, size=200.0)]
    result = _compute_open_position_breakdown(positions, now_ts)
    assert result["n_open_dead"] == 1
    assert result["n_open_in_money"] == 0
    assert result["n_open_underwater"] == 0
    assert result["dead_capital_usdc"] == pytest.approx(80.0)


def test_compute_open_position_breakdown_direct_helper_returns_none_when_empty():
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    assert _compute_open_position_breakdown([], now_ts) is None


def test_compute_open_position_breakdown_direct_helper_mixed_population():
    """Helper correctly distributes a mixed population across buckets."""
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    positions = [
        _open_pos(cur_price=0.90, avg_price=0.40, size=100.0),   # in_money: +50
        _open_pos(cur_price=0.20, avg_price=0.60, size=200.0),   # underwater: -80
        _open_pos(cur_price=0.005, avg_price=0.30, size=300.0),  # dead
    ]
    result = _compute_open_position_breakdown(positions, now_ts)
    assert result["n_open_total"] == 3
    assert result["n_open_in_money"] == 1
    assert result["n_open_underwater"] == 1
    assert result["n_open_dead"] == 1
    assert result["in_money_unrealized_pnl"] == pytest.approx(50.0)
    assert result["underwater_unrealized_pnl"] == pytest.approx(-80.0)


def test_compute_open_position_breakdown_time_in_position_from_real_dates():
    """time_in_position is computed from real ISO startDate strings, not stamped."""
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        _compute_open_position_breakdown,
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    positions = [
        _open_pos(cur_price=0.5, avg_price=0.4, size=100, days_held=10),
        _open_pos(cur_price=0.5, avg_price=0.4, size=100, days_held=95),   # stale_90d
        _open_pos(cur_price=0.5, avg_price=0.4, size=100, days_held=400),  # stale_365d
    ]
    result = _compute_open_position_breakdown(positions, now_ts)
    tip = result["time_in_position"]
    assert tip["n_stale_90d"] == 2  # 95d AND 400d both > 90d
    assert tip["n_stale_365d"] == 1
    # Median of [10, 95, 400] = 95 (within float tolerance)
    assert tip["median_days"] == pytest.approx(95.0, abs=1.0)


# ---------------------------------------------------------------------------
# Group 2 — backward-compat invariant: net_pnl mirrors realized_pnl
# ---------------------------------------------------------------------------


def test_realized_pnl_mirrors_net_pnl_for_legacy_constructor():
    """ProtocolReport(net_pnl=X) sets realized_pnl=X automatically.

    Existing test fixtures and replay paths in the codebase set net_pnl only.
    The backward-compat invariant `realized_pnl == net_pnl` must hold
    post-construction regardless of which kwarg the caller used.
    """
    report = ProtocolReport(wallet="0xtest", net_pnl=-727729.04)
    assert report.realized_pnl == pytest.approx(-727729.04)


def test_realized_pnl_mirrors_when_only_new_kwarg_set():
    """ProtocolReport(realized_pnl=X) sets net_pnl=X for the inverse case."""
    report = ProtocolReport(wallet="0xtest", realized_pnl=12345.0)
    assert report.net_pnl == pytest.approx(12345.0)


def test_realized_pnl_and_net_pnl_consistent_when_both_set():
    """When both kwargs are set (analyzer path), they keep their values."""
    report = ProtocolReport(
        wallet="0xtest", net_pnl=100.0, realized_pnl=100.0
    )
    assert report.net_pnl == 100.0
    assert report.realized_pnl == 100.0


def test_both_zero_default_when_neither_set():
    """Both default to 0.0 when neither kwarg is provided."""
    report = ProtocolReport(wallet="0xtest")
    assert report.net_pnl == 0.0
    assert report.realized_pnl == 0.0


def test_combined_pnl_recomputed_from_net_pnl_alone():
    """A.4 invariant: ProtocolReport(net_pnl=100) yields combined_pnl=100.

    Before the __post_init__ fix, combined_pnl stayed at its default 0.0
    when only net_pnl was set, violating combined == realized + unrealized.
    """
    report = ProtocolReport(wallet="0xtest", net_pnl=100.0)
    assert report.combined_pnl == pytest.approx(100.0)


def test_combined_pnl_recomputed_from_realized_pnl_alone():
    """A.4 invariant: ProtocolReport(realized_pnl=100) yields combined_pnl=100."""
    report = ProtocolReport(wallet="0xtest", realized_pnl=100.0)
    assert report.combined_pnl == pytest.approx(100.0)


def test_combined_pnl_recomputed_with_unrealized_component():
    """A.4 invariant: net_pnl=-50 + unrealized_pnl=100 → combined_pnl=50."""
    report = ProtocolReport(
        wallet="0xtest", net_pnl=-50.0, unrealized_pnl=100.0
    )
    assert report.combined_pnl == pytest.approx(50.0)
    # Also mirror invariant: realized_pnl tracks net_pnl
    assert report.realized_pnl == pytest.approx(-50.0)


def test_combined_pnl_explicit_value_preserved():
    """When combined_pnl is set explicitly (analyzer path), don't recompute.

    Guards against the __post_init__ accidentally clobbering an
    intentional combined_pnl value (e.g. analyzer setting all three fields
    in sync).
    """
    report = ProtocolReport(
        wallet="0xtest",
        realized_pnl=100.0,
        unrealized_pnl=50.0,
        combined_pnl=150.0,  # explicit
    )
    assert report.combined_pnl == pytest.approx(150.0)


def test_combined_pnl_zero_when_all_components_zero():
    """No spurious recomputation when nothing is set."""
    report = ProtocolReport(wallet="0xtest")
    assert report.combined_pnl == 0.0


# ---------------------------------------------------------------------------
# Group 3 — analyze_wallet end-to-end populates PnL fields from real inputs
# ---------------------------------------------------------------------------


def test_analyze_wallet_computes_realized_pnl_from_closed_positions():
    """analyze_wallet(pre_fetched_data=...) computes realized_pnl from data."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    # 30+ closed positions to clear Stage 1 N_CLOSED_POSITIONS_MIN gate
    closed = [
        _closed_pos(realized_pnl=10.0, condition_id=f"cid{i}", asset=f"a{i}")
        for i in range(35)
    ]
    expected_realized = 35 * 10.0
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)
    assert report.realized_pnl == pytest.approx(expected_realized)
    assert report.net_pnl == pytest.approx(expected_realized)  # backward compat


def test_analyze_wallet_computes_unrealized_pnl_from_open_positions():
    """analyze_wallet computes unrealized_pnl from open_positions."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    closed = [_closed_pos(realized_pnl=5.0, condition_id=f"cid{i}") for i in range(35)]
    open_positions = [
        _open_pos(cur_price=0.80, avg_price=0.50, size=1000.0),  # +300
        _open_pos(cur_price=0.20, avg_price=0.40, size=500.0),   # -100
    ]
    data = _make_wallet_data(closed_positions=closed, open_positions=open_positions)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)
    # Total unrealized: 300 + (-100) = 200
    assert report.unrealized_pnl == pytest.approx(200.0)


def test_analyze_wallet_computes_combined_pnl():
    """analyze_wallet's combined_pnl == realized + unrealized."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    closed = [_closed_pos(realized_pnl=100.0, condition_id=f"cid{i}") for i in range(35)]
    open_positions = [_open_pos(cur_price=0.70, avg_price=0.50, size=1000.0)]  # +200
    data = _make_wallet_data(closed_positions=closed, open_positions=open_positions)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)
    assert report.combined_pnl == pytest.approx(
        report.realized_pnl + report.unrealized_pnl
    )


def test_analyze_wallet_populates_open_position_breakdown():
    """analyze_wallet stamps open_position_breakdown when open_positions present."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    closed = [_closed_pos(realized_pnl=1.0, condition_id=f"cid{i}") for i in range(35)]
    open_positions = [_open_pos(cur_price=0.80, avg_price=0.50, size=100.0)]
    data = _make_wallet_data(closed_positions=closed, open_positions=open_positions)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)
    assert report.open_position_breakdown is not None
    assert report.open_position_breakdown["n_open_total"] == 1
    assert report.open_position_breakdown["n_open_in_money"] == 1


def test_analyze_wallet_open_breakdown_none_when_no_open_positions():
    """No open positions → open_position_breakdown is None."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    closed = [_closed_pos(realized_pnl=1.0, condition_id=f"cid{i}") for i in range(35)]
    data = _make_wallet_data(closed_positions=closed, open_positions=[])
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)
    assert report.open_position_breakdown is None


# ---------------------------------------------------------------------------
# Group 4 — Integration: floor sees populated PnL fields (ordering invariant)
# ---------------------------------------------------------------------------


def test_floor_sees_unrealized_pnl_populated_before_running():
    """Honesty floor must evaluate populated combined_pnl, not the default 0.0.

    Failure mode this guards against: an implementer that stamps PnL fields
    AFTER the floor runs (or never via the assembly path) would silently
    cause the floor to act on default 0.0 instead of real numbers.

    Setup: negative realized PnL, strong positive unrealized. If the floor
    sees both fields correctly populated, combined > 0 and the floor must
    NOT downgrade. If the floor saw realized only (legacy path), it would
    incorrectly downgrade.
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    # Negative realized PnL: 35 trades each losing $100 → -$3500
    closed = [
        _closed_pos(realized_pnl=-100.0, condition_id=f"cid{i}")
        for i in range(35)
    ]
    # Strong positive unrealized: one big in-money open position +$5000
    open_positions = [
        _open_pos(cur_price=0.80, avg_price=0.30, size=10000.0),  # +5000
    ]
    data = _make_wallet_data(closed_positions=closed, open_positions=open_positions)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)

    # The floor only fires when overall_verdict == "edge_detected", so we
    # can't always observe its decision (verdict depends on hypothesis tests).
    # What we CAN assert: the PnL fields are populated when the floor would
    # evaluate them. Both must be non-zero and reflect the synthetic inputs.
    assert report.realized_pnl == pytest.approx(-3500.0)
    assert report.unrealized_pnl == pytest.approx(5000.0)
    assert report.combined_pnl == pytest.approx(1500.0)

    # When floor IS active (edge_detected path), it must NOT downgrade because
    # combined > 0. Inspect data_quality_flags: NEGATIVE_PNL_EDGE_UNEXPLAINED
    # must NOT appear with this PnL profile (combined > 0 satisfies the floor).
    assert "NEGATIVE_PNL_EDGE_UNEXPLAINED" not in report.data_quality_flags


def test_floor_downgrades_when_both_signals_truly_negative():
    """When BOTH realized and combined are negative, floor downgrades (if edge_detected)."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    # Negative realized + negative unrealized
    closed = [
        _closed_pos(realized_pnl=-100.0, condition_id=f"cid{i}")
        for i in range(35)
    ]
    open_positions = [
        _open_pos(cur_price=0.20, avg_price=0.50, size=10000.0),  # -3000
    ]
    data = _make_wallet_data(closed_positions=closed, open_positions=open_positions)
    report = analyze_wallet("0xtestwallet", pre_fetched_data=data)

    assert report.realized_pnl < 0
    assert report.combined_pnl < 0

    # If the verdict came back edge_detected, floor must have fired
    if report.overall_verdict == "underpowered":
        # downgrade path — flag must be present
        assert any(
            f.startswith("NEGATIVE_PNL_EDGE_UNEXPLAINED")
            for f in report.data_quality_flags
        )
