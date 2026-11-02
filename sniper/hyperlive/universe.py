"""State-aware universe scanner. One cheap metaAndAssetCtxs call gives per-coin state for
all ~230 HL perps; we score each by 'where something is happening' (dislocation, funding
crowding, premium, 24h move, OI change) above a liquidity floor, then the engine subscribes
deep feeds only to the top opportunities. Transparent, tunable weights (flexible-A spirit).
"""
from __future__ import annotations

import json
import math
import os

# Liquidity floor — below this 24h notional volume we can't execute, so never watch it.
MIN_VOLUME_USD = 5_000_000.0

# Each signal is normalized to ~0-10+ "interestingness points" at the scale where it
# becomes notable, THEN weighted by importance. Cascade-precursors (dislocation, funding
# crowding, OI shift) lead; a raw 24h move is generic activity; volume is a small tiebreak.
# Normalizers = the level at which a signal scores ~1 point.
N_DISLOCATION = 0.0002          # 2 bps mark-oracle gap = 1 pt
N_FUNDING = 0.00005             # 0.5 bps/interval funding = 1 pt
N_PREMIUM = 0.001               # 10 bps premium = 1 pt
N_MOVE = 0.01                   # 1% 24h move = 1 pt
N_OI_CHANGE = 0.02              # 2% OI change = 1 pt
CAP_PTS = 12.0                  # no single signal dominates past this

# Hand-set weights (the baseline `hand_score_v0`). Live selection uses LEARNED weights when a
# fitted file is present (promoted by the recall horse-race); otherwise falls back to these.
HAND_WEIGHTS = {"dislocation": 1.5, "funding": 1.5, "oi_shift": 1.5, "premium": 1.0,
                "move_24h": 1.0, "volume": 0.5}


def raw_points(ctx: dict, prev_oi: float | None = None) -> dict:
    """Per-component normalized points (0..CAP), weight-free — the features the score and the
    learner both build on."""
    vol = _f(ctx.get("dayNtlVlm"))
    mark, oracle = _f(ctx.get("markPx")), _f(ctx.get("oraclePx"))
    mid = _f(ctx.get("midPx")) or mark
    funding, premium, prev_day = _f(ctx.get("funding")), _f(ctx.get("premium")), _f(ctx.get("prevDayPx"))
    oi = _f(ctx.get("openInterest"))
    disloc = abs(mark - oracle) / oracle if (mark and oracle) else 0.0
    move = abs(mid - prev_day) / prev_day if (mid and prev_day) else 0.0
    oi_change = abs(oi - prev_oi) / prev_oi if (prev_oi and prev_oi > 0 and oi) else 0.0
    return {
        "dislocation": min(CAP_PTS, disloc / N_DISLOCATION),
        "funding": min(CAP_PTS, abs(funding) / N_FUNDING),
        "oi_shift": min(CAP_PTS, oi_change / N_OI_CHANGE),
        "premium": min(CAP_PTS, abs(premium) / N_PREMIUM),
        "move_24h": min(CAP_PTS, move / N_MOVE),
        "volume": max(0.0, math.log10(max(vol, 1.0)) - 6.0),
        "_disloc_bps": disloc * 1e4, "_funding_bps": abs(funding) * 1e4,
        "_premium_bps": abs(premium) * 1e4, "_move_pct": move * 100, "_oi_change_pct": oi_change * 100,
        "_vol": vol,
    }


def opportunity(ctx: dict, prev_oi: float | None = None, weights: dict | None = None) -> dict:
    """Score one market. weights = learned (live) or HAND_WEIGHTS (baseline)."""
    w = weights or HAND_WEIGHTS
    raw = raw_points(ctx, prev_oi)
    vol = raw["_vol"]
    scored = {c: w.get(c, 0.0) * raw[c] for c in ("dislocation", "funding", "oi_shift", "premium", "move_24h")}
    score = sum(scored.values()) + w.get("volume", 0.5) * raw["volume"]
    lead = max(scored, key=scored.get)
    labels = {"dislocation": "mark-oracle dislocation", "funding": "funding extreme",
              "oi_shift": "OI shift", "premium": "premium extreme", "move_24h": "24h move"}
    return {
        "score": round(score, 2),
        "tradeable": vol >= MIN_VOLUME_USD,
        "volume_usd": vol,
        "components": {
            "dislocation_bps": round(raw["_disloc_bps"], 1),
            "funding_bps": round(raw["_funding_bps"], 2),
            "premium_bps": round(raw["_premium_bps"], 1),
            "move_pct": round(raw["_move_pct"], 2),
            "oi_change_pct": round(raw["_oi_change_pct"], 1),
        },
        "lead_reason": labels[lead] if scored[lead] > 0 else "liquid baseline",
    }


def select_universe(contexts: list[dict], prev_oi: dict | None = None, *,
                    max_assets: int = 12, min_volume: float = MIN_VOLUME_USD,
                    weights: dict | None = None) -> list[dict]:
    """Top tradeable opportunities for live selection + the UI (asset/score/components/
    lead_reason/tradeable/volume_usd)."""
    out = []
    for r in score_all(contexts, prev_oi, weights):
        if r["volume_usd"] < min_volume:
            continue
        out.append({"asset": r["asset"], "score": r["score"], "components": r["components"],
                    "lead_reason": r["lead_reason"], "tradeable": r["tradeable"], "volume_usd": r["volume_usd"]})
        if len(out) >= max_assets:
            break
    return out


