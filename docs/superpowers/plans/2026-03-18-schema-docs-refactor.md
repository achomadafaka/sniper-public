# Sniper Schema, Docs & db.py Refactor Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document the agent system, expand Supabase from 3 to 7 tables for structured intelligence, and split the 11,342-line db.py god object into focused modules.

**Architecture:** Three independent phases that each deliver standalone value. Phase 1 (docs) is zero-risk. Phase 2 (Supabase) adds new tables without touching existing ones. Phase 3 (db.py split) is a pure refactor with no behavior changes — every function keeps its exact signature and import path via re-exports.

**Tech Stack:** Python 3.12, SQLite WAL, Supabase (Postgres), Claude Agent SDK

---

## Chunk 1: Agent Documentation (Phase 1)

### Task 1: Create insider-copy agent definition

**Files:**
- Create: `.claude/agents/insider-copy-agent.md`

- [ ] **Step 1: Write the agent definition**

```markdown
---
name: insider-copy-agent
description: Run the InsiderCopy strategy agent — analyzes wallet scores, researches traders on Polymarket, checks position health, writes observations to Supabase. Use when asked to analyze insider copy strategies or research tracked wallets.

<example>
Context: User wants to check on copy trading strategies
user: "How are the insider copy strategies doing?"
assistant: "I'll use the insider-copy-agent to analyze all active strategies."
</example>

model: sonnet
tools: ["Bash", "Read", "WebSearch", "WebFetch"]
---

You run the InsiderCopy strategy agent for the Sniper system.

## Quick Run

```bash
cd ~/sniper && .venv/bin/python3 scripts/run_strategy_agent.py insider_copy --verbose
```

## What It Does

1. Reads past observations from Supabase (`agents/tools/obs_tool.py read`)
2. Queries all insider_copy_v1 strategies and their tracked wallets
3. Checks wallet score trends via `score_history` table
4. Researches wallets on Polymarket API + web search
5. Analyzes win/loss patterns and exit reasons
6. Takes safe actions if needed: pause bleeding strategies, flag wallets, recommend param changes
7. Writes observations to Supabase for cross-session memory

## Tools

- `agents/tools/obs_tool.py` — Supabase observation read/write
- `agents/tools/action_tool.py` — pause, flag, recommend, alert
- SQLite queries via `sqlite3 -json sniper_tracker.db`

## Schedule

Runs twice daily at 6AM and 6PM UTC via `sniper-agent-insider-copy.timer`.
Budget cap: $1.00/run. Max turns: 60.
```

- [ ] **Step 2: Commit**
```bash
git add .claude/agents/insider-copy-agent.md
git commit -m "docs: add insider-copy-agent definition for Claude Code"
```

### Task 2: Create world-analyst agent definition

**Files:**
- Create: `.claude/agents/world-analyst.md`

- [ ] **Step 1: Write the agent definition**

```markdown
---
name: world-analyst
description: Run the World Analyst agent — researches geopolitical events, maps them to Polymarket markets, writes world_event memories to Supabase for the Intel tab. Use when asked about world events, geopolitical analysis, or market intelligence.

<example>
Context: User wants fresh world intelligence
user: "Update the intel tab with latest events"
assistant: "I'll use the world-analyst agent to research current events and update the brain."
</example>

model: sonnet
tools: ["Bash", "Read", "WebSearch", "WebFetch"]
---

You run the World Analyst for the Sniper system.

## Quick Run

```bash
cd ~/sniper && .venv/bin/python3 scripts/world_analyst.py --quick
# Full analysis (all 5 domains):
cd ~/sniper && .venv/bin/python3 scripts/world_analyst.py
```

## What It Does

1. Searches 5 intelligence domains: geo, politics, finance, sports, crypto
2. Analyzes events from last 24-48h via web search
3. Maps events to open Polymarket markets
4. Writes structured `world_event:*` memories to Supabase
5. Generates `market_boosts_current` for active trading adjustments
6. Sends Telegram summary of key findings

## Domains

- `geo` — military conflicts, territorial disputes, sanctions
- `politics` — elections, legislation, executive orders
- `finance` — central bank decisions, market crashes, crypto regulation
- `sports` — injuries, suspensions, team changes
- `crypto` — protocol upgrades, exchange events, regulatory actions

## Schedule

Runs every 4 hours via cron. Also triggers on bot session stop (relay.py hook).
Budget cap: $1.50/run. Max turns: 80.
```

