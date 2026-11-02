# Radar Runtime Reliability Implementation Plan

> **Required skill:** Use `superpowers:executing-plans` for each task.

**Goal:** Restore current Radar capture, direct quote health, honest API state, and immutable worker paths.

**Architecture:** Keep each source fault local. Publish bounded status rows from direct work. Start all Radar workers from one merged release.

**Tech stack:** Python 3, asyncio, SQLite, Bash, Flask, pytest.

## Constraints

- Keep Radar paper-only.
- Do not change a rule, threshold, result, V6 policy, or funded authority.
- Do not restart the funded runner.
- Use one logical commit for each repair group.
- Use a failing test before each code change.

## Task 1: Remove the 4663 shared-loop stall

**Files:**

- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_confirmation.py`
- Test: `tests/test_memecoin_opportunity_runtime.py`

1. Add a test that gives the poll one retained and one expired Pons pool.
2. Confirm that the test fails because the request contains both pools.
3. Filter the stored Pons pool request with retained `op_launches` token rows.
4. Add a test that starts another coroutine between two persistence batches.
5. Confirm that the second batch runs before the other coroutine.
6. Add `await asyncio.sleep(0)` after each full persistence batch.
7. Add a test for the stored untracked Bags curve gap.
8. Confirm that the known irrelevant row keeps the gap open.
9. Resolve only an untracked Bags curve log from the immutable raw row.
10. Preserve an unknown tracked contract version as an open gap.
11. Run the confirmation, runtime, live-source, and engine tests.
12. Commit with `fix(radar): isolate the shared 4663 scan`.

## Task 2: Publish direct quote-path health

**Files:**

- Modify: `sniper/research/memecoin_opportunity/evm_probe.py`
- Modify: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `scripts/radar_alert_lane_watchdog.sh`
- Test: `tests/test_memecoin_opportunity_evm_probe.py`
- Test: `tests/test_memecoin_opportunity_solana_probe.py`
- Test: `tests/test_memecoin_opportunity_watchdog.py`

1. Add probe tests for the last request, success, failure, and per-venue counters.
2. Confirm that the current metrics omit these fields.
3. Record each field under the existing probe lock.
4. Keep each metrics result bounded by the fixed venue set.
5. Add worker tests for healthy, degraded, and idle quote-path rows.
6. Confirm that the EVM row reports `healthy` after all failures.
7. Derive each quote-path state from direct probe work.
8. Add watchdog tests that reject alert-count SQL.
9. Confirm that the watchdog still reads `op_insight_alerts`.
10. Make the watchdog read direct quote-path status rows.
11. Keep the confirmed-delivery latch and recovery behavior.
12. Run the probe, worker, and watchdog tests.
13. Commit with `fix(radar): report direct quote path health`.

## Task 3: Add bounded runtime metrics and honest API states

**Files:**

- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_worker_paper_adapter.py`
- Test: `tests/test_memecoin_opportunity_api.py`
- Test: `tests/test_memecoin_opportunity_sources.py`

1. Add tests for fixed-size timing samples and over-limit counts.
2. Confirm that no runtime timing type exists.
3. Add one fixed-key timing accumulator.
4. Measure the main loop, source lanes, quote drains, and paper lifecycle.
5. Publish one `opportunity:runtime` row at a bounded cadence.
6. Add API tests for `healthy`, `degraded`, and `stale`.
7. Confirm that the API returns `live` for each active case.
8. Grade partial source failure as `degraded`.
9. Grade total source failure or a stale worker as `stale`.
10. List all failed required paths in `reason` and `stale_sources`.
11. Keep `disabled` and `unavailable` unchanged.
12. Run the worker, source, projection, and API tests.
13. Commit with `fix(radar): expose bounded runtime health`.

## Task 4: Pin the Coin Radar worker

**Files:**

- Modify: `scripts/memecoin_radar_watchdog.sh`
- Modify: `tests/test_memecoin_discovery_external.py`
- Modify: `AGENTS.md`

1. Add a test for code and state path separation.
2. Add a test for one exact release worker path.
3. Confirm that the watchdog uses the mutable checkout path.
4. Derive `CODE_ROOT` from the watchdog path.
5. Read shared state from `SNIPER_STATE_ROOT`.
6. Match and start the worker from `CODE_ROOT`.
7. Keep logs and the main database under the state root.
8. Add the content-addressed Coin Radar rule to `AGENTS.md`.
9. Run the discovery and watchdog tests.
10. Commit with `fix(radar): pin the Coin Radar worker`.

## Task 5: Verify and integrate

**Files:**

- Inspect: `docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md`
- Inspect: `.github/pull_request_template.md`
- Runtime: `.runtime/opportunity`, `.runtime/web`, and cron

1. Run all focused Radar tests.
2. Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`.
3. Run Python compile checks for each changed Python file.
4. Run `bash -n` for each changed shell file.
5. Run `git diff --check` and inspect the full branch diff.
6. Run the Knowledge Sync Gate.
7. Push the branch and open a PR with the required Knowledge Sync block.
8. Merge the PR after all required checks pass.
9. Create opportunity and web release worktrees from the merge commit.
10. Back up `.env` before a source endpoint change.
11. Set Pump recovery to the verified official Solana endpoint.
12. Switch the opportunity and web `current` links atomically.
13. Change cron to the release-pinned Coin Radar watchdog.
14. Restart the opportunity, Coin Radar, and web processes.
15. Do not restart the funded runner.
16. Verify one process for each changed runtime.
17. Verify HTTP 200, direct quote health, source health, zero gaps, and bounded queues.
18. Verify CPU, memory, frontier lag, and logs for 30 minutes.

## Task 6: Remove the SQLite quantity limit

**Files:**

- Modify: `sniper/research/memecoin_opportunity/store.py`
- Modify: `sniper/research/memecoin_opportunity/paper_adapter.py`
- Modify: `sniper/research/memecoin_opportunity/paper_lifecycle.py`
- Test: `tests/test_memecoin_opportunity_store.py`
- Test: `tests/test_memecoin_opportunity_paper_adapter.py`
- Test: `tests/test_memecoin_opportunity_paper_lifecycle.py`

1. Add a request test with a token quantity above 64 bits.
2. Confirm that SQLite rejects the current integer bind.
3. Store all exact paper quantities as decimal text.
4. Preserve the semantic fingerprint input for old request retries.
5. Add schema version 13.
6. Migrate the version 12 quantity columns to `TEXT`.
7. Preserve all immutable rows and triggers during the migration.
8. Add an exit test with a token quantity above 64 bits.
9. Run all focused Radar tests.
10. Deploy the merged release.
11. Repeat the 30-minute runtime proof.
