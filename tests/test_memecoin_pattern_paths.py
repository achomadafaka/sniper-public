from datetime import datetime, timedelta, timezone

import pytest

from sniper.research.memecoin_strategy.pattern_paths import (
    analyze_path,
    classify_observed_path,
    classify_path,
)


START = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _quote(index, minutes, price, quality="ok"):
    return {
        "id": index,
        "observed_at": (START + timedelta(minutes=minutes)).isoformat(),
        "price": price,
        "quality": quality,
    }


def _metrics(prices, *, complete=True):
    quotes = [_quote(index, index, price) for index, price in enumerate(prices)]
    return analyze_path(1.0, START.isoformat(), quotes, complete=complete)


def test_analyze_path_rejects_naive_time_and_ignores_bad_quotes():
    with pytest.raises(ValueError, match="timezone aware"):
        analyze_path(1.0, "2026-07-11T00:00:00", [], complete=True)

    metrics = analyze_path(
        1.0,
        START.isoformat(),
        [_quote(1, 1, 100.0, quality="ratio_outlier"), _quote(2, 2, 1.6)],
        complete=True,
    )
    assert metrics.max_gain_pct == pytest.approx(60)
    assert metrics.quality_excluded == 1
    assert metrics.observation_count == 1


def test_same_timestamp_order_is_conservative_and_metrics_preserve_path_order():
    at = (START + timedelta(minutes=1)).isoformat()
    metrics = analyze_path(
        1.0,
        START.isoformat(),
        [
            {"id": 1, "observed_at": at, "price": 1.8, "quality": "ok"},
            {"id": 2, "observed_at": at, "price": 0.5, "quality": "ok"},
            _quote(3, 2, 2.0),
        ],
        complete=True,
    )
    assert metrics.max_drawdown_pct == pytest.approx(-50)
    assert metrics.reclaimed_entry
    assert metrics.new_high_after_trough
    assert metrics.time_to_trough_s == 60
    assert metrics.time_to_peak_s == 120


def test_nonfinite_nonpositive_and_before_signal_quotes_are_excluded():
    metrics = analyze_path(
        1.0,
        START.isoformat(),
        [
            _quote(1, -1, 0.9),
            _quote(2, 1, 0),
            _quote(3, 2, float("nan")),
            _quote(4, 3, 1.2),
        ],
        complete=True,
    )
    assert metrics.observation_count == 1
    assert metrics.input_excluded == 3
    assert metrics.terminal_return_pct == pytest.approx(20)


def test_entry_is_zero_return_baseline_and_rising_path_is_not_a_recovery():
    metrics = _metrics([1.1, 1.2, 1.3])
    assert metrics.max_drawdown_pct == 0
    assert not metrics.reclaimed_entry
    assert not metrics.new_high_after_trough


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ([1.0, 1.61, 1.30], "immediate_runner"),
        ([1.0, 1.4, 0.6, 1.5], "deep_recovery_new_high"),
        ([1.0, 1.4, 0.6, 1.1], "deep_recovery"),
        ([1.0, 1.3, 0.7, 0.8], "pump_fade"),
        ([1.0, 0.8, 0.6], "terminal_failure"),
        ([1.0, 1.1, 1.05], "slow_or_unresolved"),
    ],
)
def test_archetypes_are_deterministic(prices, expected):
    assert classify_path(_metrics(prices)) == expected


def test_incomplete_path_is_never_given_terminal_archetype():
    metrics = _metrics([1.0, 1.4, 0.5, 1.6], complete=False)
    assert classify_path(metrics) == "incomplete"
    assert classify_observed_path(metrics) == "observed_deep_recovery_new_high"


def test_observed_shape_keeps_unresolved_drawdown_provisional():
    metrics = _metrics([1.0, 0.5, 0.6], complete=False)
    assert classify_observed_path(metrics) == "observed_deep_drawdown_unresolved"


def test_entry_price_must_be_finite_and_positive():
    for value in (None, 0, -1, float("nan")):
        with pytest.raises(ValueError, match="entry_price"):
            analyze_path(value, START.isoformat(), [], complete=True)