def score_all(contexts: list[dict], prev_oi: dict | None = None, weights: dict | None = None) -> list[dict]:
    """Score EVERY market (not just the top 12), ranked, with the raw ctx attached — the
    spine of the Step-0 recall log. Untradeable markets are included (selected_bool is
    decided by the caller against the volume floor)."""
    prev_oi = prev_oi or {}
    rows = []
    for c in contexts:
        name = c.get("name")
        ctx = c.get("ctx") or {}
        if not name:
            continue
        opp = opportunity(ctx, prev_oi.get(name), weights)
        rows.append({"asset": str(name).upper(), "ctx": ctx, **opp})
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def change_signals(ctx: dict, prev_ctx: dict | None = None, prev2_ctx: dict | None = None) -> dict:
    """Step-1 SHADOW rate/acceleration components from refresh-to-refresh context. Levels
    describe the room after the fight; rates catch it forming. Logged, never drives selection."""
    basis, basis_p, basis_p2 = _basis(ctx), _basis(prev_ctx), _basis(prev2_ctx)
    basis_change = (basis - basis_p) if (basis is not None and basis_p is not None) else 0.0
    prev_change = (basis_p - basis_p2) if (basis_p is not None and basis_p2 is not None) else 0.0
    basis_accel = basis_change - prev_change
    mid, mid_p = _fn(ctx, "midPx"), _fn(prev_ctx, "midPx")
    mid_return = (mid - mid_p) / mid_p if (mid and mid_p) else 0.0
    oi, oi_p, oi_p2 = _fn(ctx, "openInterest"), _fn(prev_ctx, "openInterest"), _fn(prev2_ctx, "openInterest")
    oi_delta_pct = (oi - oi_p) / oi_p if (oi and oi_p) else 0.0
    oi_delta_prev = (oi_p - oi_p2) / oi_p2 if (oi_p and oi_p2) else 0.0
    oi_accel = oi_delta_pct - oi_delta_prev
    prem, prem_p = _fn(ctx, "premium"), _fn(prev_ctx, "premium")
    premium_change = (prem - prem_p) if (prem is not None and prem_p is not None) else 0.0
    change_score = (HAND_WEIGHTS["dislocation"] * min(CAP_PTS, abs(basis_change) / N_DISLOCATION)
                    + HAND_WEIGHTS["dislocation"] * min(CAP_PTS, abs(basis_accel) / N_DISLOCATION)
                    + HAND_WEIGHTS["move_24h"] * min(CAP_PTS, abs(mid_return) / N_MOVE)
                    + HAND_WEIGHTS["oi_shift"] * min(CAP_PTS, abs(oi_delta_pct) / N_OI_CHANGE)
                    + HAND_WEIGHTS["premium"] * min(CAP_PTS, abs(premium_change) / N_PREMIUM))
    return {
        "basis_bps": round((basis or 0.0) * 1e4, 2),
        "basis_change_bps": round(basis_change * 1e4, 2),
        "basis_accel_bps": round(basis_accel * 1e4, 3),
        "basis_sign": (1 if (basis or 0) > 0 else -1 if (basis or 0) < 0 else 0),
        "mid_return_bps": round(mid_return * 1e4, 2),
        "oi_delta_pct": round(oi_delta_pct * 100, 3),
        "oi_accel_pct": round(oi_accel * 100, 3),
        "oi_collapse": oi_delta_pct < -0.05,
        "premium_change_bps": round(premium_change * 1e4, 2),
        "change_score_v0": round(change_score, 2),
    }


def _basis(ctx):
    if not ctx:
        return None
    try:
        mark, oracle = float(ctx.get("markPx")), float(ctx.get("oraclePx"))
    except (TypeError, ValueError):
        return None
    return (mark - oracle) / oracle if oracle else None


def _fn(ctx, key):
    if not ctx:
        return None
    try:
        return float(ctx.get(key))
    except (TypeError, ValueError):
        return None


def oi_map(contexts: list[dict]) -> dict:
    """Current OI per asset, to diff against on the next refresh (ΔOI)."""
    out = {}
    for c in contexts:
        name = c.get("name")
        oi = _f((c.get("ctx") or {}).get("openInterest"))
        if name and oi:
            out[name] = oi
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ── Learned weights (promoted by the recall horse-race) ───────────────────────
WEIGHTS_PATH = "/home/bigpapi/Desktop/Sniper/data/hyperlive_scan_weights.json"
# Components the live score reads weights for. volume is a tiebreak, fit-fixed at HAND value.
WEIGHT_KEYS = ("dislocation", "funding", "oi_shift", "premium", "move_24h", "volume")


def load_weights(path: str = WEIGHTS_PATH) -> dict | None:
    """Read a fitted weights file → weights dict, or None if absent/malformed (caller then
    falls back to HAND_WEIGHTS, so selection never breaks on a bad file)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    w = d.get("weights") if isinstance(d, dict) else None
    if not isinstance(w, dict):
        return None
    out = {k: float(v) for k, v in w.items() if k in WEIGHT_KEYS and isinstance(v, (int, float))}
    return out or None


def save_weights(weights: dict, meta: dict | None = None, path: str = WEIGHTS_PATH) -> None:
    """Atomically write a fitted weights file. meta carries provenance (n movers, fit_ts, etc.)."""
    payload = {"weights": {k: round(float(weights[k]), 4) for k in weights if k in WEIGHT_KEYS}}
    if meta:
        payload["meta"] = meta
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
