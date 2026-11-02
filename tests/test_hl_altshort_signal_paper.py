import pytest

from sniper.hyperlive.altshort_signal_paper import (
    assign_regime_episodes,
    causal_basket_regime,
    evaluate_sampled_exit,
    matched_forward_basket,
    regime_label,
    runup_crossing,
)


HOUR = 3_600_000


def _history(*, current=131.0, previous=120.0, hours=721):
    rows = []
    for offset in range(-hours, -1):
        rows.append({"ts_ms": offset * HOUR, "open": 100.0, "high": 100.0,
                     "low": 100.0, "close": 100.0})
    rows.append({"ts_ms": -HOUR, "open": previous, "high": previous,
                 "low": previous, "close": previous})
    rows.append({"ts_ms": 0, "open": current, "high": current,
                 "low": current, "close": current})
    return rows


def test_runup_crossing_is_causal_and_requires_upward_cross():
    rows = _history()
    result = runup_crossing(rows, decision_ts_ms=0)
    assert result["valid"] is True
    assert result["crossed"] is True
    assert result["runup"] == pytest.approx(0.31)
    assert result["previous_runup"] == pytest.approx(0.20)

    rows.append({"ts_ms": HOUR, "open": 1000, "high": 1000, "low": 1000, "close": 1000})
    assert runup_crossing(rows, decision_ts_ms=0) == result
    assert runup_crossing(_history(current=132, previous=131), decision_ts_ms=0)["crossed"] is False


def test_runup_crossing_requires_thirty_day_history_and_coverage():
    assert runup_crossing(_history(hours=100), decision_ts_ms=0)["reason"] \
        == "insufficient_history_span"
    sparse = _history()
    sparse = [row for i, row in enumerate(sparse) if i % 3 == 0 or row["ts_ms"] >= -HOUR]
    assert runup_crossing(sparse, decision_ts_ms=0)["reason"] \
        == "insufficient_history_coverage"


def test_sampled_exit_stop_wins_tie_then_tp_then_cap():
    tie = evaluate_sampled_exit(
        entry_mark=100, entry_ts_ms=0, now_ts_ms=HOUR,
        current={"open": 100, "high": 121, "low": 84, "close": 90},
    )
    assert tie == {"exit": True, "reason": "stop", "exit_mark": 120.0}
    tp = evaluate_sampled_exit(
        entry_mark=100, entry_ts_ms=0, now_ts_ms=HOUR,
        current={"open": 100, "high": 110, "low": 84, "close": 90},
    )
    assert tp == {"exit": True, "reason": "take_profit", "exit_mark": 85.0}
    cap = evaluate_sampled_exit(
        entry_mark=100, entry_ts_ms=0, now_ts_ms=8 * HOUR,
        current={"open": 98, "high": 110, "low": 90, "close": 95},
    )
    assert cap == {"exit": True, "reason": "time_cap", "exit_mark": 95.0}


def test_matched_forward_basket_requires_price_and_funding_coverage():
    result = matched_forward_basket(
        entries={"BBB": 100.0, "CCC": 200.0},
        exits={"BBB": 90.0, "CCC": 220.0},
        funding_bps={"BBB": 2.0, "CCC": -1.0},
        cost_bps=39.0,
    )
    expected = (((100 - 90) / 100 * 10_000 + 2) +
                ((200 - 220) / 200 * 10_000 - 1)) / 2 - 39
    assert result["valid"] is True
    assert result["basket_net_bps"] == pytest.approx(expected)
    blocked = matched_forward_basket(
        entries={"BBB": 100.0, "CCC": 200.0}, exits={"BBB": 90.0},
        funding_bps={"BBB": 2.0}, cost_bps=39.0,
    )
    assert blocked["valid"] is False


def test_causal_basket_regime_requires_coverage_and_uses_no_future_bar():
    bars = {
        "AAA": [{"ts_ms": -72 * HOUR, "close": 100}, {"ts_ms": 0, "close": 90},
                {"ts_ms": HOUR, "close": 1000}],
        "BBB": [{"ts_ms": -72 * HOUR, "close": 100}, {"ts_ms": 0, "close": 95}],
        "CCC": [{"ts_ms": -72 * HOUR, "close": 100}, {"ts_ms": 0, "close": 100}],
    }
    result = causal_basket_regime(
        bars, decision_ts_ms=0, universe=["AAA", "BBB", "CCC"],
        bearish_threshold_bps=-400,
    )
    assert result["label"] == "bearish"
    assert result["basket_return_bps"] == pytest.approx(-500)
    assert result["coverage"] == 1.0
    del bars["CCC"][0]
    assert causal_basket_regime(
        bars, decision_ts_ms=0, universe=["AAA", "BBB", "CCC"], min_coverage=0.8
    )["label"] == "unknown"


def test_regime_label_calls_empty_or_nan_window_unknown():
    assert regime_label(None, coverage=0.0) == "unknown"
    assert regime_label(float("nan"), coverage=1.0) == "unknown"


def test_regime_episode_needs_24_contiguous_valid_nonbearish_hours():
    rows = [{"decision_ts_ms": 0, "label": "bearish"}]
    rows.extend(
        {"decision_ts_ms": hour * HOUR, "label": "neutral"}
        for hour in range(1, 24)
    )
    rows.append({"decision_ts_ms": 24 * HOUR, "label": "bearish"})
    annotated = assign_regime_episodes(rows, separation_hours=24)
    assert [row["episode_id"] for row in annotated if row["label"] == "bearish"] == [1, 1]

    rows.insert(-1, {"decision_ts_ms": 24 * HOUR, "label": "neutral"})
    rows[-1]["decision_ts_ms"] = 25 * HOUR
    annotated = assign_regime_episodes(rows, separation_hours=24)
    assert [row["episode_id"] for row in annotated if row["label"] == "bearish"] == [1, 2]


def test_unknown_or_gap_cannot_create_regime_episode_separation():
    rows = [{"decision_ts_ms": 0, "label": "bearish"}]
    rows.extend(
        {"decision_ts_ms": hour * HOUR, "label": "neutral"}
        for hour in range(1, 25)
    )
    rows[12]["label"] = "unknown"
    rows.append({"decision_ts_ms": 25 * HOUR, "label": "bearish"})
    annotated = assign_regime_episodes(rows, separation_hours=24)
    assert annotated[-1]["episode_id"] == 1

    rows = [{"decision_ts_ms": 0, "label": "bearish"}]
    rows.extend(
        {"decision_ts_ms": hour * HOUR, "label": "neutral"}
        for hour in range(1, 13)
    )
    rows.extend(
        {"decision_ts_ms": hour * HOUR, "label": "neutral"}
        for hour in range(14, 27)
    )
    rows.append({"decision_ts_ms": 27 * HOUR, "label": "bearish"})
    assert assign_regime_episodes(rows, separation_hours=24)[-1]["episode_id"] == 1
