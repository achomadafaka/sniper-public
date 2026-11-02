"""Jump-aware regime volatility for crypto5m brain v2 (pure; no I/O, no cross-poll state).

vol_slow: jump-EXCLUDED realized vol (so a 4sigma tick can't inflate the diffusion denominator for 3 min).
vol_fast: jump-INCLUDED short EWMA (it MUST spike on a jump -- that spike is the regime signal that drives
the cede). jump_flag/ticks_since_jump let the blend cede immediately on the one tick before vol_fast's EWMA
fully absorbs it. All derived from the per-poll kline window -> no buffer, restart-safe."""
from __future__ import annotations

import math
import statistics


def _log_returns(closes_by_sec, lo, hi):
    secs = sorted(s for s in closes_by_sec if lo <= s <= hi)
    rets = []  # (sec, r)
    for a, b in zip(secs, secs[1:]):
        pa, pb = closes_by_sec[a], closes_by_sec[b]
        if pa > 0 and pb > 0:
            rets.append((b, math.log(pb / pa)))
    return rets


def regime_vol(closes_by_sec, now, *, kappa=4.0, fast_span_s=10, slow_lookback_s=180, min_points=10,
               vol_floor=1e-4):
    rets = _log_returns(closes_by_sec, now - slow_lookback_s, now)
    if len(rets) < min_points:
        return None
    vals = [r for _, r in rets]
    # robust scale for jump detection (MAD): immune to the very jumps it is flagging
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) * 1.4826
    thresh = kappa * mad if mad > 0 else float("inf")
    jumps = {s for s, r in rets if abs(r) > thresh}

    nonjump = [r for s, r in rets if s not in jumps]
    if len(nonjump) < 2:
        return None
    # FLOOR vol_slow exactly like the live vol (`live_desk.VOL_FLOOR_PER_S=1e-4`): BTC 1s log-returns are
    # zero-dominated (the price is flat in many 1s buckets), so the raw jump-excluded stdev collapses to
    # ~1e-7 and z_ours = ln(now/start)/(vol_slow·√t) explodes to ±600 — which pins divergence huge and the
    # blend weight at 0, so z_v2 degenerates to the market price on EVERY tick (calm or sharp). The floor
    # keeps z_ours sane and lets the diffusion actually contribute, so the regime signal can do its job.
    vol_slow = max(statistics.pstdev(nonjump), vol_floor)

    # vol_fast: EWMA of r^2 over ~fast_span_s, jump INCLUDED, most-recent weighted heaviest
    alpha = 2.0 / (fast_span_s + 1)
    ewma_var, started = 0.0, False
    for _, r in rets:
        ewma_var = (r * r) if not started else (alpha * r * r + (1 - alpha) * ewma_var)
        started = True
    vol_fast = math.sqrt(ewma_var)

    latest_sec = rets[-1][0]
    jump_flag = latest_sec in jumps
    ticks_since_jump = (now - max(jumps)) if jumps else None
    return {"vol_slow": vol_slow, "vol_fast": vol_fast, "jump_flag": jump_flag,
            "ticks_since_jump": ticks_since_jump, "divergence": vol_fast / vol_slow}


def sharp_window_label(returns, settlement, *, rel_k=4.0, abs_floor=5e-4):
    """Window-level label from the BTC path only (independent of z_v2). abs_floor ~= 5x BTC's ~1e-4 typical
    1s log-return stdev. revert/continue is settlement-defined and POST-HOC -- it never reaches the live path."""
    vals = [r for r in returns if r is not None]
    if not vals:
        return "calm"
    mags = [abs(r) for r in vals]
    peak = max(mags)
    med = statistics.median(mags)
    rel_ok = (med <= 0) or (peak >= rel_k * med)
    if not (rel_ok and peak >= abs_floor):
        return "calm"
    move_sign = 1 if vals[mags.index(peak)] >= 0 else 0     # 1 = up move, 0 = down move
    return "sharp_continue" if move_sign == settlement else "sharp_revert"
