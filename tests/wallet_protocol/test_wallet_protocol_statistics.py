"""Tests for shared statistical primitives — protocol v2 §6, §7, §9, §10."""

import numpy as np
import pytest

from sniper.analysis.wallet_protocol.statistics import (
    BH_FDR_Q,
    CUSUM_H_CALIBRATED,
    CUSUM_H_FALLBACK,
    CUSUM_K_SIGMA,
    TIME_SPLIT_MIN_N,
    bh_fdr_correct,
    bootstrap_ci,
    cluster_bootstrap_ci,
    cusum_signal,
    permutation_test_difference,
    permutation_test_spearman,
    time_split_70_30,
    winsorize,
    winsorized_bootstrap_ci,
)


# ----------------------------------------------------------------------------
# bootstrap_ci
# ----------------------------------------------------------------------------


def test_bootstrap_reproducible_with_seed():
    """Same seed → identical CI bounds."""
    rng = np.random.default_rng(0)
    samples = rng.normal(loc=2.0, scale=1.0, size=200).tolist()
    a = bootstrap_ci(samples, n_iter=2000, seed=42)
    b = bootstrap_ci(samples, n_iter=2000, seed=42)
    assert a == b


def test_bootstrap_recovers_mean_within_ci():
    """CI should bracket the true mean for well-behaved data."""
    rng = np.random.default_rng(1)
    samples = rng.normal(loc=5.0, scale=2.0, size=500).tolist()
    point, lo, hi = bootstrap_ci(samples, n_iter=2000, seed=7)
    assert lo < 5.0 < hi
    assert lo < point < hi


def test_bootstrap_empty_input_returns_zeros():
    point, lo, hi = bootstrap_ci([])
    assert (point, lo, hi) == (0.0, 0.0, 0.0)


def test_bootstrap_single_sample_zero_width():
    point, lo, hi = bootstrap_ci([3.5])
    assert point == 3.5
    assert lo == 3.5 and hi == 3.5


def test_bootstrap_ci_width_shrinks_with_n():
    """More data → tighter CI."""
    rng = np.random.default_rng(2)
    small = rng.normal(0, 1, size=30).tolist()
    big = rng.normal(0, 1, size=3000).tolist()
    _, slo, shi = bootstrap_ci(small, n_iter=2000, seed=99)
    _, blo, bhi = bootstrap_ci(big, n_iter=2000, seed=99)
    assert (bhi - blo) < (shi - slo)


def test_bootstrap_with_custom_statistic_callable():
    """Generic statistic_fn path (not np.mean/median) — exercises the per-row loop."""
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    # Custom: trimmed mean dropping max
    def trimmed(arr):
        a = np.sort(arr)[:-1]
        return float(a.mean())
    point, lo, hi = bootstrap_ci(samples, statistic_fn=trimmed, n_iter=500, seed=12)
    assert lo <= point <= hi
    # Trimmed mean of full sorted sample (drop max=10) = mean(1..9) = 5.0
    assert abs(point - 5.0) < 1e-9


def test_bootstrap_with_median_statistic():
    samples = list(range(1, 21))
    point, lo, hi = bootstrap_ci(samples, statistic_fn=np.median, n_iter=1000, seed=14)
    # Median of 1..20 = 10.5
    assert abs(point - 10.5) < 1e-9
    assert lo < 10.5 < hi


def test_winsorize_clips_extremes():
    """Winsorizing replaces extremes with the boundary quantile values."""
    samples = list(range(100))
    clipped = winsorize(samples, clip_frac=0.05)
    # 5th-percentile ≈ 4.95, 95th-percentile ≈ 94.05; clipped should hit those bounds
    assert clipped.min() >= 4.0
    assert clipped.max() <= 95.0


def test_winsorize_clip_zero_is_noop():
    samples = [1.0, 2.0, 3.0, 100.0]
    clipped = winsorize(samples, clip_frac=0.0)
    assert list(clipped) == [1.0, 2.0, 3.0, 100.0]


