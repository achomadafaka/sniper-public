# Edge Research Framework Design Spec

**Date:** 2026-03-26
**Status:** Approved
**Origin:** GPT-reviewed design, grounded in system audit (2026-03-25) and statistical methodology research

## 1. Problem Statement

### Why current scoring + paper trading is insufficient

The wallet_quality_score rebuild (2026-03-25) solved wallet *selection* — it correctly ranks wallets by lifetime P&L instead of inflated win rate, and hard filters block market makers, Gate 0 losers, and low-evidence wallets. But selection is not edge. The system currently has no way to answer:

- Is 49.5% WR on 275 closed paper trades real or noise? (Answer: essentially a coin flip, p >> 0.05)
- Is the insider 90+ band's 67.6% WR durable or a hot streak? (Answer: suggestive on 37 trades, not yet robust after multiplicity correction)
- Would this edge survive if we started copying tomorrow? (Answer: unknown — no out-of-sample validation)
- How much of our PnL is from market selection vs timing vs wallet selection? (Answer: no decomposition exists)
- Is a 52% WR strategy worth pursuing if its P/L ratio is 3:1? (Answer: yes, but the system cannot evaluate this)

Win rate alone is insufficient; a real edge must also show positive expectancy after costs and tolerable drawdown. A 52% WR strategy with 3:1 P/L ratio is highly profitable. A 60% WR strategy with 1:5 P/L ratio loses money.

### What "real edge" means operationally

An edge is real when it is statistically distinguishable from random after a predeclared multiple-testing correction, shows positive expectancy after realistic transaction costs (1.5-3% for copy trading), persists out-of-sample across at least two temporal windows, and has a plausible causal mechanism rather than pure pattern-fitting.

### Five dimensions of edge

| Dimension | Question | Current State | Data Constraint |
|-----------|----------|---------------|-----------------|
| Wallet selection | Are we copying the right traders? | wallet_quality_score deployed, backfilling. No validation yet. | lb_pnl available for 1,325/2,379 wallets |
| Signal timing | Does position_insider_score >= 90 predict wins? | 67.6% WR on 37 trades; suggestive, not yet robust after multiplicity correction | Paper-trade window ~2 months |
| Market selection | Are some market categories structurally better? | No systematic category-level analysis with resolution data | Market resolution coverage ~21% (948/9,048) |
| Execution quality | Does copy slippage eat the edge? | slippage_pct tracked but not analyzed | No historical OHLCV prices |
| Copy replicability | Can we actually follow fast enough? | No latency measurement | No systematic latency data |

---

## 2. Statistical Methodology

### Significance framework: two-stage testing

**Stage 1 — Exploratory (hypothesis generation):** Uncorrected p < 0.05 on retrospective data. Identifies candidates worth investigating. Any finding here is labeled "suggestive" and does NOT constitute evidence for deployment.

**Stage 2 — Confirmatory (promotion-eligible):** Familywise alpha = 0.05, corrected within each hypothesis family using Holm-Bonferroni. Applied to out-of-sample or prospective data only.

**Promotion bar (real money):** Corrected p < 0.01 within family, plus positive expectancy after worst-case costs, plus out-of-sample confirmation across 2+ temporal windows.

The correction denominator is the number of hypotheses in the same *family* — e.g., testing 4 wallet quality bands = 1 family of 4. Testing score bands AND market categories = separate families, each corrected within their own group. Prefer Holm-Bonferroni over plain Bonferroni (same familywise control, less unnecessarily punitive).

### Minimum sample sizes

From binomial power analysis:
- Null hypothesis: WR = 50% (no edge)
- Test: one-sided binomial (testing WR > 50%)
- Power: 80%
- Assumption: independent trials

| True WR | Trades @ p<0.05 (uncorrected) | Trades @ p<0.005 (Bonf/10) |
|:-------:|:-----------------------------:|:---------------------------:|
| 52% | 3,863 | 7,297 |
| 55% | 617 | 1,166 |
| 58% | 240 | 454 |
| 60% | 153 | 290 |
| 65% | 67 | 127 |

**Sniper current: 275 closed paper trades at ~49.5% WR. Not close to significance at any level.**

Hard minimum: 5 closed trades for any group statistic. 50 closed trades minimum for confirmatory claims.

### Effect size: minimum viable edge after costs

Cost model: round-trip cost applied as reduction to net payoff. Win pays `(1-entry_price)/entry_price - cost_pct`; loss pays `-1.0`. `cost_pct` includes taker fee + estimated slippage on copy-trade execution.

