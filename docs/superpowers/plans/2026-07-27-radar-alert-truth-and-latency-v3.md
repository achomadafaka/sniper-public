# Radar Alert Truth and Latency v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce correctly attributed alert-time paper outcomes, spend bounded quote capacity only on inward candidates, and restore stable live capture without weakening data-gap honesty.

**Architecture:** Preserve generic landmark research while adding an append-only alert-to-entry relation and a distinct v3 alert evaluator. Split direction evaluation into non-route qualification followed by prioritized remote quoting; completed fresh quote pairs return to the main worker for immediate state re-evaluation. Protect all durable frontier cursor facts from retention so a cleanup cannot make the next restart impossible.

**Tech Stack:** Python 3.13, SQLite WAL, asyncio/threaded bounded queues, Flask JSON, vanilla JavaScript, pytest.

## Global Constraints

- Capture-only and paper-only. No funded, signing, submission, position, reservation, or funded-runner imports.
- Keep the Jupiter default provider pace at one call every 2 seconds for the first rollout.
- Defaults: queue age 2,000 ms; quote-pair age 3,500 ms; completion age 1,000 ms; cache 256; completions 256.
- Old unlinked alerts are `UNSCORED_LEGACY` and contribute no v3 alert metrics.
- Generic landmark diagnostics remain separate from alert-linked evidence.
- Use focused tests only; do not run the full suite.
- Preserve immutable receipts, corrected UTC, chain-native trigger clocks, and explicit source gaps.

---

### Task 1: Preserve frontier evidence and stop the worker crash loop

**Files:**
- Modify: `sniper/research/memecoin_opportunity/retention.py`
- Test: `tests/test_memecoin_opportunity_retention.py`

**Interfaces:**
- Consumes: `op_source_frontiers` and unresolved `op_data_gaps` cursor fields.
- Produces: `_protected_raw_event_ids(conn) -> set[str]`, used by episode and orphan cleanup.

- [ ] **Step 1: Write failing cursor-retention tests**

Add one test where an old canonical episode owns the frontier's `durable_cursor`, and one where an orphan raw row owns an unresolved gap's `resume_cursor`. Run cleanup and assert both raw rows remain while unrelated rows are removed.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_retention.py -q
```

Expected: the new tests fail because cleanup deletes the cursor facts.

- [ ] **Step 3: Protect referenced raw facts**

Implement one bounded query that resolves raw IDs referenced by:

```text
op_source_frontiers.durable_cursor
op_source_frontiers.resume_cursor
op_source_frontiers.latest_observed_cursor
unresolved op_data_gaps.previous_durable_cursor
unresolved op_data_gaps.resume_cursor
unresolved op_data_gaps.newest_observed_cursor
```

Skip those raw IDs in `_delete_episode` and the orphan purge. Do not clear or fabricate frontier state.

- [ ] **Step 4: Verify GREEN**

Run the focused retention test file and `git diff --check`.

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/retention.py tests/test_memecoin_opportunity_retention.py
git commit -m "fix(radar): retain durable frontier evidence"
```

### Task 2: Add immutable alert-entry identity

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/research/memecoin_opportunity/outcomes.py`
- Test: `tests/test_memecoin_opportunity_store.py`
- Test: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Produces: `forward_alert_id(launch_id, rule_version, clip_usd_micros) -> str`.
- Produces: `append_forward_alert_entry(conn, *, alert_id, entry_id, snapshot_id, quote_observed_at, quote_completed_at, quote_age_ms, entry_effective_at, provider, evaluator_version) -> bool`.
- Persists: `op_forward_alert_entries` with unique `alert_id` and `entry_id`.

- [ ] **Step 1: Write failing schema and immutability tests**

Assert schema version 8, relation columns, foreign keys, idempotent identical insertion, conflicting insertion rejection, and update/delete rejection.

- [ ] **Step 2: Verify RED**

Run the two named test files with `-q`; expect missing relation/helper failures.

- [ ] **Step 3: Implement schema v8 and helpers**

Add the table to `SCHEMA`, allow v7 migration, install append-only triggers, centralize the deterministic alert ID, and use the helper from alert persistence.

- [ ] **Step 4: Verify GREEN**

Run the two focused files and `python3 scripts/validate_schema_refs.py`.

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/store.py sniper/research/memecoin_opportunity/projection.py sniper/research/memecoin_opportunity/outcomes.py tests/test_memecoin_opportunity_store.py tests/test_memecoin_opportunity_engine.py
git commit -m "feat(radar): link alerts to exact paper entries"
```

