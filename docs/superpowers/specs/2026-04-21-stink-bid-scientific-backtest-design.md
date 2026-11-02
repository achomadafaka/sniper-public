# Stink Bid Scientific Backtest — Design

**Date:** 2026-04-21
**Version:** v2 (amended same day after review)
**Status:** design approved; implementation begins with Phase 0
**Target strategy:** `stink_bid_v1` (strategy 41 on VPS)

---

## 1. Goal

Determine, with statistical rigor that matches published quantitative-finance practice, whether the `stink_bid_v1` tennis panic-dip strategy — evaluated in its IDEAL form (incorporating fixes identified during the design review) — has positive expected value on held-out historical data, and if so, identify its optimal parameters.

Either outcome is useful. A rigorous negative result retires a made-up strategy. A rigorous positive result deploys with a confidence interval as the performance guide, deflated for selection bias.

## 2. Non-goals

- Not building a new replay engine. The existing `replay_engine.py` + `replay/adapters.py` adapter pattern is reused.
- Not backtesting any wallet-follow strategy (insider copy, scout). Separate track.
- Not classifying wallets into archetypes. That's the analysis pipeline — separate track.
- Not generalizing the backtest framework to other market-pattern strategies yet. Stink-bid is the first concrete build. Generalization happens after, if shape repeats.
- Not testing against real money. Decision output informs whether to deploy real money; this spec produces the evidence, not the deployment.
- Not running dog-side (NO-side) bids in v1. Adapter is architecturally neutral so dog-side is a single flag flip in v2. See section 13.

## 3. Background

`stink_bid_v1` was deployed with 100% default parameters:
- `dip_pct = 0.30` — guess
- `min_favorite = 0.55` — guess
- `min_bid = 0.05` — guess
- `min_volume_24h = 10000` — guess
- `max_resting = 3` — guess
- `max_daily_fills = 5` — guess
- `discovery cycle = 15 min` — guess

Zero rows in `strategy_research`. Empty `ai_analysis`. No prior backtest. Thesis ("favorites panic-dip in tennis matches and recover") is folk wisdom. Since deploy: 4 orders placed, 0 filled.

