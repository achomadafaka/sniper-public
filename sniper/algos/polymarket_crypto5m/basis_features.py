"""Feature family D — Binance↔Chainlink basis, known-at-decision.

Basis = ``binance_price - chainlink_price``. A basis observation is formed ONLY when the two sources'
event timestamps are sufficiently aligned (``|skew| <= max_skew_ms``); on excessive skew the basis fields
are ``None`` and ``basis_quality = "stale_or_misaligned"`` — we never silently interpolate across a gap.
Both inputs obey the strict known-at-time cutoff (source_ts <= cutoff AND recv <= decision). Binance stays
a predictor lane — never settlement truth, never the label.
"""
from __future__ import annotations

import statistics

DEFAULT_MAX_SKEW_MS = 1000
DEFAULT_MAX_SOURCE_AGE_MS = 10_000   # a basis built from data >10s old vs the decision is stale (1s feeds)
BASIS_CHANGE_HORIZONS = (5, 15, 30, 60)
BASIS_VOL_HORIZONS = (30, 60)

_KEYS = (["binance_chainlink_basis_abs", "binance_chainlink_basis_bps", "binance_source_age_ms",
          "chainlink_source_age_ms", "basis_observation_skew_ms"]
         + [f"basis_change_{h}s" for h in BASIS_CHANGE_HORIZONS]
         + [f"basis_volatility_{h}s" for h in BASIS_VOL_HORIZONS])


def _known_binance(obs, *, cutoff_ms, decision_ts, expected_symbol=None):
    out = [o for o in (obs or [])
           if o.get("source_event_ts") is not None and int(o["source_event_ts"]) <= cutoff_ms
           and o.get("receive_ts") is not None and int(o["receive_ts"]) <= decision_ts
           and o.get("price") is not None
           and (expected_symbol is None or o.get("symbol") in (None, expected_symbol))]
    out.sort(key=lambda o: o["source_event_ts"])
    return out


def _known_chainlink(ticks, *, cutoff_ms, decision_ts):
    out = [t for t in (ticks or [])
           if int(t["source_ts_ms"]) <= cutoff_ms
           and t.get("recv_ts") is not None and int(t["recv_ts"]) <= decision_ts]
    out.sort(key=lambda t: t["source_ts_ms"])
    return out


def _basis_series(bobs, cl, *, max_skew_ms):
    """Pair each chainlink tick with the nearest-in-time binance obs within ``max_skew_ms``.
    Returns ``[(ts_ms, basis)]`` sorted by ts. No pairing across a gap larger than the skew limit."""
    if not bobs or not cl:
        return []
    out = []
    for t in cl:
        nearest = min(bobs, key=lambda o: abs(int(o["source_event_ts"]) - int(t["source_ts_ms"])))
        if abs(int(nearest["source_event_ts"]) - int(t["source_ts_ms"])) <= max_skew_ms:
            out.append((int(t["source_ts_ms"]), float(nearest["price"]) - float(t["price"])))
    return out


def _basis_change(series, *, now_ms, horizon_s):
    if len(series) < 2:
        return None
    b_now = series[-1][1]
    cut = now_ms - horizon_s * 1000
    prior = [b for ts, b in series if ts <= cut]
    return (b_now - prior[-1]) if prior else None


def _basis_vol(series, *, now_ms, horizon_s):
    lo = now_ms - horizon_s * 1000
    win = [b for ts, b in series if lo <= ts <= now_ms]
    if len(win) < 3:
        return None
    diffs = [win[i] - win[i - 1] for i in range(1, len(win))]
    return statistics.pstdev(diffs) if len(diffs) >= 2 else None


def build_basis_features(binance_obs, chainlink_ticks, *, cutoff_ts, decision_ts,
                         max_skew_ms=DEFAULT_MAX_SKEW_MS, max_source_age_ms=DEFAULT_MAX_SOURCE_AGE_MS,
                         expected_symbol=None) -> dict:
    now_ms = int(decision_ts) * 1000
    cutoff_ms = int(cutoff_ts) * 1000
    feats = {k: None for k in _KEYS}
    feats["basis_quality"] = "unavailable"

    bobs = _known_binance(binance_obs, cutoff_ms=cutoff_ms, decision_ts=decision_ts,
                          expected_symbol=expected_symbol)
    cl = _known_chainlink(chainlink_ticks, cutoff_ms=cutoff_ms, decision_ts=decision_ts)
    if not bobs or not cl:
        return feats

    # Pair the LATEST chainlink tick to its NEAREST binance obs (same rule as the basis series), so the
    # point basis is consistent with basis_change/volatility — not a stale latest-vs-latest subtraction.
    lc = cl[-1]
    nb = min(bobs, key=lambda o: abs(int(o["source_event_ts"]) - int(lc["source_ts_ms"])))
    feats["binance_source_age_ms"] = now_ms - int(nb["source_event_ts"])
    feats["chainlink_source_age_ms"] = now_ms - int(lc["source_ts_ms"])
    skew = abs(int(nb["source_event_ts"]) - int(lc["source_ts_ms"]))
    feats["basis_observation_skew_ms"] = skew

    if skew > max_skew_ms:
        feats["basis_quality"] = "stale_or_misaligned"     # do NOT interpolate — leave basis None
        return feats
    if (feats["binance_source_age_ms"] > max_source_age_ms
            or feats["chainlink_source_age_ms"] > max_source_age_ms):
        feats["basis_quality"] = "stale_or_misaligned"     # aligned but both sides too old vs decision
        return feats

    feats["basis_quality"] = "ok"
    basis_abs = float(nb["price"]) - float(lc["price"])
    feats["binance_chainlink_basis_abs"] = basis_abs
    feats["binance_chainlink_basis_bps"] = (basis_abs / float(lc["price"]) * 1e4
                                            if float(lc["price"]) > 0 else None)
    series = _basis_series(bobs, cl, max_skew_ms=max_skew_ms)
    for h in BASIS_CHANGE_HORIZONS:
        feats[f"basis_change_{h}s"] = _basis_change(series, now_ms=now_ms, horizon_s=h)
    for h in BASIS_VOL_HORIZONS:
        feats[f"basis_volatility_{h}s"] = _basis_vol(series, now_ms=now_ms, horizon_s=h)
    return feats
