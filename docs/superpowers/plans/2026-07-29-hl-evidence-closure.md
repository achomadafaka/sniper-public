# Hyperliquid Evidence Closure Implementation Plan

> **Execution:** implement sequentially in this worktree with test-driven development. Commit each
> logical slice. Do not mutate production databases until the merged release is deployed.

**Goal:** Close the prospective Hyperliquid symbol, funding, episode, capacity, and source-latency
evidence gaps while preserving paper/funded separation and current runner cadence.

**Architecture:** Shared pure helpers define funding, episode, and capacity semantics. Existing
SQLite stores receive additive migrations. The copy runner owns a bounded WebSocket source-fill
collector but remains the only `hl_intel.sqlite` writer. Historical rerun tooling emits only sealed
`partial_censored` artifacts because delisted archives were declined.

**Tech stack:** Python 3.13, SQLite WAL, `websocket-client`, pytest, existing pinned-runtime deploy
scripts.

---

## Task 1: Seal a provider-exact partial historical cache

**Files:**

- Modify: `scripts/research/cache_hl_altshort_history.py`
- Create: `sniper/hyperlive/altshort_cache.py`
- Create/modify: `tests/test_hl_altshort_cache.py`
- Modify: `data/research/altshort_study/INVALIDATION.symbol-identity.json`

**TDD:**

1. Add failing tests for reuse of byte-identical uppercase symbols, forced refetch of lowercase-`k`
   symbols, per-file coverage/checksums, immutable prior artifacts, and
   `delisted_tail_status=censored_archive_declined`.
2. Run `python3 -m pytest tests/test_hl_altshort_cache.py -q` and confirm failure.
3. Add pure manifest/seal helpers and the smallest cache-runner changes.
4. Rerun focused tests, then `python3 scripts/validate_schema_refs.py` and `git diff --check`.
5. Commit `fix(hl): seal provider-exact partial history cache`.

The production-history run occurs only after merge, against a new state-root directory. It may
reuse verified unchanged files and fetch the seven exact-case symbols. It never overwrites sealed
v2/v3 outputs.

## Task 2: Canonicalize funding contribution and interval

**Files:**

- Create: `sniper/hyperlive/funding.py`
- Modify: `sniper/hyperlive/copy_capture.py`
- Modify: `sniper/hyperlive/intel_store.py`
- Modify: `scripts/run_hl_copy.py`
- Modify: `scripts/run_hl_altshort_paper.py`
- Modify: `sniper/hyperlive/copy_scorecard.py`
- Modify: `sniper/hyperlive/edge_desk.py`
- Modify: `tests/test_hyperlive_copy_capture_pure.py`
- Modify: `tests/test_hl_intel_store.py`
- Modify: `tests/test_hl_altshort_paper.py`
- Modify: `tests/test_hyperlive_copy_scorecard.py`
- Modify: `tests/test_api_hyperlive_edge_desk.py`

**TDD:**

1. Add failing pure tests for `(entry, exit]`, long/short `funding_pnl_bps`, zero-boundary
   `not_applicable`, missing expected prints, fetch failure, and exact-exit ambiguity.
2. Add failing production-shaped migration and dual-write tests for copy cost-sign legacy and paper
   contribution-sign legacy.
3. Run focused tests and confirm the intended failures.
4. Implement the pure result type/helper, additive columns, canonical-first readers, dual writes,
   and provenance counts.
5. Preserve `NULL` for incomplete history and exclude legacy mismatches from scorecards.
6. Rerun focused tests and commit `fix(hl): unify funding provenance and sign`.

## Task 3: Give nominations a trough-episode identity

**Files:**

- Modify: `scripts/run_hl_altshort_paper.py`
- Modify: `tests/test_hl_altshort_paper.py`
- Modify: `sniper/hyperlive/edge_desk.py`
- Modify: `tests/test_api_hyperlive_edge_desk.py`

**TDD:**

1. Add a BOME-shaped failing test with four upward crossings sharing one trough; assert one primary
   and three persisted `repeat_same_episode` observations.
2. Add a failing test where the causal trough changes and a new primary episode is allowed.
3. Confirm failures with `python3 -m pytest tests/test_hl_altshort_paper.py -q`.
4. Add trough selection/episode identity pure helpers, additive observation columns, immutable arm
   semantics, and Edge Desk counts.
5. Rerun focused tests and commit `fix(hl): dedupe nominations by causal trough`.

## Task 4: Remove the artificial 15-level capacity ceiling

**Files:**

- Modify: `sniper/hyperlive/fills.py`
- Modify: `sniper/hyperlive/copy_capture.py`
- Modify: `sniper/hyperlive/copy_scorecard.py`
- Modify: `scripts/hl_shadow_real.py`
- Modify: `tests/test_hyperlive_fills.py`
- Modify: `tests/test_hyperlive_copy_scorecard.py`

