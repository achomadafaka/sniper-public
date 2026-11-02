# Performance Agents Design Spec

## Overview

Two new specialized agents that own performance analysis across the two position types:

1. **Insider Scoring Analyst** — owns the insider detection system calibration
2. **Strategy Performance Reviewer** — owns all plugin strategy parameter health

## Agent 1: Insider Scoring Analyst

### Role
Personalized analyst for the paper trader's core insider copy strategy. Studies paper trade outcomes as ground truth to continuously validate and improve the insider scoring system.

### Brain Section: `signals/`
Persistent reference data this agent builds over time:
- `signals/scoring_accuracy_by_band` — WR and PnL per insider score band (updated each run)
- `signals/false_positives` — wallets with high insider scores that consistently lose
- `signals/false_negatives` — wallets with lower scores that would have been profitable
- `signals/threshold_sweep_latest` — last sweep_2d result with optimal frontier
- `signals/calibration_history` — running log of threshold recommendations + outcomes

### What it does each run
1. Auto-grade predictions (Step 0)
2. Read brain health + own section history
3. Pull all resolved paper trades, compute WR/PnL by insider score band
4. Run `sweep_insider_threshold()` with current data
5. Run `sweep_2d()` (insider x entry_price) to find optimal frontier
6. Compare current thresholds (PAPER_ENTRY_MIN_INSIDER_SCORE=70) to optimal
7. Identify false positives (insider 90+ wallets that lost) — investigate WHY
8. Identify false negatives (insider 50-69 wallets that won repeatedly)
9. Check if speculator_score filter is adding value or filtering out winners
10. Write recommendations via action_tool.py recommend
11. Update `signals/` brain section with latest calibration data
12. Make predictions + log run

### Safety rules
- NEVER modify scoring weights (hardcoded in analyze_users.py) — only recommend
- NEVER modify .env directly — only recommend threshold changes with backtest evidence
- Recommendations must include: current_value, recommended_value, backtest WR, backtest PnL, sample_size, confidence
- Minimum 30 resolved trades in the affected band before recommending changes
- Maximum parameter change of 10 points per recommendation (e.g., 70→80, not 70→90)
- Cooldown: don't re-recommend the same parameter within 7 days

### Schedule
Weekly (Sunday 14:00 UTC). Scoring system changes slowly — daily is overkill.

### Tools
- SQLite queries (paper_trades, user_analysis, score_history)
- `retrospective_analyzer.py` via Python (sweep functions)
- `action_tool.py recommend`
- `obs_tool.py` (observations)
- `predict_tool.py` (self-improvement)
- `upsert_memory()` for brain section updates

### Key queries
```sql
-- WR by insider band
SELECT CASE WHEN signal_insider_score >= 90 THEN '90+' ... END as band,
  COUNT(*), SUM(CASE WHEN status='resolution_win' THEN 1 END), SUM(pnl_usdc)
FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
GROUP BY band

-- False positives: high score losses
SELECT wallet, signal_insider_score, market_title, pnl_usdc
FROM paper_trades WHERE signal_insider_score >= 85 AND status='resolution_loss'

-- Speculator filter effectiveness
SELECT CASE WHEN signal_speculator_score >= 70 THEN 'filtered' ELSE 'passed' END,
  COUNT(*), SUM(CASE WHEN status='resolution_win' THEN 1 END)
FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
GROUP BY 1
```

---

## Agent 2: Strategy Performance Reviewer

### Role
Owns all plugin strategy health. Reviews each strategy's parameter-to-reality fit, catches bleeding strategies, recommends pauses and parameter changes.

### Brain Section: `strategies/`
Persistent reference data:
- `strategies/plugin_performance_snapshot` — per-plugin WR, PnL, trade count (updated each run)
- `strategies/winprob_calibration` — estimated_win_prob vs actual WR per plugin
- `strategies/parameter_drift_log` — history of recommended changes + whether they helped
- `strategies/bleeding_watchlist` — strategies with 3+ consecutive losses

### What it does each run
1. Auto-grade predictions (Step 0)
2. Read brain health + own section history
3. For EACH active plugin strategy:
   a. Compute actual WR vs `estimated_win_prob` parameter
   b. Check PnL trend (last 10 closed trades)
   c. Count consecutive wins/losses
   d. Check if entry price gates are filtering correctly
   e. Check max_concurrent utilization (are we full?)
