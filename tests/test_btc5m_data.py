"""BTC 5m data adapter — pure parse + merge (no network)."""
from __future__ import annotations

from sniper.algos.btc5m import data as D


def _raw(open_ms, o, h, l, c, v, close_ms=None):
    # Binance kline row: [open_time, open, high, low, close, volume, close_time, ...]
    return [open_ms, str(o), str(h), str(l), str(c), str(v), close_ms or (open_ms + 299_999)]


def test_parse_klines_maps_ohlcv():
    rows = [_raw(1000, 100, 110, 95, 105, 12.5)]
    bars = D.parse_klines(rows, symbol="BTCUSDT")
    assert len(bars) == 1
    b = bars[0]
    assert b["symbol"] == "BTCUSDT"
    assert b["open_ms"] == 1000 and b["close_ms"] == 1000 + 299_999
    assert b["open"] == 100.0 and b["high"] == 110.0 and b["low"] == 95.0
    assert b["close"] == 105.0 and b["volume"] == 12.5


def test_parse_klines_skips_malformed_rows():
    rows = [_raw(1000, 100, 110, 95, 105, 1), ["bad"], [], None, _raw(1300_000, 1, 2, 0.5, 1.5, 3)]
    bars = D.parse_klines(rows, symbol="BTCUSDT")
    assert [b["open_ms"] for b in bars] == [1000, 1300_000]


def test_merge_bars_dedups_by_open_ms_and_sorts():
    a = D.parse_klines([_raw(2000, 2, 2, 2, 2, 1), _raw(1000, 1, 1, 1, 1, 1)], symbol="BTCUSDT")
    b = D.parse_klines([_raw(2000, 9, 9, 9, 9, 9), _raw(3000, 3, 3, 3, 3, 1)], symbol="BTCUSDT")
    merged = D.merge_bars(a, b)
    assert [x["open_ms"] for x in merged] == [1000, 2000, 3000]
    # newer wins on collision (b overrides a at open_ms=2000)
    assert next(x for x in merged if x["open_ms"] == 2000)["close"] == 9.0


def test_closes_extracts_close_series_in_time_order():
    bars = D.parse_klines([_raw(3000, 3, 3, 3, 3.3, 1), _raw(1000, 1, 1, 1, 1.1, 1)], symbol="BTCUSDT")
    bars = D.merge_bars(bars, [])
    assert D.closes(bars) == [1.1, 3.3]