Design review (2026-04-21 brainstorming session) identified implementation defects in addition to parameter defaults:
- ESPN gate cancels indiscriminately at `sets_completed ≥ 2` instead of only when favorite is behind
- Reprice logic updates bid both directions (chases favorite's price up, off-thesis)
- No favorite-switch handling (thesis requires locking pre-match favorite)
- No TTL on resting orders
- Hardcoded `max_daily_fills` and `max_resting` without evidence they help
- Exit triggers on 0.97/0.03 price threshold instead of ESPN-confirmed resolution

This backtest evaluates the strategy in its IDEAL form (all fixes applied), not its currently-deployed buggy form. Rationale: "would this strategy have edge at its best?" is the decision-relevant question. If IDEAL fails, current-buggy definitely fails.

Prediction markets are generally efficient (Wolfers & Zitzewitz 2004). Genuine edge requires specific identifiable mispricings. Our thesis posits one such mispricing — sub-minute to sub-hour overreaction during live matches — and this backtest is the falsifier.

## 4. Hypothesis (pre-registered)

### 4.1 Null and alternative

- **H0:** `stink_bid_v1` in IDEAL form has expected value ≤ economic-significance threshold per attempted market, net of realistic costs, on held-out test-set tennis markets.
- **H1:** `stink_bid_v1` in IDEAL form has expected value > economic-significance threshold per attempted market, net of realistic costs, on held-out test-set tennis markets, with deflated lower 95% bootstrap CI > 0.

### 4.2 Primary metric

Expected value per attempted market, computed as:
```
EV_per_attempt = P(fill) × E[net_pnl | fill] + P(no_fill) × 0
```

Reported with:
- **Raw 95% bootstrap CI** from 1000 resamples (standard)
- **Deflated 95% CI** adjusted for selection bias across the 388,800-config grid (see section 9, per Bailey-López de Prado framework)

Decision uses the deflated CI.

### 4.3 Secondary metrics

- Fill rate per attempted market
- Win rate conditional on fill
- Average time-in-position
- Max drawdown over simulated portfolio
- Sharpe-equivalent (mean net PnL / std net PnL per market)

### 4.4 Economic significance threshold

Separate from statistical significance. Per Harvey & Liu (2015), a strategy can be statistically significant but economically trivial.

**Threshold:** EV per attempted market ≥ **2× modeled transaction costs** (bid-ask spread + fees + gas). Details in Phase 0.1 and 0.2.

**Pre-registered H1 effect size: 4pp EV per attempted market** (amended 2026-04-22 from original 2pp). Rationale from Phase 0 feasibility memo section 5: Phase 0.1 established realistic round-trip transaction cost ≈ 1% of notional (half-spread on market-sell exit; maker-in entry is 0% on Polymarket). 2× that gives a 2% economic threshold; raising H1 to 4pp keeps the effect comfortably above the 2× cost rule under the conservative cost path (~2.3% round-trip) and provides margin against spread widening in out-of-sample windows. Required per-stratum N at 4pp, σ=0.10, α=0.0125 (Bonferroni-4), power=0.80: **60 samples**, well-cleared by the 5,840 H2H corpus from Phase 0.4.

### 4.5 Decision rule

Accept H1 only if ALL of the following hold on the UNMODIFIED test set:

1. Finalist's **deflated lower 95% CI is strictly > 0**
2. Finalist's **EV ≥ economic-significance threshold** (2× transaction costs)
3. Finalist's EV **survives BH-FDR correction** across the 388,800-config sweep at q = 0.05
4. Finalist **statistically beats random-entry baseline** at p < 0.05 using block bootstrap that resamples across strategies (White 2000 Reality Check style, not naive t-test)
5. **Walk-forward stability:** edge does not show catastrophic decline across 6-month rolling windows (most recent window's EV is within 50% of training-period EV, or the most-recent window's CI lower bound still exceeds economic threshold)

If any of 1-5 fails: reject H1, strategy retired.

## 5. Strategy under test — IDEAL rules

### 5.0 Preamble: IDEAL vs current-live

The rules below are the TARGET implementation. They incorporate defects identified in design review that are known-wrong in the live code. The backtest runs the IDEAL rules. The production code requires a separate deploy task (gate-rewrite spec from prior session, plus amendments listed here) to reach parity.

Deviations from current live implementation are marked [IDEAL] below.

### 5.1 Market scope

- Tennis head-to-head (H2H) binary markets with exactly 2 outcomes
- Filter: market passes `min_volume_24h` threshold at discovery time
- Filter: no existing resting order for this market from the same strategy
- Filter: favorite_price (pre-match) ≥ `min_favorite`

### 5.2 Entry

At each discovery-cycle tick (parameter: `cycle_seconds`):

1. For markets passing filters in section 5.1, **[IDEAL]** identify favorite outcome from Gamma `outcomePrices` **at or before market start time** (pre-match favorite). This token is the "entry favorite" and is immutable for the lifetime of any resting order on this market.
2. Compute bid: `bid_price = max(fav_price_now × (1 - dip_pct), min_bid)` where `fav_price_now` is the current price of the entry_favorite_token (not the dynamically-evaluated favorite).
3. Place resting buy order on entry_favorite_token at `bid_price`, size `position_size_usdc`, `min_fill_size` floor.

**[IDEAL]** — entry favorite is locked pre-match. If the dog pulls ahead mid-match and becomes the market's "favorite" by price, our bid remains on the pre-match favorite (which is now under-priced — that's the panic-dip opportunity, not a reason to switch).

### 5.3 Fill

Resting bid fills when:
- A sell trade occurs on the entry_favorite_token at price ≤ bid_price, AND
- Trade size ≥ min_fill_size