| Entry Price | Net payoff ratio (b_net) | Break-even WR (at 3% cost) | Minimum target WR (+4pp buffer) |
|:-----------:|:------------------------:|:--------------------------:|:-------------------------------:|
| 0.30 | 2.30 | 30.3% | 34% |
| 0.40 | 1.47 | 40.5% | 44% |
| 0.50 | 0.97 | 50.8% | 55% |
| 0.60 | 0.64 | 61.1% | 65% |
| 0.70 | 0.40 | 71.5% | 76% |

Formula: `WR_breakeven = 1 / (b_net + 1)` where `b_net = (1-entry_price)/entry_price - cost_pct`.

Key insight: entry price dominates edge viability. At entry=0.40, you risk $0.40 to win $0.57 net (2.3:1 after costs) — even a 50% WR is profitable. At entry=0.60, you risk $0.60 to win $0.37 net (0.6:1) — you need 61%+ WR just to break even. At WR=58%, entry=0.40 yields expectancy +$0.43/dollar risked; entry=0.60 yields -$0.05 (losing money).

### Metrics beyond win rate

All computed by deterministic scripts, never by agents:

1. **Expectancy** = (avg_win * WR) - (avg_loss * (1-WR)), net of costs. The single most important number. Must be positive after worst-case cost assumptions.

2. **Profit factor** = gross_profit / gross_loss. Benchmarks: <1.0 losing, 1.0-1.25 marginal, 1.25-1.5 decent, 1.5+ good, 3.0+ suspect/overfit.

3. **Maximum drawdown** = largest peak-to-trough equity decline. Must be survivable at planned position sizing.

4. **Drawdown duration** = longest time spent below previous equity high.

5. **Capital efficiency** = (PnL / capital_deployed) * (365 / avg_holding_days). Fast-resolving markets are dramatically more capital-efficient.

6. **Decay curve** = rolling 30-trade expectancy over time. Declining curve = regime-dependent edge. Stable or improving = durable.

7. **Kelly fraction** = (WR * b - (1-WR)) / b, where b = (1-entry_price)/entry_price - cost_pct (cost-adjusted payoff). Negative Kelly = do not trade. Always use fractional Kelly for deployment. Quarter-Kelly materially reduces drawdown versus full Kelly, but actual drawdown must be estimated from simulated or observed equity paths, not assumed from a fixed cap.

### Out-of-sample validation: three stages

1. **Retrospective** (historical data): Split by time. Train on [0, T], test on [T, T+k]. Minimum 3 temporal windows. Red flag if edge appears in only 1 window.

2. **Prospective paper** (live, forward-looking): Paper trades with frozen criteria. No parameter changes during validation.
   - Duration depends on trade rate, which varies dramatically by hypothesis scope. At 10 trades/week and 58% true WR: ~12 weeks for p<0.05. At 3 trades/week (typical for a specific quality band): ~40 weeks. Plan accordingly.
   - **No-peeking rule:** Prospective validation runs must have a predeclared evaluation horizon and stopping rule. No mid-test threshold changes, no interim peeking with optional stopping. The test either reaches the predeclared sample size or time horizon, or it doesn't.

3. **Prospective real** (small real money): Quarter-Kelly sizing, $5-50/trade, minimum 50 trades before scaling.

### Bias controls

| Bias | Risk in Sniper | Control |
|------|---------------|---------|
| Survivorship | Only see wallets still active | Include all 2,379 analyzed wallets; track wallets that went inactive |
| Lookahead | wallet_quality_score uses lb_pnl known after trades | Prospective paper trading with frozen criteria |
| Selection | Picked wallets after seeing results | Pre-register hypotheses before testing |
| Data snooping | ~20 strategies tested, expect 1 false positive by chance | Holm-Bonferroni within hypothesis families; report ALL tested hypotheses |
| Cost blindness | Ignoring 1.5-3% copy slippage | Always report net-of-cost expectancy |
| Small sample | 37 trades in best band | Hard minimum: 5 for any group stat, 50 for confirmatory |

Probability of at least one false positive without correction: 5 tests = 23%, 10 tests = 40%, 20 tests = 64%. This is why Holm-Bonferroni is mandatory for confirmatory claims.

### Decision tiers

**Tier 1 — Strong Evidence (deploy real money):**
- Corrected p < 0.01 (Holm-Bonferroni within family)
- 200+ out-of-sample resolved trades
- Profit factor > 1.3 after costs
- Positive expectancy after worst-case 3% cost haircut
- Edge stable across 2+ temporal windows
- Adversarial review passed
- Human sign-off

