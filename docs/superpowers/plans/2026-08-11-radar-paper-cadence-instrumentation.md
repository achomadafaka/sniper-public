# Radar Paper Sampler Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store each asynchronous v2 paper quote in its requested bucket and measure quote round-trip time.

**Architecture:** Each probe emits a bounded completion with the bucket-specific observation ID. The worker binds that completion to bounded request context and writes the exact bucket before the next lifecycle drive.

**Tech Stack:** Python 3, SQLite, `dataclasses`, `collections.OrderedDict`, `collections.deque`, and pytest.

<!-- schema:external=op_runtime_status -->

## Global Constraints

- Use TDD for each behavior change.
- Add no provider call, queue item, schema migration, arm, or post-outcome quote.
- Keep adaptive v3 disabled.
- Keep the v2 constants and current outcome rules unchanged.
- Keep each existing queue bound and priority unchanged.
- A telemetry error cannot stop or change the lifecycle.
- Run focused tests only.

---

### Task 1: Add bucket-specific probe completions

**Files:**

- Create: `sniper/research/memecoin_opportunity/paper_completion.py`
- Modify: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `sniper/research/memecoin_opportunity/evm_probe.py`
- Modify: `tests/test_memecoin_opportunity_solana_probe.py`
- Modify: `tests/test_memecoin_opportunity_evm_probe.py`

**Interfaces:**

- Produces: `PaperQuoteCompletion`
- Produces: `drain_paper_completions(limit: int = 32) -> tuple[PaperQuoteCompletion, ...]`

- [ ] Add a failing Solana test that submits `obs-5000` and `obs-10000` before either result is consumed.
- [ ] Confirm that both IDs enter the pending set or queue.
- [ ] Add a failing EVM test with the same two IDs.
- [ ] Add failing tests that require each completion to keep its observation ID and completion time.
- [ ] Run the two focused test files and confirm the expected failures.
- [ ] Add the immutable completion type and bounded completion queues.
- [ ] Keep `_paper_results` and the existing poll behavior unchanged.
- [ ] Run the two focused test files and confirm they pass.
- [ ] Commit the probe completion change.

---

### Task 2: Bind completion context and persist the requested bucket

**Files:**

- Create: `sniper/research/memecoin_opportunity/paper_sampler.py`
- Modify: `sniper/research/memecoin_opportunity/paper_lifecycle.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `tests/test_memecoin_opportunity_paper_lifecycle.py`
- Modify: `tests/test_memecoin_opportunity_worker_paper_adapter.py`

**Interfaces:**

- Produces: `PaperSampler.track(request: Mapping[str, object], requested_at: str) -> None`
- Produces: `PaperSampler.consume(completion: PaperQuoteCompletion) -> BoundPaperQuote | None`
- Produces: `append_bound_paper_completion(conn, completion: BoundPaperQuote) -> bool`

- [ ] Add a failing lifecycle test for a 5-second completion that arrives during the 10-second bucket.
- [ ] Assert that the stored `scheduled_age_ms` is 5,000.
- [ ] Assert that the late completion does not create an outcome.
- [ ] Add a failing test for a current-bucket completion and the existing outcome behavior.
- [ ] Add failing tests for a rejected completion, a duplicate completion, and an orphan completion.
- [ ] Run the two focused test files and confirm the expected failures.
- [ ] Track request context before each probe submission.
- [ ] Drain both probe completion queues before the v2 lifecycle drive.
- [ ] Store the completion under the requested observation ID and bucket.
- [ ] Keep late old-bucket completions outside the outcome decision path.
- [ ] Run the two focused test files and confirm they pass.
- [ ] Commit the completion binding change.

---

### Task 3: Add bounded round-trip and concurrency metrics

**Files:**

- Modify: `sniper/research/memecoin_opportunity/paper_sampler.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Create: `tests/test_radar_paper_sampler.py`
- Modify: `tests/test_memecoin_opportunity_worker_paper_adapter.py`

**Interfaces:**

- Produces: `PaperSampler.restore(conn, now_iso: str) -> PaperSampler`
- Produces: `PaperSampler.snapshot(now_iso: str) -> dict[str, object]`
- Produces: `opportunity:paper_sampler` with detail version `paper-sampler-v1`

- [ ] Add failing tests for the round-trip histogram and its sample count.
- [ ] Add a failing test for two concurrent buckets on one position.
- [ ] Add failing tests for the 1,024-item context bound and orphan count.
- [ ] Add failing tests for unfinished-window restore and completed-window preservation.
- [ ] Add a failing test for the one-minute status-write limit.
- [ ] Run the two focused test files and confirm the expected failures.
- [ ] Add fixed histograms and bounded counters.
- [ ] Add all-venue and per-venue snapshots.
- [ ] Persist one status row at most once each minute.
- [ ] Catch telemetry errors outside the quote and lifecycle result paths.
- [ ] Run the two focused test files and confirm they pass.
- [ ] Commit the sampler metrics change.

---

### Task 4: Verify the separate release candidate

**Files:**

- Modify: `AGENTS.md`
- Create: `docs/superpowers/evidence/2026-08-11-radar-paper-sampler-ksg.md`

**Interfaces:**

- Produces: one reviewed Radar release candidate.

- [ ] Run the probe, lifecycle, worker adapter, sampler, v3 gate, Pons, and settings tests.
- [ ] Run `python3 scripts/validate_schema_refs.py`.
- [ ] Run `python3 scripts/validate_knowledge_sync.py --mode local`.
- [ ] Run `python3 -m py_compile` on each changed Python file.
- [ ] Run `git diff --check`.
- [ ] Review the complete branch diff.
- [ ] Record that the branch adds no schema, provider call, arm, or funded path.
- [ ] Merge only after review approval.
- [ ] Deploy as a separate pinned Radar release.
- [ ] Verify one worker, zero adaptive v3 rows, v2 progress, Pons health, and sampler status.
