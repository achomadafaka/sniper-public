"""Hold-band forward-study read-out (pre-registered 2026-07-07, hardened per methodology review).

Aggregates CLOSED copy positions by the copied trader's FROZEN hold band, discovery pool, and
execution arm, with a TRADER-CLUSTERED bootstrap CI so the pre-registered decision rule can be
executed by code (not eyeballed). Computes numbers only — verdicts wait for gates_met.

Review-driven hardening:
- band is read FROZEN from memecoin_positions.hold_band (set at open); live analytics is only a
  fallback for legacy positions (median_hold_s is re-computable, so a live join would silently
  reclassify history).
- CIs are block-bootstrapped by trader (positions cluster hard: few traders -> many correlated
  positions); a positions-independent CI would overstate power.
- by_band_open exposes the survivorship/censoring lopsidedness (holders close later, so their
  closed sample is biased) before anyone interprets.
- primary comparison is Arm A (momentum_a) ONLY — its exit is our own machine, so hold band is a
  clean SELECTION covariate. control/alpha_b mirror-exit, where hold band mechanically IS trade
  duration (a confound), so they are secondary/context only.
See docs/superpowers/specs/2026-07-07-memecoin-holdband-forward-study-preregistration.md.
"""
from __future__ import annotations

import random
from collections import defaultdict

FAST_MAX_S = 3600
HOLDER_MIN_S = 86400
_BOOT_SEED = 1729          # fixed -> reproducible CIs across runs


def band_of(median_hold_s, fast_max=FAST_MAX_S, holder_min=HOLDER_MIN_S):
    if median_hold_s is None:
        return "unknown"
    if median_hold_s < fast_max:
        return "fast"
    if median_hold_s < holder_min:
        return "med"
    return "holder"


def _median(v):
    v = sorted(v)
    if not v:
        return None
    n = len(v); mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


def _bootstrap_ci_clustered(rows, n_resamples=2000, alpha=0.05, seed=_BOOT_SEED):
    """Block bootstrap of the mean, resampling by TRADER (uid) not position — positions within
    a trader are correlated (repeated buys on the same pumps). None if < 2 distinct traders."""
    clusters = defaultdict(list)
    for pnl, uid in rows:
        if pnl is not None:
            clusters[uid].append(pnl)
    keys = list(clusters)
    if len(keys) < 2:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        pool = []
        for _ in range(len(keys)):
            pool.extend(clusters[rng.choice(keys)])
        if pool:
            means.append(sum(pool) / len(pool))
    if not means:
        return (None, None)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (round(lo, 4), round(hi, 4))


def _agg(rows):
    """rows: list of (realized_pnl, uid). -> summary incl. trader-clustered 95% CI."""
    pnls = [p for p, _ in rows if p is not None]
    n = len(pnls)
    if not n:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "sum": 0.0,
                "n_traders": 0, "ci_low": None, "ci_high": None}
    wins = sum(1 for p in pnls if p > 0)
    lo, hi = _bootstrap_ci_clustered(rows)
    return {"n": n, "mean": round(sum(pnls) / n, 4), "median": _median(pnls),
            "win_rate": round(wins / n, 4), "sum": round(sum(pnls), 2),
            "n_traders": len({uid for _, uid in rows}), "ci_low": lo, "ci_high": hi}


def _eff_band(frozen, live_hold, fast_max, holder_min):
    return frozen if frozen else band_of(live_hold, fast_max, holder_min)


def hold_band_readout(conn, now_iso=None, fast_max=FAST_MAX_S, holder_min=HOLDER_MIN_S):
    """By-band / by-source / by-(band×arm) aggregation of CLOSED copy-PnL, using the FROZEN band.
    Also returns by_band_open (censoring diagnostic). Monitoring-safe; interpretation gated."""
    rows = conn.execute("""
        SELECT p.realized_pnl AS pnl, p.source_user_id AS uid, p.strategy AS arm,
               p.hold_band AS frozen, p.is_open AS is_open,
               a.median_hold_s AS hold, w.discovery_source AS src
        FROM memecoin_positions p
        LEFT JOIN memecoin_trader_analytics a ON a.source_user_id = p.source_user_id
        LEFT JOIN memecoin_wallets w ON w.source_user_id = p.source_user_id""").fetchall()
    by_band, by_source, by_band_arm = {}, {}, {}
    open_by_band = defaultdict(int)
    for r in rows:
        band = _eff_band(r["frozen"], r["hold"], fast_max, holder_min)
        if r["is_open"]:
            open_by_band[band] += 1
            continue
        arm = r["arm"] or "control"
        src = r["src"] or "unknown"
        rec = (r["pnl"], r["uid"])
        by_band.setdefault(band, []).append(rec)
        by_source.setdefault(src, []).append(rec)
        by_band_arm.setdefault(f"{band}|{arm}", []).append(rec)
    return {
        "by_band": {k: _agg(v) for k, v in by_band.items()},
        "by_source": {k: _agg(v) for k, v in by_source.items()},
        "by_band_arm": {k: _agg(v) for k, v in by_band_arm.items()},
        "by_band_open": dict(open_by_band),   # censoring diagnostic (open positions per band)
        "as_of": now_iso,
        "total_closed": sum(len(v) for v in by_band.values()),
    }


def decision(readout):
    """Legacy modeled outcomes cannot establish a hold-band conclusion."""
    return {"h1": "blocked_protocol", "direction": None, "reason": "complete one-second executable paths and future validation required"}


def gates_met(readout, today, min_date="2026-07-21", band_min_n=30, arm_min_n=20, arm_min_traders=5):
    """Pre-registered gates: >=band_min_n per band, >=arm_min_n AND >=arm_min_traders per Arm-A
    fast&holder cell, and not before min_date. Trader floor guards the clustering (n positions can
    be many but from 2-3 traders). Returns (met, reasons-unmet)."""
    reasons = ["protocol_missing_or_legacy"]
    if str(today) < str(min_date):
        reasons.append(f"calendar: {today} < {min_date}")
    for b in ("fast", "med", "holder"):
        n = readout["by_band"].get(b, {}).get("n", 0)
        if n < band_min_n:
            reasons.append(f"band {b}: n={n} < {band_min_n}")
    for cell in ("fast|momentum_a", "holder|momentum_a"):
        c = readout["by_band_arm"].get(cell, {})
        if c.get("n", 0) < arm_min_n:
            reasons.append(f"cell {cell}: n={c.get('n', 0)} < {arm_min_n}")
        if c.get("n_traders", 0) < arm_min_traders:
            reasons.append(f"cell {cell}: n_traders={c.get('n_traders', 0)} < {arm_min_traders}")
    return (len(reasons) == 0, reasons)
