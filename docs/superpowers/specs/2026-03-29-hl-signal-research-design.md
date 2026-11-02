# HL Derivatives Signal Research — Spec

## Purpose

Determine which combinations of Hyperliquid derivatives signals predict BTC/ETH/SOL price direction at horizons relevant to Polymarket binary crypto markets (1h to 3d). Output: an LLM-optimized research document in Supabase brain with full methodology, results per combination, and actionable conclusions for strategy gating.

## Scope

**Assets:** BTC, ETH, SOL
**Venue data source:** Hyperliquid (perpetual futures)
**Venue execution target:** Polymarket (binary crypto markets)
**Output:** Supabase brain entries (LLM-readable, not human-presentation)

## Data Acquisition

### Source 1: HL Funding Rate History (free, no auth)
- API: `POST https://api.hyperliquid.xyz/info` with `{"type": "fundingHistory", "coin": "BTC", "startTime": <ms>}`
- Returns hourly funding rates. Max 500 hours/request. Paginate backward.
- Target: 6 months of history per asset (BTC, ETH, SOL)
- Store in: new `hl_funding_history` table (separate from live snapshots)
- Fields: asset, funding_rate, premium, timestamp_ms

### Source 2: HL Open Interest History (Coinalyze API, free tier)
- API: `https://api.coinalyze.net/v1/open-interest-history` — requires free API key
- Daily data retained indefinitely. Intraday (5min) retained ~1500 bars.
- Target: 6 months daily OI for BTC, ETH, SOL on Hyperliquid specifically
- Store in: new `hl_oi_history` table
- **Note on existing `hyperliquid_snapshots`:** 40% of rows have `funding_rate=0 AND open_interest=0` (WS mid-price-only rows). This table is a last-resort gap filler, not a reliable OI source. Any use must first filter to `open_interest > 0` rows only and audit for coverage gaps.

### Source 3: Price History (Binance klines)
- HL does not provide historical mark price candles via API. The `asset_ctxs` archive is monthly, may have gaps, and has requester-pays transfer.
- **Primary source: Binance klines.** Free, reliable, 1-min to 1-month granularity.
- API: `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000`
- Target: 6 months hourly OHLCV for BTCUSDT, ETHUSDT, SOLUSDT
- Store in: new `price_history` table
- **Venue basis risk acknowledged:** Binance spot != HL perp mark price. Typical basis is <0.1% for BTC/ETH. Acceptable for 1h+ horizons. Not suitable for sub-5-min analysis.

### Source 4: Trade Flow (optional-core, backfill attempted)
- Our `hl_trades` table has ~24 hours of live data. Will grow as WS feed runs.
- **Backfill sources (prioritized):**
  1. HL S3 archive: `s3://hl-mainnet-node-data/node_fills_by_block` — requester-pays (~$0.09/GB)
  2. Dwellir cleaned tick data (if free tier available)
  3. HypeRPC `hl_getBatchBlocks` (paginated, free tier)
- **Promotion rule:** S4 joins the core signal matrix only if backfill yields all three assets (BTC/ETH/SOL), covers the full 6-month window, aligns to hourly UTC anchors with >=95% coverage, and total cost <= $10. Otherwise S4 remains exploratory with forward-only data.
- Store in: `hl_trade_history` table if promoted; use live `hl_trades` for exploratory-only.

### Removed from v1: Liquidation Proximity
- Our `hl_whale_positions` is a current-state snapshot (349 rows from one poll cycle), not a time series.
- Moon Dev API provides historical proximity snapshots but requires a paid key.
- **Decision:** Liquidation proximity (S3 in prior draft) is removed from the v1 signal matrix. If Moon Dev access becomes available or our own poller accumulates 30+ days of history, it can be added as v2.

## Signal Definitions

### Core Signals (v1 hypothesis family — backfillable, train/test validated)

**S1: OI Regime (ternary)**
- `oi_rising`: daily OI increased >0.5% vs previous day
- `oi_falling`: daily OI decreased >0.5% vs previous day
- `oi_flat`: change within +/-0.5%

**S2: Funding Rate (ternary)**
- `funding_positive`: 8h-equivalent rate > +0.005%
- `funding_negative`: 8h-equivalent rate < -0.005%
- `funding_neutral`: within +/-0.005%
- Note: extreme funding (>0.05%) tracked as metadata but not a separate state to avoid sparse cells.

**S3: Price Momentum (ternary)**
- `mom_up`: price change > +0.5% over trailing 4h
- `mom_down`: price change < -0.5% over trailing 4h
- `mom_flat`: within +/-0.5%
- Source: Binance hourly klines

### Optional-Core Signal (backfill attempted — promoted or demoted based on data quality)

**S4: Trade Flow Imbalance (ternary)**
- `flow_buy`: (buy_volume - sell_volume) / total_volume > 10% over trailing 1h
- `flow_sell`: ratio < -10%
- `flow_neutral`: within +/-10%
- **If promoted:** joins the core matrix. All subsets become 2^4-1=15, joint states up to 255, cells x 4 horizons = 1020 pre-pruning. Same train/test split and Holm correction family as S1-S3.
- **If not promoted:** evaluated separately as exploratory appendix using forward-only data with its own train/test split. Results not mixed into core ranking. Not in core Holm family.

## Testing Methodology

### Hypothesis Space
The core signal count depends on whether S4 is promoted. The test is over **feature subsets**, not exact states:

