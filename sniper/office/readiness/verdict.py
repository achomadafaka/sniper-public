"""Office readiness verdict: pure computation of prereg sections 3-10.

Source of truth: docs/audits/2026-10-28-office-readiness-preregistration.md (Amendment 2).
Read-only: takes an Epoch snapshot, returns pass / fail / no_verdict. It never reads the Office store.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import norm, t as student_t

K_MAX = 10
FAMILY_ALPHA = 0.05
MAX_CENSORED_SHARE = 0.20
MIN_POSITIVE_DAYS = 3
TOP3_LIMIT = 0.50
_CHUNK_CELLS = 20_000_000


def z(p: float) -> float:
    return float(norm.ppf(p))


def alpha_k(k: int) -> float:
    return FAMILY_ALPHA / (k * (k + 1))


def draws_for(alpha: float) -> int:
    return max(10_000, math.ceil(200 / alpha))


def n_plan(*, sigma: float, delta0: float, delta1: float, k: int) -> int:
    """Planning size (a feasibility approximation, not a power guarantee; prereg 7)."""
    a = alpha_k(k)
    return math.ceil(((z(1 - a) + z(0.80)) * sigma / (delta1 - delta0)) ** 2)


@dataclass(frozen=True)
class Coin:
    id: str
    day: str
    y: float | None
    n: float | None
    entered: bool
    y_censored: bool = False
    n_censored: bool = False


@dataclass(frozen=True)
class Epoch:
    coins: tuple
    k: int
    delta0: float
    delta1: float
    clip: float
    loss_bound: float          # L = clip + B_entry + B_exit + max network/rent on the round trip
    n_upper: float | None      # enforceable realized-return cap on the null arm, else None
    cost: float | None         # Office cost C for the epoch, None = a cost item is open/unbounded
    exec_bound_present: bool
    counters: dict
    calibrated_ks: frozenset
    seed: int
    min_days: int = 30
    n_plan: int = 30
    draws_cap: int | None = None
    alpha_scale: float = 1.0   # calibration-chosen conservatism: bounds are taken at alpha_k * alpha_scale
    block_days: int = 3        # block length for the UTC-day bound
    day_method: str = "t_block"
    proofs: dict | None = None  # verified envelope from export_epoch: ids checked here, never loose booleans


REQUIRED_PROOFS = ("metric_registry_hash", "calibration_proof_id", "stopping_rule", "cost_rule_id",
                   "exec_bound_evidence_id")
MONTHLY_COST_USD = 800.0   # operator order 2026-10-28: Office costs are a flat 800 USD per month (prereg Amendment 4)


def epoch_days(open_ts: float, close_ts: float) -> list:
    """Every UTC calendar day the epoch is open for any part of the day, close day included (no proration;
    a restart or a new epoch id cannot drop a day). Timestamps are corrected-UTC seconds."""
    import datetime as _dt
    if close_ts < open_ts:
        raise ValueError("epoch closes before it opens")
    d0 = _dt.datetime.fromtimestamp(open_ts, _dt.timezone.utc).date()
    d1 = _dt.datetime.fromtimestamp(close_ts, _dt.timezone.utc).date()
    if close_ts > open_ts and close_ts % 86400 == 0:
        d1 -= _dt.timedelta(days=1)          # ended_at is exclusive at exact midnight
    return [(d0 + _dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def flat_office_cost(days) -> float:
    """C for an epoch: sum over its UTC days of 800 / days_in_month(day). `days` are 'YYYY-MM-DD' strings."""
    import calendar
    total = 0.0
    for d in days:
        y, m, _ = (int(x) for x in d.split("-"))
        total += MONTHLY_COST_USD / calendar.monthrange(y, m)[1]
    return total


def proof_problems(e: "Epoch") -> list:
    """A positive verdict needs every proof present AND the whole calibrated design bound to this epoch (Zeta, Amendment 5):
    the envelope's `design` must equal what evaluate() will actually run. An unvalidated fixture cannot pass."""
    pr = e.proofs
    if not isinstance(pr, dict):
        return ["proofs_missing"]
    out = []
    for key in REQUIRED_PROOFS:
        if not pr.get(key):
            out.append(f"proof_missing:{key}")
    if e.k not in set(pr.get("alpha_support") or ()):
        out.append("proof_alpha_support_lacks_k")
    d = pr.get("design")
    if not isinstance(d, dict):
        out.append("proof_missing:design")
        return out
    alpha = alpha_k(e.k) if 1 <= e.k <= K_MAX else None
    expect = {"alpha_scale": e.alpha_scale, "day_method": e.day_method, "block_days": e.block_days,
              "delta0": e.delta0, "delta1": e.delta1, "n_coins": e.n_plan, "days": e.min_days,
              "alpha_k": alpha, "draws": draws_for(alpha) if alpha else None}
    for key, want in expect.items():
        if d.get(key) != want:
            out.append(f"proof_design_mismatch:{key}")
    if pr.get("n_plan") != e.n_plan:
        out.append("proof_n_plan_mismatch")
    if e.draws_cap is not None:
        out.append("draws_cap_forbidden")
    return out


