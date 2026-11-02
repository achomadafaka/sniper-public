"""Setup library — parameterized hypotheses. Each `propose` returns (side, target_bps,
stop_bps, horizon_ms, rationale) from a snapshot, or None if it doesn't apply. Targets are
vol/event-scaled and floored so they can clear the fee; the strategist attaches EV + gates.
"""
from __future__ import annotations

from sniper.hyperlive.models import MarketSnapshot, ROUND_TRIP_FEE_BPS

MIN_TARGET_BPS = 2.5 * ROUND_TRIP_FEE_BPS   # ~22.5 — any setup must aim past the fee

# Horizon tracks. FAST = seconds-to-minutes; SWING = minutes-to-hours (where the move grows
# past the fixed fee). The engine runs one independent state machine per track.
SETUP_TRACK = {
    "continuation": "fast", "reversion": "fast", "event_reaction": "fast",
    "cluster_magnet": "fast", "cross_venue": "fast",
    "basis_reversion": "swing", "funding_reversion": "swing", "post_cascade": "swing",
}
TRACK_SETUPS = {
    "fast": {s for s, t in SETUP_TRACK.items() if t == "fast"},
    "swing": {s for s, t in SETUP_TRACK.items() if t == "swing"},
}


def _f(d, k, default=0.0):
    try:
        return float(d.get(k))
    except (TypeError, ValueError):
        return default


def propose(setup: str, s: MarketSnapshot) -> tuple | None:
    f = s.features
    vol = max(5.0, _f(f, "vol_bps"))
    if setup == "continuation":
        drive = _f(f, "ret60_bps") or _f(f, "ofi")
        if drive == 0:
            return None
        if _f(f, "oi_confirm") < 0:          # OI diverging from the move = short-covering, skip
            return None
        side = "long" if drive > 0 else "short"
        tgt = max(MIN_TARGET_BPS, 1.5 * vol)
        rationale = "60s drive, OI-confirmed" if _f(f, "oi_confirm") > 0 else "60s drive continues"
        return side, round(tgt, 1), round(0.6 * tgt, 1), 120_000, rationale
    if setup == "reversion":
        ret = _f(f, "ret60_bps")
        mtf = _f(f, "mins_to_funding")
        near_funding = 0 < mtf <= 5.0        # within 5 min of hourly funding settlement
        k = 3.0 if near_funding else 4.0     # funding flow mean-reverts -> fade a touch sooner
        if abs(ret) < max(25.0, k * vol):
            return None
        side = "short" if ret > 0 else "long"
        tgt = max(MIN_TARGET_BPS, 0.5 * abs(ret))
        rat = "fade overextension into funding settlement" if near_funding else "fade overextension"
        return side, round(tgt, 1), round(0.8 * tgt, 1), 180_000, rat
    if setup == "event_reaction":
        es = f.get("event_side")
        if es not in ("long", "short"):
            return None
        strength = max(_f(f, "event_strength_bps"), 30.0)
        tgt = max(MIN_TARGET_BPS, 0.6 * strength)
        return es, round(tgt, 1), round(0.5 * tgt, 1), 120_000, "forced-flow continuation"
    if setup == "cluster_magnet":
        cs = f.get("cluster_side")
        dist = _f(f, "cluster_distance_bps")
        if cs not in ("long", "short") or dist < MIN_TARGET_BPS:
            return None
        tgt = max(MIN_TARGET_BPS, 0.8 * dist)
        return cs, round(tgt, 1), round(0.6 * tgt, 1), 300_000, "price drawn to liq cluster"
    if setup == "cross_venue":
        bs = f.get("binance_lead_side")
        lead = _f(f, "binance_lead_bps")
        if bs not in ("long", "short") or abs(lead) < 8.0:
            return None
        tgt = max(MIN_TARGET_BPS, 1.2 * abs(lead))
        return bs, round(tgt, 1), round(0.7 * tgt, 1), 60_000, "binance leads hl"
    # ---- SWING setups (minutes-to-hours; the move grows past the fixed fee) ----
    if setup == "basis_reversion":
        basis_bps = (_f(f, "basis") or 0.0) * 1e4         # (mark-oracle)/oracle in bps
        if abs(basis_bps) < 15.0:
            return None
        side = "short" if basis_bps > 0 else "long"       # mark above index -> reverts down
        tgt = max(MIN_TARGET_BPS, 0.6 * abs(basis_bps))
        return side, round(tgt, 1), round(0.5 * tgt, 1), 1_800_000, "fade mark-oracle basis (~30m revert)"
    if setup == "funding_reversion":
        funding_bps = (_f(f, "funding") or 0.0) * 1e4     # funding per interval in bps
        if abs(funding_bps) < 3.0:
            return None
        side = "short" if funding_bps > 0 else "long"     # positive funding: longs pay -> fade longs
        tgt = max(MIN_TARGET_BPS, 12.0 * abs(funding_bps))
        return side, round(tgt, 1), round(0.5 * tgt, 1), 3_600_000, "fade funding-crowded side (~1h)"
    if setup == "post_cascade":
        if _f(f, "liq_rate") < 2.0:                       # needs recent forced flow
            return None
        ret = _f(f, "ret300_bps")
        if abs(ret) < 50.0:                               # and a big recent move to fade
            return None
        side = "short" if ret > 0 else "long"             # fade the overshoot
        tgt = max(MIN_TARGET_BPS, 0.5 * abs(ret))
        return side, round(tgt, 1), round(0.5 * tgt, 1), 900_000, "fade post-cascade exhaustion (~15m)"
    return None