- [ ] **Step 2: Commit**
```bash
git add .claude/agents/world-analyst.md
git commit -m "docs: add world-analyst agent definition for Claude Code"
```

### Task 3: Create agents & tools index doc

**Files:**
- Create: `docs/AGENTS_AND_TOOLS.md`

- [ ] **Step 1: Write the index document**

Document all 4 agents (insider-copy, world-analyst, deploy-and-verify, vps-health), 3 skills (db-stats, deploy, proof-bar), and 2 tools (obs_tool, action_tool) with purpose, schedule, and cross-references.

- [ ] **Step 2: Commit**
```bash
git add docs/AGENTS_AND_TOOLS.md
git commit -m "docs: add agents and tools index"
```

### Task 4: Create Supabase schema doc

**Files:**
- Create: `docs/SUPABASE_SCHEMA.md`

- [ ] **Step 1: Write the schema document**

Document all 3 current tables (memories, session_logs, strategy_agent_observations) with columns, types, constraints, indexes, RLS policies, and usage patterns. Include the 5 memory types and 7 observation types.

- [ ] **Step 2: Commit**
```bash
git add docs/SUPABASE_SCHEMA.md
git commit -m "docs: add Supabase schema reference"
```

---

## Chunk 2: Supabase Schema Expansion (Phase 2)

### Task 5: Add `world_events` table

**Files:**
- Create: `migrations/supabase_002_world_events.sql`
- Modify: `world_context.py` — read from new table instead of memory rows
- Modify: `scripts/world_analyst.py` — write to new table
- Test: `tests/test_world_events_table.py`

Currently world events are stored as `world_event:*` rows in the `memories` table with unstructured text content. This makes querying by region, status, or trajectory impossible.

- [ ] **Step 1: Write migration SQL**

```sql
CREATE TABLE IF NOT EXISTS world_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    region TEXT,
    status TEXT CHECK (status IN ('active','developing','monitoring','resolved')) DEFAULT 'monitoring',
    trajectory TEXT CHECK (trajectory IN ('escalating','de-escalating','stable','unknown')) DEFAULT 'unknown',
    status_note TEXT,
    trajectory_note TEXT,
    key_developments JSONB DEFAULT '[]',
    connected_events JSONB DEFAULT '[]',
    market_connections JSONB DEFAULT '[]',
    source TEXT DEFAULT 'world_analyst',
    last_updated TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_world_events_status ON world_events(status);
CREATE INDEX IF NOT EXISTS idx_world_events_region ON world_events(region);
CREATE INDEX IF NOT EXISTS idx_world_events_updated ON world_events(last_updated DESC);

ALTER TABLE world_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS "Allow all for world_events"
    ON world_events FOR ALL USING (true) WITH CHECK (true);
```

- [ ] **Step 2: Write test for world_events read/write**
- [ ] **Step 3: Update `world_context.py` to read from new table (with memory fallback)**
- [ ] **Step 4: Run test, verify pass**
- [ ] **Step 5: Commit**

### Task 6: Add `performance_snapshots` table

**Files:**
- Create: `migrations/supabase_003_performance_snapshots.sql`
- Modify: `bot/supabase_helper.py` — sync_project_state writes snapshots
- Test: `tests/test_performance_snapshots.py`

Track proof bar, WR, PnL over time for trend analysis. Currently sync-state overwrites the same memory row — no history.

- [ ] **Step 1: Write migration SQL**