@dataclass
class Verdict:
    status: str
    reasons: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _lower_bound_coin(x: np.ndarray, alpha: float, draws: int, rng) -> float:
    n = len(x)
    step = max(1, _CHUNK_CELLS // n)
    means = np.empty(draws)
    done = 0
    while done < draws:
        c = min(step, draws - done)
        means[done:done + c] = x[rng.integers(0, n, (c, n))].mean(axis=1)
        done += c
    return float(np.quantile(means, alpha))


def _lower_bound_day(x: np.ndarray, day_ids: np.ndarray, alpha: float, draws: int, rng, block: int = 3) -> float:
    """Circular block bootstrap over UTC days (block length `block` keeps serial day dependence)."""
    days = np.unique(day_ids)
    s = np.array([x[day_ids == d].sum() for d in days])
    m = np.array([(day_ids == d).sum() for d in days], dtype=float)
    g = len(days)
    block = max(1, min(block, g))
    nb = math.ceil(g / block)
    offs = np.arange(block)
    step = max(1, _CHUNK_CELLS // (nb * block))
    means = np.empty(draws)
    done = 0
    while done < draws:
        c = min(step, draws - done)
        starts = rng.integers(0, g, (c, nb))
        idx = ((starts[:, :, None] + offs) % g).reshape(c, nb * block)
        means[done:done + c] = s[idx].sum(axis=1) / m[idx].sum(axis=1)
        done += c
    return float(np.quantile(means, alpha))


def _lower_bound_day_t(x: np.ndarray, day_ids: np.ndarray, alpha: float, block: int = 3) -> float:
    """Cluster-robust t bound over non-overlapping UTC-day blocks (ratio estimator, G-1 df).
    Few clusters make percentile bootstraps undercover, so the day bound is a t interval; fewer than 3 blocks = no bound."""
    days = np.unique(day_ids)
    g = len(days)
    nb = g // max(1, block)
    if nb < 3:
        return float("-inf")
    owner = {d: min(i // block, nb - 1) for i, d in enumerate(days)}
    b = np.array([owner[d] for d in day_ids])
    s = np.array([x[b == i].sum() for i in range(nb)])
    m = np.array([(b == i).sum() for i in range(nb)], dtype=float)
    mean = s.sum() / m.sum()
    resid = s - mean * m
    se = math.sqrt(nb / (nb - 1) * float((resid ** 2).sum())) / m.sum()
    return float(mean - student_t.ppf(1 - alpha, nb - 1) * se)


def _bounds(x: np.ndarray, day_ids: np.ndarray, alpha: float, draws: int, seed: int,
            block: int = 3, scale: float = 1.0, day_method: str = "t_block"):
    a = alpha * scale
    coin = _lower_bound_coin(x, a, draws, np.random.default_rng(seed))
    if day_method == "boot":
        day = _lower_bound_day(x, day_ids, a, draws, np.random.default_rng(seed + 1), block)
    else:
        day = _lower_bound_day_t(x, day_ids, a, block)
    return coin, day, min(coin, day)


def evaluate(e: Epoch) -> Verdict:
    pre: list = list(proof_problems(e))
    if e.k > K_MAX or e.k < 1:
        return Verdict("no_verdict", ["k_above_kmax"], {"k": e.k})
    alpha = alpha_k(e.k)
    coins = e.coins
    n = len(coins)
    if not e.exec_bound_present:
        pre.append("no_execution_cost_bound")
    if e.cost is None:
        pre.append("office_cost_unknown")
    for name, val in sorted((e.counters or {}).items()):
        if val:
            pre.append(f"counter:{name}")
    if e.k not in e.calibrated_ks:
        pre.append("calibration_missing_for_k")
    if len({c.id for c in coins}) != n:
        pre.append("duplicate_coin_ids")
    days = sorted({c.day for c in coins})
    if len(days) < e.min_days:
        pre.append("fewer_than_min_days")
    if n < e.n_plan:
        pre.append("underpowered_n_below_plan")
    cens = sum(1 for c in coins if c.y_censored or c.n_censored)
    share = cens / n if n else 1.0
    if share > MAX_CENSORED_SHARE:
        pre.append("censored_share_above_20pct")
    if any(c.n_censored for c in coins) and e.n_upper is None:
        pre.append("paired_lift_unbounded")
    if pre:
        return Verdict("no_verdict", pre, {"n": n, "days": len(days), "censored_share": share, "alpha_k": alpha})

    cost_per_coin = e.cost / n
    y_adv, y_obs, n_adv = [], [], []
    for c in coins:
        if c.y_censored:
            y_adv.append(-e.loss_bound if c.entered else 0.0)
            y_obs.append(0.0)
        else:
            y_adv.append(c.y)
            y_obs.append(c.y)
        n_adv.append(e.n_upper if c.n_censored else c.n)
    y_adv = np.array(y_adv, dtype=float)
    y_obs = np.array(y_obs, dtype=float)
    n_adv = np.array(n_adv, dtype=float)
    day_ids = np.array([c.day for c in coins])
    net = y_adv - cost_per_coin
    lift = y_adv - n_adv
    draws = draws_for(alpha)
    if e.draws_cap is not None:
        draws = min(draws, e.draws_cap)
    nc, nd, nlb = _bounds(net, day_ids, alpha, draws, e.seed, e.block_days, e.alpha_scale, e.day_method)
    lc, ld, llb = _bounds(lift, day_ids, alpha, draws, e.seed + 100, e.block_days, e.alpha_scale, e.day_method)

    fail: list = []
    if not nlb > e.delta0:
        fail.append("office_net_lb_not_above_delta0")
    if not llb > e.delta0:
        fail.append("lift_lb_not_above_delta0")
    day_net = {d: float(net[day_ids == d].sum()) for d in days}
    pos_days = sum(1 for v in day_net.values() if v > 0)
    if pos_days < MIN_POSITIVE_DAYS:
        fail.append("fewer_than_3_positive_days")
    pos_sum = float(np.clip(y_adv, 0, None).sum())
    top3 = None
    if pos_sum <= 0:
        fail.append("no_positive_profit")
    else:
        top3 = float(np.sort(y_adv)[-3:].clip(0, None).sum() / pos_sum)
        if top3 >= TOP3_LIMIT:
            fail.append("top3_share_ge_50pct")
    details = {
        "alpha_k": alpha, "draws": draws, "n": n, "days": len(days), "censored_share": share,
        "lb_office_net_coin": nc, "lb_office_net_day": nd, "lb_office_net": nlb,
        "lb_lift_coin": lc, "lb_lift_day": ld, "lb_lift": llb,
        "office_net_mean_adverse": float(net.mean()),
        "office_net_mean_observed_only": float(y_obs.mean() - cost_per_coin),
        "alpha_scale": e.alpha_scale, "block_days": e.block_days, "positive_days": pos_days, "top3_share": top3, "delta0": e.delta0,
    }
    return Verdict("fail" if fail else "pass", fail, details)
