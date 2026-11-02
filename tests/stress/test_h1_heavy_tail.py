"""Stress tests for H1 under finite-mean heavy-tail contamination."""

from __future__ import annotations

import numpy as np

from sniper.analysis.wallet_protocol.held_analyst import run_h1_net_pnl
from sniper.analysis.wallet_protocol.statistics import robust_location_bootstrap_ci


def _contaminated_matrix(*, mu: float, n_trials: int = 1000, n: int = 200):
    rng = np.random.default_rng(20260430)
    normal = rng.normal(loc=mu, scale=1.0, size=(n_trials, n))
    tail = rng.standard_t(df=3, size=(n_trials, n)) * 5.0 + mu
    use_tail = rng.random((n_trials, n)) < 0.05
    return np.where(use_tail, tail, normal)


def test_robust_location_recovers_positive_sign_under_heavy_tails():
    samples = _contaminated_matrix(mu=0.5)
    supported = 0
    for i, row in enumerate(samples):
        point, lo, hi, p_value, details = robust_location_bootstrap_ci(
            row,
            n_iter=300,
            seed=10_000 + i,
            null_value=0.0,
        )
        assert details["tested_location_statistic"] in {
            "winsorized_mean",
            "median_iqr_bootstrap",
        }
        assert point > 0
        if lo > 0 and p_value <= 0.10:
            supported += 1
    assert supported / len(samples) >= 0.95


def test_robust_location_false_alarm_rate_at_zero_is_bounded():
    samples = _contaminated_matrix(mu=0.0)
    false_positives = 0
    for i, row in enumerate(samples):
        _point, lo, _hi, p_value, _details = robust_location_bootstrap_ci(
            row,
            n_iter=300,
            seed=20_000 + i,
            null_value=0.0,
        )
        if lo > 0 and p_value <= 0.10:
            false_positives += 1
    assert false_positives / len(samples) <= 0.05


def test_h1_reports_location_statistic_not_raw_mean():
    closed = [
        {
            "conditionId": f"cid_{i}",
            "outcome": "Yes",
            "realizedPnl": str(value),
            "avgPrice": "0.5",
            "size": "2",
            "totalBought": "1",
            "curPrice": "1.0",
        }
        for i, value in enumerate([1.0] * 95 + [1000.0] * 5)
    ]
    h1 = run_h1_net_pnl(closed, n_iter=300, seed=42)
    assert h1.details["tested_location_statistic"] in {
        "winsorized_mean",
        "median_iqr_bootstrap",
    }
    assert h1.details["tested_location_statistic"] != "raw_mean"
