"""Type definitions for wallet-analysis protocol v2.

Spec: docs/superpowers/specs/wallet-analysis-protocol-v2.md
Brain: methodology/wallet-analysis-protocol-v2

ProtocolReport is the top-level output. Every wallet analysis produces one,
following the fixed-section template in §12 of the spec. Sections that don't
apply (e.g., flipper branch on a held-analyst-only wallet) are explicitly
marked N/A rather than omitted.

Conventions:
- All P&L values in USDC.
- Timestamps in unix epoch seconds.
- Verdicts use a small fixed vocabulary; see VerdictLabel below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger

PROTOCOL_VERSION = "v2"

# Verdicts a hypothesis can return (per spec §6, §7).
VerdictLabel = Literal[
    "supported",                   # BH-adjusted p < 0.10 AND CI excludes null AND survived time-split (when applicable)
    "refuted",                     # CI fully on the wrong side of null
    "inconclusive",                # CI includes null
    "underpowered",                # n below test-specific threshold
    "inference_not_run",           # H5/FH4 only — peer_count < 10 baseline floor
    "descriptive_only",            # n_held below threshold; reported but no inference
    "baseline_underpowered",       # peer_count 10-29 — runs but flagged
    "n/a",                         # hypothesis doesn't apply (archetype, mode-router routing)
]

# Top-level decision a wallet report can produce (§11 mixed-mode matrix).
OverallVerdict = Literal[
    "edge_detected",               # at least one branch supported, survived time-split
    "no_edge",                     # both branches refuted/inconclusive, stationary
    "underpowered",                # both branches underpowered
    "data_insufficient",           # rejected at Stage 1 or before deep analysis
    "operator_review",             # held supported but flipper refuted (or inverse) — conflict
]

GateResult = Literal[
    "pass",
    "reject_hedge_archetype",
    "reject_insufficient_sample",
    "reject_dormant",
]

# Mode-router output (§5).
TradingMode = Literal[
    "held_analyst_only",           # flip_rate < 0.30
    "flipper_only",                # flip_rate > 0.70
    "mixed_mode",                  # 0.30 <= flip_rate <= 0.70
    "n/a_routing",                 # rejected at Stage 1
]


UnderpoweredReason = Literal[
    "n_below_threshold",                # sample size short of the test floor (H1_MIN_N etc.)
    "heavy_tail_inconclusive",          # bootstrap CI straddles zero on a heavy-tail dist
    "regime_change_too_recent",         # H7/CUSUM signaled but post-regime n insufficient
    "no_execution_profile",             # H4 — execution_profile is None / status='not_run'
    "no_price_history_fn",              # H6/FH6 — price_history_fn is None
    "peer_count_below_floor",           # H5/FH4 — peers <10 (kept distinct from inference_not_run)
    "zero_variance",                    # degenerate case (e.g. all wins or all losses)
    # Slice 5 — market-structure guard applied via the materialized branch path:
    "market_structure_unsupported",     # binary-only hypothesis received no binary rows
    "outcome_index_missing",            # binary rows present but outcome_index is None
    "no_token_history",                 # token-price-only hypothesis received no token_id
]


@dataclass
class HypothesisResult:
    """One hypothesis's verdict + supporting numbers.

    Specific hypotheses (H1, H2, H3, H4, H5, H6, H7, FH1-FH6) populate the
    `details` dict with hypothesis-specific fields. Common fields surface here.

    `underpowered_reason` is populated whenever `verdict == "underpowered"`.
    See I2 (wallet-protocol-bulletproofing.md) — graduated reason-bands let
    consumers tell `n_below_threshold` apart from `no_execution_profile` etc.
    """

    name: str                                              # e.g., "H1", "FH3"
    verdict: VerdictLabel
    n: int = 0                                             # sample size used
    test_statistic: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None
    p_value_raw: float | None = None                       # before BH adjustment
    p_value_bh_adjusted: float | None = None               # after BH-FDR within branch
    details: dict[str, Any] = field(default_factory=dict)  # hypothesis-specific extras
    notes: list[str] = field(default_factory=list)         # caveats, warnings, gates triggered
    underpowered_reason: UnderpoweredReason | None = None  # populated iff verdict=="underpowered"


@dataclass
class TagGrainResult:
    """One tag grain's H5 / FH4 result. Multiple per branch."""

    grain: Literal["leaf", "nav"]
    tag: str                                               # leaf slug OR nav-category slug
    peer_count: int                                        # # of peer wallets with data on this tag
    confidence_flag: Literal["ok", "low_population"]
    n_at_grain: int                                        # this wallet's positions/flips at this grain
    metric_value: float | None                             # WR_held or mean_flip_roi
    pop_baseline: float | None
    excess_pp: float | None                                # (metric - baseline) * 100
    ci_lower: float | None = None
    ci_upper: float | None = None
    p_value_bh_adjusted: float | None = None
    verdict: VerdictLabel = "inference_not_run"
    leaf_vs_nav_agreement: str | None = None               # populated only if verdict=supported
    underpowered_reason: UnderpoweredReason | None = None  # populated iff verdict=="underpowered"
    # Edge Surface v1 — descriptive per-grain financials. Display-only;
    # NEVER feed verdict/gate/primary_edge_type. Distinct from `metric_value`
    # (which is the H5 sub-080 WR used by the hypothesis test). All optional
    # for back-compat with v1 callers and existing fixtures.
    grain_win_rate: float | None = None                    # un-filtered WR over grain positions
    grain_roi: float | None = None                         # mean(pnl/entry_notional) over grain positions
    grain_total_pnl: float | None = None                   # sum(realizedPnl) (or sum(flip_pnl) for flipper)
    grain_avg_win: float | None = None                     # mean(pnl) over winners; None if no winners
    grain_avg_loss: float | None = None                    # mean(pnl) over losers; None if no losers
    grain_max_realized_loss: float | None = None           # min(pnl); None if grain has no positions
    # Diagnostic stash for callers that need to attach hypothesis-specific
    # metadata (e.g. raw p-values, bootstrap iteration counts). Mirror of
    # HypothesisResult.details. Codex 2026-05-27 audit: held_analyst.py was
    # constructing TagGrainResult with details={...} but the dataclass had
    # no such field, raising TypeError at runtime. The call sites already
    # carried `# type: ignore[call-arg]` markers — the right fix is to make
    # the field real, not to drop the diagnostics.
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StationarityResult:
    """H7 / per-branch CUSUM stationarity outcome."""

    cusum_status: Literal["calibrated", "uncalibrated"]
    h_used: float
    k: float
    signaled: bool
    signal_index: int | None                               # position index where regime change detected
    pre_regime_n: int = 0
    post_regime_n: int = 0
    inference_basis: Literal["full_history", "post_regime_only", "insufficient_post_regime"] = "full_history"


