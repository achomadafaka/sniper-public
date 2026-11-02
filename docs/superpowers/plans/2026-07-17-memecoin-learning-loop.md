# Memecoin Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build trustworthy Robinhood outcome capture, radar facts, missed-entry grading, execution substages, shadow self-signals, wallet recommendations, and paired exit-policy evaluation without changing funded decisions.

**Architecture:** Existing capture and trace writers gain bounded evidence-only extensions. New grading and recommendation modules are read-only projections; the shadow-signal ledger is populated only after capture. Flask and the existing Memecoin dashboard expose the results without adding work to the entry path.

**Tech Stack:** Python 3, SQLite, Flask, vanilla JavaScript, pytest.

## Global Constraints

- Coin identity is `(network_id, mint)`; Robinhood is network `4663`.
- `global_feed` and `follow_activity` remain separate.
- No new funded-entry network request or request-count increase in blind capture.
- No automatic funded gate, arming, sizing, TP, SL, mirror, or dynamic-policy change.
- Use reconciled monotonic trace time; missing facts stay unknown.
- One logical commit per task; tests must fail before production code changes.

---

### Task 1: Robinhood eligible-signal capture

**Files:**
- Modify: `sniper/services/memecoin_capture_v2.py`
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_capture_blind.py`
- Test: `tests/test_memecoin_capture_v2_horizons.py`

**Interfaces:**
- Produces: `_blind_capture_rows(conn, now_iso, blind) -> list[dict]`
- Preserves: `capture_blind_pass(...) -> CaptureSweepResult`

- [ ] Add failing tests proving a 30-key pass is one injected batch call, eligible pending horizons sort before candidates, and one quote can satisfy two distinct outcomes.
- [ ] Run `python3 -m pytest tests/test_memecoin_capture_blind.py tests/test_memecoin_capture_v2_horizons.py -q`; expect the new priority/default tests to fail.
- [ ] Add the deadline-aware row selector and change `MEMECOIN_CAPTURE_BLIND_MAX_MINTS` default from `8` to `30`.
- [ ] Rerun the focused test and `tests/test_memecoin_geckoterminal_quote.py`.
- [ ] Commit `perf(memecoin): widen eligible 4663 capture batch`.

### Task 2: Radar evidence enrichment

**Files:**
- Modify: `sniper/services/memecoin_radar_view.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_radar_view.py`

**Interfaces:**
- Extends each `radar_rows` item with `latest_price`, `quote_observed_at`, `quote_age_s`, `quote_quality`, `liquidity`, `volume_24h`, `route_state`, `source_user_id`, `wallet_ab_pnl`, `wallet_ab_closes`, and `attribution_state`.

- [ ] Add failing read-only projection tests for known, stale, and unknown quote states and for blocked route truth.
- [ ] Run `python3 -m pytest tests/test_memecoin_radar_view.py -q`; expect missing-field failures.
- [ ] Enrich the projection from existing quote/event/position tables without writing or issuing network calls.
- [ ] Render the flight-recorder facts in the existing radar card and drawer, preserving unknown values as `—`.
- [ ] Run focused pytest and `node --check static/app.js`.
- [ ] Commit `feat(memecoin): enrich radar evidence`.

### Task 3: Missed-entry grader

**Files:**
- Create: `sniper/services/memecoin_missed_entry_grader.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_missed_entry_grader.py`
- Test: `tests/test_memecoin_api.py`

**Interfaces:**
- Produces: `grade_missed_entries(conn, *, window_hours=24, chain=None, source=None, now_iso=None) -> dict`
- Produces: `simulate_first_touch(ref_price, marks, *, tp_pct=60, sl_pct=60, clip_usd=50) -> dict`

- [ ] Add failing tests for episode deduplication, later-fill exclusion, exact-outcome join, missing coverage, and first-touch simulation ordering.
- [ ] Run the focused tests; expect import failure.
- [ ] Implement the pure simulator and SELECT-only grader.
- [ ] Add `GET /api/memecoin/missed-entry-report` and a compact radar summary section.
- [ ] Run focused service/API tests and `node --check static/app.js`.
- [ ] Commit `feat(memecoin): grade missed entries by episode`.

### Task 4: EVM execution substage latency

**Files:**
- Modify: `sniper/services/memecoin_entry_trace.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/execution/evm_swap_executor.py`
- Create: `sniper/services/memecoin_latency_report.py`
- Modify: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_entry_trace.py`
- Test: `tests/test_memecoin_evm_entry_executor.py`
- Create: `tests/test_memecoin_latency_report.py`

