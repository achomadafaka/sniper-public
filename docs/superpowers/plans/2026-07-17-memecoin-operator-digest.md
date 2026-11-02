# Memecoin Operator Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the concurrent operator-digest work onto current `origin/main` without blocking or mutating funded execution.

**Architecture:** A DB-only collector and formatter run in a dedicated worker with its own connection. Delivery is submitted to the existing bounded asynchronous Telegram dispatcher. Corrected-clock SQL windows have explicit lower and upper bounds.

**Tech Stack:** Python 3.13, SQLite, pytest, existing memecoin runner and Telegram dispatcher.

## Global Constraints

- Never call Telegram HTTP, pricing, RPC, Fomo, LLM, browser, or dashboard code from the entry loop.
- Preserve coin identity as `(network_id, mint)` and keep all funded gates unchanged.
- Repository default is disabled; host activation is explicit.
- Use corrected real-UTC observations and reject future-clock rows.
- Fail soft without changing funded state.

---

### Task 1: Establish the digest contract with failing tests

**Files:**
- Create: `tests/test_memecoin_digest.py`
- Modify: `tests/test_memecoin_runner_loop_cadence.py`

**Interfaces:**
- Consumes: existing memecoin SQLite schema and `enqueue_telegram(text) -> bool`.
- Produces: test contracts for `collect_digest`, `format_digest`, `digest_pass`, and runner worker startup.

- [ ] Add the concurrent digest tests, plus future-row and asynchronous-wiring cases.
- [ ] Run `python3 -m pytest tests/test_memecoin_digest.py tests/test_memecoin_runner_loop_cadence.py -q`.
- [ ] Confirm failure is caused by the missing digest module and worker wiring.

### Task 2: Implement the DB-only digest

**Files:**
- Create: `sniper/services/memecoin_digest.py`
- Modify: `sniper/settings/memecoin.py`

**Interfaces:**
- `collect_digest(conn, *, now_epoch=None, period_s=3600.0) -> dict`
- `format_digest(data: dict) -> str`
- `digest_pass(conn, *, period_s=None, now_epoch=None, enqueue_fn=None) -> dict`

- [ ] Implement corrected `[since, now]` SQLite windows with no external requests.
- [ ] Make `digest_pass` submit to `enqueue_telegram` and catch every collection/queue exception.
- [ ] Add `MEMECOIN_TG_DIGEST_ENABLED` defaulting to `0` and `MEMECOIN_TG_DIGEST_PERIOD_S` defaulting to `3600`.
- [ ] Run the focused digest tests and confirm they pass.
- [ ] Commit as `feat(memecoin): add asynchronous operator digest`.

### Task 3: Wire a dedicated worker

**Files:**
- Modify: `scripts/memecoin_runner_loop.py`
- Test: `tests/test_memecoin_runner_loop_cadence.py`

**Interfaces:**
- `start_operator_digest_worker(*, enabled, period_s, thread_factory, worker_fn) -> bool`

- [ ] Add a test proving disabled startup creates no thread and enabled startup creates a daemon worker without calling the worker inline.
- [ ] Run the test and confirm it fails because the helper is missing.
- [ ] Implement the startup helper and a worker that sleeps one period, creates its SQLite connection in-thread, then calls `digest_pass` on fixed deadlines with contiguous corrected-UTC windows.
- [ ] Run focused tests and commit as `feat(memecoin): run operator digest off-thread`.

### Task 4: Verify, merge, deploy, and clean

**Files:**
- Modify: `AGENTS.md` only if the new spec must be registered by Knowledge Sync.

- [ ] Run `python3 scripts/validate_schema_refs.py`.
- [ ] Run `python3 -m pytest tests/test_memecoin*.py -q`.
- [ ] Run `python3 -m compileall -q sniper scripts`.
- [ ] Run `git diff --check`.
- [ ] Complete the Knowledge Sync Gate, push, open and merge a PR.
- [ ] Deploy the exact merge SHA with `scripts/deploy_memecoin_runtime.sh <sha> --restart`.
- [ ] Explicitly enable the host setting, then verify one pinned runner, fresh authenticated heartbeat, completed exit sweeps, and a queued digest pass.
- [ ] Remove the feature branch/worktree and the five stale files from the original checkout, then fast-forward local `main` to `origin/main`.
