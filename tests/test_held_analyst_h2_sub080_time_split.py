"""Regression test for the H2_sub080 time-split (Phase 3 v3 amendment A).

Locks the behavior introduced by commit 8a371a7
("feat(wallet-protocol/v3): H2_sub080 conditional promotion + diagnostic flags"):
when full-sample H2 is refuted but the sub-0.80 entry-price slice is
supported, the rolling-origin k-fold time-split must run on H2_sub080 and
return a powered, non-underpowered ``TimeSplitResult``.

Fixture mirrors the eatpraylove failure mode the v3 amendment fixed:
- 200 high-entry losers (avgPrice 0.95, ~15% WR)
- 200 low-entry winners (avgPrice 0.40, ~95% WR)
- Interleaved chronologically so CUSUM does not trim post-regime.
"""
from __future__ import annotations

import random
import time

import pytest

from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
from sniper.analysis.wallet_protocol.types import WalletData


def _make_held_position(*, avg_price: float, win: bool, ts: int, idx: int) -> dict:
    cur_price = 1.0 if win else 0.0
    return {
        "conditionId": f"0xcid{idx:04d}",
        "outcome": "yes",
        "avgPrice": avg_price,
        "size": 1.0,
        "curPrice": cur_price,
        # held-to-resolution PnL: (settle - entry) * size with size=1.
        "realizedPnl": (cur_price - avg_price) * 1.0,
        "timestamp": ts,
    }


@pytest.fixture
def eatpraylove_walletdata() -> WalletData:
    """Hand-built WalletData fixture exercising the v3-A failure mode:
    full H2 refuted, sub-0.80 H2 supported, n_held large enough for time-split."""
    rng = random.Random(42)
    n_a = 200  # high-entry cluster (>= 0.80, NOT in sub_080 set)
    n_b = 200  # low-entry cluster (< 0.80, in sub_080 set)
    a_win_count = 30   # 15% WR — losses dominate Cluster A
    b_win_count = 190  # 95% WR — wins dominate Cluster B

    a_win_set = set(rng.sample(range(n_a), a_win_count))
    b_win_set = set(rng.sample(range(n_b), b_win_count))

    positions: list[dict] = []
    # Interleave A / B by timestamp so CUSUM doesn't see a sustained upward
    # shift at the cluster boundary.
    for i in range(n_a):
        positions.append(
            _make_held_position(
                avg_price=0.95, win=(i in a_win_set), ts=i * 2, idx=i,
            )
        )
    for i in range(n_b):
        positions.append(
            _make_held_position(
                avg_price=0.40, win=(i in b_win_set), ts=i * 2 + 1,
                idx=i + n_a,
            )
        )

    return WalletData(
        wallet="0xeatpraylove",
        closed_positions=positions,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
    )


# Lower n_iter than the production default so the test runs quickly.
_TEST_N_ITER = 2_000


def test_fixture_no_cusum_trim(eatpraylove_walletdata):
    """Sanity: verify CUSUM does NOT trigger on this fixture (so the
    post-regime trim doesn't cut Cluster A out and break the H2 refutation).
    If this fails, the fixture needs re-balancing — the rest of the suite
    is meaningless without it."""
    branch = run_held_branch(eatpraylove_walletdata, n_iter=_TEST_N_ITER, seed=42)
    h2 = next(h for h in branch.hypotheses if h.name == "H2")
    # All 400 closed positions are held-to-resolution (curPrice ∈ {0, 1}).
    # CUSUM trimming would drop H2's n below 400.
    assert h2.n == 400, (
        f"CUSUM trimmed the fixture (H2 n={h2.n}, expected 400). "
        f"stationarity={branch.stationarity}"
    )


def test_full_h2_refuted_sub080_supported(eatpraylove_walletdata):
    """The fixture exercises the documented failure mode."""
    branch = run_held_branch(eatpraylove_walletdata, n_iter=_TEST_N_ITER, seed=42)
    h2 = next(h for h in branch.hypotheses if h.name == "H2")
    h2_sub = next(h for h in branch.hypotheses if h.name == "H2_sub080")
    assert h2.verdict == "refuted", (
        f"expected H2 refuted, got {h2.verdict} "
        f"(ci=[{h2.ci_lower}, {h2.ci_upper}], n={h2.n})"
    )
    assert h2_sub.verdict == "supported", (
        f"expected H2_sub080 supported, got {h2_sub.verdict} "
        f"(ci=[{h2_sub.ci_lower}, {h2_sub.ci_upper}], n={h2_sub.n})"
    )


def test_h2_sub080_time_split_runs_when_full_refuted_sub080_supported(
    eatpraylove_walletdata,
):
    """v3-A fix: time_split must include H2_sub080 with a powered fold result."""
    branch = run_held_branch(eatpraylove_walletdata, n_iter=_TEST_N_ITER, seed=42)
    assert "H2_sub080" in branch.time_split, (
        "v3-A regression: H2_sub080 time-split was not run"
    )
    ts = branch.time_split["H2_sub080"]
    assert ts.holdout_status in {"survived", "failed"}, (
        f"H2_sub080 time-split underpowered: holdout_status={ts.holdout_status} "
        f"per_fold={ts.per_fold_statuses}"
    )
    assert ts.n_train > 0 and ts.n_test > 0, (
        f"empty fold split: n_train={ts.n_train} n_test={ts.n_test}"
    )
    assert ts.n_folds_total >= 1, f"no folds produced: {ts.n_folds_total}"
    assert ts.n_folds_underpowered < ts.n_folds_total, (
        f"all folds underpowered: {ts.n_folds_underpowered}/{ts.n_folds_total}"
    )


def test_h2_full_refuted_does_not_block_sub080_time_split(eatpraylove_walletdata):
    """Pre-v3 bug: time-split skipped sub080 when full H2 was refuted.
    Post-fix: sub080 time-split runs on its own merits."""
    branch = run_held_branch(eatpraylove_walletdata, n_iter=_TEST_N_ITER, seed=42)
    h2 = next(h for h in branch.hypotheses if h.name == "H2")
    assert h2.verdict == "refuted"
    assert "H2_sub080" in branch.time_split  # the assertion that breaks pre-fix
