# Crypto5m Adaptive Learner V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a versioned `maker-adaptive-paper-v2` learner path with markout-aware rewards and richer selector telemetry.

**Architecture:** Keep V1 intact. Add learner-version config inside `maker_adaptive.py`, let the runner choose `v1` or `v2`, and persist V2 reward components in the same adaptive tables with `reward_version='maker-reward-v2'`.

**Tech Stack:** Python stdlib, SQLite, pytest, existing crypto5m maker runner.

## Global Constraints

- No real orders.
- No private keys.
- No production DB.
- No neural net.
- No retroactive profile relabeling.
- No future data in current decisions.
- Do not mutate `maker-adaptive-paper-v1`.
- V2 campaign id: `maker-adaptive-paper-v2`.
- V2 selector version: `adaptive-ucb-v2`.
- V2 reward version: `maker-reward-v2`.

---

### Task 1: Versioned V2 Selector

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/maker_adaptive.py`
- Test: `tests/test_crypto5m_maker_adaptive.py`

**Interfaces:**
- Produces: `learner_config(version: str) -> LearnerConfig`
- Extends: `select_profile(..., learner_version: str = "v1") -> AdaptiveDecision`
- Extends: `AdaptiveDecision` with fallback and status telemetry.

- [x] Write failing tests for V2 selector versions and fallback metadata.
- [x] Implement version config, V2 selector constants, and telemetry fields.
- [x] Run focused adaptive tests.

### Task 2: Markout-Aware Reward V2

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/maker_adaptive.py`
- Test: `tests/test_crypto5m_maker_adaptive.py`

**Interfaces:**
- Extends: `update_rewards_for_settled_window(...)`
- Produces: V2 reward columns:
  - `settled_fill_pnl`
  - `adverse_selection_3s_bps`
  - `adverse_selection_10s_bps`
  - `adverse_selection_30s_bps`
  - `future_executable_unwind_10s`
  - `shadow_quality`
  - `concentration_penalty`

- [x] Write failing tests for V2 reward components from future quote snapshots.
- [x] Implement markout lookup from `maker_quotes`.
- [x] Keep missing markouts as `not_evaluable`.
- [x] Run focused adaptive tests.

### Task 3: Runner Flag and Campaign Wiring

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Modify: `scripts/crypto5m_watchdog.sh`
- Test: `tests/test_crypto5m_maker_runner.py`
- Test: `tests/test_crypto5m_watchdog_config.py`

**Interfaces:**
- Adds CLI flag: `--adaptive-learner-version v1|v2`
- Launch target:
  - DB: `data/crypto5m/maker_adaptive_v2.sqlite`
  - PID: `data/crypto5m/maker_adaptive_v2.pid`
  - campaign: `maker-adaptive-paper-v2`

- [x] Write failing runner/watchdog tests for V2.
- [x] Wire runner registration, selection, and think JSON to version config.
- [x] Add watchdog stanza for V2 without removing V1.
- [x] Run focused runner/watchdog tests.
