import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from sniper.hyperlive.rollover_research import (
    RolloverPolicy,
    attach_matched_basket,
    block_bootstrap,
    build_causal_frames,
    causal_regime_report,
    continuation_feature_report,
    historical_verdict,
    period_report,
    policy_sha256,
    replay_sampled_exits,
    simulate_rollover,
)


POLICY = {
    "policy_version": "rollover-v1",
    "excluded_coins": ["BTC", "ETH", "SOL"],
    "min_history_hours": 720,
    "min_history_coverage": 0.8,
    "return_percentile_min": 0.95,
    "runup_percentile_min": 0.95,
    "rollover_6h_bps_max": 0.0,
    "max_entries_per_24h": 5,
    "take_profit_bps": 200.0,
    "cap_hours": 96,
    "fee_bps": 9.0,
    "slippage_each_bps": 15.0,
    "min_benchmark_coverage": 0.8,
    "bootstrap_replications": 5000,
    "bootstrap_seed": 20260727,
}


def test_rollover_v1_policy_round_trips_and_derives_total_cost():
    policy = RolloverPolicy.from_dict(POLICY)

    assert policy.to_dict() == POLICY
    assert policy.round_trip_cost_bps == 39.0
    assert policy.excluded_coins == ("BTC", "ETH", "SOL")


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("return_percentile_min", 0.90),
        ("rollover_6h_bps_max", 1.0),
        ("cap_hours", 95),
        ("excluded_coins", ["BTC", "ETH"]),
        ("take_profit_bps", math.nan),
    ],
)
def test_rollover_v1_policy_rejects_changed_or_nonfinite_contract(field, invalid):
    value = {**POLICY, field: invalid}

    with pytest.raises(ValueError, match=field):
        RolloverPolicy.from_dict(value)


def test_rollover_v1_policy_rejects_missing_and_unknown_fields():
    missing = dict(POLICY)
    missing.pop("cap_hours")
    with pytest.raises(ValueError, match="cap_hours"):
        RolloverPolicy.from_dict(missing)

    with pytest.raises(ValueError, match="unexpected"):
        RolloverPolicy.from_dict({**POLICY, "unexpected": True})


def test_policy_hash_is_stable_for_canonical_policy():
    policy = RolloverPolicy.from_dict(POLICY)

    assert policy_sha256(policy) == "322225bdc0bc7d30e744f6b3c75e636e46209e66a03d072b60871d61ccf5c0c6"


HOUR_MS = 3_600_000


def _candles(*, coin_n=21, hours=830, signal_coins=("C000",), decision=721):
    times = np.arange(hours, dtype=np.int64) * HOUR_MS
    candles = {}
    for index in range(coin_n):
        coin = f"C{index:03d}"
        close = np.full(hours, 100.0)
        open_ = close.copy()
        high = close.copy()
        low = close.copy()
        volume = np.full(hours, 1000.0)
        if coin in signal_coins:
            strength = 6.0 + signal_coins.index(coin)
            close[decision - 6:decision] = 100.0 + strength
            close[decision] = 100.0 + strength - 1.0
            open_[decision + 1] = close[decision]
            close[decision + 1] = close[decision] - 1.0
            if decision + 2 < hours:
                close[decision + 2] = close[decision] - 4.0
            high = np.maximum(open_, close)
            low = np.minimum(open_, close)
            volume[decision] = 5000.0
        candles[coin] = pd.DataFrame(
            {"t": times, "o": open_, "h": high, "l": low, "c": close, "v": volume}
        )
    return candles


def _funding(candles, rate=0.00001):
    return {
        coin: pd.DataFrame({"time": frame["t"] + 85, "rate": rate})
        for coin, frame in candles.items()
    }


def test_signal_is_causal_enters_next_open_and_exits_on_sampled_close():
    policy = RolloverPolicy.from_dict(POLICY)
    candles = _candles()
    changed_future = {coin: frame.copy() for coin, frame in candles.items()}
    changed_future["C000"].loc[730, ["o", "h", "l", "c"]] = 250.0

    base = simulate_rollover(build_causal_frames(candles, policy), _funding(candles), policy)
    future = simulate_rollover(
        build_causal_frames(changed_future, policy), _funding(changed_future), policy
    )

    event = next(row for row in base if row["decision_ts_ms"] == 721 * HOUR_MS)
    future_event = next(row for row in future if row["decision_ts_ms"] == 721 * HOUR_MS)
    assert event["coin"] == "C000"
    assert event["entry_ts_ms"] == 722 * HOUR_MS
    assert event["entry_mark"] == 105.0
    assert event["exit_ts_ms"] == 723 * HOUR_MS
    assert event["exit_mark"] == 101.0
    assert event["exit_reason"] == "take_profit"
    assert event["funding_bps"] == pytest.approx(0.1)
    assert event["net_bps"] == pytest.approx((105.0 - 101.0) / 105.0 * 10_000 + 0.1 - 39.0)
    assert future_event["decision_features"] == event["decision_features"]
    assert future_event["entry_mark"] == event["entry_mark"]


