# HL Watch Lifecycle Fallback Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Hyperliquid wallet lifecycle manager that keeps the watch cohort fresh, culls unwanted wallets, and activates a paper `watch_fallback` copy basket when no proven wallet exists.

**Architecture:** Add additive SQLite lifecycle columns, a pure lifecycle module, a standalone lifecycle script, and copy-runner/API/UI support for explicit `proven` vs `watch_fallback` tiers. `screen_hl_smart_money.py --persist` remains the discovery funnel and invokes lifecycle after each persisted screen.

**Tech Stack:** Python 3, SQLite, Flask API, existing static JS UI, pytest.

## Global Constraints

- Paper only: no keys, orders, exchange signing, or production execution.
- Fallback copy is explicit `watch_fallback`, never disguised as `proven`.
- `proven` copy targets override fallback copy targets.
- Existing SQLite files migrate additively only.
- Do not stage unrelated memecoin, Supabase, or brain WIP.

---

### Task 1: Store Lifecycle and Copy Tier Metadata

**Files:**
- Modify: `sniper/hyperlive/intel_store.py`
- Test: `tests/test_hyperlive_intel_store.py`

**Interfaces:**
- Produces: `set_tracked_lifecycle(conn, wallet, status, reason, ts_ms) -> None`
- Produces: `get_tracked(conn, include_culled=False) -> list[dict]`
- Produces: `set_copy_target(..., copy_tier='proven', reason=None, ts_ms: int) -> None`
- Produces: `get_executable_copy_targets(conn, now_ms: int) -> list[dict]`

- [ ] Write failing tests for additive migration, lifecycle culling visibility, copy target tier persistence, proven target selection, and watch fallback target selection.
- [ ] Run `python3 -m pytest tests/test_hyperlive_intel_store.py -q` and confirm the new tests fail.
- [ ] Add lifecycle columns to `hl_tracked_traders` and copy-tier columns to `hl_copy_targets`.
- [ ] Add helper functions listed above.
- [ ] Rerun `python3 -m pytest tests/test_hyperlive_intel_store.py -q`.

### Task 2: Lifecycle Manager

**Files:**
- Create: `sniper/hyperlive/watch_lifecycle.py`
- Create: `tests/test_hyperlive_watch_lifecycle.py`

**Interfaces:**
- Consumes: `IS.follow_stats_by_wallet`, `IS.set_tracked_lifecycle`, `IS.set_copy_target`
- Produces: `maintain_watchlist(conn, now_ms: int, max_watch_targets: int = 5) -> dict`

- [ ] Write failing tests for fresh watch retention, reject culling, mature negative culling, stale culling, open-follow stale protection, fallback activation when no proven exists, and proven override of fallback.
- [ ] Run `python3 -m pytest tests/test_hyperlive_watch_lifecycle.py -q` and confirm failures.
- [ ] Implement deterministic lifecycle decisions and target activation/deactivation.
- [ ] Rerun `python3 -m pytest tests/test_hyperlive_watch_lifecycle.py -q`.

### Task 3: Funnel and Runner Integration

**Files:**
- Modify: `scripts/screen_hl_smart_money.py`
- Create: `scripts/maintain_hl_watchlist.py`
- Modify: `scripts/run_hl_follow.py`
- Modify: `scripts/run_hl_copy.py`
- Modify: `scripts/hl_copy_watchdog.sh`
- Test: `tests/test_run_hl_copy_selection.py`
- Test: `tests/test_screen_hl_smart_money_lifecycle.py`

**Interfaces:**
- Consumes: `watch_lifecycle.maintain_watchlist`
- Consumes: `IS.get_executable_copy_targets`

- [ ] Write failing tests proving the copy runner opens fallback targets, skips unmanaged watch targets, and disables fallback when proven exists.
- [ ] Write failing tests proving persisted screen calls lifecycle.
- [ ] Run focused tests and confirm failures.
- [ ] Wire screen persistence to lifecycle with `--no-maintain-lifecycle`.
- [ ] Add standalone lifecycle script.
- [ ] Change copy runner to use executable targets.
- [ ] Fix copy watchdog repo path to `/home/bigpapi/Desktop/Sniper`.
- [ ] Rerun focused tests and `bash -n scripts/hl_copy_watchdog.sh scripts/hl_follow_watchdog.sh`.

### Task 4: API and UI Truth

**Files:**
- Modify: `sniper/routes/api_hyperlive.py`
- Modify: `static/app.js`
- Test: `tests/test_api_hyperlive.py`
- Test: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: copy target rows with `copy_tier`, `reason`, and lifecycle status.

- [ ] Write failing tests for analyzer target grouping and manual copy rejecting watch fallback activation.
- [ ] Run focused API/UI tests and confirm failures.
- [ ] Expose `copy_tier`, lifecycle state, and fallback copy wording.
- [ ] Rerun focused API/UI tests.

### Task 5: Verification, Review, Commit, Push, Merge

**Files:**
- All changed files from Tasks 1-4.

- [ ] Run `python3 -m pytest tests/test_hyperlive_trader_intel.py tests/test_hyperlive_intel_store.py tests/test_hyperlive_watch_lifecycle.py tests/test_run_hl_follow_selection.py tests/test_run_hl_copy_selection.py tests/test_api_hyperlive.py tests/test_hyperlive_follow_sim.py tests/test_hyperlive_copy_sim.py tests/test_screen_hl_smart_money_lifecycle.py tests/test_ui_algo_tabs.py -q`.
- [ ] Run `python3 -m py_compile sniper/hyperlive/intel_store.py sniper/hyperlive/watch_lifecycle.py sniper/routes/api_hyperlive.py scripts/run_hl_copy.py scripts/run_hl_follow.py scripts/screen_hl_smart_money.py scripts/maintain_hl_watchlist.py`.
- [ ] Run `bash -n scripts/hl_copy_watchdog.sh scripts/hl_follow_watchdog.sh`.
- [ ] Run `git diff --check`.
- [ ] Run a read-only DB lifecycle pass or dry-run against `data/hl_intel.sqlite`.
- [ ] Review the diff for unrelated files and stage only HL/spec/test files.
- [ ] Commit, push `main`, and merge only if remote requires a merge step.
