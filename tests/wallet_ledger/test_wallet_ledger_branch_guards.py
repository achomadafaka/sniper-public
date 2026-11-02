"""Slice 5 Task 4 — branch-orchestrator wiring of the structure guard.

Verifies that:

* Legacy callers (no `replay_surface_meta` set, or set without a
  `ledger_source = "duckdb_warehouse"` entry) reach the leaves with no
  guard interference. Existing direct-call tests cover this; the test
  here only sanity-checks the gate on `run_held_branch` + `run_flipper_branch`.
* When the materialized marker is present, branch orchestrators stamp
  `verdict = "underpowered"` + `underpowered_reason = <structure reason>`
  on hypotheses whose structure class fails on the materialized rows.
* No leaf signature changes — every leaf keeps its current callable
  contract, so direct legacy invocations of `run_h*` / `run_fh*` still
  work bit-for-bit.
* When every active branch hypothesis is underpowered for a structure
  reason, `run_held_branch` still returns a well-formed `BranchReport`
  whose pool is empty.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from sniper.analysis.wallet_protocol.flipper import Flip, run_flipper_branch
from sniper.analysis.wallet_protocol.held_analyst import (
    H1_MIN_N,
    H2_MIN_N,
    H3_MIN_N,
    run_h1_net_pnl,
    run_h2_excess_wr,
    run_held_branch,
)
from sniper.analysis.wallet_protocol.types import WalletData


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_MATERIALIZED_META: dict[str, Any] = {
    "events": {
        "ledger_source": "duckdb_warehouse",
        "wallet_path": "/tmp/test/wallet.duckdb",
        "window_id": "test-window-v1",
    }
}


def _position(
    idx: int,
    *,
    win: bool = True,
    market_structure: str | None = "binary",
    outcome_index: int | None = 0,
    token_id_present: bool = True,
) -> dict:
    """One closed-resolved position, optionally Slice-5-enriched."""
    row: dict[str, Any] = {
        "conditionId": f"cid-{idx}",
        "asset": f"asset-{idx}",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "avgPrice": 0.40,
        "size": 100,
        "totalBought": 40.0,
        "realizedPnl": 60.0 if win else -40.0,
        "curPrice": 1.0 if win else 0.0,
        "timestamp": 1_700_000_000 + idx,
    }
    # Slice 5 enrichment — applied only by the materializer in production.
    if market_structure is not None:
        row["market_structure"] = market_structure
    row["outcome_index"] = outcome_index
    if token_id_present:
        row["token_id"] = f"asset-{idx}"
    else:
        row["token_id"] = None
    return row


def _wallet_data(
    closed: list[dict],
    *,
    materialized: bool,
    activity_rows: list[dict] | None = None,
) -> WalletData:
    return WalletData(
        wallet="0xtestwallet0000000000000000000000000000",
        closed_positions=closed,
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
        replay_surface_meta=_MATERIALIZED_META if materialized else None,
    )


def _hypothesis(branch, name: str):
    return next((h for h in branch.hypotheses if h.name == name), None)


# Light bootstrap iters to keep the suite fast.
_FAST_N_ITER = 1_000


# ---------------------------------------------------------------------------
# Direct legacy leaf tests — no signature changes (regression)
# ---------------------------------------------------------------------------


def test_direct_run_h1_net_pnl_unchanged_signature() -> None:
    closed = [_position(i, win=(i % 2 == 0)) for i in range(5)]
    h1 = run_h1_net_pnl(closed, n_iter=_FAST_N_ITER, seed=42)
    assert h1.name == "H1"
    assert h1.verdict in ("supported", "refuted", "inconclusive", "underpowered")


def test_direct_run_h2_excess_wr_unchanged_signature() -> None:
    closed = [_position(i, win=(i % 2 == 0)) for i in range(H2_MIN_N + 5)]
    h2 = run_h2_excess_wr(closed, n_iter=_FAST_N_ITER, seed=42)
    assert h2.name == "H2"
    assert h2.verdict in ("supported", "refuted", "inconclusive", "underpowered")


# ---------------------------------------------------------------------------
# Held branch — unmarked WalletData runs unguarded
# ---------------------------------------------------------------------------


def test_unmarked_held_branch_does_not_apply_structure_guard() -> None:
    """Closed positions are non-binary but no replay_surface_meta is set.
    The guard MUST NOT fire — H2 should not carry a structure reason."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="scalar", outcome_index=None)
        for i in range(H3_MIN_N + 5)
    ]
    data = _wallet_data(closed, materialized=False)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    h2 = _hypothesis(branch, "H2")
    h3 = _hypothesis(branch, "H3")
    assert h2 is not None and h3 is not None
    structure_reasons = {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    assert h2.underpowered_reason not in structure_reasons
    assert h3.underpowered_reason not in structure_reasons


def test_replay_meta_without_ledger_source_does_not_trigger_guard() -> None:
    """v2 replay fixtures populate replay_surface_meta but with a different
    schema; guard must only fire when an entry has
    `ledger_source = "duckdb_warehouse"`."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="scalar", outcome_index=None)
        for i in range(H3_MIN_N + 5)
    ]
    data = WalletData(
        wallet="0xtestwallet0000000000000000000000000000",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
        replay_surface_meta={"v2_fixture": {"version": "v2"}},
    )

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    h2 = _hypothesis(branch, "H2")
    assert h2 is not None
    assert h2.underpowered_reason != "market_structure_unsupported"


# ---------------------------------------------------------------------------
# Held branch — materialized non-binary trips the guard
# ---------------------------------------------------------------------------


def test_materialized_non_binary_rows_underpower_binary_hypotheses() -> None:
    """All closed positions are non-binary (e.g. scalar outcomes). The
    Slice 5 marker is set. H2, H2_sub080, H3 must come back underpowered
    with `underpowered_reason="market_structure_unsupported"`."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="scalar", outcome_index=None)
        for i in range(H3_MIN_N + 5)
    ]
    data = _wallet_data(closed, materialized=True)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    for name in ("H2", "H2_sub080", "H3"):
        h = _hypothesis(branch, name)
        assert h is not None, f"{name} missing from branch report"
        assert h.verdict == "underpowered", (
            f"{name} expected underpowered, got {h.verdict}"
        )
        assert h.underpowered_reason == "market_structure_unsupported", (
            f"{name} expected market_structure_unsupported reason, "
            f"got {h.underpowered_reason}"
        )