def test_signal_quota_admits_five_strongest_crossings():
    policy = RolloverPolicy.from_dict(POLICY)
    signal_coins = tuple(f"C{index:03d}" for index in range(6))
    candles = _candles(coin_n=121, signal_coins=signal_coins)

    events = simulate_rollover(build_causal_frames(candles, policy), _funding(candles), policy)
    admitted = [row["coin"] for row in events if row["decision_ts_ms"] == 721 * HOUR_MS]

    assert admitted == ["C005", "C004", "C003", "C002", "C001"]


def test_execution_gate_runs_after_nomination_and_does_not_create_late_retrigger():
    policy = RolloverPolicy.from_dict(POLICY)
    signal_coins = tuple(f"C{index:03d}" for index in range(6))
    candles = _candles(coin_n=121, signal_coins=signal_coins)
    frames = build_causal_frames(candles, policy)
    execution_gate = pd.DataFrame(True, index=frames.index, columns=frames.coins)
    execution_gate.loc[721 * HOUR_MS, "C005"] = False
    execution_gate.loc[722 * HOUR_MS, "C005"] = True

    events = simulate_rollover(
        frames, _funding(candles), policy, execution_gate=execution_gate
    )
    admitted = [row["coin"] for row in events if row["decision_ts_ms"] == 721 * HOUR_MS]

    assert admitted == ["C004", "C003", "C002", "C001"]
    assert not any(row["coin"] == "C005" for row in events)
    assert not any(row["coin"] == "C000" for row in events)

    replacement_events = simulate_rollover(
        frames, _funding(candles), policy, candidate_gate=execution_gate
    )
    replacements = [
        row["coin"]
        for row in replacement_events
        if row["decision_ts_ms"] == 721 * HOUR_MS
    ]

    assert replacements == ["C004", "C003", "C002", "C001", "C000"]
    assert not any(row["coin"] == "C005" for row in replacement_events)


def test_rejected_nomination_keeps_causal_shadow_cooldown():
    policy = RolloverPolicy.from_dict(POLICY)
    candles = _candles(signal_coins=())
    frames = build_causal_frames(candles, policy)
    signal = pd.DataFrame(False, index=frames.index, columns=frames.coins)
    signal.loc[721 * HOUR_MS, "C000"] = True
    signal.loc[723 * HOUR_MS, "C000"] = True
    gate = pd.DataFrame(True, index=frames.index, columns=frames.coins)
    gate.loc[721 * HOUR_MS, "C000"] = False

    events = simulate_rollover(
        replace(frames, signal=signal),
        _funding(candles),
        policy,
        execution_gate=gate,
    )

    assert not any(row["coin"] == "C000" for row in events)


def test_signal_requires_full_cap_horizon_and_reports_censoring():
    policy = RolloverPolicy.from_dict(POLICY)
    candles = _candles(hours=800, decision=798)

    events = simulate_rollover(build_causal_frames(candles, policy), _funding(candles), policy)
    event = next(row for row in events if row["decision_ts_ms"] == 798 * HOUR_MS)

    assert event["status"] == "censored"
    assert event["exclusion_reason"] == "insufficient_exit_horizon"
    assert event["net_bps"] is None


def test_exit_replay_uses_sampled_stop_or_shorter_cap_without_changing_entry():
    policy = RolloverPolicy.from_dict(POLICY)
    candles = _candles()
    candles["C000"].loc[723, ["o", "h", "l", "c"]] = 111.0
    frames = build_causal_frames(candles, policy)
    source = [{
        "coin": "C000",
        "decision_ts_ms": 721 * HOUR_MS,
        "entry_ts_ms": 722 * HOUR_MS,
        "entry_mark": 105.0,
        "decision_features": {},
    }]

    stopped = replay_sampled_exits(
        source, frames, _funding(candles), policy, cap_hours=24, stop_loss_bps=500.0
    )[0]
    capped = replay_sampled_exits(
        source, frames, _funding(candles), policy, cap_hours=1
    )[0]

    assert stopped["entry_ts_ms"] == source[0]["entry_ts_ms"]
    assert stopped["exit_ts_ms"] == 723 * HOUR_MS
    assert stopped["exit_reason"] == "stop_loss"
    assert stopped["gross_bps"] == pytest.approx((105.0 - 111.0) / 105.0 * 10_000)
    assert capped["exit_ts_ms"] == 723 * HOUR_MS
    assert capped["exit_reason"] == "time_cap"


def test_matched_basket_uses_same_clock_excludes_signal_and_charges_same_cost():
    policy = RolloverPolicy.from_dict(POLICY)
    candles = _candles()
    funding = _funding(candles)
    frames = build_causal_frames(candles, policy)
    events = simulate_rollover(frames, funding, policy)

    attached = attach_matched_basket(events, frames, funding, policy)
    event = next(row for row in attached if row["decision_ts_ms"] == 721 * HOUR_MS)

    assert event["benchmark_valid"] is True
    assert event["benchmark_eligible_n"] == 20
    assert event["benchmark_covered_n"] == 20
    assert event["benchmark_net_bps"] == pytest.approx(-38.9)
    assert event["residual_bps"] == pytest.approx(event["net_bps"] + 38.9)


