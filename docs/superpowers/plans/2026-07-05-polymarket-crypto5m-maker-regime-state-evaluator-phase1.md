# Polymarket Crypto5m Maker Regime State Evaluator Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Completed 2026-07-05. Live paper maker restarted on the same `maker-regime-v1` active campaign after focused verification.

**Goal:** Add log-only market/window regime episodes and side/token profile decisions to the active paper maker without changing quote behavior.

**Architecture:** Create a pure `maker_regime_state.py` module for market/window episode state, moment keys, and side profile decisions. Extend the existing maker SQLite init and persistence helpers so new quote/fill/decision rows can join back to episode/profile context. Wire the runner in `log_only` mode beside `maker-regime-v1 active`.

**Tech Stack:** Python 3.13, SQLite, pytest, existing maker runner.

## Global Constraints

- No live orders.
- No private keys.
- No service restart required for the plan document.
- No current quote-behavior change in Phase 1.
- No self-learning active switcher.
- No backfill requirement for old v4, old shadow, or pre-evaluator rows.
- Campaign boundaries stay strict.
- Quote-created context and fill-time context must both be preserved.

---

### Task 1: Pure State Module

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/maker_regime_state.py`
- Test: `tests/test_crypto5m_maker_regime_state.py`

**Interfaces:**
- Consumes: raw `regime_id`, known-at-time market fields, inventory fields.
- Produces:
  - `RegimeStateConfig`
  - `Moment`
  - `RegimeEpisodeState`
  - `ProfileDecision`
  - `start_episode(...) -> RegimeEpisodeState`
  - `advance_episode(current, candidate_regime, now_ts, snapshot, config) -> tuple[RegimeEpisodeState, TransitionEvent | None]`
  - `build_moment(snapshot) -> Moment`
  - `decide_profile(episode, token, token_inventory, max_shares, active_policy) -> ProfileDecision`

- [x] **Step 1: Write failing tests**

Tests cover first episode, same-regime dwell, noisy candidate debounce, emergency interrupt, moment key coarseness, and profile decision shape.

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime_state.py -q`

- [x] **Step 3: Implement pure module**

Add deterministic dataclasses and pure functions only. No DB, no network, no runner imports.

- [x] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime_state.py -q` -> `6 passed`

### Task 2: Persistence Schema And Helpers

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Test: `tests/test_crypto5m_maker_runner.py`

**Interfaces:**
- Consumes: `RegimeEpisodeState`, `ProfileDecision`.
- Produces:
  - tables `maker_regime_episodes`, `maker_regime_transitions`, `maker_profile_decisions`;
  - new metadata columns on `maker_quotes`, `maker_fills`, `maker_regime_decisions`;
  - helper functions `persist_episode`, `persist_transition`, `persist_profile_decision`.

- [x] **Step 1: Write failing schema/persistence tests**

Tests assert tables exist, helper writes an episode row, helper writes a transition row, helper writes a profile decision row, and quote/fill tables have episode/profile columns.

- [x] **Step 2: Run tests to verify fail**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_regime_episode_tables_and_columns_exist tests/test_crypto5m_maker_runner.py::test_regime_episode_and_profile_persistence_helpers -q`

- [x] **Step 3: Implement schema and helpers**

Use idempotent `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN` migrations.

- [x] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_regime_episode_tables_and_columns_exist tests/test_crypto5m_maker_runner.py::test_regime_episode_and_profile_persistence_helpers -q` -> `2 passed`

### Task 3: Runner Log-Only Wiring

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Test: `tests/test_crypto5m_maker_runner.py`

**Interfaces:**
- Consumes: pure state module and persistence helpers.
- Produces:
  - current episode/profile data in `maker_state.think_json`;
  - `maker_regime_decisions`, `maker_quotes`, and `maker_fills` rows with `episode_id`, `moment_key`, `profile_id`, `profile_mode`;
  - no quote-price, clip, margin, or side-enable behavior change from the active router.

- [x] **Step 1: Write failing active-run metadata test**

Test one active iteration and assert state has `regime_episode`, profile decisions were appended in `log_only`, and quote rows include episode/profile fields.

- [x] **Step 2: Run test to verify fail**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_active_run_logs_regime_episode_and_profile_context_without_shadow -q`

- [x] **Step 3: Wire log-only state**

Keep active quote policy untouched. Add state/evaluator logging only.

- [x] **Step 4: Run focused runner tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_active_run_logs_regime_episode_and_profile_context_without_shadow tests/test_crypto5m_maker_runner.py::test_active_regime_run_records_policy_and_exposes_current_regime -q`

Additional guard added after live verification exposed a bad bucket:
`python3 -m pytest tests/test_crypto5m_maker_runner.py::test_regime_state_snapshot_marks_missing_book -q` -> `1 passed`

### Task 4: API Visibility

**Files:**
- Modify: `sniper/routes/api_crypto5m.py`
- Test: `tests/test_api_crypto5m.py`

**Interfaces:**
- Consumes: `maker_state.think_json.regime_episode`, `profile_decisions`.
- Produces compact API fields:
  - `engine.regime_episode`
  - `engine.profile_decisions`

- [x] **Step 1: Write failing API test**

Test maker API returns current episode and profile decision data when present.

- [x] **Step 2: Run test to verify fail**

Run: `python3 -m pytest tests/test_api_crypto5m.py::test_maker_api_surfaces_regime_state_from_current_think -q`

- [x] **Step 3: Patch API only if needed**

Added explicit `regime_state` API projection so UI/API consumers do not need to know raw `think_json` internals.

- [x] **Step 4: Run focused API tests**

Run: `python3 -m pytest tests/test_api_crypto5m.py::test_maker_api_surfaces_regime_state_from_current_think tests/test_api_crypto5m.py::test_maker_api_suppresses_stale_shadow_when_active_regime_is_running -q`

### Task 5: Verification

**Files:**
- Verify changed files only.

- [x] **Step 1: Run bounded suite**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime_state.py tests/test_crypto5m_maker_runner.py tests/test_api_crypto5m.py tests/test_ui_algo_tabs.py -q` -> `73 passed`

- [x] **Step 2: Run diff hygiene**

Run: `git diff --check` -> clean

- [x] **Step 3: Read-only API/process check**

Confirm current maker still runs `maker-regime-v1 active` and API still shows the active campaign.

Live check after restart:
- maker pidfile: `1306486`
- campaign: `maker-regime-v1`
- `regime_state.current_regime`: `mid_chop`
- `regime_state.profile_mode`: `log_only`
- direct DB counts: `episodes=3`, `profiles=72`, `quotes_with_episode=144`
