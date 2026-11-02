# V6 Admission and Quote-Alert Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every eligible live paper fill durably visible to V6, quote candidates concurrently
with bounded retries, and replace per-sweep quote-infrastructure Telegram spam with provider episodes.

**Architecture:** An append-only source-position reader feeds a local SQLite admission queue. The
single database owner claims queue rows while a bounded quote executor runs calls concurrently;
immutable attempt rows and terminal summaries preserve every outcome. Funded quote safety counters
stay unchanged while a pure alert reducer groups provider episode transitions.

**Tech Stack:** Python 3, asyncio, sqlite3 WAL, pytest, existing V6 paper worker and Telegram queue.

## Global Constraints

- V6 stays paper-only and `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` stays empty.
- Source, tape, and exit-state databases remain read-only; only the V6 paper database is writable.
- Maximum admission attempts: 3. Retry delays: 2 seconds then 5 seconds. Freshness: 90 seconds.
- Maximum concurrent entry quotes is bounded by free book capacity and never exceeds 4.
- No funded signing, approval, broadcast, reservation, or exit-policy change.
- Do not touch Coin Radar or Hyperliquid files.
- Use focused tests only; do not run a full suite.

---

### Task 1: Append-only source admission facts

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_signals.py`
- Modify: `tests/test_memecoin_v6_paper_signals.py`

**Interfaces:**
- Produces: `accepted_signal_scan(conn, after_id: int, limit: int)` for a bounded raw-position page,
  plus the compatibility `accepted_signals(...)` list surface. `AcceptedSignal.source_event_id` is
  the owning `memecoin_positions.id`.
- Consumes: `memecoin_positions.origin_event_id`, joined event metadata, wallet approval state.

- [ ] Add tests proving a paper fill discovered after an older mutable event cursor is returned,
  multiple arms deduplicate to the lowest position id, bounded raw pages advance across duplicate
  and ineligible rows, non-paper/unapproved/malformed rows are excluded or represented with exact
  terminal identity, and ordering/limit use position id.
- [ ] Run `python3 -m pytest tests/test_memecoin_v6_paper_signals.py -q`; confirm failures describe
  the current event query.
- [ ] Replace the mutable-event query with the paper-position query and immutable mapping.
- [ ] Rerun the focused test file and commit both files as `fix: source v6 admission from paper fills`.

### Task 2: Durable queue and schema-v6 migration

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Modify: `tests/test_memecoin_v6_paper_db.py`

**Interfaces:**
- Produces: `enqueue_admission`, `claim_admissions`, `record_entry_quote_result`,
  `recover_admission_leases`, `admission_summary`, and immutable `AdmissionClaim`.
- Queue natural key: `(origin_event_id, policy_id)`; attempts natural key:
  `(origin_event_id, policy_id, attempt_no)`.

- [ ] Add migration tests from a real v5 fixture and behavior tests for duplicate enqueue,
  conflicting identity, oldest-first multi-claim, open-plus-inflight cap, obsolete claim rejection,
  retry schedule, freshness expiry, terminal result, and restart lease recovery.
- [ ] Run the named DB tests and confirm schema/functions fail before implementation.
- [ ] Add explicit v5-to-v6 migration, required-column validation, dataclasses, and transaction-safe
  queue operations. Preserve existing immutable signal and entry contracts.
- [ ] Rerun `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q` and commit as
  `feat: add durable v6 admission queue`.

### Task 3: Concurrent fast-runtime admission

**Files:**
- Modify: `sniper/services/memecoin_v6_fast_runtime.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Modify: `sniper/settings/memecoin_v6_paper.py`
- Modify: `scripts/memecoin_v6_paper_worker.py`
- Modify: `tests/test_memecoin_v6_fast_runtime.py`
- Modify: `tests/test_memecoin_v6_paper_runner.py`
- Modify: `tests/test_memecoin_v6_paper_settings.py`

**Interfaces:**
- `FastRunnerAdapter.poll_entry(now_iso) -> EntryQuoteJob | None` ingests source rows, claims one
  eligible queue row, and returns a claim-token job.
