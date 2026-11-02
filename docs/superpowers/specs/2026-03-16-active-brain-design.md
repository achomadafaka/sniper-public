# Active Brain & Trade Ideas Execution — Design Spec

**Date:** 2026-03-16
**Status:** Approved
**Scope:** Brain knowledge base architecture, news_intel_v1 strategy plugin, Intel tab execution, bot reconfiguration, outcome feedback loop, cross-layer intelligence

---

## 1. Problem Statement

Three loops in the Sniper system are broken:

1. **Trade ideas → positions:** The Intel tab's "Act on this" button marks ideas as `acted` in the DB but never creates a position. 8 trade ideas were "acted on" with zero positions created.
2. **Trade outcomes → learning:** When positions close, nothing records what the decision source was, whether the reasoning was correct, or what pattern it fits. The system doesn't learn.
3. **Cross-layer signals:** News intel, insider wallet signals, and autonomous strategy positions operate in complete isolation. A news headline matching an insider's position is a convergence signal — currently invisible.

Additionally, the brain (Supabase `memories` table) functions as a session notepad rather than a domain knowledge base. The Telegram bot wastes ~30 seconds on context loading for simple tasks.

---

## 2. Design Overview

### 2.1 Brain Knowledge Base

**New memory type: `knowledge`**

Added to the existing Supabase `memories` table alongside `user`, `feedback`, `project`, `reference`. The `knowledge` type stores curated trading domain expertise.

**Entry structure:**

```markdown
## Finding
[Core insight — one paragraph]

## Evidence
[Data supporting this — our trade stats, external research, or both]

## Source
[Origin — "paper_trades analysis 2026-03-16", "SSRN paper", "polymarket docs"]

## Confidence
[high/medium/low — based on sample size and source quality]

## Last Verified
[ISO date — when last checked against current data]

## Applies To
[Which strategies/decisions this informs]
```

**Topic namespace convention:**

| Prefix | Content | Example name |
|--------|---------|-------------|
| `strategy:*` | Per-strategy findings | `strategy:political-no-value-performance` |
| `market:*` | Category-specific patterns | `market:sports-resolution-timing` |
| `wallet:*` | Wallet research profiles | `wallet:yatsen-pattern-analysis` |
| `risk:*` | Risk management findings | `risk:stop-loss-effectiveness` |
| `platform:*` | Polymarket mechanics | `platform:clob-resolution-rules` |
| `research:*` | External findings | `research:prediction-market-efficiency-2026` |

**Validation rule:** `VALID_TYPES` in `supabase_helper.py` extended to include `"knowledge"`.

**Query pattern:** Both Claude instances call `search_memories(query)` to find relevant knowledge before making strategy or threshold decisions. Substring search is sufficient for ~100 entries. When entry count exceeds ~200, migrate to Pinecone vector search.

**Failure handling:** All brain queries are wrapped in try/except with a 5-second timeout. If Supabase is slow or unavailable, callers proceed without brain input. The brain is advisory, never blocking.

### 2.2 Bot Reconfiguration

**Replace the "Session start routine" feedback memory** with task-proportional context loading:

| Task Type | Context to Load |
|-----------|----------------|
| Quick (status, restart, query) | None — just do it |
| Trading (threshold, wallet, strategy) | `search_memories("topic")` + `curl /api/system-stats` |
| Code changes | Read specific file(s), run tests after |
| Research | Query brain for existing knowledge, do research, write back |

**Remove** the instruction to read CLAUDE.md on every startup (the relay.py system prompt already contains architecture).

**Keep** PreCompact/Stop checkpoint hooks (cheap, valuable).

**Implementation:** Update two Supabase feedback memories by name:
- `Session start routine` — replace content with the task-proportional rules above
- `Bot Identity and Capabilities` — remove the "read CLAUDE.md first" instruction, keep everything else

### 2.3 `news_intel_v1` Strategy Plugin

New file: `strategies/news_intel_v1.py`

**Plugin lifecycle:**