def test_winsorize_invalid_clip_raises():
    with pytest.raises(ValueError, match="clip_frac"):
        winsorize([1.0, 2.0], clip_frac=0.5)


def test_winsorize_empty_returns_empty():
    out = winsorize([])
    assert out.size == 0


def test_winsorized_bootstrap_recovers_central_mean():
    """Heavy-tail-poisoned mean: 95 ones + 5 outliers at 1000.

    Naive bootstrap CI of the sum is wide and can include zero. Winsorized
    bootstrap clips the 5 outliers to the 95th-percentile (still 1.0 in
    this distribution), producing a tight CI around the true central mean.
    """
    samples = [1.0] * 95 + [1000.0] * 5
    raw_point, raw_lo, raw_hi = bootstrap_ci(
        samples, statistic_fn=lambda a: float(a.mean()), n_iter=2000, seed=42,
    )
    win_point, win_lo, win_hi = winsorized_bootstrap_ci(
        samples, clip_frac=0.05,
        statistic_fn=lambda a: float(a.mean()), n_iter=2000, seed=42,
    )
    # Winsorized point estimate is ~1.0 (the central tendency); raw is ~50.95
    assert win_point < 5.0
    assert raw_point > 30.0
    # Winsorized CI is tight; raw CI is wide
    assert (win_hi - win_lo) < (raw_hi - raw_lo)


def test_winsorized_bootstrap_seed_deterministic():
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 100.0]
    a = winsorized_bootstrap_ci(samples, n_iter=500, seed=7)
    b = winsorized_bootstrap_ci(samples, n_iter=500, seed=7)
    assert a == b


def test_winsorized_bootstrap_empty_returns_zeros():
    point, lo, hi = winsorized_bootstrap_ci([])
    assert (point, lo, hi) == (0.0, 0.0, 0.0)


def test_winsorized_bootstrap_single_sample_degenerate():
    point, lo, hi = winsorized_bootstrap_ci([3.5])
    assert point == 3.5
    assert lo == hi == 3.5


def test_bootstrap_default_iters_is_10000():
    """Spec §10.1 locks 10,000 iters as the default."""
    from sniper.analysis.wallet_protocol.statistics import DEFAULT_BOOTSTRAP_ITERS
    assert DEFAULT_BOOTSTRAP_ITERS == 10_000


# ----------------------------------------------------------------------------
# bootstrap_ci with null_value (M1 — empirical bootstrap p-value)
# ----------------------------------------------------------------------------


def test_bootstrap_p_null_none_preserves_legacy_signature():
    """When null_value is omitted, return shape stays (point, lo, hi)."""
    out = bootstrap_ci([1.0, 2.0, 3.0, 4.0, 5.0], n_iter=200, seed=1)
    assert len(out) == 3
    point, lo, hi = out
    assert lo <= point <= hi


def test_bootstrap_p_pure_positive_samples_p_zero():
    """All samples > null → 0% of resamples on the wrong side → p = 0."""
    samples = [0.01] * 100
    point, lo, hi, p = bootstrap_ci(
        samples, n_iter=2000, seed=42, null_value=0.0,
    )
    assert point > 0
    assert lo > 0
    assert p == 0.0


def test_bootstrap_p_pure_negative_samples_p_zero():
    """All samples < null → 0% of resamples on the wrong side → p = 0."""
    samples = [-0.01] * 100
    point, lo, hi, p = bootstrap_ci(
        samples, n_iter=2000, seed=42, null_value=0.0,
    )
    assert point < 0
    assert hi < 0
    assert p == 0.0


def test_bootstrap_p_symmetric_around_zero_p_high():
    """Symmetric around null → resamples split ~50/50 → p ≈ 1.0."""
    # 50 each of +0.01 / -0.01. Resample mean is symmetric around 0.
    samples = [0.01] * 50 + [-0.01] * 50
    point, lo, hi, p = bootstrap_ci(
        samples, n_iter=5000, seed=7, null_value=0.0,
    )
    # observed mean is ~0, both tails of the resample distribution
    # straddle zero → 2 * min(P(<=0), P(>=0)) ~ 1.0
    assert p > 0.85


