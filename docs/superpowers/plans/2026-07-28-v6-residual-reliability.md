# V6 Residual Reliability Implementation Plan

**Goal:** Make Solana arrival recovery restart-safe and repair decision/feed reporting truth without
changing V6 policy behavior.

## Task 1: Durable arrival ledger

**Files:** `sniper/persistence/db_memecoin_v6_paper.py`,
`tests/test_memecoin_v6_paper_db.py`

1. Add failing schema/API tests for immutable enqueue, ordered due retries, success/no-op resolution,
   and retry persistence.
2. Add schema v9 table, migration, validation, and minimal ledger functions.
3. Backfill filled historical decisions in the v8-to-v9 migration.

## Task 2: Restart-safe Solana stream

**Files:** `sniper/services/memecoin_v6_solana_feed.py`,
`scripts/memecoin_v6_paper_worker.py`, `tests/test_memecoin_v6_solana_feed.py`,
`tests/test_memecoin_v6_paper_worker.py`

1. Add failing tests for persist-before-read, quiet-period retry, restart replay, and pool-local gaps.
2. Process durable arrivals in pool order with capped retry delay.
3. Resolve rows only after the fast runtime persists their event batch.
4. Make feed-health updates pool-specific.

## Task 3: Current feed-health reporting

**Files:** `sniper/services/memecoin_v6_paper_report.py`,
`tests/test_memecoin_v6_runtime_telemetry.py`, API/UI compatibility tests.

1. Add a failing active-versus-draining report test.
2. Add explicit groups while retaining the existing compatibility map.

## Task 4: Verification and live pin

1. Run focused RED/GREEN tests and the full `tests/test_memecoin_v6*.py` suite.
2. Run shared exit tests, schema validation, smoke, `git diff --check`, and report/API latency probes.
3. Commit exact files, back up the isolated V6 DB, deploy an immutable V6 release, restart V6 and web
   only if web code changed, and verify pins, process count, logs, authenticated health, funded guards,
   and live report latency.
