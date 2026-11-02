# Radar 4663 Active-Pool Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore current Robinhood capture and prevent its database work from blocking Pump.

**Architecture:** Filter the stored Pons pool request with retained `op_launches` rows. Yield after each bounded persistence batch.

**Tech Stack:** Python 3, asyncio, SQLite, pytest.

## Global Constraints

- Preserve the `600`-block scan-lag limit.
- Preserve the 120-second capture window and 300-second candidate window.
- Preserve native event order and bounded transactions.
- Keep Pump, Bags, and Pons scoring separate.
- Do not change funded authority or V6.
- Do not run the full test suite.

---

### Task 1: Filter expired Pons pools

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_confirmation.py`

**Interfaces:**
- Consumes: `runtime.conn`, `VenueIdentityMap.pons_pool_tokens`, and `op_launches`.
- Produces: `_retained_pons_pool_tokens(runtime, identities) -> dict[str, tuple[str, str]]`.

- [x] **Step 1: Write the failing request-filter test**

Create two Pons pool identities with different tokens.
Insert one matching `op_launches` row into a real opportunity database.
Run `_evm_poll_range()` with a fake RPC boundary.
Assert the stored-pool `eth_getLogs` request contains only the retained pool.

- [x] **Step 2: Run the test and confirm the expected failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py::test_evm_poll_requests_only_retained_pons_pools -q
```

Expected: the request contains both pools.

- [x] **Step 3: Add the active-pool helper**

Query retained Pons token IDs from `op_launches`.
Return only identity-map entries whose token ID is retained.
Let SQLite errors escape so the poll retries the same range.

- [x] **Step 4: Use the filtered map**

Build the initial stored-pool request from the helper result.
Use the same filtered map for initial venue classification.
Keep new factory pools in the same-range request.

- [x] **Step 5: Run the focused test**

Run the command from Step 2.
Expected: one test passes.

### Task 2: Yield between persistence batches

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_confirmation.py`

**Interfaces:**
- Consumes: `_drain_evm_poll_batch(runtime, pending_items)`.
- Produces: one cooperative event-loop yield after each full batch.

- [x] **Step 1: Write the failing cooperative-yield test**

Use a queue limit of one and two accepted Pons logs.
Start a second coroutine that appends one marker after `asyncio.sleep(0)`.
Record the marker state at each drain.
Assert the second drain sees the marker.

- [x] **Step 2: Run the test and confirm the expected failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py::test_evm_poll_yields_between_persistence_batches -q
```

Expected: the second drain occurs before the marker.

- [x] **Step 3: Add the cooperative yield**

After each full batch, call `await asyncio.sleep(0)`.
Do not add a delay after the final partial batch.

- [x] **Step 4: Run the focused test**

Run the command from Step 2.
Expected: one test passes.

### Task 3: Verify and publish

**Files:**
- Modify: `docs/superpowers/plans/2026-08-07-radar-4663-active-pool-capture.md`

**Interfaces:**
- Consumes: the completed Tasks 1 and 2.
- Produces: a reviewed commit and a deployable PR.

- [x] **Step 1: Run the focused component tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_live_sources.py tests/test_memecoin_opportunity_engine.py -q
```

Expected: all selected tests pass.

- [x] **Step 2: Run static checks**

Run:

```bash
python3 scripts/validate_schema_refs.py --warn-unknown-tables
python3 -m py_compile scripts/memecoin_opportunity_worker.py
git diff --check
```

Expected: no schema violations, compile errors, or diff errors.

- [x] **Step 3: Review the complete diff**

Check every change against the design.
Reject any scoring, schema, V6, or funded change.

- [ ] **Step 4: Commit and create a PR**

Commit the tests and implementation as one bug fix.
Run the Knowledge Sync Gate.
Push the branch and create the PR against `main`.

- [ ] **Step 5: Merge and deploy**

Merge only after required checks pass.
Create a pinned opportunity release from the merge commit.
Restart only the opportunity worker.

- [ ] **Step 6: Verify live recovery**

Confirm one pinned worker, fresh source clocks, zero gaps, and bounded queues.
Sample the Robinhood frontier twice.
Require decreasing lag and final lag at most `600` blocks.