def test_bootstrap_p_known_distribution_in_expected_range():
    """30 samples from N(0.05, 0.1): SE ≈ 0.0183, z ≈ 2.74 → analytic p ≈ 0.006.

    Bootstrap p should land in [0.0, 0.05] range and be stable across seeds
    that share the same input (the input itself is fixed via rng seed).
    """
    rng = np.random.default_rng(123)
    samples = rng.normal(loc=0.05, scale=0.1, size=30).tolist()
    out_a = bootstrap_ci(samples, n_iter=5000, seed=1, null_value=0.0)
    out_b = bootstrap_ci(samples, n_iter=5000, seed=2, null_value=0.0)
    p_a = out_a[3]
    p_b = out_b[3]
    # Bootstrap p across two different bootstrap seeds (same data) should
    # be in the analytically expected neighborhood.
    assert 0.0 <= p_a <= 0.05
    assert 0.0 <= p_b <= 0.05
    # Adjacent seeds produce p-values close to each other on a tight
    # underlying distribution — sanity bound.
    assert abs(p_a - p_b) < 0.02


def test_bootstrap_p_seed_deterministic():
    """Same seed + same data → identical p-value."""
    samples = [0.1, -0.05, 0.2, -0.3, 0.4, 0.05, -0.1, 0.0, 0.15, -0.2] * 5
    out_a = bootstrap_ci(samples, n_iter=1000, seed=99, null_value=0.0)
    out_b = bootstrap_ci(samples, n_iter=1000, seed=99, null_value=0.0)
    assert out_a == out_b


def test_bootstrap_p_in_unit_interval():
    """p must always be clipped to [0, 1] regardless of input."""
    rng = np.random.default_rng(0)
    for trial in range(5):
        samples = rng.normal(loc=trial * 0.2 - 0.4, scale=1.0, size=40).tolist()
        out = bootstrap_ci(samples, n_iter=500, seed=trial, null_value=0.0)
        p = out[3]
        assert 0.0 <= p <= 1.0


def test_bootstrap_p_empty_input_returns_conservative_one():
    """Empty input + null_value: no evidence → p = 1.0."""
    out = bootstrap_ci([], null_value=0.0)
    assert len(out) == 4
    assert out == (0.0, 0.0, 0.0, 1.0)


def test_bootstrap_p_single_sample_zero_or_one():
    """Single sample equals null → p=1.0; differs from null → p=0.0."""
    out_eq = bootstrap_ci([0.0], null_value=0.0)
    assert out_eq == (0.0, 0.0, 0.0, 1.0)
    out_neq = bootstrap_ci([3.5], null_value=0.0)
    assert out_neq == (3.5, 3.5, 3.5, 0.0)


def test_bootstrap_p_non_zero_null():
    """Null can be any value, not just 0."""
    samples = [10.5, 10.4, 10.6, 10.3, 10.7] * 10
    # Test against null=10.0 — all samples are above; p should be 0
    out = bootstrap_ci(samples, n_iter=1000, seed=11, null_value=10.0)
    assert out[3] == 0.0
    # Test against null=10.5 (mid of distribution) — p should be high
    out = bootstrap_ci(samples, n_iter=1000, seed=11, null_value=10.5)
    assert out[3] > 0.5


def test_winsorized_bootstrap_p_passes_null_value_through():
    """winsorized_bootstrap_ci honors null_value and returns 4-tuple."""
    samples = [1.0] * 95 + [1000.0] * 5
    out = winsorized_bootstrap_ci(
        samples, clip_frac=0.05, n_iter=2000, seed=42, null_value=0.0,
    )
    assert len(out) == 4
    point, lo, hi, p = out
    # Winsorized point ~ 1.0 (above null), CI excludes 0 → p == 0
    assert lo > 0
    assert p == 0.0


