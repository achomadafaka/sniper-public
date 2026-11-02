"""Pure Hyperliquid cascade detector + exploitability primitives. No I/O.

Thesis 2 (primary): detect a realized cascade from its public footprint (fast adverse move +
OI drop + trade-count spike) and test whether the AFTERMATH is tradeable net of the 9 bps taker
fee. All detector constants are pre-registered and frozen; any change must bump DETECTOR_VERSION.
See docs/superpowers/specs/2026-06-25-liquidation-cascade-detector-design.md."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import ROUND_TRIP_FEE_BPS

DETECTOR_VERSION = "v1"

_MAJORS = {"BTC", "ETH", "SOL"}


@dataclass(frozen=True)
class DetectorParams:
    W_sec: int = 60
    bucket_sec: int = 10
    k: float = 3.0
    min_move_bps: float = 30.0
    d_oi_frac: float = 0.01          # OI must drop >= 1% over the window
    m_trade: float = 3.0             # trade_count_w >= m * median(trade_count_w)
    sigma_lookback_sec: int = 1800   # 30 min
    min_sigma_obs: int = 30
    cooldown_sec: int = 300
    version: str = DETECTOR_VERSION


def magnitude_bucket(ret_bps: float) -> str:
    a = abs(ret_bps)
    if a < 60:
        return "30-60"
    if a < 120:
        return "60-120"
    if a < 250:
        return "120-250"
    return "250+"


def asset_class(coin: str) -> str:
    return "major" if (coin or "").upper() in _MAJORS else "alt"


def detect_cascades(series, params: DetectorParams = DetectorParams()):
    """series: obs dicts for ONE asset ascending in ts_ms. Returns version-stamped event dicts."""
    n = len(series)
    if n < 2:
        return []
    asset = series[0]["asset"]
    w = max(1, round(params.W_sec / params.bucket_sec))
    cooldown_ms = params.cooldown_sec * 1000

    mids = [r.get("mid") for r in series]
    # per-bucket simple returns (for σ); a None/0 mid (transient capture gap) -> 0 return, never crash.
    rets = [0.0]
    for i in range(1, n):
        p = mids[i - 1]
        rets.append((mids[i] - p) / p if (p and mids[i]) else 0.0)

    # current-window trade counts; the spike baseline uses PRIOR windows only (causal).
    tcw = [None] * n
    for i in range(n):
        if i >= w:
            tcw[i] = sum(series[j]["n_trades"] or 0 for j in range(i - w + 1, i + 1))

    lookback = round(params.sigma_lookback_sec / params.bucket_sec)
    events = []
    last_ts = None
    for i in range(w, n):
        base = i - w                         # last bucket BEFORE the event window (causal cutoff)
        # prior-only σ: realized vol of returns ending at `base`; EXCLUDES the move window so the
        # move can't inflate its own σ (and no look-ahead into the event/future).
        lo = max(1, base - lookback + 1)
        wr = rets[lo:base + 1]
        if len(wr) < params.min_sigma_obs:
            continue
        mu = sum(wr) / len(wr)
        var = sum((x - mu) ** 2 for x in wr) / (len(wr) - 1)
        sigma = math.sqrt(var) if var > 0 else 0.0

        mid_now, mid_prev = mids[i], mids[i - w]
        if not mid_prev or not mid_now:        # None/0 mid (capture gap) -> skip, never crash
            continue
        ret_w = (mid_now - mid_prev) / mid_prev
        ret_w_bps = ret_w * 10000
        if abs(ret_w_bps) < params.min_move_bps:
            continue
        if sigma <= 0 or abs(ret_w) < params.k * sigma:
            continue

        oi_now, oi_prev = series[i].get("oi"), series[i - w].get("oi")
        if not oi_now or not oi_prev or oi_prev <= 0:      # fail-closed on missing/stale OI
            continue
        oi_delta = (oi_now - oi_prev) / oi_prev
        if oi_delta > -params.d_oi_frac:
            continue

        # prior-only baseline median trade count: windows ending at/<= `base` (never the future).
        lo2 = max(w, base - lookback + 1)
        prior_counts = [tcw[j] for j in range(lo2, base + 1) if tcw[j] is not None]
        if not prior_counts:
            continue
        med_prior = sorted(prior_counts)[len(prior_counts) // 2]
        if med_prior <= 0 or (tcw[i] or 0) < params.m_trade * med_prior:
            continue

        ts = series[i]["ts_ms"]
        if last_ts is not None and ts - last_ts < cooldown_ms:
            continue

        direction = 1 if ret_w > 0 else -1
        events.append({
            "event_id": f"{asset}:{ts}:{params.version}",
            "asset": asset, "ts_ms": ts, "direction": direction,
            "ret_w": ret_w_bps, "sigma_w": sigma * 10000, "oi_delta_w": oi_delta * 100,
            "trade_count_w": tcw[i], "traded_notional_w": sum(
                series[j]["notional"] or 0 for j in range(i - w + 1, i + 1)),
            "magnitude_bucket": magnitude_bucket(ret_w_bps),
            "detector_version": params.version,
        })
        last_ts = ts
    return events


# --- forward aftermath measurement (Thesis 2 exploitability) ---

def _nearest_mid(series, target_ms, tol_ms, *, after=False):
    """Mid of the obs nearest target_ms within tol_ms. after=True restricts to obs AT OR AFTER
    target (closest forward) — never an obs before the horizon, so the forward measurement can't
    peek at a pre-horizon price. after=False is symmetric (used for the entry point). None if none."""
    best, best_d = None, None
    for r in series:
        dt = r["ts_ms"] - target_ms
        if after:
            if dt < 0 or dt > tol_ms:
                continue
            d = dt
        else:
            d = abs(dt)
            if d > tol_ms:
                continue
        if best_d is None or d < best_d:
            best, best_d = r.get("mid"), d
    return best


def aligned_moves(events, series_by_asset, horizon_ms, mode, *, tol_ms=15_000):
    """Pre-fee aligned forward move per event. mode: 'continuation' (dir*fwd) or 'reversion'
    (-dir*fwd). The 9 bps fee is applied ONCE later, on the mean, in the gate. Returns
    [{'asset','ts_ms','bps'}]; events without a usable forward mid are dropped."""
    if mode not in ("continuation", "reversion"):
        raise ValueError(f"bad mode {mode!r}")
    out = []
    for e in events:
        series = series_by_asset.get(e["asset"]) or []
        m0 = _nearest_mid(series, e["ts_ms"], tol_ms)
        m1 = _nearest_mid(series, e["ts_ms"] + horizon_ms, tol_ms, after=True)
        if not m0 or m1 is None or m0 <= 0:
            continue
        fwd_bps = (m1 - m0) / m0 * 10000
        aligned = e["direction"] * fwd_bps
        if mode == "reversion":
            aligned = -aligned
        out.append({"asset": e["asset"], "ts_ms": e["ts_ms"], "bps": aligned})
    return out


# --- negative-control battery (an edge must survive ALL) ---

def control_flip(events, series_by_asset, horizon_ms, mode, *, tol_ms=15_000, **_):
    """Direction flip: a directional edge must die when the prediction is reversed."""
    flipped = [{**e, "direction": -e["direction"]} for e in events]
    return aligned_moves(flipped, series_by_asset, horizon_ms, mode, tol_ms=tol_ms)


def control_shuffle(events, series_by_asset, horizon_ms, mode, *, rng, tol_ms=15_000, **_):
    """Asset shuffle: measure each event's forward move against a random OTHER asset's series.
    A real cascade response must null here (else it's market-wide drift)."""
    assets = list(series_by_asset)
    out = []
    for e in events:
        others = [a for a in assets if a != e["asset"]]
        if not others:
            continue
        sub = rng.choice(others)
        m0 = _nearest_mid(series_by_asset[sub], e["ts_ms"], tol_ms)
        m1 = _nearest_mid(series_by_asset[sub], e["ts_ms"] + horizon_ms, tol_ms, after=True)
        if not m0 or m1 is None or m0 <= 0:
            continue
        fwd = (m1 - m0) / m0 * 10000
        aligned = e["direction"] * fwd
        if mode == "reversion":
            aligned = -aligned
        out.append({"asset": e["asset"], "ts_ms": e["ts_ms"], "bps": aligned})
    return out


def control_shift(events, series_by_asset, horizon_ms, mode, *, shift_ms=1_800_000,
                  tol_ms=15_000, **_):
    """Event-time shift: a real reaction decays once the window is moved off the event."""
    shifted = [{**e, "ts_ms": e["ts_ms"] + shift_ms} for e in events]
    res = aligned_moves(shifted, series_by_asset, horizon_ms, mode, tol_ms=tol_ms)
    for r in res:                       # restore the true event time (each result ts = shifted ts)
        r["ts_ms"] = r["ts_ms"] - shift_ms
    return res


def _hour_bucket(ts_ms):
    return (ts_ms // 3_600_000) % 24


def control_matched(events, series_by_asset, horizon_ms, mode, *,
                    params: DetectorParams = DetectorParams(), rng, tol_ms=15_000, n_per_event=1,
                    exclude_events=None, **_):
    """Matched non-event windows: same asset, same hour-of-day bucket, not within cooldown of ANY
    real cascade. A real edge must null here (else it's just time-of-day). `exclude_events` is the
    full event set to keep out of the non-event pool (pass ALL detected events, not just the cell's
    subset, so another cell's cascade can't leak in as a 'non-event')."""
    cooldown_ms = params.cooldown_sec * 1000
    excl = exclude_events if exclude_events is not None else events
    by_asset_evts = {}
    for e in excl:
        by_asset_evts.setdefault(e["asset"], []).append(e["ts_ms"])
    out = []
    for e in events:
        series = series_by_asset.get(e["asset"]) or []
        evt_ts = by_asset_evts.get(e["asset"], [])
        cands = [r["ts_ms"] for r in series
                 if _hour_bucket(r["ts_ms"]) == _hour_bucket(e["ts_ms"])
                 and all(abs(r["ts_ms"] - t) >= cooldown_ms for t in evt_ts)]
        rng.shuffle(cands)
        for t0 in cands[:n_per_event]:
            m0 = _nearest_mid(series, t0, tol_ms)
            m1 = _nearest_mid(series, t0 + horizon_ms, tol_ms, after=True)
            if not m0 or m1 is None or m0 <= 0:
                continue
            fwd = (m1 - m0) / m0 * 10000
            aligned = e["direction"] * fwd
            if mode == "reversion":
                aligned = -aligned
            out.append({"asset": e["asset"], "ts_ms": t0, "bps": aligned})
    return out


# --- exploitability statistics (clustered-t / BH-FDR / chronological thirds) ---

def _cluster_means(rows):
    buckets = {}
    for r in rows:
        key = (r["asset"], r["ts_ms"] // 3_600_000)        # asset × hour
        buckets.setdefault(key, []).append(r["bps"])
    return [sum(v) / len(v) for v in buckets.values()]


def clustered_t(rows) -> float:
    """One-sample t over (asset × hour) cluster mean bps; H0: mean <= 0."""
    vals = _cluster_means(rows)
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0 if mean <= 0 else 999.0
    return mean / math.sqrt(var / n)


def t_to_p(t, df) -> float:
    """One-sided p-value, normal approximation (df unused; approx acceptable per FDR docstring)."""
    p = 0.5 * math.erfc(t / math.sqrt(2))
    return min(1.0, max(0.0, p))


def bh_fdr(pairs, q=0.10) -> set:
    """Benjamini-Hochberg survivors. pairs=[(key,p)]. APPROXIMATE (cells are non-independent
    nested subsets) — a p-hacking guard, not an exact FDR. Mirrors crypto5m_lane_eval.bh_fdr."""
    valid = sorted(((k, p) for k, p in pairs if p is not None), key=lambda x: x[1])
    m = len(valid)
    if m == 0:
        return set()
    rank = 0
    for i, (_k, p) in enumerate(valid, 1):
        if p <= (i / m) * q:
            rank = i
    return {k for i, (k, _p) in enumerate(valid, 1) if i <= rank}


def thirds_positive(rows) -> int:
    """Count of chronological thirds (split by ts_ms) whose mean bps > 0."""
    rows = sorted(rows, key=lambda r: r["ts_ms"])
    n = len(rows)
    if n == 0:
        return 0
    cut = max(1, n // 3)
    thirds = [rows[:cut], rows[cut:2 * cut], rows[2 * cut:]]
    out = 0
    for t in thirds:
        if t and sum(r["bps"] for r in t) / len(t) > 0:
            out += 1
    return out


# --- canonical exploitability gate (single source of truth; see spec §3) ---

def cell_passes_gate(cell, *, fdr_survivors, min_n=40, fee_bps=ROUND_TRIP_FEE_BPS) -> bool:
    """An (hypothesis × cell) is an EDGE iff ALL hold: n>=min_n, after-fee>0 (mean - fee),
    clustered-t>2, positive in >=2/3 chronological thirds, passes the control battery, and
    survives BH-FDR across the grid."""
    return bool(
        cell["n"] >= min_n
        and (cell["mean_bps"] - fee_bps) > 0
        and cell["t"] > 2.0
        and cell["thirds_pos"] >= 2
        and cell["controls_pass"]
        and cell["key"] in fdr_survivors
    )