```sql
CREATE TABLE IF NOT EXISTS performance_snapshots (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    snapshot_type TEXT NOT NULL CHECK (snapshot_type IN ('proof_bar','strategy','overall')),
    strategy_type TEXT,
    closed_trades INT,
    win_rate REAL,
    total_pnl REAL,
    open_positions INT,
    insider_60_closed INT,
    insider_60_wr REAL,
    insider_60_pnl REAL,
    metadata JSONB DEFAULT '{}',
    captured_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_perf_snap_type ON performance_snapshots(snapshot_type, captured_at DESC);

ALTER TABLE performance_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS "Allow all for performance_snapshots"
    ON performance_snapshots FOR ALL USING (true) WITH CHECK (true);
```

- [ ] **Step 2: Write test**
- [ ] **Step 3: Update sync_project_state to INSERT a snapshot each run (in addition to upserting memory)**
- [ ] **Step 4: Run test, verify pass**
- [ ] **Step 5: Commit**

### Task 7: Add `agent_runs` table

**Files:**
- Create: `migrations/supabase_004_agent_runs.sql`
- Modify: `agents/insider_copy_agent.py` — log run metadata
- Modify: `scripts/world_analyst.py` — log run metadata
- Test: `tests/test_agent_runs.py`

Track every agent execution: which agent, model used, cost, turns, duration, findings summary, actions taken.

- [ ] **Step 1: Write migration SQL**

```sql
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    agent_type TEXT NOT NULL,
    model TEXT,
    cost_usd REAL,
    turns INT,
    duration_seconds REAL,
    findings_summary TEXT,
    actions_taken JSONB DEFAULT '[]',
    status TEXT CHECK (status IN ('success','error','timeout','budget_exceeded')) DEFAULT 'success',
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_type ON agent_runs(agent_type, created_at DESC);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS "Allow all for agent_runs"
    ON agent_runs FOR ALL USING (true) WITH CHECK (true);
```

- [ ] **Step 2: Write test**
- [ ] **Step 3: Add run logging to both agents (after ResultMessage)**
- [ ] **Step 4: Run test, verify pass**
- [ ] **Step 5: Commit**

---

## Chunk 3: db.py Refactoring (Phase 3)

**Strategy:** Extract functions into new modules. Keep `db.py` as the public API by re-exporting everything. No import changes needed anywhere in the codebase. Each split is a pure move with zero behavior change.

### Task 8: Extract `db_utils.py` — utility helpers

**Files:**
- Create: `db_utils.py`
- Modify: `db.py` — move functions, add re-exports

**Functions to move** (~400 lines):
- `_coerce_float()`, `_coerce_int()`, `_safe_str()`
- `_normalize_wallet()`, `_normalize_outcome()`
- `_classify_title_domain()`
- `compute_followability_score()`, `_has_followability_inputs()`
- `_sort_leaderboard_v2()`
- All `_ensure_column()` calls

- [ ] **Step 1: Run full test suite — establish baseline**
Run: `python3 -m pytest tests/ -q`

- [ ] **Step 2: Create `db_utils.py` with moved functions**
- [ ] **Step 3: In `db.py`, replace function bodies with imports from `db_utils`**
```python
from db_utils import _coerce_float, _coerce_int, _safe_str  # etc.
```
- [ ] **Step 4: Run full test suite — must match baseline exactly**
- [ ] **Step 5: Commit**

### Task 9: Extract `db_snapshots.py` — snapshot table handlers

**Files:**
- Create: `db_snapshots.py`
- Modify: `db.py` — move + re-export

**Functions to move** (~1000 lines):
- All `ensure_*_table()` + `insert_*_snapshot()` + `recent_*_snapshots()` for:
  weather, commodity, news, odds, poly_price, edge_observations

- [ ] **Step 1: Create `db_snapshots.py`**
- [ ] **Step 2: Replace in `db.py` with re-exports**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 10: Extract `db_leaderboards.py` — ranking functions

**Files:**
- Create: `db_leaderboards.py`
- Modify: `db.py` — move + re-export

