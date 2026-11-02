# Performance Agents — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build two specialized agents — an Insider Scoring Analyst (calibrates insider detection using paper trade outcomes) and a Strategy Performance Reviewer (monitors plugin strategies, catches parameter drift, recommends pauses) — each with its own brain section in Supabase.

**Architecture:** Two agent files following the wallet_scout_agent pattern (Claude Agent SDK + Bash + obs_tool + predict_tool + action_tool). The Insider Analyst runs weekly, analyzes paper_trades as ground truth, runs sweep functions from `retrospective_analyzer.py`, and writes calibration data to the `signals/` brain section. The Strategy Reviewer runs twice weekly, queries `strategy_positions` per plugin, compares `estimated_win_prob` defaults to actual WR, and writes performance snapshots to the `strategies/` brain section. Neither agent can modify .env, scoring weights, or strategy parameters directly — they recommend only.

**Tech Stack:** Python 3.12, Claude Agent SDK (`claude-sonnet-4-6`), SQLite, Supabase REST, retrospective_analyzer.py

**Spec:** `docs/superpowers/specs/2026-03-20-performance-agents-design.md`

---

## Verified Production Schemas

```sql
-- paper_trades (key columns for analysis)
paper_trades: trade_id, wallet, condition_id, outcome, market_title,
  entry_price, exit_price, size_usdc, pnl_usdc, roi_percent,
  signal_insider_score, signal_speculator_score, wallet_win_rate,
  status (open|resolution_win|resolution_loss|early_exit|manual),
  exit_reason, opened_at, closed_at

-- strategy_positions (key columns)
strategy_positions: position_id, wallet, condition_id, outcome,
  strategy_id, state (open|closed), entry_price, avg_entry_price,
  pnl_usdc, realized_pnl, entry_insider_score, size_usdc,
  opened_ts, closed_ts

-- strategies
strategies: id, wallet, label, strategy_type, parameters (JSON), status

-- Plugin DEFAULTS with estimated_win_prob (hardcoded, NOT in DB params):
--   sports_no_value_v1: 0.85 (strategies/sports_no_value_v1.py:48)
--   political_no_value_v1: 0.88 (strategies/political_no_value_v1.py:51)

-- retrospective_analyzer.py functions:
--   run_analysis(config: AnalysisConfig, conn) -> AnalysisResult
--   sweep_insider_threshold(data_source, thresholds, base_config, conn) -> list[dict]
--   sweep_2d(data_source, insider_thresholds, price_thresholds, base_config, conn) -> dict
```

---

## Chunk 1: Insider Scoring Analyst

### Task 1: Create the insider scoring analyst agent

**Files:**
- Create: `agents/insider_scoring_analyst.py`

- [ ] **Step 1: Write the full agent file**

The agent follows the wallet_scout_agent pattern. SYSTEM_PROMPT includes:
- Role: insider scoring calibration analyst
- Tools: SQLite queries (read-only), retrospective_analyzer.py (via Python in Bash), action_tool.py (recommend only), obs_tool.py (memory), predict_tool.py (self-improvement), upsert_memory via Bash
- Brain Map: owns `signals/` section, reads all others
- DB schema for paper_trades (the columns it queries)
- Safety rules: never modify .env, never modify scoring code, recommend only with evidence, min 30 trades per band, max 10pt change, 7-day cooldown
- Analysis framework: score band WR/PnL, false positive detection, false negative detection, speculator filter effectiveness, sweep functions

The task prompt (build_run_prompt) follows Steps 0-10:

```
STEP 0: Auto-grade predictions
  python3 agents/tools/predict_tool.py auto-grade --agent insider_analyst

STEP 1: Load context
  - Read own observations: python3 agents/tools/obs_tool.py read --strategy insider_analyst --limit 10
  - Read brain health: python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'none')"
  - Read own brain section: python3 -c "from bot.supabase_helper import get_memory; m=get_memory('signals/scoring_accuracy_by_band',type_filter='knowledge'); print(m['content'][:1000] if m else 'first run')"
  - Read lessons: python3 agents/tools/predict_tool.py lessons --agent insider_analyst

STEP 2: Score band analysis
  sqlite3 -json sniper_tracker.db "SELECT
    CASE WHEN signal_insider_score >= 90 THEN '90-100'
         WHEN signal_insider_score >= 80 THEN '80-89'
         WHEN signal_insider_score >= 70 THEN '70-79'
         WHEN signal_insider_score >= 60 THEN '60-69'
         WHEN signal_insider_score >= 50 THEN '50-59'
         ELSE '0-49' END as band,
    COUNT(*) as total,
    SUM(CASE WHEN status='resolution_win' THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(pnl_usdc), 2) as pnl,
    ROUND(AVG(entry_price), 3) as avg_entry
  FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
  GROUP BY band ORDER BY band DESC"

STEP 3: Run threshold sweeps
  python3 -c "
  import sys; sys.path.insert(0, '.')
  from retrospective_analyzer import sweep_insider_threshold, sweep_2d, AnalysisConfig
  import json
  # 1D sweep
  results = sweep_insider_threshold('paper_trades')
  for r in results:
      print(f'insider>={r[\"min_insider_score\"]}: {r[\"trade_count\"]} trades, WR={r[\"win_rate\"]:.1%}, PnL=\${r[\"total_pnl\"]:.2f}')
  # 2D sweep
  grid = sweep_2d('paper_trades')
  print(f'Best cell: insider>={grid[\"best_cell\"][\"insider\"]}, price<={grid[\"best_cell\"][\"price\"]}, WR={grid[\"best_cell\"][\"win_rate\"]:.1%}, PnL=\${grid[\"best_cell\"][\"total_pnl\"]:.2f}')
  "

STEP 4: False positive analysis
  sqlite3 -json sniper_tracker.db "SELECT wallet, signal_insider_score, market_title,
    ROUND(pnl_usdc, 2) as pnl, entry_price, outcome
  FROM paper_trades WHERE signal_insider_score >= 85 AND status='resolution_loss'
  ORDER BY pnl_usdc ASC LIMIT 15"
  -- Group by wallet to find repeat offenders
  sqlite3 -json sniper_tracker.db "SELECT wallet, COUNT(*) as loss_count,
    ROUND(SUM(pnl_usdc), 2) as total_loss, ROUND(AVG(signal_insider_score), 1) as avg_score
  FROM paper_trades WHERE signal_insider_score >= 85 AND status='resolution_loss'
  GROUP BY wallet ORDER BY total_loss ASC LIMIT 10"

STEP 5: False negative analysis
  sqlite3 -json sniper_tracker.db "SELECT wallet, COUNT(*) as win_count,
    ROUND(SUM(pnl_usdc), 2) as total_profit, ROUND(AVG(signal_insider_score), 1) as avg_score
  FROM paper_trades WHERE signal_insider_score < 70 AND status='resolution_win'
  GROUP BY wallet HAVING win_count >= 2 ORDER BY total_profit DESC LIMIT 10"

STEP 6: Speculator filter check
  sqlite3 -json sniper_tracker.db "SELECT
    CASE WHEN signal_speculator_score >= 70 THEN 'would_filter' ELSE 'passes' END as spec_band,
    COUNT(*) as total,
    SUM(CASE WHEN status='resolution_win' THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(pnl_usdc), 2) as pnl
  FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
  GROUP BY spec_band"

STEP 7: Compare to current thresholds + recommend
  Current: PAPER_ENTRY_MIN_INSIDER_SCORE=70
  If sweep shows optimal at 80 or 90 with >30 trade sample and >5pt improvement:
    python3 agents/tools/action_tool.py recommend \
      --strategy-id 0 --strategy insider_copy_v1 \
      --recommendation "Raise PAPER_ENTRY_MIN_INSIDER_SCORE from 70 to {optimal}" \
      --reason "Sweep shows WR={wr}% at {optimal} vs {current_wr}% at 70. Sample={n}. PnL improvement: ${delta}"

STEP 8: Update brain section
  Write signals/scoring_accuracy_by_band to Supabase with latest band data
  Write signals/threshold_sweep_latest with sweep results
  If false positives found, write signals/false_positives with wallet list

STEP 9: Write observations
  - diagnosis: current scoring accuracy summary with numbers
  - pattern: any new patterns discovered (e.g., "sports tennis at 100 score = all losses")
  - run_summary: what was found, what was recommended

STEP 10: Predictions + log run
  Make 2+ predictions (e.g., "insider 90+ WR will stay above 60% next week")
  python3 agents/tools/predict_tool.py log-run --agent insider_analyst ...
```

Runner: same pattern as wallet_scout_agent.py (claude-sonnet-4-6, $1.50 budget, 60 turns, bypassPermissions).

- [ ] **Step 2: Verify syntax**

Run: `python3 -c "import agents.insider_scoring_analyst"`

- [ ] **Step 3: Commit**

---

### Task 2: Register + systemd timer for insider analyst

