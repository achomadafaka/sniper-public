"""Auto-customize exits by entry archetype.

The funded band policy is generic (a micro-cap coin gets a loose 50% trail regardless of whether it's a
thin-liquidity scalp or a deep-liquidity runner). This classifies each new real position by its live
LIQUIDITY + short-term MOMENTUM and applies a tailored exit override where the band default is clearly
mismatched — the fix for the two lessons the book taught us:
  - thin liquidity -> take profit FAST (a tight TP + tight trail); you can't wait for +40% derisk because
    the liquidity won't be there.
  - a coin entered into strength (a runner) -> ride with a protective ~25% trail, NOT the loose 50%
    default that let a +87% winner nearly round-trip.

Applied as a post-entry SWEEP (overlay override, source 'auto-classify') — it never mutates the frozen
funded contract or the runner entry path, and it always yields to an operator override.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.services.memecoin_origin import bypasses_selection

THIN_LIQ_USD = 3_000.0          # below this you must exit on strength, not wait for the derisk
DEEP_LIQ_USD = 3_000.0          # runner/dip archetypes require at least this much depth to be real
RUNNER_MOM_PCT = 25.0           # 1h change at entry above which it's a momentum runner
DIP_MOM_PCT = -15.0             # ...below which it's a falling-knife dip


def classify_entry(*, liquidity, mcap=None, momentum_1h=None, armed_consensus=0):
    """Return {archetype, policy, reason}. `policy` is an override dict (tp_pct/sl_pct/trail_pct/
    derisk_pct) or None to keep the band default. When a policy sets trail_pct it ALSO sets derisk_pct,
    because a trail-only override silently zeroes the derisk stage."""
    liq = liquidity or 0.0
    if 0 < liq < THIN_LIQ_USD:
        return {"archetype": "thin_liq",
                "policy": {"tp_pct": 30.0, "trail_pct": 20.0, "derisk_pct": 40.0},
                "reason": f"thin liq ${liq:,.0f} — take profit fast (TP 30% / trail 20%)"}
    if momentum_1h is not None and momentum_1h >= RUNNER_MOM_PCT and liq >= DEEP_LIQ_USD:
        return {"archetype": "runner",
                "policy": {"trail_pct": 25.0, "derisk_pct": 40.0},
                "reason": f"entered strength (+{momentum_1h:.0f}%/1h) — ride with a protective 25% trail"}
    if momentum_1h is not None and momentum_1h <= DIP_MOM_PCT and liq >= DEEP_LIQ_USD:
        return {"archetype": "dip",
                "policy": {"sl_pct": 45.0, "derisk_pct": 40.0},
                "reason": f"entered a dip ({momentum_1h:.0f}%/1h) — tighter 45% stop, patient derisk"}
    return {"archetype": "normal", "policy": None,
            "reason": "liquid + neutral — band default is appropriate"}


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def apply_entry_classification(conn, *, quote_fn, max_age_min=45.0, now_iso=None):
    """Sweep: for open real positions opened within `max_age_min` that carry NO active exit override of
    ANY source, classify by live liquidity+momentum and apply the tailored overlay (source
    'auto-classify'). `quote_fn(net, mint)` returns {liquidity, market_cap, momentum_1h} (injectable).
    Returns the applied classifications.

    Deferring to ANY existing override (not just operator/auto-classify) is deliberate: the funded
    runner writes its own TESTED cohort policies at fill (source 'micro'/'ride'/'scalp'/'template'), and
    this heuristic overlay must not shadow them. Previously the predicate only skipped
    operator/auto-classify, so it wrote an 'auto-classify' row that `effective_exit_override` could never
    honor (it ranks below the cohort sources) — protection that was logged but never in effect.
    """
    from sniper.persistence import db_memecoin as m
    from sniper.util.clock import real_utc_now_iso
    now_iso = now_iso or real_utc_now_iso()
    now_dt = _parse(now_iso) or datetime.now(timezone.utc)
    applied = []
    rows = conn.execute(
        "SELECT id, network_id, mint, ticker, opened_at, signal_source FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=1").fetchall()
    for p in rows:
        if bypasses_selection(p["signal_source"]):   # manual or desk rails are never overlaid
            continue
        opened = _parse(p["opened_at"])
        if opened is None or (now_dt - opened).total_seconds() / 60.0 > float(max_age_min):
            continue
        # skip if ANY active override already exists — operator, a prior auto-classify, OR a tested
        # runner cohort policy (micro/ride/scalp/template). This overlay only fills the gap for
        # positions the cohort system doesn't already cover.
        prior = conn.execute(
            "SELECT 1 FROM memecoin_position_exit_overrides WHERE position_id=? AND active=1",
            (p["id"],)).fetchone()
        if prior:
            continue
        q = quote_fn(p["network_id"], p["mint"]) or {}
        cls = classify_entry(liquidity=q.get("liquidity"), mcap=q.get("market_cap"),
                             momentum_1h=q.get("momentum_1h"))
        if not cls["policy"]:
            continue
        m.set_position_exit_override(conn, p["id"], "auto-classify", actor="auto-classify",
                                     note=cls["reason"], now_iso=now_iso, **cls["policy"])
        applied.append({"id": p["id"], "ticker": p["ticker"], "archetype": cls["archetype"],
                        "reason": cls["reason"]})
    return applied
