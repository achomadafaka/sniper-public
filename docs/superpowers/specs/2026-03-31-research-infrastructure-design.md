# Research Infrastructure Design — 2026-03-31

## Why This Exists

We've spent a month building a copy-trading system on assumptions. Phase 3 research
proved those assumptions wrong:

- **The "insider" classification is fiction.** 99.5% of analyzed wallets entered through
  a back door that bypasses the conviction gate. Our best wallet (tennis sharp, +$196)
  has insider_score=54 and is classified "high_winrate_review." Our 2nd-4th best wallets
  are classified "speculator_bot" with insider_score near 0.

- **The edge is in trade characteristics, not wallet identity.** Entries <2h before
  resolution have 63.7% WR regardless of who made them. Tennis challengers show +7pp
  mispricing regardless of the wallet's classification. Entry price 0.45-0.55 shows
  +7.7pp edge over fair price.

- **We can't confirm any of this at scale.** All findings come from 234 paper trades
  that were pre-filtered through our broken wallet system. The patterns might be real,
  or they might be selection bias. We don't know.

**Decision:** Build a proper research lab, understand the tools and data thoroughly,
document everything, and ONLY THEN design and run experiments. No more building on assumptions.

**This supersedes the 2026-03-30 "CONSOLIDATE, not expand" decision.** That decision was
correct at the time — stop building new strategies without evidence. This project IS the
evidence-gathering that consolidation called for. We're not expanding; we're validating
whether what we have is real before deploying money.

**Time estimate:** 2-3 weeks minimum across multiple sessions. This is worth it because
deploying unvalidated strategies with real money has unbounded downside, while 2-3 weeks
of research has bounded cost (engineering time only).

**Runs locally, not on VPS.** The 36GB dataset requires more RAM than the VPS (2GB).
All research runs on the local machine. Only validated, production-ready code deploys to VPS.

---

## Phase 0: Pre-Flight Check — COMPLETED 2026-03-31

### Becker Dataset: CONFIRMED SUITABLE
- **Wallet addresses:** YES — `maker` and `taker` fields on every trade (on-chain OrderFilled events)
- **Resolution outcomes:** YES — derivable from `outcome_prices` field (one side >0.99, other <0.01)
- **Date range:** Dec 2022 to present (Polygon block 33605403 onward, continuously updatable)
- **Download:** 36GB zstd-compressed from `https://s3.jbecker.dev/data.tar.zst` via aria2c/curl
- **Disk:** ~80-120GB uncompressed Parquet
- **System:** DuckDB streams Parquet (memory-efficient, spill-to-disk), Python 3.9+

### NautilusTrader Fork: DROPPED
- L2 replay requires PMXT order book snapshots — completely different data source, not in Becker
- Trade-tick mode uses Polymarket REST API — also not Becker format
- Designed for price-action strategies, not wallet-level analysis
- Requires Rust toolchain — heavy setup for wrong use case
- **Our replay engine with -2% estimated slippage is the right tool**

### poly_data: DEFERRED until Phase 2 reveals Becker coverage gaps

---

## Phase 1: Infrastructure Setup

**Goal:** Get every tool downloaded, installed, and verified working. No analysis yet —
just confirm we have a functioning lab.

### 1A: Jon Becker Dataset (36GB Parquet + DuckDB)

**What it is:** The largest publicly available Polymarket + Kalshi dataset. Parquet format,
queryable via DuckDB (columnar SQL engine). Contains all trades with wallet addresses
(maker + taker fields), market metadata, and resolution outcomes.

**Repository:** github.com/Jon-Becker/prediction-market-analysis

**What to do:**
1. Clone the repository
2. Download the dataset (36GB — likely via Git LFS or S3 link in the repo)
3. Install DuckDB (`pip install duckdb`)
4. Run the repo's example queries to verify data loads
5. Record: total row counts, date ranges, schema for each table

**What to verify before moving on:**
- Can you query `SELECT COUNT(*) FROM polymarket_trades`? What number?
- Can you query `SELECT COUNT(*) FROM polymarket_markets WHERE closed = true`?
- Do trades have `maker` and `taker` wallet addresses populated?
- What date range do trades cover? (MIN/MAX of timestamp)
- Are there any NULL or obviously broken fields?

**What to document:**
- Exact schema (column names, types, sample values)
- Row counts per table
- Date coverage
- Any data quality issues found
- How to run queries (exact DuckDB commands)

### ~~1B: NautilusTrader~~ — CANCELLED (Phase 0 finding)
Dropped: requires PMXT L2 data not in Becker dataset, wrong tool for wallet analysis.

### 1C: poly_data Pipeline (DEFERRED)

**Deferred until Phase 2 reveals whether gap-filling is needed.**