**Files:**
- Modify: `scripts/run_strategy_agent.py`
- Create: `systemd/sniper-agent-insider-analyst.service`
- Create: `systemd/sniper-agent-insider-analyst.timer`

- [ ] **Step 1: Add to AGENT_REGISTRY**

```python
"insider_analyst": ("agents.insider_scoring_analyst", "run"),
```

- [ ] **Step 2: Create service (oneshot, 900s timeout)**

- [ ] **Step 3: Create timer (weekly Sunday 14:00 UTC)**

```ini
[Timer]
OnCalendar=Sun *-*-* 14:00:00
Persistent=true
RandomizedDelaySec=300
```

- [ ] **Step 4: Commit**

---

## Chunk 2: Strategy Performance Reviewer

### Task 3: Create the strategy performance reviewer agent

**Files:**
- Create: `agents/strategy_reviewer_agent.py`

- [ ] **Step 1: Write the full agent file**

SYSTEM_PROMPT includes:
- Role: strategy plugin performance reviewer
- Tools: same as insider analyst + strategy_positions queries
- Brain Map: owns `strategies/` section, reads all others
- DB schema for strategy_positions and strategies tables
- Plugin DEFAULTS reference (hardcoded win probs): sports_no=0.85, political_no=0.88
- Safety rules: can pause via action_tool (safe), recommend params, CANNOT modify DB params directly, CANNOT create/delete strategies, min 5 closed positions before recommending
- Analysis framework: per-plugin WR/PnL, estimated vs actual WR, consecutive loss detection, early exit attribution

Task prompt Steps 0-10:

```
STEP 0: Auto-grade predictions
STEP 1: Load context (own observations, brain health, own brain section)

STEP 2: Per-plugin performance
  sqlite3 -json sniper_tracker.db "SELECT s.id, s.label, s.strategy_type, s.status, s.parameters,
    COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed,
    SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl,
    COUNT(CASE WHEN sp.state='open' THEN 1 END) as open_ct,
    ROUND(SUM(CASE WHEN sp.state='open' THEN sp.pnl_usdc ELSE 0 END), 2) as open_pnl
  FROM strategies s
  LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE s.strategy_type NOT IN ('insider_copy_v1')
  GROUP BY s.id ORDER BY s.status, closed_pnl"

STEP 3: Win probability calibration
  For each plugin with estimated_win_prob:
    Compare hardcoded default vs actual closed WR
    sports_no_value_v1: default=0.85, check actual
    political_no_value_v1: default=0.88, check actual
  Flag if divergence > 15 percentage points

STEP 4: Consecutive loss detection
  sqlite3 -json sniper_tracker.db "SELECT sp.strategy_id, s.label,
    sp.pnl_usdc, sp.closed_ts
  FROM strategy_positions sp
  JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE sp.state='closed'
  ORDER BY sp.strategy_id, sp.closed_ts DESC"
  -- Count consecutive losses from most recent trade backward

STEP 5: Early exit attribution
  sqlite3 -json sniper_tracker.db "SELECT s.label,
    COUNT(*) as early_exits, ROUND(SUM(sp.pnl_usdc), 2) as exit_pnl,
    ROUND(AVG(sp.pnl_usdc), 2) as avg_exit_pnl
  FROM strategy_positions sp
  JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE sp.state='closed' AND sp.realized_pnl < 0
  GROUP BY s.label"

STEP 6: Cross-strategy ranking
  Rank all plugins by: total PnL, WR, PnL per trade
  Identify best and worst performers

STEP 7: Recommend actions
  - Pause if 5+ consecutive losses: action_tool.py pause --strategy-id X --reason "..."
  - Recommend win_prob update if divergence > 15pts: action_tool.py recommend
  - Flag strategies losing > $50 total: action_tool.py flag

STEP 8: Update brain section
  Write strategies/plugin_performance_snapshot with per-plugin data
  Write strategies/bleeding_watchlist if any strategies are losing

STEP 9: Observations + predictions
STEP 10: Log run
```

- [ ] **Step 2: Verify syntax**
- [ ] **Step 3: Commit**

---

### Task 4: Register + systemd timer for strategy reviewer

**Files:**
- Modify: `scripts/run_strategy_agent.py`
- Create: `systemd/sniper-agent-strategy-reviewer.service`
- Create: `systemd/sniper-agent-strategy-reviewer.timer`

- [ ] **Step 1: Add to AGENT_REGISTRY**

```python
"strategy_reviewer": ("agents.strategy_reviewer_agent", "run"),
```

