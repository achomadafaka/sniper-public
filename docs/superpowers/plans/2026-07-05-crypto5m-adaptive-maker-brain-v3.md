# Crypto5m Adaptive Maker Brain V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and launch `maker-adaptive-paper-v3`, a paper-only adaptive maker brain with logit fair blending, tail-risk override, quote-action arms, V3 rewards, ablations, and read-only visibility.

**Architecture:** Add a focused `maker_adaptive_v3.py` module that owns V3 fair/risk/action/ablation logic. Wire it into the existing maker runner behind `--maker-adaptive-v3`, preserving V1/V2 history and using a separate V3 DB/campaign.

**Tech Stack:** Python stdlib, SQLite, existing crypto5m maker runner, Flask API, vanilla JS UI, pytest.

## Global Constraints

- No live orders.
- No private keys.
- No production DB.
- No mutation of V1/V2 campaign history.
- No real-money execution.
- No hidden online self-modification outside the V3 paper campaign.
- Campaign: `maker-adaptive-paper-v3`.
- Selector: `adaptive-maker-brain-v3`.
- Reward: `maker-reward-v3`.

---

### Task 1: Pure V3 Brain Module

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/maker_adaptive_v3.py`
- Test: `tests/test_crypto5m_maker_adaptive_v3.py`

**Interfaces:**
- `logit_blend(q_structural, q_market, weight, residual=0.0) -> float`
- `choose_fair_value(...) -> FairDecision`
- `govern_tail_risk(...) -> TailRiskDecision`
- `select_action_arm(...) -> ActionDecision`
- `ensure_v3_tables(conn) -> None`
- `register_campaign(conn, campaign_id, control_campaign, now_ts, config) -> None`

- [x] Write failing tests for logit blend, market fallback, tail-risk override, action arms, UCB fallback, and schema.
- [x] Implement minimal pure V3 module.
- [x] Run focused V3 tests.

### Task 2: Runner Integration

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Test: `tests/test_crypto5m_maker_runner.py`

**Interfaces:**
- CLI flag: `--maker-adaptive-v3`
- Campaign: `maker-adaptive-paper-v3`
- Think JSON key: `adaptive_v3`

- [x] Write failing runner test for V3 campaign registration, action arm selection, fair decision, risk decision, and ablation rows.
- [x] Wire V3 into the runner behind `--maker-adaptive-v3`.
- [x] Keep V1/V2 paths untouched.
- [x] Run focused runner tests.

### Task 3: Rewards, Ablations, API/UI, Watchdog

**Files:**
- Modify: `sniper/routes/api_crypto5m.py`
- Modify: `static/app.js`
- Modify: `scripts/crypto5m_watchdog.sh`
- Test: `tests/test_api_crypto5m.py`
- Test: `tests/test_ui_algo_tabs.py`
- Test: `tests/test_crypto5m_watchdog_config.py`

**Interfaces:**
- Endpoint extends `/api/crypto5m/maker-adaptive` with V3 block.
- Watchdog starts `data/crypto5m/maker_adaptive_v3.sqlite`.

- [x] Write failing tests for API V3 visibility, UI V3 render hooks, and watchdog V3 stanza.
- [x] Add read-only V3 API fields.
- [x] Add compact UI V3 panel.
- [x] Add V3 watchdog stanza.
- [x] Run focused API/UI/watchdog tests.

### Task 4: Verification and Launch

**Files:**
- No additional source files unless tests expose a gap.

**Interfaces:**
- Launch command uses main-compatible runner path.

- [x] Run broad crypto5m tests.
- [x] Run `git diff --check`.
- [x] Commit.
- [x] Launch `maker-adaptive-paper-v3` in tmux.
- [x] Verify PID, first decisions, first fair blend, risk examples, ablation rows, and V2 untouched.
