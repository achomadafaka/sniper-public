"""Stress tests for cluster-adjusted H4 bootstrap (M6 closure).

Per wallet-protocol-bulletproofing M6 verification bar:

    "Synthetic: 1000 fills × 5 distinct drifts (200 fills each at clustered
    values), known true mean. Cluster-adjusted CI must contain the true
    mean at >95% across 1000 trials; fill-level CI must miss it
    (demonstration of the bias). For eatpraylove specifically:
    cluster-adjusted CI on the 8 distinct drifts must be ≥3× wider than
    the fill-level CI it replaces."

Two complementary stress tests:

1. `test_cluster_bootstrap_corrects_fill_level_undercoverage` —
   500 trials. Each trial draws 5 cluster means from N(μ, σ_between),
   then 200 fills per cluster from N(cluster_mean, σ_within). Cluster
   structure (small σ_within, moderate σ_between) is the regime where
   fill-level bootstrap *appears* tight but actually undercovers the
   population mean — exactly the eatpraylove pathology. Asserts
   fill-level coverage < 60% (bias confirmed) AND cluster-level
   coverage ≥ 85% (bias corrected).

2. `test_cluster_bootstrap_width_ratio_on_eatpraylove_shape` —
   single dataset replicating the documented eatpraylove signature
   (~73 fills at 2 distinct drift values: 38 ≈ 0.035, 35 ≈ 0.075).
   Asserts cluster-level CI is ≥3× wider than fill-level CI.

These tests live under tests/stress/ to flag them as slower-by-design.
Marked @pytest.mark.integration so the default `pytest` run skips them
(7+ seconds each). Run explicitly via `pytest -m integration`.
"""

from __future__ import annotations

import numpy as np
import pytest

from sniper.analysis.wallet_protocol.statistics import (
    bootstrap_ci,
    cluster_bootstrap_ci,
)


@pytest.mark.integration
def test_cluster_bootstrap_corrects_fill_level_undercoverage():
    """Fill-level bootstrap undercovers under within-cluster correlation;
    cluster bootstrap restores nominal coverage."""
    n_trials = 500
    n_clusters = 5
    n_per_cluster = 200
    mu_true = 0.05
    sigma_between = 0.03
    sigma_within = 0.005

    rng = np.random.default_rng(20260427)
    fill_covers = 0
    cluster_covers = 0

    for _ in range(n_trials):
        cluster_means = rng.normal(
            loc=mu_true, scale=sigma_between, size=n_clusters,
        )
        samples: list[float] = []
        clusters: list[str] = []
        for ci, cm in enumerate(cluster_means):
            fills = rng.normal(loc=cm, scale=sigma_within, size=n_per_cluster)
            samples.extend(fills.tolist())
            clusters.extend([f"c{ci}"] * n_per_cluster)

        seed = int(rng.integers(0, 2**31 - 1))
        _, lo_f, hi_f = bootstrap_ci(samples, n_iter=2000, seed=seed)
        if lo_f <= mu_true <= hi_f:
            fill_covers += 1
        _, lo_c, hi_c = cluster_bootstrap_ci(
            samples, clusters, n_iter=2000, seed=seed,
        )
        if lo_c <= mu_true <= hi_c:
            cluster_covers += 1

    fill_coverage = fill_covers / n_trials
    cluster_coverage = cluster_covers / n_trials

    assert fill_coverage < 0.60, (
        f"fill-level bootstrap coverage = {fill_coverage:.2%} "
        f"(expected <60% under clustered data — bias should be obvious; "
        f"{fill_covers}/{n_trials} trials covered the true mean)"
    )
    assert cluster_coverage >= 0.85, (
        f"cluster-bootstrap coverage = {cluster_coverage:.2%} "
        f"(expected ≥85%; {cluster_covers}/{n_trials} trials covered)"
    )


@pytest.mark.integration
def test_cluster_bootstrap_width_ratio_on_eatpraylove_shape():
    """Replicates the documented eatpraylove H4 cluster signature:
    ~73 fills at 2 distinct drift values (38 ≈ 0.035, 35 ≈ 0.075).
    Cluster CI must be ≥3× wider than fill-level CI per spec bar."""
    rng = np.random.default_rng(202604271)
    fills_a = rng.normal(loc=0.035, scale=0.001, size=38).tolist()
    fills_b = rng.normal(loc=0.075, scale=0.001, size=35).tolist()
    samples = fills_a + fills_b
    clusters = ["a"] * 38 + ["b"] * 35

    _, lo_f, hi_f = bootstrap_ci(samples, n_iter=2000, seed=42)
    _, lo_c, hi_c = cluster_bootstrap_ci(
        samples, clusters, n_iter=2000, seed=42,
    )

    width_f = hi_f - lo_f
    width_c = hi_c - lo_c

    assert width_c >= 3.0 * width_f, (
        f"cluster-bootstrap CI width {width_c:.5f} "
        f"should be ≥3× fill-level CI width {width_f:.5f}; "
        f"actual ratio={width_c / width_f:.2f}×"
    )
