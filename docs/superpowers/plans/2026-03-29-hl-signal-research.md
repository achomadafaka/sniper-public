# HL Signal Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible research pipeline that determines which combinations of Hyperliquid derivatives signals predict BTC/ETH/SOL price direction at horizons relevant to Polymarket binary crypto markets. Write findings to Supabase brain for LLM consumption.

**Spec:** `docs/superpowers/specs/2026-03-29-hl-signal-research-design.md`

**Architecture:** Two layers — core path (S1 OI, S2 funding, S3 momentum) always runs; optional-core path (S4 trade flow) joins if backfill passes quality gates.

---

## Phase 1: Data Backfill

### Task 1.1: Schema + backfill script for HL funding history
- [ ] Create `hl_funding_history` table: `(id, asset, timestamp_ms, funding_rate, premium, backfilled_at)`
- [ ] Write `scripts/backfill_hl_funding.py`:
  - POST `https://api.hyperliquid.xyz/info` with `{"type": "fundingHistory", "coin": X, "startTime": Y}`
  - Paginate backward in 500-hour chunks
  - Target: 6 months per asset (BTC, ETH, SOL)
  - Idempotent: skip existing timestamps
- [ ] Run on VPS, verify row counts and timestamp monotonicity
- [ ] Test: assert funding backfill produces >=4000 hourly rows per asset (6mo * 30d * 24h = 4320)

### Task 1.2: Schema + backfill script for Binance price history
- [ ] Create `price_history` table: `(id, asset, timestamp_ms, open, high, low, close, volume, venue)`
- [ ] Write `scripts/backfill_binance_klines.py`:
  - GET `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000`
  - Paginate with `startTime` parameter
  - Assets: BTCUSDT, ETHUSDT, SOLUSDT
  - Target: 6 months hourly OHLCV
- [ ] Run on VPS, verify row counts
- [ ] Test: assert price backfill produces >=4000 hourly rows per asset

### Task 1.3: Schema + backfill script for Coinalyze OI history
- [ ] Register for free Coinalyze API key
- [ ] Create `hl_oi_history` table: `(id, asset, timestamp_ms, open_interest, source)`
- [ ] Write `scripts/backfill_coinalyze_oi.py`:
  - GET `https://api.coinalyze.net/v1/open-interest-history`
  - Symbol mapping: BTC -> BTCUSDT.6 (Hyperliquid), ETH -> ETHUSDT.6, SOL -> SOLUSDT.6
  - Interval: daily (retained indefinitely)
  - Target: 6 months daily OI per asset
- [ ] Run on VPS, verify row counts
- [ ] Test: assert OI backfill produces >=180 daily rows per asset
- [ ] **Fallback:** If Coinalyze requires paid tier or blocks, use existing `hyperliquid_snapshots` filtered to `open_interest > 0`, with explicit gap audit logged in methodology

### Task 1.4: Attempt S4 trade flow backfill (optional-core gate)
- [ ] Probe HL S3 archive: `aws s3 ls s3://hl-mainnet-node-data/node_fills_by_block/ --request-payer requester` — check date range and file sizes
- [ ] Estimate total download cost for BTC/ETH/SOL fills over 6 months
- [ ] If cost <= $10 and data covers all 3 assets over 6 months:
  - [ ] Download and parse into `hl_trade_history` table: `(id, asset, side, size, price, timestamp_ms, user_addr)`
  - [ ] Verify >=95% hourly anchor coverage for all 3 assets in both train and test windows
  - [ ] **Promote S4 to core** — log promotion rationale
- [ ] If any gate fails:
  - [ ] Log the specific failure (cost, coverage gap, missing asset)
  - [ ] **Keep S4 exploratory** — use live `hl_trades` for appendix-only analysis
  - [ ] Document in methodology entry

### Task 1.5: Data validation report
- [ ] For each source: report asset, row count, date range, gap count, gap %
- [ ] Verify no timestamp duplicates within each table
- [ ] Verify all timestamps are UTC
- [ ] Log S4 promotion/demotion decision with evidence
- [ ] Commit validation report as `scripts/data_validation_report.txt`

---

## Phase 2: Canonical Dataset Builder

### Task 2.1: Hourly anchor dataset generator
- [ ] Write `scripts/build_research_dataset.py`:
  - Generate hourly UTC anchors from start of backfill to end
  - For each anchor H:
    - S1 (OI): forward-fill latest daily OI on or before H, compute ternary state vs previous day
    - S2 (Funding): latest completed hourly funding on or before H, compute ternary state
    - S3 (Momentum): trailing 4h return from Binance closes at H-4 to H, compute ternary state
    - S4 (Flow, if promoted): trailing 1h buy/sell volume ratio from hl_trade_history, compute ternary state
    - Forward labels: close-to-close return from H to H+1, H+4, H+24, H+72
  - Strict no-leak: all features use data at or before H; all labels use data after H
- [ ] Output: SQLite table `research_dataset` with columns: `anchor_ts, asset, s1_state, s2_state, s3_state, s4_state (nullable), ret_1h, ret_4h, ret_1d, ret_3d`
- [ ] Test: no-leak assertion — for each row, verify max feature timestamp <= anchor_ts and min label timestamp > anchor_ts
- [ ] Test: row count matches expected (hours in range * 3 assets)