### Task 3: Bound and prioritize Solana quote work

**Files:**
- Modify: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/settings/memecoin_opportunity.py`
- Modify: `.env.example`
- Test: `tests/test_memecoin_opportunity_solana_probe.py`
- Test: `tests/test_memecoin_opportunity_directional.py`
- Test: `tests/test_memecoin_opportunity_settings.py`

**Interfaces:**
- Extend `RemoteRoundTripProbe` with corrected timing and state identity.
- Produce `OpportunitySolanaProbe.drain_completions(limit: int = 32) -> tuple[RemoteRoundTripProbe, ...]`.
- Produce metrics for stale discards, cache evictions, completion drops, queue delay, pair age, and completion age.

- [ ] **Step 1: Write failing qualification, freshness, priority, and bound tests**

Cover weak landmarks not enqueuing, INNER candidates enqueuing, priority ordering, stale queue discard before provider calls, stale result rejection, expired global cache eviction, cache cap 256, completion cap 256, and unchanged call pacing.

- [ ] **Step 2: Verify RED**

Run the three focused test files; expect failures on missing timing, bounds, and two-stage qualification.

- [ ] **Step 3: Implement two-stage qualification and bounded priority queues**

Evaluate Level-1 once with remote fields false. Call the provider only when the result is `INNER`. Coalesce by venue/token/clip, attach the launch state fingerprint, discard stale queued work, and sweep expired cache entries on every enqueue/completion/metrics read.

- [ ] **Step 4: Implement explicit freshness blockers**

Set `REMOTE_QUOTE_QUEUE_STALE`, `REMOTE_QUOTE_PAIR_STALE`, or `REMOTE_QUOTE_RESULT_STALE`. A stale result cannot set both remote route booleans true.

- [ ] **Step 5: Verify GREEN and commit**

Run the three focused files plus `git diff --check`, then commit only the named paths as `feat(radar): prioritize fresh executable quotes`.

### Task 4: Re-evaluate on quote completion and create the exact alert entry

**Files:**
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_engine.py`
- Test: `tests/test_memecoin_opportunity_runtime.py`

**Interfaces:**
- Produce: `OpportunityEngine.observe_remote_completion(result, *, observed_at: str) -> EngineObservation | None`.
- Consume: `OpportunitySolanaProbe.drain_completions()` from Task 3.
- Produce: one `CounterfactualEntry` from exact acquired tokens and one `op_forward_alert_entries` row per v3 alert.

- [ ] **Step 1: Write failing completion and attribution tests**

Prove a completed quote can move the unchanged latest state from `INNER` to `CENTER` without a new chain event; deterioration while the request was in flight blocks it; and the alert-linked entry uses remote acquired tokens with `entry_effective_at = quote_completed_at + configured entry latency`.

- [ ] **Step 2: Verify RED**

Run the engine and runtime files; expect missing completion consumer failures.

- [ ] **Step 3: Implement main-thread completion consumption**

Drain at most 32 completions per worker loop. Recheck current non-route facts and state fingerprint, persist a decision timestamp later than the referenced canonical event, and never write SQLite from the quote thread.

- [ ] **Step 4: Append the exact paper entry and link**

Use the remote `tokens_out_atomic`, $25 entry cost, remote provider/version, and configured latency. Insert the alert, entry, and link idempotently before recording later outcomes.

- [ ] **Step 5: Verify GREEN and commit**

