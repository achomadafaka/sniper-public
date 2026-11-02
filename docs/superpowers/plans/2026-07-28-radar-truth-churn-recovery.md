# Radar Truth, Churn, and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover Bags/Solana after reconnects, bound the live Radar payload, and make every headline use the full retained paper ledger.

**Architecture:** Keep source recovery in the socket owner, add a compact API projection at the read boundary, and extend the existing retained arm aggregate rather than adding another store. The UI consumes aggregates for conclusions and recent rows only for receipts.

**Tech Stack:** Python 3.13, asyncio, SQLite, Flask SSE, vanilla JavaScript, pytest, Playwright CLI.

## Global Constraints

- Paper/read-only Radar only; no funded execution imports or behavior.
- No arm-threshold changes.
- No historical ledger rewrites.
- Focused tests only; do not run the full repository suite.

---

### Task 1: Start bounded Bags/Solana recovery

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py:1210-1233,2155-2173`
- Test: `tests/test_memecoin_opportunity_confirmation.py`

**Interfaces:**
- Consumes: `_solana_recovery_loop(..., abandon_fn=Callable | None)`.
- Produces: one recovery task per owned `VenueId`, including `BAGS_SOLANA`.

- [ ] Write a production-shaped async test whose Bags shard receives two pool subscription acks and assert `_solana_recovery_loop` is called once with `VenueId.BAGS_SOLANA` and the supplied abandonment callback.
- [ ] Run the exact test and confirm it fails because the current branch starts recovery only for Pump.
- [ ] Generalize the subscription-ready branch to create a recovery task when `venue not in recovery_tasks`; pass `abandon_fn` into every shard from `start_source_tasks()`.
- [ ] Run the exact test and the existing Solana recovery/confirmation tests.
- [ ] Commit the worker and test as one recovery fix.

### Task 2: Make retained arm aggregates complete

**Files:**
- Modify: `sniper/research/memecoin_opportunity/insights.py:563-634`
- Test: `tests/test_memecoin_opportunity_insights.py`

**Interfaces:**
- Consumes: full merged `alert_evidence` list.
- Produces: `arm_comparison[rule][horizon]` with `hits`, `losses`, `timeouts`, `invalid`, `pending`, `scored`, `samples`, `hit_rate_bps`, `final_return_samples`, and `avg_final_return_bps`.

- [ ] Add a failing test with scored, invalid, pending, and retained historical rows; assert scored rates exclude invalid/pending while those counts remain visible.
- [ ] Run the exact test and confirm the missing keys fail.
- [ ] Extend `_arm_comparison` in one pass over alerts and target receipts; preserve existing `samples` as the number of scored target receipts for compatibility.
- [ ] Run the exact test and all insight tests.
- [ ] Commit the aggregate and test.

### Task 3: Compact and pace the live API

**Files:**
- Modify: `sniper/routes/api_memecoin_opportunity.py:64-75,110-167,341-366`
- Test: `tests/test_memecoin_opportunity_api.py`

**Interfaces:**
- Produces: `_compact_snapshot(payload, alert_limit=16) -> dict` and a SHA-256 `_stream_fingerprint`.
- The evidence endpoint continues returning full insight detail.

- [ ] Add failing tests that compact history/forward-alerts away, remove per-alert `targets`, cap recent receipts at 16, preserve arm aggregates, and ignore heartbeat/version-only changes in the stream digest.
- [ ] Run the exact tests and confirm the current payload/fingerprint behavior fails.
- [ ] Build and cache one full snapshot, return its compact projection from the primary endpoint/SSE, and keep the full projection for `/evidence`.
- [ ] Add a five-second health-only stream deadline while keeping market changes immediate.
- [ ] Run API tests and assert a representative serialized compact fixture is below 150 KiB.
- [ ] Commit the API and tests.

### Task 4: Put decision and retained evidence above the map

**Files:**
- Modify: `static/app.js:2914-2954,3028-3039,4925-5225`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: complete `insights.arm_comparison` cells from Task 2.
- Produces: a stable top `Now` plus `Evidence` strip and recent receipt list.

- [ ] Add failing UI-contract assertions that headline counts come from `arm_comparison`, not `alert_evidence`, and partial capture copy names degraded venues.
- [ ] Run the exact tests and confirm failure.
- [ ] Move the current verdict and early retained comparison into a two-cell strip above the radar layout; keep the map and its sweep intact.
- [ ] Derive candidate totals, control totals, target ladder, and current-read tone from retained cells. Use recent alert rows only for the receipt list.
- [ ] Render affected venue names beside `CAPTURE PARTIAL`; keep healthy venues live.
- [ ] Run UI contract tests and `node --check static/app.js`.
- [ ] Commit the UI and tests.

### Task 5: Focused verification and read-only live audit

**Files:**
- Inspect only: changed files, local API payload, rendered Radar page.

**Interfaces:**
- Produces: verification evidence and a branch ready for review; no deployment or restart.

- [ ] Run `python3 -m pytest tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_insights.py tests/test_memecoin_opportunity_api.py tests/test_memecoin_opportunity_ui_contract.py -q`.
- [ ] Run `python3 -m py_compile scripts/memecoin_opportunity_worker.py sniper/research/memecoin_opportunity/insights.py sniper/routes/api_memecoin_opportunity.py`.
- [ ] Run `node --check static/app.js`, `python3 scripts/validate_schema_refs.py`, and `git diff --check`.
- [ ] Use the authenticated local API read-only to measure current full payload and calculate the compact projection size.
- [ ] Use Playwright against a worktree-local web process or static fixture to verify the top decision/evidence strip and stable open panels.
- [ ] Report the branch, commits, remaining Bags/Robinhood venue limitations, and exact live-data caveats. Do not restart or deploy without explicit authority.
