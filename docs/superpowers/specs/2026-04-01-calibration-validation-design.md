# Calibration Validation Study — Design Spec
## Date: 2026-04-01

## Context

We computed a calibration curve for Polymarket YES/NO markets using the Becker dataset
(404M trades, 143K resolved YES/NO markets). The curve showed:

- Overall market: well-calibrated (1-3pp miscalibration at most price levels)
- Geopolitical markets: 7-16pp NO underpriced at every price level
- Crypto launch markets: 12-16pp NO underpriced
- Politics US: 9pp NO overpriced

These were computed on TRADE-LEVEL data (each trade = one observation). This inflates
sample sizes and may produce misleading results. The literature (Reichenbach & Walther 2025,
124M Polymarket trades) finds "no evidence of a general longshot bias" and says prices
"closely track realized probabilities." Our 7-16pp finding would be extraordinary and
contradicts the academic consensus.

We need to determine: is our miscalibration finding real, or a methodological artifact?

## What This Study Tests

Three specific, permanent market structures:

1. **Achievement markets** ("Will X win/reach/hit/accomplish Y?")
   - 30,563 resolved markets, 78.8% NO, $536K avg volume
   - Thesis: people overestimate positive achievements (favorite-longshot bias)

2. **Deadline event markets** ("Will X happen by [date]?")
   - 8,950 resolved markets, 68.1% NO, $628K avg volume
   - Thesis: deadlines usually pass unfulfilled, market underprices this

3. **Valuation target markets** ("FDV/market cap above $X?")
   - 589 resolved markets, 80.1% NO, $668K avg volume
   - Thesis: ambitious valuations are systematically overpriced

These structures will exist on any prediction market indefinitely. They represent
permanent human cognitive biases, not temporary events.

## Methodology

### Step 1: Market-Level Calibration (the critical correction)

**Problem with prior analysis:** Computed on trades (each trade = one observation).
A single market with 50,000 trades contributes 50,000 data points, all with the same
outcome. This inflates N and deflates p-values.

**Correction:** ONE observation per market.
- For each resolved YES/NO market in target categories:
  - Compute VWAP NO entry price = sum(usdc_paid_for_NO) / sum(NO_tokens_received)
    across ALL NO-side trades on this market
  - Assign to 5-cent price bucket based on VWAP
  - Record: did NO win (1) or lose (0)?
- Calibration = (actual NO WR in bucket) - (midpoint of price bucket)
- N = number of markets per bucket, NOT number of trades

**GATE 1:** If market-level miscalibration < 3pp in ALL buckets for ALL categories,
STOP. The prior finding was a trade-count inflation artifact. Report negative result.

### Step 2: Volume-Stratified Calibration

**Problem:** Literature shows markets under $100K are "severely mispriced" (5-10% spreads).
If our signal comes entirely from small illiquid markets, it's not tradeable.

**Test:** Compute calibration separately for:
- Tier A: volume >= $100K (liquid, tradeable)
- Tier B: volume $10K-$100K (marginal)
- Tier C: volume < $10K (illiquid, not tradeable)