**TDD:**

1. Add failing tests for storing all 20 returned levels, a 100-level defensive cap, returned/stored
   metadata, legacy 15-level classification, and structured measured/lower-bound/censored states.
2. Confirm failures with the fill and scorecard test files.
3. Implement metadata-preserving compact capture and a structured capacity result. Keep a numeric
   compatibility wrapper only where existing callers require it.
4. Ensure censored depth never becomes a zero-capacity claim and no new API request is added.
5. Rerun focused tests and commit `fix(hl): expose capacity depth censoring`.

## Task 5: Capture prospective source-fill latency

**Files:**

- Create: `sniper/hyperlive/source_fills.py`
- Modify: `sniper/markets/hyperliquid_api.py`
- Modify: `sniper/hyperlive/intel_store.py`
- Modify: `scripts/run_hl_copy.py`
- Modify: `sniper/hyperlive/edge_desk.py`
- Create: `tests/test_hl_source_fills.py`
- Modify: `tests/test_hyperliquid_api_helpers.py`
- Modify: `tests/test_hl_intel_store.py`
- Modify: `tests/test_run_hl_copy.py`
- Modify: `tests/test_api_hyperlive_edge_desk.py`

**TDD:**

1. Add failing tests for WebSocket subscription messages, initial-snapshot baseline, `(wallet,tid)`
   dedup, bounded queue overflow, reconnect backoff state, and REST recovery payloads.
2. Add failing matching tests for exact causal match, multiple-fill aggregation, ambiguous direction,
   unmatched events, provider-exact coin identity, and historical-not-captured rows.
3. Add failing runner tests proving subscriber failure is fail-open and periodic REST call count does
   not increase.
4. Implement the bounded collector, normalization/matching helpers, API recovery function, append-
   only event table, timing columns, queue drain, and heartbeat health.
5. Keep the main runner as the only SQLite writer and restrict instrumentation to the arm manifest's
   nine wallets.
6. Rerun focused tests and commit `feat(hl): capture source-fill latency provenance`.

## Task 6: Integrated API, migration, and performance verification

**Files:**

- Modify as required by failing integration tests only.

**Verification:**

```bash
python3 -m pytest \
  tests/test_hl_altshort_cache.py \
  tests/test_hyperlive_copy_capture_pure.py \
  tests/test_hl_intel_store.py \
  tests/test_hl_altshort_paper.py \
  tests/test_hyperlive_fills.py \
  tests/test_hyperlive_copy_scorecard.py \
  tests/test_hl_source_fills.py \
  tests/test_hyperliquid_api_helpers.py \
  tests/test_run_hl_copy.py \
  tests/test_api_hyperlive_edge_desk.py -q
python3 scripts/validate_schema_refs.py
python3 scripts/validate_schema_refs.py --warn-unknown-tables
python3 -m compileall -q sniper/hyperlive sniper/markets scripts/run_hl_copy.py \
  scripts/run_hl_altshort_paper.py scripts/research/cache_hl_altshort_history.py
git diff --check
```

Run production-shaped temporary-DB migrations from the current live schemas. Benchmark 10,000
20-level capacity calculations and source-event queue drain; reject material regression to the
30-second copy cycle. Check that test instrumentation makes zero network calls.

Commit any integration-only repair separately.

## Task 7: Knowledge Sync, PR, merge, deploy, and live proof

1. Run the 11-question Knowledge Delta Report. Update `AGENTS.md` authoritative docs if the new spec
   is load-bearing. Record the required Supabase architecture/semantic-fix memory only through the
   approved KSG path; if unavailable, disclose it in the PR block rather than fabricating success.
2. Build a PR body ending with the mandatory Knowledge Sync block.
3. Push the feature branch, open the PR, inspect all checks, merge only when green, and capture the
   exact merged SHA.
4. Back up `hl_intel.sqlite` and `hl_altshort_paper.db`; require `PRAGMA integrity_check=ok`.
5. Deploy the merged SHA through the existing Hyperliquid and web pinned-runtime scripts. Preserve
   automatic rollback.
6. Verify:
   - both runtime symlinks and manifests equal the merged SHA;
   - exactly one copy runner, squeeze-tape runner, alt-short paper runner, and web server;
   - zero `hl_real` processes;
   - fresh authenticated health/Edge Desk responses;
   - source subscriber connected with nine baselined wallets or an explicit provider error;
   - additive schemas and dual-write invariants;
   - database integrity and fresh backups;
   - no recurring 429s, lock errors, queue overflow, or heartbeat regression;
   - exact-case partial cache seal and seven affected symbols, without overwriting prior artifacts.
7. Roll back to the prior pinned SHA on process, schema-reader, rate-limit, or authenticated endpoint
   failure. Append-only rows/additive columns remain compatible.
