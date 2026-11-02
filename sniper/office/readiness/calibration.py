"""Calibration gate for the readiness bounds (prereg section 7, Amendment 2).

For one alpha_k it simulates the clustered-bootstrap decision rule on the LISTED data generators and reports exact
one-sided Clopper-Pearson Monte Carlo bounds: type I (rejection at the null boundary, upper bound) and power at delta1
(lower bound). Inference at that k is allowed only when type I upper <= alpha_k. A power claim needs power lower >= 0.80.
No claim beyond the listed generators.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import beta

from sniper.office.readiness import verdict as V

GENERATORS = ("iid", "heavy_tail", "day_cluster", "serial", "adaptive_entry", "pods_shifts")
METHODS = ("production", "coin_only_wrong", "very_conservative_test_only")
CONF = 0.95


def cp_upper(r: int, m: int, conf: float = CONF) -> float:
    return 1.0 if r >= m else float(beta.ppf(conf, r + 1, m - r))


def cp_lower(r: int, m: int, conf: float = CONF) -> float:
    return 0.0 if r <= 0 else float(beta.ppf(1 - conf, r, m - r + 1))


def min_reps(alpha: float) -> int:
    """Smallest M for which zero rejections still give an upper bound <= alpha."""
    return math.ceil(3.0 / alpha)


@dataclass(frozen=True)
class Calibration:
    k: int
    generator: str
    method: str
    supported: bool        # inference allowed at this k (type I upper <= alpha_k)
    power_claim: bool      # power lower >= 0.80 at delta1
    reps: int
    type1_upper: float | None
    power_lower: float | None
    reason: str | None


def _noise(gen: str, rng, n: int, days: int, sigma: float):
    """Return (e_y, e_n, day_index, entry_scale) with SD about sigma; entry_scale rescales y to keep its mean."""
    day = (np.arange(n) * days) // n          # chronological: coins are time-ordered, days never step backwards
    ez = rng.standard_normal(n)
    en = rng.standard_normal(n)
    ey = 0.5 * en + math.sqrt(0.75) * ez
    scale = np.ones(n)
    if gen == "heavy_tail":
        t = rng.standard_t(3, (2, n)) / math.sqrt(3.0)
        en, ez = t[0], t[1]
        ey = 0.5 * en + math.sqrt(0.75) * ez
    elif gen == "day_cluster":
        u = rng.standard_normal(days) * 0.8
        ey, en = ey * 0.6 + u[day], en * 0.6 + u[day]
    elif gen == "serial":
        u = np.zeros(days)
        for d in range(1, days):
            u[d] = 0.7 * u[d - 1] + math.sqrt(1 - 0.49) * rng.standard_normal()
        u *= 0.9
        ey, en = ey * 0.6 + u[day], en * 0.6 + u[day]
    elif gen == "adaptive_entry":
        trail = 0.0          # entry depends on PAST outcomes only, so a coin's own noise never selects it
        mask = np.zeros(n)
        for i in range(n):
            mask[i] = float(rng.random() < 0.6 + 0.3 * math.tanh(trail))
            trail = 0.9 * trail + 0.1 * ey[i]
        scale = mask / max(mask.mean(), 1e-9)
    elif gen == "pods_shifts":
        night = (np.arange(n) // 3) % 2 == 1
        pod = rng.integers(0, 3, n)
        p = np.where(night, 0.5, 0.9)
        trail = np.zeros(3)
        mask = np.zeros(n)
        for i in range(n):          # deterministic reallocation of capacity by trailing pod score (frozen rule)
            cap = np.clip(1.0 + 0.2 * np.tanh(trail[pod[i]]), 0.6, 1.2)
            mask[i] = float(rng.random() < min(1.0, p[i] * cap))
            trail[pod[i]] = 0.9 * trail[pod[i]] + 0.1 * ey[i]
        scale = mask / max(mask.mean(), 1e-9)
    return sigma * ey, sigma * en, day, scale


def _draw(gen: str, rng, n: int, days: int, sigma: float, mu_y: float, mu_n: float):
    ey, en, day, scale = _noise(gen, rng, n, days, sigma)
    y = (mu_y + ey) * scale
    nn = mu_n + en
    return y, nn, day


def _rejects(y, nn, day, alpha, draws, seed, delta0, method, scale, block, day_method) -> bool:
    a = alpha / 100 if method == "very_conservative_test_only" else alpha
    day_ids = np.array([f"d{d:02d}" for d in day])
    nc, nd, nlb = V._bounds(y, day_ids, a, draws, seed, block, scale, day_method)
    lc, ld, llb = V._bounds(y - nn, day_ids, a, draws, seed + 100, block, scale, day_method)
    if method == "coin_only_wrong":
        nlb, llb = nc, lc
    return bool(nlb > delta0 and llb > delta0)


def calibrate(*, k: int, generator: str, n_coins: int, days: int, delta0: float, delta1: float, sigma: float,
              compute_budget_cells: float, seed: int, method: str = "production", reps: int | None = None,
              draws: int | None = None, alpha_scale: float = 1.0, block_days: int = 3, day_method: str = "t_block") -> Calibration:
    if generator not in GENERATORS:
        raise ValueError(f"unlisted generator {generator!r}")
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    if k < 1 or k > V.K_MAX:
        raise ValueError("k")
    alpha = V.alpha_k(k)
    m = reps or math.ceil(25.0 / alpha)      # 25/alpha certifies a true rate of about alpha/2; the floor is min_reps = 3/alpha
    if m < min_reps(alpha):
        raise ValueError("reps below min_reps")
    d = draws or V.draws_for(alpha)          # `draws` override is for unit tests only; production uses draws_for
    cells = 3.0 * m * 2 * d * (n_coins + days)
    if cells > compute_budget_cells:
        return Calibration(k, generator, method, False, False, m, None, None, "reps_beyond_compute_budget")
    rng = np.random.default_rng(seed)
    big = 3 * delta1
    # boundary A: office-net mean == delta0 (lift large); boundary B: lift mean == delta0 (net large); power: both == delta1
    scen = {"A": (delta0, delta0 - big), "B": (delta0 + big, delta0 + big - delta0), "P": (delta1, 0.0)}
    hits = {}
    for name, (mu_y, mu_n) in scen.items():
        r = 0
        for i in range(m):
            y, nn, day = _draw(generator, rng, n_coins, days, sigma, mu_y, mu_n)
            r += _rejects(y, nn, day, alpha, d, int(rng.integers(0, 2**31 - 1)), delta0, method, alpha_scale, block_days, day_method)
        hits[name] = r
    t1 = max(cp_upper(hits["A"], m), cp_upper(hits["B"], m))
    pl = cp_lower(hits["P"], m)
    supported = t1 <= alpha
    return Calibration(k, generator, method, supported, supported and pl >= 0.80, m, t1, pl,
                       None if supported else "type1_upper_above_alpha")


def make_proof(results: list, *, params: dict, code_commit: str, plan_sha256: str, created_at: float) -> dict:
    """The calibration proof artifact the exporter resolves. `supported_for_k` is true only if EVERY listed generator
    was supported at that k; the proof id is the sha256 of the canonical JSON of everything else."""
    import hashlib
    import json
    ks = {r.k for r in results}
    if len(ks) != 1 or {r.generator for r in results} != set(GENERATORS):
        raise ValueError("a proof needs every listed generator at one k")
    body = {
        "schema": "office-calibration-proof-v1", "k": ks.pop(), "params": params, "code_commit": code_commit,
        "plan_sha256": plan_sha256, "created_at": created_at,
        "generators": [{"name": r.generator, "method": r.method, "reps": r.reps, "supported": r.supported,
                        "power_claim": r.power_claim, "type1_upper": r.type1_upper, "power_lower": r.power_lower,
                        "reason": r.reason} for r in sorted(results, key=lambda r: r.generator)],
    }
    body["supported_for_k"] = all(g["supported"] for g in body["generators"])
    body["power_claim_for_k"] = all(g["power_claim"] for g in body["generators"])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {**body, "proof_id": hashlib.sha256(raw).hexdigest()}
