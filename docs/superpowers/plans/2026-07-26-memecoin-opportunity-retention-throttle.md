# Memecoin Opportunity Retention and Throttle Implementation Plan

> **For agentic workers:** Execute this plan inline with focused RED/GREEN checks. Do not run the full suite or restart funded/web services.

**Goal:** Keep the opportunity Radar continuously useful without unbounded disk growth or public/provider RPC throttling.

**Architecture:** Preserve a short immutable hot ledger, project matured launch samples into a compact long-lived insight database, and delete hot rows only through a connection-local retention authority. One adaptive asynchronous governor owns worker RPC concurrency and pacing. Storage and RPC state are surfaced through the existing fail-closed health model.

**Tech Stack:** Python 3, SQLite/WAL, asyncio, Flask route JSON, vanilla JavaScript UI, pytest.

---

### Task 1: Add bounded settings

**Files:**
- Modify: `sniper/settings/memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_settings.py`

1. Add a failing settings test for the insight path, 30-minute/7-day retention, 400/750 MiB hot caps, 60-second cleanup, two concurrent RPC calls, and the measured 2 rps ceiling.
2. Run `python3 -m pytest tests/test_memecoin_opportunity_settings.py -q` and confirm RED.
3. Add typed settings fields, environment overrides, and strict positive/ordering validation.
4. Rerun the focused test and commit.

### Task 2: Add the compact insight store and idempotent projector

**Files:**
- Create: `sniper/research/memecoin_opportunity/insights.py`
- Create: `tests/test_memecoin_opportunity_insights.py`

1. Add failing tests proving one deterministic sample per launch/role, compressed feature payloads, idempotent re-projection, seven-day sample pruning, and durable daily aggregate preservation.
2. Run the new test and confirm RED.
3. Implement schema v1, deterministic sample identities, atomic inserts, aggregate rebuilds, and bounded pruning.
4. Rerun the new test and commit.

### Task 3: Make hot-ledger deletion authorized and episode-safe

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Create: `sniper/research/memecoin_opportunity/retention.py`
- Create: `tests/test_memecoin_opportunity_retention.py`

1. Add failing tests proving ordinary deletes remain blocked, authority is connection-local and restored after errors, unresolved/tentative/gapped launches remain, projected terminal launches delete in dependency order, and unreferenced raw rows prune.
2. Run the new test and confirm RED.
3. Bump the hot schema, migrate immutable delete triggers, install the connection-local SQL guard, enable WAL auto-checkpoint plus incremental auto-vacuum on fresh databases, and implement bounded retention batches.
4. Add soft/hard cap state with emergency retention and fail-closed `STORAGE_CAP_REACHED` when no safe cleanup remains.
5. Rerun the focused store/retention tests and commit.

### Task 4: Coalesce runtime writes and govern RPC reads

**Files:**
- Modify: `sniper/services/memecoin_opportunity_runtime.py`
- Create: `sniper/services/memecoin_opportunity_rpc.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_runtime.py`
- Create: `tests/test_memecoin_opportunity_rpc.py`

1. Add failing tests proving one status write per source per drain batch, priority ordering, max concurrency two, adaptive 429 backoff, and gradual recovery.
2. Run both focused tests and confirm RED.
3. Coalesce runtime status writes after processing a batch.
4. Route worker Solana and EVM read-RPC calls through the shared governor; publish compact governor metrics.
5. Run retention only every configured interval and only below 25% queue occupancy. Pause source ingestion on the hard-cap blocker while leaving health/maintenance alive.
6. Rerun the focused tests and commit.

### Task 5: Expose storage and throttle health

**Files:**
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Test: `tests/test_api_memecoin_opportunity.py`

1. Add a failing API test for the `storage` object and RPC metrics.
2. Run the API test and confirm RED.
3. Read compact runtime health into the snapshot and render one small storage/RPC chip without increasing poll cadence.
4. Rerun the API test and run `node --check static/app.js`; commit.

### Task 6: Activation and focused runtime proof

**Files:**
- Create: `scripts/prepare_memecoin_opportunity_retention.py`
- Test: `tests/test_prepare_memecoin_opportunity_retention.py`

1. Add a failing test for project-first, explicit fresh-hot activation semantics.
2. Implement a CLI that projects eligible existing evidence, checkpoints/closes it, and removes only the explicitly supplied hot database plus WAL/SHM when `--fresh-hot` is present.
3. Run all opportunity-focused tests only, `git diff --check`, schema reference validation, and JavaScript syntax check.
4. Merge the scoped commits to local `main` without touching unrelated work.
5. Stop only `memecoin_opportunity_worker.py`, run the activation CLI with `--fresh-hot`, restart it from local `main`, and verify process freshness, API storage/RPC fields, healthy source statuses, bounded file growth, cleanup metrics, and no open gaps.
