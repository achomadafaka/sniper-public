"""Pure stats primitives for the fresh-anomaly engine — Poisson-binomial surprise,
anytime-valid e-process, empirical-Bayes shrinkage. These are the correctness lock:
the cheap-win false-positive that motivated the rebuild must read as ORDINARY here."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis import stats as ST


def test_poisson_binomial_12_at_020_three_wins_is_ordinary():
    # 12 bets each priced 0.20, 3 wins: P(>=3) ~ 0.44 -> ordinary variance, NOT an anomaly.
    sf = ST.poisson_binomial_sf([0.2] * 12, 3)
    assert 0.40 < sf < 0.48


def test_poisson_binomial_three_penny_wins_is_extreme():
    sf = ST.poisson_binomial_sf([0.01, 0.01, 0.01], 3)
    assert sf < 1e-5  # 0.01^3 = 1e-6


def test_poisson_binomial_obs_zero_is_one():
    assert ST.poisson_binomial_sf([0.3, 0.4], 0) == 1.0


def test_poisson_binomial_obs_above_n_is_zero():
    assert ST.poisson_binomial_sf([0.3, 0.4], 3) == 0.0


def test_eprocess_high_for_clear_outperformance():
    trials = [(0.05, 1)] * 8  # 8 wins each priced 5% -> huge e-value
    out = ST.eprocess_value(trials)
    assert out["e_value"] > 20 and out["anytime_p"] < 0.05


def test_eprocess_near_one_under_null():
    # wins land exactly at expectation -> e-value should not be significant
    trials = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
    out = ST.eprocess_value(trials)
    assert out["e_value"] < 5 and out["anytime_p"] > 0.2


def test_eprocess_increments_nonnegative_at_lambda_max():
    # m = 1 + lambda*(y-p) must stay >= 0 even for losses at the highest-priced trial
    out = ST.eprocess_value([(0.9, 0), (0.9, 0)])
    assert out["e_value"] >= 0


def test_eprocess_empty_is_neutral():
    out = ST.eprocess_value([])
    assert out["e_value"] == 1.0 and out["anytime_p"] == 1.0


def test_eb_shrinks_thin_extreme_toward_one():
    raw = 3 / 0.3  # observed/expected = 10x on tiny N
    shrunk = ST.eb_shrunk_oe(W=3, E0=0.3, k=5.0)
    assert 1.0 < shrunk < raw and shrunk < 2.5
