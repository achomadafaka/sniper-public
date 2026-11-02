"""Phase 2A — structural and residual probability models.

Q2 ``monte_carlo_up_probability`` — vectorized, seeded Monte Carlo of P(S_end >= PriceToBeat): driftless
log-return diffusion + optional Poisson jumps. Generalizes the closed-form ThresholdDiffusionV0 and adds
jump/regime mass. Beating Q1 is not assumed — it must be shown OOS on captured data before any use.

Q3 ``fit_residual_logistic`` / ``residual_predict`` — residual model anchored on the market prior:
``logit(q_final) = logit(q_market) + intercept + w . features``. Asks whether external/microstructure
information adds anything BEYOND the price the market already shows. Interpretable (regularized logistic);
NOT a black box. Fitting must use the existing window-blocked walk-forward + negative controls before trust.

Both pure. The Monte Carlo is deterministic given ``seed``. No data, network, or DB access.
"""
from __future__ import annotations

import math

import numpy as np

EPS = 1e-9


# ---------------------------------------------------------------- Q2: structural Monte Carlo
def monte_carlo_up_probability(*, current_price, price_to_beat, vol_per_s, seconds_remaining,
                               n_paths=20000, jump_intensity_per_s=0.0, jump_sigma=0.0, seed=0) -> dict:
    """P(terminal price >= price_to_beat) under driftless lognormal diffusion + optional Poisson jumps.

    Diffusion: log-return ~ Normal(0, (vol_per_s^2) * seconds_remaining).
    Jumps: count ~ Poisson(jump_intensity_per_s * seconds_remaining), each ~ Normal(0, jump_sigma^2).
    Returns q_structural_up/down, the binomial simulation standard error, terminal-price quantiles, and the
    assumptions used. Degenerate inputs -> q=0.5 with status flagged.
    """
    cur, p2b = float(current_price), float(price_to_beat)
    vol, secs = float(vol_per_s), float(seconds_remaining)
    assumptions = {"model": "driftless_lognormal+poisson_jumps", "n_paths": int(n_paths),
                   "jump_intensity_per_s": float(jump_intensity_per_s), "jump_sigma": float(jump_sigma),
                   "seed": int(seed)}
    if cur <= 0 or p2b <= 0 or vol <= 0 or secs <= 0:
        return {"q_structural_up": 0.5, "q_structural_down": 0.5, "simulation_standard_error": None,
                "quantiles": {}, "status": "degenerate_inputs", "model_assumptions": assumptions}

    rng = np.random.default_rng(int(seed))
    diff_sigma = vol * math.sqrt(secs)
    log_ret = rng.normal(0.0, diff_sigma, int(n_paths))
    if jump_intensity_per_s > 0 and jump_sigma > 0:
        counts = rng.poisson(jump_intensity_per_s * secs, int(n_paths))
        # total jump contribution per path = sum of `count` Normal(0, jump_sigma) draws ~ Normal(0, count*sigma^2)
        log_ret = log_ret + rng.normal(0.0, 1.0, int(n_paths)) * (np.sqrt(counts) * jump_sigma)

    terminal = cur * np.exp(log_ret)
    up = float(np.mean(terminal >= p2b))
    se = math.sqrt(max(up * (1.0 - up), 0.0) / int(n_paths))
    qs = np.quantile(terminal, [0.05, 0.25, 0.5, 0.75, 0.95])
    return {
        "q_structural_up": up,
        "q_structural_down": 1.0 - up,
        "simulation_standard_error": se,
        "quantiles": {"p05": float(qs[0]), "p25": float(qs[1]), "p50": float(qs[2]),
                      "p75": float(qs[3]), "p95": float(qs[4])},
        "status": "ok",
        "model_assumptions": assumptions,
    }


# ---------------------------------------------------------------- Q3: residual logistic on the market prior
def _logit(p):
    p = min(1.0 - EPS, max(EPS, float(p)))
    return math.log(p / (1.0 - p))


def fit_residual_logistic(rows, *, feature_names, l2=1.0, iters=300, lr=0.1) -> dict:
    """Fit logit(q_final) = logit(q_market) + b0 + w . x by L2-regularized gradient descent.

    rows: dicts with 'q_market', 'y' (0/1), and each name in feature_names. The market prior enters as a
    fixed per-row OFFSET, so weights capture only residual information. Returns {'intercept', 'weights':
    {name: w}, 'feature_names', 'l2'}.
    """
    n = len(rows)
    k = len(feature_names)
    X = np.array([[float(r[f]) for f in feature_names] for r in rows], dtype=float) if n else np.zeros((0, k))
    y = np.array([float(r["y"]) for r in rows], dtype=float)
    offset = np.array([_logit(r["q_market"]) for r in rows], dtype=float)
    w = np.zeros(k)
    b = 0.0
    if n == 0:
        return {"intercept": 0.0, "weights": {f: 0.0 for f in feature_names},
                "feature_names": list(feature_names), "l2": l2}
    for _ in range(int(iters)):
        z = offset + b + X.dot(w)
        p = 1.0 / (1.0 + np.exp(-z))
        err = p - y
        grad_w = X.T.dot(err) / n + l2 * w / n
        grad_b = float(np.sum(err)) / n
        w -= lr * grad_w
        b -= lr * grad_b
    return {"intercept": float(b), "weights": {f: float(w[i]) for i, f in enumerate(feature_names)},
            "feature_names": list(feature_names), "l2": float(l2)}


def residual_predict(fit, features, *, q_market) -> float:
    """q_final = sigmoid(logit(q_market) + intercept + w . features)."""
    z = _logit(q_market) + fit.get("intercept", 0.0)
    for f, wv in fit.get("weights", {}).items():
        z += wv * float(features.get(f, 0.0))
    return 1.0 / (1.0 + math.exp(-z))
