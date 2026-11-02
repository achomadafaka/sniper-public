"""Pure signal generators. Each returns a list of target positions in {-1,0,1} aligned to `bars`
(+1 long, -1 short, 0 flat). Warmup bars are 0. Signals are intentionally simple and rule-based;
the backtest charges realistic costs on every position change, so only signals whose moves clear
fees survive. Registered in SIGNALS for the sweep/forward runner."""
from __future__ import annotations

from sniper.algos.btc5m import features as FE


def ema_crossover(bars: list[dict], *, fast: int = 12, slow: int = 26) -> list[int]:
    """Trend: long when fast EMA > slow EMA, short otherwise. Flat until `slow` bars seen."""
    closes = [float(b["close"]) for b in bars]
    ef, es = FE.ema(closes, fast), FE.ema(closes, slow)
    out = [0] * len(bars)
    for i in range(len(bars)):
        if i < slow:
            continue
        out[i] = 1 if ef[i] > es[i] else -1
    return out


def donchian_breakout(bars: list[dict], *, n: int = 20) -> list[int]:
    """Breakout: go long when close breaks above the prior n-bar high, short below the prior n-bar
    low; otherwise carry the existing position (classic turtle-style). Flat until n bars seen."""
    out = [0] * len(bars)
    pos = 0
    for i in range(len(bars)):
        if i < n:
            continue
        prior_high = max(float(b["high"]) for b in bars[i - n:i])
        prior_low = min(float(b["low"]) for b in bars[i - n:i])
        c = float(bars[i]["close"])
        if c > prior_high:
            pos = 1
        elif c < prior_low:
            pos = -1
        out[i] = pos
    return out


def meanrev_zscore(bars: list[dict], *, n: int = 20, entry: float = 2.0, exit_z: float = 0.5,
                   max_vol: float | None = None, vol_n: int = 20) -> list[int]:
    """Mean reversion: enter long when z-score <= -entry (oversold), short when z >= +entry; exit to
    flat when |z| <= exit_z. Optional volatility gate: when max_vol is set, suppress NEW entries while
    realized vol exceeds it (don't fade a violent regime). Carries position between entry and exit."""
    closes = [float(b["close"]) for b in bars]
    z = FE.zscore(closes, n)
    vol = FE.realized_vol(closes, vol_n) if max_vol is not None else [None] * len(bars)
    out = [0] * len(bars)
    pos = 0
    for i in range(len(bars)):
        zi = z[i]
        if zi is None:
            continue
        if pos != 0 and abs(zi) <= exit_z:
            pos = 0
        elif pos == 0:
            gated = max_vol is not None and vol[i] is not None and vol[i] > max_vol
            if not gated:
                if zi <= -entry:
                    pos = 1
                elif zi >= entry:
                    pos = -1
        out[i] = pos
    return out


def meanrev_confirmed(bars: list[dict], *, n: int = 20, entry: float = 2.0, exit_z: float = 0.5,
                      min_vol_z: float = 1.0, max_vol: float | None = None, vol_n: int = 20) -> list[int]:
    """Mean reversion that only fades a z-extreme when it's CONFIRMED by a volume spike (a ≥2σ price
    move on a volume spike = capitulation/forced flow that snaps back; the same move on quiet volume
    is more likely informed drift — don't fade it). Optional calm-regime gate (max_vol): skip entries
    when realized vol is high (don't catch a knife mid-explosion). Exit to flat when |z| <= exit_z."""
    closes = [float(b["close"]) for b in bars]
    z = FE.zscore(closes, n)
    vz = FE.volume_zscore(bars, n)        # volume confirmation shares the price-dislocation window `n`
    rv = FE.realized_vol(closes, vol_n) if max_vol is not None else [None] * len(bars)  # vol_n = realized-vol/calm-gate window only
    out = [0] * len(bars)
    pos = 0
    for i in range(len(bars)):
        zi = z[i]
        if zi is None:
            continue
        if pos != 0 and abs(zi) <= exit_z:
            pos = 0
        elif pos == 0 and abs(zi) >= entry:
            confirmed = (vz[i] is not None and vz[i] >= min_vol_z)
            calm = (max_vol is None) or (rv[i] is not None and rv[i] <= max_vol)
            if confirmed and calm:
                pos = 1 if zi <= -entry else -1
        out[i] = pos
    return out


SIGNALS = {
    "ema_crossover": ema_crossover,
    "donchian_breakout": donchian_breakout,
    "meanrev_zscore": meanrev_zscore,
    "meanrev_confirmed": meanrev_confirmed,
}