**Tier 2 — Promising (continue paper, start minimal real):**
- p < 0.05 uncorrected with 100+ out-of-sample trades
- Profit factor > 1.1 after costs
- Edge visible in 2+ windows
- Quarter-Kelly sizing, max $50/trade

**Tier 3 — Insufficient (keep paper, no real money):**
- p >= 0.05 or < 100 resolved trades
- Single time window only
- Edge not distinguishable from transaction cost noise

---

## 3. Data Inventory and Collection Gaps

### What we already have

| Source | Rows | Coverage | Research Utility |
|--------|------|----------|-----------------|
| user_analysis | 2,379 | All analyzed wallets | Wallet scoring, classification, lb_pnl for 1,325 wallets |
| position_signals | ~15,000 | Feb-Mar 2026 | Entry timing, hold duration, activity states |
| signal_history | ~290,000 | Mar 8-26 | Immutable snapshots, score evolution |
| score_history | ~44,000 | Mar 8-26 | Wallet score trends over time |
| paper_trades | ~275 | Feb 23 - present | Copy-simulation outcomes (primary validation source) |
| strategy_positions | ~78 | Mar 2026 | Strategy-level position tracking |
| market_metadata | ~23,000 | All known markets | Category, end_date, volume_24h |
| market_outcomes | ~1,900 | 948 unique (~21% coverage) | Resolution prices — critically incomplete |
| followed_wallet_activity | ~28,000 | Nov 2024 - present | Raw Polymarket activity (buy/sell/claim) for 20 wallets |
| external_leaderboard_entries | ~4,200 | Snapshot-based | Lifetime P&L rankings |
| position_snapshots | ~3,900 | Mar 7-9 only | Sparse hourly snapshots (19 wallets) |

### What is stream-window limited

- **low_buy_trades**: Only conviction-gate matches (~73/week), not all Polymarket trades. ~30-day window.
- **position_signals**: Current analysis cycle only. Closed-before-analysis positions invisible.
- **position_snapshots**: 3 days of hourly data for 19 wallets. Not usable for backtesting.

### What needs systematic collection (priority order)

**1. Market resolution outcomes (CRITICAL)**
- Only 21% of markets have resolution data. Cannot validate ANY historical edge claim without this.
- Source: Polymarket CLOB API or gamma-api resolved markets endpoint
- Script: extend existing `market_outcome_poller.py` to run periodic (~daily) backfill of all markets with end_date in the past
- Target: 100% resolution coverage for all markets with end_date < today

**2. Wallet closed-position history (HIGH)**
- We see current open positions but not historical closed positions for most wallets
- Source: Polymarket `closed-positions` API endpoint per wallet
- Enables: true wallet WR (not zombie-inflated), actual P/L ratio, historical drawdown
- Target: closed-position history for experiment cohorts, copy-pipeline wallets, and top-N research targets. Do NOT gate on wallet_quality_score — that bakes the current scoring model into research collection (selection bias). Scope by experiment need, not by score threshold.

**3. Market price snapshots (MEDIUM)**
- No OHLCV data. Cannot analyze price paths, momentum, or entry timing quality.
- Source: Polymarket CLOB API midpoint prices, sampled every 1-4 hours for active markets
- Enables: entry timing analysis, copy slippage measurement, momentum research
- Target: hourly midpoint for all markets with >$10K daily volume

**4. Copy execution latency (MEDIUM)**
- No measurement of delay between wallet's trade and our paper entry
- Source: compare followed_wallet_activity timestamps with paper_trades.opened_at
- Enables: latency sensitivity analysis (does edge decay with delay?)
- Target: latency measurement for all paper trades with matched activity records

**5. Prediction/hypothesis outcome tracking (LOW — framework exists)**
- predict_tool tables in Supabase exist but aren't systematically graded
- trade_ideas framework exists (39 total)
- Needs: automated grading on resolution, structured experiment outcomes

### Data constraints (stated upfront)

- Paper-trade window: ~2 months. Insufficient for seasonal or regime analysis.
- Market resolution: 21% coverage. Any retrospective analysis on unresolved markets is invalid.
- No orderbook depth data. Cannot simulate real execution quality.
- lb_pnl available for 1,325/2,379 wallets. Remaining wallets have no ground-truth P&L.

---

## 4. Storage Architecture