def test_winsorized_bootstrap_no_null_value_keeps_legacy_signature():
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = winsorized_bootstrap_ci(samples, n_iter=200, seed=1)
    assert len(out) == 3


# ----------------------------------------------------------------------------
# cluster_bootstrap_ci (M6 — cluster-adjusted H4 bootstrap)
# ----------------------------------------------------------------------------


def test_cluster_bootstrap_seed_deterministic():
    samples = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    clusters = ["a", "a", "a", "b", "b", "b"]
    a = cluster_bootstrap_ci(samples, clusters, n_iter=500, seed=42)
    b = cluster_bootstrap_ci(samples, clusters, n_iter=500, seed=42)
    assert a == b


def test_cluster_bootstrap_single_cluster_collapses_to_point():
    """One cluster → every resample is the same data → CI = (point, point)."""
    samples = [1.0, 2.0, 3.0, 4.0]
    clusters = ["x"] * 4
    point, lo, hi = cluster_bootstrap_ci(samples, clusters, n_iter=500, seed=7)
    assert lo == hi == point == 2.5


def test_cluster_bootstrap_empty_input_zeros():
    point, lo, hi = cluster_bootstrap_ci([], [], n_iter=100, seed=1)
    assert (point, lo, hi) == (0.0, 0.0, 0.0)


def test_cluster_bootstrap_length_mismatch_raises():
    with pytest.raises(ValueError, match="must align"):
        cluster_bootstrap_ci([1.0, 2.0], ["a"], n_iter=100, seed=1)


def test_cluster_bootstrap_each_singleton_matches_fill_bootstrap():
    """When every cluster has size 1, cluster-resample == fill-resample."""
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    clusters = list(range(len(samples)))
    fill = bootstrap_ci(samples, n_iter=2000, seed=11)
    cluster = cluster_bootstrap_ci(samples, clusters, n_iter=2000, seed=11)
    assert cluster[0] == fill[0]  # point statistic identical
    # CI bounds drift slightly — different RNG draw shape — but mean-of-bounds
    # should match within a tight tolerance since both procedures sample
    # n indices uniformly from {0..n-1}.
    assert abs(cluster[1] - fill[1]) < 0.15
    assert abs(cluster[2] - fill[2]) < 0.15


def test_cluster_bootstrap_two_clusters_imbalanced():
    """Imbalanced cluster sizes resample by cluster, not by member count."""
    samples = [10.0] * 100 + [20.0]  # 100 fills cluster A, 1 fill cluster B
    clusters = ["A"] * 100 + ["B"]
    rng_iters = 4000
    point, lo, hi = cluster_bootstrap_ci(
        samples, clusters, n_iter=rng_iters, seed=99,
    )
    # Cluster bootstrap draws 2 clusters with replacement → 4 outcomes:
    # {AA, AB, BA, BB} with prob {0.25, 0.25, 0.25, 0.25}. Resample means:
    #   AA -> 10.0
    #   AB -> mean of [10×100, 20×1] ≈ 10.0990
    #   BA -> same ≈ 10.0990
    #   BB -> 20.0
    # 95% CI should span roughly 10.0 to 20.0 (the BB outcome is the upper
    # tail). Fill-level bootstrap on the same data would give a CI tightly
    # around 10.099 — that's the bias cluster-bootstrap corrects.
    assert lo <= 10.5
    assert hi >= 15.0  # cluster CI honors the rare BB resample


def test_cluster_bootstrap_null_value_p_zero_for_pure_positive():
    """All cluster means above null → resample mean rarely <=0 → p≈0."""
    samples = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    clusters = ["a", "a", "b", "b", "c", "c"]
    point, lo, hi, p = cluster_bootstrap_ci(
        samples, clusters, n_iter=2000, seed=5, null_value=0.0,
    )
    assert point > 0
    assert lo > 0  # CI cleanly excludes zero
    assert p < 0.05