- [ ] **Step 2: Create service**

- [ ] **Step 3: Create timer (Wed + Sat 15:00 UTC)**

```ini
[Timer]
OnCalendar=Wed *-*-* 15:00:00
OnCalendar=Sat *-*-* 15:00:00
Persistent=true
RandomizedDelaySec=300
```

- [ ] **Step 4: Commit**

---

## Chunk 3: Brain Manifest Update + Tests

### Task 5: Update brain manifest with new sections

**Files:**
- Modify: `scripts/brain_manifest.py`

- [ ] **Step 1: Add `strategies/` section and update `signals/` owner**

In the MANIFEST dict:
- Change `signals.owner` from `"insider_copy"` to `"insider_scoring_analyst"`
- Add new section:
```python
"strategies": {
    "owner": "strategy_reviewer",
    "purpose": "Plugin strategy performance analysis, win_prob calibration, bleeding detection",
    "writes_to": {
        "observations": "strategy_type=strategy_reviewer",
        "memories": ["strategies/plugin_performance_snapshot", "strategies/winprob_calibration",
                     "strategies/bleeding_watchlist"]
    },
    "read_by": ["all agents (cross-read in Step 1)"],
    "schedule": "twice weekly (Wed + Sat 15:00 UTC)"
}
```

- [ ] **Step 2: Run manifest on VPS to update**
- [ ] **Step 3: Commit**

---

### Task 6: Integration tests

**Files:**
- Create: `tests/test_performance_agents.py`

- [ ] **Step 1: Write tests**

```python
import sqlite3
import pytest

@pytest.fixture
def perf_db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE paper_trades (
            trade_id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT,
            outcome TEXT, market_title TEXT, entry_price REAL,
            exit_price REAL, size_usdc REAL, pnl_usdc REAL,
            signal_insider_score REAL, signal_speculator_score REAL,
            status TEXT, opened_at TEXT, closed_at TEXT
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY, strategy_id TEXT,
            condition_id TEXT, outcome TEXT, state TEXT,
            entry_price REAL, pnl_usdc REAL, realized_pnl REAL,
            opened_ts INTEGER, closed_ts INTEGER
        );
    """)
    return p, c


def test_insider_band_analysis(perf_db):
    """Score band WR query returns correct grouping."""
    p, c = perf_db
    # Insert trades in different bands
    c.executemany("INSERT INTO paper_trades (trade_id, wallet, signal_insider_score, status, pnl_usdc, entry_price) VALUES (?,?,?,?,?,?)", [
        (1, "0xa", 95, "resolution_win", 10, 0.5),
        (2, "0xa", 92, "resolution_win", 8, 0.4),
        (3, "0xa", 91, "resolution_loss", -12, 0.6),
        (4, "0xb", 75, "resolution_win", 5, 0.3),
        (5, "0xb", 72, "resolution_loss", -8, 0.5),
        (6, "0xb", 71, "resolution_loss", -7, 0.4),
    ])
    c.commit()
    rows = c.execute("""
        SELECT CASE WHEN signal_insider_score >= 90 THEN '90-100'
                    WHEN signal_insider_score >= 70 THEN '70-79' END as band,
          COUNT(*) as total,
          SUM(CASE WHEN status='resolution_win' THEN 1 ELSE 0 END) as wins,
          ROUND(SUM(pnl_usdc), 2) as pnl
        FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
        GROUP BY band ORDER BY band DESC
    """).fetchall()
    assert len(rows) == 2
    # 90-100 band: 2 wins, 1 loss, PnL = 10+8-12 = 6
    assert rows[0][1] == 3  # total
    assert rows[0][2] == 2  # wins
    assert rows[0][3] == 6.0  # pnl
    # 70-79 band: 1 win, 2 losses
    assert rows[1][2] == 1


def test_false_positive_detection(perf_db):
    """High-score losses grouped by wallet."""
    p, c = perf_db
    c.executemany("INSERT INTO paper_trades (trade_id, wallet, signal_insider_score, status, pnl_usdc, entry_price, market_title) VALUES (?,?,?,?,?,?,?)", [
        (1, "0xbad", 100, "resolution_loss", -50, 0.5, "Tennis A"),
        (2, "0xbad", 100, "resolution_loss", -40, 0.6, "Tennis B"),
        (3, "0xbad", 95, "resolution_loss", -30, 0.4, "Tennis C"),
        (4, "0xgood", 92, "resolution_win", 20, 0.5, "Politics A"),
    ])
    c.commit()
    rows = c.execute("""
        SELECT wallet, COUNT(*) as loss_count, ROUND(SUM(pnl_usdc), 2) as total_loss
        FROM paper_trades WHERE signal_insider_score >= 85 AND status='resolution_loss'
        GROUP BY wallet ORDER BY total_loss ASC
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xbad"
    assert rows[0][1] == 3
    assert rows[0][2] == -120.0


def test_plugin_performance_query(perf_db):
    """Per-plugin WR and PnL aggregation."""
    p, c = perf_db
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1, 'auto', 'Sports NO', 'sports_no_value_v1', 'active', '2026-03-20', '2026-03-20')")
    c.executemany("INSERT INTO strategy_positions (position_id, strategy_id, state, pnl_usdc, opened_ts, closed_ts) VALUES (?,?,?,?,?,?)", [
        (1, "1", "closed", 5, 1000, 2000),
        (2, "1", "closed", -15, 1000, 2000),
        (3, "1", "closed", -10, 1000, 2000),
        (4, "1", "open", 2, 1000, 0),
    ])
    c.commit()
    rows = c.execute("""
        SELECT s.label,
          COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed,
          SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
          ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl
        FROM strategies s
        LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id
        GROUP BY s.id
    """).fetchall()
    assert rows[0][1] == 3  # 3 closed
    assert rows[0][2] == 1  # 1 win
    assert rows[0][3] == -20.0  # total pnl


def test_consecutive_losses(perf_db):
    """Detect consecutive losses from most recent trades."""
    p, c = perf_db
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1, 'auto', 'Loser', 'term_structure_v1', 'active', '2026-03-20', '2026-03-20')")
    c.executemany("INSERT INTO strategy_positions (position_id, strategy_id, state, pnl_usdc, closed_ts) VALUES (?,?,?,?,?)", [
        (1, "1", "closed", -10, 100),
        (2, "1", "closed", -8, 200),
        (3, "1", "closed", -12, 300),
        (4, "1", "closed", 5, 400),  # one win breaks the streak
        (5, "1", "closed", -7, 500),
    ])
    c.commit()
    # Count recent consecutive losses (from most recent backward)
    rows = c.execute("""
        SELECT pnl_usdc FROM strategy_positions
        WHERE strategy_id='1' AND state='closed'
        ORDER BY closed_ts DESC
    """).fetchall()
    streak = 0
    for r in rows:
        if r[0] <= 0:
            streak += 1
        else:
            break
    assert streak == 1  # only the most recent is a loss, then a win at position 4
```