**GATE 2:** If miscalibration exists only in Tier C, STOP. The mispricing is real but
not exploitable (can't enter/exit at meaningful size).

### Step 3: Time-Based Train/Test Split

**Problem:** All prior analysis is in-sample. No out-of-sample validation on anything.

**Method:**
- Determine the time midpoint of the dataset from ACTUAL block timestamps
  (not assumed block 70M — verify the date)
- Assign each market to train or test based on the block number of its FIRST trade
  (not resolution block — that would be look-ahead bias)
- Compute market-level calibration separately for train and test

**GATE 3:** If train shows >= 5pp miscalibration but test shows < 3pp for the same
(category, price bucket, volume tier), STOP. The signal is not stable over time.

### Step 4: Statistical Testing

For each (category, price bucket, volume tier) cell that passes Gates 1-3:

- **Test:** Exact one-sided binomial test
  - H0: p = price_midpoint (market is calibrated)
  - Ha: p > price_midpoint (NO is underpriced)
  - N = number of markets in cell
  - k = number of NO wins

- **Multiple comparison correction:** Holm-Bonferroni
  - 3 categories × ~6 price buckets × 2 volume tiers = ~36 tests
  - But many cells will be empty or have N < 30 (excluded)
  - Actual number of tests determined by data
  - Report BOTH raw and adjusted p-values

- **Minimum sample:** N >= 30 markets per cell. Below this, the binomial test has
  insufficient power. Report the cell but mark as "insufficient data."

### Step 5: Capital Efficiency

For markets in cells that pass all gates and statistical tests:

- Compute market duration: last_trade_block - first_trade_block, converted to days
  using block timestamps
- Compute annualized ROI: (1 + per_market_ROI) ^ (365 / duration_days) - 1
- Report median and interquartile range of annualized ROI

**GATE 4:** If median annualized ROI < 20%, the edge exists but is too small to justify
capital lockup. Report as "statistically significant but not economically significant."

### Step 6: Comparison to Literature

Compare our findings to established benchmarks:
- Kalshi favorite-longshot: 1.9% post-fee return (Bürgi et al. 2025)
- Polymarket Brier score: 0.0838 (TradeTheOutcome 2025)
- Reichenbach & Walther: "no general longshot bias" on 124M trades
- If our miscalibration exceeds these by >3x, flag for extra scrutiny

## Kill Criteria (pre-registered)

The finding is DEAD if ANY of these occur:
1. Gate 1 fails: market-level miscalibration < 3pp everywhere
2. Gate 2 fails: miscalibration only in volume < $10K markets
3. Gate 3 fails: test set miscalibration < 3pp where train showed >= 5pp
4. No cell achieves Holm-Bonferroni adjusted p < 0.05
5. Gate 4 fails: median annualized ROI < 20%

The finding is VALIDATED if ALL of these hold:
1. At least one (category, price bucket, volume tier) cell has >= 5pp miscalibration
   on the test set
2. That cell has Holm-Bonferroni adjusted p < 0.05
3. That cell has volume tier >= $10K (tradeable)
4. That cell has median annualized ROI >= 20%
5. The miscalibration is not more than 5x what literature reports for similar platforms
   (if it is, investigate further before claiming it's real)

## Output

A single Python script (`scripts/study_5_calibration_validation.py`) that:
1. Runs all 6 steps sequentially
2. Prints GATE PASS/FAIL at each step
3. Saves complete results to `results/study_5_calibration_validation.json`
4. Produces a final VERDICT: VALIDATED / DEAD / INCONCLUSIVE

The JSON results file contains every intermediate number so any finding can be
independently verified.

## Edge Cases and Exclusions

- **Markets with no NO-side trades:** Excluded. We can't compute VWAP NO price if
  nobody bought NO tokens directly. These markets are not relevant to a NO strategy.
- **"Other" structural category (75K markets):** Excluded. Too heterogeneous to form
  a testable hypothesis. The three included categories have clear structural theses.
- **Binary up/down markets (120K crypto):** Excluded. 50/50 by design, no structural bias.
- **Matchup markets (81K A-vs-B):** Excluded. No YES/NO structure — both outcomes are symmetric.
- **Time split assignment:** Based on the block number of the FIRST trade of any kind on the
  market (not first NO trade, not resolution block). This avoids look-ahead bias.
- **Consistency with literature:** Reichenbach & Walther (2025) found "no general longshot bias"
  on Polymarket. Our thesis is NOT that there is a general bias — we confirmed the overall
  market is well-calibrated. Our thesis is that SPECIFIC structural categories have
  category-specific miscalibration that the general average masks.

## Critical Data Limitation: Sell-Side Omission

46% of geopolitical NO tokens bought are SOLD before resolution.
This affects PnL calculations but NOT calibration miscalibration measurements.

**Why calibration is unaffected:** The calibration asks "of markets where NO was
bought at $0.70, what fraction resolved NO?" The market outcome is the same whether
the buyer later sold or held. Miscalibration in percentage points is valid.

**Why PnL is affected:** The prior Study 4 PnL numbers (+$36.4M geopolitical) assumed
all bought tokens are held to resolution. With 46% sell-through, actual PnL is lower.
For one active trader (0x4BBE), buy-only PnL was $1.63M but correct buy+sell PnL was
$145K — a 91% error.

**Consequence for this study:** Steps 1-4 (calibration validation) are testing win rates
and miscalibration, which are UNAFFECTED. Step 5 (capital efficiency / ROI) needs to
use a HOLD-TO-RESOLUTION model: buy at the observed price, hold until the market
resolves, compute PnL = (payout - entry_price) per token. This is the purest test and
avoids the sell-side accounting problem entirely.

**Future study needed:** Position management (active selling vs hold) requires full
buy+sell PnL accounting and is a SEPARATE question from whether the calibration edge exists.

## What This Study Does NOT Test

- Whether actively managing positions (selling before resolution) adds or destroys value
- Whether the mispricing can be captured with limit orders vs market orders
- Whether the edge is being arbitraged away over time (would need rolling-window analysis)
- Sports or crypto up/down markets (near-efficient, different thesis)
- Optimal position sizing (concentrated vs diversified)

## Computational Notes

- Market-level analysis requires: for each of ~30K+ markets, compute VWAP NO price
  from the trades table (404M rows). This is a heavy join.
- Strategy: scan trades once, aggregate to market level, then do all analysis on
  the aggregated table (~30K rows, fits in memory)
- Memory limit: 8GB DuckDB, spill to disk
- Estimated runtime: 5-10 minutes for the trade scan, seconds for the analysis