@dataclass
class TimeSplitResult:
    """OOS validation outcome for a hypothesis.

    M3 closure (2026-04-26): the legacy single 70/30 split was replaced
    with rolling-origin k-fold (default k=5). `n_train`/`n_test`/`train_p`/
    `test_p` are populated from the FINAL (most recent) fold so existing
    readers keep working; `per_fold_statuses` and `n_folds_*` carry the
    full picture, and `holdout_status` reflects the majority rule across
    all powered folds.
    """

    n_total: int
    n_train: int
    n_test: int
    train_p: float | None
    test_p: float | None
    holdout_status: Literal["survived", "failed", "underpowered", "not_attempted"]
    per_fold_statuses: list[str] = field(default_factory=list)
    n_folds_total: int = 0
    n_folds_supported: int = 0
    n_folds_underpowered: int = 0


@dataclass
class IndependenceCheckResult:
    """Cluster-reduced sample-size check (§7 in v2)."""

    n_clusters: int
    fraction_in_clusters_size_3plus: float
    flag: Literal["concentrated", "normal"]
    cluster_reduced_h2_excess_pp: float | None = None
    cluster_reduced_h2_ci_lower: float | None = None
    cluster_reduced_h2_ci_upper: float | None = None


@dataclass
class BranchReport:
    """Either held-analyst or flipper branch.

    Hypotheses populated depending on which branch this is.
    """

    branch: Literal["held_analyst", "flipper"]
    is_active: bool                                        # was this branch run?
    skip_reason: str | None = None                         # populated when is_active=False
    hypotheses: list[HypothesisResult] = field(default_factory=list)
    tag_grains: list[TagGrainResult] = field(default_factory=list)
    stationarity: StationarityResult | None = None
    time_split: dict[str, TimeSplitResult] = field(default_factory=dict)  # keyed by hypothesis name
    independence_check: IndependenceCheckResult | None = None