- `FastRunnerAdapter.apply_entry(job, outcome, now_iso) -> bool` validates the claim before any fill.
- Runtime entry polling fills available quote lanes on startup, heartbeat, recovery, and completion.

- [ ] Add RED tests for two simultaneous entry jobs, one blocked chain not blocking another,
  heartbeat admission under one second, retry-to-fill, expiry-to-terminal, stale result rejection,
  crash recovery, and cap accounting with open plus in-flight positions.
- [ ] Run only the named runtime/runner/settings tests and confirm the current global single-flight
  and terminal quote failure behavior fails them.
- [ ] Wire queue ingestion, claims, retries, explicit settings, bounded Solana capture work, and
  repeated runtime polling. Keep provider calls outside the owner loop.
- [ ] Rerun the focused files plus `tests/test_memecoin_v6_paper_worker.py`; commit as
  `fix: run v6 admissions concurrently with retries`.

### Task 4: Provider-episode quote alerts

**Files:**
- Modify: `sniper/services/memecoin_alerts.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `tests/test_memecoin_engine_authoritative_exit.py`
- Create: `tests/test_memecoin_quote_infra_alerts.py`

**Interfaces:**
- Produces pure `quote_infra_alert_events(before, after, provider, failures)` records for start,
  milestone 5/20, and full recovery.
- Alert rendering consumes provider, failure, affected count, max streak, milestone, and recovery.

- [ ] Add RED unit tables for streak transitions 0-to-1, 1-to-2, 4-to-5, 19-to-20, restart at 6,
  mixed provider results, partial recovery, full recovery, and token-failure exclusion.
- [ ] Add an integration test proving two affected positions send one provider alert and retain
  independent unsellable counters.
- [ ] Implement the pure reducer, aggregate the prepass by provider, and update alert copy to say
  `token unsellable counters unchanged`.
- [ ] Run the two alert-focused files and commit as `fix: coalesce quote infra alert episodes`.

### Task 5: Admission operator evidence

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Modify: `tests/test_memecoin_v6_paper_report.py`
- Modify: `tests/test_memecoin_v6_paper_api.py`

**Interfaces:**
- `summary(conn)` adds `admission` with state counts, terminal reasons, oldest queue age, total
  immutable attempts, and the bounded count-unknown cursor-migration marker. Existing fields remain
  compatible.

- [ ] Add RED report/API tests using real queue rows for queued, retrying, filled, terminal, and the
  cursor-domain migration marker.
- [ ] Implement the compact report from ledger-owned queries; do not expose mutable controls.
- [ ] Run the report and API test files and commit as `feat: expose v6 admission evidence`.

### Task 6: Migration, validation, deployment, and live proof

**Files:**
- Modify: `docs/superpowers/plans/2026-07-27-v6-admission-and-quote-alert-remediation.md`
- Modify only if truth changed: `AGENTS.md`, `CLAUDE.md`

**Interfaces:**
- Runtime release is built from the merged main SHA with `scripts/deploy_memecoin_runtime.sh`.
- V6 paper worker remains separately enabled and has no funded authority.

- [ ] Run focused V6 admission, DB, runtime, worker, report, API, and funded quote-alert tests;
  run `python3 scripts/validate_schema_refs.py`, compile changed Python, `bash -n` changed shell,
  `node --check static/app.js` only if JS changed, and `git diff --check`.
- [ ] Run the Knowledge Sync Gate. Record `Knowledge delta: architecture and workflow` because the
  admission source, schema, and alert operations changed; update current doctrine if required.
- [ ] Commit the gate evidence. Merge the branch into the dedicated `main` worktree without touching
  active Radar/HL branches, then push normal `main`.
- [ ] Deploy the exact merged SHA, verify funded V6 authority remains empty, restart the V6 paper
  worker, and inspect process, heartbeat, cursor, queue, and logs.
- [ ] Prove live intake advances from a new paper fill or leave the worker waiting with a healthy
  empty queue; never synthesize a fake live signal. Confirm alert code is deployed without changing
  funded policy.
