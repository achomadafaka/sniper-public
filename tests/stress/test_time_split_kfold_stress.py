"""Stress tests for the rolling-origin k-fold time split (M3).

Per wallet-protocol-bulletproofing closure protocol §3.1, every protocol
change ships a synthetic-distribution stress test that locks the
empirical pass rate. M3's verification bar:

    On a stationary synthetic series with known edge, k-fold should
    support in >=4 of 5 folds at >95% power.

These trials run end-to-end against the helded-branch hypothesis runner
(H2 excess WR) so we exercise the whole pipeline, not just the split
function. They are slow by design — kept under tests/stress/ so the
default pytest suite skips them. Run explicitly:

    python3 -m pytest tests/stress/test_time_split_kfold_stress.py -q
"""

from __future__ import annotations

import random


def _make_held_positions(
    n: int,
    win_rate: float,
    seed: int,
    sub_080_share: float = 1.0,
) -> list[dict]:
    """Build n synthetic held-to-resolution rows.

    Schema matches what `run_h2_excess_wr` consumes:
      - timestamp (entry ts; used for sort)
      - entry_price
      - last_price (resolution price; near 0 or 1)
      - size_usdc (proxy for capital deployment)
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    base_ts = 1_700_000_000
    for i in range(n):
        is_sub_080 = rng.random() < sub_080_share
        entry = rng.uniform(0.30, 0.79) if is_sub_080 else rng.uniform(0.81, 0.99)
        is_win = rng.random() < win_rate
        cur = 0.999 if is_win else 0.001
        rows.append({
            "timestamp": base_ts + i * 3600,
            "avgPrice": entry,        # _entry_price helper reads avgPrice
            "curPrice": cur,          # _is_win helper reads curPrice
            "size": 1.0,
            "outcome": "Yes",
            "side": "BUY",
            "conditionId": f"0xc{i:04d}",
        })
    return rows


def _run_one_trial(seed: int, n: int = 2000, win_rate: float = 0.70) -> dict:
    """Run one synthetic wallet through k-fold and return per-fold statuses."""
    from sniper.analysis.wallet_protocol.held_analyst import run_h2_excess_wr
    from sniper.analysis.wallet_protocol.statistics import time_split_kfold

    rows = _make_held_positions(n, win_rate, seed=seed)
    folds, eligible = time_split_kfold(
        rows, sort_key=lambda p: p["timestamp"],
    )
    if not eligible:
        return {"eligible": False, "fold_statuses": []}
    statuses: list[str] = []
    for train, test in folds:
        train_h = run_h2_excess_wr(train, n_iter=300, seed=seed)
        test_h = run_h2_excess_wr(test, n_iter=300, seed=seed)
        if train_h.verdict == "underpowered" or test_h.verdict == "underpowered":
            statuses.append("underpowered")
        elif train_h.verdict == "supported" and test_h.verdict == "supported":
            statuses.append("supported")
        else:
            statuses.append("failed")
    return {"eligible": True, "fold_statuses": statuses}


def test_stationary_series_with_edge_supports_at_spec_power():
    """M3 spec bar: on a stationary synthetic series with known edge,
    k-fold should support in >=4 of 5 folds at >95% power.

    n=2000 / WR=0.70 / sub_080 entries uniform 0.30-0.79 -> per-position
    excess ~0.15. With test fold n=200 and SE ~0.034, every fold should
    cleanly reject the null. We assert >=95% of trials show 4-or-more
    'supported' folds (the spec power bar) AND a strict-majority survival
    rate of >=95%."""
    from sniper.analysis.wallet_protocol.statistics import (
        decision_rule_majority_supported,
    )

    n_trials = 30
    survived = 0
    four_plus_supported = 0
    for seed in range(n_trials):
        result = _run_one_trial(seed=seed, n=2000, win_rate=0.70)
        assert result["eligible"], f"trial {seed} not eligible"
        statuses = result["fold_statuses"]
        n_supported = sum(1 for s in statuses if s == "supported")
        verdict = decision_rule_majority_supported(statuses)
        if verdict == "survived":
            survived += 1
        if n_supported >= 4:
            four_plus_supported += 1

    survived_rate = survived / n_trials
    four_plus_rate = four_plus_supported / n_trials
    # Spec bar: >=4 of 5 folds supported in >95% of trials.
    assert four_plus_rate >= 0.95, (
        f"only {four_plus_supported}/{n_trials} ({four_plus_rate:.1%}) "
        f"trials had >=4-of-5 folds supported; spec bar is 95%"
    )
    # Majority-rule survival should also clear 95% as a byproduct.
    assert survived_rate >= 0.95, (
        f"k-fold survived only {survived}/{n_trials} ({survived_rate:.1%}); "
        f"expected >=95% with spec-grade power"
    )


def test_null_series_survives_at_under_alarm_rate():
    """Slow: ~30s. Runs N=20 trials at WR=0.50 (null; entry-implied prob
    averages 0.55 so true excess is slightly negative) and asserts the
    aggregate k-fold majority rule survives in <=10% of trials. Bounds
    the false-alarm rate end-to-end."""
    from sniper.analysis.wallet_protocol.statistics import (
        decision_rule_majority_supported,
    )

    n_trials = 30
    survived = 0
    for seed in range(100, 100 + n_trials):
        result = _run_one_trial(seed=seed, n=2000, win_rate=0.50)
        verdict = decision_rule_majority_supported(result["fold_statuses"])
        if verdict == "survived":
            survived += 1
    rate = survived / n_trials
    assert rate <= 0.10, (
        f"k-fold survived {survived}/{n_trials} ({rate:.1%}) on null series; "
        f"expected <=10% false-alarm rate"
    )
