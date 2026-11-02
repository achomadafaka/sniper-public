# Radar-Owned Realistic Paper Lifecycle Implementation Plan

**Goal:** Measure the V5 Radar rule with one Radar-owned realistic paper lifecycle.

**Architecture:** The opportunity database stores immutable activation, sell observations, and outcomes. The opportunity worker uses its paced quote lane. V6 does not consume Radar receipts.

## Task 1: Add the immutable lifecycle ledger

**Files:**

- Modify: `sniper/research/memecoin_opportunity/store.py`
- Create: `sniper/research/memecoin_opportunity/paper_lifecycle.py`
- Create: `tests/test_memecoin_opportunity_paper_lifecycle.py`

1. Add failing schema, immutability, activation, quote, outcome, and summary tests.
2. Confirm the focused tests fail.
3. Add schema version 12 and the three immutable tables.
4. Add deterministic identities and append-only lifecycle functions.
5. Run the focused tests.

## Task 2: Add exact sell quotes

**Files:**

- Modify: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `tests/test_memecoin_opportunity_solana_probe.py`

1. Add a failing exact-input token-to-quote test.
2. Confirm the test fails.
3. Reuse the paced paper queue for sell quotes.
4. Keep entry quote behavior unchanged.
5. Run the focused tests.

## Task 3: Drive the lifecycle in the opportunity worker

**Files:**

- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `tests/test_memecoin_opportunity_worker_paper_adapter.py`

1. Add failing worker tests for activation and lifecycle work.
2. Confirm the tests fail.
3. Start the forward cohort before new entries.
4. Drive due sell quotes and publish lifecycle health.
5. Run the focused tests.

## Task 4: Publish and render realistic results

**Files:**

- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Modify: related projection, API, and UI tests

1. Add failing API and UI contract tests.
2. Confirm the tests fail.
3. Publish the realistic lifecycle summary.
4. Render only that summary in the simple Radar results panel.
5. Keep the existing global source-scope rule.
6. Run the focused tests.

## Task 5: Remove the V6 Radar consumer

**Files:**

- Modify: `scripts/memecoin_v6_paper_worker.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Modify: `sniper/settings/memecoin_v6_paper.py`
- Remove: `sniper/services/memecoin_v6_radar_signals.py`
- Replace or remove the V6 Radar consumer tests

1. Add a failing static isolation test.
2. Confirm the test fails.
3. Remove all Radar imports, cursors, calls, and settings from V6.
4. Keep historical database rows unchanged.
5. Run the focused V6 tests.

## Task 6: Verify, publish, and deploy

1. Run the focused opportunity, API, UI, and V6 tests.
2. Run the schema reference check.
3. Run the Knowledge Sync Gate.
4. Commit each logical change.
5. Push the branch and open a pull request.
6. Merge after all checks pass.
7. Deploy the exact merged release to opportunity, web, and disabled V6 runtime roots.
8. Keep `MEMECOIN_V6_PAPER_ENABLED=0`.
9. Set `MEMECOIN_V6_RADAR_CONSUMER_ENABLED=0` if the old key remains in `.env`.
10. Verify process paths, heartbeats, the API, the new activation, and logs.
11. Verify that the funded runtime path and process did not change.

