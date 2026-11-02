# Desk Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox tracking.

**Goal:** Extend the existing immutable lessons and daily report with honest watch-entry bounds, entry-stage shifts, exit comparisons and descriptive close-batch digests.

**Architecture:** Keep the existing lessons table, worker and replay engine. Add a new feature version and a nested `learning_loop` fact block; render it once per realized position through the current daily report. Missing evidence remains unknown, including on early censored rows.

**Tech Stack:** Existing Python/SQLite readers, native curve math, Markdown and pytest; no dependencies.

**Spec:** Operator decision `f9d358d6`, Alpha/Gamma accepted contract on 2026-10-26, and `docs/audits/2026-10-26-standing-orders-preregistration.md` section 3.

## Global Constraints

- Report only; no provider/model calls, funded writes, runtime/cron/env changes or automatic rules.
- Native venue fees and clip impact, plus explicitly named 2.5 percentage-point residual model cost; all-in net is literal `unknown`.
- Response/touch entries use 1.5 seconds of modeled lag and the same exact clip; fixed comparison horizon is 300 seconds. Existing exit lag stays 1 second.
- Watch bounds come from frozen response/expiry and actual fill, never the old fill-plus/minus-120-second window. Hindsight bounds are never called achievable fills.
- Native quote and every fee must be known; nonzero cashback is unsupported and censored. Pool entries are a separate censored category.
- Bands are [4000,10000), [10000,20000), [20000,30000), and 30k-completion (curve). Sheet cap comes from frozen JUDGE inputs; fill cap comes from recorded entry cap, otherwise unknown.
- Existing integer quantities, FX0 modeling assumption and clock provenance stay explicit. Human timestamps are corrected UTC; desk/tape host time is converted exactly once.
- One row per realized position; cohort/prompt populations stay separate. Twenty-close batches set cadence only; unknown cohort/prompt cannot produce a candidate batch.
- Grids retain live booked rails, 10/15/20-percent trails, existing time caps, hold-to-plan if known, and an explicit no-exit terminal mark. A missing baseline is censored.
- Old feature rows are immutable and retained. This task does not activate the worker or backfill production lessons.

## Review Focus

- A cheaper point before WATCH or after fill must not enter the watch minimum; gaps make a full-window bound unknown.
- Missing quote/cashback proof must not turn into zero fees; known zero is valid.
- Early censorship retains position/cohort/prompt and corrected close time, so missing evidence does not erase a trade.
- Exact band edges and post-migration entries stay distinct; fixed FX0 must not invent an actual fill-market-cap shift.
- Batches never pool cohorts/prompts, and intervals count coins rather than treating repeat trades as independent.

### Task 1: Facts and worker metadata

**Files:** `sniper/services/memecoin_desk_lessons.py`, `scripts/memecoin_desk_lessons.py`, `tests/test_desk_lessons_learning_loop.py`.

**Interfaces:** `mcap_stage(mcap_usd, migrated=False) -> str`; `learning_facts(unit, tape, ...) -> dict`; worker `_learning_inputs(vet, trigger=None) -> dict`. A nested block carries basis/cost assumptions, window coverage, response/touch/fill comparisons, stage shift, baselines and explicit censor reasons.

- [x] Write one fixture test covering bounds, costs/unknowns, clocks, band edges, early censor identity and report integration.
- [x] Run `flock /tmp/sniper-pytest.lock python3 -m pytest -n 2 tests/test_desk_lessons_learning_loop.py -q`; observed missing `learning_facts` assertion failure.
- [ ] Extend source metadata and optional tape-proof columns without changing existing readers' missing-column semantics.
- [ ] Reuse native replay math; add trail/plan/mark comparisons and guard the new block against unsupported units or proof.
- [ ] Preserve identity on every completed row and bump the immutable feature version; do not run production persistence.

### Task 2: Deterministic report and cadence

**Files:** `sniper/services/memecoin_desk_lessons_digest.py`, `scripts/memecoin_desk_daily_report.py`, same fixture test.

**Interfaces:** `learning_loop(rows, batch_n=20) -> dict`; `render_learning_loop(rows, batch_n=20) -> list[str]`. Consume existing `facts.learning_loop`; older rows render explicit unavailable values. Retain immutable arrival order in the loader for stable stored-close batches.

- [x] Extend the fixture with two cohort/prompt groups, a censored trade, repeated coin IDs and a partial final batch.
- [ ] Render one per-position table, sheet-to-fill stage transitions, comparison counts and completed-batch digests using the current proposal/clustered-interval helpers.
- [ ] Label grid-derived proposals `hypothesis, needs prereg + holdout` and print `descriptive; no rule change from a digest alone`.
- [ ] Append the new section once through the existing lessons-enabled daily-report branch; make no new review calls.

### Task 3: Verify and review

- [ ] Green the new fixture and run the existing focused daily-report/lessons/witness files with `-n 2`, serialized through the shared test lock.
- [ ] Validate schema references on owned files; auxiliary desk/lesson/tape schemas come from their current DDL.
- [ ] Use `scripts/crew_commit.sh` with only owned paths, then send the stable SHA and evidence to Gamma/Alpha.
- [ ] Keep source/test proof separate from runtime collection, executable landing and strategy evidence.
