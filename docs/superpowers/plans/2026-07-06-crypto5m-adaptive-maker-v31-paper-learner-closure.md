# Crypto5m Adaptive Maker V3.1 Paper Learner Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn V3 from a collecting paper learner into V3.1, an acting paper learner that learns fair-blend weights and filters/promotes quote-action arms by context.

**Architecture:** Keep the existing `maker_adaptive_v3.py` module and runner flag, but launch a separate `maker-adaptive-paper-v3.1` campaign. Settlement is the learner update boundary: fill rewards, action-arm status, and fair-blend forecast labels are updated only after a window resolves.

**Tech Stack:** Python stdlib, SQLite, existing crypto5m paper maker runner, Flask API, vanilla JS UI, pytest.

## Global Constraints

- No live orders.
- No private keys.
- No production DB.
- Do not mutate V2 or V3 campaign history.
- Campaign: `maker-adaptive-paper-v3.1`.
- Selector: `adaptive-maker-brain-v3.1`.
- Reward: `maker-reward-v3.1`.
- Do not hand-tune specific contexts.

---

### Task 1: Root-Cause Regression Tests

**Files:**
- Modify: `tests/test_crypto5m_maker_adaptive_v3.py`

**Interfaces:**
- `record_policy_action(..., fair_context_key=...) -> int`
- `evaluate_action_arm_status(conn, campaign_id, context_key, arm_id, now_ts) -> str`
- `update_rewards_for_settled_window(...) -> int`

- [x] Write failing test proving settled V3.1 rows populate `crypto5m_maker_v3_fair_blend_stats`.
- [x] Write failing test proving a bad context/action arm becomes disabled and is not selected.
- [x] Write failing test proving a promoted arm wins selection while safety override still wins.
- [x] Run tests and verify they fail for missing API/behavior.

### Task 2: Learner Loop Implementation

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/maker_adaptive_v3.py`

**Interfaces:**
- `FAIR_BLEND_ARMS`
- `update_fair_blend_stats_for_settled_window(...) -> int`
- `evaluate_action_arm_status(...) -> str`

- [x] Add V3.1 versions.
- [x] Extend V3 tables with fair metrics, action status fields, and fair observation idempotency.
- [x] Persist `fair_context_key` on policy actions.
- [x] Update fair-blend stats on settled windows.
- [x] Evaluate disable/promote after reward insertion.
- [x] Filter disabled exact-context arms in the selector.
- [x] Prefer `paper_preferred` before exploration/UCB.
- [x] Preserve reward-row return-count semantics.

### Task 3: Runner, API, UI, Watchdog

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Modify: `sniper/routes/api_crypto5m.py`
- Modify: `static/app.js`
- Modify: `scripts/crypto5m_watchdog.sh`
- Modify: `tests/test_crypto5m_maker_runner.py`
- Modify: `tests/test_api_crypto5m.py`
- Modify: `tests/test_crypto5m_watchdog_config.py`

**Interfaces:**
- Runner passes `fair_context_key`.
- API default adaptive DB is `data/crypto5m/maker_adaptive_v3_1.sqlite`.
- Watchdog adds `crypto5m_adaptive_maker_v31`.

- [x] Wire runner fair-context persistence.
- [x] Add API fair stats, selector-source distribution, disabled/preferred counts, and V2 paired comparison.
- [x] Add UI rows for V3.1 fair stats and selector-source distribution.
- [x] Add a separate V3.1 watchdog stanza.
- [x] Keep V3 and V2 DBs/campaigns separate.

### Task 4: Verification and Launch

**Files:**
- No additional source files.

- [x] Run focused V3.1 tests.
- [x] Run JS syntax check.
- [x] Run broader crypto5m/API focused tests.
- [x] Run `git diff --check`.
- [x] Commit (`0aa802a`).
- [x] Launch `maker-adaptive-paper-v3.1` (`pid=2197563`, watchdog-managed).
- [x] Verify PID, first policy actions, fair stats count, disabled/preferred counts, selector-source distribution, and V2 untouched.

Launch evidence (2026-07-06):

- DB: `data/crypto5m/maker_adaptive_v3_1.sqlite`.
- Campaign row: `maker-adaptive-paper-v3.1`, `learner_version=v3.1`, `selector_version=adaptive-maker-brain-v3.1`, `reward_version=maker-reward-v3.1`.
- Final live policy-action count checked: `368`.
- Selector-source distribution checked: `learned_ucb=234`, `safety_override=96`, `exploration=29`, `hardcoded_fallback=9`.
- Fair-blend stats checked: `25` contexts populated after settlement.
- Reward rows checked: `8` rows from the first filled settled window.
- Disabled / preferred checked: `0 / 0`; expected at launch because the rewarded cells are still below the `n_fills >= 10` catastrophic-disable gate.
- V2 control DB checked: no `maker-adaptive-paper-v3.1` window or fill rows.