# ----------------------------------------------------------------------------
# permutation_test_spearman
# ----------------------------------------------------------------------------


def test_spearman_perfect_positive_correlation():
    """Strictly increasing y on increasing x → ρ ≈ 1, p ≈ 0."""
    x = list(range(20))
    y = [v * 2 + 0.1 for v in x]
    rho, p = permutation_test_spearman(x, y, n_iter=500, seed=3)
    assert rho > 0.99
    # +1 smoothing → minimum p = 1/(n_iter+1)
    assert p < 0.01


def test_spearman_no_correlation_yields_high_p():
    """Independent shuffled y → no correlation, p > 0.10."""
    rng = np.random.default_rng(4)
    x = rng.normal(size=50).tolist()
    y = rng.normal(size=50).tolist()
    rho, p = permutation_test_spearman(x, y, n_iter=2000, seed=5)
    assert -0.5 < rho < 0.5
    assert p > 0.10


def test_spearman_perfect_negative_correlation():
    x = list(range(20))
    y = [-v for v in x]
    rho, p = permutation_test_spearman(x, y, n_iter=500, seed=6)
    assert rho < -0.99
    # two-sided test → still significant
    assert p < 0.01


def test_spearman_handles_short_input():
    rho, p = permutation_test_spearman([1.0], [2.0])
    assert (rho, p) == (0.0, 1.0)


# ----------------------------------------------------------------------------
# permutation_test_difference
# ----------------------------------------------------------------------------


def test_diff_test_detects_separated_distributions():
    rng = np.random.default_rng(8)
    a = rng.normal(loc=2.0, scale=1.0, size=80).tolist()
    b = rng.normal(loc=0.0, scale=1.0, size=80).tolist()
    diff, p = permutation_test_difference(a, b, n_iter=2000, seed=9)
    assert diff > 1.0
    assert p < 0.05


def test_diff_test_no_difference_yields_high_p():
    rng = np.random.default_rng(10)
    a = rng.normal(0, 1, size=80).tolist()
    b = rng.normal(0, 1, size=80).tolist()
    _, p = permutation_test_difference(a, b, n_iter=2000, seed=11)
    assert p > 0.10


# ----------------------------------------------------------------------------
# bh_fdr_correct
# ----------------------------------------------------------------------------


def test_bh_fdr_default_q_is_010():
    """Spec §6.4, §7.7 lock q=0.10."""
    assert BH_FDR_Q == 0.10


def test_bh_fdr_preserves_input_order():
    raw = [0.001, 0.5, 0.04]
    adjusted, rejected = bh_fdr_correct(raw, q=0.10)
    assert len(adjusted) == 3
    assert len(rejected) == 3
    # Indices must map back to input order
    assert adjusted[0] < adjusted[1]
    assert adjusted[2] < adjusted[1]


def test_bh_fdr_rejects_significant_p():
    raw = [0.001, 0.001, 0.001, 0.5]
    adjusted, rejected = bh_fdr_correct(raw, q=0.10)
    assert rejected[0] and rejected[1] and rejected[2]
    assert not rejected[3]


def test_bh_fdr_no_rejections_when_all_high():
    raw = [0.5, 0.6, 0.7]
    _, rejected = bh_fdr_correct(raw, q=0.10)
    assert not any(rejected)


def test_bh_fdr_handles_nan_as_one():
    """NaN p-value treated as 1.0 (no rejection)."""
    raw = [float("nan"), 0.001]
    adjusted, rejected = bh_fdr_correct(raw, q=0.10)
    assert adjusted[0] >= 0.99
    assert not rejected[0]
    assert rejected[1]


def test_bh_fdr_empty_input():
    adjusted, rejected = bh_fdr_correct([])
    assert adjusted == []
    assert rejected == []


