# Memecoin Entry Radar and Coin-State Evidence Implementation Plan

> Execute with TDD in the isolated `feat/memecoin-entry-radar-coin-state` worktree. Keep every funded decision observationally identical.

**Goal:** Persist exact, immutable entry-quality facts and fail-closed holder evidence without adding network work or funded authority.

**Architecture:** Extend the existing episode/snapshot/entry-attempt spine. Add a pure radar scorer and a holder-evidence ledger. Record protected-hold decisions only as exit-trigger audit evidence.

**Tech:** Python 3, SQLite, pytest.

---

### Task 1: Add episode membership and deterministic boundaries

**Files:**

- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_episode.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `tests/test_memecoin_episode_resolver.py`
- Modify: `tests/test_memecoin_copy_snapshot_hook.py`
- Modify: `tests/test_memecoin_observation_schema.py`

1. Add failing tests for exact-chain separation, duplicate-event idempotency, source/executable anchors, and a new episode after a greater-than-60-minute gap.
2. Run `python3 -m pytest tests/test_memecoin_episode_resolver.py tests/test_memecoin_copy_snapshot_hook.py tests/test_memecoin_observation_schema.py -q` and confirm failure.
3. Add additive episode columns and `memecoin_episode_signals` with exact event identity.
4. Make `resolve_episode()` transactional and idempotent; preserve the existing tuple return contract.
5. Resolve episodes for every non-base-asset buy snapshot, including rejected/chase outcomes, and persist episode runup facts in `features_json`.
6. Rerun the focused tests and commit the logical change.

### Task 2: Add immutable radar ratings

**Files:**

- Create: `sniper/services/memecoin_entry_radar.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Create: `tests/test_memecoin_entry_radar.py`
- Modify: `tests/test_memecoin_real_runner.py`

1. Add failing pure-scoring tests for red existing refusals, yellow missing/adverse facts, consensus non-upgrade, source-lane preservation, and green complete facts.
2. Add failing persistence tests for exact attempt/event joins, one immutable row, and fact-hash collision refusal.
3. Run the new tests and confirm failure.
4. Add `memecoin_radar_ratings` and indexes.
5. Implement canonical fact collection and pure versioned rating without network I/O.
6. Finalize the rating best-effort after every real attempt terminal result, including exception results; never alter the returned funded result.
7. Rerun focused entry trace and real-runner tests and commit.

### Task 3: Add holder evidence reconciliation

**Files:**

- Create: `sniper/services/memecoin_holder_evidence.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `sniper/services/memecoin_follow_activity.py`
- Modify: `sniper/settings/memecoin.py`
- Create: `tests/test_memecoin_holder_evidence.py`
- Modify: `tests/test_memecoin_follow_activity.py`

1. Add failing tests for explicit positive/zero, missing-amount unknown, sell invalidation, exact-chain separation, stale evidence refusal, and successful-poll stamps.
2. Run the focused tests and confirm failure.
3. Add `memecoin_holder_evidence` with exact-coin primary key and a bounded evidence-age setting.
4. Reconcile already-retained global-feed observations after bulk retention.
5. Reconcile existing follow-activity responses locally; any sell invalidates positive evidence. Do not add a request.
6. Rerun focused runner/follow tests and commit.

### Task 4: Log protected-hold shadow decisions

**Files:**

- Modify: `sniper/services/memecoin_real_runner.py`
- Create: `tests/test_memecoin_protected_hold_shadow.py`

1. Add a failing funded-sweep test: fresh other-armed-holder evidence produces an audit row, but `exit_fn` is still called for `b_stop`.
2. Add failing unknown/stale tests that record `would_protect=false`.
3. Run the focused test and confirm failure.
4. Add best-effort shadow evaluation after executable stop confirmation and before the unchanged exit call.
5. Rerun real exit, executable guard, mirror consensus, and concurrency tests; commit.

### Task 5: Review and validate

1. Inspect the complete branch diff for identity fallback, duplicate writes, transaction boundaries, clock mixing, accidental authority, and added network calls.
2. Run `python3 scripts/validate_schema_refs.py`.
3. Run all `tests/test_memecoin*.py`.
4. Run relevant chain-identity concurrency and runtime deploy/verify tests.
5. Run `python3 -m compileall -q sniper scripts` and `git diff --check`.
6. Correct defects with focused failing tests and separate commits.
