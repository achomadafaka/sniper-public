# Radar Pons Poll Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise dense Robinhood 4663 poll persistence throughput so Pons can catch the live chain and clear the truthful `CAPTURE PARTIAL` state.

**Architecture:** `_evm_poll_range()` retains native event ordering but submits and drains bounded batches. Each drain runs inside `store.transaction(runtime.conn)`, collapsing nested writes into one commit while preserving the existing runtime failure semantics.

**Tech Stack:** Python 3, asyncio, SQLite, pytest.

## Global Constraints

- Preserve the Pons five-minute event expectation and the UI stale grade.
- Preserve bounded queue overflow, source-gap, and decode-failure behavior.
- Do not change schemas, retention, strategy decisions, paper engines, V6, or funded execution.
- Do not mutate the production database, call providers, deploy, or restart services during implementation.

---

### Task 1: Batch dense EVM poll persistence

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_confirmation.py`

**Interfaces:**
- Consumes: `OpportunityCaptureRuntime.submit(source_key, envelope)`, `OpportunityCaptureRuntime.drain(max_items=...)`, `store.transaction(conn)`.
- Produces: bounded ordered drain behavior inside `_evm_poll_range()`.

- [x] **Step 1: Write the failing dense-range test**

Extend the native-order poll test runtime with `conn`, `settings.queue_max = 8`,
pending queue accounting, recorded drain sizes, transaction-state assertions, and
a patched `_source_heartbeat` that fails if called. Assert two ordered events are
drained as one batch of two and the drain observes `conn.in_transaction is True`.

- [x] **Step 2: Run the focused test and verify failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py::test_evm_http_poll_range_preserves_native_log_order -q
```

Expected: failure because current drains are per-event outside an outer transaction
and call `_source_heartbeat()` directly.

- [x] **Step 3: Implement bounded transactional drains**

Add `_EVM_PERSIST_BATCH_SIZE = 256`. In `_evm_poll_range()`, calculate the
effective batch limit as `min(256, runtime.settings.queue_max)` when settings are
available, otherwise 256. Submit in native order, drain whenever the pending count
reaches the limit, and drain the final partial batch. Wrap each drain with
`transaction(runtime.conn)`. Remove the direct per-event `_source_heartbeat()` call;
retain `on_progress()`.

- [x] **Step 4: Cover the default 256-event cap**

Add a lightweight unit test for the batch-limit helper or a production-shaped dense
range with capacity above 256. Assert the effective limit is 256, capacities below
256 are honored, and invalid/missing lightweight test seams default safely.

- [x] **Step 5: Run focused and component tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py -q
python3 -m pytest tests/test_memecoin_opportunity_runtime.py -q
```

Expected: all pass.

- [x] **Step 6: Run static project validation**

Run:

```bash
python3 scripts/validate_schema_refs.py
git diff --check
```

Expected: schema references clean and no whitespace errors.

- [x] **Step 7: Commit the implementation**

```bash
git add scripts/memecoin_opportunity_worker.py tests/test_memecoin_opportunity_confirmation.py
git commit -m "fix(radar): batch dense EVM poll persistence"
```

### Task 2: Keep transient health blockers recoverable across restart

**Files:**
- Modify: `sniper/services/memecoin_opportunity_runtime.py`
- Test: `tests/test_memecoin_opportunity_runtime.py`

**Interfaces:**
- Consumes: `_GAP_BLOCKERS`, persisted `op_runtime_status`, and durable source frontiers.
- Produces: restart initialization that restores only integrity blockers as in-memory gaps.

- [x] **Step 1: Reproduce the restart trap**

Persist `BAGS_SUBSCRIPTIONS_EMPTY`, construct a new runtime, send a healthy transport
heartbeat, and assert the source is not gapped and the transient blocker clears.

- [x] **Step 2: Preserve the integrity case**

Persist `UNKNOWN_PROGRAM_OR_CONTRACT_VERSION`, construct a new runtime, and assert the
source remains gapped.

- [x] **Step 3: Filter restored status blockers**

Initialize `gapped_sources` from status rows only when `blocker_code` belongs to
`_GAP_BLOCKERS`; retain the existing union with every open durable frontier gap.

- [x] **Step 4: Verify**

Run the focused red/green tests, all opportunity tests, schema validation, and
`git diff --check`.