**Functions to move** (~1100 lines):
- `wallet_leaderboard_rows()` (383 lines)
- `wallet_followable_leaderboard_rows()` (318 lines)
- `wallet_leaderboard_v2_rows()` (209 lines)
- `high_winrate_wallets()`
- Related sorting/scoring helpers

- [ ] **Step 1: Create `db_leaderboards.py`**
- [ ] **Step 2: Replace in `db.py` with re-exports**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 11: Extract `db_signals.py` — position signal management

**Files:**
- Create: `db_signals.py`
- Modify: `db.py` — move + re-export

**Functions to move** (~500 lines):
- `upsert_position_signal()` (254 lines)
- `position_signals()`, `thesis_signal_rows()`
- `closed_signals_for_wallet()`
- `market_convergence_signals()`, `convergence_from_history()`

- [ ] **Step 1: Create `db_signals.py`**
- [ ] **Step 2: Replace in `db.py` with re-exports**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 12: Extract `db_followed.py` — followed wallet operations

**Files:**
- Create: `db_followed.py`
- Modify: `db.py` — move + re-export

**Functions to move** (~700 lines):
- `upsert_followed_trade()`, `followed_trades_for_wallet()`
- `sync_auto_followed_positions_for_wallet()`
- `followed_wallet_research_scope_wallets()`
- `capture_position_snapshots()`, `position_snapshot_history()`
- Watchlist CRUD functions

- [ ] **Step 1: Create `db_followed.py`**
- [ ] **Step 2: Replace in `db.py` with re-exports**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 13: Extract `db_replay.py` — retrospective analysis

**Files:**
- Create: `db_replay.py`
- Modify: `db.py` — move + re-export

**Functions to move** (~600 lines):
- `analyzer_paper_trades()`, `analyzer_signal_outcomes()`, `analyzer_fwa_trades()`
- `replay_curated_signals()`, `insert_replay_config()`
- Replay run management functions

- [ ] **Step 1: Create `db_replay.py`**
- [ ] **Step 2: Replace in `db.py` with re-exports**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 14: Final cleanup and verification

**Files:**
- Modify: `db.py` — verify all re-exports work
- Modify: `CLAUDE.md` — update architecture table

- [ ] **Step 1: Verify `db.py` line count dropped from 11,342 to ~5,000**
```bash
wc -l db.py db_utils.py db_snapshots.py db_leaderboards.py db_signals.py db_followed.py db_replay.py
```

- [ ] **Step 2: Verify ALL imports still work**
```bash
python3 -c "from db import *; print('All re-exports OK')"
```

- [ ] **Step 3: Run full test suite one final time**
```bash
python3 -m pytest tests/ -q
```

- [ ] **Step 4: Update CLAUDE.md architecture table**
Add new modules to the component table.

- [ ] **Step 5: Final commit**
```bash
git add -A
git commit -m "refactor: split db.py god object into 6 focused modules

db.py: 11,342 → ~5,000 lines (core CRUD, init, connections)
db_utils.py: ~400 lines (coerce, normalize, classify helpers)
db_snapshots.py: ~1,000 lines (weather, odds, price, edge snapshots)
db_leaderboards.py: ~1,100 lines (wallet rankings, followability)
db_signals.py: ~500 lines (position signals, convergence)
db_followed.py: ~700 lines (followed wallets, snapshots, watchlist)
db_replay.py: ~600 lines (retrospective analysis, replay configs)

All functions re-exported from db.py — zero import changes needed.
All 1444 tests passing."
```

---

## Execution Order

| Phase | Tasks | Risk | Time |
|-------|-------|------|------|
| **Phase 1: Docs** | Tasks 1-4 | Zero | ~20 min |
| **Phase 2: Supabase** | Tasks 5-7 | Low (additive) | ~45 min |
| **Phase 3: db.py** | Tasks 8-14 | Medium (refactor) | ~2 hours |

Phase 1 can ship immediately. Phase 2 is independent. Phase 3 should be done in a feature branch or worktree.
