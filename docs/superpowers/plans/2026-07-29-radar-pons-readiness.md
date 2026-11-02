# Radar Pons Readiness Implementation Plan

**Goal:** Add bounded Pons V1 evidence capture, honest freshness, and forward-only non-fire labels,
then deploy the exact reviewed release without changing funded execution.

## 1. Pons decoder and identities

- Add RED decoder/source tests for exact launch and registered-pool Swap events.
- Add `pons_v1_4663`, `active_pool`, pure ABI decoding, and restart-restorable pool identity.
- Prove unknown factories/pools fail closed and Pons never enters a Radar arm.

## 2. Bounded 4663 capture

- Add RED worker tests for one bounded batch, newly launched pool catch-up, event ordering, and call
  bounds.
- Generalize the existing 4663 lane without renaming Bags evidence.
- Keep one governor, one lane, bounded chunks, and no per-pool tasks.

## 3. Typed event freshness

- Add RED projection/UI tests for heartbeat age, event age, and optional expected-event staleness.
- Expose event silence separately; only declared high-cadence sources affect capture health.

## 4. Forward-only sample labels

- Add RED insight migration and projection tests for non-fired selected samples.
- Migrate insight schema to v8 and stamp `selected-outcome-v1`/`selected-outcome-v2`.
- Link only exact existing executable entries and outcomes; leave missing evidence unknown.

## 5. Collection and regression

- Add an import-safe CLI test for `screen_rising_mechanics.py`, reproduce collection failure, then
  move argument parsing into `main()`.
- Run explicit focused Radar test files, schema checks, JS syntax, import boundaries, and
  `git diff --check`. Do not run a full suite.

## 6. Review, integration, and live proof

- Self-review the diff for RPC amplification, unbounded state, funded imports, evidence relabeling,
  and unrelated changes.
- Complete the Knowledge Sync report, push a feature branch, open and merge a reviewed PR.
- Deploy the merged SHA with the repository's pinned Radar deployment path and backups.
- Verify release symlink/manifest, one worker, heartbeat/event ages, Pons launch progress, RPC queue
  and backoff, storage, authenticated API, logs, and unchanged funded services.