Run the two focused files and commit the named paths as `feat(radar): evaluate exact entries at quote completion`.

### Task 5: Compute v3 alert results without contaminating launch research

**Files:**
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Modify: `sniper/research/memecoin_opportunity/diagnostics.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_insights.py`
- Test: `tests/test_memecoin_opportunity_api.py`

**Interfaces:**
- Produce API fields: `alert_evidence`, `alert_summary`, and `generic_launch_diagnostics`.
- Alert rows include quote drag, quote ages, exact entry time, MFE, MAE, time-to-best, target ladder, terminal result, and score status.

- [ ] **Step 1: Write failing mismatched-outcome regression tests**

Seed first/latest launch outcomes that differ from the linked alert entry. Assert only the linked outcome appears. Seed an unlinked old alert and assert `UNSCORED_LEGACY` with no contribution to alert counts or return metrics.

- [ ] **Step 2: Verify RED**

Run the insight and API files; expect the current launch-level selector to return the wrong outcome.

- [ ] **Step 3: Add alert-linked aggregation**

Evaluate targets from observations belonging to `op_forward_alert_entries.entry_id`. Keep `_diagnostic_receipts` explicitly named and returned as generic landmark research. Never fall back from an alert to a launch-level entry.

- [ ] **Step 4: Verify GREEN and commit**

Run both files, the schema validator, and commit the named paths as `fix(radar): score only exact alert entries`.

### Task 6: Make the UI decision-first and semantically honest

**Files:**
- Modify: `static/app.js`
- Modify: `static/styles.css` or the existing Radar stylesheet discovered by `rg`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes the Task 5 API fields.
- Renders `WATCH`, `QUOTE NEEDED`, `ROUTE READY`, `FORWARD RESULT`, and `UNSCORED LEGACY`.

- [ ] **Step 1: Write failing UI contract assertions**

Require plain decision labels, forbid `Only CENTER is actionable`, require distinct quote-drag and forward-return labels, and require terminal result plus target hits.

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py -q`.

- [ ] **Step 3: Implement stable incremental rendering**

Update the existing keyed Radar DOM without replacing the page or open detail elements. Put current decision first, forward results second, and generic launch research under a collapsed labelled section.

- [ ] **Step 4: Verify GREEN and commit**

Run the UI contract and API tests, then commit the named paths as `fix(radar): separate route readiness from forward results`.

### Task 7: Fresh activation and live verification

**Files:**
- No production source changes unless live evidence reveals a new reproducible defect.

**Interfaces:**
- Uses: `scripts/prepare_memecoin_opportunity_retention.py --fresh-hot` and `scripts/memecoin_opportunity_watchdog.sh`.

- [ ] **Step 1: Run focused verification**

Run only opportunity retention, store, probe, directional, engine, runtime, insights, API, UI-contract tests; run `python3 scripts/validate_schema_refs.py`; scan opportunity imports for funded authority; run `git diff --check`.

- [ ] **Step 2: Stop the isolated dead worker and activate a fresh hot ledger**

Confirm the PID does not belong to another process. Run the project-first activation against the configured hot and insight paths with `--fresh-hot`. Existing insight receipts remain; old alerts render unscored.

- [ ] **Step 3: Restart through the watchdog**

Start only `scripts/memecoin_opportunity_worker.py` through its watchdog. Do not restart the funded runner. Restart `sniper-web.service` only if the loaded web process predates the UI commit.

- [ ] **Step 4: Verify live operation**

Require a live worker PID, advancing corrected heartbeat, no open gaps after bounded bootstrap recovery, healthy storage/RPC/quote lanes, bounded cache and queues, stable API refreshes, and no page-level DOM replacement. If no natural `ROUTE READY` alert occurs, report it honestly rather than forcing one.

- [ ] **Step 5: Close knowledge and commit any operational-only documentation**

Run the Knowledge Sync Gate. Record `Knowledge delta: none` if implementation only restores the approved v3 contract. Confirm `main` status and do not push without a separate explicit push instruction.
