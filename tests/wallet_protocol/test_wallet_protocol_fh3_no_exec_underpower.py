"""FH3 must underpower when no execution cost surface exists."""
from __future__ import annotations

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import (
    FH1_MIN_N,
    run_fh3_spread_capture,
)


def _flip(idx: int) -> Flip:
    return Flip(
        condition_id=f"0xcid{idx}",
        outcome="Yes",
        asset_id=f"asset{idx}",
        buy_ts=1700000000 + idx * 60,
        sell_ts=1700000000 + idx * 60 + 3600,
        buy_price=0.5,
        sell_price=0.6,
        size=100.0,
        buy_notional=100.0,
        flip_roi=0.20,
        hold_duration_seconds=3600,
    )


def test_fh3_underpowered_with_no_execution_profile():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    result = run_fh3_spread_capture(
        flips, execution_profile=None, n_iter=200, seed=1,
    )

    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "no_execution_profile"


def test_fh3_underpowered_with_execution_profile_but_no_cost_surface():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    result = run_fh3_spread_capture(
        flips, execution_profile={"status": "populated"}, n_iter=200, seed=1,
    )

    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "no_execution_profile"


def test_fh3_runs_normally_with_round_trip_cost_estimate():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    result = run_fh3_spread_capture(
        flips, execution_profile={"round_trip_cost_estimate": 0.05},
        n_iter=200, seed=1,
    )

    assert result.verdict in ("supported", "refuted", "inconclusive")
    assert result.underpowered_reason is None
