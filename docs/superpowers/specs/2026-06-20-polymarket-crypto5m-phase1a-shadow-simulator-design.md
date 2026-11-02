# Crypto-5m Phase 1A — Decision-Time Feature Spine + Shadow Taker Simulator — Design

**Date:** 2026-06-20
**Status:** building (autonomous; GPT Pro directive after Phase 0.5 merge)
**Branch:** `feat/polymarket-crypto5m-phase1a-shadow-sim` (worktree off `origin/main@5d744cd`; PR, not direct merge)
**Builds on:** the merged Phase 0/0.5 capture spine (`sniper/algos/polymarket_crypto5m/`).
**Scope:** the deterministic feature → fee → execution → model → decision → replay layer Phase 1 will need, built **now** while the collector accumulates the 200-window calibration dataset. **Shadow/paper only, fail-closed.**

## Hard guards (do not violate)
No live orders. No private-key/wallet. **No paper *entry* activates while `price_to_beat_resolver.LOCKED_BOUNDARY_RULE is None`** or while the model is uncalibrated — every decision is `blocked` or `paper_no_trade`. No final-model training, no edge claim. No maker fills mixed into taker results. No production DB; no services. Do **not** touch the 6 Codex UI files. Binance is a *predictor feature only* — never the label, never settlement truth.

## Instrument contract
Target: `P(Polymarket Up wins under the Chainlink settlement rule | information available at decision time)`.
Label: the final Polymarket winning token / Chainlink-rule outcome (from `crypto5m_settlements`). **Not** Binance candle direction.

## Known-at-time (the anti-lookahead rule)
Every feature is computed only from events with `source_ts <= feature_cutoff_ts` and, where the value had to *arrive*, `receive_ts <= decision_ts`. The Gamma post-hoc `priceToBeat` (settlement-time only) must **never** seed a decision-time feature — the decision-time Price-to-Beat comes from the RTDS reconstruction lane and carries `price_to_beat_known_ts`; a feature using it is only valid when `price_to_beat_known_ts <= decision_ts` (else `price_to_beat_arrived_after_decision`).

## Observation checkpoints (dataset construction, NOT the trade policy)
Snapshots at offsets after window start: `15,30,60,90,120,150,180,210,240,255,270,285` s. If Price-to-Beat was not known by a checkpoint, still write the row with `feature_status=boundary_unavailable_at_decision` (do not backfill). Never select the "best" checkpoint from the test period.

## Feature contract (`features.py`, `Crypto5mFeatureSnapshot`, versioned)
Lineage (every snapshot): condition_id, market_id, window_start/end_ts, decision_ts, feature_cutoff_ts, feature_version, price_to_beat, price_to_beat_quality, price_to_beat_known_ts, chainlink_last_source/receive_ts, binance_last_source/receive_ts, up_book_ts, down_book_ts, book_integrity_status, market_rule_hash, code_commit, feature_status.
Families: **A settlement geometry** (distance_to_beat, seconds_remaining, fraction_elapsed, normalized_distance_to_beat = `ln(chainlink_now/price_to_beat) / max(realized_vol_per_s·sqrt(seconds_remaining), eps)`), **B Chainlink short-horizon path** (returns/velocity/accel/realized_vol/intrawindow hi-lo-range over 5–120 s), **C Binance reframed** (returns/vol/volume_z/vwap_dist/ema_slope — reuse `btc5m/features.py` pure fns; never the spot PnL/fee engine; LONG/SHORT/FLAT only as `legacy_spot_signal_diagnostic`), **D Binance↔Chainlink basis** (basis_abs/bps, basis_change, staleness, direction agreement), **E Polymarket book per token** (best/mid/spread/depth/imbalance/last-trade + executable price/slippage for $10/$20 via the FOK engine — never derive one token from the other), **F market-implied baseline** (up/down midpoint probability — the benchmark; midpoint ≠ executable price). Insufficient data → field `null` + a feature-quality reason; never fabricate. Realized volatility is per-second log-return stdev; document units.

## Fee resolver (`fee_model.py`) — empirically validated, fail-closed
**Empirical finding (2026-06-20):** the market config says `feesEnabled=true, taker_base_fee=1000`, but realized `last_trade_price.fee_rate_bps = 0` on live BUY trades — the sources **conflict**. So: capture both the config (`taker_base_fee`, `feesEnabled`, `feeType`, from CLOB `/markets/<condition_id>`) and observed `fee_rate_bps`; persist raw + `decoder_version`. Documented formula `fee = shares · fee_rate · price · (1-price)`. `fee_status`: `known` only when config & observation agree (or only one authoritative source exists and decodes); `fee_conflict` when they disagree (the 0-vs-1000 case); `fee_unknown` when undecodable; `fee_stale` when the source is old. **Never** treat `taker_base_fee=1000` as a rate by assumption. A non-`known` fee blocks any paper entry.