poly_data (github.com/warproxxx/poly_data) fetches Polymarket trade events from the
Goldsky subgraph. It could fill the gap between Becker's last update and today. But
we don't know if that gap matters until we see what Becker covers. If Becker has data
through last week, poly_data is unnecessary for research purposes. If Becker stops
6 months ago, poly_data becomes essential.

**Revisit after Phase 2A** (market universe mapping reveals Becker's date coverage).

### 1D: Existing Sniper Infrastructure Audit

**What it is:** Our own replay engine, statistical framework, and adapter system.
We've already built significant infrastructure that should be reused.

**What to verify:**
- `replay_engine.py`: Run an existing replay on our paper trade data. Does it work?
  Record the exact command and output.
- `replay/adapters.py`: Read the adapter interface. What methods must a new adapter implement?
  List them with their signatures.
- `scripts/run_signal_research.py`: Run on existing data. What does it produce?
- `scripts/hl_research_v2.py`: What statistical methods does it implement?
  (NW HAC, block bootstrap, etc.)

**Known issue: adapter interface assumes SQLite.**
The existing `StrategyReplayAdapter` ABC takes `conn: sqlite3.Connection` as first
positional argument in `get_candidates()`, `get_lifecycle_state()`, `get_exit_price()`.
The Becker data is Parquet/DuckDB.

**Design decision (made):** BeckerReplayAdapter will pass `None` for `conn` and hold its
own internal DuckDB connection. This avoids changing the existing ABC. The replay engine
calls `adapter.get_candidates(conn, ...)` — the Becker adapter ignores `conn` and queries
DuckDB internally. Simple, no-touch on existing code.

**What to verify and document:**
- Adapter interface specification (exact methods, signatures, return types)
- What dict schema the replay engine expects from `get_candidates()`
- Confirm replay_engine.py only passes `conn` to the adapter (doesn't use it directly)
- Statistical methods available: run `run_signal_research.py` and `hl_research_v2.py`
  on existing data, record what they produce
- Verify `replay_engine.py` runs a successful replay on existing paper trade data

**Setup conventions:**
- Clone Becker repo to `~/Desktop/polymarket-research/` (not inside Sniper repo)
- Use existing Sniper venv — add `duckdb` as research dependency
- Do NOT add DuckDB to production requirements.txt — keep it research-only

---

## Phase 2: Data Exploration

**Goal:** Understand what's actually in the Becker dataset. Not hypothesis testing —
just looking at the data with fresh eyes. What markets exist? What's the distribution?
What's missing? What surprises us?

**Why this matters:** If we design experiments before understanding the data, we'll
test the wrong things or make bad assumptions about what's available.

### 2A: Market Universe Mapping

Answer these questions by querying the dataset:

- How many total resolved markets? How many by year?
- What categories exist? (sports, politics, crypto, entertainment, etc.)
  How are they labeled? How many in each?
- What's the volume distribution? (median, mean, 10th/90th percentile)
- What's the liquidity distribution?
- How many markets have <$1K volume? <$10K? <$100K? >$1M?
- What's the resolution split? (YES wins vs NO wins by category)
- Are there markets with unusual structures we need to handle?
  (negativeRisk, multi-outcome, grouped events)

### 2B: Trade-Level Data Quality

Answer these questions:

- How many total trades in the dataset?
- What % have valid wallet addresses (non-null maker AND taker)?
- What's the trade size distribution? (micro-trades to whale bets)
- What time granularity do we have? (trades timestamped to the second?)
- Are there gaps in the data? (days with zero trades?)
- Can we reliably determine which outcome (YES/NO) a trade is for?
  (This requires mapping asset_id to outcome — how hard is that?)
- Can we reconstruct price timeseries from trades?
  (Sort by timestamp, compute running last-trade-price)

### 2C: Wallet Landscape

**Basic stats (straightforward):**
- How many unique wallets in the dataset?
- What's the distribution? (how many wallets have 1 trade vs 10 vs 100 vs 1000+?)
- Are our 234-trade wallets present in the Becker dataset?
  (Cross-reference by wallet address — critical validation)

**PnL reconstruction (complex, may be multi-day):**
Computing per-wallet PnL from raw trades requires: matching buys to sells per
wallet+market, handling partial fills and DCA, knowing resolution outcomes for
held-to-expiry positions, and handling negativeRisk markets. This is the hardest
single item in Phase 2.

Scope limit for Phase 2: compute SIMPLE PnL only (total bought value vs total
resolved value, ignoring mid-trade exits). Full PnL reconstruction with partial
fills is a Phase 4/5 task if wallet-level hypotheses survive initial screening.

- How many wallets are profitable on the simple metric?
- What's the rough distribution of lifetime PnL?

### 2D: Cross-Validation Against Our Paper Trades

**Critical sanity check:** Look up our 234-trade wallets in the Becker dataset.
- Do the same wallets show the same WR in the broader dataset?
- If our insider>=90 cohort shows 63.3% WR on 234 trades but 51% on 10,000
  Becker trades, that's a critical finding that kills the wallet-following thesis.
- This is the single most important Phase 2 analysis because it directly tests
  whether our existing results are real or selection bias.

**Kill criteria for Phase 2:** If resolved markets < 5,000 or date coverage < 12 months,
the dataset is insufficient — stop and find alternatives.

### 2E: Surprises and Gaps

Document anything unexpected:
- Data that's missing that we assumed would be there
- Fields that don't mean what we thought
- Market types we didn't know existed
- Scale differences (more or fewer of something than expected)
- Quality issues that would affect analysis

### 2F: Documentation (continuous, not a separate phase)

Document as you go — don't wait until Phase 2 is "done." Every query result,
every finding, every surprise gets documented immediately in:

**Supabase brain:** Tool capabilities, dataset schema, data quality, statistics.
**Git (docs/research/):**
- `DATASET_SCHEMA.md` — schema with examples
- `DATA_QUALITY_REPORT.md` — quality issues, gaps
- `MARKET_UNIVERSE.md` — category counts, volumes, resolution rates
- `INFRASTRUCTURE_GUIDE.md` — tool setup instructions (verified, not README-copied)

---

## Phase 3: Thesis Formulation

**Goal:** Based on what we learned in Phases 1-2, design specific experiments.
NOT before. The hypotheses listed below are starting candidates — the actual test
design must be informed by the data we explored.

### What this phase produces:

For each hypothesis:
1. **Statement** — precise, falsifiable claim
2. **Null hypothesis** — what we're testing against
3. **Data requirements** — what subset of the dataset, what fields
4. **Methodology** — exact statistical test, sample selection, controls
5. **Power analysis** — do we have enough data to detect the effect?
6. **Success criteria** — what p-value, what effect size, what out-of-sample validation
7. **Pre-registration** — commit the test design in one git commit BEFORE running it.
   Commit results in a separate commit. If methodology changes after seeing results,
   document why and label the analysis as **exploratory** (not confirmatory).
   This prevents unconscious p-hacking while allowing legitimate refinement.

### Candidate hypotheses (to be refined after Phase 2):

**Market efficiency:**
- H1: Specific market types (tennis challengers, spread bets) are structurally mispriced
- H2: YES tokens are systematically overpriced at 50-65 cents
- H3: Low-liquidity markets are more mispriced than high-liquidity
- H4: Mispricing varies by category (sports vs politics vs crypto)

**Timing:**
- H5: Trades placed close to resolution have higher WR (controlling for price level)
- H6: A "dead zone" exists where timing adds no value
- H7: Pre-event positioning and live-event reading are distinct signals

**Wallet behavior:**
- H8: Wallet identity adds marginal value after controlling for trade characteristics
- H9: Specialist wallets outperform generalists within their domain
- H10: Profitable bot strategies are identifiable and replicable from public data

### What this phase does NOT do:

- Does not run the experiments (that's Phase 5)
- Does not commit to specific strategies (that's Phase 6)
- Does not assume which hypotheses will survive (that's the point of testing)

---

## Phase 4: Execute Tests

**Goal:** Run the pre-registered experiments against the dataset.

### Statistical standards (mandatory for every test):

1. **Sample size** and time range
2. **Point estimate** with **95% confidence interval** (bootstrap or exact binomial)
3. **p-value** against the null hypothesis
4. **Multiple comparison correction** (Holm-Bonferroni when testing multiple hypotheses)
5. **Out-of-sample validation** (time-based train/test split — train on first 2/3, test on last 1/3)
6. **Effect size** after fees (is the edge large enough to trade after Polymarket's 2% + slippage?)
7. **Sensitivity analysis** (how do results change with +-10% on each parameter?)

**Choose the right test per hypothesis type:**
- Cross-sectional (market type X vs Y): exact binomial test or simple bootstrap.
  Markets resolve independently — autocorrelation methods are inappropriate.
- Time-series (price momentum, timing): block bootstrap or NW HAC.
  Trades near resolution on the same market ARE correlated.
- Wallet-level (specialist vs generalist): clustered bootstrap by wallet.
  Multiple trades from one wallet are not independent observations.

Phase 4 specifies the exact test per hypothesis. This list is the minimum bar,
not a one-size-fits-all prescription.

**Kill criteria for Phase 4:** If no hypothesis survives p < 0.05 after Holm-Bonferroni
correction on out-of-sample data, there is no exploitable edge — stop.

### For each test:
- Script produces a JSON results file + markdown summary
- Summary committed to `docs/research/`
- Findings stored in Supabase brain with methodology reference
- Unexpected results get investigated before being accepted or rejected

---

## Phase 5: Strategy Backtesting

**Goal:** Strategies that survive Phase 4 get backtested with realistic execution.

Only reached if Phase 4 produces validated edges.

**Execution model:** Our replay engine (BeckerReplayAdapter) with:
- Trade-level replay from Becker dataset
- Slippage estimate: -2% adverse (based on our 234-trade empirical average)
- Polymarket 2% taker fee applied
- Walk-forward validation (train on first 2/3 of data, test on last 1/3)
- Bootstrap confidence intervals on Sharpe ratio

NautilusTrader was evaluated and dropped in Phase 0 — it requires PMXT L2 order book
data not available in any public dataset, and is designed for price-action strategies
rather than wallet-level analysis. Our replay engine is the right tool.

**Kill criteria for Phase 5:** If no strategy is profitable after fees (2% taker + 2%
slippage) on out-of-sample data, the theoretical edge doesn't survive execution — stop.

### Candidate strategies (pending Phase 5 results):
- S1: Pure timing (enter toss-ups near resolution)
- S2: Market-type specialist (tennis challengers, spreads)
- S3: Structural NO (exploit YES overpricing bias)
- S4: Wallet-enhanced timing (trade characteristics + wallet context)
- S5: Political NO Value (replicate our live strategy at historical scale)

---

## Phase 6: Decision

**Goal:** Based on all evidence, decide what to build, what to deploy, what to kill.

This is where the system redesign happens — informed by data, not assumptions.
Could result in:
- Improved wallet-following (with real archetype data)
- Pure algorithmic trading (if trade characteristics dominate)
- Both running in parallel with tracked performance
- Or: nothing, if no edge survives rigorous testing

---

## Phasing Summary

| Phase | What | Depends On | Output | Est. Time |
|-------|------|------------|--------|-----------|
| 0 | Pre-Flight Check | Nothing | Go/no-go on tools | 30 min |
| 1 | Infrastructure Setup | Phase 0 | Working tools, verified | 1-2 sessions |
| 2 | Data Exploration + Documentation | Phase 1 | Understanding + lab notebook | 2-3 sessions |
| 3 | Thesis Formulation | Phase 2 | Pre-registered experiment designs | 1 session |
| 4 | Execute Tests | Phase 3 | Statistical results with CIs | 2-3 sessions |
| 5 | Strategy Backtesting | Phase 4 | Realistic PnL estimates | 1-2 sessions |
| 6 | Decision | Phase 5 | Build plan based on evidence | 1 session |

**Total estimate: 2-3 weeks across 8-12 sessions.**

**Each phase is a gate.** Don't start the next until the current one is done and documented.
This prevents the pattern of building on assumptions that got us here.

**Kill criteria:**
- Phase 0: If Becker dataset lacks wallet addresses or resolution outcomes → stop, find alternative data source
- Phase 2: If resolved markets < 5,000 or date coverage < 12 months → stop, dataset insufficient
- Phase 4: If no hypothesis survives with p < 0.05 after correction → stop, no exploitable edge found
- Phase 5: If no strategy is profitable after fees → stop, theoretical edge doesn't survive execution

---

## Existing Code to Reuse

| Component | File | What It Does |
|-----------|------|-------------|
| Replay engine | `replay_engine.py` (600 LOC) | Core simulation loop, P&L, circuit breakers |
| Adapter interface | `replay/adapters.py` | Pluggable data source for replay |
| Statistical tests | `scripts/hl_research_v2.py` | NW HAC, block bootstrap, non-overlap thinning |
| Signal research | `scripts/run_signal_research.py` | Multi-signal subset enumeration, Holm-Bonferroni |
| Bayesian optimizer | `scripts/bayesian_threshold.py` | Beta-Binomial threshold tuning |
| MCP replay wrapper | `mcp_tools/replay_tools.py` | Parameter sweep interface |

## New Code to Create

| Component | Purpose |
|-----------|---------|
| `scripts/becker_setup.py` | Download, verify, and configure dataset |
| `scripts/becker_explore.py` | Data exploration queries (Phase 2) |
| `replay/adapters/becker_adapter.py` | Connect Becker data to replay engine |
| `scripts/becker_cross_validate.py` | Extract our 234 paper trade wallets, look them up in Becker (Phase 2D) |
| `scripts/hypothesis_test.py` | Thin wrapper: loads data, runs scipy.stats test, outputs JSON+markdown |
| `docs/research/*.md` | One document per phase output |
