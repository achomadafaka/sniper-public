# Batch 1 — Monte Carlo on the Volume-Filtered NO Slice

> **Status:** pre-registered plan, awaiting owner approval before execution.
> **Runtime location:** `~/Desktop/polymarket-research/` (separate git repo, last touched 2026-04-01 at `10d0b69`).
> **Dataset:** Becker 404M trades, 383K resolved markets (already downloaded).

## Why this plan exists

Prior research memory (`project_research_infrastructure.md`, 17d old) listed "Batch 1 tests (C+D)" as the next action. On inspection: **there are no artifacts labelled C or D**. The phrase was shorthand. What actually sits open is ONE thing:

> *Volume-filtered slice ($10K-$200K, NO $0.55-$0.85): 10.2% ROI, 121K trades — NOT YET Monte Carlo tested.*

Study 4 killed the aggregate structural-NO thesis via Monte Carlo. This volume-filtered sub-slice survived aggregate analysis with positive ROI, but never had Monte Carlo run against it. That's the single outstanding claim — either confirm it or kill it.

## Non-goals

- Not rebuilding the simulation platform (that was a different Batch 1 proposal; deferred).
- Not starting Batches 2-5.
- Not extending to new categories/price bands beyond the one specific slice.
- Not claiming "tradeable edge" even on pass — further scrutiny required.

## Pre-registered methodology

### Scope

Geopolitical category only. Volume band `$10K ≤ market_volume ≤ $200K`. NO entry price `0.55 ≤ NO_price ≤ 0.85`. Per Study 4 design, NO = `outcome[1]` in YES/NO-structured markets (exclude "Team A vs Team B" markets).

Per-market observation unit (one row per market, not per trade) — per Standard #11 in the research methodological standards.

### Sample

Reuse Study 4's market filter logic (VWAP entry price per market from Becker trades, outcome from `outcome_prices[1] > 0.99`). Expected n ≈ 121K markets per prior memory — verify actual count as first output.

### Train / test split

Time-based, not random:
- **Train:** markets resolved before 2025-07-01.
- **Test:** markets resolved on or after 2025-07-01.

Report PnL, WR, ROI, n separately for each set before merging.

### Fee model

Use the actual `fee` field from Becker trades. Per Study 4 finding: 89.6% of trades are zero-fee maker orders; the naive 2% blanket assumption was what originally inflated the finding.

### Monte Carlo

For the full slice (and for train + test separately):
1. Hold the set of markets and their NO entry prices fixed.
2. Shuffle the binary resolution outcome (1 if NO won, 0 if NO lost) across the sample, preserving the marginal win rate.
3. Recompute PnL for the shuffled sample.
4. Repeat 10,000 times.
5. Report: fraction of shuffled runs with PnL ≥ observed.

### Success criteria (kill criteria pre-registered)

All three must hold:

1. **C1 — Sign stability.** Positive PnL after fees on BOTH train and test sets.
2. **C2 — Statistical significance.** Monte Carlo p-value < 0.01 on the full slice AND on the test set alone.
3. **C3 — Economic significance.** ROI ≥ 5% after fees on the test set alone.

If ANY of C1/C2/C3 fails → finding is killed, no further action.

### Reproducibility artifacts (required)

Every output must land on disk in the research repo:
- `~/Desktop/polymarket-research/scripts/batch1_volume_filtered_no.py` — the full script, deterministic seed.
- `~/Desktop/polymarket-research/results/batch1_volume_filtered_no.json` — result JSON with: n_train, n_test, pnl_train, pnl_test, roi_train, roi_test, wr_train, wr_test, mc_p_full, mc_p_test, random_seed, dataset_snapshot_hash, run_ts.
- `~/Desktop/polymarket-research/results/batch1_monte_carlo_distribution.parquet` — the 10K shuffled-PnL values (for plotting / audit).

Failing to save any of these is a process failure, not a research result.

## Execution plan

### Step 1 — Verify dataset state (5 min)

```bash
cd ~/Desktop/polymarket-research
duckdb -c "SELECT COUNT(*) FROM read_parquet('prediction-market-analysis/data/**/*.parquet')"
```

