import copy

import pytest

from sniper.hyperlive.altshort_evidence import (
    CostModel,
    causal_regime_features,
    matched_alt_basket_return,
    next_tradeable_price,
    passive_alt_basket,
)


def _bars(*points):
    return [
        {"ts_ms": ts, "open": px, "close": px, "funding_bps": 0.0}
        for ts, px in points
    ]


def test_regime_features_ignore_bars_after_decision():
    bars = {
        "BTC": _bars((0, 100), (100, 102), (200, 500)),
        "AAA": _bars((0, 100), (100, 90), (200, 1)),
        "BBB": _bars((0, 100), (100, 110), (200, 1000)),
    }
    before = causal_regime_features(
        bars, decision_ts_ms=150, universe=["AAA", "BBB"], lookbacks_ms=(100,)
    )
    changed = copy.deepcopy(bars)
    changed["AAA"][-1]["close"] = 1_000_000
    changed["BBB"][-1]["close"] = 0.01
    after = causal_regime_features(
        changed, decision_ts_ms=150, universe=["AAA", "BBB"], lookbacks_ms=(100,)
    )
    assert before == after
    assert before["coverage"] == 1.0
    assert before["basket_return_bps"]["100"] == pytest.approx(0.0)
    assert before["breadth_negative"]["100"] == 0.5
    assert before["alt_minus_btc_bps"]["100"] == pytest.approx(-200.0)


def test_next_tradeable_price_uses_strictly_later_bar_open():
    bars = [
        {"ts_ms": 100, "open": 99, "close": 100},
        {"ts_ms": 200, "open": 101, "close": 102},
    ]
    assert next_tradeable_price(bars, decision_ts_ms=100) == (200, 101.0)
    assert next_tradeable_price(bars, decision_ts_ms=200) is None


def test_event_matched_basket_is_leave_one_out_and_costed():
    bars = {
        "AAA": _bars((200, 100), (300, 90)),
        "BBB": _bars((200, 100), (300, 95)),
        "CCC": _bars((200, 100), (300, 105)),
        "BTC": _bars((200, 100), (300, 50)),
    }
    result = matched_alt_basket_return(
        {"coin": "AAA", "side": "short", "decision_ts_ms": 100, "exit_ts_ms": 250},
        bars,
        universe=["AAA", "BBB", "CCC", "BTC"],
        costs=CostModel(round_trip_bps=20),
    )
    assert result["eligible_n"] == 2
    assert result["covered_n"] == 2
    assert result["coverage"] == 1.0
    assert result["entry_ts_ms"] == 200
    assert result["exit_ts_ms"] == 300
    assert result["signal_net_bps"] == pytest.approx(980.0)
    assert result["basket_net_bps"] == pytest.approx(-20.0)
    assert result["residual_bps"] == pytest.approx(1000.0)


def test_event_matched_basket_blocks_low_coverage():
    bars = {
        "AAA": _bars((200, 100), (300, 90)),
        "BBB": _bars((200, 100), (300, 95)),
    }
    result = matched_alt_basket_return(
        {"coin": "AAA", "side": "short", "decision_ts_ms": 100, "exit_ts_ms": 250},
        bars,
        universe=["AAA", "BBB", "CCC", "DDD"],
        costs=CostModel(round_trip_bps=20),
        min_coverage=0.8,
    )
    assert result["coverage"] == pytest.approx(1 / 3)
    assert result["valid"] is False
    assert result["reason"] == "insufficient_basket_coverage"
    assert result["residual_bps"] is None


def test_event_matched_basket_applies_coin_specific_short_funding():
    bars = {
        "AAA": _bars((200, 100), (300, 90)),
        "BBB": _bars((200, 100), (300, 95)),
        "CCC": _bars((200, 100), (300, 105)),
    }
    funding = {
        "AAA": [{"time": 250, "rate": 0.001}],
        "BBB": [{"time": 250, "rate": 0.0005}],
        "CCC": [{"time": 250, "rate": -0.0005}],
    }
    result = matched_alt_basket_return(
        {"coin": "AAA", "side": "short", "decision_ts_ms": 100, "exit_ts_ms": 250},
        bars, universe=["AAA", "BBB", "CCC"], costs=CostModel(round_trip_bps=20),
        funding_by_coin=funding,
    )
    assert result["signal_net_bps"] == pytest.approx(990.0)
    assert result["basket_net_bps"] == pytest.approx(-20.0)
    assert result["residual_bps"] == pytest.approx(1010.0)


def test_passive_basket_reports_equity_path_and_drawdown():
    bars = {
        "AAA": _bars((100, 100), (200, 80), (300, 90)),
        "BBB": _bars((100, 100), (200, 120), (300, 110)),
    }
    result = passive_alt_basket(
        bars,
        start_ms=100,
        end_ms=300,
        universe=["AAA", "BBB"],
        costs=CostModel(round_trip_bps=10),
    )
    assert result["valid"] is True
    assert result["coverage"] == 1.0
    assert result["net_bps"] == pytest.approx(-10.0)
    assert result["max_drawdown_bps"] <= 0
    assert len(result["equity_curve"]) == 3
