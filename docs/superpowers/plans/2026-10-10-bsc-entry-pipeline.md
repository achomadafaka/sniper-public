# BSC entry pipeline implementation plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` task by task. The operator authorized implementation and local activation; retain the flat-book deployment requirement.

**Goal:** Remove receipt head-of-line blocking and redundant read latency without weakening funded source, route, safety or signed-output proof.

**Architecture:** Reuse bounded decoder admission for canonical BSC receipts and compose unfinished coverage with existing recovery. Batch the exact existing V2 route reads through the verified V3 read transport. Share one fresh Flap state only inside a single check. Separate stored quote-to-confirmation age from quote-to-submission age.

**Tech stack:** Existing Python threads/futures, SQLite WAL, JSON-RPC read pool, pytest with `-n 2`.

**Specs:** Current EVM watcher gap-backfill/recovery, BSC fast-lane and fill-timing specs; dated amendments describe the changed behavior. See the accompanying audit for source paths and exact verification.

## Global constraints

- No new provider, discovery poll, synchronous entry wait, global mutable state or executable quote cache.
- Original receive time, custody/chain/token/amount identity, source initiation/payment, tax, premium, round-trip, preflight and signed ownership stay authoritative.
- Keep canonical recovery synchronous: queue admission is not evidence that a block was decoded.
- No full suites; one focused test job at a time, explicitly `-n 2`.
- Activate a merged immutable SHA locally with `--restart --require-flat --defer-backup`; preserve foreign work and rollback.

## Review focus

- Queue full, expired, unknown, failed and stopped work must hold a durable frontier and receive bounded recovery.
- Roster revocation and shutdown must suppress new publication, without serializing provider calls behind a publication lock.
- Exact integer JSON-RPC IDs bind outputs; missing, duplicate, Boolean and unsolicited IDs are unavailable, not a price or an empty route.
- Flap state/fee/identity changes between attempts are reread; exact clip simulations still run.
- Naive/reversed/missing clocks must remain unknown; confirmation time must never masquerade as submission time.

## Task 1: Canonical receipt admission

**Files:** `scripts/research/onchain_evm_wallet_watch.py`, a minimal existing-worker adapter if needed, `tests/test_onchain_evm_watch_async_decode.py`.

**Interface:** Socket admission returns promptly; worker invokes existing synchronous `on_transfer` using original arrival/roster and worker-owned ledger/tape state. Heartbeat composes queued/active/unread coverage with gap and Relay-origin frontiers.

- [x] Verify blocked decode A cannot hide a head or independent decode B; retain original clocks and current roster authority.
- [x] Implement bounded admission, dedupe, worker ownership, typed incomplete coverage and shutdown fencing.
- [x] Run new regressions and nearest restart/backfill/null-read/roster/BSC handoff gates; measure offline admission p50/p90.
- [x] Review and commit this logical change independently.

## Task 2: Exact route read batches

**Files:** `sniper/execution/evm_dex_quote.py`, `sniper/execution/evm_v2_exit_quote.py`, `tests/test_memecoin_v2_quote_batch.py`.

**Interface:** Shared `batch_responses(calls)` returns strictly matched read replies, explicit unsupported batching returns `None`, unavailable reads remain typed. Multi-path V2 scans use it; single-path mixed legs remain scalar.

- [x] Verify RED: 15 failed, 2 passed; old V3 duplicate IDs cross-bind an output and V2 reads serially.
- [x] Preserve exact path/output ranking, minimums, calldata and revert versus infrastructure classification.
- [x] Run nearest V2/V3/mixed/round-trip gates and offline equal-output latency proof.
- [x] Review and commit independently.

## Task 3: Flap safety state

**Files:** `sniper/execution/evm_flap_curve.py`, `tests/test_memecoin_flap_check_state_binding.py`, the existing warm-simulation call-count assertion.

**Interface:** One fresh, copied and identity-bound state per synchronous check; standalone exits and the next check reread state.

- [x] Verify RED: 8 failed, 6 passed, including repeated reads and mutation contamination.
- [x] Retain exact whole-clip simulations, graduation/native/converted rules, fee verification, deadline and actual-exit behavior.
- [x] Run nearest Flap/token-safety gates; measure offline request count and p50/p90.
- [x] Review and commit independently.

## Task 4: Honest attribution and activation

**Files:** `sniper/services/memecoin_fill_timing.py`, `tests/test_memecoin_quote_boundary_attribution.py`, corrected prior latency audit and new engineering report.

**Interface:** Add read-only boundary facts; keep legacy `quote_age_ms` meaning and persisted schema intact.

- [x] Verify false submission-age attribution and missing clock boundaries before changing code.
- [x] Preserve source/recovery cohorts and unknowns; record remaining native format evidence limits honestly.
- [ ] Run focused integration, schema/contract checks and knowledge-sync in `audit_only`; attach and merge reviewed PR.
- [ ] Wait for a flat book, activate runner/web from the merged SHA, verify actual process paths, fresh beats, authenticated APIs, assets, queue/native readiness and ledger state.

## Review amendment: early safety completion

`EarlySafety.wait` now binds PASS reuse to its recorded monotonic completion, before the
original deadline. Verified RED: 4 failed, 1 passed. Focused deadline/V2/V3/mixed gate: 161
passed. This adds no wait or provider call and preserves timely PASS consumed later.