def test_materialized_outcome_index_missing_uses_specific_reason() -> None:
    """Closed positions are flagged binary but outcome_index is None on at
    least one row. The guard surfaces `outcome_index_missing`, not
    `market_structure_unsupported`."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="binary",
                  outcome_index=None)
        for i in range(H3_MIN_N + 5)
    ]
    data = _wallet_data(closed, materialized=True)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    for name in ("H2", "H2_sub080", "H3"):
        h = _hypothesis(branch, name)
        assert h is not None
        assert h.verdict == "underpowered"
        assert h.underpowered_reason == "outcome_index_missing"


def test_materialized_with_clean_binary_rows_runs_normally() -> None:
    """Clean binary rows; the guard returns supported and the leaf's
    underpowered_reason (if any) is preserved as something other than the
    structure reasons. H2 may still be underpowered for n/coverage reasons,
    but never for structure reasons."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="binary",
                  outcome_index=0)
        for i in range(H3_MIN_N + 5)
    ]
    data = _wallet_data(closed, materialized=True)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    structure_reasons = {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    for name in ("H1", "H2", "H2_sub080", "H3"):
        h = _hypothesis(branch, name)
        assert h is not None
        assert h.underpowered_reason not in structure_reasons, (
            f"{name} unexpectedly has structure reason {h.underpowered_reason}"
        )


def test_materialized_pre_underpowered_leaf_keeps_existing_reason() -> None:
    """Don't overwrite an already-underpowered hypothesis's reason. If H1
    was underpowered for `n_below_threshold` (or any non-structure reason),
    the guard must leave it alone."""
    closed: list[dict] = []  # n=0 → H1 trips n_below_threshold
    data = _wallet_data(closed, materialized=True)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    h1 = _hypothesis(branch, "H1")
    assert h1 is not None
    assert h1.verdict == "underpowered"
    assert h1.underpowered_reason == "n_below_threshold"


# ---------------------------------------------------------------------------
# Whole-wallet poisoning rule
# ---------------------------------------------------------------------------


def test_whole_wallet_poisoning_returns_well_formed_branch_report() -> None:
    """Every guarded hypothesis trips the structure guard. The branch
    report still constructs cleanly (no exceptions, no lost surfaces).
    Pool is empty so no `supported` verdicts emerge."""
    closed = [
        _position(i, win=(i % 2 == 0), market_structure="scalar", outcome_index=None)
        for i in range(H3_MIN_N + 5)
    ]
    data = _wallet_data(closed, materialized=True)

    branch = run_held_branch(data, n_iter=_FAST_N_ITER, seed=42)

    assert branch is not None
    assert any(h.name == "H2" for h in branch.hypotheses)

    # No hypothesis should have been promoted to 'supported' under structure
    # poisoning — at minimum H2/H2_sub/H3 must be underpowered for structure.
    for name in ("H2", "H2_sub080", "H3"):
        h = _hypothesis(branch, name)
        assert h is not None
        assert h.verdict == "underpowered"


# ---------------------------------------------------------------------------
# Flipper branch — token_price_only set
# ---------------------------------------------------------------------------


def _flip(idx: int, *, asset_id: str = "") -> Flip:
    return Flip(
        condition_id=f"cid-{idx}",
        outcome="Yes",
        buy_ts=1_700_000_000 + idx * 100,
        sell_ts=1_700_000_500 + idx * 100,
        buy_price=0.40,
        sell_price=0.60,
        size=10.0,
        buy_notional=4.0,
        flip_roi=0.50,
        hold_duration_seconds=500,
        asset_id=asset_id,
    )


def test_unmarked_flipper_branch_does_not_apply_structure_guard() -> None:
    flips = [_flip(i, asset_id="") for i in range(25)]  # all asset_ids missing
    branch = run_flipper_branch(flips, n_iter=_FAST_N_ITER, seed=42)

    structure_reasons = {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    for h in branch.hypotheses:
        assert h.underpowered_reason not in structure_reasons


def test_materialized_flipper_no_token_history_underpowers_token_hypotheses() -> None:
    """Flips with empty `asset_id` plus the Slice 5 marker force every
    token-price-only flipper hypothesis whose leaf otherwise would have
    run to `no_token_history`. FH5 (structure_tolerant) is unaffected.

    Fixture is sized so FH1/FH2 clear `FH1_MIN_N=30` and the structure
    guard is the binding underpowered reason. FH3/FH6 stay underpowered
    via their own legitimate non-structure reasons (no execution_profile,
    no price_history_fn) — those are unaffected by the guard, as the
    contract requires.
    """
    flips = [_flip(i, asset_id="") for i in range(35)]
    branch = run_flipper_branch(
        flips,
        n_iter=_FAST_N_ITER,
        seed=42,
        materialized_meta=_MATERIALIZED_META,
    )

    # FH1 / FH2 — leaf runs (n=35 >= 30) so the guard is the binding
    # underpowered reason.
    for name in ("FH1", "FH2"):
        h = _hypothesis(branch, name)
        assert h is not None
        assert h.verdict == "underpowered", (
            f"{name} expected underpowered, got {h.verdict}"
        )
        assert h.underpowered_reason == "no_token_history", (
            f"{name} expected no_token_history reason, "
            f"got {h.underpowered_reason}"
        )

    # FH3 / FH6 — already underpowered for legitimate non-structure reasons
    # (no execution_profile / no price_history_fn). The guard MUST NOT
    # overwrite those reasons.
    fh3 = _hypothesis(branch, "FH3")
    fh6 = _hypothesis(branch, "FH6")
    assert fh3 is not None and fh6 is not None
    assert fh3.underpowered_reason == "no_execution_profile"
    assert fh6.underpowered_reason == "no_price_history_fn"


def test_materialized_flipper_with_token_ids_runs_normally() -> None:
    flips = [_flip(i, asset_id=f"asset-{i}") for i in range(25)]
    branch = run_flipper_branch(
        flips,
        n_iter=_FAST_N_ITER,
        seed=42,
        materialized_meta=_MATERIALIZED_META,
    )

    structure_reasons = {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    for h in branch.hypotheses:
        assert h.underpowered_reason not in structure_reasons


def test_unmarked_flipper_branch_signature_unchanged_when_meta_none() -> None:
    """Calling run_flipper_branch without materialized_meta keeps the
    legacy signature — the kwarg is optional and defaults preserve current
    behaviour."""
    flips = [_flip(i, asset_id=f"asset-{i}") for i in range(25)]
    branch = run_flipper_branch(flips, n_iter=_FAST_N_ITER, seed=42)
    assert any(h.name == "FH1" for h in branch.hypotheses)
