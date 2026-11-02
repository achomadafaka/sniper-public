"""Stress tests for flipper-branch k-fold OOS validation."""

from __future__ import annotations

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import run_flipper_branch
from sniper.analysis.wallet_protocol.statistics import (
    MIN_POWERED_FOLDS,
    decision_rule_majority_supported,
)


def _flip(i: int, *, roi: float = 0.20) -> Flip:
    buy_price = 0.50
    sell_price = buy_price * (1.0 + roi)
    return Flip(
        condition_id=f"cid_{i}",
        outcome="Yes",
        buy_ts=1_700_000_000 + i,
        sell_ts=1_700_000_100 + i,
        buy_price=buy_price,
        sell_price=sell_price,
        size=100.0,
        buy_notional=50.0,
        flip_roi=roi,
        hold_duration_seconds=100,
        asset_id=f"asset_{i}",
    )


def test_flipper_time_split_uses_kfold_statuses_for_fh1_fh2():
    flips = [_flip(i, roi=0.20) for i in range(120)]
    report = run_flipper_branch(flips, n_iter=300, seed=7)

    for name in ("FH1", "FH2"):
        split = report.time_split[name]
        assert split.n_folds_total >= 5
        assert len(split.per_fold_statuses) == split.n_folds_total
        assert split.holdout_status == decision_rule_majority_supported(
            split.per_fold_statuses
        )
        powered = [
            status for status in split.per_fold_statuses
            if status != "underpowered"
        ]
        if split.holdout_status == "survived":
            assert len(powered) >= MIN_POWERED_FOLDS


def test_majority_rule_blocks_one_good_fold_four_underpowered():
    statuses = ["supported", "underpowered", "underpowered", "underpowered", "underpowered"]
    assert decision_rule_majority_supported(statuses) == "underpowered"