```
discover()
  → SELECT from trade_ideas WHERE status='open' AND expires_at > now
  → Deduplicate by condition_id (pick highest confidence)
  → For HIGH confidence: return as StrategySignal
  → For MEDIUM confidence: send Telegram alert, do NOT return as signal
  → Return List[StrategySignal]

enter()
  → Gates: max concurrent (10 default), price in bounds
  → Cross-dedup: check BOTH strategy_positions AND paper_trades for same (condition_id, outcome)
  → Query brain: search_memories("strategy:{market_category}") for blockers (5s timeout, skip on failure)
  → Position size: bet_size_usdc (default $10)
  → Return StrategyEntryDecision

manage()
  → Fetch current YES price from CLOB API
  → Check if trade_idea was rejected/expired → flag for exit
  → Return StrategyManageState

exit()
  → Resolution: YES >= 0.97 → WIN, YES <= 0.03 → LOSS
  → Stop-loss: adverse drift > 25% from entry
  → Staleness: held > max_days (default 90)
  → Source invalidated: trade idea rejected
  → Return StrategyExitDecision
```

**Plugin metadata:**

```python
plugin_id = "news_intel_v1"
display_name = "News Intel v1"
strategy_family = "news_intel"
required_feeds = ("polymarket",)
supported_execution_modes = ("paper",)
default_loop_seconds = 1800  # 30 min, aligned with news_intel cycle
```

**MEDIUM confidence Telegram alert format:**

```
📰 News Trade Idea (MEDIUM)
"Trump ends Iran operations by April" → BUY NO @ ¢51
Reasoning: [truncated reasoning from trade_idea]
Source: The Hill
```

Note: The Telegram alert is informational only. To execute a MEDIUM idea, the operator uses the Intel tab's Execute button or the bot can call the execute API directly if asked. No `/act_idea` command is implemented — the bot's relay already handles natural language requests like "execute trade idea 12."

**Alert ownership:** MEDIUM confidence alerts are sent from `news_intel_v1.py` discover() only. The existing `news_intel.py` alert behavior (HIGH confidence) is NOT modified — it continues to send its own alerts independently. The two systems use different Telegram message formats to avoid confusion.

**Registration:**
- Add `NewsIntelV1Strategy` to `strategies/registry.py`
- Create DB row in `strategies` table (wallet='autonomous', status='active', strategy_type='news_intel_v1')

### 2.4 Execute Button & Trade Idea Dedup

**New API endpoint:**

```
POST /api/trade-ideas/<int:idea_id>/execute

Request: { "size_usdc": 10.0 }  (optional, defaults to strategy config)

Response: {
  "ok": true,
  "idea_id": 12,
  "position_id": 45,
  "entry_price": 0.51,
  "outcome": "NO",
  "size_usdc": 10.0
}
```

**Backend logic:**
1. Load the trade idea from brain
2. Validate: status must be 'open' (reject if 'acted', 'expired', or 'rejected'), market must still be active
3. Look up the news_intel_v1 strategy row in `strategies` table by `strategy_type='news_intel_v1'` — fail if not found
4. Fetch live CLOB price from CLOB API
5. Run risk evaluation via the strategy_runner's `_build_risk_context()` + `evaluate_entry_risk()` — reject if risk limits exceeded
6. Write to `strategy_positions` (state='open', strategy_id=news_intel_v1 row id, entry_price from CLOB, shares=size/price) and `strategy_executions` (execution_type='paper', status='open')
7. Mark the trade idea as `status='acted'`
8. Return position details

**Why direct DB write, not importing strategy_runner:** The execute endpoint writes directly to `strategy_positions` and `strategy_executions` using the same schema that `_record_plugin_entry()` uses, but without importing the strategy_runner module (which has module-level global state). The strategy_runner's manage/exit cycle will naturally pick up these positions on its next loop because it queries `strategy_positions WHERE state='open'` for the news_intel_v1 strategy.

**Frontend changes:**
- Intel tab: add green "Execute" button alongside grey "Act on this"
- "Execute" shows a brief confirmation (market, direction, size) before submitting
- After execution, button changes to "Entered" with position link

**Deduplication fix in `news_intel.py`:**
- Add migration in `brain.py _ensure_tables()`: `CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_ideas_dedup ON trade_ideas(condition_id, direction)`
- The dedup key is `(condition_id, direction)` — this allows both a `buy_yes` and `buy_no` idea for the same market (legitimate if different news triggers different directions), but prevents duplicate `buy_no` ideas for the same market from repeated news cycles
- Change `INSERT INTO trade_ideas` to `INSERT OR IGNORE INTO trade_ideas`
- Also update `news_intel.py`'s in-memory `seen_conditions` set to use `(condition_id, direction)` tuples instead of `condition_id` alone (currently at line ~376)
- Existing duplicates cleaned up via one-time migration (keep lowest id per condition_id+direction, delete rest)
- Existing 8 "acted" trade ideas are left as-is (they already have no linked position; resetting them to 'open' would be confusing)