4. Cross-strategy comparison: which plugins are making money, which are losing?
5. Check early_exit attribution: are stop-losses saving money or cutting winners?
6. Recommend pause for strategies with 5+ consecutive losses
7. Recommend param changes when estimated vs actual WR diverges by >15 points
8. Update `strategies/` brain section with latest performance data
9. Make predictions + log run

### Strategies it covers
- sports_no_value_v1 (estimated_win_prob=0.85, actual=27% — BROKEN)
- political_no_value_v1 (estimated_win_prob=0.88)
- cross_arb_v1
- news_intel_v1
- sports_line_dislocation_v1
- term_structure_v1 (paused, -$120)
- crypto_momentum_v1 (paused, BTC momentum)

### Safety rules
- Can recommend pausing a strategy via action_tool.py pause (already safe)
- Can recommend parameter changes via action_tool.py recommend
- CANNOT modify strategy parameters directly in the DB
- CANNOT create or delete strategies (Scout's job)
- Parameter recommendations must include backtest evidence
- Minimum 5 closed positions before recommending plugin param changes
- Must compare against both all-time AND last-30-day performance

### Schedule
Twice weekly (Wednesday + Saturday 15:00 UTC). Plugin performance changes slowly.

### Tools
Same as Insider Scoring Analyst plus:
- `strategy_positions` queries for per-plugin performance
- Can read strategy params from `strategies.parameters` JSON

### Key queries
```sql
-- Per-plugin actual WR
SELECT s.label, s.strategy_type,
  COUNT(*) as closed,
  SUM(CASE WHEN sp.realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
  ROUND(SUM(sp.pnl_usdc), 2) as total_pnl
FROM strategy_positions sp
JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
WHERE sp.state = 'closed'
GROUP BY s.label

-- Consecutive losses per strategy
SELECT strategy_id, COUNT(*) as streak
FROM (
  SELECT strategy_id, pnl_usdc,
    ROW_NUMBER() OVER (PARTITION BY strategy_id ORDER BY closed_ts DESC) as rn
  FROM strategy_positions WHERE state='closed'
) WHERE pnl_usdc <= 0 AND rn <= 10
GROUP BY strategy_id

-- estimated_win_prob vs reality
-- (requires reading strategy.parameters JSON + computing actual WR)
```

---

## Safety Architecture

### Parameter change flow
```
Agent detects divergence → writes recommendation to Supabase (action_tool recommend)
  → Telegram alert with evidence
  → Human reviews (approve/reject)
  → If approved: human edits .env or DB params manually
  → Agent detects change on next run, records outcome
```

### What agents CANNOT do
- Modify .env files
- Modify scoring weights (analyze_users.py code)
- Update strategy parameters in SQLite directly
- Create/delete strategies (Scout's job)
- Modify paper_settings.py constants at runtime

### Validation requirements for recommendations
- Minimum sample size: 30 trades (insider), 5 trades (plugins)
- Maximum change: 10 points per threshold parameter per week
- Must include: current_value, recommended_value, backtest_wr, backtest_pnl, sample_size
- 7-day cooldown between repeated recommendations for same parameter

---

## Brain Section Updates

Update brain_manifest to add `strategies/` section:
```json
"strategies": {
  "owner": "strategy_reviewer",
  "purpose": "Plugin strategy performance analysis, parameter calibration, bleeding detection",
  "writes_to": {
    "observations": "strategy_type=strategy_reviewer",
    "memories": ["strategies/plugin_performance_snapshot", "strategies/winprob_calibration",
                 "strategies/parameter_drift_log", "strategies/bleeding_watchlist"]
  },
  "read_by": ["all agents (cross-read in Step 1)"],
  "schedule": "twice weekly (Wed + Sat 15:00 UTC)"
}
```

Update `signals` section owner to `insider_scoring_analyst`.

---

## Immediate findings (from research)

These should be addressed regardless of agent timeline:

1. **Sports NO Value estimated_win_prob=0.85 vs actual 27%** — this strategy is sized assuming 85% wins but only winning 27%. Kelly sizing is dangerously wrong.
2. **Term Structure -$120 and still running 3 open losers** — should have been paused weeks ago.
3. **80-89 insider band is -$144** — the paper trader enters at insider >= 70 but only 90+ is profitable. The threshold may need raising.
4. **Top 10 highest-scored trades (all 100) are ALL losses** — sports tennis bets. High insider score ≠ profitable in sports.
5. **No parameter bounds validation** — strategy params accept any value. Needs range enforcement.

---

## Implementation order

1. Build Insider Scoring Analyst first (paper trader is the core revenue engine)
2. Build Strategy Performance Reviewer second
3. As individual plugins prove themselves, spin off dedicated agents
