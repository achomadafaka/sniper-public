"""BTC 5m feature library — pure indicators."""
from __future__ import annotations

import math

import pytest

from sniper.algos.btc5m import features as FE


def test_ema_alpha_one_is_identity():
    assert FE.ema([1.0, 2.0, 3.0], 1) == [1.0, 2.0, 3.0]   # n=1 -> alpha=1 -> ema==x


def test_ema_known_values():
    out = FE.ema([1.0, 2.0, 3.0], 3)                        # alpha=0.5, seed=first
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(1.5)
    assert out[2] == pytest.approx(2.25)


def test_zscore_rolling_population_std():
    out = FE.zscore([1.0, 2.0, 3.0, 4.0], 3)
    assert out[0] is None and out[1] is None               # warmup
    expected = (3 - 2) / math.sqrt(2 / 3)                   # window [1,2,3], pop std
    assert out[2] == pytest.approx(expected)
    assert out[3] == pytest.approx(expected)                # window [2,3,4] same shape


def test_zscore_zero_variance_is_zero_not_div0():
    out = FE.zscore([5.0, 5.0, 5.0, 5.0], 3)
    assert out[2] == 0.0 and out[3] == 0.0                  # flat window -> z=0, no div-by-0


def test_rsi_extremes():
    up = FE.rsi([float(i) for i in range(1, 20)], 14)       # strictly rising
    down = FE.rsi([float(i) for i in range(20, 1, -1)], 14)  # strictly falling
    assert up[-1] == pytest.approx(100.0)
    assert down[-1] == pytest.approx(0.0)
    assert up[0] is None                                    # warmup is None


def test_atr_true_range_average():
    bars = [{"high": 10, "low": 8, "close": 9},
            {"high": 12, "low": 9, "close": 11},            # TR=max(3, |12-9|, |9-9|)=3
            {"high": 11, "low": 7, "close": 8}]             # TR=max(4, |11-11|, |7-11|)=4
    out = FE.atr(bars, 2)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx((3 + 4) / 2)


def test_realized_vol_flat_is_zero():
    out = FE.realized_vol([100.0] * 5, 3)
    assert out[-1] == 0.0


def test_volume_zscore_spike_is_positive_warmup_none():
    bars = [{"volume": v} for v in [10, 10, 10, 10, 100]]   # last bar = volume spike
    out = FE.volume_zscore(bars, 4)
    assert out[0] is None and out[2] is None                # warmup (first n-1)
    assert out[4] is not None and out[4] > 1.5              # spike reads as a high positive z


def test_volume_zscore_flat_is_zero_not_div0():
    out = FE.volume_zscore([{"volume": 5.0} for _ in range(5)], 3)
    assert out[-1] == 0.0                                   # flat volume -> z=0, no divide-by-zero
