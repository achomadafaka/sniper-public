"""Feature family C — Binance-reframed, known-at-decision, continuous predictor features.

Pure (lists of observation dicts in, flat feature dict out). All features obey a strict known-at-time
cutoff: ``source_event_ts <= cutoff_ts`` AND ``receive_ts <= decision_ts`` (an observation whose arrival
can't be proven before the decision is excluded — fail closed). Price-derived features (returns, realized
vol, EMA, trend) use any price source; volume/VWAP/flow features require a real volume-bearing source and
are ``None`` for price-only RTDS — they are NEVER fabricated from price. The legacy categorical spot
signal survives only as ``legacy_spot_signal_diagnostic`` (never a load-bearing numeric input).
"""
from __future__ import annotations

import math
import statistics
from typing import Optional

RETURN_HORIZONS = (5, 15, 30, 60, 120)
VOL_HORIZONS = (15, 30, 60, 120)
VOLUME_HORIZONS = (5, 15, 30, 60)
VWAP_HORIZONS = (15, 30, 60)
VOLUME_Z_HORIZONS = (30, 60, 120)
EMA_FAST_SPAN = 5
EMA_SLOW_SPAN = 20
_TREND_EPS = 1e-4


def _known_obs(binance_obs, *, cutoff_ms, decision_ts, expected_symbol=None):
    out = [o for o in (binance_obs or [])
           if o.get("source_event_ts") is not None and int(o["source_event_ts"]) <= cutoff_ms
           and o.get("receive_ts") is not None and int(o["receive_ts"]) <= decision_ts
           and o.get("price") is not None
           and (expected_symbol is None or o.get("symbol") in (None, expected_symbol))]
    out.sort(key=lambda o: o["source_event_ts"])
    return out


def _known_cl(chainlink_ticks, *, cutoff_ms, decision_ts):
    out = [t for t in (chainlink_ticks or [])
           if int(t["source_ts_ms"]) <= cutoff_ms
           and t.get("recv_ts") is not None and int(t["recv_ts"]) <= decision_ts]
    out.sort(key=lambda t: t["source_ts_ms"])
    return out


def _log_return(obs, *, now_ms, horizon_s):
    if len(obs) < 2:
        return None
    p_now = float(obs[-1]["price"])
    cut = now_ms - horizon_s * 1000
    prior = [o for o in obs if o["source_event_ts"] <= cut]
    if not prior:
        return None
    p_past = float(prior[-1]["price"])
    if p_now <= 0 or p_past <= 0:
        return None
    return math.log(p_now / p_past)


def _realized_vol(series, *, now_ms, horizon_s):
    """Per-step stdev of consecutive log returns within the trailing ``horizon_s`` (the stream is ~1s
    spaced, so this is ~per-second). ``series`` = sorted ``[(ts_ms, price)]``."""
    lo = now_ms - horizon_s * 1000
    win = [(ts, p) for ts, p in series if lo <= ts <= now_ms and p and p > 0]
    if len(win) < 3:
        return None
    rets = [math.log(win[i][1] / win[i - 1][1]) for i in range(1, len(win))]
    return statistics.pstdev(rets) if len(rets) >= 2 else None


def _window(obs, *, now_ms, horizon_s, need_qty=False):
    lo = now_ms - horizon_s * 1000
    return [o for o in obs if lo <= o["source_event_ts"] <= now_ms
            and (not need_qty or (o.get("quantity") is not None))]


def _volume(obs, *, now_ms, horizon_s):
    win = _window(obs, now_ms=now_ms, horizon_s=horizon_s, need_qty=True)
    return sum(float(o["quantity"]) for o in win) if win else None


def _vwap_and_dist(obs, *, now_ms, horizon_s, price_now):
    win = [o for o in _window(obs, now_ms=now_ms, horizon_s=horizon_s, need_qty=True)
           if float(o["quantity"]) > 0]
    tot_q = sum(float(o["quantity"]) for o in win)
    if tot_q <= 0:
        return None, None
    num = sum((float(o["quote_quantity"]) if o.get("quote_quantity") is not None
               else float(o["price"]) * float(o["quantity"])) for o in win)
    vwap = num / tot_q
    dist = ((price_now - vwap) / vwap) if (price_now is not None and vwap > 0) else None
    return vwap, dist


def _volume_z(obs, *, now_ms, horizon_s):
    """z-score of the current trailing-window volume vs the distribution of PRIOR equal windows (all
    strictly before the decision). None if no volume source or < 2 prior windows with data."""
    buckets = []
    for k in range(0, 6):
        hi = now_ms - k * horizon_s * 1000
        lo = hi - horizon_s * 1000
        win = [o for o in obs if lo < o["source_event_ts"] <= hi and o.get("quantity") is not None]
        buckets.append(sum(float(o["quantity"]) for o in win) if win else None)
    cur, prior = buckets[0], [b for b in buckets[1:] if b is not None]
    if cur is None or len(prior) < 2:
        return None
    sd = statistics.pstdev(prior)
    if sd == 0:
        return 0.0
    return (cur - statistics.mean(prior)) / sd


