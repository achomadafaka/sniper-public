"""Online false-discovery control over the wallet stream. We scan many wallets and
re-analyze each continuously; without correction a large pipeline mints 'anomalies' by
chance. The conservative e-value bound must keep false discoveries bounded under the null
and not inflate under repeated peeking."""
from __future__ import annotations

import random

from sniper.analysis.winning_wallet_thesis import online_fdr as F
from sniper.analysis.winning_wallet_thesis import stats as ST


def test_threshold_grows_with_index():
    assert F.fdr_threshold(100) > F.fdr_threshold(1)


def test_threshold_is_inverse_alpha_gamma():
    # t=1: gamma=6/pi^2 ~ 0.6079; threshold = 1/(alpha*gamma)
    import math
    expected = 1.0 / (0.10 * (6.0 / math.pi ** 2))
    assert abs(F.fdr_threshold(1, 0.10) - expected) < 1e-9


def test_false_discoveries_bounded_on_null_stream():       # case 12
    rng = random.Random(7)
    false_disc = 0
    for t in range(1, 3001):
        ps = [rng.uniform(0.1, 0.6) for _ in range(6)]
        trials = [(p, 1 if rng.random() < p else 0) for p in ps]  # null: outcome ~ Bernoulli(p)
        e = ST.eprocess_value(trials)["e_value"]
        if F.is_flag_eligible(e, t):
            false_disc += 1
    # E[# false discoveries] <= alpha = 0.1 across the whole stream; allow generous slack
    assert false_disc <= 3


def test_peeking_does_not_inflate_alerts():                # case 11
    rng = random.Random(3)
    flips = 0
    for run in range(200):
        trials = [(0.5, 1 if rng.random() < 0.5 else 0) for _ in range(10)]
        e = ST.eprocess_value(trials)["e_value"]
        if F.is_flag_eligible(e, t=run + 1):
            flips += 1
    assert flips <= 5


def test_true_anomaly_still_flags_early():
    # a genuine penny-win wallet at a low test index clears the threshold
    e = ST.eprocess_value([(0.02, 1)] * 5)["e_value"]
    assert F.is_flag_eligible(e, t=1)
