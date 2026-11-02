"""Phase 2B — probability ensemble (pure).

Combines the component probabilities (q_market, structural, MC, residual, ...) via a weighted
LOG-OPINION POOL (weighted mean in logit space — the standard calibrated way to pool probabilities), and
reports a predictive uncertainty (component dispersion) and an UNCERTAINTY-SHRUNK conservative probability
that pulls toward the 0.5 prior as disagreement grows and never blows to 0/1. The conservative value is
what the decision/sizing layers consume, so model disagreement automatically shrinks edge and size — the
fix for the v1 cohort, where agreement on an extreme drove q_trade to 0/1 and manufactured fake favorite
edges.
"""
from __future__ import annotations

import math

EPS = 1e-6


def _logit(p):
    p = min(1.0 - EPS, max(EPS, float(p)))
    return math.log(p / (1.0 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


def ensemble_probability(components, *, weights=None, shrink_k=2.0) -> dict:
    """components: {name: q in [0,1]}; None values dropped. Returns q_ensemble (log-pool), q_conservative
    (shrunk toward 0.5 by k*uncertainty), predictive_uncertainty (component std), and the inputs used."""
    items = [(n, float(q)) for n, q in (components or {}).items() if q is not None]
    if not items:
        return {"q_ensemble": None, "q_conservative": None, "predictive_uncertainty": None,
                "n_components": 0, "weights": {}, "components": {}}

    w = {n: float((weights or {}).get(n, 1.0)) for n, _ in items}
    wsum = sum(w.values()) or 1.0
    logit_ens = sum(w[n] * _logit(q) for n, q in items) / wsum
    q_ens = _sigmoid(logit_ens)

    probs = [q for _, q in items]
    if len(probs) > 1:
        mean = sum(probs) / len(probs)
        uncertainty = (sum((q - mean) ** 2 for q in probs) / (len(probs) - 1)) ** 0.5
    else:
        uncertainty = 0.0

    shrink = max(0.0, 1.0 - shrink_k * uncertainty)
    q_cons = 0.5 + (q_ens - 0.5) * shrink

    return {
        "q_ensemble": q_ens,
        "q_conservative": q_cons,
        "predictive_uncertainty": uncertainty,
        "n_components": len(items),
        "weights": {n: w[n] / wsum for n, _ in items},
        "components": {n: q for n, q in items},
    }