### 2.5 Outcome Feedback Loop

**Trigger:** Position close event in `paper_trader.py` or `strategy_runner.py`.

**Logic — NOT every trade.** Only write to brain when patterns emerge:

1. **Every 10 closed trades per strategy type:** Compute rolling WR, avg PnL, avg ROI for that strategy. Compare to brain's stored `strategy:*` knowledge entry. If materially different (>5% WR change or sign flip on PnL), update the knowledge entry.

2. **Performance degradation alert:** If a strategy's rolling WR (last 20 trades) drops below its stored expectation by >10 points, write an alert-type knowledge entry + send Telegram notification.

3. **New category first observation:** If a trade closes in a market category not yet seen in knowledge entries, write a "first observation" entry with the initial data point.

**Implementation:**
- New function `_maybe_update_brain(strategy_type, market_category, outcome, pnl)` in a shared module (`brain.py` — already imported by both services for trade ideas)
- Called from `paper_trader.py` close path and `strategy_runner.py` `_close_plugin_position()`
- **Fire-and-forget:** Entire function wrapped in try/except with 5-second Supabase timeout. If Supabase is unavailable, the position close still succeeds. Brain update failure is logged but never blocks trading operations.
- **Counter mechanism:** The function queries `SELECT COUNT(*) FROM {table} WHERE status/state IN ('closed', 'resolution_win', 'resolution_loss') AND strategy_type/signal_type = ?`. If `count % 10 == 0`, runs the analysis. This may re-trigger near the boundary on concurrent closes, but duplicate brain updates are idempotent (upsert).
- Lightweight: no new DB tables, no new services

### 2.6 Cross-Layer Intelligence

**Runs in existing `sync-state` cron** (every 30 minutes, `bot/supabase_helper.py sync-state`).

**Signal convergence detection:**
```python
# Find trade_ideas whose condition_id matches position_signals with insider_score >= 70
overlap = SELECT ti.condition_id, ti.direction, ps.wallet, ps.position_insider_score
          FROM trade_ideas ti
          JOIN position_signals ps ON ti.condition_id = ps.condition_id
          WHERE ti.status = 'open' AND ps.is_open = 1 AND ps.position_insider_score >= 70

# If overlap found: write convergence knowledge entry + Telegram alert
```

**Conflict detection:**
```python
# Find open strategy_positions whose condition_id matches trade_ideas with opposing direction
# Note: LOWER() used because strategy_positions stores 'Yes'/'No' (mixed case from some plugins)
# while trade_ideas stores 'buy_yes'/'buy_no' in direction field
conflicts = SELECT sp.condition_id, sp.outcome, ti.direction, ti.reasoning
            FROM strategy_positions sp
            JOIN trade_ideas ti ON sp.condition_id = ti.condition_id
            WHERE sp.state = 'open' AND ti.status = 'open'
              AND ((LOWER(sp.outcome) = 'yes' AND ti.direction = 'buy_no')
                OR (LOWER(sp.outcome) = 'no' AND ti.direction = 'buy_yes'))

# If conflict found: write warning knowledge entry + Telegram alert
```

**Output:** Brain knowledge entries of type `knowledge` with names like `convergence:2026-03-16-iran-invasion` and `conflict:2026-03-16-iran-ceasefire`.

### 2.7 Brain Hygiene & Initial Knowledge Seeding

**Migrate existing memories to knowledge type:**

| Current Memory | New Name | New Type |
|---------------|----------|----------|
| Trading Insights — Research Findings | `research:trading-insights-march-2026` | knowledge |
| Edge Hypothesis and Strategy Framework | `strategy:edge-hypothesis-framework` | knowledge |
| Wallet Intelligence Report | `wallet:intelligence-report-march-2026` | knowledge |
| Leaderboard Intelligence | `research:leaderboard-top-earners-analysis` | knowledge |
| Trading Lessons Learned | `risk:lessons-learned` | knowledge |

**Update stale memories:**
- "Proof Bar Status" (shows 34/150 at 71.4% — reality is 73 at 49.3%)
- "System Architecture" (missing strategy_runner, news_intel, bot services)

**Update `checkpoint.py`:**
- Include `strategy_positions` data (currently only tracks `paper_trades`)
- Add strategy performance summary per active strategy