Modeled via Polymarket `/trades` endpoint cross-reference. `/prices-history` alone can show the price crossing our bid level without an actual sell event of sufficient size occurring — this is the optimistic bias Fidelity item #1 addresses.

Queue position: we assume first-in-queue at our price (optimistic). Bid-ask spread applied as pessimistic adjustment (see Fidelity item #1).

### 5.4 Reprice

At every discovery-cycle tick, for each resting order:

- Recompute `new_bid_price = max(current_fav_price × (1 - dip_pct), min_bid)`
- **[IDEAL] Down-only rule:** if `new_bid_price < current_bid AND |new_bid_price - current_bid| ≥ reprice_threshold`, cancel and replace at new bid
- If `new_bid_price > current_bid` (favorite price rose), leave bid unchanged — don't chase the favorite upward (off-thesis)

### 5.5 Cancel triggers

- **[IDEAL] ESPN gate:** `sets_completed ≥ 2 AND favorite_behind_in_sets` (per gate-rewrite spec). Not the indiscriminate `sets_completed ≥ 2` of current live code.
- **[IDEAL] Match status terminal:** `match_status ∈ {final, postponed, suspended}` (ESPN-confirmed) → cancel regardless of sets.
- **[IDEAL] Position TTL:** `current_ts - market_start_ts > max_position_duration_min` (parameterized; may be `none`) → cancel.
- **Outage:** ESPN feed down ≥ `espn_pause_after` cycles → pause new orders (keep existing bids).
- **[IDEAL] max_daily_fills:** parameterized (may be `none`). If set and reached: skip new placements, don't cancel existing bids.

**[IDEAL]** — `max_daily_fills` and `max_resting` are now parameters in the grid, not constants. The backtest tells us if they add value.

### 5.6 Exit

On filled position:

- **[IDEAL]** If `match_status == "final"` (ESPN-confirmed), close position at `resolution_price` (0.0 or 1.0 once market settles on Polymarket). Don't rely on 0.97/0.03 price-threshold crossings.
- **[IDEAL]** If ESPN data unavailable: fall back to price-threshold (win at ≥ 0.97, loss at ≤ 0.03).
- Alternative exit policies parameterized (see section 9): partial-exit at threshold, trailing stop, time cutoff.

### 5.7 PnL

```
shares_acquired = position_size_usdc / fill_price
gross_exit_notional = shares_acquired × exit_price
gross_pnl = gross_exit_notional - position_size_usdc
entry_fees = position_size_usdc × entry_fee_rate
exit_fees = gross_exit_notional × exit_fee_rate
spread_cost = position_size_usdc × modeled_bid_ask_half_spread   # applied to both legs
total_costs = entry_fees + exit_fees + 2 × spread_cost + gas_cost
net_pnl = gross_pnl - total_costs
EV_per_attempted = P(fill) × net_pnl_if_filled
```

Where `entry_fee_rate`, `exit_fee_rate`, `modeled_bid_ask_half_spread`, `gas_cost` are per-epoch constants from Phase 0.1 research.

## 6. Fidelity issues and locked decisions

| # | Issue | Decision |
|---|---|---|
| 1 | **Fill realism + bid-ask spread.** `/prices-history` gives mid or last-trade price. A price dip to our bid level doesn't guarantee fill (queue position, available sell-side depth, min_fill_size). | Cross-reference price dips against `/trades` endpoint. Bid fills only if a sell trade occurred at ≤ `bid_price` with `size ≥ min_fill_size` within the dip window. Apply `modeled_bid_ask_half_spread` as pessimistic haircut (Frazzini-Israel-Moskowitz 2012). Document queue-first assumption as optimistic bias. |
| 2 | **Discovery cycle timing** — parameterized. | `cycle_seconds ∈ {60, 300, 900}`. Bid exists in simulation only from nearest cycle tick at or after entry criteria first met. cycle_seconds=30 removed (amended 2026-04-22): Phase 0.5 confirmed `/prices-history` minimum observable timescale is 60s, so sub-minute decisions cannot be simulated faithfully. |
| 3 | **Price granularity** — `/prices-history?fidelity=60` gives 1-min bars. | Phase 0.5 verifies whether `fidelity=1` (1-sec) is available. If yes, use it. If no, accept 1-min and document bias: sub-minute dips missed. |
| 4 | **Reprice behavior** — down-only, tracks locked entry_favorite_token. | Simulated at every cycle tick. `new_bid > current_bid` → no update. Models red-team fix from design review. |
| 5 | **ESPN gate simulation** | **Primary (c):** Jeff Sackmann's `tennis_atp` / `tennis_wta` for set scores + match duration. Align via duration-based timing proxy (uniform per-set split; refined via known set-length in games if available). Gate fires when `sets_completed ≥ 2 AND favorite_behind_in_sets`. **Sanity (b):** price-proxy run in parallel (cancel when fav price dropped X% from entry for > Y minutes). If (b) and (c) diverge substantially on the same markets, investigate before trusting either. |
| 6 | **Fees** — per-epoch accurate. | Phase 0.1 pulls Polymarket fee schedule across history (historical 2% → current 0%). Modeled as per-timestamp lookup. No flat haircut. |
| 7 | **Survivorship** — historical market universe not from local DB. | Pull from Polymarket Gamma `/events?closed=true&tag=tennis&limit=500` paginated. Cross-reference with Sackmann for match-occurrence validation. `market_metadata` only as sanity cross-reference. |
| 8 | **Look-ahead bias** — strict. | Every feature used at `decision_ts` must derive from data with `timestamp ≤ decision_ts`. Enforced via decorator on feature functions; raises if any input has a later timestamp. Look-ahead becomes a caught-in-test bug. |
| 9 | **Statistical rigor** | Chronological train 60% / validate 20% / test 20% split. Sweep on train. Finalists chosen on val. Single test-set evaluation. Bootstrap 1000× CI. BH-FDR correction q=0.05 within each stratum. Bonferroni factor 4 across strata. Deflated-EV CI per Bailey-López de Prado. Random-entry baseline via block bootstrap (White 2000). |
| 10 | **Sample size + stratification** | Strata: ATP main, WTA main, Challenger/Qualifier, ALL. Stratum-level results reported. Power analysis in Phase 0.6 confirms each stratum has N sufficient to detect **4pp** EV at 80% power, α=0.0125 (Bonferroni-4). Required N per stratum at σ=0.10: 60 samples (4pp effect); 238 samples if falling back to 2pp effect. Phase 0.4 corpus of 5,840 H2H events across ~3,420 ATP / ~1,916 WTA / ~588 Slam-level clears either threshold with substantial margin. |
| 11 | **ESPN-confirmed resolution** | When Sackmann confirms match outcome, close position at actual resolution price (0 or 1 once market settles). Fallback to 0.97/0.03 price-threshold only when Sackmann data missing. Eliminates the "position held forever because price never crossed threshold" edge case. |
| 12 | **Selection-bias deflation** | Finalist's measured CI is the single-best of 388,800 tested configs; even unbiased per-config estimation produces an upward-biased winner. Apply Deflated Sharpe Ratio analog (Bailey-López de Prado 2014) to the finalist's CI. Decision uses deflated, not raw. |

## 7. Locked discipline rules (non-negotiable)

These rules exist because they're what distinguishes actual science from p-hacked vibes:

1. **Test set is sacred.** If finalist fails on test set, strategy is retired. No retuning. No "let me try one more thing." No exceptions.
2. **Pre-registration.** Parameter grid, strata, hypotheses, decision rule are locked in this document before any data is pulled. Changes after data access require a dated amendment and full restart.
3. **Multiple-testing correction always applied.** Every report of a "best" value across a grid must show BH-FDR-corrected significance.
4. **Deflation always applied.** Finalist's reported CI on test set must be the deflated CI (Bailey-López de Prado) alongside the raw CI. Decision uses deflated.
5. **Economic significance separate from statistical.** Finalist must meet both: lower deflated CI > 0 AND EV ≥ 2× modeled transaction costs.
6. **Random baseline always reported.** Finalist must beat random-entry baseline via block bootstrap (White 2000), not just beat zero.
7. **No post-hoc stratification.** Strata are listed in section 10 and fixed. Finding an interesting subset and reporting on it post-hoc is forbidden (observations are labeled as descriptive, not inferential).
8. **Reproducibility.** All RNG seeded. Dataset snapshot versioned via DuckDB export + commit hash. Backtest re-runnable from the repo at any time.
9. **Entry favorite is pre-match, never re-evaluated.** Determined from Gamma `outcomePrices` before match start. Matches the strategy's thesis that pre-match favorites panic-dip and recover.
10. **Walk-forward stress test required.** Phase 5 produces rolling 6-month-train / 3-month-test analysis. If edge declines materially across windows, deploy decision uses most-recent window's CI, not full-period.

## 8. Data sources

**Primary:**
- Polymarket Gamma API `/events?closed=true&tag=tennis` — historical market universe
- Polymarket CLOB `/prices-history` — per-token price trajectories
- Polymarket Data API `/trades` — trade-level data for fill validation (fidelity item #1)
- Jeff Sackmann `tennis_atp` + `tennis_wta` GitHub repos — match results, set scores, durations, surface, tournament level

**Reference only:**
- Local `market_metadata` — cross-reference sanity, never primary

**Storage:**
- Local DuckDB database under `data/backtest/stink_bid/` — versioned snapshot
- Raw API responses cached locally to avoid re-fetching during parameter sweeps
- Git commit hash tagged against each DuckDB snapshot for reproducibility

## 9. Parameter space to sweep

Every parameter that interacts with the decision function (which markets get a bid, when bids are placed, when they fill, when they cancel, when they exit) is in the grid. Parameters that are orthogonal to decision logic (pure scalar multipliers) are fixed with an explicit justification.

### 9.1 Full grid (decision-affecting parameters)

| Parameter | Grid | Count |
|---|---|---|
| `dip_pct` | {0.15, 0.18, 0.22, 0.25, 0.30} | 5 |
| `cycle_seconds` | {60, 300, 900} | 3 |
| `min_favorite` | {0.55, 0.60, 0.65, 0.70} | 4 |
| `min_volume_24h` | {1000, 5000, 10000, 25000} | 4 |
| `exit_policy` | {hold_to_resolution, partial_30pct, partial_50pct, trailing_20pct, time_180min} | 5 |
| `max_resting` | {none, 3, 10} | 3 |
| `max_daily_fills` | {none, 5, 10} | 3 |
| `max_position_duration_min` | {none, 120, 180, 240} | 4 |
| `phase_restriction` | {all, skip_set1, set3_only} | 3 |
| `position_size_usdc` | {10, 25, 50} | 3 |

Total configs per stratum: 5 × 3 × 4 × 4 × 5 × 3 × 3 × 4 × 3 × 3 = **388,800** (amended 2026-04-22; cycle_seconds dropped from 4 values to 3 per Phase 0.5 memo — see section 6 fidelity #2).

Strata: ATP / WTA / Challenger / ALL = 4 runs

Total test-set evaluations: **1,555,200** configs

### 9.2 Parameters fixed with justification

- **`min_bid = 0.05`** — structurally inactive given the other grid ranges. For the most extreme combination in the grid (lowest `min_favorite = 0.55`, highest `dip_pct = 0.30`), the bid is `0.55 × (1 - 0.30) = 0.385`, well above any reasonable min_bid floor. The `min_bid` constraint never fires in our grid space. Testing its effect would be testing on a region of parameter space we never enter.
- **`reprice_threshold = 0.02`** — controls bid churn sensitivity. Likely matters for PnL via transaction-cost accumulation but the interaction is second-order. Sensitivity check in Phase 5 as an ablation at the finalist config only. Documented limitation.

### 9.3 Correction

BH-FDR at q=0.05 within each 388,800-config stratum. Bonferroni factor 4 across the 4 strata (adjusted α = 0.0125).

## 10. Phase structure

### Phase 0 — Feasibility research

**Output:** `docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md`

- 0.1 Polymarket fee schedule — current + historical epochs, with dates and rates. Also: bid-ask spread typical values in tennis markets (sample 10 live markets, observe spread).
- 0.2 Kalshi comparison — tennis contract availability, fee structure, liquidity, decision (Polymarket or Kalshi as venue for v1 — likely Polymarket given current 0% fees)
- 0.3 Sackmann data audit — ATP + WTA coverage 2023-2026, set-score completeness, duration completeness, surface/tournament-level fields, alignment-test with 10 sample Polymarket markets
- 0.4 Polymarket Gamma tennis market universe — paginate `/events?closed=true&tag=tennis`, count markets by year / tour, confirm field completeness (condition_id, endDate, resolution, outcomePrices, outcomes)
- 0.5 `/prices-history` depth audit — 10 sample historical markets, confirm data availability + granularity + any gaps. Test whether `fidelity=1` (1-sec) is available.
- 0.6 Power analysis — given counts from 0.3/0.4, compute required N per stratum to detect 4pp EV at 80% power, α=0.0125 (Bonferroni-4). (Amended 2026-04-22 from 2pp; 2pp retained as a secondary falsifiability check against any finalist whose observed EV lies in the 2pp-4pp gap.)
- 0.7 Min-viable position size — given modeled fees + spread from 0.1, compute break-even position_size per EV level

Memo output: go/no-go on scientific feasibility at target fidelity.

### Phase 1 — Data pipeline

- 1.1 `PolymarketClient.get_prices_series(token_id, start_ts, end_ts, fidelity)` — bulk prices-history, extracted from `scripts/backtest_sports_2025.py:317`
- 1.2 `PolymarketClient.get_trades_window(condition_id, start_ts, end_ts)` — new method for fill validation
- 1.3 Gamma tennis market paginator — `get_resolved_tennis_markets(since_ts)` — pull full historical universe
- 1.4 Sackmann fetcher + parser — clone repos, parse ATP/WTA CSVs into DuckDB-friendly schema
- 1.5 Market-to-Sackmann alignment — match Polymarket markets to Sackmann records via player names + dates; log alignment success rate (target ≥ 90%)
- 1.6 DuckDB schema under `data/backtest/stink_bid/` — versioned, commit-hash tagged
- 1.7 Idempotent backfill script — re-runnable, skips already-cached data
- 1.8 Tests for each fetcher

### Phase 2 — Strategy specification

- 2.1 Exhaustive rules document — section 5 expanded into per-branch reference implementation pseudocode
- 2.2 Cross-check with live code — every rule traced to a line in `stink_bid_runner.py` / `strategies/stink_bid_v1.py` / `paper_limit_engine.py`. Mark IDEAL deviations explicitly.
- 2.3 Edge case enumeration — retirement, walkover, tiebreak mid-set, name-mismatch (Sackmann vs Polymarket), incomplete Sackmann record, favorite-switch scenario (confirming entry-favorite lock prevents the edge case)

### Phase 3 — Replay adapter

- 3.1 `MarketPatternReplayAdapter` subclass in `replay/adapters.py` with `side` flag (default "fav", prepared for v2 "dog")
- 3.2 Fill model — price-cross + `/trades` validation + spread haircut (fidelity #1)
- 3.3 Timing model — discovery cycle ticks (fidelity #2)
- 3.4 Reprice model — down-only (fidelity #4)
- 3.5 ESPN gate simulation — Sackmann-based primary (fidelity #5c), price-proxy secondary
- 3.6 Match-resolution handling — ESPN-confirmed when available (fidelity #11)
- 3.7 Fee + spread application — per-epoch lookup (fidelity #6)
- 3.8 Look-ahead audit decorator (fidelity #8)
- 3.9 Register adapter via `get_replay_adapter_for_strategy("stink_bid_v1")`
- 3.10 Tests: known-input-known-output, look-ahead-detection, edge cases, entry-favorite-lock behavior

### Phase 4 — Backtest methodology

- 4.1 Chronological train/val/test split implementation (60/20/20)
- 4.2 Parameter sweep runner (parallel, DuckDB persisted)
- 4.3 Bootstrap CI computation (1000 resamples)
- 4.4 BH-FDR correction within stratum
- 4.5 Bonferroni correction across 4 strata
- 4.6 Deflated EV CI per Bailey-López de Prado 2014
- 4.7 Random-entry baseline — same markets, entry at random `dip_pct` and random time-in-match. Block bootstrap for finalist-vs-baseline comparison (White 2000).
- 4.8 Stratification analysis — ATP / WTA / Challenger / ALL
- 4.9 Report template — auto-generates markdown from results with all CIs, p-values, corrections applied

### Phase 5 — Execution, stress test, and decision

- 5.1 Run full sweep on train set
- 5.2 Pick finalists on validate set (top-3 per stratum)
- 5.3 Single test-set evaluation on finalists only
- 5.4 **Walk-forward stress test:** rolling 6-month-train / 3-month-test windows across full available history. Report edge trajectory.
- 5.5 Post-hoc descriptive stratification — match phase, surface, tour, favorite-band quintile, time period. Descriptive only, not inferential.
- 5.6 Generate report `docs/superpowers/research/2026-04-21-stink-bid-backtest-results.md`
- 5.7 Decision per rule 4.5: deploy tuned / retire / inconclusive-and-need-more-data

## 11. Risks and open questions

- **Sackmann alignment accuracy.** Names in Polymarket (`Tatjana Maria vs Laura Samson`) must map to Sackmann records. Unicode, nicknames, retirement mid-tournament edge cases. Audit in Phase 0.3; mitigation if < 90% match rate is to expand to additional sources (e.g. ATP/WTA official rosters).
- **Polymarket price history depth.** If `/prices-history` only goes back a few months, sample size may be insufficient for stratified analysis. Phase 0.5 determines this. Mitigation: expand to Kalshi if Polymarket depth is too shallow, or reduce stratification granularity.
- **Fill model optimism.** Assuming first-in-queue at our price is optimistic. Real production may fill at lower rate than replay suggests. Mitigation: Phase 5 produces a fill-rate prediction for the currently-deployed config; live fills accumulate (currently 0/4) provide calibration data; if predicted fill rate » observed fill rate after 20+ live attempts, raise the pessimism haircut and re-evaluate.
- **ESPN gate proxy error.** Sackmann duration-based timing has ±5min uncertainty per set transition. Mitigation: cross-check with price-proxy (option b) in parallel. Disagreement surfaces the problem.
- **Declining edge over time.** Polymarket efficiency has improved; early-period edge may not persist. Mitigation: walk-forward analysis in Phase 5.4. Decision rule 4.5.5 explicitly adjusts for this — if most-recent window's CI lower is below economic threshold even though full-period is above, strategy is retired.
- **Zero-finding overreaction.** If H1 fails but edge exists at sub-threshold levels, strategy is retired even though minor tuning might have helped. This is intentional. Alternative strategy candidates (from wallet analysis pipeline, other market patterns) have higher expected return than polishing a marginal one.
- **Selection bias despite deflation.** Deflated CI adjusts for grid size but cannot fully eliminate the upward bias of "best of 518k." Only a second independent sample (more time, more markets) fully resolves this. If edge measured is borderline, recommend 3-6 months of live paper trading at finalist config before real-money commit — adds independent validation.

## 12. Success criteria for this spec

This spec is complete when:

1. Phase 0 memo confirms feasibility at target fidelity OR recommends a documented scope reduction
2. Phases 1-5 produce a decision (accept H1, reject H1, or inconclusive with documented reason)
3. Decision is backed by a reproducible analysis stored in the repo (DuckDB snapshot + commit hash)
4. All decision-rule 4.5 criteria explicitly reported on the finalist
5. Walk-forward edge trajectory explicitly reported
6. If accept H1: tuned parameters deployed to strategy 41 with real-money budget capped at deflated lower CI level
7. If reject H1: strategy 41 retired, learnings captured in `strategy_research` for future strategy-design work

## 13. Directional asymmetry (architectural prep for v2)

Strategy thesis ("markets overreact and recover") is directionally symmetric. If fav-side has edge, dog-side has mirror-image edge. v1 tests fav-side only.

Architectural requirements to keep v2 cheap:

- `MarketPatternReplayAdapter` takes a `side ∈ {fav, dog}` parameter. Default `fav`.
- Fill model and exit logic are symmetric with respect to side.
- Data layer methods (prices-series, trades-window, Sackmann alignment) are side-agnostic.
- v1 runs only `side=fav`. v2 adds `side=dog` run. Direct comparison of measured edges across sides is part of v2 scope.

Corollary: v2 becomes a much faster build because infrastructure exists.

## 14. References and guidelines

Primary methodology references:

- **Bailey, D.H., Borwein, J.M., López de Prado, M., Zhu, Q.J. (2014).** "The Probability of Backtest Overfitting." *Journal of Computational Finance*. Source of Deflated Sharpe Ratio / selection-bias correction applied in section 4.2 and fidelity #12.
- **López de Prado, M. (2018).** "Advances in Financial Machine Learning." Wiley. General reference for quant ML methodology; Chapter 7 (Cross-Validation) and Chapter 8 (Feature Importance) are specifically relevant.
- **Harvey, C.R., Liu, Y., Zhu, H. (2016).** "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1). Argues for stricter t-stat thresholds when selecting from many strategies. Motivates our deflation requirement.
- **Harvey, C.R., Liu, Y. (2015).** "Backtesting." *Journal of Portfolio Management* 42(1). Source of economic-significance-threshold separation (section 4.4).
- **White, H. (2000).** "A Reality Check for Data Snooping." *Econometrica* 68(5). Source of block-bootstrap comparison for finalist-vs-baseline (section 4.5.4).
- **Hansen, P.R. (2005).** "A Test for Superior Predictive Ability." *Journal of Business & Economic Statistics* 23(4). Alternative/complement to Reality Check.
- **Benjamini, Y., Hochberg, Y. (1995).** "Controlling the False Discovery Rate." *JRSS B* 57(1). Source of BH-FDR correction applied across 388,800-config grid.

Prediction-market and market-efficiency priors:

- **Wolfers, J., Zitzewitz, E. (2004).** "Prediction Markets." *Journal of Economic Perspectives* 18(2). Establishes baseline efficiency of prediction markets; our thesis must survive as a specific mispricing against this prior.

Transaction costs:

- **Frazzini, A., Israel, R., Moskowitz, T.J. (2012).** "Trading Costs of Asset Pricing Anomalies." AQR Capital / NBER. Empirical evidence that many academic edges disappear after realistic cost modeling. Motivates fidelity #1 (spread haircut) and #6 (per-epoch fees).

Reproducibility:

- **Nosek, B.A. et al (Center for Open Science).** Pre-registration protocols. Section 7 rules 1-2 follow this framework.
- **Wilkinson, M.D. et al (2016).** "The FAIR Guiding Principles for scientific data management and stewardship." *Scientific Data* 3. DuckDB-snapshot + commit-hash versioning follows FAIR.
- **Ioannidis, J.P.A. (2005).** "Why Most Published Research Findings Are False." *PLoS Medicine* 2(8). Motivates section 7 discipline as a whole.

## 15. Locked resume point

Next action when execution resumes: **Phase 0, starting with 0.1 (Polymarket fee schedule + bid-ask spread research).**
