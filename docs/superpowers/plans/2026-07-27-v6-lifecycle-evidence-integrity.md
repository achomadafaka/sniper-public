# V6 Lifecycle Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make V6 paper lifecycle evidence crash-safe, monotonic, policy-separated, and explicit about provider, admission, drawdown, and quote-unit uncertainty.

**Architecture:** Keep the funded boundary unchanged. Normalize quote facts before policy evaluation, finalize successful admission in one V6-ledger transaction, give each persisted semantic transition a monotonic per-position identity, compute performance per immutable policy cohort, and persist raw admission rejection evidence in schema v7.

**Tech Stack:** Python 3, sqlite3 WAL, pytest, existing isolated V6 paper worker and report API.

## Global Constraints

- V6 stays paper-only; no signer, approval, broadcast, reservation, funded policy, production DB write, service restart, deployment, or external provider call.
- Source/tape/state databases remain read-only. Only temporary test V6 databases are written.
- Existing V1/V2 evidence remains immutable and is never rewritten or re-attributed.
- Current performance uses exact `(engine_policy_id, policy_hash)` identity. Operational book counts may remain all-policy and must be labeled as such.
- Every behavior fix is RED-first. Run focused tests before the complete `tests/test_memecoin_v6_*.py` suite.

---

### Task 1: Separate provider failure from route loss and retire invalidated intents

**Files:**
- Modify: `sniper/services/memecoin_v6_observations.py`
- Modify: `sniper/services/memecoin_v6_engine.py`
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Test: `tests/test_memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_paper_db.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- `route_degraded` is true only for typed `QuoteStatus.ROUTE_ABSENT` evidence.
- A semantic HOLD that explicitly invalidates an active intent sets `supersedes_token` to the active token, clears the state intent, and persists that intent as `superseded` with a cancellation reason.
- Provider-unavailable and stale observations cannot create or strengthen route-loss/break actions.

- [x] Add a failing observation test proving provider-unavailable and stale outcomes set `route_degraded=False`, while route-absent sets it true.
- [x] Add a failing engine test for `DERISK` without degradation versus provider failure without `EXIT_FULL` escalation.
- [x] Add a failing engine/ledger/runner test proving healthy recovery supersedes an unfilled action intent before another exact-quote job can fill it.
- [x] Run the named tests and confirm failures are the reported route conflation and retained intent.
- [x] Implement the minimal normalization, explicit HOLD cancellation transition, and persistence update.
- [x] Add the behavior switches to `policy_document()` so the policy hash changes while `engine_policy_id` remains `v6-position-engine-v2`.
- [x] Rerun the focused tests.

### Task 2: Make successful admission finalization crash-atomic and restart-reconcilable

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_db.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- `finalize_admission_fill(conn, claim, entry, completed_at, provider) -> V6PaperPosition` validates the live claim and atomically writes the immutable quote attempt, filled signal summary, entry/position/fill, and queue terminal state.
- `recover_admission_leases()` first reconciles an existing filled entry or terminal signal summary, then expires only genuinely unfinished claims.

- [x] Add a failing transaction-injection test showing any exception rolls back signal, entry, quote attempt, and queue completion together.
- [x] Add the exact legacy crash-window fixture: existing filled signal/entry plus expired in-flight queue; recovery must mark the queue `filled`, not `terminal_unfilled`.
- [x] Add a terminal-summary recovery fixture proving a lease cannot overwrite terminal evidence.
- [x] Run the focused DB/runner tests and confirm the current three-transaction path and lease recovery fail.
- [x] Extract in-transaction helpers from `record_signal_attempt`, `record_entry_fill`, and `record_entry_quote_result`; compose them in `finalize_admission_fill`.
- [x] Replace the runner’s three writes with the atomic finalizer and implement restart reconciliation.
- [x] Rerun the focused tests.

### Task 3: Add monotonic persisted observation identity

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Modify: `sniper/services/memecoin_v6_observations.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_paper_db.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- `_position_context()` supplies `observation_sequence = persisted observation count + 1`.
- `semantic_observation_id(position_id, evidence, sequence=None)` includes the sequence when supplied; fixture callers without a sequence retain the compatibility identity.
- `_action_token()` includes `observation.observation_id`, preventing A-to-B-to-A decisions from reusing the first A token.

- [x] Add a failing A-to-B-to-A integration test that appends all three observations without `ImmutableConflict` and preserves ordering.
- [x] Add a repeated-wake test proving unchanged evidence remains a semantic no-op and does not consume a sequence.
- [x] Run the focused engine/observation/DB/runner tests and confirm the third transition conflicts today.
- [x] Implement the sequence plumbing and action-token binding.
- [x] Rerun the focused tests.