@dataclass
class WalletData:
    """All raw data fetched for a wallet, before any analysis.

    Built by sniper.analysis.wallet_protocol.fetch.fetch_wallet_data().
    Shared by Stage 1 gates and Stage 2 branches.
    """

    wallet: str
    closed_positions: list[dict]                           # /closed-positions raw rows
    open_positions: list[dict]                             # /positions raw rows
    activity_rows: list[dict]                              # /activity?type=TRADE rows (capped at 3000)
    activity_cap_hit: bool                                 # True if n_activity_rows == 3000
    archetype_layer1: str | None                           # from wallet_archetype_extractor.py
    execution_profile: dict | None                         # from --with-execution; may be None
    fetched_at: int                                        # unix epoch when fetched
    archetype_layer2: str | None = None                    # Layer-2 intent label (display-only); see CLAUDE.md note
    capital_ledger: CapitalLedger | None = None            # C1 onchain capital flow (optional)
    portfolio_value_usdc: float | None = None              # current open-position value (optional)
    fetch_quality_flags: list[str] = field(default_factory=list)  # subgraph/fetch warnings
    subgraph_fallback_used: bool = False                   # True when C2 fallback replaced capped REST activity
    # Completeness Surface v1 — capture-state flags persisted from fetch.
    # walk_full_history defaults False to match historical behavior; old
    # fixtures load with walk_full_history=False (truthful — pre-feature
    # captures didn't track this). n_unmapped_fills is the typed
    # counterpart to the ACTIVITY_SUBGRAPH_UNMAPPED string flag.
    walk_full_history: bool = False
    n_unmapped_fills: int = 0
    # CTF event streams captured for position_reconciliation_surface.
    # `None` (fetch failed) is distinct from `[]` (fetch succeeded, no
    # events) — the surface relies on that distinction to pick
    # `data_unavailable` vs `ok`. Old fixtures without these keys
    # deserialize to `None`, preserving backward compatibility.
    redemptions: list[dict] | None = None    # CTF Redemption events
    splits: list[dict] | None = None         # CTF Split events
    merges: list[dict] | None = None         # CTF Merge events
    # Edge Surface v1 — display-only subset of wallet_archetype_extractor
    # features. Stamped by callers (run_wallet_protocol.py / record_fetch_snapshot.py)
    # when the extractor is invoked. Shape: {usdc_size_cv, top_condition_fill_share,
    # redeem_ratio, domain_concentration, pnl_cv_abs}. NEVER feeds verdict logic.
    extractor_features_summary: dict | None = None
    # --- v2 replay surfaces (spec 2026-05-01-fixture-v2-schema.md) ---
    price_history_lookups: dict[str, float | None] | None = None
    # Copy-latency post-entry price grid — keyed "<asset>|<ts>", captured at
    # fidelity=1 (finest). SEPARATE from price_history_lookups because that
    # dict carries H6/FH6 prices at the default (coarse) fidelity and a
    # single "<asset>|<ts>" key cannot safely hold two fidelities for the
    # same (asset, ts). `None` until a grid-enabled capture runs.
    copy_latency_prices: dict[str, float | None] | None = None
    leafs_by_cid: dict[str, list[str]] | None = None
    nav_lookup: dict[str, str] | None = None
    peer_baselines: dict[str, list] | None = None  # values are [mean_wr, peer_count] lists
    replay_surface_meta: dict[str, dict] | None = None
    # Fetch termination reasons per endpoint — populated by fetch_wallet_data
    # when progress_emitter is wired (Phase A split #3). Shape:
    #   {"closed_positions": "empty_page"|"partial_page"|"api_refused", ...}
    fetch_termination: dict | None = None


