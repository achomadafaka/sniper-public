"""Forecasting trend filter for the BTC 5m scalper — the engine's MEMORY of where the price is and where it's
headed, carried forward and updated each tick (not re-derived from scratch).

Holt's linear method (double exponential smoothing): maintains a `level` (denoised price) and a `trend`
(velocity = denoised direction), each updated by the new observation. This is the steady-state Kalman filter
for a constant-velocity model — a real statistical forecasting model with a principled signal/noise split,
governed by two smoothing constants:
  level_t = α·price_t + (1-α)·(level_{t-1} + trend_{t-1})
  trend_t = β·(level_t - level_{t-1}) + (1-β)·trend_{t-1}
α large → responsive level; β large → responsive trend. The `trend` is the velocity (price units per step);
forecast k steps ahead = level + k·trend. Pure compute — unit-tested.
"""
from __future__ import annotations

ALPHA = 0.3      # level smoothing (how fast the denoised price tracks new prints)
BETA = 0.1       # trend smoothing (how fast the velocity adapts)


def holt_trend(prices, *, alpha=ALPHA, beta=BETA):
    """Run Holt's linear filter over a price series. Returns (level, velocity) at the end; velocity = the
    denoised per-step trend (the direction signal). None if too few points."""
    if not prices or len(prices) < 3:
        return (prices[-1] if prices else None, None)
    level = float(prices[0])
    trend = float(prices[1]) - float(prices[0])
    for z in prices[1:]:
        z = float(z)
        prev = level
        level = alpha * z + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    return (level, trend)


def holt_forecast(prices, k, *, alpha=ALPHA, beta=BETA):
    """k-step-ahead forecast of the price: level + k·velocity. None if unavailable."""
    level, trend = holt_trend(prices, alpha=alpha, beta=beta)
    if level is None or trend is None:
        return None
    return level + k * trend