Confirm trade count ≈ 404M. Note the hash of the largest parquet file (goes into the results JSON).

### Step 2 — Write the script (~1 hour)

Single script at `scripts/batch1_volume_filtered_no.py`. No interactive notebooks — scriptable reproducibility or nothing.

Structure:

```python
def compute_slice(trades_path, markets_path, train_cutoff) -> dict:
    """Load markets, filter geo + volume band + YES/NO structure, compute per-market
    VWAP NO entry prices, filter by price band, merge train/test, return slice."""

def compute_pnl(sample, fee_col) -> dict:
    """Per-market PnL = (payout - entry_price) * (1 - fee). Aggregate."""

def monte_carlo(sample, n_iter=10_000, seed=42) -> dict:
    """Shuffle outcomes, recompute PnL, return distribution + p-value."""

def main():
    # Determinism
    random.seed(42); np.random.seed(42)
    # Run
    # Write JSON + parquet
```

### Step 3 — Dry-run on train set only (~15 min compute)

Sanity check: Study 4's original finding should reproduce — 10.2% ROI, ~121K markets. If it doesn't, the script has a bug. Fix before proceeding to test set.

### Step 4 — Full run (~30-60 min compute)

Train + test + Monte Carlo 10K iterations. Save all artifacts.

### Step 5 — Decision

Against C1/C2/C3:

- **All pass** → write up finding in `studies/batch1_result.md`, update Supabase brain entry, MEMORY.md, and explicitly flag that "edge claim" still needs live-execution stress-test (slippage, adverse selection) before any real-money action.
- **Any fail** → write up killed finding in `studies/batch1_killed.md`, close the lead, update MEMORY.md, proceed to Batch 2 (Study 2 reproducibility gaps).

## Runtime + compute budget

- Data I/O: ~2 min (subset of parquet trades for geo slice)
- Slice + aggregate PnL: ~5 min
- Monte Carlo 10K iter: ~30-45 min (single-threaded; vectorized numpy shuffle should fit)
- Total: ~1 hour compute, ~1 hour writing the script

No GPU, no cloud. Local DuckDB + numpy.

## Pre-registered decision matrix

| C1 (sign) | C2 (MC p<0.01) | C3 (ROI≥5%) | Action |
|---|---|---|---|
| PASS | PASS | PASS | Finding survives → write up → still NOT tradeable without slippage/execution stress test |
| PASS | PASS | FAIL | Noise-sized edge — close as unlikely to be actionable after execution costs |
| PASS | FAIL | any | Edge explained by randomness given market selection — kill |
| FAIL | any | any | Doesn't replicate on test set — kill, likely overfit |

No judgment calls or post-hoc adjustments. If criteria were wrong, we update them and re-pre-register BEFORE running again, not after.

## What this plan does NOT do

- Does not re-run Study 2 or Study 4 aggregate claims.
- Does not test any new market category or price band.
- Does not build an event-driven backtester.
- Does not attempt live-execution simulation (slippage, queue position, orderbook depth). That's Batch 5 territory.

## Approval gate

Owner approval required before Step 2 (writing the script). The pre-registered criteria above are the contract — changing them after data is seen is p-hacking.

## Open questions for owner

1. **Test-set cutoff date** — proposed 2025-07-01 (gives roughly equal train/test sizes given the dataset window Dec 2022 - Feb 2026). Accept or revise?
2. **Kill-criterion C3 threshold** — 5% ROI on test set. Lower (3%) would admit weaker edges; higher (10%) would demand stronger. Pin before running.
3. **Reshuffle unit** — markets (n ≈ 121K) or trades (n ≈ millions). Markets is the right answer per Standard #11 (market-level, not trade-level). Confirm.

## References

- `~/Desktop/polymarket-research/studies/study_4_structural_no.md` — original pre-registered methodology
- `~/Desktop/polymarket-research/results/study_1_top_sharps.json` — prior result format template
- `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/project_research_infrastructure.md` — research infrastructure notes (17d stale)
- `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/research_edge_fundamentals.md` — transaction cost / market efficiency priors