- **If 3 core signals (S4 not promoted):** 2^3-1 = 7 subsets, 63 joint states, 252 cells pre-pruning.
- **If 4 core signals (S4 promoted):** 2^4-1 = 15 subsets, 255 joint states, 1020 cells pre-pruning.
- **Feature subset test:** For each non-empty subset, group observations by the joint state of the included signals. Measure directional accuracy and return for each joint state independently.
- **Example:** Subset {S1, S2} has 3x3 = 9 possible joint states (oi_rising+funding_positive, oi_rising+funding_negative, etc.).
- **Pruning:** Any joint state with < 20 observations in the train split is excluded.
- Post-pruning estimate: ~80-150 cells (3-signal) or ~150-300 cells (4-signal).
- **If S4 stays exploratory:** tested separately as appendix — standalone states + overlay on top core combinations. Not in core Holm family.

### Timestamp Alignment Policy
All signals are anchored to **hourly UTC boundaries** (00:00, 01:00, ..., 23:00).

- **OI (daily):** Forward-filled to each hour. The OI value at hour H is the most recent daily OI reading on or before H.
- **Funding (hourly):** Matched to the latest completed funding period on or before the anchor hour.
- **Momentum (trailing 4h):** Computed from the close price at H minus close price at H-4, using Binance hourly klines.
- **Trade flow (trailing 1h, exploratory):** Computed from hl_trades in the 60-minute window ending at H.
- **Forward labels:** The return from close at H to close at H+horizon. E.g., for 1d horizon, return from H to H+24.
- **No future leak:** All signal inputs use data available strictly before or at the anchor hour. Forward labels use data strictly after the anchor.

### Forward Horizons
- 1h, 4h, 1d, 3d
- **Non-overlapping sampling:** For each horizon, observations are thinned to non-overlapping windows. E.g., for 1d horizon, one observation per day. For 1h, one per hour. This prevents serial dependence from inflating significance.
- If non-overlapping sampling reduces N below threshold for a cell, that cell is excluded.

### For each state-horizon pair, measure:
1. **N** — sample size (non-overlapping observations)
2. **Directional accuracy** — % of times price moved in the empirically-learned direction
3. **Average forward return** — mean return over the horizon
4. **Sharpe-like ratio** — mean return / std return
5. **Statistical significance** — binomial test p-value for directional accuracy vs 50%, with Holm-Bonferroni correction across all tested cells
6. **PM-adjusted EV (secondary)** — illustrative expected value at PM entry prices 0.20, 0.30, 0.40. This is a translation layer, not a primary ranking metric.

### Direction Learning (train-only, frozen)
- **In-sample (train):** First 4 months of backfilled data.
- **Out-of-sample (test):** Last 2 months.
- For each joint state, compute the empirical directional bias on train data ONLY.
- If train shows >55% up, label as "bullish". >55% down, label as "bearish". Otherwise "no signal".
- **Freeze the label.** Apply the frozen label unchanged to the test period.
- Report both train and test accuracy. A signal is credible only if test accuracy remains above 55% after multiple-testing correction.
- **Optional extension:** rolling walk-forward (retrain monthly, test next month). Only if the static split shows promising results first.

### Validation Guards
- **Multiple testing correction**: Holm-Bonferroni across all tested cells
- **Minimum N**: 20 per cell per split
- **Non-overlapping windows**: one observation per horizon-length period
- **Regime diversity check**: report what % of train/test observations fall in rally/decline/consolidation regimes. Flag if >70% of a cell's observations come from one regime.

## Output Format (Supabase Brain)

### Entry 1: `hl_signal_research_methodology`
- Full methodology as described above
- Data sources with coverage dates and row counts
- Signal definitions with exact numeric thresholds
- Test procedure with all parameters
- Written for LLM consumption: explicit, no ambiguity, reproducible

### Entry 2: `hl_signal_research_results`
- Full results table (all cells that passed minimum-N filter)
- Primary ranking: OOS directional accuracy and Sharpe, with significance flags
- Secondary: PM-adjusted EV at 0.20/0.30/0.40 entry prices
- Top 10 cells ranked by OOS Sharpe
- Regime breakdown for top performers
- Explicit list of cells that survived multiple-testing correction

### Entry 3: `hl_signal_research_conclusions`
- Which signals have standalone predictive power (if any)
- Which combinations survive multiple-testing correction
- Recommended strategy gates based on results
- Known limitations: trade flow limited sample, no liquidation data, Binance venue basis
- Comparison to Presto Research findings (12.5% R-squared, zero single-asset predictive power)
- Go/no-go recommendation for adding HL gates to crypto_momentum_v1

### Entry 4: Per-strategy knowledge bases (separate brain entries)
- `strategy_knowledge_hl_liquidation_v1` — thesis, mechanism, live data, current status, limitations
- `strategy_knowledge_crypto_momentum_v1` — same schema
- `strategy_knowledge_insider_copy_v1` — same schema
- `strategy_knowledge_political_no_value_v1` — same schema
- Each entry structured for LLM comprehension: what it does, why, what the evidence says, what's unproven

## Implementation Sequence

1. **Backfill scripts** — fetch HL funding history (paginated) + Binance klines + Coinalyze OI
2. **Data validation** — verify coverage, align timestamps, audit gaps
3. **Signal computation** — build 4 signal time series from backfilled data, compute joint states
4. **Combination tester** — run all cells with train/test split, non-overlapping windows, Holm-Bonferroni
5. **Results writer** — format and push to Supabase brain (entries 1-3)
6. **Strategy knowledge seeder** — write per-strategy brain entries (entry 4)
7. **Bidirectional crypto_momentum_v1 upgrade** — only if results support bearish signals with OOS significance

## What This Does NOT Cover
- Live execution changes (no code changes to running strategies)
- UI/dashboard work
- Real money deployment decisions
- Liquidation proximity signal (deferred to v2 pending data availability)
- Sub-5-minute analysis (Binance basis risk too large)