### Task 4: Report exact policy cohorts and legitimate drawdown lows

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_api.py`

**Interfaces:**
- `summary(conn)` returns `current_policy`, `policy_cohorts`, and `all_policy_counts_by_chain`.
- Top-level performance fields project only the exact current `(engine_policy_id, policy_hash)` cohort; each cohort contains the same performance fields and counts.
- Catastrophe evaluation emits `catastrophe:quote_quarantined` only for the adverse-ratio quarantine. Reporting excludes only that explicit reason from drawdown.

- [x] Add a failing mixed V1/V2 fixture proving the current cohort has only its own closed lifecycles, return, and drawdown; assert both cohorts remain visible.
- [x] Add a failing drawdown fixture proving a first valid catastrophic confirmation-pending low is included, while an explicitly quarantined quote is excluded.
- [x] Run report/API/engine tests and confirm current aggregation and broad quarantine fail.
- [x] Refactor report accumulation into one cohort helper, retain all-policy operational counts under an explicit label, and project the current exact hash at top level.
- [x] Emit and consume the explicit quarantine reason.
- [x] Rerun focused tests.

### Task 5: Persist every bounded-page admission rejection

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_signals.py`
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_signals.py`
- Test: `tests/test_memecoin_v6_paper_db.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_report.py`

**Interfaces:**
- `source_admission_scan(conn, after_id, limit) -> AdmissionScanPage` left-joins raw position, event, and wallet facts and returns accepted signals plus typed `AdmissionRejection` rows and the inclusive cursor.
- Schema v7 adds immutable `v6_paper_admission_rejections(source_position_id PRIMARY KEY, origin_event_id, network_id, mint, reason_code, detail_json, observed_at, content_fingerprint)`.
- `record_admission_rejection()` is exact-redelivery idempotent and conflicting-redelivery immutable.

- [x] Add failing scan tests for missing event, missing wallet, unsupported network, blank origin/mint, non-paper, non-buy, paused/excluded/unapproved wallet, invalid timestamp/nonpositive fill, and duplicate origin.
- [x] Add failing v6-to-v7 migration and immutable rejection persistence tests.
- [x] Add a runner test proving rejections persist before the source cursor advances and never reach a quote adapter.
- [x] Add a report test for rejection counts/reasons separate from quote-terminal outcomes.
- [x] Run focused signal/DB/runner/report tests and confirm filtered rows currently disappear.
- [x] Implement the left-join classifier, explicit migration, persistence, runner ingestion, and report surface.
- [x] Rerun focused tests.

### Task 6: Fail closed on unknown quote-unit comparability

**Files:**
- Modify: `sniper/services/memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Executable price may substitute for missing structural price only when both quote assets are known and normalize to the same unit.
- Observation payload exposes `quote_units_comparable` as `true`, `false`, or `null`; unknown identity remains data-insufficient.

- [x] Add failing Solana tests for missing market asset, missing executable quote asset, equal USDC assets, and unequal WSOL/USDC assets.
- [x] Run the focused observation/engine tests and confirm missing identity is currently accepted.
- [x] Implement tri-state unit evidence and the fail-closed substitution rule.
- [x] Rerun focused tests.

### Task 7: Adjacent audit and verification

**Files:**
- Modify only files with a reproduced certain defect in the same V6 paper lifecycle boundary.
- Update: `docs/superpowers/plans/2026-07-27-v6-lifecycle-evidence-integrity.md`

- [x] Search V6 source-cursor, immutable-identity, transaction, intent, report-denominator, and unit-comparison paths for the same defect classes.
- [x] For each certain additional defect, add a failing focused test, implement the smallest repair, and record it in this plan. Leave uncertain findings unresolved and report them.
- [x] Run `python3 -m pytest tests/test_memecoin_v6_*.py -q`.
- [x] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`.
- [x] Run `python3 -m compileall -q` on changed Python modules.
- [x] Run `git diff --check` and inspect `git status --short` plus the complete diff.
- [x] Confirm no changed V6 paper module imports funded signing, approval, broadcast, reservation, or execution code.
- [x] Do not restart or deploy. Report host-level worker/watchdog liveness as unresolved unless separately authorized and verified.

#### Adjacent certain defects repaired

- The compatibility `--once` paper runner still wrote its filled signal and entry in separate
  transactions. `finalize_direct_entry_fill()` now commits them together; exact redelivery after a
  cursor lag remains idempotent.
- Worker startup still required schema v6 after admission rejection evidence moved the ledger to
  v7. Startup now requires the complete v7 schema. The raw classifier also avoids depending on the
  optional source `entry_notional` column, preserving smoke/older source-schema compatibility.

#### Verification record

- `python3 -m pytest tests/test_memecoin_v6_*.py -q`: 391 passed.
- `python3 scripts/validate_schema_refs.py --warn-unknown-tables`: no violations; 66 unrelated
  pre-existing unknown-table warnings.
- Changed-module `compileall`: passed. `git diff --check`: passed.
- A read-only backup of the live V6 ledger migrated to schema v7 and rendered without mutation:
  22 closed, 2 censored, 1 open across all policies; 14 graded V1 and 8 graded prior-hash V2.
  The repaired current exact policy hash has 0 graded lifecycles, as required by cohort separation.
- No service restart, deployment, provider call, production DB write, or funded-path change was
  performed. The temporary backup was deleted after verification.