### Task 2.2: Train/test split
- [ ] Define split: first 4 months = train, last 2 months = test
- [ ] Add `split` column to research_dataset: 'train' or 'test'
- [ ] Verify no data from test period leaks into train features

---

## Phase 3: Research Engine

### Task 3.1: Subset enumeration and cell grouping
- [ ] Write `scripts/run_signal_research.py`:
  - Enumerate all non-empty subsets of active core signals
  - For each subset, group research_dataset rows by joint state of included signals
  - Prune cells with < 20 train observations
- [ ] Test: with 3 core signals, verify 7 subsets enumerated; with 4, verify 15

### Task 3.2: Direction learning (train-only, frozen)
- [ ] For each surviving cell on train data:
  - Compute % of observations where forward return was positive
  - If >55%: label "bullish"
  - If <45% (i.e., >55% down): label "bearish"
  - Otherwise: label "no_signal"
- [ ] Freeze labels. Store in a lookup table: `(subset, joint_state, horizon) -> direction_label`
- [ ] Test: verify labels are computed from train data only

### Task 3.3: Non-overlapping sampling
- [ ] For each horizon, thin observations to non-overlapping windows:
  - 1h: one observation per hour (already the anchor frequency — no thinning needed)
  - 4h: one observation every 4 hours
  - 1d: one observation every 24 hours
  - 3d: one observation every 72 hours
- [ ] Apply thinning to both train and test splits
- [ ] Re-check minimum N after thinning; prune cells that drop below 20

### Task 3.4: Scoring and statistics
- [ ] For each surviving cell on test data:
  - Apply the frozen direction label
  - Compute directional accuracy (% correct)
  - Compute mean forward return, std, Sharpe-like ratio
  - Compute binomial test p-value (accuracy vs 50%)
- [ ] Apply Holm-Bonferroni correction across all core cells
- [ ] Flag cells that survive correction at alpha=0.05
- [ ] Compute PM-adjusted EV as secondary metric at entry prices 0.20, 0.30, 0.40
- [ ] Compute regime diversity: % of each cell's observations in rally/decline/consolidation

### Task 3.5: S4 exploratory appendix (if not promoted)
- [ ] If S4 was not promoted to core:
  - Use live `hl_trades` data (growing daily)
  - Once 30+ days available: run S4 standalone + S4 overlaid on top 3 core cells
  - Own train/test split, own significance test
  - Results written to separate appendix section
- [ ] If S4 was promoted: this task is skipped (already in core matrix)

---

## Phase 4: Brain Outputs

### Task 4.1: Write methodology entry
- [ ] Push to Supabase brain: `hl_signal_research_methodology`
- [ ] Include: data sources with coverage dates and row counts, signal definitions with exact thresholds, test procedure, S4 promotion decision and rationale, all parameters

### Task 4.2: Write results entry
- [ ] Push to Supabase brain: `hl_signal_research_results`
- [ ] Include: full results table (all surviving cells), top 10 by OOS Sharpe, top 10 by OOS accuracy, significance flags, regime breakdown, PM EV translation layer
- [ ] Clearly separate core results from S4 appendix results

### Task 4.3: Write conclusions entry
- [ ] Push to Supabase brain: `hl_signal_research_conclusions`
- [ ] Include: which signals have standalone power, which combinations survive correction, go/no-go for crypto_momentum_v1 HL gates, comparison to Presto Research, known limitations

### Task 4.4: Seed per-strategy knowledge bases
- [ ] Push to Supabase brain: `strategy_knowledge_hl_liquidation_v1`
  - Thesis, mechanism, live performance data, current status (canary), limitations (public signal, sparse isolated positions, 30-min time stops dominating)
- [ ] Push: `strategy_knowledge_crypto_momentum_v1`
  - Thesis, mechanism, phantom close bug history, clean cohort start date, current positions, research findings
- [ ] Push: `strategy_knowledge_insider_copy_v1`
  - Thesis (copy high-quality wallets), mechanism, paper trading performance (58.5% WR, +$558), scoring audit findings
- [ ] Push: `strategy_knowledge_political_no_value_v1`
  - Thesis (behavioral overpricing of dramatic events), mechanism, 80% WR in research, Le 2026 evidence

---

## Phase 5: Strategy Upgrade (conditional)

### Task 5.1: Bidirectional crypto_momentum_v1 (if research supports it)
- [ ] **Only execute if:** research conclusions recommend bearish signal gates with OOS significance
- [ ] Add negative momentum detection to `discover()`: when BTC momentum is negative, find "dip to" or "above" markets for NO-side entry
- [ ] Add entry gates: require the HL OI+funding regime that research identified as significant
- [ ] Paper-test only — no live execution changes
- [ ] Update `strategy_knowledge_crypto_momentum_v1` brain entry with the upgrade

---

## Verification Checklist

- [ ] All backfill scripts are idempotent (safe to re-run)
- [ ] No future-leak in dataset construction
- [ ] Train/test split is strictly time-ordered
- [ ] Direction labels frozen on train only
- [ ] Non-overlapping sampling applied per horizon
- [ ] Holm-Bonferroni applied to core family only (not exploratory S4)
- [ ] Brain entries written with exact names from spec
- [ ] S4 promotion/demotion documented with evidence
- [ ] All scripts committed to `scripts/` directory
- [ ] Full test suite remains green after all changes
