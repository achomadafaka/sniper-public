"""Shared statistical primitives for wallet-analysis protocol v2.

Used by both the held-analyst branch (H1–H7) and the flipper branch (FH1–FH6).
All functions are pure and deterministic given a seed.

References:
- Bootstrap: §10.1 (10,000 iter, percentile method).
- BH-FDR: §6.4, §7.7, §8.3 (q=0.10, single correction per branch — H5 and FH4
  per-tag tests pooled with branch-level tests under one BH run).
- CUSUM: §9.1. Tabular CUSUM, h=25.0, k=0.5σ (calibrated for heavy-tail
  per-position P&L; see brain memory cusum-calibration-pnl-heavy-tail-2026-04-25).
- Time-split: §6.3, §7.8 (70/30 OOS, threshold n>=60).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np


# Locked CUSUM parameters (§9.1). Calibrated 2026-04-25 against 26,359 closed
# positions (excess kurtosis ≈918). Brain memory:
# cusum-calibration-pnl-heavy-tail-2026-04-25.
CUSUM_H_CALIBRATED = 25.0
CUSUM_K_SIGMA = 0.5
CUSUM_H_FALLBACK = 5.0

DEFAULT_BOOTSTRAP_ITERS = 10_000
DEFAULT_PERMUTATION_ITERS = 10_000
DEFAULT_SEED = 42
BH_FDR_Q = 0.10
TIME_SPLIT_TRAIN_FRACTION = 0.70
TIME_SPLIT_MIN_N = 60

# M3 punch-list item #2 (2026-04-26): require at least this many powered folds
# before the majority-supported rule may emit a verdict. With strict majority of
# powered alone, a single "supported" fold and four "underpowered" siblings
# produced "survived" (1*2 > 1). The floor blocks that loophole — fewer than
# MIN_POWERED_FOLDS powered folds is not enough evidence to claim a majority.
MIN_POWERED_FOLDS = 3


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------


def winsorize(samples: Sequence[float], clip_frac: float = 0.05) -> np.ndarray:
    """Symmetric winsorization: clip the top and bottom `clip_frac` of values.

    For clip_frac=0.05, clips the lowest 5% to the 5th-percentile value and
    the highest 5% to the 95th-percentile value. Pure function; returns a
    new array. Does NOT modify the input.

    Used by `winsorized_bootstrap_ci` to tame heavy-tail (kurtosis-918)
    P&L distributions whose percentile bootstrap CI gets blown out by a
    handful of outlier positions. Ref: Wilcox (2012) ch. 4.
    """
    arr = np.asarray(samples, dtype=float).copy()
    if arr.size == 0:
        return arr
    if not (0.0 <= clip_frac < 0.5):
        raise ValueError(f"clip_frac must be in [0, 0.5), got {clip_frac}")
    if clip_frac == 0.0:
        return arr
    lo = float(np.quantile(arr, clip_frac))
    hi = float(np.quantile(arr, 1.0 - clip_frac))
    return np.clip(arr, lo, hi)


def winsorized_bootstrap_ci(
    samples: Sequence[float],
    clip_frac: float = 0.05,
    statistic_fn: Callable[[np.ndarray], float] = np.mean,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
    null_value: float | None = None,
) -> tuple[float, float, float] | tuple[float, float, float, float]:
    """Bootstrap CI computed on the winsorized array.

    Per spec v3 amendment A: H1's percentile bootstrap on raw P&L fails
    under kurtosis-918 distributions (eatpraylove case: CI [-$294K, $1.4M]
    on +$449K). Winsorizing the source ONCE before resampling tames the
    tails without distorting central tendency.

    Important: clip BEFORE resampling (not per-resample) so extremes don't
    re-enter the resample distribution.

    When `null_value` is None, returns (point, ci_lower, ci_upper).
    When `null_value` is set, returns (point, ci_lower, ci_upper, p_value)
    where `p_value` is the two-sided empirical bootstrap p-value computed
    from the SAME (winsorized-source) resample distribution that produced
    the CI bounds. See `bootstrap_ci` for the two-sided formula.
    """
    clipped = winsorize(samples, clip_frac=clip_frac)
    return bootstrap_ci(
        clipped, statistic_fn=statistic_fn,
        n_iter=n_iter, seed=seed, alpha=alpha,
        null_value=null_value,
    )


def robust_location_bootstrap_ci(
    samples: Sequence[float],
    *,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
    null_value: float | None = None,
    clip_frac: float = 0.10,
) -> tuple[float, float, float, dict[str, Any]] | tuple[float, float, float, float, dict[str, Any]]:
    """H1 robust location CI for finite-mean heavy-tail samples.

    The tested estimand is the sample median with percentile bootstrap.
    This is the Phase-1 M2 closure choice over a 10% winsorized mean:
    both recover positive location under 5% Student-t contamination, but
    the median path kept the null false-alarm rate below 5% on the locked
    stress fixture.
    """
    arr = np.asarray(samples, dtype=float)
    result = bootstrap_ci(
        samples,
        statistic_fn=np.median,
        n_iter=n_iter,
        seed=seed,
        alpha=alpha,
        null_value=null_value,
    )
    details = {
        "tested_location_statistic": "median_iqr_bootstrap",
        "winsorize_clip_frac": clip_frac,
        "iqr": float(np.subtract(*np.percentile(arr, [75, 25]))) if arr.size else 0.0,
        "bootstrap_iters": n_iter,
    }
    if null_value is None:
        point, lo, hi = result
        return point, lo, hi, details
    point, lo, hi, p_value = result
    return point, lo, hi, p_value, details


def bootstrap_ci(
    samples: Sequence[float],
    statistic_fn: Callable[[np.ndarray], float] = np.mean,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
    null_value: float | None = None,
) -> tuple[float, float, float] | tuple[float, float, float, float]:
    """Percentile-method bootstrap CI for an arbitrary statistic.

    Returns (point_estimate, ci_lower, ci_upper) when `null_value is None`.
    `point_estimate` is `statistic_fn(samples)` on the original data. CI
    bounds are the `alpha/2` and `1 - alpha/2` quantiles of the resampled
    statistics.

    When `null_value` is supplied, returns
    (point_estimate, ci_lower, ci_upper, p_value) where `p_value` is the
    two-sided empirical bootstrap p-value:

        p = 2 * min(
                P(stat <= null_value) over the resamples,
                P(stat >= null_value) over the resamples,
            )

    clipped to [0.0, 1.0]. The p-value is computed from the SAME resample
    distribution that feeds the CI quantiles — no extra compute. This is
    the M1 closure of the wallet-protocol-bulletproofing spec, replacing
    the previous coarse 0.05-or-0.5 approximation derived from CI sign.

    Bootstrap p uses the raw resample fraction without the (count+1)/(n+1)
    smoothing that `permutation_test_*` applies. The smoothing is
    appropriate for permutation tests where the observed statistic is
    deterministically present in the null distribution; for percentile
    bootstrap it would bias all p-values away from zero by ~1/n_iter and
    fail the M1 verification bar (synthetic series with true p≈0 must
    yield empirical p≈0).

    Empty input → (0.0, 0.0, 0.0) — or (0.0, 0.0, 0.0, 1.0) when
    `null_value is None` is False (no evidence either way → conservative
    p=1.0). Single-element input → degenerate; CI collapses to a point;
    p=0.0 if the point differs from null, else 1.0.
    """
    arr = np.asarray(samples, dtype=float)
    n = len(arr)
    if n == 0:
        if null_value is None:
            return 0.0, 0.0, 0.0
        return 0.0, 0.0, 0.0, 1.0
    point = float(statistic_fn(arr))
    if n == 1:
        if null_value is None:
            return point, point, point
        p = 0.0 if point != null_value else 1.0
        return point, point, point, p

    rng = np.random.default_rng(seed)
    # Vectorized resample: draw (n_iter, n) indices in one shot
    idx = rng.integers(0, n, size=(n_iter, n))
    resamples = arr[idx]
    # Apply statistic over axis=1
    if statistic_fn is np.mean:
        stats = resamples.mean(axis=1)
    elif statistic_fn is np.median:
        stats = np.median(resamples, axis=1)
    else:
        stats = np.array([float(statistic_fn(row)) for row in resamples])

    lo = float(np.quantile(stats, alpha / 2))
    hi = float(np.quantile(stats, 1 - alpha / 2))

    if null_value is None:
        return point, lo, hi

    # Two-sided empirical p-value from the resample distribution.
    p_left = float((stats <= null_value).mean())
    p_right = float((stats >= null_value).mean())
    p = 2.0 * min(p_left, p_right)
    p = max(0.0, min(1.0, p))
    return point, lo, hi, p


def cluster_bootstrap_ci(
    samples: Sequence[float],
    clusters: Sequence,
    statistic_fn: Callable[[np.ndarray], float] = np.mean,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
    null_value: float | None = None,
) -> tuple[float, float, float] | tuple[float, float, float, float]:
    """Cluster-resampled percentile-method bootstrap CI.

    Resamples *clusters* (with replacement), not individual observations.
    For each iteration: draw n_clusters cluster IDs with replacement,
    concatenate their member values, compute statistic. The CI is then
    quantiles of those n_iter statistics.

    Used by H4 entry-timing (M6 closure of wallet-protocol-bulletproofing).
    For wallets that ladder many fills into few markets — eatpraylove had
    73/100 fills at 2 distinct drift values — the fill-level bootstrap CI
    can be ~20× too narrow because it treats correlated within-market
    fills as independent. Cluster-resampling honors the unit of
    independent observation (the market outcome).

    `samples` and `clusters` must be the same length. Cluster IDs may be
    any hashable; they are used only to group `samples`. Group order is
    preserved by first appearance.

    Returns (point, lo, hi) when `null_value is None`. When `null_value`
    is supplied, returns (point, lo, hi, p) — same two-sided empirical
    p-value contract as `bootstrap_ci`. The point estimate is computed
    on the original (un-resampled) `samples`.

    Edge cases:
    - Empty input → (0.0, 0.0, 0.0) or (0.0, 0.0, 0.0, 1.0) when null is set.
    - Single cluster → CI collapses to the point statistic; p depends on
      whether point equals null_value.
    """
    if len(samples) != len(clusters):
        raise ValueError(
            f"cluster_bootstrap_ci: samples (n={len(samples)}) and "
            f"clusters (n={len(clusters)}) must align"
        )

    arr = np.asarray(samples, dtype=float)
    n = len(arr)
    if n == 0:
        if null_value is None:
            return 0.0, 0.0, 0.0
        return 0.0, 0.0, 0.0, 1.0

    point = float(statistic_fn(arr))

    # Group sample indices by cluster, preserving first-appearance order.
    groups: dict = {}
    for i, cid in enumerate(clusters):
        groups.setdefault(cid, []).append(i)
    cluster_index_lists = [np.asarray(idxs, dtype=np.int64) for idxs in groups.values()]
    n_clusters = len(cluster_index_lists)

    if n_clusters == 1:
        # Every resample is the same data → degenerate.
        if null_value is None:
            return point, point, point
        p = 0.0 if point != null_value else 1.0
        return point, point, point, p

    rng = np.random.default_rng(seed)
    # Draw (n_iter, n_clusters) cluster picks, then assemble each resample.
    picks = rng.integers(0, n_clusters, size=(n_iter, n_clusters))

    stats = np.empty(n_iter, dtype=float)
    for t in range(n_iter):
        chosen = [cluster_index_lists[k] for k in picks[t]]
        idx = np.concatenate(chosen)
        resample = arr[idx]
        if statistic_fn is np.mean:
            stats[t] = resample.mean()
        elif statistic_fn is np.median:
            stats[t] = np.median(resample)
        else:
            stats[t] = float(statistic_fn(resample))

    lo = float(np.quantile(stats, alpha / 2))
    hi = float(np.quantile(stats, 1 - alpha / 2))

    if null_value is None:
        return point, lo, hi

    p_left = float((stats <= null_value).mean())
    p_right = float((stats >= null_value).mean())
    p = 2.0 * min(p_left, p_right)
    p = max(0.0, min(1.0, p))
    return point, lo, hi, p


# ---------------------------------------------------------------------------
# Permutation tests (Spearman / generic)
# ---------------------------------------------------------------------------


def _spearman_rho(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation. NaN-safe via rankdata-equivalent argsort."""
    if len(x) < 2:
        return 0.0
    rx = _rankdata(x)
    ry = _rankdata(y)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt((rx * rx).sum() * (ry * ry).sum()))
    if denom <= 0:
        return 0.0
    return float((rx * ry).sum() / denom)


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-rank of ties (matches scipy.stats.rankdata default)."""
    arr = np.asarray(a, dtype=float)
    sorter = np.argsort(arr, kind="stable")
    inv = np.empty_like(sorter)
    inv[sorter] = np.arange(len(arr))
    ranks = inv.astype(float) + 1.0
    # Average ties
    sorted_arr = arr[sorter]
    i = 0
    while i < len(sorted_arr):
        j = i + 1
        while j < len(sorted_arr) and sorted_arr[j] == sorted_arr[i]:
            j += 1
        if j > i + 1:
            avg = (i + j + 1) / 2.0
            ranks[sorter[i:j]] = avg
        i = j
    return ranks


def permutation_test_spearman(
    x: Sequence[float],
    y: Sequence[float],
    n_iter: int = DEFAULT_PERMUTATION_ITERS,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Two-sided permutation test for Spearman ρ(x, y).

    Returns (rho_observed, p_value). p_value = fraction of |ρ_perm| >= |ρ_obs|.

    Used by FH2 (flip notional vs flip ROI). Independent shuffle of `y` is
    valid because under the null, x and y are independent.
    """
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if len(xa) != len(ya):
        raise ValueError("permutation_test_spearman: x and y must align")
    n = len(xa)
    if n < 2:
        return 0.0, 1.0
    rho_obs = _spearman_rho(xa, ya)
    abs_obs = abs(rho_obs)

    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(n_iter):
        ya_shuffled = rng.permutation(ya)
        rho_perm = _spearman_rho(xa, ya_shuffled)
        if abs(rho_perm) >= abs_obs:
            count += 1
    p = (count + 1) / (n_iter + 1)  # +1 smoothing avoids p=0
    return rho_obs, p