### Principle: separate raw facts from derived features from experimental results

| Layer | What | Where | Why |
|-------|------|-------|-----|
| Raw facts | Market resolutions, wallet activity, price snapshots, trade outcomes | SQLite | Fast queries, version-controlled, reproducible |
| Derived features | Wallet metrics, category stats, expectancy calculations | SQLite (computed from raw) | Deterministic, recomputable |
| Experiment specs | Hypothesis definitions, dataset configs, parameter sets | SQLite `research_experiments` | Reproducible experiment definitions |
| Experiment results | Statistical outputs, p-values, CIs, artifact paths | SQLite `research_results` | One row per run, immutable |
| Research conclusions | Agent interpretations, adversarial reviews, promotion decisions | SQLite `research_conclusions` (canonical) → mirrored to Supabase brain for cross-agent access | SQLite is source of truth; Supabase is read mirror |

### New SQLite tables

**`market_outcomes`** — ground truth for resolved markets (ALREADY EXISTS, 948 markets populated)

Uses the existing table. Schema: `(condition_id, outcome) PRIMARY KEY`, `resolved_price REAL`, `resolved_at INTEGER` (unix epoch), `source TEXT`. The resolution poller script backfills this table — does NOT create a new one. See CLAUDE.md Core DB tables.

**Gap to close:** Currently 21% coverage (948/9,048 markets). Poller must backfill all markets with `end_date_iso` in the past.

**`wallet_closed_positions`** — historical closed positions per wallet
```sql
CREATE TABLE wallet_closed_positions (
    wallet TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    closed_at TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    UNIQUE (wallet, condition_id, outcome, closed_at)
);
```

**`poly_price_snapshots`** — periodic market price captures (ALREADY EXISTS in repo, reuse)

The repo already has `poly_price_snapshots` support and collection code. Do NOT create a duplicate `price_snapshots` table. Extend/operationalize the existing infrastructure to run on a schedule and cover active markets with >$10K daily volume.

**`research_experiments`** — experiment registry
```sql
CREATE TABLE research_experiments (
    experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis TEXT NOT NULL,
    family TEXT NOT NULL,          -- for multiple-testing grouping
    family_size INTEGER NOT NULL,
    dataset_spec TEXT NOT NULL,    -- JSON: source, filters, split
    parameters TEXT NOT NULL,      -- JSON: test type, null hypothesis, correction method
    predeclared_horizon TEXT,      -- "90 days" or "100 resolved trades"
    status TEXT NOT NULL DEFAULT 'draft',  -- draft|running|complete|rejected|promoted
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT NOT NULL       -- 'agent:world_analyst' or 'human'
);
```

**`research_results`** — experiment outcomes (script-produced only)
```sql
CREATE TABLE research_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES research_experiments(experiment_id),
    run_ts TEXT NOT NULL DEFAULT (datetime('now')),
    stage TEXT NOT NULL,           -- exploratory|confirmatory|prospective
    dataset_hash TEXT NOT NULL,    -- SHA256 of assembled dataset for reproducibility
    sample_size INTEGER NOT NULL,
    win_rate REAL,
    expectancy REAL,              -- net of costs
    profit_factor REAL,
    max_drawdown REAL,
    drawdown_duration_days REAL,
    capital_efficiency REAL,
    p_value REAL,
    ci_lower REAL,
    ci_upper REAL,
    corrected_p_value REAL,
    correction_method TEXT,        -- 'holm_bonferroni' | 'bonferroni' | 'none'
    artifact_path TEXT,            -- path to full report artifact
    cost_assumption_pct REAL       -- what cost% was used
);
```

**`research_conclusions`** — agent interpretations
```sql
CREATE TABLE research_conclusions (
    conclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES research_experiments(experiment_id),
    result_id INTEGER REFERENCES research_results(result_id),
    finding TEXT NOT NULL,
    caveats TEXT,
    adversarial_case TEXT,
    recommendation TEXT NOT NULL,  -- reject|monitor|prospective|promote
    agent TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### What stays where

**SQLite:** All raw data, derived features, experiment specs, statistical results. Everything deterministic and reproducible.

**Supabase:** Research conclusions (mirrored for cross-agent accessibility), world events, market opportunities, agent observations. Narrative knowledge that agents need to read.

---

## 5. Hypothesis-Testing Pipeline

### Three-layer design (C: scripts own evidence, agents interpret, humans gate)

```
SCRIPT LAYER (deterministic)          AGENT LAYER (interpretation)         HUMAN LAYER (gates)
─────────────────────────────         ───────────────────────────          ──────────────────
                                      1. Generate hypothesis
                                      2. Write experiment spec
                                         → research_experiments
