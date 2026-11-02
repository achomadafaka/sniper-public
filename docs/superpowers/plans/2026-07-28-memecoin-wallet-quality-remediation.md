# Memecoin Wallet-Quality Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make wallet discovery, curation, recommendation, and operator presentation truthful, current, low-churn, and safe to activate.

**Architecture:** Reuse the current SQLite ledgers and Flask/vanilla-JS surfaces. Correct shared persistence helpers first, then make API projections consume the corrected facts. Keep all funded authority and execution policy unchanged.

**Tech Stack:** Python 3.13, SQLite, Flask, vanilla JavaScript, pytest, bash pinned-runtime deployment.

## Global Constraints

- No composite wallet score or automatic promotion, revival, or arming.
- Missing data remains unknown.
- Fomo-priced fills are barred from wallet evidence.
- Funded admission remains automated `REFUSE` versus manual `ADMIT`.
- No funded policy, sizing, reservation, entry, exit, or V6 change.
- Use corrected UTC and the pinned merged release for activation.

---

### Task 1: Snapshot truth and idempotency

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_snapshot.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_pnl_snapshots.py`
- Test: `tests/test_memecoin_snapshot_pass.py`
- Test: `tests/test_memecoin_selection_gate.py`

**Interfaces:**
- Produces: `latest_snapshot_day(conn) -> str | None`; calendar-day-idempotent `record_pnl_snapshot`; null-safe `windowed_realized`.

- [ ] Add tests proving null tiles do not count, repeated days stay one row, a leaderboard-only repeat preserves a realized tile, and gates require 7/30 valid days.
- [ ] Run the new tests and confirm failures are caused by current null counting, duplicate inserts, and 4/10 thresholds.
- [ ] Implement the minimal shared persistence and runner-latch changes.
- [ ] Run the three focused files and commit `fix(memecoin): make wallet snapshots truthful`.

### Task 2: Roster lifecycle and trust-boundary validation

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_candidates.py`
- Test: `tests/test_memecoin_api_gate.py`

**Interfaces:**
- Produces: `promote_candidate(conn, uid)`, `reobserve_wallet(conn, uid)`, and non-destructive `remove_wallet` retirement.

- [ ] Add endpoint tests for empty IDs, missing candidates, excluded promotion refusal, atomic promotion, watch-only re-observation, ignored reconsideration, and non-destructive manual removal.
- [ ] Verify each new test fails against the current routes.
- [ ] Implement validation and transaction-scoped persistence helpers; render Add/Re-observe/Reconsider correctly.
- [ ] Run focused lifecycle tests and commit `fix(memecoin): repair wallet roster lifecycle`.

### Task 3: Rolling discovery and pagination

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_candidates.py`

**Interfaces:**
- Produces: `list_candidates(..., status='new', offset=0) -> rows`; response metadata `total`, `limit`, `offset`; latest `network_id` and roster state.

- [ ] Add fixtures where a fresh smaller wallet outranks a stale lifetime accumulator and page two is reachable.
- [ ] Confirm RED under the lifetime composite and fixed limit.
- [ ] Implement rolling event aggregates, stable ordering, status filtering, pagination, and exact latest network projection.
- [ ] Verify API and persistence tests and commit `feat(memecoin): surface fresh wallet candidates`.

### Task 4: Honest recommender, exact arm comparison, and analyzer labels

**Files:**
- Modify: `sniper/services/memecoin_wallet_recommender.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/analysis/fomo_account/metrics.py`
- Modify: `sniper/analysis/fomo_account/selection_gate.py`
- Modify: `sniper/settings/memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_wallet_recommender.py`
- Test: `tests/test_memecoin_candidates.py`
- Test: `tests/test_memecoin_selection_gate.py`
- Test: `tests/test_memecoin_ui_honest_copy.py`

**Interfaces:**
- Recommender accepts `include_inactive=False` and returns honest exact-event rows with `distinct_coins`.
- Arm matrix returns `exact_paired=True` and only intersection-backed cells.

- [ ] Add tests proving any fomo-filled position is excluded, excluded wallets are absent by default, repeated one-coin events cannot promote, and arm totals use an exact shared origin set.
- [ ] Add a test proving partial bag data is insufficient and analyzer output distinguishes matched lots, exit events, and coins.
- [ ] Confirm the tests fail for the audited reasons.
- [ ] Implement the smallest query/label changes and remove obsolete hold-gate settings.
- [ ] Run focused evidence tests and commit `fix(memecoin): grade wallets on honest independent evidence`.

### Task 5: Query, polling, and analyzer-process churn

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_candidates.py`
- Test: `tests/test_memecoin_api_pump.py`
- Test: `tests/test_memecoin_ui_honest_copy.py`

**Interfaces:**
- `trader_pump_profile(..., source_user_ids=None)` bounds aggregation.
- `/api/memecoin/screen` holds one nonblocking lock file and reports `already_running`.

- [ ] Add behavioral tests for bounded pump rows and duplicate screen refusal.
- [ ] Confirm RED, then replace per-wallet stats/last-signal scans with grouped queries, add measured indexes, remove dead payload work, and bound pump aggregation.
- [ ] Change wallet polling to 30 seconds with an in-flight guard and mutation-triggered refresh.
- [ ] Run focused API/UI tests and commit `perf(memecoin): remove wallet workspace churn`.

### Task 6: Verification, knowledge sync, publication, and activation

**Files:**
- Modify if required: `AGENTS.md`, `CLAUDE.md`, PR body knowledge block.

- [ ] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`.
- [ ] Run every `tests/test_memecoin*.py`, compile affected Python, `bash -n` affected scripts, and `git diff --check`.
- [ ] Benchmark corrected live read-only projections and reproduce the original defects against a database copy.
- [ ] Complete the 11-question Knowledge Sync block and inspect the full diff/commit range.
- [ ] Push the feature branch, open a PR, wait for checks, merge through GitHub, and obtain the exact merged SHA.
- [ ] Run `scripts/deploy_memecoin_runtime.sh <merged-sha> --dry-run`, then `--restart`; restart the web surface through its pinned workflow.
- [ ] Verify current symlink/manifest SHA, one runner command, fresh heartbeat, authenticated health/status, zero pending reservations, kill switch, wallet API truth, and rollback evidence.
