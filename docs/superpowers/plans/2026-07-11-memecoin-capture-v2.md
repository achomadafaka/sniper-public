# Memecoin Capture v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add replayable point-in-time feed, quote, horizon, clock, and coverage records without rewriting legacy evidence.

**Architecture:** Dual-write additive v2 tables from the existing runner. A capture coordinator owns cohort budgeting and horizons; research reads require one explicit capture version.

**Tech Stack:** Python 3.13, SQLite, pytest, JSON

## Global Constraints

- Existing v1 tables and live reads remain operational during rollout.
- V2 timestamps use `real_utc_now_iso()`; never SQLite wall time.
- Missing, late, suspect, and budget-skipped data remain visible in denominators.

---

### Task 1: Raw Feed Observation Contract

**Files:**
- Modify: `sniper/feeds/fomo_feed.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Create: `tests/test_memecoin_capture_v2_feed.py`

**Interfaces:**
- Produces: `FomoObservation(raw_type, raw_payload, event, source_ts_raw, source_ts, observed_at, lag_s)`
- Produces: `insert_feed_observation_v2(conn, observation, was_curated, was_real_approved) -> bool`

- [ ] Add tests proving non-trade types persist, trade normalization is optional, flags freeze before roster changes, and duplicate upstream ids dedup.
- [ ] Run the test and confirm current fetch/normalization discards non-buy/sell payloads.
- [ ] Preserve every returned raw item, add the v2 schema/indexes, and dual-write before curated filtering.
- [ ] Rerun feed/runner tests; commit `feat(memecoin): retain versioned raw feed observations`.

### Task 2: Capture Cohorts and Exact Quote Sweeps

**Files:**
- Create: `sniper/services/memecoin_capture_v2.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Create: `tests/test_memecoin_capture_v2_quotes.py`

**Interfaces:**
- Produces: `register_capture_cohort(conn, network_id, mint, context, observed_at, expires_at, priority) -> None`
- Produces: `capture_v2_sweep(conn, now_iso, stats_batch_fn, max_mints) -> CaptureSweepResult`

- [ ] Add tests for context merge, two-chain isolation, exact separate sweeps, budget ordering, skipped counts, pair-change confirmation, ratio-outlier exclusion, quality retention, and stable ties.
- [ ] Run the test and confirm the minute-unique v1 ledger loses observations.
- [ ] Add cohort/quote/sweep tables, deterministic due selection, pair continuity state, quality classification, and keyed quote calls.
- [ ] Register signal and open-position cohorts, then run the v2 sweep on the existing 30-second cadence while preserving v1 dual-write.
- [ ] Rerun pump, mark, runner, and new capture tests; commit `feat(memecoin): capture exact versioned quote paths`.

### Task 3: Honest Horizon Assignment

**Files:**
- Modify: `sniper/services/memecoin_capture_v2.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Create: `tests/test_memecoin_capture_v2_horizons.py`

**Interfaces:**
- Produces: `create_outcome_v2(...) -> str`
- Produces: `assign_due_horizons(conn, now_iso, tolerance_s) -> HorizonResult`

- [ ] Add on-time, early, late, reused-observation, bad-quality, missing, and abandoned tests.
- [ ] Run the test and confirm no v2 horizon model exists.
- [ ] Add outcome/horizon tables, target manifest, unique observation assignment, tolerance closure, and explicit coverage status.
- [ ] Wire qualifying buy signals to create v2 outcomes and cohorts.
- [ ] Rerun v1 pump tests plus v2 horizon tests; commit `feat(memecoin): preserve horizon timing and missingness`.

### Task 4: Clock Classification and Version-Safe Research

**Files:**
- Modify: `scripts/research/normalize_clock_domains.py`
- Modify: `scripts/research/memecoin_conditioning_study.py`
- Create: `scripts/research/memecoin_capture_v2_manifest.py`
- Create: `tests/test_memecoin_capture_v2_research.py`

**Interfaces:**
- Produces: `classify_legacy_clock_rows(conn, apply=False) -> dict`
- Produces: `build_capture_manifest(conn, capture_version=2) -> dict`

- [ ] Add idempotent dry-run/apply tests, ambiguous-row preservation, replay ordering, manifest denominator, and mixed-version rejection.
- [ ] Run the tests and confirm the current study pools one unversioned `max_gain_pct` surface.
- [ ] Add explicit legacy clock/capture columns where needed, classify only provable rows, and make v2 the study default with an explicit legacy mode.
- [ ] Emit manifest counts, spans, networks, coins, quality exclusions, horizon coverage, and budget gaps.
- [ ] Run schema validation, study fixture tests, and the full focused suite; commit `fix(memecoin): separate capture versions and clock domains`.
