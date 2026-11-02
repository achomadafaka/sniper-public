"""BTC 5m backtest engine — cost-honest, look-ahead-free."""
from __future__ import annotations

import pytest

from sniper.algos.btc5m import backtest as BT


def _bars(closes):
    return [{"open_ms": i * 300000, "open": c, "high": c, "low": c, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)]


def test_all_long_uptrend_zero_cost_matches_buy_and_hold():
    bars = _bars([100, 101, 102, 103])
    r = BT.backtest(bars, [1, 1, 1, 1], taker_fee=0.0, slippage=0.0, execution_lag=0)
    assert r["total_return"] == pytest.approx(103 / 100 - 1, rel=1e-9)
    assert r["n_trades"] == 1
    assert r["win_rate"] == 1.0
    assert 0.0 <= r["exposure"] <= 1.0


def test_execution_lag_delays_entry_by_one_bar():
    # lag=1: signal decided at bar i fills at bar i+1, so the first bar's move is NOT captured.
    bars = _bars([100, 110, 121])              # +10% then +10%
    lagged = BT.backtest(bars, [1, 1, 1], taker_fee=0.0, slippage=0.0, execution_lag=1)
    nolag = BT.backtest(bars, [1, 1, 1], taker_fee=0.0, slippage=0.0, execution_lag=0)
    assert lagged["total_return"] == pytest.approx(121 / 110 - 1, rel=1e-9)   # misses 100->110
    assert nolag["total_return"] == pytest.approx(121 / 100 - 1, rel=1e-9)
    assert lagged["total_return"] < nolag["total_return"]


def test_costs_reduce_return():
    bars = _bars([100, 101, 102, 103])
    free = BT.backtest(bars, [1, 1, 1, 1], taker_fee=0.0, slippage=0.0, execution_lag=0)["total_return"]
    costed = BT.backtest(bars, [1, 1, 1, 1], taker_fee=0.001, slippage=0.0, execution_lag=0)["total_return"]
    assert costed < free


def test_fees_eat_churn_on_flat_prices():
    bars = _bars([100, 100, 100, 100])
    r = BT.backtest(bars, [1, -1, 1, -1], taker_fee=0.001, slippage=0.0, execution_lag=0)
    assert r["total_return"] < 0          # gross 0, costs dominate
    assert r["n_trades"] == 4
    assert r["win_rate"] == 0.0


def test_flat_position_is_zero():
    r = BT.backtest(_bars([100, 110, 90, 105]), [0, 0, 0, 0], taker_fee=0.001, slippage=0.001)
    assert r["total_return"] == 0.0
    assert r["n_trades"] == 0
    assert r["exposure"] == 0.0


def test_exposure_fraction():
    r = BT.backtest(_bars([100, 101, 102, 103]), [1, 1, 0, 0], taker_fee=0.0, slippage=0.0, execution_lag=0)
    assert r["exposure"] == pytest.approx(0.5)


def test_metrics_keys_present():
    r = BT.backtest(_bars([100, 101, 100, 102, 101]), [1, 1, -1, 1, 0], execution_lag=0)
    for k in ("total_return", "sharpe", "profit_factor", "max_drawdown", "n_trades",
              "win_rate", "exposure", "equity", "largest_trade_share", "avg_trade"):
        assert k in r
    assert r["max_drawdown"] >= 0.0       # reported as a positive magnitude


def test_final_bar_entry_is_charged():
    # REGRESSION: a position opened only on the LAST bar must still pay entry cost (was uncharged —
    # the turnover loop stopped at n-1, so equity diverged from the trade-PnL path on a final-bar entry).
    r = BT.backtest(_bars([100, 100, 100]), [0, 0, 1], taker_fee=0.001, slippage=0.0, execution_lag=0)
    assert r["total_return"] == pytest.approx(-0.002, abs=1e-5)   # entry + liquidation (~2 sides, compounded)
    assert r["total_return"] < -0.0015                            # clearly 2 sides charged, not the old 1
    assert r["n_trades"] == 1


def test_positions_must_align_with_bars():
    with pytest.raises(ValueError):
        BT.backtest(_bars([100, 101, 102]), [1, 1], execution_lag=0)


def test_split_train_oos():
    bars = _bars(list(range(100, 200)))
    train, oos = BT.split(bars, train_frac=0.7)
    assert len(train) == 70 and len(oos) == 30
    assert train[-1]["open_ms"] < oos[0]["open_ms"]
