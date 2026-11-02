"""Tests for §4.7 flip reconstruction + §5 mode router."""

import time

from sniper.analysis.wallet_protocol.flip_reconstruction import (
    Flip,
    reconstruct_flips,
)
from sniper.analysis.wallet_protocol.mode_router import (
    FLIP_RATE_FLIPPER_ONLY_MIN,
    FLIP_RATE_HELD_ONLY_MAX,
    classify_mode,
)
from sniper.analysis.wallet_protocol.types import WalletData


def _act(side, cid="cid_a", outcome="Yes", price=0.5, size=100, ts=None,
         asset=None):
    row = {
        "side": side,
        "conditionId": cid,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts if ts is not None else int(time.time()),
    }
    if asset is not None:
        row["asset"] = asset
    return row


def _make_data(activity, closed=None, cap_hit=False):
    return WalletData(
        wallet="0xabc",
        closed_positions=closed or [],
        open_positions=[],
        activity_rows=activity,
        activity_cap_hit=cap_hit,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
    )


# ----------------------------------------------------------------------------
# Flip FIFO matching
# ----------------------------------------------------------------------------


def test_simple_buy_then_sell_yields_one_flip():
    activity = [
        _act("BUY", price=0.50, size=100, ts=1000),
        _act("SELL", price=0.60, size=100, ts=2000),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 1
    f = result.flips[0]
    assert f.size == 100
    assert f.buy_price == 0.50
    assert f.sell_price == 0.60
    assert abs(f.flip_roi - 0.20) < 1e-9
    assert f.hold_duration_seconds == 1000


def test_buy_only_yields_no_flip():
    activity = [_act("BUY", price=0.5, size=100, ts=1000)]
    result = reconstruct_flips(_make_data(activity, closed=[]))
    assert result.n_flips == 0


def test_partial_sell_yields_partial_flip():
    """BUY 100 @ 0.50, then SELL 30 @ 0.60. One flip of size 30."""
    activity = [
        _act("BUY", price=0.50, size=100, ts=1000),
        _act("SELL", price=0.60, size=30, ts=2000),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 1
    assert result.flips[0].size == 30


def test_two_buys_one_sell_consumes_fifo():
    """BUY 50 @ 0.40, BUY 50 @ 0.60, SELL 100 @ 0.55.
    Two flips: 50 @ entry 0.40, 50 @ entry 0.60.
    """
    activity = [
        _act("BUY", price=0.40, size=50, ts=1000),
        _act("BUY", price=0.60, size=50, ts=1500),
        _act("SELL", price=0.55, size=100, ts=2000),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 2
    flips = sorted(result.flips, key=lambda f: f.buy_price)
    assert flips[0].buy_price == 0.40 and abs(flips[0].flip_roi - 0.375) < 1e-9
    assert flips[1].buy_price == 0.60 and abs(flips[1].flip_roi - (-0.0833333)) < 1e-6


def test_separate_positions_dont_cross_match():
    """BUY on cid_a should never be consumed by SELL on cid_b."""
    activity = [
        _act("BUY", cid="cid_a", price=0.50, size=100, ts=1000),
        _act("SELL", cid="cid_b", price=0.60, size=100, ts=2000),
    ]
    result = reconstruct_flips(_make_data(
        activity,
        closed=[
            {"conditionId": "cid_a", "outcome": "Yes"},
            {"conditionId": "cid_b", "outcome": "Yes"},
        ],
    ))
    assert result.n_flips == 0  # neither cid has a complete BUY+SELL match


def test_no_position_match_drops_orphan_sell():
    """SELL with no preceding BUY in the activity window — dropped silently."""
    activity = [_act("SELL", price=0.60, size=100, ts=2000)]
    result = reconstruct_flips(_make_data(activity))
    assert result.n_flips == 0


# ----------------------------------------------------------------------------
# Flip rate
# ----------------------------------------------------------------------------


def test_flip_rate_zero_when_no_pre_resolution_sells():
    activity = [_act("BUY", price=0.5, size=100, ts=1000)]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.flip_rate == 0.0


def test_flip_rate_one_when_all_positions_have_sells():
    activity = [
        _act("BUY", cid="cid_a", price=0.5, size=100, ts=1000),
        _act("SELL", cid="cid_a", price=0.6, size=100, ts=2000),
        _act("BUY", cid="cid_b", price=0.5, size=100, ts=3000),
        _act("SELL", cid="cid_b", price=0.6, size=100, ts=4000),
    ]
    result = reconstruct_flips(_make_data(
        activity,
        closed=[
            {"conditionId": "cid_a", "outcome": "Yes"},
            {"conditionId": "cid_b", "outcome": "Yes"},
        ],
    ))
    assert result.flip_rate == 1.0


# ----------------------------------------------------------------------------
# Cap-hit truncation
# ----------------------------------------------------------------------------


def test_truncation_flag_when_high_flip_rate_and_cap_hit():
    activity = [
        _act("BUY", cid="cid_a", price=0.5, size=100, ts=1000),
        _act("SELL", cid="cid_a", price=0.6, size=100, ts=2000),
    ]
    closed = [{"conditionId": f"cid_{i}", "outcome": "Yes"} for i in ["a"]]
    result = reconstruct_flips(_make_data(activity, closed=closed, cap_hit=True))
    assert result.flip_rate == 1.0
    assert result.activity_cap_hit
    assert result.flipper_analysis_truncated


def test_truncation_flag_off_when_low_flip_rate_even_if_cap_hit():
    activity = [
        _act("BUY", cid="cid_a", price=0.5, size=100, ts=1000),
        # 9 other closed positions with no SELL events
    ]
    closed = [{"conditionId": f"cid_{i}", "outcome": "Yes"} for i in range(10)]
    result = reconstruct_flips(_make_data(activity, closed=closed, cap_hit=True))
    assert result.flip_rate == 0.0
    assert result.activity_cap_hit
    assert not result.flipper_analysis_truncated


# ----------------------------------------------------------------------------
# Mode router
# ----------------------------------------------------------------------------


def test_mode_held_only_below_threshold():
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flips=[], n_flips=0, flip_rate=0.20, n_pre_resolution_sells=2,
        n_closed_positions=10, activity_cap_hit=False,
        flipper_analysis_truncated=False,
    ))
    assert decision.mode == "held_analyst_only"
    assert decision.run_held_branch
    assert not decision.run_flipper_branch


def test_mode_flipper_only_above_threshold():
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flips=[], n_flips=0, flip_rate=0.85, n_pre_resolution_sells=85,
        n_closed_positions=100, activity_cap_hit=False,
        flipper_analysis_truncated=False,
    ))
    assert decision.mode == "flipper_only"
    assert not decision.run_held_branch
    assert decision.run_flipper_branch


