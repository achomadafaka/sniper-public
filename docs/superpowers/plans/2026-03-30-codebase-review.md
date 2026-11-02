# Phase 2: Full Codebase Review Plan

> **Mindset:** Question everything. Assume nothing. Every claim in this review must be backed by reading actual code, running actual queries, or observing actual behavior. No assumptions from function names, docstrings, or memory of prior sessions.

**Goal:** Produce a verified inventory of every component in the Sniper system — what it does, whether it works correctly, whether it's needed, and what needs fixing. This is a prerequisite to deploying real money.

**Output:** A single audit document (`docs/audits/CODEBASE_AUDIT_2026-03-30.md`) with findings per module, plus targeted fixes for anything broken.

---

## Review Method

For each module/file:
1. **Read the actual code** — not summaries, not docstrings, the code itself
2. **Trace the data flow** — where does input come from, where does output go, who depends on it
3. **Verify the math** — any PnL calculation, scoring formula, or threshold check gets hand-verified
4. **Check for dead code** — functions never called, tables never queried, strategies never active
5. **Check for silent failures** — bare except blocks, swallowed errors, fallback-to-default paths that hide problems
6. **Verify against live state** — does the VPS code match git HEAD, do services use what we think they use

---

## Review Sections (in dependency order)

### Section 1: Core Data Layer
**Files:** `db.py`, `db_utils.py`, `db_snapshots.py`, `db_leaderboards.py`, `db_signals.py`, `db_followed.py`, `db_replay.py`, `db_hyperliquid.py`
**Questions to answer:**
- [ ] Is every table schema correct and complete?
- [ ] Are there orphaned tables (defined but never used)?
- [ ] Is every SQL query safe from injection?
- [ ] Are there race conditions in concurrent writers (WAL mode, timeout=30)?
- [ ] Does `db.py` (9200 lines) need further decomposition?
- [ ] Are the PnL calculations in `_calc_pnl` and `_calc_unrealized_pnl` mathematically correct for both YES and NO outcomes?

### Section 2: Signal Pipeline (The Money Path)
**Files:** `stream_low_buys.py`, `analyze_users.py`, `paper_trader.py`, `alerter.py`, `sync.py`
**Questions to answer:**
- [ ] Does the streamer correctly subscribe to `assets_ids` (plural) and receive trades?
- [ ] Does the analyzer correctly compute `insider_score`, `followability_score`, `wallet_quality_score`?
- [ ] Is the two-score system (ADR-007) correctly implemented — wallet_quality for selection, position_insider for timing?
- [ ] Does the paper trader correctly enter, manage, and exit positions?
- [ ] Are resolution thresholds (0.97/0.03) applied consistently?
- [ ] Does the 58.5% WR and +$449 PnL claim survive manual verification against actual DB rows?
- [ ] Is the alerter sending the right signals to Telegram?

### Section 3: Strategy Framework
**Files:** `strategy_runner.py`, `strategies/base.py`, `strategies/registry.py`, `instance_profile.py`, all strategy plugins in `strategies/`
**Questions to answer:**
- [ ] Is `strategy_runner.py` (2500+ lines) maintainable?
- [ ] Does the HL venue branching (added this session) have any bugs or edge cases?
- [ ] Are paused strategies still managing their open positions correctly?
- [ ] Is the circuit breaker logic correct (what triggers pause, what triggers resume)?
- [ ] For each active/paused strategy: is the discover/enter/manage/exit logic correct?
- [ ] Are there strategies that should be completely removed from the codebase (not just stopped)?

### Section 4: Intelligence Pipeline
**Files:** `scripts/world_analyst.py`, `finance_analyst.py`, `agents/tools/intel_tool.py`, `agents/tools/predict_tool.py`, `brain.py`, `bot/supabase_helper.py`
**Questions to answer:**
- [ ] What do the world analyst and finance analyst actually produce?
- [ ] Are they producing actionable intelligence or burning API credits?
- [ ] Is the brain/Supabase store useful — do any strategies actually read from it?
- [ ] How much do the analyst runs cost (API calls per run)?
- [ ] Is the news intel pipeline (`news_items`, `trade_ideas`) producing quality output?

### Section 5: Risk Module
**Files:** `risk/policy.py`, `risk/models.py`, `execution/hyperliquid_paper.py`
**Questions to answer:**
- [ ] Is `evaluate_entry_risk` correct for both PM and HL venues?
- [ ] Are the VenueRiskLimits applied correctly?
- [ ] Is the HL paper execution engine mathematically correct (PnL, fees, liquidation price)?
- [ ] Should the HL execution code be removed or kept for future use?

### Section 6: Web UI & API
**Files:** `web_server.py`, `routes/api_core.py`, `routes/api_followed.py`, `routes/api_strategies.py`, `routes/api_agent.py`, `routes/api_watchlist.py`, `routes/api_misc.py`, `static/app.js`
**Questions to answer:**
- [ ] Does the dashboard show accurate, current data?
- [ ] Are there UI tabs/sections that show stale or misleading information?
- [ ] Does the mobile view work?
- [ ] Are API endpoints returning correct data?
- [ ] Is there sensitive data exposed (API keys, wallet keys)?

### Section 7: Feeds & External Data
**Files:** `feeds/registry.py`, `feeds/polymarket.py`, `feeds/hyperliquid.py`, `feeds/hyperliquid_ws.py`, `feeds/hl_whale.py`, `feeds/odds.py`, `feeds/poly_binary_snapshot.py`
**Questions to answer:**
- [ ] Which feeds are actively running and which are dead weight?
- [ ] Is the HL WebSocket streamer still needed (787K trades captured but no strategy uses them)?
- [ ] Are any feeds silently failing?
- [ ] Can we reduce VPS resource usage by stopping unnecessary feeds?

### Section 8: Tests
**Files:** `tests/` directory (2029+ tests)
**Questions to answer:**
- [ ] Are the tests testing the right things?
- [ ] Are there tests that always pass regardless (vacuous tests)?
- [ ] What is the actual code coverage on the money path (paper_trader, analyzer, strategy_runner)?
- [ ] Are there pre-existing failures that should be fixed (currently 1 known)?

### Section 9: Git & Deployment
**Questions to answer:**
- [ ] Is the working tree clean on both local and VPS?
- [ ] Are there untracked files that should be committed or .gitignored?
- [ ] Is VPS HEAD matching origin/main?
- [ ] Are there stale branches?
- [ ] Do systemd services match the current code?
- [ ] Is the .env file on VPS correct and minimal?

### Section 10: Operational Health
**Questions to answer:**
- [ ] Are all services healthy and not OOM-killing?
- [ ] Is the DB growing at a sustainable rate (2.3GB now)?
- [ ] Is db_prune running and effective?
- [ ] Are there log files growing unbounded?
- [ ] What's the actual VPS cost vs value delivered?

---

## Deliverables

1. **Audit document** with findings per section, severity ratings (critical/important/minor), and fix recommendations
2. **Targeted fixes** for anything critical or important found during review
3. **Dead code removal** for anything confirmed unused
4. **Updated brain entries** if any strategy knowledge is found to be incorrect
5. **Updated MEMORY.md** with corrected system state

## Time Estimate

This is at least 2 full sessions. Each section should take 30-60 minutes of thorough reading and verification. Do NOT rush. The point is to be tedious and thorough — every shortcut here risks deploying real money on broken code.
