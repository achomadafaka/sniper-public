"""Acceptance tests for `time_split_kfold` (M3 closure).

Spec wallet-protocol-bulletproofing M3: replace the single 70/30 cut with a
rolling-origin k-fold time split so the OOS verdict isn't determined by one
unlucky cut. Decision rule: a hypothesis is "survived" when the majority of
folds (≥ ceil(k/2)+? — see decision_rule_majority_supported) survived.

For k=5 the folds are:
- fold 0: train [0, 0.5), test [0.5, 0.6)
- fold 1: train [0, 0.6), test [0.6, 0.7)
- fold 2: train [0, 0.7), test [0.7, 0.8)
- fold 3: train [0, 0.8), test [0.8, 0.9)
- fold 4: train [0, 0.9), test [0.9, 1.0)
"""

from __future__ import annotations


def test_underpowered_when_below_min_n():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    folds, eligible = time_split_kfold(list(range(20)), k=5, min_n=60)
    assert eligible is False
    assert folds == []


def test_eligible_returns_k_folds():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    obs = list(range(100))
    folds, eligible = time_split_kfold(obs, k=5, min_n=60)
    assert eligible is True
    assert len(folds) == 5


def test_rolling_origin_train_grows_test_advances():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    obs = list(range(100))
    folds, _ = time_split_kfold(obs, k=5, min_n=60)
    # Expected slice anchors at 0.5, 0.6, 0.7, 0.8, 0.9
    expected_train_ends = [50, 60, 70, 80, 90]
    expected_test_ends  = [60, 70, 80, 90, 100]
    for i, (train, test) in enumerate(folds):
        assert train == obs[:expected_train_ends[i]], f"fold {i} train"
        assert test == obs[expected_train_ends[i]:expected_test_ends[i]], f"fold {i} test"


def test_train_test_disjoint_per_fold():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    obs = list(range(80))
    folds, _ = time_split_kfold(obs, k=5, min_n=60)
    for train, test in folds:
        assert set(map(id, train)).isdisjoint(set(map(id, test)))


def test_sort_key_applied_before_slicing():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    obs = [{"ts": t, "i": i} for i, t in enumerate([5, 1, 4, 2, 3] * 20)]
    folds, eligible = time_split_kfold(obs, k=5, sort_key=lambda r: r["ts"], min_n=60)
    assert eligible
    # First fold's train must start at the smallest ts, end at fold boundary
    train0 = folds[0][0]
    timestamps = [r["ts"] for r in train0]
    assert timestamps == sorted(timestamps)
    # Across folds, the boundary moves forward only
    for i in range(len(folds) - 1):
        assert len(folds[i][0]) <= len(folds[i + 1][0])


def test_majority_survived_majority_rule():
    from sniper.analysis.wallet_protocol.statistics import (
        decision_rule_majority_supported,
    )
    # 3-of-5 supported => survived
    assert decision_rule_majority_supported(
        ["supported", "supported", "supported", "failed", "failed"]
    ) == "survived"
    # 2-of-5 supported => failed
    assert decision_rule_majority_supported(
        ["supported", "supported", "failed", "failed", "failed"]
    ) == "failed"
    # all underpowered => underpowered
    assert decision_rule_majority_supported(
        ["underpowered"] * 5
    ) == "underpowered"
    # mixed underpowered+supported: underpowered folds excluded from numerator
    # AND from denominator. 2 supported / 3 powered = majority => survived.
    assert decision_rule_majority_supported(
        ["supported", "supported", "failed", "underpowered", "underpowered"]
    ) == "survived"
    # 1 supported / 3 powered = minority => failed
    assert decision_rule_majority_supported(
        ["supported", "failed", "failed", "underpowered", "underpowered"]
    ) == "failed"


def test_majority_rule_underpowered_when_too_few_powered_folds():
    """Punch-list item #2: a single supported fold with the rest underpowered
    used to flip the verdict to 'survived' under strict majority of powered
    (1*2 > 1). Require >=MIN_POWERED_FOLDS powered folds before the rule
    fires; otherwise the aggregate is 'underpowered'.
    """
    from sniper.analysis.wallet_protocol.statistics import (
        MIN_POWERED_FOLDS,
        decision_rule_majority_supported,
    )

    assert MIN_POWERED_FOLDS == 3

    # 1 supported, 4 underpowered: would have been "survived" under the old
    # strict-majority rule; with the floor it must be "underpowered".
    assert decision_rule_majority_supported(
        ["supported", "underpowered", "underpowered", "underpowered", "underpowered"]
    ) == "underpowered"

    # 2 supported, 3 underpowered: n_powered=2 < 3 -> still underpowered.
    assert decision_rule_majority_supported(
        ["supported", "supported", "underpowered", "underpowered", "underpowered"]
    ) == "underpowered"

    # 3 supported, 2 underpowered: n_powered==floor and unanimous powered -> survived.
    assert decision_rule_majority_supported(
        ["supported", "supported", "supported", "underpowered", "underpowered"]
    ) == "survived"

    # 1 supported + 2 failed (n_powered=3, 1*2=2 not > 3) -> failed (regression
    # against the floor accidentally upgrading minority verdicts).
    assert decision_rule_majority_supported(
        ["supported", "failed", "failed", "underpowered", "underpowered"]
    ) == "failed"


def test_kfold_default_k_5():
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold
    folds, _ = time_split_kfold(list(range(100)), min_n=60)
    assert len(folds) == 5


def test_kfold_min_n_default_matches_legacy():
    """Same minimum sample as time_split_70_30 to preserve back-compat."""
    from sniper.analysis.wallet_protocol.statistics import (
        time_split_kfold, TIME_SPLIT_MIN_N,
    )
    folds, eligible = time_split_kfold(list(range(TIME_SPLIT_MIN_N - 1)))
    assert eligible is False
    folds, eligible = time_split_kfold(list(range(TIME_SPLIT_MIN_N)))
    assert eligible is True