def test_bh_fdr_step_up_monotone_after_sort():
    """Adjusted p-values, when re-sorted by raw p, must be non-decreasing."""
    raw = [0.001, 0.01, 0.04, 0.05, 0.5]
    adjusted, _ = bh_fdr_correct(raw, q=0.10)
    paired = sorted(zip(raw, adjusted), key=lambda t: t[0])
    sorted_adj = [a for _, a in paired]
    for i in range(1, len(sorted_adj)):
        assert sorted_adj[i] >= sorted_adj[i - 1] - 1e-12


# ----------------------------------------------------------------------------
# cusum_signal
# ----------------------------------------------------------------------------


def test_cusum_default_h_matches_calibrated():
    """h=25.0 locked per §9.1 brain memory."""
    assert CUSUM_H_CALIBRATED == 25.0
    assert CUSUM_K_SIGMA == 0.5
    assert CUSUM_H_FALLBACK == 5.0


def test_cusum_no_signal_on_stationary_series():
    """i.i.d. series with no regime change → no signal."""
    rng = np.random.default_rng(20)
    series = rng.normal(loc=0.0, scale=1.0, size=200).tolist()
    result = cusum_signal(series, h=CUSUM_H_CALIBRATED)
    assert result.cusum_status == "calibrated"
    # Heavy-tail-calibrated h=25 should not signal on plain N(0,1) most of the time
    # (false-alarm 3.1% per spec). With seed=20, this run is in the 96.9% non-signal.
    assert not result.signaled


def test_cusum_signals_on_clear_regime_shift():
    """Mean shifts from 0 to +5 halfway through → CUSUM should fire AFTER the shift.

    Realistic deployment: mu/sigma come from an in-sample stable baseline
    (here, the pre-regime portion). Using the full series mu causes the
    detector to fire spuriously on the early portion (which now looks like
    a negative-deviation regime relative to the global mean).
    """
    rng = np.random.default_rng(21)
    pre = rng.normal(loc=0.0, scale=1.0, size=200)
    post = rng.normal(loc=5.0, scale=1.0, size=200)
    series = np.concatenate([pre, post]).tolist()
    # In-sample mu/sigma from the stable pre-regime period
    pre_mu = float(pre.mean())
    pre_sigma = float(pre.std(ddof=1))
    result = cusum_signal(
        series, h=CUSUM_H_CALIBRATED, mu=pre_mu, sigma=pre_sigma,
    )
    assert result.signaled
    assert result.signal_index is not None
    # Detection happens shortly after the regime change at index 200
    assert result.signal_index >= 200


def test_cusum_signals_on_downward_shift_when_lower_direction():
    """direction='lower' fires on a negative regime change."""
    rng = np.random.default_rng(30)
    pre = rng.normal(loc=0.0, scale=1.0, size=200)
    post = rng.normal(loc=-5.0, scale=1.0, size=200)
    series = np.concatenate([pre, post]).tolist()
    pre_mu = float(pre.mean())
    pre_sigma = float(pre.std(ddof=1))
    result = cusum_signal(
        series, h=CUSUM_H_CALIBRATED, mu=pre_mu, sigma=pre_sigma,
        direction="lower",
    )
    assert result.signaled
    assert result.signal_index is not None and result.signal_index >= 200
    # 'lower' is not in the calibration sample; flagged uncalibrated.
    assert result.cusum_status == "uncalibrated"


def test_cusum_upper_default_does_not_fire_on_downward_shift():
    """Spec-default 'upper' detector ignores downward shifts (intentional)."""
    rng = np.random.default_rng(31)
    pre = rng.normal(loc=0.0, scale=1.0, size=100)
    post = rng.normal(loc=-5.0, scale=1.0, size=100)
    series = np.concatenate([pre, post]).tolist()
    pre_mu = float(pre.mean())
    pre_sigma = float(pre.std(ddof=1))
    result = cusum_signal(series, mu=pre_mu, sigma=pre_sigma)  # default direction='upper'
    assert not result.signaled


