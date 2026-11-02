"""Phase 2A — multi-source latent BTC-state fusion (pure).

Combines BTC price across exchanges into a robust latent estimate with an explicit uncertainty and a
source-disagreement measure. Robust (weighted median) so a single bad/outlier feed cannot drag the latent.
Stale/gapped/too-old sources are dropped. High disagreement raises uncertainty and degrades the state so
downstream sizing can shrink toward zero. NEVER treats any source as settlement truth — Chainlink remains
the settlement reference elsewhere; this is a predictor-state estimate only.
"""
from __future__ import annotations

DISAGREEMENT_DEGRADED_BPS = 25.0   # >0.25% spread across sources -> degraded state


def _usable(s, max_age_ms):
    if s.get("stale") or s.get("gap"):
        return False
    p = s.get("price")
    age = s.get("source_age_ms")
    return p is not None and float(p) > 0 and age is not None and float(age) < max_age_ms


def _weight(s, max_age_ms):
    freshness = max(0.0, 1.0 - float(s["source_age_ms"]) / max_age_ms)
    te = s.get("tracking_error")
    reliability = 1.0 / (1.0 + float(te)) if te is not None else 1.0
    return freshness * reliability


def _weighted_median(pairs):
    """pairs: list of (price, weight), weight>0. Lower weighted median (first price whose cumulative
    weight reaches half the total)."""
    ordered = sorted(pairs, key=lambda x: x[0])
    total = sum(w for _, w in ordered)
    half, cum = total / 2.0, 0.0
    for price, w in ordered:
        cum += w
        if cum >= half:
            return price
    return ordered[-1][0]


def fuse_latent_state(sources, *, max_age_ms=2000) -> dict:
    usable = [s for s in sources if _usable(s, max_age_ms)]
    n = len(usable)
    if n == 0:
        return {"latent_btc_price": None, "latent_price_uncertainty": None,
                "source_disagreement_abs": None, "source_disagreement_bps": None,
                "source_contribution_weights": {}, "state_quality": "unusable", "n_usable": 0}

    weights = {s["source"]: _weight(s, max_age_ms) for s in usable}
    wsum = sum(weights.values()) or 1.0
    contrib = {k: v / wsum for k, v in weights.items()}   # unrounded so contributions sum to exactly 1.0
    prices = [float(s["price"]) for s in usable]
    latent = _weighted_median([(float(s["price"]), weights[s["source"]]) for s in usable])

    disagreement = max(prices) - min(prices)
    disagreement_bps = (disagreement / latent) * 1e4 if latent else 0.0
    uncertainty = (sum(weights[s["source"]] * (float(s["price"]) - latent) ** 2 for s in usable)
                   / wsum) ** 0.5

    if n == 1:
        quality = "single_source"
    elif disagreement_bps > DISAGREEMENT_DEGRADED_BPS:
        quality = "degraded"
    else:
        quality = "ok"

    return {
        "latent_btc_price": round(latent, 4),
        "latent_price_uncertainty": round(uncertainty, 4),
        "source_disagreement_abs": round(disagreement, 4),
        "source_disagreement_bps": round(disagreement_bps, 4),
        "source_contribution_weights": contrib,
        "state_quality": quality,
        "n_usable": n,
    }