3. Assemble dataset
4. Compute metrics
5. Run significance tests
6. Generate report artifact
   → research_results
                                      7. Read artifact
                                      8. Write interpretation
                                      9. Adversarial review
                                      10. Recommend: reject/
                                          monitor/prospective/
                                          promote
                                         → research_conclusions
                                                                          11. Review if promote
                                                                          12. Approve/reject
                                                                              deployment
```

**Boundary principle:** Agents should never be the source of raw evidence. They should interpret evidence produced by scripts.

### Stage gates

| Stage | Owner | Inputs | Outputs | Pass criteria |
|-------|-------|--------|---------|---------------|
| Hypothesis | Agent | Observation, data pattern, domain knowledge | Experiment spec in research_experiments | Falsifiable prediction, declared family and family_size |
| Retrospective | Script | Historical data, experiment spec | research_results row + artifact | p < 0.05 uncorrected (exploratory) |
| OOS holdout | Script | Temporal holdout data, same spec | research_results row | Same direction, p < 0.05 on holdout |
| Prospective paper | Script + Paper trader | Live paper trades, frozen criteria, predeclared horizon | research_results row | Corrected p < 0.01 (Holm-Bonferroni) + positive expectancy after costs |
| Adversarial review | Agent | All result artifacts | research_conclusions row | No fatal flaw identified |
| Promotion | Human | Full evidence chain | Deploy decision | Human sign-off |

### Experiment spec format

```json
{
  "hypothesis": "Wallets with wallet_quality_score >= 70 produce positive copy-trade expectancy after 3% costs",
  "family": "wallet_quality_bands",
  "family_size": 4,
  "dataset": {
    "source": "paper_trades JOIN user_analysis",
    "filters": {"status": "!= 'open'"},
    "split": {"type": "temporal", "train_pct": 0.6, "holdout_pct": 0.4}
  },
  "primary_test": {"null_hypothesis": "WR = 50%", "type": "one_sided_binomial"},
  "descriptive_metrics": ["expectancy", "profit_factor", "max_drawdown", "capital_efficiency"],
  "min_sample": 50,
  "correction": "holm_bonferroni",
  "cost_assumption_pct": 0.03,
  "predeclared_horizon": "90 days or 100 resolved trades, whichever first",
  "no_peek": true
}
```

### Revalidation cadence

- Active strategies: revalidate every 90 days or 100 new resolved trades
- Decaying strategies (rolling expectancy declining 2 consecutive periods): revalidate immediately
- Rejected hypotheses: re-testable with genuinely new data after 6 months minimum

---

## 6. Research Surfaces / Tooling

What the system needs to answer "where is the edge?" — all backed by deterministic scripts, displayed via API endpoints or CLI reports.

### Scripts (deterministic evidence producers)

1. **`scripts/research/experiment_runner.py`** — takes experiment spec, assembles dataset, computes all metrics, runs significance tests, saves to research_results with artifact. The core statistical engine.

2. **Existing `market_outcome_poller.py`** (operationalize/extend) — backfills market_outcomes from Polymarket API. Without full resolution coverage, nothing else works.

3. **`scripts/research/cohort_analysis.py`** — wallet cohort comparison across quality bands, classifications, edge tiers. Outputs expectancy, profit factor, sample size per cohort.

4. **`scripts/research/category_heatmap.py`** — market category x time period → expectancy matrix. Requires market_outcomes table.

5. **`scripts/research/timing_decay.py`** — rolling expectancy by position_insider_score band over time. Answers: "is the 90+ band edge stable or decaying?"

6. **`scripts/research/slippage_analysis.py`** — copy latency measurement + correlation with outcome. Compares source wallet activity timestamps with paper entry timestamps.

7. **`scripts/research/report_generator.py`** — reads research_results, produces human/agent-readable artifact (markdown + JSON bundle).

### API surfaces (for dashboard integration, phase 3)

- `/api/research/experiments` — list experiments with status, latest p-value, expectancy
- `/api/research/results/{experiment_id}` — full result details for an experiment
- `/api/research/edge-registry` — promoted edges with evidence chain and revalidation schedule

### Edge registry

A queryable view of promoted edges: hypothesis, evidence chain (all result_ids), current status, last revalidation date, next revalidation deadline. Stored as research_conclusions with recommendation='promote', queryable via brain or API.

---

## 7. MVP Recommendation

### Smallest useful version: data foundation + one experiment

**Phase 1 MVP — build this first:**

**New scripts:**
- Operationalize/extend the existing `market_outcome_poller.py` to run on a schedule and backfill all resolved markets. Do NOT create a new resolution_poller.py from scratch.
- `scripts/research/experiment_runner.py` — deterministic statistical engine. Takes spec, assembles dataset, computes metrics, runs tests, saves results.
- `scripts/research/report_generator.py` — produces readable artifact from results.

**New tables:**
- `market_outcomes`
- `research_experiments`
- `research_results`

**First experiment:**
- Hypothesis: "wallet_quality_score >= 70 wallets produce positive copy-trade expectancy after 3% costs"
- Family: wallet_quality_bands (family_size=4)
- Dataset: paper_trades joined with user_analysis, closed only
- Retrospective first, then prospective paper with predeclared 90-day horizon

**Also create (trivial, supports full pipeline from day 1):**
- `research_conclusions` table schema — manual agent review for first experiment, but table exists so conclusions can be recorded immediately

**What NOT to build yet:**
- Price snapshot collector (needs resolution data first)
- Closed-position history poller (expensive API calls, defer until framework validated)
- UI surfaces / API endpoints (CLI reports sufficient for MVP)
- Automated agent interpretation workflow (manual for first experiment)

---

## 8. 30/60/90 Plan

### Days 1-30: Data + Methodology

- Operationalize existing market_outcome_poller.py to backfill all resolved markets in market_outcomes
- Build research_experiments + research_results tables
- Build experiment_runner.py (deterministic statistical engine)
- Build report_generator.py (artifact producer)
- Run first experiment: wallet_quality_score bands x paper trade outcomes
- **Deliverable:** One fully-evidenced finding with p-value, expectancy, CI, cost-adjusted metrics, artifact bundle

### Days 31-60: Experiments + Reports

- Build wallet_closed_positions table + closed-position poller
- Run 3-5 experiments across edge dimensions:
  - Market category profitability (requires resolution data)
  - Entry timing (insider score bands x outcome)
  - Copy slippage (latency x outcome)
  - Entry price band (value vs certainty buying)
- Build cohort_analysis.py and category_heatmap.py
- Start prospective paper validation for any hypothesis passing retrospective
- **Deliverable:** Edge registry with 3+ tested hypotheses, each with full evidence chain

### Days 61-90: Research Workbench

- Operationalize existing poly_price_snapshots collection (schedule hourly midpoints for active markets)
- Build timing_decay.py and slippage_analysis.py
- Build research_conclusions table + agent interpretation workflow
- Add `/api/research/experiments` endpoint for dashboard integration
- Adversarial reviewer reads experiment artifacts, writes structured conclusions
- Revalidation automation: flag experiments approaching 90-day deadline
- **Deliverable:** Functioning research loop — agents propose, scripts test, humans gate

---

## Top 5 Highest-Leverage Changes

Ordered by impact / effort:

**1. Market resolution poller**
Without ground truth on which markets resolved YES/NO, nothing else works. One script, one table, unlocks all retrospective analysis.
*Impact: critical. Effort: small. Files: extend existing `market_outcome_poller.py`, backfill `market_outcomes` table.*

**2. Experiment runner script**
Deterministic statistical engine that takes a spec and produces a result with p-value, expectancy, CI, profit factor. The core of the entire framework. Reusable for every future hypothesis.
*Impact: critical. Effort: medium. Files: `scripts/research/experiment_runner.py`, `research_experiments` + `research_results` tables.*

**3. Wallet closed-position history**
Enables true WR (not zombie-inflated), true P/L ratio, actual drawdown. Transforms wallet evaluation from proxy-based to ground-truth.
*Impact: high. Effort: medium. Files: closed-position poller script, `wallet_closed_positions` table.*

**4. Predeclared experiment specs**
The discipline of writing down "what we're testing, how many trades we need, what would falsify it" before looking at results. Zero code cost, enormous rigor benefit. Start using the experiment spec JSON format immediately.
*Impact: high. Effort: zero.*

**5. Copy latency measurement**
Compare source wallet activity timestamp with paper_trades.opened_at. One query, answers "is our edge being eaten by delay?" Can be done today with existing data.
*Impact: medium. Effort: tiny. Files: one-off query or `scripts/research/slippage_analysis.py`.*
