# Polymarket Crypto5m Maker Regime v1 Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start a clean `maker-regime-v1` paper campaign that actively applies regime-aware quote posture without mutating `maker-unified-v4`.

**Architecture:** Keep the existing runner and pure `maker_regime.py` router. Change the toxic `mid_chop` posture from tight/full-size to wide/small-size, expose active regime state in `maker_state`, and launch the watchdog with `--maker-regime-router active --campaign maker-regime-v1`.

**Tech Stack:** Python 3.13, SQLite, pytest, existing shell watchdog.

## Global Constraints

- Paper only; no live orders, no private keys.
- Old v4 and shadow rows stay intact; no DB deletion or pooling.
- Evaluation bar for this faster campaign is `60` settled windows, not `200`.
- `mid_chop` is experimental wide/small, not pause and not tight/full-size.
- Active mode records `regime_id`, `policy_id`, route reason, and effective knobs to quotes/fills/decisions.

---

### Task 1: Patch Router Policy

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/maker_regime.py`
- Test: `tests/test_crypto5m_maker_regime.py`

**Interfaces:**
- Consumes: `route_maker_regime(snapshot, inventory, config) -> MakerRegimePolicy`
- Produces: `mid_chop` policy with `margin_multiplier > 1.0`, `clip_multiplier <= 0.5`, `max_shares_multiplier <= 0.5`, `risk_action="widen"`.

- [x] **Step 1: Write failing test**

Change `test_mid_chop_is_tightest_normal_maker_regime` to assert defensive wide/small behavior.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime.py::test_mid_chop_is_wide_small_experimental_not_tight_aggressive -q`

- [x] **Step 3: Implement minimal router change**

Change only the `mid_chop` branch.

- [x] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime.py -q`

### Task 2: Expose Active Regime State

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_maker.py`
- Test: `tests/test_crypto5m_maker_runner.py`

**Interfaces:**
- Consumes: active `regimes[token] = (policy, inputs_hash, knobs)`
- Produces: `think["regime_active"][token]` with `regime_id`, `policy_id`, `risk_action`, `reason`, `margin`, `clip`, `max_shares`, `min_edge`, `quote_ttl_ms`.

- [x] **Step 1: Write failing active-run test**

Run one active iteration and assert active quote rows have regime metadata and `maker_state.think_json.regime_active.up.regime_id == "mid_chop"`.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_active_regime_run_records_policy_and_exposes_current_regime -q`

- [x] **Step 3: Add `regime_active` to `think`**

Serialize the active regime state after active policies are computed.

- [x] **Step 4: Run focused runner tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py::test_active_regime_run_records_policy_and_exposes_current_regime tests/test_crypto5m_maker_runner.py::test_shadow_regime_run_records_decisions_without_altering_v4_quote_rows -q`

### Task 3: Launch Clean Active Campaign

**Files:**
- Modify: `scripts/crypto5m_watchdog.sh`
- Test: `tests/test_crypto5m_watchdog_config.py`

**Interfaces:**
- Consumes: maker runner CLI `--campaign`, `--maker-regime-router active`
- Produces: watchdog launch command for `maker-regime-v1`.

- [x] **Step 1: Write failing config test**

Assert watchdog contains `--campaign maker-regime-v1` and `--maker-regime-router active`.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_watchdog_config.py -q`

- [x] **Step 3: Patch watchdog launch**

Change only the maker launch block.

- [x] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_crypto5m_watchdog_config.py tests/test_crypto5m_maker_regime.py tests/test_crypto5m_maker_runner.py::test_active_regime_run_records_policy_and_exposes_current_regime -q`

### Task 4: Restart Paper Engine

**Files:**
- Runtime only: current maker process, watchdog.

**Interfaces:**
- Consumes: `scripts/crypto5m_watchdog.sh`
- Produces: one running maker process with `--campaign maker-regime-v1 --maker-regime-router active`.

- [x] **Step 1: Stop old maker process**

Run: `kill <old-maker-pid>`

- [x] **Step 2: Start via watchdog**

Run: `scripts/crypto5m_watchdog.sh`

- [x] **Step 3: Verify process command**

Run: `ps -eo pid,etime,cmd | rg "run_polymarket_crypto5m_maker.py"`

- [x] **Step 4: Verify UI/API**

Run: `curl -sS http://127.0.0.1:5000/api/crypto5m/maker`

Expected: campaign `maker-regime-v1`, active think present, settled count starts clean.

### Task 5: Prevent Stale Shadow UI During Active Campaign

**Files:**
- Modify: `sniper/routes/api_crypto5m.py`
- Test: `tests/test_api_crypto5m.py`

**Interfaces:**
- Consumes: `maker_state.think_json.regime_active`
- Produces: empty `regime_shadow` report when active mode is running and no current shadow sidecar is declared.

- [x] **Step 1: Write failing API test**

Assert old `maker-regime-shadow-v1` rows are not displayed when current think has `regime_active`.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_crypto5m.py::test_maker_api_suppresses_stale_shadow_when_active_regime_is_running -q`

- [x] **Step 3: Patch API fallback**

Return the empty shadow report when `regime_active` is present and `regime_shadow` is absent.

- [x] **Step 4: Verify API**

Run: `python3 -m pytest tests/test_api_crypto5m.py::test_maker_api_suppresses_stale_shadow_when_active_regime_is_running tests/test_api_crypto5m.py::test_maker_api_discovers_shadow_campaign_without_binding_live_campaign_chars tests/test_api_crypto5m.py::test_maker_api_reports_regime_shadow_by_regime_stats -q`

Live API after web restart: `campaign maker-regime-v1`, `settled 0`, active `mid_chop`, `shadow_available False`.

### Task 6: Surface 60-Window Bar On The Maker Card

**Files:**
- Modify: `static/app.js`
- Test: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: `session.eval_min_settled`, `session.eval_ready`
- Produces: maker campaign line text `eval <n>/<min>`.

- [x] **Step 1: Write failing UI string test**

Assert `static/app.js` references `eval_min_settled`.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py::test_polymarket_algo_tab_is_crypto5m_and_hyperliquid_gets_microstructure_board -q`

- [x] **Step 3: Patch maker card text**

Add `eval ${session.n_settled}/${session.eval_min_settled}` beside the settled-window count.

- [x] **Step 4: Verify UI/API set**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py tests/test_api_crypto5m.py tests/test_crypto5m_maker_regime.py tests/test_crypto5m_maker_runner.py tests/test_crypto5m_watchdog_config.py -q`

Live API after final restart: `campaign maker-regime-v1`, `eval 60 False`, `shadow_available False`.