**Initial research seeding (5-10 entries via WebSearch):**
- `platform:polymarket-resolution-mechanics`
- `research:prediction-market-efficiency`
- `risk:optimal-position-sizing-binary-markets`
- `market:category-specific-patterns`
- `platform:polymarket-clob-api-reference`

---

## 3. Files Changed

### New Files
| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `strategies/news_intel_v1.py` | ~250 | News intel strategy plugin |

### Modified Files
| File | Change | Size |
|------|--------|------|
| `strategies/registry.py` | Add import + registration | +3 lines |
| `bot/supabase_helper.py` | Add `knowledge` to VALID_TYPES, fix CLI env loading (done), add convergence/conflict detection to `sync-state` | +52 lines |
| `brain.py` | Add unique index migration for trade_ideas, add dedup cleanup, add `_maybe_update_brain()` function | +40 lines |
| `news_intel.py` | Change INSERT to INSERT OR IGNORE | +2 lines |
| `routes/api_core.py` | Add `/api/trade-ideas/<id>/execute` endpoint | +40 lines |
| `templates/index.html` | Add Execute button markup | +5 lines |
| `static/app.js` | Add `executeIdea()` function | +30 lines |
| `strategy_runner.py` | Add `_maybe_update_brain()` call on close path | +5 lines |
| `paper_trader.py` | Add `_maybe_update_brain()` call on close path | +5 lines |
| `scripts/checkpoint.py` | Include strategy_positions in checkpoint | +15 lines |

### Supabase Changes
| Change | Impact |
|--------|--------|
| Add `knowledge` to type enum | No migration needed — type column is TEXT |
| Migrate 5 existing memories to knowledge type | Upsert with new type |

### No New Services
All changes piggyback on existing processes:
- `sniper-sports-runner` runs the news_intel_v1 plugin
- Existing 30-min cron runs convergence/conflict detection
- Existing close paths trigger feedback loop

---

## 4. Resource Impact

| Resource | Impact |
|----------|--------|
| RAM | ~0 — no new processes, plugin runs inside strategy_runner |
| Disk | Negligible — ~50 Supabase entries, dedup reduces trade_ideas |
| Network | +1-2 Supabase API calls per trade close, +1-2 per 30-min cron cycle |
| CPU | Negligible — queries are lightweight |

---

## 5. Testing Strategy

**Happy path tests:**
- Unit tests for `news_intel_v1.py` plugin (discover, enter, manage, exit)
- Unit test for trade idea dedup (INSERT OR IGNORE behavior)
- Unit test for execute endpoint
- Integration: verify strategy_runner picks up news_intel_v1 and processes ideas
- Manual: place a trade idea from Intel tab, verify it appears in Positions tab
- Existing test suite must remain green (1341 tests)

**Negative/edge case tests:**
- Execute endpoint called for expired/acted/rejected idea → returns error, no position created
- Execute endpoint called when risk limits exceeded → returns error with reason
- `_maybe_update_brain()` when Supabase is unavailable → close still succeeds, warning logged
- Dedup: INSERT OR IGNORE when (condition_id, direction) already exists with status='acted' → no new row
- Cross-layer convergence detection when condition_id has no market_metadata → skips gracefully
- news_intel_v1 discover() when trade_ideas table is empty → returns empty list, no errors

---

## 6. Rollout Order

1. Trade idea dedup fix (zero risk, immediate value)
2. news_intel_v1 plugin + registry registration + DB strategy row → restart `sniper-sports-runner`
3. Execute button (UI + API endpoint) → restart `sniper-web`
4. Bot memory reconfiguration (update Supabase feedback memories) → restart `sniper-bot`
5. Brain knowledge type + migrate existing memories (Supabase only, no service restart)
6. Outcome feedback loop (paper_trader + strategy_runner hooks) → restart `sniper-paper` + `sniper-sports-runner`
7. Cross-layer intelligence (convergence + conflict detection) — no restart (cron-based)
8. Brain hygiene (update stale memories, update checkpoint.py)
9. Initial knowledge seeding (web research → curated entries)

**Service restart order when multiple restarts needed:** web → stream → analyzer → paper → alerter → sports-runner → bot (per CLAUDE.md).

Steps 1-3 close the trade ideas loop.
Steps 4-5 restructure the brain.
Steps 6-7 make the brain active.
Steps 8-9 clean up and seed knowledge.
