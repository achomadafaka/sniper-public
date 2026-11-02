"""§6 — Held-analyst branch (H1–H7).

Hypotheses run on the wallet's closed-positions data. The unit of observation
is one closed position. H7 (CUSUM stationarity) is a sample-selection pre-pass:
when it signals, downstream H1–H6 run on the post-regime subset only (when
n_post >= 20). H7 is NOT in the BH pool (§9.2).

Data dependencies that may be absent:
- H4 needs `execution_profile.drift_6h` → underpowered when missing.
- H5 needs an injected
  `peer_baseline_fn(grain, tag, wallet) -> (baseline_wr, peer_count)`
  → no inference when missing (returns inference_not_run). The `wallet`
  argument is the analyzed wallet itself; concrete implementations
  (e.g. `db.peer_baseline_for_grain`) MUST exclude it from the peer set
  to avoid self-contamination of the baseline.
- H6 needs a `price_history_fn(condition_id, sell_ts) -> price_24h_later`
  → underpowered when missing.

These are dependency-injected so the branch is testable without a live DB.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sniper.analysis.wallet_protocol.h5_tag_edge import (
    PEER_COUNT_INFERENCE_FLOOR,
    PEER_COUNT_OK_THRESHOLD,
    classify_peer_confidence,
)
from sniper.analysis.wallet_protocol.independence import (
    cluster_by_event_or_news_window,
    cluster_by_eventslug,
)
from sniper.analysis.wallet_protocol.protocol_guards import (
    market_structure_supported,
)
from sniper.analysis.wallet_protocol.statistics import (
    BH_FDR_Q,
    DEFAULT_BOOTSTRAP_ITERS,
    TIME_SPLIT_MIN_N,
    bootstrap_ci,
    cluster_bootstrap_ci,
    cusum_signal,
    permutation_test_spearman,
    pool_and_correct,
    decision_rule_majority_supported,
    time_split_70_30,
    time_split_kfold,
    robust_location_bootstrap_ci,
    winsorized_bootstrap_ci,
)
from sniper.analysis.wallet_protocol.tag_inference import (
    TagGrainEnumeration,
    enumerate_tag_grains,
)
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    IndependenceCheckResult,
    StationarityResult,
    TagGrainResult,
    TimeSplitResult,
    UnderpoweredReason,
    VerdictLabel,
    WalletData,
)


# Per-hypothesis min_n thresholds (spec §6).
H1_MIN_N = 1
H2_MIN_N = 20
H3_MIN_N = 30
H5_MIN_N_PER_GRAIN = 10
H6_MIN_N = 20
H6_POST_SELL_OFFSET_SECONDS = 24 * 3600

EPSILON_RESOLUTION = 0.01           # §6.1 H2: curPrice within ε of {0, 1}
SUB_080_ENTRY_FILTER = 0.80         # ADR-002

PeerBaselineFn = Callable[[str, str, str], tuple[float, int] | None]
PriceHistoryFn = Callable[[str, int], float | None]


# ---------------------------------------------------------------------------
# Held-to-resolution filtering
# ---------------------------------------------------------------------------


def _has_pre_resolution_sell(
    condition_id: str,
    outcome: str,
    activity_rows: Sequence[Mapping],
) -> bool:
    """True if there's a SELL event in `activity_rows` for this (cid, outcome).

    Per spec §4.7 + §6.1 H2: `/activity?type=TRADE` excludes redemption events,
    so any SELL is necessarily pre-resolution.
    """
    for row in activity_rows:
        if (row.get("side") or "").upper() != "SELL":
            continue
        if row.get("conditionId") != condition_id:
            continue
        if (row.get("outcome") or "") != outcome:
            continue
        return True
    return False


def filter_held_to_resolution(data: WalletData) -> list[dict]:
    """Apply the §6.1 H2 locked definition.

    A position is "held to resolution" iff:
        curPrice ∈ {0, 1} (within ε=0.01)
      AND
        no SELL event exists in /activity for this (conditionId, outcome)

    Closed positions whose curPrice falls in (ε, 1-ε) are still "closed" in
    the API sense (resolved markets) but were not held — wallet exited before
    resolution. Those go to the flipper branch, not held-analyst.
    """
    held: list[dict] = []
    for p in data.closed_positions:
        cp = p.get("curPrice")
        if cp is None:
            continue
        cp_f = float(cp)
        if not (cp_f <= EPSILON_RESOLUTION or cp_f >= 1.0 - EPSILON_RESOLUTION):
            continue
        cid = p.get("conditionId")
        outcome = p.get("outcome") or ""
        if not cid:
            continue
        if _has_pre_resolution_sell(cid, outcome, data.activity_rows):
            continue
        held.append(p)
    return held


def _is_win(position: Mapping) -> bool:
    """A held position 'wins' iff its held-token redeemed at $1.

    Per ADR-003 + R2 empirical resolution (commit cba241e):
    closed_positions[].curPrice is the held-token price uniformly.
    A YES position wins iff the YES token redeemed near $1; a NO position
    wins iff the NO token redeemed near $1; a categorical outcome wins iff
    its outcome token redeemed near $1. The same threshold (cp >= 1−ε)
    applies to every outcome label.

    Note: live spot-check (R2 §8) found ~11% of NO-side rows where
    realizedPnl sign and curPrice disagree; these are pre-resolution-sell
    artifacts and are already excluded by `filter_held_to_resolution`'s
    `_has_pre_resolution_sell` gate before reaching this function.
    """
    cp = float(position.get("curPrice") or 0.0)
    return cp >= 1.0 - EPSILON_RESOLUTION


def _entry_price(position: Mapping) -> float:
    return float(position.get("avgPrice") or 0.0)


def _realized_pnl(position: Mapping) -> float:
    return float(position.get("realizedPnl") or 0.0)


def _closed_position_cost(position: Mapping) -> float:
    """Cost basis for a /closed-positions row (I1, M7).

    Prefers ``avgPrice * size`` when ``size`` is present (synthetic test data
    or /positions payloads). Falls back to ``totalBought`` when ``size`` is
    absent — Polymarket's ``/closed-positions`` endpoint omits ``size`` but
    always carries ``totalBought``.

    ADR-004's warning against ``totalBought`` is OPEN-position-specific: re-
    buys after partial sells inflate ``totalBought`` while ``size`` shrinks,
    breaking the snapshot cost-basis identity. For CLOSED positions every
    buy and every sell has settled, so ``realizedPnl / totalBought`` is a
    coherent ROI on gross dollars deployed. For held-to-resolution closed
    positions (no sells) ``totalBought == avgPrice * size`` exactly. For
    early-exit closed positions ``totalBought`` overstates the snapshot
    entry cost basis, which conservatively dampens DW ROI — surfaced
    rather than fabricated.

    Also used by H3 (size-conviction) as the cost-basis proxy. H3 only
    needs *variance across positions*, not exact open-snapshot cost basis,
    so the early-exit overstatement does not bias the Spearman rank
    correlation against itself; using ``totalBought`` is preferable to
    a uniform zero, which mechanically forced ρ=0/p=1.0 (M7).
    """
    size = float(position.get("size") or 0.0)
    if size > 0:
        return _entry_price(position) * size
    return float(position.get("totalBought") or 0.0)


def _entry_notional(position: Mapping) -> float:
    """Conviction signal for H3 (size-vs-win Spearman).

    Delegates to :func:`_closed_position_cost` so the open-/closed-position
    cost-basis fall-back lives in one place. See ADR-004 + M7 docstring on
    ``_closed_position_cost`` for the ``totalBought`` rationale.
    """
    return _closed_position_cost(position)


# ---------------------------------------------------------------------------
# Capital efficiency — dollar-weighted vs equal-weighted ROI (I1)
# ---------------------------------------------------------------------------


def compute_dw_ew_roi(
    closed_positions: Sequence[Mapping],
    *,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
) -> tuple[float, float, float, float, int] | None:
    """Compute dollar-weighted and equal-weighted ROI over closed positions.

    Definitions (per I1 + ADR-004):
        cost_i = avgPrice_i * size_i  (true cost basis)
        DW ROI = sum(realized_pnl_i) / sum(cost_i)
        EW ROI = mean(realized_pnl_i / cost_i)  per-position

    Bootstrap CI is on the DW ratio. Standard ratio bootstrap: resample
    (pnl_i, cost_i) PAIRS with replacement, recompute sum(pnl)/sum(cost)
    each iteration. Resampling pairs (rather than holding the denominator
    fixed) is the only correct way to surface ratio uncertainty.

    Positions with cost <= 0 are dropped. Returns None when no positions
    have a valid cost basis (caller should leave report fields as None).

    Returns:
        (dw_point, dw_ci_lower, dw_ci_upper, ew_point, n_positions)
        or None when computation is not possible.
    """
    pairs: list[tuple[float, float]] = []
    for p in closed_positions:
        cost = _closed_position_cost(p)
        if cost <= 0:
            continue
        pnl = _realized_pnl(p)
        pairs.append((pnl, cost))

    n = len(pairs)
    if n == 0:
        return None

    pnls = np.asarray([pn for pn, _ in pairs], dtype=float)
    costs = np.asarray([co for _, co in pairs], dtype=float)
    total_cost = float(costs.sum())
    if total_cost <= 0:
        return None

    dw_point = float(pnls.sum() / total_cost)
    ew_point = float(np.mean(pnls / costs))

    if n == 1:
        # Degenerate — CI collapses to point estimate.
        return dw_point, dw_point, dw_point, ew_point, n

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_iter, n))
    pnls_resample = pnls[idx]
    costs_resample = costs[idx]
    sum_pnls = pnls_resample.sum(axis=1)
    sum_costs = costs_resample.sum(axis=1)
    # Guard: a resample where every drawn cost is zero would NaN-out;
    # we already filtered cost<=0 so sum_costs>0 by construction (n>=1
    # always has at least one positive cost in the resample).
    ratios = sum_pnls / sum_costs
    lo = float(np.quantile(ratios, 0.025))
    hi = float(np.quantile(ratios, 0.975))
    return dw_point, lo, hi, ew_point, n


# ---------------------------------------------------------------------------
# H7 — CUSUM stationarity pre-pass
# ---------------------------------------------------------------------------


def run_h7_stationarity(
    closed_positions: Sequence[Mapping],
) -> StationarityResult:
    """Run upper-side CUSUM on chronological per-position P&L.

    Spec §9.1: h=25.0, k=0.5σ, calibrated for heavy-tail PnL. Detects abrupt
    upward shifts in cumulative deviation from in-sample mean — i.e., the
    wallet's PnL distribution materially changed.

    For "did the edge disappear" detection (downward shift), callers should
    re-run with `direction="lower"` and accept the uncalibrated false-alarm
    flag. The pre-pass here uses the spec-default upper detector.
    """
    sorted_positions = sorted(
        closed_positions,
        key=lambda p: int(p.get("timestamp") or 0),
    )
    series = [_realized_pnl(p) for p in sorted_positions]
    if len(series) < 2:
        return StationarityResult(
            cusum_status="calibrated", h_used=25.0, k=0.0,
            signaled=False, signal_index=None,
            pre_regime_n=0, post_regime_n=len(series),
            inference_basis="full_history",
        )
    arr = np.asarray(series, dtype=float)
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1)) if len(arr) >= 2 else 0.0

    cusum = cusum_signal(series, mu=mu, sigma=sigma)  # default h=25, direction="upper"

    if not cusum.signaled:
        return StationarityResult(
            cusum_status=cusum.cusum_status, h_used=cusum.h_used, k=cusum.k,
            signaled=False, signal_index=None,
            pre_regime_n=0, post_regime_n=len(series),
            inference_basis="full_history",
        )

    sig_idx = cusum.signal_index or 0
    pre_n = sig_idx
    post_n = len(series) - sig_idx
    basis: str = "post_regime_only" if post_n >= 20 else "insufficient_post_regime"
    return StationarityResult(
        cusum_status=cusum.cusum_status, h_used=cusum.h_used, k=cusum.k,
        signaled=True, signal_index=sig_idx,
        pre_regime_n=pre_n, post_regime_n=post_n,
        inference_basis=basis,
    )


def select_post_regime_subset(
    closed_positions: Sequence[Mapping],
    stationarity: StationarityResult,
) -> list[dict]:
    """Slice closed_positions to post-regime when CUSUM signaled and n_post >= 20."""
    if not stationarity.signaled or stationarity.signal_index is None:
        return list(closed_positions)
    if stationarity.inference_basis != "post_regime_only":
        # Insufficient post-regime sample → caller should mark hypotheses underpowered
        return list(closed_positions)
    sorted_positions = sorted(
        closed_positions,
        key=lambda p: int(p.get("timestamp") or 0),
    )
    return list(sorted_positions[stationarity.signal_index:])


# ---------------------------------------------------------------------------
# Slice 5 — materialized-mode structure guard (no leaf signature changes)
# ---------------------------------------------------------------------------


_LEDGER_MATERIALIZATION_TAG = "duckdb_warehouse"


def _is_materialized_from_ledger(meta: Any) -> bool:
    """Return True iff `meta` was produced by the Slice 5 materializer.

    The materializer stamps every replay-surface entry with
    `ledger_source = "duckdb_warehouse"`. Other producers of
    `replay_surface_meta` (v2 replay fixtures, future surfaces) MUST NOT
    set that key, so the guard only fires on the materialized path.
    """
    if not isinstance(meta, Mapping):
        return False
    for entry in meta.values():
        if (
            isinstance(entry, Mapping)
            and entry.get("ledger_source") == _LEDGER_MATERIALIZATION_TAG
        ):
            return True
    return False


def _build_held_guard_rows(data: WalletData) -> list[Mapping]:
    """Combine closed positions and activity rows into the guard's input
    list. Both surfaces carry ``market_structure``, ``outcome_index``, and
    ``token_id`` when supplied by the materializer."""
    rows: list[Mapping] = []
    rows.extend(data.closed_positions)
    rows.extend(data.activity_rows)
    return rows


def _stamp_structure_underpowered(
    h: HypothesisResult, reason: UnderpoweredReason
) -> None:
    """Mark `h` as underpowered for a structure reason without losing prior
    diagnostic context. Existing notes are preserved; the BH pool's
    underpowered-exclusion paths drop the result naturally."""
    h.verdict = "underpowered"
    h.underpowered_reason = reason
    h.notes.append(f"market_structure_guard:{reason}")


def _apply_held_structure_guards(
    held_results: Sequence[tuple[HypothesisResult, str]],
    grain_results: Sequence[TagGrainResult],
    data: WalletData,
) -> None:
    """Apply the Slice 5 market-structure guard to held-branch results when
    `data` was produced by the ledger materializer. Legacy callers (no
    `replay_surface_meta`, or meta without a `ledger_source` tag) are
    unaffected.

    Already-underpowered hypotheses keep their existing reason — the guard
    only stamps results whose leaves succeeded and might otherwise enter
    the BH pool with mismatched structure.
    """
    if not _is_materialized_from_ledger(data.replay_surface_meta):
        return

    rows = _build_held_guard_rows(data)
    for h, hid in held_results:
        if h.verdict == "underpowered":
            continue
        supported, reason = market_structure_supported(hid, rows)
        if not supported and reason is not None:
            _stamp_structure_underpowered(h, reason)
    for g in grain_results:
        if g.verdict == "underpowered":
            continue
        supported, reason = market_structure_supported("H5", rows)
        if not supported and reason is not None:
            g.verdict = "underpowered"
            g.underpowered_reason = reason
            # Drop the stashed raw p so the BH pool excludes it.
            if hasattr(g, "_p_raw"):
                setattr(g, "_p_raw", None)


# ---------------------------------------------------------------------------
# H1 — Net P&L positive (gate hypothesis)
# ---------------------------------------------------------------------------


def run_h1_net_pnl(
    closed_positions: Sequence[Mapping],
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
    top3_indices: list[int] | None = None,
    winsorize_clip_frac: float = 0.10,
) -> HypothesisResult:
    """H1 with winsorized bootstrap as the verdict driver (v3 amendment A).

    Args:
        top3_indices: indices into `closed_positions` to EXCLUDE for the
            optional H1_sub_top3 diagnostic (v3 amendment B). When provided
            and non-empty, runs a second bootstrap on the excluded subset
            and stashes the result in `details["sub_top3"]` (no separate
            verdict; operator-facing diagnostic only).
        winsorize_clip_frac: top+bottom clip fraction for the robust
            location bootstrap. 0.10 default per Phase-1 M2 closure.

    Verdict driver: winsorized bootstrap on per-position P&L (mean). Raw
    sum CI is preserved in `details` for transparency.
    """
    pnls = [_realized_pnl(p) for p in closed_positions]
    n = len(pnls)
    if n < H1_MIN_N:
        return HypothesisResult(
            name="H1", verdict="underpowered", n=n,
            notes=[f"n_closed={n} < H1_MIN_N={H1_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    # Winsorized bootstrap on per-position mean (verdict driver in v3).
    # null_value=0.0 → empirical two-sided p computed on the SAME resample
    # distribution that produced (lo, hi). This replaces the legacy
    # 0.05/0.5 CI-sign approximation per M1 of the bulletproofing spec.
    win_point, win_lo, win_hi, p_h1, location_details = robust_location_bootstrap_ci(
        pnls, clip_frac=winsorize_clip_frac,
        n_iter=n_iter, seed=seed,
        null_value=0.0,
    )
    # Spec §6.1 H1: refuted iff CI lower bound <= 0
    verdict: VerdictLabel
    if win_lo > 0:
        verdict = "supported"
    elif win_hi < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"

    # Preserve raw sum for transparency
    raw_pnl_sum = float(np.asarray(pnls, dtype=float).sum())
    raw_point, raw_lo, raw_hi = bootstrap_ci(
        pnls, statistic_fn=lambda a: float(a.sum()),
        n_iter=n_iter, seed=seed,
    )
    details: dict[str, Any] = {
        "bootstrap_iters": n_iter,
        "winsorize_clip_frac": winsorize_clip_frac,
        "tested_location_statistic": location_details["tested_location_statistic"],
        "winsorized_point": win_point,
        "winsorized_ci": [win_lo, win_hi],
        "raw_pnl_sum": raw_pnl_sum,
        "raw_sum_ci": [raw_lo, raw_hi],
    }

    # H1_sub_top3 diagnostic (v3 amendment B). Conditional: only when caller
    # passes top3_indices (i.e., F2 fired). Excluded subset gets its OWN
    # winsorized bootstrap; result is a diagnostic, NOT in the BH pool.
    if top3_indices:
        excl = set(int(i) for i in top3_indices)
        sub_pnls = [p for i, p in enumerate(pnls) if i not in excl]
        if len(sub_pnls) >= H1_MIN_N:
            sp_point, sp_lo, sp_hi = winsorized_bootstrap_ci(
                sub_pnls, clip_frac=winsorize_clip_frac,
                statistic_fn=lambda a: float(a.mean()),
                n_iter=n_iter, seed=seed,
            )
            details["sub_top3"] = {
                "n": len(sub_pnls),
                "point": sp_point,
                "ci_lower": sp_lo,
                "ci_upper": sp_hi,
            }
        else:
            details["sub_top3"] = {
                "n": len(sub_pnls),
                "skipped": "underpowered_after_exclusion",
            }

    return HypothesisResult(
        name="H1", verdict=verdict, n=n,
        test_statistic=win_point, ci_lower=win_lo, ci_upper=win_hi,
        p_value_raw=p_h1,
        details=details,
    )


# ---------------------------------------------------------------------------
# H2 — Held WR exceeds entry-price-implied
# ---------------------------------------------------------------------------


def run_h2_excess_wr(
    held_positions: Sequence[Mapping],
    sub_080_only: bool = False,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    """Excess WR over mean entry price (implied probability).

    Spec §3.8 v1 says "Bootstrap: shuffle win/loss labels stratified by entry-
    price decile" — that's the null-distribution construction. We approximate
    via a simpler bootstrap on the per-position contribution `(is_win -
    avgPrice)`, whose mean is the test statistic. CI on that mean against zero
    is equivalent to the stratified procedure under the null of independence
    between win/loss and entry-price decile.
    """
    if sub_080_only:
        positions = [p for p in held_positions if _entry_price(p) < SUB_080_ENTRY_FILTER]
    else:
        positions = list(held_positions)
    n = len(positions)
    if n < H2_MIN_N:
        return HypothesisResult(
            name=("H2_sub080" if sub_080_only else "H2"),
            verdict="underpowered", n=n,
            notes=[f"n_held={n} < H2_MIN_N={H2_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    excess_per_position = [
        (1.0 if _is_win(p) else 0.0) - _entry_price(p)
        for p in positions
    ]
    point, lo, hi, p_value = bootstrap_ci(
        excess_per_position, n_iter=n_iter, seed=seed,
        null_value=0.0,
    )
    verdict: VerdictLabel
    if lo > 0:
        verdict = "supported"
    elif hi < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name=("H2_sub080" if sub_080_only else "H2"),
        verdict=verdict, n=n,
        test_statistic=point, ci_lower=lo, ci_upper=hi,
        p_value_raw=p_value,
        details={"bootstrap_iters": n_iter, "sub_080_only": sub_080_only},
    )


# ---------------------------------------------------------------------------
# H3 — Conviction signal (size vs. win)
# ---------------------------------------------------------------------------


def run_h3_size_conviction(
    held_positions: Sequence[Mapping],
    archetype_layer1: str | None = None,
    n_iter: int = 10_000,
    seed: int = 42,
) -> HypothesisResult:
    n = len(held_positions)
    # Spec §6.1 H3: skip for hedge archetypes.
    if archetype_layer1 in ("two_sided_inventory_trader", "paired_outcome_arb"):
        return HypothesisResult(
            name="H3", verdict="n/a", n=n,
            notes=[f"skipped: hedge archetype {archetype_layer1!r}"],
        )
    if n < H3_MIN_N:
        return HypothesisResult(
            name="H3", verdict="underpowered", n=n,
            notes=[f"n_held={n} < H3_MIN_N={H3_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    notionals = [_entry_notional(p) for p in held_positions]
    wins = [1.0 if _is_win(p) else 0.0 for p in held_positions]
    rho, p_value = permutation_test_spearman(
        notionals, wins, n_iter=n_iter, seed=seed,
    )
    # Spec falsification: p > 0.10 → no conviction signal
    verdict: VerdictLabel
    if p_value <= 0.10 and rho > 0:
        verdict = "supported"
    elif p_value <= 0.10 and rho < 0:
        verdict = "refuted"  # negative correlation: bigger positions LOSE more
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="H3", verdict=verdict, n=n,
        test_statistic=rho, p_value_raw=p_value,
        details={"permutation_iters": n_iter, "test": "spearman_rho"},
    )


# ---------------------------------------------------------------------------
# H4 — Entry-timing edge (drift_6h)
# ---------------------------------------------------------------------------


def run_h4_entry_timing(
    execution_profile: Mapping | None,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    """Spec §3 H4: mean(drift_6h) > 0 across qualifying fills.

    Falls back to underpowered when execution_profile is missing or unpopulated.
    """
    if not execution_profile:
        return HypothesisResult(
            name="H4", verdict="underpowered", n=0,
            notes=["no execution_profile provided"],
            underpowered_reason="no_execution_profile",
        )
    status = execution_profile.get("status")
    if status != "populated":
        return HypothesisResult(
            name="H4", verdict="underpowered", n=0,
            notes=[f"execution_profile.status={status!r}"],
            underpowered_reason="no_execution_profile",
        )
    sample_tier = execution_profile.get("sample_tier")
    if sample_tier == "unknown":
        return HypothesisResult(
            name="H4", verdict="underpowered", n=0,
            notes=["execution_profile.sample_tier=unknown"],
            underpowered_reason="no_execution_profile",
        )
    drifts: list[float] = list(execution_profile.get("drifts_6h") or [])
    n = len(drifts)
    if n < 5:  # arbitrary tiny floor; v1 doesn't lock a number
        return HypothesisResult(
            name="H4", verdict="underpowered", n=n,
            notes=["execution_profile drifts_6h sample too small (<5)"],
            underpowered_reason="n_below_threshold",
        )

    # M6 closure: cluster-adjusted bootstrap by condition_id when the
    # envelope provides aligned cluster ids. Wallets that ladder many fills
    # into few markets (eatpraylove: 73/100 fills at 2 distinct drifts) make
    # per-fill bootstrap CIs ~20x too narrow. Fall back to fill-level
    # bootstrap when clusters are absent (back-compat for synthetic test
    # envelopes and older callers).
    clusters_raw = execution_profile.get("drifts_6h_clusters")
    use_clusters = (
        isinstance(clusters_raw, (list, tuple))
        and len(clusters_raw) == n
    )
    n_distinct_clusters = len(set(clusters_raw)) if use_clusters else None

    if use_clusters:
        point, lo, hi, p_value = cluster_bootstrap_ci(
            drifts, list(clusters_raw),
            n_iter=n_iter, seed=seed, null_value=0.0,
        )
        bootstrap_method = "cluster"
    else:
        point, lo, hi, p_value = bootstrap_ci(
            drifts, n_iter=n_iter, seed=seed, null_value=0.0,
        )
        bootstrap_method = "fill"

    verdict: VerdictLabel
    if lo > 0:
        verdict = "supported"
    elif hi < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"

    details: dict[str, Any] = {
        "bootstrap_iters": n_iter,
        "bootstrap_method": bootstrap_method,
    }
    if n_distinct_clusters is not None:
        details["n_distinct_clusters"] = n_distinct_clusters

    return HypothesisResult(
        name="H4", verdict=verdict, n=n,
        test_statistic=point, ci_lower=lo, ci_upper=hi,
        p_value_raw=p_value,
        details=details,
    )


# ---------------------------------------------------------------------------
# H5 — Tag-level edge concentration (multi-grain)
# ---------------------------------------------------------------------------


def _build_h5_grain_results(
    held_positions: Sequence[Mapping],
    enumeration: TagGrainEnumeration,
    peer_baseline_fn: PeerBaselineFn | None,
    n_iter: int,
    seed: int,
    *,
    wallet: str = "",
) -> list[TagGrainResult]:
    """Per-grain test result PRE-BH (raw p-values populated, verdicts deferred).

    NOTE: superseded by `_build_one_grain` in the live `run_held_branch`
    pipeline. Kept for backward compatibility; signature kept in sync with
    the 3-arg `PeerBaselineFn` contract so an accidental caller still
    receives a self-excluded baseline.
    """
    results: list[TagGrainResult] = []
    if peer_baseline_fn is None:
        return results

    def _build_grain(grain: str, tag: str, indices: list[int]) -> TagGrainResult | None:
        n = len(indices)
        baseline_info = peer_baseline_fn(grain, tag, wallet)
        peer_count = baseline_info[1] if baseline_info else 0
        confidence = classify_peer_confidence(peer_count)
        if peer_count < PEER_COUNT_INFERENCE_FLOOR:
            return TagGrainResult(
                grain=grain, tag=tag, peer_count=peer_count,
                confidence_flag="low_population",
                n_at_grain=n, metric_value=None, pop_baseline=None,
                excess_pp=None, verdict="inference_not_run",
            )
        if n < H5_MIN_N_PER_GRAIN:
            return TagGrainResult(
                grain=grain, tag=tag, peer_count=peer_count,
                confidence_flag=confidence,
                n_at_grain=n, metric_value=None,
                pop_baseline=baseline_info[0] if baseline_info else None,
                excess_pp=None, verdict="descriptive_only",
            )
        wins = [1.0 if _is_win(held_positions[i]) else 0.0 for i in indices]
        wr = float(np.mean(wins))
        baseline_wr = baseline_info[0] if baseline_info else 0.0
        excess = (wr - baseline_wr) * 100.0
        # Bootstrap CI on (wr - baseline_wr)
        point, lo, hi = bootstrap_ci(
            [w - baseline_wr for w in wins], n_iter=n_iter, seed=seed,
        )
        # Simple p-value approximation: 2 * min(P(stat<=0), P(stat>=0))
        # using the bootstrap empirical distribution (re-derive via sign of CI).
        # We compute a quick raw p via tail fraction in the resample.
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(wins), size=(n_iter, len(wins)))
        wins_arr = np.asarray(wins, dtype=float)
        resamples = wins_arr[idx].mean(axis=1) - baseline_wr
        # Two-sided p
        observed = wr - baseline_wr
        p_raw = float(min(
            (resamples <= 0).mean() if observed > 0 else (resamples >= 0).mean(),
            0.5,
        )) * 2.0
        return TagGrainResult(
            grain=grain, tag=tag, peer_count=peer_count,
            confidence_flag=confidence,
            n_at_grain=n, metric_value=wr,
            pop_baseline=baseline_wr, excess_pp=excess,
            ci_lower=point + lo - point,  # lo is on (wr - baseline) scale already
            ci_upper=hi,
            p_value_bh_adjusted=None,  # filled in by caller after BH
            verdict="inference_not_run",  # placeholder; filled after BH
            details={"p_value_raw": p_raw},
        )

    # Note: TagGrainResult has no `details` field, so we stash p_raw on the object via attr
    for tag, indices in enumeration.leaf_grain_members.items():
        result = _build_grain("leaf", tag, indices)
        if result is not None:
            results.append(result)
    for nav, indices in enumeration.nav_grain_members.items():
        result = _build_grain("nav", nav, indices)
        if result is not None:
            results.append(result)
    return results


def _grain_raw_p(g: TagGrainResult) -> float | None:
    """Extract raw p-value attached to a TagGrainResult during construction."""
    return getattr(g, "_p_raw", None)


# ---------------------------------------------------------------------------
# H6 — Exit quality (placeholder when no price-history fn)
# ---------------------------------------------------------------------------


def run_h6_exit_quality(
    activity_rows: Sequence[Mapping],
    price_history_fn: PriceHistoryFn | None,
) -> HypothesisResult:
    if price_history_fn is None:
        return HypothesisResult(
            name="H6", verdict="underpowered", n=0,
            notes=["no price_history_fn provided"],
            underpowered_reason="no_price_history_fn",
        )
    sells = [r for r in activity_rows if (r.get("side") or "").upper() == "SELL"]
    n = len(sells)
    if n < H6_MIN_N:
        return HypothesisResult(
            name="H6", verdict="underpowered", n=n,
            notes=[f"n_sells={n} < H6_MIN_N={H6_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    correct = 0
    examined = 0
    for r in sells:
        # Polymarket /prices-history takes `market=<asset_id>` (the CLOB
        # token id). conditionId silently returns empty `history` — verified
        # 2026-04-26 against production. The activity row carries both
        # `asset` (token id) and `conditionId`; H6 needs the asset.
        market_id = r.get("asset")
        sell_ts = int(r.get("timestamp") or 0)
        sell_price = float(r.get("price") or 0.0)
        if not market_id or not sell_ts:
            continue
        # H6 compares SELL price to the market price 24h later. The
        # price_history_fn contract is raw timestamp lookup, so H6 owns
        # the offset explicitly.
        future = price_history_fn(
            market_id, sell_ts + H6_POST_SELL_OFFSET_SECONDS,
        )
        if future is None:
            continue
        examined += 1
        if sell_price > future:
            correct += 1
    coverage = examined / n if n else 0.0
    if examined < H6_MIN_N:
        return HypothesisResult(
            name="H6", verdict="underpowered", n=examined,
            notes=[f"price_history coverage {examined}/{n}"],
            details={"correct": correct, "examined": examined,
                     "n_sells": n, "coverage": coverage},
            underpowered_reason="n_below_threshold",
        )
    fraction = correct / examined
    # Binomial test against 0.5: simple normal approximation
    se = (0.5 * 0.5 / examined) ** 0.5
    z = (fraction - 0.5) / se if se > 0 else 0.0
    # Two-sided p via normal CDF approximation
    from scipy.stats import norm
    p_value = float(2 * (1 - norm.cdf(abs(z))))
    verdict: VerdictLabel
    if p_value <= 0.10 and fraction > 0.5:
        verdict = "supported"
    elif p_value <= 0.10 and fraction < 0.5:
        verdict = "refuted"
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="H6", verdict=verdict, n=examined,
        test_statistic=fraction, p_value_raw=p_value,
        details={"correct": correct, "examined": examined,
                 "n_sells": n, "coverage": coverage},
    )


# ---------------------------------------------------------------------------
# Verdict assembly + BH pooling
# ---------------------------------------------------------------------------


def _verdict_after_pool(
    base: VerdictLabel,
    rejected: bool,
    ci_lower: float | None,
    ci_upper: float | None,
) -> VerdictLabel:
    """Final verdict after BH correction.

    Spec: supported requires (BH-rejected) AND (CI excludes null in the right
    direction). If the raw test was already 'underpowered' or 'n/a', BH does
    not promote it.
    """
    if base in ("underpowered", "n/a", "inference_not_run", "descriptive_only"):
        return base
    # Permutation tests (H3) carry their direction in `base` and don't populate
    # CIs. Trust `base` when BH rejects; demote to inconclusive otherwise.
    # Without this, a refuted/supported permutation result fell through to the
    # CI-direction checks and silently became "inconclusive".
    if ci_lower is None and ci_upper is None and base in ("supported", "refuted"):
        return base if rejected else "inconclusive"
    if not rejected:
        # BH pool deemed not significant
        if ci_upper is not None and ci_upper < 0:
            return "refuted"
        return "inconclusive"
    # rejected
    if ci_lower is not None and ci_lower > 0:
        return "supported"
    if ci_upper is not None and ci_upper < 0:
        return "refuted"
    return "inconclusive"


def run_held_branch(
    data: WalletData,
    *,
    peer_baseline_fn: PeerBaselineFn | None = None,
    price_history_fn: PriceHistoryFn | None = None,
    leafs_by_cid: Mapping[str, Sequence[str]] | None = None,
    nav_lookup: Mapping[str, str] | None = None,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
    q: float = BH_FDR_Q,
    f2_top3_indices: list[int] | None = None,
) -> BranchReport:
    """Run H1–H7 on the held-analyst subset of `data`.

    Args:
        data: wallet data fetched in §4. The held-to-resolution subset is
            derived here via `filter_held_to_resolution`.
        peer_baseline_fn: callable for H5 per-grain population baseline lookup.
            If None, H5 grains return inference_not_run for all entries.
        price_history_fn: callable for H6 sell-then-24h-later price lookup.
            If None, H6 returns underpowered.
        leafs_by_cid / nav_lookup: tag-grain inputs (typically from
            `tag_inference.fetch_tag_lookups_from_db`). If None, H5 skipped.
        n_iter / seed / q: pass-through to bootstrap / BH.

    Returns:
        BranchReport populated with hypotheses, tag_grains, stationarity,
        time_split, and independence_check fields.
    """
    # 1. CUSUM stationarity pre-pass
    stationarity = run_h7_stationarity(data.closed_positions)
    closed_subset = select_post_regime_subset(data.closed_positions, stationarity)

    # 2. Held-to-resolution filter on the post-regime subset
    derived = WalletData(
        wallet=data.wallet,
        closed_positions=closed_subset,
        open_positions=data.open_positions,
        activity_rows=data.activity_rows,
        activity_cap_hit=data.activity_cap_hit,
        archetype_layer1=data.archetype_layer1,
        execution_profile=data.execution_profile,
        fetched_at=data.fetched_at,
    )
    held_positions = filter_held_to_resolution(derived)

    # 3. Run hypotheses
    # F2 top-3 indices are computed against `data.closed_positions` (the
    # original list, pre-CUSUM-trim). When CUSUM signals, indices into the
    # post-regime subset wouldn't match — so only pass top3_indices when
    # closed_subset is the full closed list (no regime trim).
    h1_top3 = (
        f2_top3_indices
        if (
            f2_top3_indices is not None
            and len(closed_subset) == len(data.closed_positions)
        )
        else None
    )
    h1 = run_h1_net_pnl(
        closed_subset, n_iter=n_iter, seed=seed,
        top3_indices=h1_top3,
    )
    h2 = run_h2_excess_wr(held_positions, n_iter=n_iter, seed=seed)
    h2_sub = run_h2_excess_wr(held_positions, sub_080_only=True, n_iter=n_iter, seed=seed)
    h3 = run_h3_size_conviction(
        held_positions, archetype_layer1=data.archetype_layer1,
        n_iter=n_iter, seed=seed,
    )
    h4 = run_h4_entry_timing(data.execution_profile, n_iter=n_iter, seed=seed)
    h6 = run_h6_exit_quality(data.activity_rows, price_history_fn)

    # 4. H5 per-grain (if tag inputs present)
    grain_results: list[TagGrainResult] = []
    if leafs_by_cid is not None and nav_lookup is not None:
        # enumerate over held_positions (the §6 unit of observation for H5 in held branch)
        enumeration = enumerate_tag_grains(
            held_positions, leafs_by_cid, nav_lookup,
        )
        # Compute per-grain raw p inline (light, avoids extra helper)
        for tag, indices in enumeration.leaf_grain_members.items():
            r = _build_one_grain(
                "leaf", tag, indices, held_positions,
                peer_baseline_fn, n_iter, seed,
                wallet=data.wallet,
            )
            grain_results.append(r)
        for nav, indices in enumeration.nav_grain_members.items():
            r = _build_one_grain(
                "nav", nav, indices, held_positions,
                peer_baseline_fn, n_iter, seed,
                wallet=data.wallet,
            )
            grain_results.append(r)

    # 4b. Slice 5 — apply market-structure guard before pooling. No-op on
    # legacy callers (no `replay_surface_meta` set with ledger_source).
    _apply_held_structure_guards(
        [
            (h1, "H1"),
            (h2, "H2"),
            (h2_sub, "H2_sub080"),
            (h3, "H3"),
            (h4, "H4"),
            (h6, "H6"),
        ],
        grain_results,
        data,
    )

    # 5. Build BH pool: H1, H2, H2_sub080, H3, H4, H6, plus per-grain (in-pool only)
    pool: list[tuple[str, float]] = []
    p_h1 = _bootstrap_p_from_ci(h1)
    if p_h1 is not None:
        pool.append(("H1", p_h1))
    p_h2 = _bootstrap_p_from_ci(h2)
    if p_h2 is not None:
        pool.append(("H2", p_h2))
    p_h2_sub = _bootstrap_p_from_ci(h2_sub)
    if p_h2_sub is not None:
        pool.append(("H2_sub080", p_h2_sub))
    if h3.p_value_raw is not None and h3.verdict not in ("n/a", "underpowered"):
        pool.append(("H3", h3.p_value_raw))
    p_h4 = _bootstrap_p_from_ci(h4)
    if p_h4 is not None:
        pool.append(("H4", p_h4))
    if h6.p_value_raw is not None and h6.verdict != "underpowered":
        pool.append(("H6", h6.p_value_raw))
    for g in grain_results:
        raw = getattr(g, "_p_raw", None)
        if raw is not None and g.verdict not in ("inference_not_run", "descriptive_only"):
            pool.append((f"H5:{g.grain}:{g.tag}", raw))

    pooled = pool_and_correct(pool, q=q) if pool else {}

    # 6. Apply BH adjustments back to each hypothesis verdict
    for h, name in (
        (h1, "H1"), (h2, "H2"), (h2_sub, "H2_sub080"),
        (h3, "H3"), (h4, "H4"), (h6, "H6"),
    ):
        if name in pooled:
            adj_p, rejected = pooled[name]
            h.p_value_bh_adjusted = adj_p
            h.verdict = _verdict_after_pool(
                h.verdict, rejected, h.ci_lower, h.ci_upper,
            )
    for g in grain_results:
        key = f"H5:{g.grain}:{g.tag}"
        if key in pooled:
            adj_p, rejected = pooled[key]
            g.p_value_bh_adjusted = adj_p
            new_verdict = _verdict_after_pool(
                "inconclusive", rejected, g.ci_lower, g.ci_upper,
            )
            # Bug 2: supported tag-grain evidence requires a full peer pool.
            if new_verdict == "supported" and g.confidence_flag != "ok":
                new_verdict = "inconclusive"
            g.verdict = new_verdict

    # 7. Time-split for H2 / H3 if eligible (n_held >= 60)
    # Rolling-origin k-fold (M3 closure): replaces the single 70/30 cut so
    # the OOS verdict isn't determined by one possibly-unlucky split. Verdict
    # survives when a strict majority of POWERED folds support it; folds
    # that come back "underpowered" drop out of both numerator and denominator.
    time_split: dict[str, TimeSplitResult] = {}
    folds, eligible = time_split_kfold(
        held_positions, sort_key=lambda p: int(p.get("timestamp") or 0),
    )
    for hyp_name, runner in (
        ("H2", lambda subset: run_h2_excess_wr(subset, n_iter=n_iter, seed=seed)),
        ("H2_sub080", lambda subset: run_h2_excess_wr(
            subset, sub_080_only=True, n_iter=n_iter, seed=seed,
        )),
        ("H3", lambda subset: run_h3_size_conviction(
            subset, archetype_layer1=data.archetype_layer1, n_iter=n_iter, seed=seed,
        )),
    ):
        if not eligible:
            time_split[hyp_name] = TimeSplitResult(
                n_total=len(held_positions), n_train=len(held_positions),
                n_test=0, train_p=None, test_p=None,
                holdout_status="underpowered",
                per_fold_statuses=[], n_folds_total=0,
                n_folds_supported=0, n_folds_underpowered=0,
            )
            continue
        per_fold_statuses: list[str] = []
        last_train: list = []
        last_test: list = []
        last_train_p: float | None = None
        last_test_p: float | None = None
        for train, test in folds:
            train_h = runner(train)
            test_h = runner(test)
            tp = _bootstrap_p_from_ci(train_h) if train_h.verdict != "underpowered" else None
            ep = _bootstrap_p_from_ci(test_h) if test_h.verdict != "underpowered" else None
            if train_h.verdict == "underpowered" or test_h.verdict == "underpowered":
                fold_status = "underpowered"
            elif train_h.verdict == "supported" and test_h.verdict == "supported":
                fold_status = "supported"
            else:
                fold_status = "failed"
            per_fold_statuses.append(fold_status)
            last_train, last_test = train, test
            last_train_p, last_test_p = tp, ep
        status = decision_rule_majority_supported(per_fold_statuses)
        time_split[hyp_name] = TimeSplitResult(
            n_total=len(held_positions),
            n_train=len(last_train), n_test=len(last_test),
            train_p=last_train_p, test_p=last_test_p,
            holdout_status=status,
            per_fold_statuses=per_fold_statuses,
            n_folds_total=len(per_fold_statuses),
            n_folds_supported=sum(1 for s in per_fold_statuses if s == "supported"),
            n_folds_underpowered=sum(1 for s in per_fold_statuses if s == "underpowered"),
        )

    # 8. Independence check. M4 closure: when tag-nav lookups are available,
    # include same-nav 24h news-window clusters; otherwise preserve the old
    # eventSlug-only fallback.
    if leafs_by_cid is not None and nav_lookup is not None:
        cluster = cluster_by_event_or_news_window(
            held_positions,
            leafs_by_cid,
            nav_lookup,
        )
    else:
        cluster = cluster_by_eventslug(held_positions)
    independence = IndependenceCheckResult(
        n_clusters=cluster.n_clusters,
        fraction_in_clusters_size_3plus=cluster.fraction_in_concentrated,
        flag=cluster.flag,
    )

    return BranchReport(
        branch="held_analyst",
        is_active=True,
        skip_reason=None,
        hypotheses=[h1, h2, h2_sub, h3, h4, h6],
        tag_grains=grain_results,
        stationarity=stationarity,
        time_split=time_split,
        independence_check=independence,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bootstrap_p_from_ci(h: HypothesisResult) -> float | None:
    """Return the empirical bootstrap p-value populated by the runner.

    M1 closure (wallet-protocol-bulletproofing): the runners (H1/H2/H4 and
    the H5 per-grain builder) now request `null_value=0.0` from
    `bootstrap_ci`, which returns the empirical two-sided p computed from
    the same resample distribution that produced the CI. This accessor is
    kept as a thin wrapper so existing call sites (BH pool, time-split
    folds) continue to work without changes.

    Returns None when the hypothesis is underpowered, has no CI, or the
    runner did not populate `p_value_raw` (defensive: legacy n=1 paths
    can fall through with no resample distribution).
    """
    if h.verdict == "underpowered" or h.ci_lower is None or h.ci_upper is None:
        return None
    return h.p_value_raw


def _attach_held_grain_financials(
    result: TagGrainResult,
    indices: list[int],
    held_positions: Sequence[Mapping],
) -> None:
    """Edge Surface v1 — attach descriptive per-grain financials.

    Display-only fields; NEVER feed verdict, gate, or primary_edge_type.
    Distinct from `metric_value` (the H5 sub-080 WR used by the test).
    """
    if not indices:
        return
    rows = [held_positions[i] for i in indices]
    pnls = [_realized_pnl(p) for p in rows]
    notionals = [_entry_notional(p) for p in rows]
    rois = [
        (p / n) if n > 0 else 0.0
        for p, n in zip(pnls, notionals)
    ]
    wins = [_is_win(p) for p in rows]
    winners_pnl = [p for p, w in zip(pnls, wins) if w]
    losers_pnl = [p for p, w in zip(pnls, wins) if not w]
    result.grain_win_rate = sum(1 for w in wins if w) / len(wins)
    result.grain_roi = sum(rois) / len(rois) if rois else None
    result.grain_total_pnl = float(sum(pnls))
    result.grain_avg_win = (
        float(sum(winners_pnl) / len(winners_pnl)) if winners_pnl else None
    )
    result.grain_avg_loss = (
        float(sum(losers_pnl) / len(losers_pnl)) if losers_pnl else None
    )
    result.grain_max_realized_loss = float(min(pnls)) if pnls else None


def _build_one_grain(
    grain: str,
    tag: str,
    indices: list[int],
    held_positions: Sequence[Mapping],
    peer_baseline_fn: PeerBaselineFn | None,
    n_iter: int,
    seed: int,
    *,
    wallet: str,
) -> TagGrainResult:
    """Build one TagGrainResult with raw p-value stashed on `_p_raw` attr.

    `wallet` is forwarded to `peer_baseline_fn` so the analyzed wallet can be
    excluded from its own peer set (self-contamination guard).
    """
    n = len(indices)
    baseline_info = peer_baseline_fn(grain, tag, wallet) if peer_baseline_fn else None
    peer_count = baseline_info[1] if baseline_info else 0
    confidence = classify_peer_confidence(peer_count)
    result = TagGrainResult(
        grain=grain, tag=tag, peer_count=peer_count,
        confidence_flag=confidence,
        n_at_grain=n, metric_value=None, pop_baseline=None,
        excess_pp=None, verdict="inference_not_run",
    )
    # Edge Surface v1: financials always attached (independent of peer
    # population / sample-size gates). Display-only, never feeds verdict.
    _attach_held_grain_financials(result, indices, held_positions)
    if peer_count < PEER_COUNT_INFERENCE_FLOOR:
        return result  # confidence_flag already set; verdict inference_not_run
    if n < H5_MIN_N_PER_GRAIN:
        result.pop_baseline = baseline_info[0] if baseline_info else None
        result.verdict = "descriptive_only"
        return result
    wins = [1.0 if _is_win(held_positions[i]) else 0.0 for i in indices]
    baseline_wr = baseline_info[0] if baseline_info else 0.0
    excess_per_position = [w - baseline_wr for w in wins]
    # M1: empirical bootstrap p-value computed from the same resample
    # distribution that produces (lo, hi). Replaces the legacy 0.05/0.5
    # CI-sign approximation.
    point, lo, hi, p_raw = bootstrap_ci(
        excess_per_position, n_iter=n_iter, seed=seed, null_value=0.0,
    )
    result.metric_value = float(np.mean(wins))
    result.pop_baseline = baseline_wr
    result.excess_pp = (result.metric_value - baseline_wr) * 100.0
    result.ci_lower = lo
    result.ci_upper = hi
    # Promote to "inconclusive" so it enters the BH pool. Final verdict is
    # assigned by the pool-and-correct step in run_held_branch.
    result.verdict = "inconclusive"
    setattr(result, "_p_raw", p_raw)
    return result


__all__ = [
    "EPSILON_RESOLUTION",
    "SUB_080_ENTRY_FILTER",
    "H1_MIN_N",
    "H2_MIN_N",
    "H3_MIN_N",
    "H5_MIN_N_PER_GRAIN",
    "H6_MIN_N",
    "H6_POST_SELL_OFFSET_SECONDS",
    "PeerBaselineFn",
    "PriceHistoryFn",
    "compute_dw_ew_roi",
    "filter_held_to_resolution",
    "run_h7_stationarity",
    "select_post_regime_subset",
    "run_h1_net_pnl",
    "run_h2_excess_wr",
    "run_h3_size_conviction",
    "run_h4_entry_timing",
    "run_h6_exit_quality",
    "run_held_branch",
]