def _event(day, coin, net, residual, *, reason="take_profit", basket72=0.0, feature=1.0):
    ts = int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)
    return {
        "status": "closed",
        "coin": coin,
        "entry_ts_ms": ts,
        "net_bps": float(net),
        "residual_bps": float(residual),
        "benchmark_valid": True,
        "benchmark_coverage": 1.0,
        "exit_reason": reason,
        "decision_features": {
            "basket_return_72h_bps": float(basket72),
            "volume_ratio_24h": float(feature),
        },
    }


def test_block_bootstrap_keeps_days_intact_and_is_seeded():
    events = [
        _event("2026-01-01", "A", 100, 50),
        _event("2026-01-01", "B", -100, -50),
        _event("2026-01-02", "C", 200, 100),
    ]

    first = block_bootstrap(events, "net_bps", seed=7, replications=200)
    second = block_bootstrap(events, "net_bps", seed=7, replications=200)

    assert first == second
    assert first["clusters"] == 2
    assert first["n"] == 3
    assert first["mean"] == pytest.approx(200 / 3)


def test_period_report_recomputes_concentration_and_marks_sparse_periods():
    events = [
        _event("2026-01-01", "A", 500, 300),
        _event("2026-01-02", "A", 500, 300),
        _event("2026-01-03", "B", -100, -50, reason="time_cap"),
        _event("2026-02-01", "C", 100, 50),
    ]

    rows = period_report(events, period="month", seed=3, replications=100)
    january = next(row for row in rows if row["period"] == "2026-01")
    february = next(row for row in rows if row["period"] == "2026-02")

    assert january["top_coin"] == "A"
    assert january["leave_top_coin_mean_net_bps"] == -100.0
    assert january["cap_exit_n"] == 1
    assert january["inference_status"] == "descriptive"
    assert february["entry_days"] == 1


def test_causal_regime_report_uses_trailing_entry_feature_only():
    events = [
        _event("2026-01-01", "A", 100, 50, basket72=-600),
        _event("2026-01-02", "B", 50, 25, basket72=0),
        _event("2026-01-03", "C", -25, -10, basket72=600),
    ]

    rows = causal_regime_report(events, seed=5, replications=100)

    assert {row["regime"]: row["n"] for row in rows} == {
        "bearish": 1,
        "neutral": 1,
        "bullish": 1,
    }


def test_continuation_feature_report_targets_cap_severity_and_names_missing_feeds():
    events = [
        _event("2026-01-01", "A", 200, 100, feature=1.0),
        _event("2026-01-02", "B", -1000, -900, reason="time_cap", feature=2.0),
        _event("2026-01-03", "C", -500, -400, reason="time_cap", feature=3.0),
        _event("2026-01-04", "D", 250, 120, feature=4.0),
    ]

    rows = continuation_feature_report(events)
    volume = next(row for row in rows if row["feature"] == "volume_ratio_24h")
    open_interest = next(row for row in rows if row["feature"] == "open_interest")

    assert volume["available"] is True
    assert volume["split_value"] == 2.5
    assert volume["lower_cap_exit_n"] == 1
    assert volume["upper_cap_exit_n"] == 1
    assert open_interest == {
        "feature": "open_interest",
        "available": False,
        "reason": "missing_feed",
    }


def test_historical_verdict_requires_residual_months_cost_coverage_and_provenance():
    report = {
        "overall": {
            "mean_net_bps": 30.0,
            "mean_residual_bps": 25.0,
            "ci95_residual_bps": [10.0, 100.0],
            "leave_top_coin_mean_residual_bps": 20.0,
            "benchmark_coverage": 0.95,
            "funding_coverage": 1.0,
        },
        "months": [
            {"ci95_net_bps": [1.0, 50.0]},
            {"ci95_net_bps": [2.0, 40.0]},
        ],
        "double_slippage_mean_net_bps": 5.0,
        "baselines": {
            "extreme_winner_without_rollover": {
                "mean_net_bps": 20.0,
                "mean_residual_bps": 15.0,
            }
        },
        "provenance_valid": True,
        "older_history": "blocked_paid_archive",
    }

    verdict = historical_verdict(report)
    failed = historical_verdict(
        {**report, "double_slippage_mean_net_bps": -1.0}
    )
    no_increment = historical_verdict(
        {
            **report,
            "baselines": {
                "extreme_winner_without_rollover": {
                    "mean_net_bps": 31.0,
                    "mean_residual_bps": 15.0,
                }
            },
        }
    )

    assert verdict == {
        "status": "candidate_continue",
        "passed": True,
        "blockers": ["older_history_blocked_paid_archive"],
        "failed": [],
    }
    assert failed["status"] == "candidate_reject"
    assert "double_slippage_net" in failed["failed"]
    assert "rollover_incremental_value" in no_increment["failed"]
