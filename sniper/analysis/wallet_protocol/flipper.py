"""§7 — Flipper branch (FH1–FH6).

Hypotheses run on the FIFO-matched (BUY, SELL) flip pairs from §4.7. Unit of
observation is one flip — NOT a closed position. ADR-002 (sub-0.80 entry filter)
does NOT apply; flippers can profitably flip 0.85→0.88 (3pp with tight risk).

H7-equivalent stationarity uses a CUSUM on cumulative flip_pnl rather than per-
position pnl. Same h=25 calibration applies (pnl distribution is heavier-tailed
than flip-roi but the calibration is conservative for both).

Data dependencies that may be absent:
- FH3 needs `execution_profile.tape_midpoint_pairs` → flagged
  `confidence=low_spread_estimate` if matched pairs < 20.
- FH4 needs
  `peer_baseline_fn(grain, tag, wallet) -> (mean_flip_roi, peer_count)`.
  The analyzed wallet is forwarded so the baseline lookup can exclude it
  from its own peer set (self-contamination guard).
- FH6 needs `price_history_fn(asset_id, ts) -> price_at_ts` at 1h fidelity.
  When coverage `< 0.80 * n_flips` → underpowered.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.h5_tag_edge import (
    PEER_COUNT_INFERENCE_FLOOR,
    classify_peer_confidence,
)
from sniper.analysis.wallet_protocol.protocol_guards import (
    market_structure_supported,
)
from sniper.analysis.wallet_protocol.statistics import (
    BH_FDR_Q,
    DEFAULT_BOOTSTRAP_ITERS,
    DEFAULT_PERMUTATION_ITERS,
    bootstrap_ci,
    cusum_signal,
    permutation_test_difference,
    permutation_test_spearman,
    pool_and_correct,
    decision_rule_majority_supported,
    time_split_kfold,
)
from sniper.analysis.wallet_protocol.tag_inference import enumerate_tag_grains
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    StationarityResult,
    TagGrainResult,
    TimeSplitResult,
    UnderpoweredReason,
    VerdictLabel,
)


_LEDGER_MATERIALIZATION_TAG = "duckdb_warehouse"


def _is_materialized_from_ledger(meta: Mapping | None) -> bool:
    """Mirror of the held-branch check; the flipper is invoked with
    materialized rows via an explicit kwarg rather than a `WalletData`
    field, so the check operates directly on the meta dict."""
    if not isinstance(meta, Mapping):
        return False
    for entry in meta.values():
        if (
            isinstance(entry, Mapping)
            and entry.get("ledger_source") == _LEDGER_MATERIALIZATION_TAG
        ):
            return True
    return False


def _build_flipper_guard_rows(flips: Sequence[Flip]) -> list[dict]:
    """Map flips to guard-input rows. Every Slice 5 flipper hypothesis
    is `token_price_only`, so the only field that matters is `token_id`.
    """
    return [{"token_id": f.asset_id or None} for f in flips]


def _stamp_structure_underpowered(
    h: HypothesisResult, reason: UnderpoweredReason
) -> None:
    h.verdict = "underpowered"
    h.underpowered_reason = reason
    h.notes.append(f"market_structure_guard:{reason}")


# Per-hypothesis min_n thresholds (spec §7).
FH1_MIN_N = 30
FH2_MIN_N = 30
FH3_MATCHED_PAIRS_FLOOR = 20
FH4_MIN_N_PER_GRAIN = 10
FH6_MIN_N = 20
FH6_PRICE_HISTORY_COVERAGE = 0.80

PeerBaselineFn = Callable[[str, str, str], tuple[float, int] | None]
# FlipRoiBaselineFn returns (mean_flip_roi, peer_count) — semantically
# distinct from PeerBaselineFn which returns held-only win rates. FH4
# requires this unit; the unit-mismatch test guards against the wrong fn
# being used. See spec
# docs/superpowers/specs/2026-05-15-flipper-tag-grain-inference-design.md §3.1
# and Phase 1 docs/superpowers/specs/2026-05-15-flipper-inference-phase1-persistence-design.md.
FlipRoiBaselineFn = Callable[[str, str, str], tuple[float, int] | None]
PriceHistoryFn = Callable[[str, int], float | None]

# FH4 inference thresholds — match spec §3.2 (peer-set threshold) and
# §3.3 (sample-size + peer-count floors for inference).
FH4_MIN_FLIPS_FOR_INFERENCE = 30  # n_at_grain floor for the wallet itself
FH4_MIN_PEERS_FOR_INFERENCE = 10  # peer_count floor for a meaningful baseline


# ---------------------------------------------------------------------------
# FH1 — Mean per-flip ROI > 0 (gate)
# ---------------------------------------------------------------------------


def run_fh1_mean_flip_roi(
    flips: Sequence[Flip],
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    n = len(flips)
    if n < FH1_MIN_N:
        return HypothesisResult(
            name="FH1", verdict="underpowered", n=n,
            notes=[f"n_flips={n} < FH1_MIN_N={FH1_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    rois = [f.flip_roi for f in flips]
    point, lo, hi, p_value = bootstrap_ci(
        rois, n_iter=n_iter, seed=seed, null_value=0.0,
    )
    verdict: VerdictLabel
    if lo > 0:
        verdict = "supported"
    elif hi < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="FH1", verdict=verdict, n=n,
        test_statistic=point, ci_lower=lo, ci_upper=hi,
        p_value_raw=p_value,
        details={"bootstrap_iters": n_iter},
    )


# ---------------------------------------------------------------------------
# FH2 — entry_notional × flip_roi (conviction)
# ---------------------------------------------------------------------------


def run_fh2_size_conviction(
    flips: Sequence[Flip],
    n_iter: int = DEFAULT_PERMUTATION_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    n = len(flips)
    if n < FH2_MIN_N:
        return HypothesisResult(
            name="FH2", verdict="underpowered", n=n,
            notes=[f"n_flips={n} < FH2_MIN_N={FH2_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    notionals = [f.buy_notional for f in flips]
    rois = [f.flip_roi for f in flips]
    rho, p_value = permutation_test_spearman(
        notionals, rois, n_iter=n_iter, seed=seed,
    )
    verdict: VerdictLabel
    if p_value <= 0.10 and rho > 0:
        verdict = "supported"
    elif p_value <= 0.10 and rho < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="FH2", verdict=verdict, n=n,
        test_statistic=rho, p_value_raw=p_value,
        details={"permutation_iters": n_iter},
    )


# ---------------------------------------------------------------------------
# FH3 — Mean flip_roi exceeds implied round-trip cost
# ---------------------------------------------------------------------------


def run_fh3_spread_capture(
    flips: Sequence[Flip],
    execution_profile: Mapping | None,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    n = len(flips)
    if n < FH1_MIN_N:
        return HypothesisResult(
            name="FH3", verdict="underpowered", n=n,
            notes=[f"n_flips={n} < {FH1_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    matched_pairs: list[float] = []
    explicit_estimate: float | None = None
    if execution_profile:
        # Either explicit `round_trip_cost_estimate` or per-pair tape midpoints
        explicit = execution_profile.get("round_trip_cost_estimate")
        if isinstance(explicit, (int, float)):
            explicit_estimate = float(explicit)
        else:
            pairs = execution_profile.get("tape_midpoint_pairs") or []
            for pr in pairs:
                fill_price = pr.get("fill_price")
                tape_mid = pr.get("tape_midpoint")
                if fill_price and tape_mid:
                    cost = 2.0 * (fill_price - tape_mid) / tape_mid
                    matched_pairs.append(cost)

    notes: list[str] = []
    if explicit_estimate is None and not matched_pairs:
        return HypothesisResult(
            name="FH3", verdict="underpowered", n=n,
            notes=[
                "no execution_profile cost surface "
                "(round_trip_cost_estimate or tape_midpoint_pairs)",
            ],
            underpowered_reason="no_execution_profile",
        )
    if explicit_estimate is not None:
        round_trip_cost = explicit_estimate
    elif len(matched_pairs) < FH3_MATCHED_PAIRS_FLOOR:
        round_trip_cost = float(np.mean(matched_pairs))
        notes.append(
            f"confidence=low_spread_estimate (matched_pairs={len(matched_pairs)} < {FH3_MATCHED_PAIRS_FLOOR})"
        )
    else:
        round_trip_cost = float(np.mean(matched_pairs))

    rois = [f.flip_roi for f in flips]
    excess = [r - round_trip_cost for r in rois]
    point, lo, hi, p_value = bootstrap_ci(
        excess, n_iter=n_iter, seed=seed, null_value=0.0,
    )
    verdict: VerdictLabel
    if lo > 0:
        verdict = "supported"
    elif hi < 0:
        verdict = "refuted"
        notes.append("spread-or-worse: mean flip_roi <= round-trip-cost estimate")
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="FH3", verdict=verdict, n=n,
        test_statistic=point, ci_lower=lo, ci_upper=hi,
        p_value_raw=p_value,
        details={
            "round_trip_cost_estimate": round_trip_cost,
            "matched_pairs": len(matched_pairs),
            "bootstrap_iters": n_iter,
        },
        notes=notes,
    )


# ---------------------------------------------------------------------------
# FH4 — Tag-conditioned flip ROI (multi-grain)
# ---------------------------------------------------------------------------


def _attach_flipper_grain_financials(
    result: TagGrainResult,
    indices: list[int],
    flips: Sequence[Flip],
) -> None:
    """Edge Surface v1 — attach descriptive per-grain financials for flips.

    Per-flip P&L = flip_roi * buy_notional. Win = flip_roi > 0.
    Display-only; NEVER feed verdict logic.
    """
    if not indices:
        return
    rows = [flips[i] for i in indices]
    pnls = [float(f.flip_roi) * float(f.buy_notional) for f in rows]
    rois = [float(f.flip_roi) for f in rows]
    wins = [float(f.flip_roi) > 0.0 for f in rows]
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


def _build_fh4_grain(
    grain: str,
    tag: str,
    indices: list[int],
    flips: Sequence[Flip],
    peer_baseline_fn: PeerBaselineFn | None,
    n_iter: int,
    seed: int,
    *,
    wallet: str,
    flip_roi_baseline_fn: "FlipRoiBaselineFn | None" = None,
) -> TagGrainResult:
    """FH4 — wallet's mean flip ROI in (grain, tag) vs flip-ROI peer mean.

    Two-baseline-fn architecture per Phase 0 spec §3.1:

      * peer_baseline_fn (PeerBaselineFn) — held-side win-rate baseline.
        Kept as a positional arg for backward-compat with existing
        callers; IGNORED by FH4 (wrong unit for flip-ROI comparison).
        Pinning test tests/test_wallet_protocol_fh4_unit_mismatch.py
        verifies this.

      * flip_roi_baseline_fn (FlipRoiBaselineFn) — returns
        (mean_flip_roi, peer_count) for (grain, tag). When this fn is
        provided and returns a meaningful baseline, FH4 runs a bootstrap
        CI on the wallet's per-flip excess ROI against null_value=0.

    Bootstrap pattern matches `held_analyst._build_held_tag_grain`
    (held_analyst.py:1283-1296): bootstrap the per-flip EXCESS
    (`flip_roi - peer_mean`) against null=0, store CI bounds on excess
    (so the shared `_verdict_after_pool` 0-test applies), set verdict
    to `inconclusive`, and stash raw p in `_p_raw` so it flows through
    the BH pool. Without flip_roi_baseline_fn or below floors, verdict
    stays `inference_not_run` / `underpowered` — fails closed per
    Phase 1.4 gate decision.

    Edge Surface v1 financials are always attached regardless of
    verdict — display-only, independent of inference.
    """
    n = len(indices)

    # peer_baseline_fn (held-WR) is the wrong unit and is not invoked.
    # peer_count is sourced from flip_roi_baseline_fn when available.
    flip_baseline_info = (
        flip_roi_baseline_fn(grain, tag, wallet)
        if flip_roi_baseline_fn is not None
        else None
    )
    peer_count = flip_baseline_info[1] if flip_baseline_info else 0
    confidence = classify_peer_confidence(peer_count)

    result = TagGrainResult(
        grain=grain, tag=tag, peer_count=peer_count,
        confidence_flag=confidence,
        n_at_grain=n, metric_value=None, pop_baseline=None,
        excess_pp=None, verdict="inference_not_run",
    )
    _attach_flipper_grain_financials(result, indices, flips)

    # Phase 1.4 gate: if no flip-ROI baseline available, stay
    # inference_not_run.
    if flip_baseline_info is None:
        result.underpowered_reason = "no_flip_roi_peer_baseline"
        return result

    peer_mean, peer_count = flip_baseline_info

    # Sample-size + peer-count floors. Spec §3.3.4.
    if n < FH4_MIN_FLIPS_FOR_INFERENCE:
        result.verdict = "underpowered"
        result.underpowered_reason = "flip_count_below_floor"
        return result
    if peer_count < FH4_MIN_PEERS_FOR_INFERENCE:
        result.verdict = "underpowered"
        result.underpowered_reason = "peer_count_below_floor"
        return result

    # Bootstrap on per-flip excess against null=0 so the pooled
    # `_verdict_after_pool(... ci_lower, ci_upper)` 0-test maps to the
    # excess directly. Mirrors held_analyst.py:1285.
    rois = [flips[i].flip_roi for i in indices]
    excess_per_flip = [r - peer_mean for r in rois]
    _excess_mean, ci_lo, ci_hi, p_raw = bootstrap_ci(
        excess_per_flip, n_iter=n_iter, seed=seed, null_value=0.0,
    )

    wallet_mean = sum(rois) / len(rois)
    result.metric_value = wallet_mean
    result.pop_baseline = peer_mean
    # excess_pp in percentage points per types.py:114 contract.
    result.excess_pp = (wallet_mean - peer_mean) * 100.0
    result.ci_lower = ci_lo
    result.ci_upper = ci_hi
    # Promote to "inconclusive" so the grain enters the BH pool. Final
    # verdict is assigned by pool-and-correct in run_flipper_branch.
    result.verdict = "inconclusive"
    setattr(result, "_p_raw", p_raw)
    return result


# ---------------------------------------------------------------------------
# FH5 — Hold-duration regime label (descriptive)
# ---------------------------------------------------------------------------


@dataclass
class HoldDurationStats:
    n: int
    median_seconds: float
    p25_seconds: float
    p75_seconds: float
    fraction_under_1h: float
    fraction_under_24h: float
    regime_label: str


def compute_fh5_hold_duration(flips: Sequence[Flip]) -> HoldDurationStats:
    """Descriptive hold-duration distribution + regime label per §7.5."""
    if not flips:
        return HoldDurationStats(
            n=0, median_seconds=0, p25_seconds=0, p75_seconds=0,
            fraction_under_1h=0.0, fraction_under_24h=0.0,
            regime_label="n/a",
        )
    durations = np.asarray([f.hold_duration_seconds for f in flips])
    median = float(np.median(durations))
    p25 = float(np.quantile(durations, 0.25))
    p75 = float(np.quantile(durations, 0.75))
    f_1h = float((durations < 3600).mean())
    f_24h = float((durations < 86400).mean())
    if median < 3600:
        label = "scalper"
    elif median < 86400:
        label = "news_arb"
    elif median < 7 * 86400:
        label = "slow_flipper"
    else:
        label = "long_hold"
    # Mixed: p25 in scalper range AND p75 in slow-flipper range
    if p25 < 3600 and p75 > 86400:
        label = "mixed"
    return HoldDurationStats(
        n=len(flips), median_seconds=median, p25_seconds=p25, p75_seconds=p75,
        fraction_under_1h=f_1h, fraction_under_24h=f_24h,
        regime_label=label,
    )


# ---------------------------------------------------------------------------
# FH6 — Directional skill during hold window
# ---------------------------------------------------------------------------


def run_fh6_directional_skill(
    flips: Sequence[Flip],
    price_history_fn: PriceHistoryFn | None,
    n_iter: int = DEFAULT_PERMUTATION_ITERS,
    seed: int = 42,
) -> HypothesisResult:
    n = len(flips)
    if n < FH6_MIN_N:
        return HypothesisResult(
            name="FH6", verdict="underpowered", n=n,
            notes=[f"n_flips={n} < FH6_MIN_N={FH6_MIN_N}"],
            underpowered_reason="n_below_threshold",
        )
    if price_history_fn is None:
        return HypothesisResult(
            name="FH6", verdict="underpowered", n=n,
            notes=["no price_history_fn (insufficient_price_history_coverage)"],
            underpowered_reason="no_price_history_fn",
        )
    with_market: list[float] = []      # flip_outcome (+1 win / 0 loss) when market moved with wallet
    against_market: list[float] = []
    coverage_hits = 0
    asset_missing = 0
    for f in flips:
        # C5 follow-on: Polymarket's /prices-history endpoint accepts only
        # the CLOB asset (token) id, NOT the conditionId. A conditionId
        # silently returns an empty history → price_history_fn would return
        # None for every flip. Use asset_id; skip flips where it is missing
        # (older Flip fixtures or activity rows lacking the `asset` field).
        if not f.asset_id:
            asset_missing += 1
            continue
        entry = price_history_fn(f.asset_id, f.buy_ts)
        exit_ = price_history_fn(f.asset_id, f.sell_ts)
        if entry is None or exit_ is None:
            continue
        coverage_hits += 1
        # market_dir and flip_roi are both in held-token coordinate (ADR-003):
        # price_history_fn is called with f.asset_id (the CLOB token id),
        # so exit_ - entry already reflects whether the held token appreciated.
        # No side-adjustment needed — the coordinate is already aligned.
        market_dir = exit_ - entry
        # Wallet wins on this flip iff sell_price > buy_price (positive ROI)
        flip_won = 1.0 if f.flip_roi > 0 else 0.0
        if market_dir > 0:
            with_market.append(flip_won)
        elif market_dir < 0:
            against_market.append(flip_won)

    coverage = coverage_hits / n
    if coverage < FH6_PRICE_HISTORY_COVERAGE:
        return HypothesisResult(
            name="FH6", verdict="underpowered", n=coverage_hits,
            notes=[f"insufficient_price_history_coverage ({coverage:.2f} < {FH6_PRICE_HISTORY_COVERAGE})"],
            underpowered_reason="no_price_history_fn",
            details={"coverage": coverage, "asset_id_missing": asset_missing},
        )
    if not with_market or not against_market:
        return HypothesisResult(
            name="FH6", verdict="underpowered", n=coverage_hits,
            notes=["one direction has zero flips after stratification"],
            underpowered_reason="zero_variance",
            details={"coverage": coverage, "asset_id_missing": asset_missing},
        )
    # Two-sample permutation test for difference in win rate
    diff, p_value = permutation_test_difference(
        with_market, against_market, n_iter=n_iter, seed=seed,
    )
    verdict: VerdictLabel
    if p_value <= 0.10 and diff > 0:
        verdict = "supported"
    elif p_value <= 0.10 and diff < 0:
        verdict = "refuted"
    else:
        verdict = "inconclusive"
    return HypothesisResult(
        name="FH6", verdict=verdict, n=coverage_hits,
        test_statistic=diff, p_value_raw=p_value,
        details={
            "wr_with_market": float(np.mean(with_market)),
            "wr_against_market": float(np.mean(against_market)),
            "coverage": coverage,
            "asset_id_missing": asset_missing,
            "permutation_iters": n_iter,
        },
    )


# ---------------------------------------------------------------------------
# Stationarity (CUSUM on cumulative flip pnl)
# ---------------------------------------------------------------------------


def run_flipper_stationarity(flips: Sequence[Flip]) -> StationarityResult:
    sorted_flips = sorted(flips, key=lambda f: f.buy_ts)
    series = [f.flip_roi * f.buy_notional for f in sorted_flips]  # USDC pnl per flip
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
    cusum = cusum_signal(series, mu=mu, sigma=sigma)
    if not cusum.signaled:
        return StationarityResult(
            cusum_status=cusum.cusum_status, h_used=cusum.h_used, k=cusum.k,
            signaled=False, signal_index=None,
            pre_regime_n=0, post_regime_n=len(series),
            inference_basis="full_history",
        )
    sig_idx = cusum.signal_index or 0
    post_n = len(series) - sig_idx
    basis = "post_regime_only" if post_n >= 20 else "insufficient_post_regime"
    return StationarityResult(
        cusum_status=cusum.cusum_status, h_used=cusum.h_used, k=cusum.k,
        signaled=True, signal_index=sig_idx,
        pre_regime_n=sig_idx, post_regime_n=post_n,
        inference_basis=basis,
    )


# ---------------------------------------------------------------------------
# Verdict assembly + run_flipper_branch
# ---------------------------------------------------------------------------


def _verdict_after_pool(
    base: VerdictLabel,
    rejected: bool,
    ci_lower: float | None,
    ci_upper: float | None,
) -> VerdictLabel:
    if base in ("underpowered", "n/a", "inference_not_run", "descriptive_only"):
        return base
    # Permutation tests carry direction in `base` and don't populate CIs.
    # Trust `base` when BH rejects; demote to inconclusive otherwise.
    if ci_lower is None and ci_upper is None and base in ("supported", "refuted"):
        return base if rejected else "inconclusive"
    if not rejected:
        if ci_upper is not None and ci_upper < 0:
            return "refuted"
        return "inconclusive"
    if ci_lower is not None and ci_lower > 0:
        return "supported"
    if ci_upper is not None and ci_upper < 0:
        return "refuted"
    return "inconclusive"


def _bootstrap_p_from_ci(h: HypothesisResult) -> float | None:
    """Return the empirical bootstrap p-value populated by the runner.

    M1 closure (wallet-protocol-bulletproofing): FH1/FH3 runners now request
    `null_value=0.0` from `bootstrap_ci`, which returns the empirical
    two-sided p computed from the same resample distribution that produced
    the CI. This accessor is kept as a thin wrapper so the BH pool and
    time-split call sites stay unchanged.
    """
    if h.verdict == "underpowered" or h.ci_lower is None or h.ci_upper is None:
        return None
    return h.p_value_raw


def run_flipper_branch(
    flips: Sequence[Flip],
    *,
    wallet: str = "",
    execution_profile: Mapping | None = None,
    peer_baseline_fn: PeerBaselineFn | None = None,
    flip_roi_baseline_fn: FlipRoiBaselineFn | None = None,
    price_history_fn: PriceHistoryFn | None = None,
    leafs_by_cid: Mapping[str, Sequence[str]] | None = None,
    nav_lookup: Mapping[str, str] | None = None,
    n_iter: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 42,
    q: float = BH_FDR_Q,
    materialized_meta: Mapping | None = None,
) -> BranchReport:
    """Run FH1–FH6 + stationarity on the flip-pair dataset.

    Spec §7.7: BH pool = {FH1, FH2, FH3, FH4 per-grain pooled, FH6}. FH5
    is descriptive only — excluded from pool.
    """
    stationarity = run_flipper_stationarity(flips)

    # FH5 first (descriptive — informs report)
    fh5_stats = compute_fh5_hold_duration(flips)

    fh1 = run_fh1_mean_flip_roi(flips, n_iter=n_iter, seed=seed)
    fh2 = run_fh2_size_conviction(flips, n_iter=n_iter, seed=seed)
    fh3 = run_fh3_spread_capture(flips, execution_profile, n_iter=n_iter, seed=seed)
    fh6 = run_fh6_directional_skill(flips, price_history_fn, n_iter=n_iter, seed=seed)

    # FH4 per-grain
    grain_results: list[TagGrainResult] = []
    if leafs_by_cid is not None and nav_lookup is not None:
        # Use Flip's condition_id as the conditionId attribute for enumerator
        flip_dicts = [{"conditionId": f.condition_id} for f in flips]
        enumeration = enumerate_tag_grains(flip_dicts, leafs_by_cid, nav_lookup)
        for tag, indices in enumeration.leaf_grain_members.items():
            grain_results.append(_build_fh4_grain(
                "leaf", tag, indices, flips, peer_baseline_fn, n_iter, seed,
                wallet=wallet, flip_roi_baseline_fn=flip_roi_baseline_fn,
            ))
        for nav, indices in enumeration.nav_grain_members.items():
            grain_results.append(_build_fh4_grain(
                "nav", nav, indices, flips, peer_baseline_fn, n_iter, seed,
                wallet=wallet, flip_roi_baseline_fn=flip_roi_baseline_fn,
            ))

    # Slice 5 — apply market-structure guard before pooling. No-op when
    # `materialized_meta` is None or carries no ledger_source tag.
    if _is_materialized_from_ledger(materialized_meta):
        guard_rows = _build_flipper_guard_rows(flips)
        for h, hid in (
            (fh1, "FH1"),
            (fh2, "FH2"),
            (fh3, "FH3"),
            (fh6, "FH6"),
        ):
            if h.verdict == "underpowered":
                continue
            supported, reason = market_structure_supported(hid, guard_rows)
            if not supported and reason is not None:
                _stamp_structure_underpowered(h, reason)
        for g in grain_results:
            if g.verdict == "underpowered":
                continue
            supported, reason = market_structure_supported("FH4", guard_rows)
            if not supported and reason is not None:
                g.verdict = "underpowered"
                g.underpowered_reason = reason
                if hasattr(g, "_p_raw"):
                    setattr(g, "_p_raw", None)

    # BH pool (FH5 excluded per §7.7)
    pool: list[tuple[str, float]] = []
    p_fh1 = _bootstrap_p_from_ci(fh1)
    if p_fh1 is not None:
        pool.append(("FH1", p_fh1))
    if fh2.p_value_raw is not None and fh2.verdict != "underpowered":
        pool.append(("FH2", fh2.p_value_raw))
    p_fh3 = _bootstrap_p_from_ci(fh3)
    if p_fh3 is not None:
        pool.append(("FH3", p_fh3))
    if fh6.p_value_raw is not None and fh6.verdict != "underpowered":
        pool.append(("FH6", fh6.p_value_raw))
    for g in grain_results:
        raw = getattr(g, "_p_raw", None)
        if raw is not None and g.verdict not in ("inference_not_run", "descriptive_only"):
            pool.append((f"FH4:{g.grain}:{g.tag}", raw))

    pooled = pool_and_correct(pool, q=q) if pool else {}

    for h, name in (
        (fh1, "FH1"), (fh2, "FH2"), (fh3, "FH3"), (fh6, "FH6"),
    ):
        if name in pooled:
            adj_p, rejected = pooled[name]
            h.p_value_bh_adjusted = adj_p
            h.verdict = _verdict_after_pool(h.verdict, rejected, h.ci_lower, h.ci_upper)
    for g in grain_results:
        key = f"FH4:{g.grain}:{g.tag}"
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

    # Time-split for FH1, FH2 (n_flips >= 60). M3b closure: use the same
    # rolling-origin k-fold majority rule as the held branch, not one 70/30 cut.
    time_split: dict[str, TimeSplitResult] = {}
    folds, eligible = time_split_kfold(flips, sort_key=lambda f: f.buy_ts)
    for hyp_name, runner in (
        ("FH1", lambda subset: run_fh1_mean_flip_roi(subset, n_iter=n_iter, seed=seed)),
        ("FH2", lambda subset: run_fh2_size_conviction(subset, n_iter=n_iter, seed=seed)),
    ):
        if not eligible:
            time_split[hyp_name] = TimeSplitResult(
                n_total=len(flips), n_train=len(flips), n_test=0,
                train_p=None, test_p=None, holdout_status="underpowered",
            )
            continue
        per_fold_statuses: list[str] = []
        last_train: list[Flip] = []
        last_test: list[Flip] = []
        train_p: float | None = None
        test_p: float | None = None
        for train, test in folds:
            last_train, last_test = train, test
            train_h = runner(train)
            test_h = runner(test)
            if train_h.verdict == "underpowered" or test_h.verdict == "underpowered":
                per_fold_statuses.append("underpowered")
                continue
            if train_h.verdict == "supported" and test_h.verdict == "supported":
                per_fold_statuses.append("supported")
            else:
                per_fold_statuses.append("failed")
            train_p = _bootstrap_p_from_ci(train_h)
            test_p = _bootstrap_p_from_ci(test_h)
        status = decision_rule_majority_supported(per_fold_statuses)
        time_split[hyp_name] = TimeSplitResult(
            n_total=len(flips), n_train=len(last_train), n_test=len(last_test),
            train_p=train_p, test_p=test_p, holdout_status=status,
            per_fold_statuses=per_fold_statuses,
            n_folds_total=len(per_fold_statuses),
            n_folds_supported=sum(1 for s in per_fold_statuses if s == "supported"),
            n_folds_underpowered=sum(1 for s in per_fold_statuses if s == "underpowered"),
        )

    # FH5 surfaced as a pseudo-hypothesis (descriptive_only verdict) so the
    # report layer can render the regime label cleanly.
    fh5 = HypothesisResult(
        name="FH5", verdict="descriptive_only", n=fh5_stats.n,
        details={
            "median_seconds": fh5_stats.median_seconds,
            "p25_seconds": fh5_stats.p25_seconds,
            "p75_seconds": fh5_stats.p75_seconds,
            "fraction_under_1h": fh5_stats.fraction_under_1h,
            "fraction_under_24h": fh5_stats.fraction_under_24h,
            "regime_label": fh5_stats.regime_label,
        },
    )

    return BranchReport(
        branch="flipper",
        is_active=True,
        skip_reason=None,
        hypotheses=[fh1, fh2, fh3, fh5, fh6],
        tag_grains=grain_results,
        stationarity=stationarity,
        time_split=time_split,
        independence_check=None,  # eventSlug clustering applies to held branch only
    )


__all__ = [
    "FH1_MIN_N",
    "FH2_MIN_N",
    "FH3_MATCHED_PAIRS_FLOOR",
    "FH4_MIN_N_PER_GRAIN",
    "FH6_MIN_N",
    "FH6_PRICE_HISTORY_COVERAGE",
    "PeerBaselineFn",
    "FlipRoiBaselineFn",
    "FH4_MIN_FLIPS_FOR_INFERENCE",
    "FH4_MIN_PEERS_FOR_INFERENCE",
    "PriceHistoryFn",
    "HoldDurationStats",
    "compute_fh5_hold_duration",
    "run_fh1_mean_flip_roi",
    "run_fh2_size_conviction",
    "run_fh3_spread_capture",
    "run_fh6_directional_skill",
    "run_flipper_stationarity",
    "run_flipper_branch",
]
