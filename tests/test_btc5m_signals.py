"""BTC 5m signal generators — pure per-bar position in {-1,0,1}."""
from __future__ import annotations

from sniper.algos.btc5m import signals as S


def _bars(closes):
    # minimal OHLC bars from a close series (high/low bracket close)
    return [{"open_ms": i * 300000, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)]


def test_positions_are_bounded_and_aligned():
    bars = _bars([float(i) for i in range(60)])
    pos = S.ema_crossover(bars, fast=5, slow=20)
    assert len(pos) == len(bars)
    assert set(pos) <= {-1, 0, 1}


def test_ema_crossover_trend_direction():
    up = S.ema_crossover(_bars([float(i) for i in range(60)]), fast=5, slow=20)
    down = S.ema_crossover(_bars([float(60 - i) for i in range(60)]), fast=5, slow=20)
    assert up[-1] == 1
    assert down[-1] == -1


def test_donchian_breakout_up_and_down():
    # flat then spike above the prior channel high -> long; flat then break below -> short
    up = S.donchian_breakout(_bars([100.0] * 20 + [130.0]), n=10)
    down = S.donchian_breakout(_bars([100.0] * 20 + [70.0]), n=10)
    assert up[-1] == 1
    assert down[-1] == -1


def test_donchian_carries_position_until_opposite_break():
    pos = S.donchian_breakout(_bars([100.0] * 12 + [130.0, 131.0, 129.0, 132.0]), n=10)
    assert pos[-1] == 1                       # still long; no downside break occurred


def test_meanrev_zscore_enters_on_dip_exits_on_revert():
    closes = [100.0] * 5 + [80.0, 100.0, 100.0, 100.0, 100.0]
    pos = S.meanrev_zscore(_bars(closes), n=5, entry=1.5, exit_z=1.0)
    assert pos[5] == 1                        # z=-2.0 -> oversold long
    assert pos[6] == 0                        # reverted toward mean -> flat


def test_meanrev_shorts_on_spike():
    closes = [100.0] * 5 + [120.0, 100.0]
    pos = S.meanrev_zscore(_bars(closes), n=5, entry=1.5, exit_z=1.0)
    assert pos[5] == -1                       # z=+2.0 -> overbought short


def _bars_v(closes, vols):
    return [{"open_ms": i * 300000, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": v}
            for i, (c, v) in enumerate(zip(closes, vols))]


def test_meanrev_confirmed_requires_volume_spike():
    closes = [100.0] * 5 + [80.0]          # sharp dip at index 5 (z<=-entry)
    spike = _bars_v(closes, [10.0] * 5 + [100.0])   # dip ON a volume spike -> confirmed
    quiet = _bars_v(closes, [10.0] * 6)             # same dip, flat volume -> NOT confirmed
    p_spike = S.meanrev_confirmed(spike, n=5, entry=1.5, exit_z=1.0, min_vol_z=1.5)
    p_quiet = S.meanrev_confirmed(quiet, n=5, entry=1.5, exit_z=1.0, min_vol_z=1.5)
    assert p_spike[5] == 1                  # entered: dislocation + volume confirmation
    assert p_quiet[5] == 0                  # filtered: dislocation but no volume spike


def test_meanrev_confirmed_vol_regime_gate_blocks_entry():
    closes = [100.0] * 5 + [80.0]
    bars = _bars_v(closes, [10.0] * 5 + [100.0])
    # max_vol tiny -> the dip bar's realized vol exceeds it -> entry suppressed even with vol spike
    p = S.meanrev_confirmed(bars, n=5, entry=1.5, exit_z=1.0, min_vol_z=1.5, max_vol=1e-6, vol_n=5)
    assert p[5] == 0


def test_meanrev_confirmed_in_registry():
    assert "meanrev_confirmed" in S.SIGNALS


def test_registry_exposes_named_signals():
    assert "ema_crossover" in S.SIGNALS and "meanrev_zscore" in S.SIGNALS
    pos = S.SIGNALS["ema_crossover"](_bars([float(i) for i in range(40)]), fast=5, slow=20)
    assert pos[-1] == 1