**Interfaces:**
- Produces: `EntryAttemptTrace.mark_substage(name: str, **metadata) -> bool`
- Produces: `latency_report(conn, *, window_hours=168, chain=None, now_iso=None) -> dict`

- [ ] Add failing tests proving substages are monotonic, appear in `trace_json`, and do not alter top-level stage durations.
- [ ] Add failing executor tests for approval, quote, signing, confirmation, and persistence marks.
- [ ] Implement substage storage and instrument only existing calls.
- [ ] Implement a SELECT-only p50/p95 report and route.
- [ ] Run trace, EVM executor, retry, and latency tests.
- [ ] Commit `feat(memecoin): measure EVM execution substages`.

### Task 5: Shadow self-signal ledger

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Create: `sniper/services/memecoin_shadow_signals.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Create: `tests/test_memecoin_shadow_signals.py`
- Modify: `tests/test_memecoin_schema.py`

**Interfaces:**
- Produces: `evaluate_shadow_signals(conn, now_iso, *, quote_window_s=90) -> dict`
- Produces: `list_shadow_signals(conn, *, limit=100, chain=None, source=None) -> list[dict]`

- [ ] Add failing schema and evaluator tests for exact-chain identity, one row per episode, wait-before-deadline, immutable facts hash, and candidate/watch/reject outputs.
- [ ] Run focused tests; expect missing table/module failures.
- [ ] Add the additive table/index and pure scoring module.
- [ ] Call evaluation after `capture_v2_pass`, never from the funded hook.
- [ ] Add a read-only route and recent shadow-signal radar section.
- [ ] Run focused tests, runtime-loop tests, and JS syntax check.
- [ ] Commit `feat(memecoin): record shadow self signals`.

### Task 6: Wallet arming recommender

**Files:**
- Create: `sniper/services/memecoin_wallet_recommender.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Create: `tests/test_memecoin_wallet_recommender.py`

**Interfaces:**
- Produces: `wallet_recommendations(conn, *, min_paired=5, window_days=30, chain=None, now_iso=None) -> dict`

- [ ] Add failing tests for exact-event matching, chain separation, thin samples, drop-top-two, drawdown, and no writes.
- [ ] Run focused tests; expect import failure.
- [ ] Implement the SELECT-only recommender with deterministic reason codes.
- [ ] Add `GET /api/memecoin/wallets/recommendations` and render recommendations beside the arm matrix without an arming action.
- [ ] Run focused API/arm-matrix tests and JS syntax check.
- [ ] Commit `feat(memecoin): recommend wallet arming from paired evidence`.

### Task 7: Exit-policy tournament

**Files:**
- Create: `sniper/services/memecoin_exit_tournament.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Create: `tests/test_memecoin_exit_tournament.py`

**Interfaces:**
- Produces: `exit_policy_tournament(conn, *, window_days=30, chain=None, min_paired=30, now_iso=None) -> dict`

- [ ] Add failing tests for exact-origin pairing, legacy exclusion, segmentation, pairwise delta, drop-top-two, winner-swallow, and readiness thresholds.
- [ ] Run focused tests; expect import failure.
- [ ] Implement the SELECT-only tournament and protected-hold evidence counter.
- [ ] Add `GET /api/memecoin/exit-policy-tournament` and a Strategy Lab readiness section.
- [ ] Run focused API/strategy tests and JS syntax check.
- [ ] Commit `feat(memecoin): compare paired exit policies`.

### Task 8: Integration, evidence, and branch close

**Files:**
- Modify: design/plan only if implementation changes the approved contract.

- [ ] Run live DB reports read-only and record current capture, missed-entry, latency, shadow-signal, wallet, and tournament evidence.
- [ ] Review `git diff origin/main...HEAD` adversarially for identity mixing, future leakage, writes in read-only modules, and funded behavior drift.
- [ ] Run `python3 scripts/validate_schema_refs.py`.
- [ ] Run `python3 -m pytest tests/test_memecoin*.py -q`.
- [ ] Run relevant concurrency/runtime-deploy tests explicitly.
- [ ] Run `python3 -m compileall -q sniper scripts`, `node --check static/app.js`, and `git diff --check`.
- [ ] Run Knowledge Sync Gate audit and add the required Knowledge Delta Report to the PR.
- [ ] Publish the branch for review. Do not deploy funded runtime solely because shadow/read-only work passed tests.