def permutation_test_difference(
    a: Sequence[float],
    b: Sequence[float],
    statistic_fn: Callable[[np.ndarray, np.ndarray], float] | None = None,
    n_iter: int = DEFAULT_PERMUTATION_ITERS,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Two-sample permutation test (default: difference of means).

    Returns (statistic_observed, p_value). Used by FH6 (WR_with_market vs
    WR_against_market) where the two samples are different observations.

    Permutation: pool all observations, randomly partition into groups of the
    original sizes 10,000 times, compute statistic, count |≥ observed|.
    """
    if statistic_fn is None:
        statistic_fn = lambda x, y: float(x.mean() - y.mean())
    aa = np.asarray(a, dtype=float)
    ba = np.asarray(b, dtype=float)
    n_a = len(aa)
    n_b = len(ba)
    if n_a == 0 or n_b == 0:
        return 0.0, 1.0
    pooled = np.concatenate([aa, ba])
    obs = statistic_fn(aa, ba)
    abs_obs = abs(obs)

    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(n_iter):
        rng.shuffle(pooled)
        perm_stat = statistic_fn(pooled[:n_a], pooled[n_a:])
        if abs(perm_stat) >= abs_obs:
            count += 1
    p = (count + 1) / (n_iter + 1)
    return obs, p


# ---------------------------------------------------------------------------
# BH-FDR correction
# ---------------------------------------------------------------------------


def pool_and_correct(
    named_p: Sequence[tuple[str, float]],
    q: float = BH_FDR_Q,
) -> dict[str, tuple[float, bool]]:
    """BH-FDR correction over a NAMED pool, preserving caller's mapping.

    Per spec §6.4, §7.7, §8.3: a single BH correction per branch, pooling
    branch-level tests with H5/FH4 per-tag tests under one run. The pool
    is heterogeneous (named tests + per-grain tests) which makes parallel
    lists fragile — name → result lookup is the safer interface.

    Args:
        named_p: list of (test_name, raw_p_value) tuples. Names must be unique.
            Caller provides names like "H2", "H3", "H5:leaf:iran",
            "H5:nav:geopolitics", etc.
        q: target FDR (default 0.10 per spec).

    Returns:
        dict[name → (adjusted_p, rejected_flag)].

    Raises:
        ValueError on duplicate names (silent overwrite would corrupt the report).
    """
    if not named_p:
        return {}
    names = [n for n, _ in named_p]
    if len(set(names)) != len(names):
        raise ValueError(
            f"pool_and_correct: duplicate test names — {sorted(names)}"
        )
    raw = [p for _, p in named_p]
    adjusted, rejected = bh_fdr_correct(raw, q=q)
    return {
        name: (adjusted[i], rejected[i])
        for i, name in enumerate(names)
    }


def bh_fdr_correct(
    p_values: Sequence[float],
    q: float = BH_FDR_Q,
) -> tuple[list[float], list[bool]]:
    """Benjamini–Hochberg FDR correction.

    Args:
        p_values: raw p-values, one per hypothesis. NaN/None entries map to 1.0.
        q: target FDR (default 0.10 per spec).

    Returns:
        (adjusted_p_values, rejected_flags). Both same length and order as input.
        adjusted_p_values follow standard step-up adjustment:
            p_adj[i] = min over j>=i of (p_sorted[j] * m / rank[j])
        capped at 1.0. rejected[i] = (p_adj[i] <= q).

    Per spec §6.4, §7.7, §8.3: a single BH correction per branch, pooling
    branch-level tests with H5/FH4 per-tag tests. Caller is responsible for
    constructing the correct pooled list in the correct order.
    """
    raw = [
        float(p) if (p is not None and not np.isnan(p)) else 1.0
        for p in p_values
    ]
    m = len(raw)
    if m == 0:
        return [], []

    order = sorted(range(m), key=lambda i: raw[i])
    adj_sorted: list[float] = [0.0] * m
    # Step-up adjustment, computed right-to-left
    running_min = 1.0
    for rank_from_end in range(m, 0, -1):
        i_in_sorted = rank_from_end - 1
        p = raw[order[i_in_sorted]]
        bh = p * m / rank_from_end
        if bh < running_min:
            running_min = bh
        adj_sorted[i_in_sorted] = min(running_min, 1.0)

    # Restore original order
    adjusted = [0.0] * m
    rejected = [False] * m
    for sorted_pos, orig_idx in enumerate(order):
        adjusted[orig_idx] = adj_sorted[sorted_pos]
        rejected[orig_idx] = adj_sorted[sorted_pos] <= q
    return adjusted, rejected


# ---------------------------------------------------------------------------
# CUSUM (calibrated tabular)
# ---------------------------------------------------------------------------


@dataclass
class CusumResult:
    signaled: bool
    signal_index: int | None              # 0-based index of first threshold crossing
    h_used: float
    k: float
    mu: float
    sigma: float
    cusum_status: str                     # "calibrated" | "uncalibrated"


def cusum_signal(
    series: Sequence[float],
    h: float = CUSUM_H_CALIBRATED,
    k_sigma_multiplier: float = CUSUM_K_SIGMA,
    mu: float | None = None,
    sigma: float | None = None,
    direction: str = "upper",
) -> CusumResult:
    """Tabular CUSUM on a per-position (or per-flip) P&L series.

    Spec §9.1 formula (upper-side):
        cusum_t = max(0, cusum_{t-1} + (x_t - μ) - k)

    The locked calibration (h=25.0, false-alarm 3.1%) was computed for the
    upper-side detector against the empirical heavy-tail P&L distribution
    (brain memory `cusum-calibration-pnl-heavy-tail-2026-04-25`). Running a
    two-sided detector at the same h roughly doubles false-alarm rate and
    invalidates the calibration.

    The detector accepts a `direction` knob:
      - `direction="upper"` (DEFAULT) → spec-default upper-side detector;
        cusum_status="calibrated" only when h matches CUSUM_H_CALIBRATED.
      - `direction="lower"` → fires on downward shifts (uses -deviation);
        marked uncalibrated even at h=25 because calibration is one-sided
        upper.
      - `direction="two_sided"` → both accumulators run; doubles false-alarm
        vs calibration. Always marked uncalibrated.

    Default is `"upper"` because the calibration sample (brain memory
    `cusum-calibration-pnl-heavy-tail-2026-04-25`) used the one-sided
    upper detector. Callers wanting "did edge disappear" should pass
    `direction="lower"` against the wallet's PnL series — and accept the
    uncalibrated false-alarm flag in their report.

    Args:
        series: per-unit P&L values in chronological order.
        h: control limit (calibrated default 25.0).
        k_sigma_multiplier: k = `k_sigma_multiplier * sigma` per spec §9.1.
        mu: in-sample mean. If None, use `series.mean()`. Caller is
            responsible for passing pre-regime mu when running on the
            full series — using full-series mean inflates k and conceals
            shifts.
        sigma: in-sample std. If None, use `series.std(ddof=1)`.
        direction: "upper" | "lower" | "two_sided".

    Returns:
        CusumResult with signaled + first-crossing index.
    """
    arr = np.asarray(series, dtype=float)
    n = len(arr)
    if n == 0:
        return CusumResult(
            signaled=False, signal_index=None, h_used=h, k=0.0,
            mu=0.0, sigma=0.0, cusum_status="calibrated",
        )

    if mu is None:
        mu = float(arr.mean())
    if sigma is None:
        if n >= 2:
            sigma = float(arr.std(ddof=1))
        else:
            sigma = 0.0
    k = k_sigma_multiplier * sigma if sigma > 0 else 0.0

    if direction not in ("upper", "lower", "two_sided"):
        raise ValueError(
            f"cusum_signal: direction must be 'upper' | 'lower' | 'two_sided', got {direction!r}"
        )

    # Spec calibration is one-sided; mark uncalibrated when h is overridden
    # OR when running a non-calibrated direction at the locked h.
    h_matches_calibrated = abs(h - CUSUM_H_CALIBRATED) < 1e-9
    if not h_matches_calibrated:
        status = "uncalibrated"
    else:
        # h matches calibrated value; only "upper" matches the calibration sample
        status = "calibrated" if direction == "upper" else "uncalibrated"

    cusum_pos = 0.0
    cusum_neg = 0.0
    for i, x in enumerate(arr):
        deviation = x - mu
        if direction in ("upper", "two_sided"):
            cusum_pos = max(0.0, cusum_pos + deviation - k)
            if cusum_pos >= h:
                return CusumResult(
                    signaled=True, signal_index=i, h_used=h, k=k,
                    mu=mu, sigma=sigma, cusum_status=status,
                )
        if direction in ("lower", "two_sided"):
            cusum_neg = max(0.0, cusum_neg - deviation - k)
            if cusum_neg >= h:
                return CusumResult(
                    signaled=True, signal_index=i, h_used=h, k=k,
                    mu=mu, sigma=sigma, cusum_status=status,
                )

    return CusumResult(
        signaled=False, signal_index=None, h_used=h, k=k,
        mu=mu, sigma=sigma, cusum_status=status,
    )


# ---------------------------------------------------------------------------
# Time-split 70/30
# ---------------------------------------------------------------------------


def time_split_70_30(
    observations: Sequence[Any],
    sort_key: Callable[[Any], float] | None = None,
    train_fraction: float = TIME_SPLIT_TRAIN_FRACTION,
    min_n: int = TIME_SPLIT_MIN_N,
) -> tuple[list[Any], list[Any], bool]:
    """Sort observations by `sort_key` (typically timestamp) and split 70/30.

    Returns (train, test, eligible). When `len(observations) < min_n`, returns
    `(observations_sorted, [], False)` — caller should mark holdout_status as
    "underpowered".

    Sort is stable. Tie-breaking on identical sort_key values preserves input
    order (rowid-equivalent). Caller is responsible for choosing sort_key
    correctly: closed_positions sort by entry timestamp, flips by buy_ts.
    """
    obs = list(observations)
    n = len(obs)
    if sort_key is not None:
        obs = sorted(obs, key=sort_key)
    if n < min_n:
        return obs, [], False
    cut = int(round(n * train_fraction))
    return obs[:cut], obs[cut:], True


# ---------------------------------------------------------------------------
# Rolling-origin k-fold time split (M3)
# ---------------------------------------------------------------------------


def time_split_kfold(
    observations: Sequence[Any],
    *,
    k: int = 5,
    sort_key: Callable[[Any], float] | None = None,
    min_n: int = TIME_SPLIT_MIN_N,
) -> tuple[list[tuple[list[Any], list[Any]]], bool]:
    """Rolling-origin k-fold time split.

    Closes wallet-protocol-bulletproofing M3. Replaces the single 70/30 cut
    so the OOS verdict is no longer determined by one possibly-unlucky split.

    For k=5 (default), produces folds:
        fold 0: train [0, 0.5),  test [0.5, 0.6)
        fold 1: train [0, 0.6),  test [0.6, 0.7)
        fold 2: train [0, 0.7),  test [0.7, 0.8)
        fold 3: train [0, 0.8),  test [0.8, 0.9)
        fold 4: train [0, 0.9),  test [0.9, 1.0)

    Train sets are nested-growing; test sets are disjoint chronological slices.
    The earliest train segment contains 50% of the data so that training power
    is comparable to the legacy 70/30 floor on most folds.

    Returns:
        `(folds, eligible)` where `folds` is a list of `(train, test)` tuples.
        Empty list + `eligible=False` when `len(observations) < min_n`.
    """
    if k < 2:
        raise ValueError(f"k must be >= 2, got {k}")
    obs = list(observations)
    if sort_key is not None:
        obs = sorted(obs, key=sort_key)
    n = len(obs)
    if n < min_n:
        return [], False
    # Anchor points at 0.5, 0.5+1/k, 0.5+2/k, ..., 1.0 (inclusive).
    # The 0.5 floor keeps the first training segment at half the sample so
    # bootstrap CIs aren't degenerate on the first fold.
    folds: list[tuple[list[Any], list[Any]]] = []
    test_width = 1.0 / (2 * k)  # so k slices cover [0.5, 1.0]
    for i in range(k):
        train_end = int(round((0.5 + i * test_width) * n))
        test_end = int(round((0.5 + (i + 1) * test_width) * n))
        if test_end > n:
            test_end = n
        folds.append((obs[:train_end], obs[train_end:test_end]))
    return folds, True


def decision_rule_majority_supported(per_fold_statuses: Sequence[str]) -> str:
    """Aggregate per-fold OOS statuses into one holdout verdict.

    Rule:
      - "underpowered" folds drop out of both numerator and denominator
        (they are inconclusive, not negative evidence).
      - When fewer than `MIN_POWERED_FOLDS` folds are powered, the aggregate
        is "underpowered" even if the powered ones unanimously support — too
        few folds to make a majority claim. This blocks the single-supported-
        fold-out-of-five-underpowered loophole flagged in the M3 review.
      - Of the remaining "powered" folds, the verdict is "survived" iff a
        strict majority were "supported" (i.e. n_supported > n_powered / 2).
      - When every fold is "underpowered" the aggregate is "underpowered".
      - Otherwise "failed".

    Status labels accepted: "supported", "failed", "underpowered". Any other
    label is treated as "failed" so unrecognized inputs degrade safely.
    """
    statuses = list(per_fold_statuses)
    if not statuses:
        return "underpowered"
    powered = [s for s in statuses if s != "underpowered"]
    if not powered:
        return "underpowered"
    if len(powered) < MIN_POWERED_FOLDS:
        return "underpowered"
    n_supported = sum(1 for s in powered if s == "supported")
    if n_supported * 2 > len(powered):
        return "survived"
    return "failed"


__all__ = [
    "CUSUM_H_CALIBRATED",
    "CUSUM_K_SIGMA",
    "CUSUM_H_FALLBACK",
    "DEFAULT_BOOTSTRAP_ITERS",
    "DEFAULT_PERMUTATION_ITERS",
    "DEFAULT_SEED",
    "BH_FDR_Q",
    "TIME_SPLIT_TRAIN_FRACTION",
    "TIME_SPLIT_MIN_N",
    "MIN_POWERED_FOLDS",
    "CusumResult",
    "bootstrap_ci",
    "winsorize",
    "winsorized_bootstrap_ci",
    "robust_location_bootstrap_ci",
    "permutation_test_spearman",
    "permutation_test_difference",
    "bh_fdr_correct",
    "pool_and_correct",
    "cusum_signal",
    "time_split_70_30",
    "time_split_kfold",
    "decision_rule_majority_supported",
]