def test_mode_mixed_mode_in_middle_band():
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flips=[], n_flips=0, flip_rate=0.50, n_pre_resolution_sells=50,
        n_closed_positions=100, activity_cap_hit=False,
        flipper_analysis_truncated=False,
    ))
    assert decision.mode == "mixed_mode"
    assert decision.run_held_branch
    assert decision.run_flipper_branch


def test_truncation_routes_to_held_only():
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flips=[], n_flips=0, flip_rate=0.85, n_pre_resolution_sells=85,
        n_closed_positions=100, activity_cap_hit=True,
        flipper_analysis_truncated=True,
    ))
    assert decision.mode == "held_analyst_only"
    assert decision.flipper_truncated
    assert not decision.run_flipper_branch
    assert decision.confidence == "low"


def test_threshold_constants_match_spec():
    """Locked at 0.30 / 0.70 per protocol v2 §5. Change = v3."""
    assert FLIP_RATE_HELD_ONLY_MAX == 0.30
    assert FLIP_RATE_FLIPPER_ONLY_MIN == 0.70


def test_mode_at_exact_lower_boundary_is_mixed():
    """flip_rate=0.30 → not strictly below 0.30, so mixed_mode."""
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flip_rate=0.30, n_pre_resolution_sells=3, n_closed_positions=10,
    ))
    # spec uses strict < and >, so 0.30 is in the middle band
    assert decision.mode == "mixed_mode"


def test_flip_carries_buy_side_asset_id():
    """C5 follow-on: Flip.asset_id is captured from the BUY row's `asset`."""
    activity = [
        _act("BUY", price=0.50, size=100, ts=1000, asset="tokenA_yes_id"),
        _act("SELL", price=0.60, size=100, ts=2000, asset="tokenA_yes_id"),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 1
    assert result.flips[0].asset_id == "tokenA_yes_id"


def test_flip_asset_id_empty_when_activity_lacks_asset_field():
    """Back-compat: rows without `asset` produce flips with asset_id=''.
    FH6 will skip these flips and may underpower if too many lack ids."""
    activity = [
        _act("BUY", price=0.50, size=100, ts=1000),
        _act("SELL", price=0.60, size=100, ts=2000),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 1
    assert result.flips[0].asset_id == ""


def test_flip_asset_id_taken_from_buy_not_sell_on_partial_match():
    """When two BUYs are consumed by one SELL, each flip carries the
    asset_id from the BUY that produced it."""
    activity = [
        _act("BUY", price=0.40, size=50, ts=1000, asset="buyA_id"),
        _act("BUY", price=0.60, size=50, ts=1500, asset="buyB_id"),
        _act("SELL", price=0.55, size=100, ts=2000, asset="sell_id"),
    ]
    result = reconstruct_flips(_make_data(
        activity, closed=[{"conditionId": "cid_a", "outcome": "Yes"}],
    ))
    assert result.n_flips == 2
    assert {f.asset_id for f in result.flips} == {"buyA_id", "buyB_id"}


def test_mode_at_exact_upper_boundary_is_mixed():
    from sniper.analysis.wallet_protocol.flip_reconstruction import (
        FlipReconstructionResult,
    )
    decision = classify_mode(FlipReconstructionResult(
        flip_rate=0.70, n_pre_resolution_sells=7, n_closed_positions=10,
    ))
    assert decision.mode == "mixed_mode"