## FOK taker fill engine (`taker_sim.py`)
Walk the **ask** ladder ascending for the side's own book. `cost_per_share = price + fee_rate·price·(1-price)` (decoded fee curve). Fill level-by-level until `gross_cost + taker_fee <= cash_budget` (default $10 inclusive of fee, configurable). **FOK:** the full budget must be executable within visible depth, else `insufficient_depth`. Record shares, gross_cost, taker_fee, total_cash_cost, depth_weighted_fill_price, fee_inclusive_average_cost, best_ask, worst_ask_level, slippage_vs_best_ask, levels_consumed, book_ts, book_age_ms, modeled_latency_ms. Reject: stale book, quarantined interval, missing ask depth, unknown/conflict fee, below min order, incomplete FOK, market closed. No maker. Up and Down books never cross-contaminate; result is independent of input ask ordering (sort internally).

## Probability model contract (`probability_model.py`)
`ProbabilityModel` Protocol → `ProbabilityPrediction(q_up, q_down=1-q_up, model_id, model_version, calibrated, training_cutoff_ts, feature_version, prediction_status, reason)`; `0<=q_up<=1`. **A `calibrated=false` model cannot create a paper entry.** Baselines (diagnostic only): `market_midpoint_baseline` (q_up=Up midpoint — the benchmark), `threshold_diffusion_v0` (driftless log-price terminal-above-threshold: `q_up = Φ(ln(price_to_beat/chainlink_now)/(vol_per_s·sqrt(seconds_remaining)))` sign-conventioned so flat/at-threshold→0.5; `calibrated=false`, `prediction_status=diagnostic_only`), `legacy_spot_signal_diagnostic` (carries the btc5m LONG/SHORT/FLAT as a diagnostic; never a probability).

## Shadow decision engine (`decision.py` / `shadow_simulator.py`)
Evaluate Up and Down **independently**. Per side: `expected_payout=q_side·shares`, `expected_net_pnl=expected_payout-total_cash_cost`, `expected_edge_per_share=q_side-fee_inclusive_average_cost`. Eligible iff `model.calibrated AND price_to_beat_quality in {exact_chainlink_report, validated_rtds_reconstruction} AND book_integrity_status==usable AND fee_status==known AND expected_edge_per_share>=min_edge`. Choose the eligible side with the larger expected net edge. Never use complement arithmetic. **While `LOCKED_BOUNDARY_RULE is None` → `decision_status=blocked, reason=boundary_rule_unvalidated`** (no entry). Diagnostic models → `blocked, reason=probability_uncalibrated`. Every checkpoint yields exactly one of `shadow_prediction | blocked_decision | paper_no_trade | paper_entry`; in Phase 1A the expected outcomes are `shadow_prediction` and `blocked_decision` only.

### Skip taxonomy (first-class)
`boundary_rule_unvalidated, boundary_unavailable_at_decision, price_to_beat_arrived_after_decision, probability_model_unavailable, probability_uncalibrated, feature_window_incomplete, feature_stale, market_rule_unsupported, market_not_capture_ready, book_missing, book_stale, book_quarantined, book_gap, fee_unknown, fee_conflict, spread_too_wide, insufficient_depth, below_min_order, edge_below_threshold, too_close_to_close, latency_book_missing, already_decided, settlement_conflict`.

## Persistence additions (dedicated crypto5m DB; idempotent; append-only; supersede-not-overwrite)
`crypto5m_feature_snapshots, crypto5m_fee_configs, crypto5m_model_predictions, crypto5m_shadow_decisions, crypto5m_paper_entries, crypto5m_paper_fills, crypto5m_paper_skips, crypto5m_paper_settlements, crypto5m_evaluation_runs`. Decision records retain condition_id, token_id, window_start_ts, checkpoint, decision_ts, feature_snapshot_id, prediction_id, book hash, fee_config_id, price_to_beat_quality, model_id/version, strategy_version, status, reason. Immutable decision inputs; version + code lineage on every row.

## Replay / evaluation (`scripts/run_polymarket_crypto5m_shadow_sim.py --mode replay`)
Reads **only** captured data (no live APIs). Materialize snapshots at fixed checkpoints → known-at-time → predict → FOK estimate → blocked/no-trade decision → reconcile final outcome → eval report. Latency: select the first usable book at/after `decision_ts + latency`. Settlement winner → exact realized paper PnL; settlement conflict → quarantine; never look ahead from the post-resolution Gamma Price-to-Beat. Flags: `--db-path --mode --model --checkpoint-policy --latency-ms --stake-usdc --min-edge --market-limit --window-start --window-end --dry-run` (no live-order flags). Report distinguishes data-availability / execution-feasibility / model-availability / boundary-readiness / paper-activation-readiness; **no strategy ROI from diagnostic or blocked decisions** (any shadow EV table is labeled "diagnostic, uncalibrated, not strategy performance").

## Activation gate (what unblocks paper entries — all required)
1. boundary rule passes calibration/holdout (`LOCKED_BOUNDARY_RULE` set); 2. feature data sufficiently complete; 3. an approved **calibrated** probability model exists; 4. fee status `known`. Until all four, the pipeline runs end-to-end but only emits `shadow_prediction`/`blocked_decision`.

## Out of scope (Phase 1A)
Maker simulation; the learned/calibrated model; live capture changes; the UI. Runtime: the existing `--continuous` collector accrues the dataset separately.