def test_cusum_two_sided_marked_uncalibrated_at_locked_h():
    """direction='two_sided' at h=25 doubles false-alarm; must NOT report calibrated."""
    series = [0.1, -0.1, 0.05, -0.05] * 50
    result = cusum_signal(series, h=CUSUM_H_CALIBRATED, direction="two_sided")
    assert result.cusum_status == "uncalibrated"


def test_cusum_invalid_direction_raises():
    import pytest
    with pytest.raises(ValueError, match="direction"):
        cusum_signal([0.0, 1.0, 2.0], direction="sideways")


def test_cusum_signal_index_upper_bounded_in_clear_shift():
    """Detection latency on a +5σ shift should be short (< ~50 steps post-break)."""
    rng = np.random.default_rng(32)
    pre = rng.normal(loc=0.0, scale=1.0, size=200)
    post = rng.normal(loc=5.0, scale=1.0, size=200)
    series = np.concatenate([pre, post]).tolist()
    result = cusum_signal(
        series, h=CUSUM_H_CALIBRATED,
        mu=float(pre.mean()), sigma=float(pre.std(ddof=1)),
    )
    assert result.signaled
    assert result.signal_index is not None
    # Must fire within 50 positions of the regime change (defensive upper bound)
    assert 200 <= result.signal_index < 250


def test_cusum_uncalibrated_status_when_h_overridden():
    """Passing non-calibrated h marks the result as uncalibrated."""
    series = [0.1, 0.2, -0.1, 0.0, 0.05]
    result = cusum_signal(series, h=CUSUM_H_FALLBACK)
    assert result.cusum_status == "uncalibrated"


def test_cusum_empty_series_returns_no_signal():
    result = cusum_signal([])
    assert not result.signaled
    assert result.signal_index is None


# ----------------------------------------------------------------------------
# time_split_70_30
# ----------------------------------------------------------------------------


def test_time_split_min_n_is_60():
    """Spec §6.3 / §7.8 lock n>=60 as eligibility threshold."""
    assert TIME_SPLIT_MIN_N == 60


def test_time_split_below_threshold_marked_ineligible():
    obs = [{"ts": i} for i in range(50)]
    train, test, eligible = time_split_70_30(obs, sort_key=lambda x: x["ts"])
    assert not eligible
    assert test == []
    assert len(train) == 50


def test_time_split_70_30_partition_at_threshold():
    obs = [{"ts": i} for i in range(60)]
    train, test, eligible = time_split_70_30(obs, sort_key=lambda x: x["ts"])
    assert eligible
    assert len(train) == 42
    assert len(test) == 18
    # Strict ordering: train all earlier than test
    assert train[-1]["ts"] < test[0]["ts"]


def test_time_split_sorts_input():
    """Out-of-order input is sorted by sort_key before splitting."""
    obs = [{"ts": i} for i in [50, 10, 80, 20, 70, 0, 90, 30, 60, 40]]
    obs += [{"ts": i} for i in range(100, 160)]  # 70 total
    train, test, eligible = time_split_70_30(obs, sort_key=lambda x: x["ts"])
    assert eligible
    # First train element must be the earliest ts
    assert train[0]["ts"] == 0
    # Test set boundary check
    assert train[-1]["ts"] < test[0]["ts"]


def test_time_split_stable_for_ties():
    """Identical sort keys preserve relative input order."""
    obs = [{"ts": 5, "id": i} for i in range(80)]
    train, _, _ = time_split_70_30(obs, sort_key=lambda x: x["ts"])
    # Stable sort → ids stay in original order within tie group
    assert [r["id"] for r in train[:5]] == [0, 1, 2, 3, 4]


def test_time_split_no_sort_key_uses_input_order():
    obs = list(range(80))
    train, test, eligible = time_split_70_30(obs)
    assert eligible
    assert train == list(range(56))
    assert test == list(range(56, 80))
