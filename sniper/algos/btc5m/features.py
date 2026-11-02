"""Pure technical features over price series / OHLCV bar lists. No network, no DB. Every function
returns a list aligned to the input; warmup positions are None (except EMA, which seeds on the
first value). Population std (ddof=0) is used for z-score / realized vol so a flat window is 0,
never a divide-by-zero."""
from __future__ import annotations

import math


def ema(series: list[float], n: int) -> list[float]:
    """Exponential moving average, seeded on the first value. alpha = 2/(n+1)."""
    if not series:
        return []
    alpha = 2.0 / (int(n) + 1.0)
    out = [float(series[0])]
    for x in series[1:]:
        out.append(alpha * float(x) + (1.0 - alpha) * out[-1])
    return out


def zscore(series: list[float], n: int) -> list:
    """Rolling z-score (x - mean_n) / popstd_n. None for the first n-1 bars; 0.0 on a flat window."""
    out: list = [None] * len(series)
    for i in range(n - 1, len(series)):
        win = [float(v) for v in series[i - n + 1: i + 1]]
        mu = sum(win) / n
        var = sum((v - mu) ** 2 for v in win) / n
        sd = math.sqrt(var)
        out[i] = 0.0 if sd == 0 else (float(series[i]) - mu) / sd
    return out


def rsi(series: list[float], n: int = 14) -> list:
    """Wilder's RSI. None for the first n bars. 100 on a strictly rising run, 0 on strictly falling."""
    out: list = [None] * len(series)
    if len(series) <= n:
        return out
    gains, losses = [], []
    for i in range(1, len(series)):
        ch = float(series[i]) - float(series[i - 1])
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # initial average over first n deltas
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(series)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        if avg_l == 0:
            out[i] = 100.0
        elif avg_g == 0:
            out[i] = 0.0
        else:
            rs = avg_g / avg_l
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def atr(bars: list[dict], n: int = 14) -> list:
    """Average True Range over OHLC bars. None for the first n bars."""
    tr: list = [None]
    for i in range(1, len(bars)):
        h, l = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    out: list = [None] * len(bars)
    for i in range(n, len(bars)):
        window = tr[i - n + 1: i + 1]
        out[i] = sum(window) / n
    return out


def volume_zscore(bars: list[dict], n: int = 20) -> list:
    """Rolling z-score of bar volume — high positive = a volume SPIKE (capitulation/forced flow,
    which mean-reverts differently than a quiet drift). None for the first n-1 bars; 0.0 on flat
    volume (population std, no divide-by-zero). Accepts OHLCV bar dicts (reads 'volume')."""
    vols = [float(b.get("volume") or 0.0) for b in bars]
    return zscore(vols, n)


def realized_vol(series: list[float], n: int) -> list:
    """Rolling population std of log returns. None for the first n bars; 0.0 on a flat window."""
    rets: list = [None]
    for i in range(1, len(series)):
        p0, p1 = float(series[i - 1]), float(series[i])
        rets.append(math.log(p1 / p0) if (p0 > 0 and p1 > 0) else 0.0)
    out: list = [None] * len(series)
    for i in range(n, len(series)):
        win = rets[i - n + 1: i + 1]
        mu = sum(win) / n
        var = sum((r - mu) ** 2 for r in win) / n
        out[i] = math.sqrt(var)
    return out
