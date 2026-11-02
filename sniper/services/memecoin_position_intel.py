"""Per-position recovery intelligence for the real book.

Automates the hold / cut / lock / ride call an operator makes by hand: given a position + its live quote
+ how fresh the armed-wallet interest is, produce a recovery_score (0-100), a verdict, the reasons, and a
suggested action. READ-ONLY / advisory — it never mutates exits or trades; it drives a display badge and
(later) the stuck-bag sweep. The score blends the levers that actually decide a memecoin bag's fate:
liquidity depth (can we even exit?), drawdown, freshness of buy interest, mcap headroom, and turnover.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# component weights (sum 1.0)
_W_LIQ, _W_DRAW, _W_FRESH, _W_MCAP, _W_TURN = 0.30, 0.28, 0.22, 0.10, 0.10

DEAD_LIQ_USD = 500.0            # below this a $50 sell is ~unroutable -> effectively untradeable


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _log_band(x, lo, hi):
    """0 at `lo`, 100 at `hi`, log-scaled, clamped. For liquidity/turnover which span orders of mag."""
    if not x or x <= 0:
        return 0.0
    return _clamp((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 100.0)


# Each component returns None when its input is UNKNOWN (missing / blind-chain 0-read) so it drops out
# of the blend and the remaining weights renormalize — a missing quote must not read as "worst" and drag
# an otherwise-healthy bag toward CUT (a 4663 position with no DexScreener pair is not a dead bag).
def _liq_score(liq):
    if liq is None or liq <= 0:                        # unknown / blind-chain 0 -> exclude
        return None
    return _log_band(liq, DEAD_LIQ_USD, 50_000.0)      # $500 -> 0, $50k -> 100


def _drawdown_score(pnl_pct):
    if pnl_pct is None:
        return None
    return _clamp((pnl_pct + 80.0) / 80.0 * 100.0)     # -80% -> 0, 0% -> 100, >0 clamps 100


def _fresh_score(last_armed_buy_min):
    if last_armed_buy_min is None:                     # no armed-buy row != dead interest (manual buys,
        return None                                    # non-armed buyers, feed gaps) -> exclude, neutral
    return _clamp((360.0 - last_armed_buy_min) / 360.0 * 100.0)   # now -> 100, >6h -> 0


def _mcap_score(mcap):
    if not mcap or mcap <= 0:
        return None
    # sub-$1M has the most headroom (100); >$30M is spent (0) — matches the pump base-rate finding.
    return _clamp(100.0 - (math.log10(mcap) - 6.0) / (math.log10(30_000_000.0) - 6.0) * 100.0)


def _turnover_score(vol_24h, mcap):
    if not vol_24h or not mcap or mcap <= 0:
        return None
    return _log_band(vol_24h / mcap, 0.05, 3.0)        # 5% daily turnover -> 0, 300% -> 100


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def position_verdict(pos, quote, *, last_armed_buy_min=None, now_iso=None):
    """Advisory verdict for one open real position.

    pos: dict-like row (entry_price, entry_notional, opened_at, derisked_at, last_quote_price).
    quote: {price, market_cap, liquidity, volume_24h}. `last_armed_buy_min` = minutes since an armed
    wallet last BOUGHT this coin (None if never/unknown). Returns {recovery_score, verdict, reasons,
    suggested, pnl_pct}.
    """
    q = quote or {}
    liq = q.get("liquidity")
    # a blind-chain / no-pair 0-read is UNKNOWN, not a known-zero: don't let it flag DEAD or zero the
    # liquidity weight. Only a KNOWN POSITIVE liquidity participates in the DEAD test + the liq score.
    liq_known = liq if (liq is not None and liq > 0) else None
    mcap = q.get("market_cap")
    vol = q.get("volume_24h")
    entry = pos.get("entry_price") or 0.0
    cur = q.get("price") or pos.get("last_quote_price")
    pnl = ((cur / entry - 1.0) * 100.0) if (cur and entry) else None
    derisked = bool(pos.get("derisked_at"))
    now_dt = _parse(now_iso) or datetime.now(timezone.utc)
    opened = _parse(pos.get("opened_at"))
    held_h = (now_dt - opened).total_seconds() / 3600.0 if opened else None

    liq_s, draw_s = _liq_score(liq_known), _drawdown_score(pnl)
    fresh_s, mcap_s, turn_s = _fresh_score(last_armed_buy_min), _mcap_score(mcap), _turnover_score(vol, mcap)
    # weighted blend over KNOWN components only, renormalized by their weight sum (neutral 50 if none).
    weighted = [(_W_LIQ, liq_s), (_W_DRAW, draw_s), (_W_FRESH, fresh_s), (_W_MCAP, mcap_s), (_W_TURN, turn_s)]
    known = [(w, s) for (w, s) in weighted if s is not None]
    wsum = sum(w for w, _ in known)
    score = round(sum(w * s for w, s in known) / wsum, 1) if wsum > 0 else 50.0

    reasons = []
    if liq_known is not None:
        reasons.append(f"liq ${liq_known:,.0f}" + (" (untradeable)" if liq_known < DEAD_LIQ_USD else ""))
    else:
        reasons.append("liq n/a")
    if pnl is not None:
        reasons.append(f"{pnl:+.0f}%")
    if last_armed_buy_min is not None:
        reasons.append(f"last armed buy {int(last_armed_buy_min)}m ago" if last_armed_buy_min < 600
                       else "stale (no fresh buys)")
    else:
        reasons.append("no armed buys")
    if held_h is not None:
        reasons.append(f"held {held_h:.0f}h")

    # verdict — flags first, then score-driven. DEAD only on a KNOWN positive-but-tiny liquidity; an
    # unknown/0-read (blind chain, no pair) is NOT a write-off.
    if liq_known is not None and liq_known < DEAD_LIQ_USD:
        verdict, suggested = "DEAD", "untradeable liquidity — write-off; catastrophe floor is the only exit"
        score = min(score, 10.0)     # untradeable -> recovery is moot regardless of mcap/turnover
    elif derisked and pnl is not None and pnl >= 30:
        verdict, suggested = "RIDE", "principal recovered + running — ride with a protective trail"
    elif pnl is not None and pnl >= 15:
        verdict = "LOCK"
        suggested = ("tighten trail / set a TP to lock the gain"
                     + ("; interest faded, don't be greedy" if (last_armed_buy_min or 999) > 180 else ""))
    elif pnl is not None and pnl <= -55 and (score < 30 or (last_armed_buy_min or 999) > 240):
        verdict, suggested = "CUT", "deep loss + low recovery odds — lower the target or cut, redeploy"
    elif score >= 55:
        verdict, suggested = "HOLD", "healthy recovery odds — let the default rails work"
    else:
        verdict, suggested = "WATCH", "weak but not dead — watch; tighten if it fades further"

    _r = lambda s: round(s) if s is not None else None      # None = component excluded (unknown input)
    return {"recovery_score": score, "verdict": verdict, "reasons": reasons,
            "suggested": suggested, "pnl_pct": round(pnl, 1) if pnl is not None else None,
            "components": {"liq": _r(liq_s), "drawdown": _r(draw_s), "freshness": _r(fresh_s),
                           "mcap": _r(mcap_s), "turnover": _r(turn_s)}}


def last_armed_buy_minutes(conn, network_id, mint, now_iso=None):
    """Minutes since a tracked/armed wallet last BOUGHT this coin (None if never)."""
    r = conn.execute(
        "SELECT MAX(fo.observed_at) t FROM memecoin_feed_observations_v2 fo "
        "JOIN memecoin_wallets w ON w.source_user_id = fo.source_user_id "
        "WHERE fo.network_id=? AND fo.mint=? AND fo.normalized_side='buy' AND w.real_approved=1 "
        "AND COALESCE(w.paused,0)=0 AND COALESCE(w.excluded,0)=0", (network_id, mint)).fetchone()
    if not r or not r["t"]:
        return None
    now_dt = _parse(now_iso) or datetime.now(timezone.utc)
    last = _parse(r["t"])
    return (now_dt - last).total_seconds() / 60.0 if last else None