@dataclass
class ProtocolReport:
    """Top-level wallet analysis output. One per wallet per protocol version.

    Fixed-section template per spec §12. All sections present even when N/A
    (so report parsing is uniform across wallets).
    """

    wallet: str
    protocol_version: str = PROTOCOL_VERSION
    analyzed_at: int = 0                                   # unix epoch seconds

    # Headline data summary
    n_closed_positions: int = 0
    n_held_to_resolution: int = 0
    n_open_positions: int = 0
    n_open_dead: int = 0
    net_pnl: float = 0.0

    # PnL composition (Phase A split #2 — fetch-rebuild design 2026-05-12)
    # Backward compat: `net_pnl` is preserved and continues to equal `realized_pnl`.
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    combined_pnl: float = 0.0
    realized_roi: float | None = None
    unrealized_roi: float | None = None
    combined_roi: float | None = None
    open_position_breakdown: dict | None = None

    activity_rows_fetched: int = 0
    activity_cap_hit: bool = False
    subgraph_fallback_used: bool = False
    archetype_layer1: str | None = None
    archetype_layer2: str | None = None                    # Layer-2 intent label (display-only)
    flip_rate: float | None = None
    trading_mode: TradingMode = "n/a_routing"

    # Completeness Surface v1 — passthrough of fetch-time capture-state flags.
    # Display-only; consumed by the report's `## Data completeness` block.
    walk_full_history: bool = False
    n_unmapped_fills: int = 0

    # Stage 1 gate result
    gate_result: GateResult = "pass"
    gate_reason: str | None = None

    # Data quality flags (caution flags F1-F4 from §3.2; tag-mapping ambiguity flags)
    data_quality_flags: list[str] = field(default_factory=list)

    # Capital efficiency — dollar-weighted vs equal-weighted ROI (I1).
    # Both computed over the universe of closed_positions whose cost basis
    # is well-defined (avgPrice * size > 0). DW reflects how the wallet's
    # actual dollars performed; EW reflects the average position. Large
    # divergence (>2x) flags concentration risk — the headline EW ROI is
    # being driven by tiny positions while a few big bets dominate dollar
    # outcomes. Render-layer surfaces this prominently before H1.
    dw_roi_point: float | None = None
    dw_roi_ci_lower: float | None = None
    dw_roi_ci_upper: float | None = None
    ew_roi_point: float | None = None

    # Stage 2 results
    held_analyst_branch: BranchReport | None = None
    flipper_branch: BranchReport | None = None

    # Supersession key for `wallet_flipper_tag_grain` writes (Phase 1.2).
    # Stamped by `analyze_wallet` from `WalletData` via
    # `compute_wallet_fixture_hash_from_data`. The writer reads this when
    # not given a fixture_path. Identical raw data → identical hash;
    # excludes wall-clock fields. Tests constructing ProtocolReport
    # directly must stamp this themselves.
    wallet_fixture_hash: str | None = None

    # Final decision (§11 mixed-mode matrix)
    overall_verdict: OverallVerdict = "data_insufficient"
    primary_edge_type: str | None = None                   # "entry_timing" | "size_conviction" | "tag_specialist" | "flipper_skill" | None
    copyability_note: str | None = None
    open_questions: list[str] = field(default_factory=list)
    applicable_limitations: list[str] = field(default_factory=list)

    # C1 — onchain capital flow ledger. None when --capital-ledger not passed.
    capital_ledger: CapitalLedger | None = None

    # Execution-quality summary — display-only passthrough of relevant fields
    # from `data.execution_profile`. NEVER feeds verdict, gate, or
    # primary_edge_type. Shape (when populated):
    #     {
    #         "skew_vs_preceding_bar": {...summary dict from execution_profile},
    #         "skew_vs_tape_5min": {...},
    #         "drift_1h": {...},
    #         "drift_24h": {...},
    #     }
    # Field is None when execution_profile is unavailable or its status != "populated".
    execution_quality_summary: dict | None = None

    # Edge Surface v1 — outcome-side (YES/NO/other) breakdown of closed
    # positions. Display-only. Shape (when populated):
    #     {
    #         "yes":   {"n": int, "win_rate": float, "roi": float, "total_pnl": float},
    #         "no":    {"n": int, "win_rate": float, "roi": float, "total_pnl": float},
    #         "other": {"n": int, "win_rate": float, "roi": float, "total_pnl": float},
    #     }
    # `other` bucket present only when at least one position has a non-yes/no outcome.
    # Field is None when there are no closed positions. NEVER feeds verdict logic.
    outcome_side_breakdown: dict | None = None

    # Edge Surface v1 — passthrough of `data.extractor_features_summary`.
    # See WalletData docstring for shape. Display-only.
    extractor_features_summary: dict | None = None

    # Exit Attribution v1 — separates held-to-resolution prediction skill
    # from pre-resolution trading skill. Display-only; NEVER feeds verdict,
    # gate, or primary_edge_type. Shape (when populated):
    #     {
    #         "summary": {category → {n, total_pnl}},
    #         "coverage": {activity_cap_hit, subgraph_fallback_used,
    #                      activity_min_ts, n_total, n_outside_window,
    #                      history_assumed_full},
    #     }
    # Categories defined in `exit_attribution.ALL_CATEGORIES`.
    # `outcome_side_breakdown` buckets carry their own per-bucket attribution
    # under the "by_attribution" key.
    exit_attribution: dict | None = None

    # Timing Surface v1 — captured-travel-only timing classification.
    # Display-only; NEVER feeds verdict, gate, or primary_edge_type.
    # See docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
    # (commit d598cf2, r2). Audit cross-ref:
    # docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md.
    # Shape (when populated): see spec §6 — top-level fields:
    #     classification, classification_reason,
    #     confidence_band, confidence_downgraded,
    #     coverage_downgraded, coverage_caveats,
    #     n_attributed, n_total_closed,
    #     held_to_resolution, pre_resolution,
    #     future_repricing_available (always False in v1),
    #     future_repricing_signal / window / price_history_source
    #         (always None in v1).
    timing_surface: dict | None = None

    # Position Building Surface v1 (W2) — pure-compute reconstruction of
    # how a wallet built, reduced, and re-accumulated positions from
    # normalized fill rows. Display-only; NEVER feeds verdict, gate,
    # primary_edge_type, or copyability_note. No I/O, no simulator, no
    # trigger logic, no copy fields, no price-history calls.
    # See docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md.
    # Shape (when populated): see spec §3 — top-level fields:
    #     wallet, surface_version (= "position_building_v1"),
    #     generated_from, source_quality_distribution,
    #     identity_quality_distribution, row_quality_summary,
    #     wallet_summary, positions, top_level_caveats.
    # Phase 2 wired the field structurally; Phase 3 populates it from
    # analyze_wallet. Default None when no activity rows and no closed
    # positions are present.
    position_building_surface: dict | None = None

    # Liquidity Surface v1 — proxy-only execution-friction context for the
    # wallet's observed trade sizes. Display-only; NEVER feeds verdict, gate,
    # primary_edge_type, copyability_note, data_quality_flags, branch
    # hypotheses, or honesty-floor logic. No live I/O and no order placement.
    # Historical order-book depth is not available in current fixtures, so the
    # surface degrades honestly instead of fabricating depth/slippage.
    # Shape (when populated): {
    #     surface_version, status, fidelity_tier,
    #     historical_depth_available, summary, degradation,
    #     market_observations,
    # }
    liquidity_surface: dict | None = None

    # Capital & Risk surface — peak concurrent deployment (capital to play),
    # max drawdown (capital to survive), recycle factor, return-on-base, and
    # PnL concentration. Display-only; NEVER feeds verdict, gate,
    # primary_edge_type, copyability_note, or data_quality_flags. `None` only
    # on the pre-surface backward-compat path.
    # Shape (when populated): {
    #     surface_version, data_state, peak_concurrent_deployed_usdc,
    #     peak_deployed_at, cumulative_deployed_usdc, recycle_factor,
    #     max_drawdown_usdc, max_drawdown_pct_of_peak,
    #     return_on_peak_deployed, pnl_concentration, lifespan_days, caveats,
    # }
    capital_risk_surface: dict | None = None

    # Sizing & Outcome surface — whether the wallet's entry sizing is
    # associated with outcome quality (stratified Spearman rho, leave-one-out
    # robustness, late-conviction gap). Display-only; NEVER feeds verdict,
    # gate, primary_edge_type, copyability_note, or data_quality_flags.
    # Reports association, never "skill". `None` only on the pre-surface
    # backward-compat path.
    # Shape (when populated): {
    #     surface_version, data_state, association, global_rho, global_rho_p,
    #     stratified_rho, n_datapoints, leave_one_out_rho, robustness_holds,
    #     size_buckets, by_category, total_entry_size_usdc,
    #     total_cost_basis_usdc, late_conviction_ratio,
    #     added_after_entry_usdc, n_positions_dropped, caveats,
    # }
    sizing_outcome_surface: dict | None = None

    # Null-Strategy Baseline surface — the wallet's hold-to-resolution
    # side-selection return vs always-favorite / always-underdog / random
    # on the markets it traded (did it beat the chalk?). Display-only;
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or
    # data_quality_flags. Reports beat/matched/underperformed, never
    # "skill". `None` only on the pre-surface backward-compat path.
    # Shape (when populated): {
    #     surface_version, data_state, comparison, n_total_closed,
    #     n_eligible, coverage, wallet_ew_roi, favorite_ew_roi,
    #     underdog_ew_roi, random_ew_roi, edge_vs_favorite,
    #     favorite_hit_rate, n_on_favorite, n_on_underdog, caveats,
    # }
    null_baseline_surface: dict | None = None

    # Survivorship surface — flags when the wallet was discovered via a
    # winner-ranked list (leaderboard / top-PnL), so a positive read is not
    # treated as unconditional. Display-only; NEVER feeds verdict, gate,
    # primary_edge_type, copyability_note, the Strategy Pattern Audit, or
    # data_quality_flags. It discloses the discovery basis — it does not
    # correct for survivorship. `None` only on the pre-surface
    # backward-compat path.
    # Shape (when populated): {
    #     surface_version, discovery_source, survivor_sampled, caveat, note,
    # }
    survivorship_surface: dict | None = None

    # Copy-Latency Decay surface — how much of the wallet's entry edge
    # survives to a copier entering 5/15/60 min late. Display-only; NEVER
    # feeds verdict, gate, primary_edge_type, copyability_note (the
    # surface's own `copyability` key is display text, not that field),
    # the Strategy Pattern Audit, or data_quality_flags. Ships dormant —
    # `data_state` is `data_unavailable` until a grid-enabled capture runs.
    # `None` only on the pre-surface backward-compat path.
    # Shape (when populated): {
    #     surface_version, data_state, copyability, primary_horizon_min,
    #     n_eligible, n_total_closed, horizons[], caveats,
    # }
    copy_latency_surface: dict | None = None

    # Position Reconciliation surface — reconciles /closed-positions against
    # CTF Redemption events to detect API undercounting. Reports the gap
    # ratio, acquisition-rail breakdown, and sets downstream_lower_bound
    # when the gap exceeds the threshold. Display-only; NEVER feeds
    # overall_verdict, gate_result, primary_edge_type, copyability_note,
    # trading_mode, or any other surface's numbers. `downstream_lower_bound`
    # is informational — it does NOT mutate sibling surfaces. Caveats live
    # in the surface's own `caveats` list; never appended to data_quality_flags.
    # `None` only on the pre-surface backward-compat path.
    # Shape (when populated): {
    #     surface_version, data_state, closed_positions_count,
    #     closed_condition_count, redeemed_condition_count,
    #     redeemed_not_in_closed_count, undercount_ratio,
    #     redeemed_orderbook_traced_count, untraced_redemption_count,
    #     rails_observed, acquisition_rail_gap_share, condition_set_summary,
    #     downstream_lower_bound, note, caveats,
    # }
    position_reconciliation_surface: dict | None = None

    # Completeness Surface v1 — typed JSON exposure of the same numbers
    # rendered inside the `## Data completeness` Markdown block. Display-only;
    # NEVER feeds verdict, gate, primary_edge_type, or copyability_note.
    # The block is sourced from this typed field whenever it is populated;
    # legacy callers that construct a ProtocolReport without going through
    # analyze_wallet still render via the inline fallback path so existing
    # behaviour is preserved byte-for-byte.
    # See report.py:_data_completeness_block for field semantics. Shape:
    #     {
    #         "activity_capture": {
    #             "activity_rows_fetched": int,
    #             "activity_cap_hit": bool,
    #             "walk_full_history": bool,
    #             "subgraph_fallback_used": bool,
    #             "n_unmapped_fills": int,
    #         },
    #         "attribution_coverage": {
    #             "n_total": int,
    #             "n_outside_window": int,
    #             "n_insufficient_data": int,
    #             "n_attributed": int,                # sample-size floor
    #             "n_with_resolved_attribution": int, # reliability signal
    #             "history_assumed_full": bool,
    #         },
    #         "closed_positions_capture": {           # G2 (2026-05-17)
    #             "closed_positions_fetched": int,
    #             "closed_positions_cap_hit": bool,   # True iff fetch
    #                                                 # terminated abnormally
    #                                                 # for /closed-positions
    #         },
    #         "tag_lookup_state": str,                # G4 (2026-05-17)
    #                                                 # one of:
    #                                                 #   "ok"          — lookup ran, found tags
    #                                                 #   "empty"       — lookup ran, no tags
    #                                                 #   "unavailable" — lookup failed
    #                                                 #   "skipped"     — operator skipped (--no-tags)
    #         "caveats": {
    #             "activity_truncated": bool,
    #             "subgraph_unmapped_fills": bool,
    #             "attribution_coverage_low": bool,
    #             "limitation_present": bool,
    #             "closed_positions_truncated": bool, # G2 — mirrors
    #                                                 # closed_positions_capture
    #                                                 # .closed_positions_cap_hit
    #         },
    #     }
    # Default None when analyze_wallet has not been run (e.g. ad-hoc
    # ProtocolReport construction in legacy tests).
    data_completeness: dict | None = None

    # Time Coverage v1 (Phase A split #3) — temporal extent of the wallet's
    # activity. Display-only; NEVER feeds verdict, gate, primary_edge_type,
    # or copyability_note. None when closed_positions is empty.
    # Shape: {
    #     "first_position_ts": int,
    #     "last_position_ts": int,
    #     "duration_days": int,
    #     "n_positions_per_month": float | None,
    #     "first_fill_ts": int | None,
    #     "last_fill_ts": int | None,
    #     "fetch_termination": dict,
    # }
    time_coverage: dict | None = None

    # G3 (2026-05-17) — operator-supplied metadata describing WHY this
    # wallet was analyzed. Verbatim dict, no schema enforcement. The
    # analyzer never reads or interprets it. Default None when no
    # context was provided. Recommended (non-enforced) shape:
    #   {"source": "polymarket_public_leaderboard",
    #    "period": "MONTH" | "ALL_TIME" | "WEEK",
    #    "fetched_at": int,
    #    "rank": int,
    #    "lb_pnl_at_capture": float,
    #    "note": str}
    # Display-only; NEVER feeds verdict, gate, primary_edge_type, or
    # copyability_note.
    triage_context: dict | None = None

    def __post_init__(self):
        # Backward-compat invariant: `realized_pnl` mirrors `net_pnl`.
        # Legacy callers (existing test fixtures, replay paths) set
        # net_pnl only; new callers may set realized_pnl only; analyze_wallet
        # sets both to the same value. Whichever single field is set, the
        # other follows so consumers can read either name post-construction.
        if self.realized_pnl == 0.0 and self.net_pnl != 0.0:
            self.realized_pnl = self.net_pnl
        elif self.net_pnl == 0.0 and self.realized_pnl != 0.0:
            self.net_pnl = self.realized_pnl

        # A.4 invariant: combined_pnl == realized_pnl + unrealized_pnl.
        # When combined_pnl is still default 0.0 but either component is
        # non-zero, recompute from the components so legacy constructors
        # that set net_pnl or realized_pnl alone (or with unrealized_pnl)
        # produce a consistent combined view. analyze_wallet's path sets
        # combined_pnl explicitly to the same sum, so this is a no-op there.
        if self.combined_pnl == 0.0 and (
            self.realized_pnl != 0.0 or self.unrealized_pnl != 0.0
        ):
            self.combined_pnl = self.realized_pnl + self.unrealized_pnl


__all__ = [
    "PROTOCOL_VERSION",
    "VerdictLabel",
    "UnderpoweredReason",
    "OverallVerdict",
    "GateResult",
    "TradingMode",
    "HypothesisResult",
    "TagGrainResult",
    "StationarityResult",
    "TimeSplitResult",
    "IndependenceCheckResult",
    "BranchReport",
    "WalletData",
    "ProtocolReport",
    "CapitalLedger",
]
