# WAL Runtime Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop routine maintenance and long-lived tick observers from pinning or blocking the main SQLite WAL.

**Architecture:** Keep routine cron maintenance PASSIVE-only so it never waits on or blocks live writers. Preserve RESTART/TRUNCATE as an explicit operator reclaim mode. Use the existing read-only autocommit connection for funded-runner tick observers so live tick capture stays enabled without holding a main-ledger snapshot.

**Tech Stack:** Python 3.13, SQLite WAL, pytest, pinned memecoin runtime deployment.

## Global Constraints

- Do not change funded entry, exit, sizing, signing, approval, broadcast, or reservation behavior.
- Do not disable the standalone Solana tape or V6 paper worker.
- Default cron invocation must issue only `PRAGMA wal_checkpoint(PASSIVE)`.
- Destructive WAL reclaim requires an explicit CLI flag and remains operator-only.
- Deploy funded code only from a merged SHA reachable from `origin/main` through `scripts/deploy_memecoin_runtime.sh`.
- Run focused tests only; do not run full suites.

---

### Task 1: Make scheduled WAL maintenance non-blocking

**Files:**
- Modify: `scripts/wal_checkpoint.py`
- Create: `tests/test_wal_checkpoint_modes.py`

**Interfaces:**
- Consumes: `checkpoint_db(db_path: Path, *, reclaim: bool = False) -> int`
- Produces: `main(argv: list[str] | None = None) -> int`; default PASSIVE-only behavior and explicit `--reclaim` escalation.

- [ ] **Step 1: Write the failing test**

```python
def test_default_checkpoint_never_uses_blocking_modes(tmp_path, monkeypatch):
    modes = []
    monkeypatch.setattr(wal_checkpoint, "_checkpoint", lambda conn, mode, timeout=30: (modes.append(mode) or (0, 1, 1)))
    assert wal_checkpoint.checkpoint_db(_seed_wal(tmp_path), reclaim=False) == 0
    assert modes == ["PASSIVE"]


def test_reclaim_mode_may_escalate(tmp_path, monkeypatch):
    modes = []
    monkeypatch.setattr(wal_checkpoint, "_wal_mb", lambda path: 200.0)
    monkeypatch.setattr(wal_checkpoint, "_checkpoint", lambda conn, mode, timeout=30: (modes.append(mode) or (0, 1, 1)))
    assert wal_checkpoint.checkpoint_db(_seed_wal(tmp_path), reclaim=True) == 2
    assert modes[:2] == ["PASSIVE", "RESTART"]
    assert "TRUNCATE" in modes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wal_checkpoint_modes.py -q -n 0`

Expected: FAIL because `checkpoint_db` has no `reclaim` argument and default behavior escalates.

- [ ] **Step 3: Write minimal implementation**

Add an `argparse` `--reclaim` flag. Return after PASSIVE in normal mode; run the existing RESTART/TRUNCATE sequence only when `reclaim=True`. Update module documentation and log output to make the mode explicit.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_wal_checkpoint_modes.py tests/test_wal_pinning.py tests/test_memecoin_sol_tape_own_db.py tests/test_exit_materializer_wiring.py -q -n 0`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/wal_checkpoint.py tests/test_wal_checkpoint_modes.py
git commit -m "fix: keep wal cron nonblocking"
```

### Task 2: Package the funded-runner observer fix

**Files:**
- Verify: `scripts/memecoin_runner_loop.py`
- Verify: `tests/test_memecoin_runner_loop_cadence.py`

**Interfaces:**
- Consumes: `get_readonly_connection(db_path: str | None = None) -> sqlite3.Connection`
- Produces: `connect_observer_main(db_path=None)` used only by EVM and Solana tick-observer threads.

- [ ] **Step 1: Run the focused regression**

Run: `python3 -m pytest tests/test_memecoin_runner_loop_cadence.py tests/test_wal_pinning.py -q -n 0`

Expected: PASS, including read-only autocommit and real WAL truncate behavior.

- [ ] **Step 2: Verify funded boundaries**

Run: `rg -n "connect_observer_main|get_readonly_connection" scripts/memecoin_runner_loop.py`

Expected: only the tick-observer main-ledger connections use `connect_observer_main`; funded execution continues to use its existing writable connection.

- [ ] **Step 3: Verify release readiness**

Run: `git diff --check && git status --short --branch`

Expected: clean diff and only intentional commits. Do not deploy until the resulting SHA is reachable from `origin/main`.
