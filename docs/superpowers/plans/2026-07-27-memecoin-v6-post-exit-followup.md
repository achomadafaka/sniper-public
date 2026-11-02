# V6 Post-Exit Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture fixed-horizon exact sold-quantity quotes after every V6 modeled action fill and expose whether the exit preceded further loss or recovery.

**Architecture:** The V6 ledger creates immutable follow-up schedules atomically with an action fill. The existing async quote scheduler services one low-priority due schedule at a time and writes one immutable typed result. Reporting reads those rows and adds score attribution; neither path calls the engine or changes position state.

**Tech Stack:** Python 3.13, SQLite WAL, asyncio, pytest, existing V6 quote/report/UI modules.

## Global Constraints

- Horizons are exactly `30,120,300,900,3600,10800,43200` seconds.
- Follow-ups quote the exact quantity sold by the referenced modeled action fill.
- No V6 policy constant, `policy_hash`, funded module, credential, signing, approval, broadcast, reservation, or production table is added or changed.
- Existing exits receive no synthetic backfill.
- One immutable result per `(action_token,horizon_s)`; failures remain typed evidence.

---

### Task 1: Immutable schedules and results

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Test: `tests/test_memecoin_v6_paper_db.py`

**Interfaces:**
- Produces: `due_exit_followup(conn, now_iso) -> ExitFollowupSchedule | None`
- Produces: `record_exit_followup_result(conn, followup_id, outcome, observed_at) -> bool`

- [ ] Write a failing test that completes a DERISK fill and expects seven schedules with the exact sold quantity and due timestamps.
- [ ] Run `python3 -m pytest tests/test_memecoin_v6_paper_db.py::test_action_fill_schedules_exact_post_exit_followups -q`; expect missing-table failure.
- [ ] Add schema version 5 tables `v6_paper_exit_followup_schedules` and `v6_paper_exit_followup_results`; create schedules in both action-fill paths inside the fill transaction.
- [ ] Write failing tests for due ordering, immutable success/failure results, and restart eligibility before a result.
- [ ] Implement the two public persistence functions and reconstruction checks.
- [ ] Run `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q`; expect pass.
- [ ] Commit with `git commit -m "feat: schedule v6 post-exit evidence"`.

### Task 2: Low-priority async follow-up quoting

**Files:**
- Modify: `sniper/services/memecoin_v6_fast_runtime.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Modify: `scripts/memecoin_v6_paper_worker.py`
- Test: `tests/test_memecoin_v6_fast_runtime.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: `due_exit_followup`, `record_exit_followup_result`
- Produces: `FollowupQuoteJob`, `FastRunnerAdapter.poll_followup`, `FastRunnerAdapter.apply_followup`

- [ ] Write failing runtime tests proving follow-up priority `20`, exact quantity, one outstanding job, and restart retry.
- [ ] Run the named tests; expect missing `FollowupQuoteJob`/callbacks.
- [ ] Add the follow-up job/result branch to `V6FastRuntime` and poll it from recovery without blocking entries or open-position events.
- [ ] Build the DB-free quote closure from closed-position entry context and persist typed `ModeledLiveFill`/`QuoteFailure` results.
- [ ] Run `python3 -m pytest tests/test_memecoin_v6_fast_runtime.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py -q`; expect pass.
- [ ] Commit with `git commit -m "feat: quote v6 post-exit followups"`.

### Task 3: Decision and follow-up insight

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Test: `tests/test_memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_ui.py`

**Interfaces:**
- Consumes: persisted staged-break rule results, observations, fills, follow-up results.
- Produces: `staged_break_diagnostics` in summary and `exit_followups` in position detail.

- [ ] Write failing report tests for early-score attribution and later executable proceeds/delta with actual elapsed time.
- [ ] Run the named report tests; expect missing fields.
- [ ] Add read-only aggregation without recomputing or changing any engine decision.
- [ ] Write failing UI text/DOM tests for an `After exit` section and missing-route labels.
- [ ] Render compact follow-up rows under position detail; keep the existing `V6 PAPER - NO FUNDS` boundary and no controls.
- [ ] Run `python3 -m pytest tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_ui.py -q` and `node --check static/app.js`; expect pass.
- [ ] Commit with `git commit -m "feat: show v6 exit followthrough"`.

### Task 4: Close and activate

**Files:**
- Modify only if required by the Knowledge Sync Gate: `AGENTS.md`, `CLAUDE.md`, `HANDOFF.md`.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: merged local `main` and a restarted V6-paper-only worker.

- [ ] Run focused V6 engine, DB, quote, observation, runtime, worker, report, API, UI, and funded-boundary tests.
- [ ] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`, compile checks, `node --check static/app.js`, source boundary scans, and `git diff --check`.
- [ ] Run the Knowledge Sync Gate and commit any required doctrine/index update separately.
- [ ] Merge the clean feature branch to local `main` without pushing.
- [ ] Restart only `scripts/memecoin_v6_paper_worker.py`, then verify PID freshness, V6 schema version 5, health timestamps, no funded imports/process changes, and no startup errors.

## Knowledge Sync Gate

1. Project doctrine changed: **no** — V6 remains the sole paper-position authority.
2. Module or subsystem added/retired: **yes** — immutable V6 post-exit measurement tables and an async quote lane.
3. Analyzer/strategy behavior changed: **no** — no engine constant, decision path, or policy hash changed.
4. Tests/validation changed: **yes** — focused DB, runtime, worker, report, API, and UI coverage.
5. Workflow/skill changed: **yes** — future action fills schedule seven measurement-only quotes.
6. Existing docs superseded: **no** — this narrows measurement under the current V6 design.
7. Bug lesson/semantic fix: **yes** — indexed mint lookup, semantic quote coalescing, and cache expiry.
8. Supabase schema/write/RLS changed: **no** — schema version 5 is isolated SQLite only.
9. Wallet insight/thesis/research candidate added: **no**.
10. Agent prerequisite knowledge changed: **yes** — registered in `AGENTS.md` and `CLAUDE.md`.
11. Legacy/frozen status changed: **no**.

**Delta:** yes
**Local file updates:** `AGENTS.md` Authoritative Docs; `CLAUDE.md` V6 candidate evaluation.
**Supabase entries created:** none — external publication was not invoked.
**Supabase entries superseded:** none.
**Open questions surfaced:** keyless Jupiter capacity still bounds concurrent fresh Solana quotes.
**Attestation:** The 11-question Knowledge Delta Report was run and recorded above.
