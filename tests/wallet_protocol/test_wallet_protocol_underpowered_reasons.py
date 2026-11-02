"""I2 closure: every underpowered verdict carries a reason-band.

Spec: docs/superpowers/specs/wallet-protocol-bulletproofing.md (I2).

For each path that produces `verdict="underpowered"` in held_analyst.py and
flipper.py, assert `underpowered_reason` is populated with the right band.
"""

from __future__ import annotations

import time

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import (
    FH1_MIN_N,
    FH2_MIN_N,
    FH6_MIN_N,
    run_fh1_mean_flip_roi,
    run_fh2_size_conviction,
    run_fh3_spread_capture,
    run_fh6_directional_skill,
)
from sniper.analysis.wallet_protocol.held_analyst import (
    H2_MIN_N,
    H3_MIN_N,
    run_h1_net_pnl,
    run_h2_excess_wr,
    run_h3_size_conviction,
    run_h4_entry_timing,
    run_h6_exit_quality,
)


def _now() -> int:
    return int(time.time())


def _pos(curPrice=1.0, avgPrice=0.5, size=100, realizedPnl=10.0, ts=None):
    return {
        "conditionId": "cid",
        "outcome": "Yes",
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts if ts is not None else _now(),
        "eventSlug": "slug",
    }


def _flip(buy_price=0.4, sell_price=0.5, buy_ts=1000, sell_ts=2000,
          asset_id=""):
    roi = (sell_price - buy_price) / buy_price
    return Flip(
        condition_id="c",
        outcome="Yes",
        buy_ts=buy_ts,
        sell_ts=sell_ts,
        buy_price=buy_price,
        sell_price=sell_price,
        size=100,
        buy_notional=buy_price * 100,
        flip_roi=roi,
        hold_duration_seconds=sell_ts - buy_ts,
        asset_id=asset_id,
    )


# ---------------------------------------------------------------------------
# Held branch
# ---------------------------------------------------------------------------


def test_h1_underpowered_n_below_threshold():
    h = run_h1_net_pnl([], n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_h2_underpowered_n_below_threshold():
    positions = [_pos() for _ in range(H2_MIN_N - 1)]
    h = run_h2_excess_wr(positions, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_h3_underpowered_n_below_threshold():
    positions = [_pos() for _ in range(H3_MIN_N - 1)]
    h = run_h3_size_conviction(positions, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_h4_underpowered_no_execution_profile_when_none():
    h = run_h4_entry_timing(None)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_execution_profile"


def test_h4_underpowered_no_execution_profile_when_status_not_populated():
    h = run_h4_entry_timing({"status": "not_run"})
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_execution_profile"


def test_h4_underpowered_no_execution_profile_when_sample_tier_unknown():
    h = run_h4_entry_timing({"status": "populated", "sample_tier": "unknown"})
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_execution_profile"


def test_h4_underpowered_n_below_threshold_when_drifts_too_few():
    h = run_h4_entry_timing(
        {"status": "populated", "sample_tier": "silver", "drifts_6h": [0.01, 0.02]}
    )
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_h6_underpowered_no_price_history_fn():
    activity = [{"side": "SELL", "price": 0.6, "timestamp": _now(),
                 "conditionId": "c", "asset": "a"}] * 30
    h = run_h6_exit_quality(activity, None)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_price_history_fn"


def test_h6_underpowered_n_below_threshold_too_few_sells():
    activity = [{"side": "SELL", "price": 0.6, "timestamp": _now(),
                 "conditionId": "c", "asset": "a"}] * 3

    def _fake(market_id, ts):
        return 0.5

    h = run_h6_exit_quality(activity, _fake)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_h6_underpowered_n_below_threshold_when_coverage_low():
    activity = [
        {"side": "SELL", "price": 0.7, "timestamp": _now() + i,
         "conditionId": f"c{i}", "asset": f"a{i}"}
        for i in range(30)
    ]

    def _no_history(market_id, ts):
        return None

    h = run_h6_exit_quality(activity, _no_history)
    assert h.verdict == "underpowered"
    # Examined count fell below the test floor due to zero coverage.
    assert h.underpowered_reason == "n_below_threshold"


# ---------------------------------------------------------------------------
# Flipper branch
# ---------------------------------------------------------------------------


def test_fh1_underpowered_n_below_threshold():
    flips = [_flip() for _ in range(FH1_MIN_N - 1)]
    h = run_fh1_mean_flip_roi(flips, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_fh2_underpowered_n_below_threshold():
    flips = [_flip() for _ in range(FH2_MIN_N - 1)]
    h = run_fh2_size_conviction(flips, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_fh3_underpowered_n_below_threshold():
    flips = [_flip() for _ in range(FH1_MIN_N - 1)]
    h = run_fh3_spread_capture(flips, execution_profile=None, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_fh6_underpowered_n_below_threshold():
    flips = [_flip() for _ in range(FH6_MIN_N - 1)]
    h = run_fh6_directional_skill(flips, price_history_fn=None, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "n_below_threshold"


def test_fh6_underpowered_no_price_history_fn():
    flips = [_flip() for _ in range(FH6_MIN_N + 5)]
    h = run_fh6_directional_skill(flips, price_history_fn=None, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_price_history_fn"


def test_fh6_underpowered_no_price_history_fn_low_coverage():
    flips = [
        _flip(buy_price=0.4, sell_price=0.5, buy_ts=1000 + i, sell_ts=2000 + i)
        for i in range(FH6_MIN_N + 5)
    ]

    def _missing(*_args, **_kwargs):
        return None

    h = run_fh6_directional_skill(flips, price_history_fn=_missing, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "no_price_history_fn"


def test_fh6_underpowered_zero_variance_when_one_direction_empty():
    """All flips win and market always moves WITH the wallet → against_market is
    empty after stratification. Path returns underpowered with zero_variance.
    Provides asset_id on every flip so the C5-follow-on coverage gate doesn't
    fire first."""
    n = FH6_MIN_N + 5
    flips = [
        _flip(buy_price=0.4, sell_price=0.5, buy_ts=1000 + i, sell_ts=2000 + i,
              asset_id=f"asset_{i}")
        for i in range(n)
    ]

    def _market_up(asset_id, ts):
        # entry < exit always — market moved up. All wallet flips are YES so
        # market_dir > 0 for every observation, leaving against_market empty.
        return 0.4 if ts < 1500 else 0.6

    h = run_fh6_directional_skill(flips, price_history_fn=_market_up, n_iter=100)
    assert h.verdict == "underpowered"
    assert h.underpowered_reason == "zero_variance"


# ---------------------------------------------------------------------------
# Schema sanity: non-underpowered verdicts must not carry a reason.
# ---------------------------------------------------------------------------


def test_supported_verdicts_have_no_reason():
    """When H1 fires positively, underpowered_reason stays None."""
    positions = [_pos(curPrice=1.0, avgPrice=0.30, realizedPnl=70.0) for _ in range(40)]
    h = run_h1_net_pnl(positions, n_iter=300)
    # Outcome may be supported or inconclusive depending on bootstrap;
    # the contract is just that non-underpowered → no reason.
    if h.verdict != "underpowered":
        assert h.underpowered_reason is None