def _flow(obs, *, now_ms, horizon_s):
    """(buy_volume_share, signed_volume, trade_count) from aggTrade side (``raw.m`` = isBuyerMaker; a
    taker BUY has ``m==False``). None when no volume / no side info."""
    win = _window(obs, now_ms=now_ms, horizon_s=horizon_s, need_qty=True)
    if not win:
        return None, None, None
    buy = sum(float(o["quantity"]) for o in win if (o.get("raw") or {}).get("m") is False)
    sell = sum(float(o["quantity"]) for o in win if (o.get("raw") or {}).get("m") is True)
    total = buy + sell
    counts = [o["trade_count"] for o in win if o.get("trade_count") is not None]
    tc = int(sum(counts)) if counts else None
    if total <= 0:
        return None, None, tc
    return buy / total, buy - sell, tc


def _ema(series, *, span):
    if len(series) < 2:
        return None
    alpha = 2.0 / (span + 1)
    ema = float(series[0][1])
    for _ts, p in series[1:]:
        ema = alpha * float(p) + (1 - alpha) * ema
    return ema


def _ema_slope(series, *, span):
    if len(series) < 2:
        return None
    full, prev = _ema(series, span=span), _ema(series[:-1], span=span)
    if full is None or prev is None:
        return None
    dt = (series[-1][0] - series[-2][0]) / 1000.0
    return (full - prev) / dt if dt > 0 else None


def _legacy_signal(spread, price_now) -> str:
    if spread is None or not price_now:
        return "flat"
    rel = spread / price_now
    if rel > _TREND_EPS:
        return "trend_up"
    if rel < -_TREND_EPS:
        return "trend_down"
    return "flat"


def build_binance_features(binance_obs, chainlink_ticks, *, cutoff_ts, decision_ts,
                           expected_symbol=None) -> dict:
    now_ms = int(decision_ts) * 1000
    cutoff_ms = int(cutoff_ts) * 1000
    obs = _known_obs(binance_obs, cutoff_ms=cutoff_ms, decision_ts=decision_ts,
                     expected_symbol=expected_symbol)
    cl = _known_cl(chainlink_ticks, cutoff_ms=cutoff_ms, decision_ts=decision_ts)
    price_now = float(obs[-1]["price"]) if obs else None
    bser = [(int(o["source_event_ts"]), float(o["price"])) for o in obs]
    cser = [(int(t["source_ts_ms"]), float(t["price"])) for t in cl]

    feats = {"binance_price_now": price_now}
    for h in RETURN_HORIZONS:
        feats[f"binance_return_{h}s"] = _log_return(obs, now_ms=now_ms, horizon_s=h)
    for h in VOL_HORIZONS:
        feats[f"binance_realized_vol_{h}s"] = _realized_vol(bser, now_ms=now_ms, horizon_s=h)
        feats[f"chainlink_realized_vol_{h}s"] = _realized_vol(cser, now_ms=now_ms, horizon_s=h)
    for h in VOLUME_HORIZONS:
        feats[f"binance_volume_{h}s"] = _volume(obs, now_ms=now_ms, horizon_s=h)
    for h in VWAP_HORIZONS:
        vwap, dist = _vwap_and_dist(obs, now_ms=now_ms, horizon_s=h, price_now=price_now)
        feats[f"binance_vwap_{h}s"] = vwap
        feats[f"binance_distance_to_vwap_{h}s"] = dist
    for h in VOLUME_Z_HORIZONS:
        feats[f"binance_volume_z_{h}s"] = _volume_z(obs, now_ms=now_ms, horizon_s=h)
    share, signed, tc = _flow(obs, now_ms=now_ms, horizon_s=60)
    feats["binance_buy_volume_share"] = share
    feats["binance_signed_volume"] = signed
    feats["binance_trade_count"] = tc

    ema_fast, ema_slow = _ema(bser, span=EMA_FAST_SPAN), _ema(bser, span=EMA_SLOW_SPAN)
    spread = (ema_fast - ema_slow) if (ema_fast is not None and ema_slow is not None) else None
    feats["binance_ema_fast"] = ema_fast
    feats["binance_ema_slow"] = ema_slow
    feats["binance_ema_spread"] = spread
    feats["binance_ema_slope"] = _ema_slope(bser, span=EMA_FAST_SPAN)
    feats["binance_short_return_z"] = _short_return_z(obs, bser, now_ms=now_ms)
    feats["binance_trend_strength"] = (spread / price_now) if (spread is not None and price_now) else None
    feats["binance_reversion_distance"] = feats["binance_distance_to_vwap_30s"]
    feats["legacy_spot_signal_diagnostic"] = _legacy_signal(spread, price_now)
    return feats


def _short_return_z(obs, bser, *, now_ms):
    r = _log_return(obs, now_ms=now_ms, horizon_s=5)
    v = _realized_vol(bser, now_ms=now_ms, horizon_s=15)
    if r is None or not v:
        return None
    return r / v