- [ ] **Step 2: Run tests + full suite**

Run: `python3 -m pytest tests/test_performance_agents.py -v && python3 -m pytest tests/ -q`

- [ ] **Step 3: Commit**

---

### Task 7: Deploy to VPS

- [ ] **Step 1: Push all code**
- [ ] **Step 2: Pull on VPS and install timers**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && git pull origin main'
ssh sniper@188.166.91.20 'for f in sniper-agent-insider-analyst sniper-agent-strategy-reviewer; do cp ~/sniper/systemd/$f.service ~/.config/systemd/user/ && cp ~/sniper/systemd/$f.timer ~/.config/systemd/user/; done && systemctl --user daemon-reload && systemctl --user enable --now sniper-agent-insider-analyst.timer sniper-agent-strategy-reviewer.timer'
```

- [ ] **Step 3: Run brain_manifest.py to update sections**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^SUPABASE" .env | xargs) && .venv/bin/python3 scripts/brain_manifest.py'
```

- [ ] **Step 4: Manual test run of insider analyst**

```bash
ssh sniper@188.166.91.20 'systemctl --user start sniper-agent-insider-analyst.service'
# Wait ~5 min, check logs + observations
ssh sniper@188.166.91.20 'journalctl --user -u sniper-agent-insider-analyst --since "10 min ago" --no-pager | tail -20'
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^SUPABASE" .env | xargs) && .venv/bin/python3 agents/tools/obs_tool.py read --strategy insider_analyst --limit 3'
```

- [ ] **Step 5: Manual test run of strategy reviewer**

```bash
ssh sniper@188.166.91.20 'systemctl --user start sniper-agent-strategy-reviewer.service'
# Wait ~5 min, check
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^SUPABASE" .env | xargs) && .venv/bin/python3 agents/tools/obs_tool.py read --strategy strategy_reviewer --limit 3'
```

- [ ] **Step 6: Verify timers**

```bash
ssh sniper@188.166.91.20 'systemctl --user list-timers --no-pager | grep sniper'
```
