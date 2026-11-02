"""Pure statistical primitives for the fresh-anomaly engine. No I/O, no fetching.

These replace the cheap-win heuristic (`large_win_count`, `excess_pp`) that flagged
ordinary variance as suspicious. The decision statistic is the anytime-valid e-process
(valid under the worker's continuous re-analysis); the Poisson-binomial survival
function is display-only intuition; EB-shrinkage tames thin-history extremes for display.
"""
from __future__ import annotations


def poisson_binomial_sf(ps: list[float], obs: int) -> float:
    """P(S >= obs) where S = sum of independent Bernoulli(p_i).

    Exact, via a DP over the Poisson-binomial PMF (O(n^2); n = number of independent
    event families, which is small). The 12-bets-at-0.20 / 3-wins case returns ~0.44
    (ordinary), while three independent penny wins returns ~1e-6 (extreme).
    """
    ps = [min(1.0, max(0.0, float(p))) for p in ps]
    if obs <= 0:
        return 1.0
    if obs > len(ps):
        return 0.0
    dp = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dp) + 1)
        for k, v in enumerate(dp):
            nxt[k] += v * (1.0 - p)
            nxt[k + 1] += v * p
        dp = nxt
    return min(1.0, max(0.0, sum(dp[obs:])))


def eprocess_value(trials: list[tuple[float, float]], grid_n: int = 12) -> dict:
    """Anytime-valid e-value for the one-sided alternative 'wins more often than the null'.

    trials = [(p_f, y_f)] over INDEPENDENT event families, with y_f in [0, 1].
    Per trial the increment
    m(lambda) = 1 + lambda*(y - p) is a non-negative martingale increment under H0
    (E[y]=p => E[m]=1). We mix the resulting capital over a lambda grid to gain power
    across effect sizes without tuning lambda. lambda_max = 0.99/max(p) keeps m >= 0.
    By Ville's inequality the running max of E controls the false-alarm probability
    under continuous monitoring/optional stopping. Returns e_value and anytime_p=1/E.
    """
    pys = [(min(1.0 - 1e-9, max(1e-9, float(p))), min(1.0, max(0.0, float(y))))
           for p, y in trials]
    if not pys:
        return {"e_value": 1.0, "anytime_p": 1.0, "lambda_max": 0.0}
    pmax = max(p for p, _ in pys)
    lam_max = 0.99 / pmax
    if grid_n > 1:
        grid = [lam_max * (0.05 + (1.0 - 0.05) * i / (grid_n - 1)) for i in range(grid_n)]
    else:
        grid = [lam_max]
    es = []
    for lam in grid:
        prod = 1.0
        for p, y in pys:
            prod *= 1.0 + lam * (y - p)
            if prod <= 0.0:
                prod = 0.0
                break
        es.append(prod)
    e = sum(es) / len(es)
    return {"e_value": e, "anytime_p": min(1.0, 1.0 / e) if e > 0 else 1.0, "lambda_max": lam_max}


def eb_shrunk_oe(W: float, E0: float, k: float = 5.0, r0: float = 1.0) -> float:
    """Empirical-Bayes shrunk observed/expected ratio (DISPLAY magnitude only, never the
    flag decision). Pulls thin-N extremes toward the population ratio r0 (~1, since the
    market is ~calibrated at entry). W = observed wins, E0 = sum of null probabilities,
    k = pseudo-count (tunable; later fit from survivorship cohorts).
    """
    if E0 + k <= 0:
        return r0
    return (W + k * r0) / (E0 + k)
