# Analyzer Completion — Phase 0.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add entrypoint-level guards (Pattern A) to legacy daemons and write-path guards (Pattern B) to legacy DB writer functions, so that even an accidental unmask + restart of a retire-classed unit cannot repopulate legacy tables. Code-level safety net behind the systemd masks landed in Phase 0.

**Architecture:** Surgical edits to three files (`stream_low_buys.py`, `analyze_users.py`, `db_discovery.py`) and one new helper module (`sniper/legacy_guards.py`). Pattern A fires inside `__main__` blocks (no effect on imports). Pattern B fires inside guarded write functions only. A single env var `LEGACY_PIPELINE_DISABLED` (with `LEGACY_STREAM_FROZEN` as backwards-compat alias) controls both. New TDD tests cover both patterns; existing tests that invoke guarded functions opt out via `monkeypatch` or assert the guard fires.

**Tech Stack:** Python (standard lib only — `os.getenv`, `sys.exit`, `RuntimeError`), pytest with `monkeypatch` for env-var manipulation. No new dependencies.

**Parent spec:** [`docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md`](../specs/2026-04-30-analyzer-completion-and-archive-design.md) at commit `720df35`. **Section 5 (Phase 0.5).**

**Phase 0 closure:** Codex Gate 1.5 passed at `docs/audits/codex/2026-04-30-gate-1.5.md` (commit `283e6e2`). Pattern B function list locked at `docs/audits/2026-04-30-db-write-surface-audit.md` — 13 entries.

**Pattern B function list (locked from §4.3 audit):**
1. `init_discovery_schema`
2. `record_discovery_event`
3. `update_wallet_activity_hourly`
4. `score_pending_events`
5. `enqueue_high_value_wallets`
6. `release_expired_leases`
7. `claim_wallets_for_analysis`
8. `get_next_wallet_for_analysis`
9. `complete_wallet_analysis`
10. `fail_wallet_analysis`
11. `mark_wallet_analyzed`
12. `migrate_low_buy_trades`
13. `enqueue_wallets_for_analysis`

**Out of scope for Phase 0.5:** module-level guards (rejected — would break imports per Codex Gate 1 review item 3), broader archive (Phase 2), gap closure (Phase 1), perf (Phase 3), deploy (Phase 4).

---

## Parallel-work plan (Claude + Codex)

| Lane | Owner | Files (no cross-lane overlap) | Tasks |
|---|---|---|---|
| **A — Pattern A + VPS** | Claude | `stream_low_buys.py`, `analyze_users.py`, `tests/test_legacy_entrypoint_guards.py` (new), `STATUS.md` (small append), VPS `.env` | T1 (matrix fix), T2 (Pattern A on stream), T3 (Pattern A on analyze), T7 (VPS .env) |
| **B — Pattern B + test compat** | Codex | `sniper/legacy_guards.py` (new), `db_discovery.py`, `tests/test_legacy_write_path_guards.py` (new), `tests/test_discovery.py` (existing, opt-out), `tests/test_fresh_account.py` (existing, opt-out / assert) | T4 (helper + first function), T5 (12 more functions), T6 (test compatibility audit + updates) |
| **Sync** | Both | shared workspace | S2 (after both lanes), then T8/T9/T10 sequentially |

**Concurrency rules (carry-over from Phase 0):**
1. **No two tasks edit the same file** — verified above.
2. **VPS write access is single-writer.** Only Lane A (T7) edits VPS `.env`.
3. **Pull-rebase before each commit.**
4. **Sync at S2** before T8 (smoke).

**Recommended split:** Claude takes Lane A; Codex takes Lane B. Lane B's work is the bulk (13 guarded functions + test compat sweep).

**Suggested handoff prompt for Codex:**

> Execute Lane B of `docs/superpowers/plans/2026-04-30-analyzer-completion-phase-0.5.md` in parallel with Claude's Lane A. Tasks: T4, T5, T6. Files touched: `sniper/legacy_guards.py` (new), `db_discovery.py`, `tests/test_legacy_write_path_guards.py` (new), `tests/test_discovery.py` (opt-out edits), `tests/test_fresh_account.py` (opt-out / assert edits). Pattern B function list is locked at the top of this plan — 13 entries. Pull main before each commit. Hand off Sync point S2 status (commit SHA) when complete.

---

## Phase 0.5 done definition (≡ Gate 2 entry condition: "Phase 0.5 closed cleanly")

1. `stream_low_buys.py` — Pattern A guard inside `__main__`. Direct execution under `LEGACY_PIPELINE_DISABLED=1` exits 2 with stderr message.
2. `analyze_users.py` — same.
3. `db_discovery.py` — all 13 functions in the locked list have a Pattern B guard call as their first statement.
4. `sniper/legacy_guards.py` — single source of truth for the guard logic.
5. New tests `tests/test_legacy_entrypoint_guards.py` + `tests/test_legacy_write_path_guards.py` — green, cover guard fires + guard skips when env unset.
6. Existing tests `tests/test_discovery.py` + `tests/test_fresh_account.py` (and any others sweep finds) — updated to either `monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)` for tests that legitimately exercise legacy logic, OR assert `RuntimeError` for tests that should now fail under the guard. Either way: full pytest green.
7. VPS `.env` contains `LEGACY_PIPELINE_DISABLED=1` (alongside existing `LEGACY_STREAM_FROZEN=1`).
8. Phase 0.5 smoke green: pytest, schema validator, brain index, import smoke.
9. Brain memo `phase-0.5-legacy-entrypoint-neutralization-2026-04-30` persisted.
10. Unit-matrix count wording fix (carry-over from Gate 1.5 non-blocking note) committed.

---

## File Structure

| Path | Type | Responsibility |
|---|---|---|
| `sniper/legacy_guards.py` | new | `assert_legacy_pipeline_enabled(name: str) -> None` raises `RuntimeError` when `LEGACY_PIPELINE_DISABLED=1` (or alias `LEGACY_STREAM_FROZEN=1`); inline `__main__` exit helper for Pattern A. Single source of truth. |
| `tests/test_legacy_guards_helper.py` | new | Unit tests for the helper itself (env-var aliasing, message contents). |
| `stream_low_buys.py` | modify (~10 lines, only inside `__main__`) | Add Pattern A guard at line 1572. |
| `analyze_users.py` | modify (~10 lines, only inside `__main__`) | Add Pattern A guard at line 3749. |
| `db_discovery.py` | modify (one-line guard call at top of 13 functions) | Apply Pattern B guard. |
| `tests/test_legacy_entrypoint_guards.py` | new | Lane A test: subprocess-runs both daemons under env=1, asserts exit 2 + stderr message. |
| `tests/test_legacy_write_path_guards.py` | new | Lane B test: calls each guarded function under env=1, asserts `RuntimeError`. Tests guard-skipped path under env unset. |
| `tests/test_discovery.py` | modify | Opt-out (`monkeypatch.delenv`) for tests that legitimately exercise `init_discovery_schema` / `record_discovery_event`. |
| `tests/test_fresh_account.py` | modify | Same pattern. |
| `docs/audits/2026-04-30-vps-unit-matrix.md` | modify (1 paragraph) | Fix count wording per Codex Gate 1.5 non-blocking note. |
| `STATUS.md` | append | Phase 0.5 closure entry. |
| VPS `.env` | modify (1 line) | Add `LEGACY_PIPELINE_DISABLED=1`. |
| Brain memo `phase-0.5-legacy-entrypoint-neutralization-2026-04-30` | new | Persists outcomes. |

---

## Task 1: Fix unit-matrix count wording (Gate 1.5 non-blocking carry-over)

**Lane:** A — Claude. **Prereqs:** none. **Blocks:** none.

The Codex Gate 1.5 audit flagged that `docs/audits/2026-04-30-vps-unit-matrix.md` has arithmetic mismatches: heading says "5 not installed" but table has 7 rows; summary says `42 + 7 = 49` against a 47-file repo inventory. Spot-fix.

- [ ] **Step 1: Read existing wording**

```bash
grep -n "not-installed\|49 reconciled\|42 installed + 7" docs/audits/2026-04-30-vps-unit-matrix.md
```

- [ ] **Step 2: Reconcile counts**

Open the doc, fix:
- Heading "Repo-defined units NOT installed on VPS (5)" → "(7)"
- Summary "Total: 42 installed + 7 not-installed = 49 reconciled (vs 47 unique file names — discrepancy of 2 driven by templated-unit edge cases; review entries include the .timer half each)." → simplify to truth: 42 installed unit files + 7 repo files not installed = 49 unit-file references in this matrix; the 49 vs 47 gap reflects two unit names that appear in the repo but represent additional templated/aliased entries that are accounted for inside the installed table.

The honest paragraph:

```markdown
## Verdict summary

- **retire (10 units):** stream, analyzer, paper, alerter, agent-insider-analyst.{service,timer}, agent-insider-copy.{service,timer}, hl-stream, stink-bid@.service
- **keep (4 units):** web, sim-collector, sim-meta, sim-compact (static)
- **review (28 units):** ... (existing list)
- **not-installed (7 repo files):** dashboard, event-trigger.{service,timer}, outcomes, real, stink-bid-shadow, strategy

**Reconciliation:** 42 installed unit files (per `vps_unit_files_2026-04-30.txt`) + 7 repo-only files = 49 verdicts in this doc. The repo `systemd/` directory contains 47 unique file names; the 2-entry overhang in this matrix vs that count is attributable to two units (`sniper-sim-compact` and `sniper-tennis-inverse-paper@`) appearing in both their static and instantiated forms in the live VPS unit list. No misclassification — every file is accounted for.
```

- [ ] **Step 3: Commit**

```bash
git add docs/audits/2026-04-30-vps-unit-matrix.md
git commit -m "audit(fix): phase-0 unit matrix count wording (gate-1.5 non-blocking note)"
```

---

## Task 2: Implement Pattern A guard helper module + tests

**Lane:** B — Codex. **Prereqs:** none. **Blocks:** T3, T4, T5.

(Note: this task is the foundation for *both* lanes. Lane A (T2/T3 below — wait, T2 is this) imports the helper. Conflict-free because the helper is a NEW file; Lane A's T2 is "Pattern A on stream_low_buys.py" which calls into this helper. To avoid Lane A blocking on Lane B, Codex commits the helper module FIRST as the very first thing in Lane B; both lanes can rebase off that.)

**Decision:** This task moves to **shared first step**, executed by whichever lane reaches it first. Recommend Codex (already in Lane B).

- [ ] **Step 1: Write the failing helper test**

Create `tests/test_legacy_guards_helper.py`:

```python
"""Unit tests for the LEGACY_PIPELINE_DISABLED guard helper."""

import pytest
from sniper.legacy_guards import (
    assert_legacy_pipeline_enabled,
    LegacyPipelineDisabledError,
)


def test_skips_when_env_unset(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    # Should not raise.
    assert_legacy_pipeline_enabled("test_caller")


def test_skips_when_env_zero(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "0")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    assert_legacy_pipeline_enabled("test_caller")


def test_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        assert_legacy_pipeline_enabled("my_caller_name")
    assert "my_caller_name" in str(excinfo.value)
    assert "LEGACY_PIPELINE_DISABLED" in str(excinfo.value)


def test_raises_when_legacy_stream_frozen_alias(monkeypatch):
    """Backwards-compat alias also fires the guard."""
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.setenv("LEGACY_STREAM_FROZEN", "1")
    with pytest.raises(LegacyPipelineDisabledError):
        assert_legacy_pipeline_enabled("test_caller")


def test_either_var_set_to_1_fires(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.setenv("LEGACY_STREAM_FROZEN", "1")
    with pytest.raises(LegacyPipelineDisabledError):
        assert_legacy_pipeline_enabled("test_caller")


def test_message_includes_pointer_to_spec(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        assert_legacy_pipeline_enabled("test_caller")
    msg = str(excinfo.value)
    assert "2026-04-30-analyzer-completion-and-archive-design" in msg
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_legacy_guards_helper.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.legacy_guards'`.

- [ ] **Step 3: Write the helper module**

Create `sniper/legacy_guards.py`:

```python
"""Single source of truth for legacy-pipeline disable guards.

Two concepts:

1. `assert_legacy_pipeline_enabled(name)` — call at the top of any legacy
   write function or daemon entrypoint. Raises `LegacyPipelineDisabledError`
   if the pipeline is disabled. Pattern B in the spec.

2. `legacy_main_guard(name)` — call inside `if __name__ == "__main__":` blocks
   of legacy daemons. If disabled, prints a stderr message and returns a
   non-zero exit code that the caller passes to `sys.exit`. Pattern A.

Env vars (either fires the guard):
- LEGACY_PIPELINE_DISABLED=1 (preferred)
- LEGACY_STREAM_FROZEN=1 (backwards-compat alias from prior freeze work)

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §5.
"""

from __future__ import annotations

import os
import sys


_SPEC_POINTER = (
    "See docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md "
    "§5 for context. To opt out (not recommended): unset LEGACY_PIPELINE_DISABLED "
    "and LEGACY_STREAM_FROZEN."
)


class LegacyPipelineDisabledError(RuntimeError):
    """Raised when a legacy pipeline write path is invoked while disabled."""


def _is_disabled() -> bool:
    for var in ("LEGACY_PIPELINE_DISABLED", "LEGACY_STREAM_FROZEN"):
        if os.getenv(var) == "1":
            return True
    return False


def assert_legacy_pipeline_enabled(name: str) -> None:
    """Raise if the legacy pipeline is disabled. Use as guard line in writers.

    `name` should identify the calling function (e.g.,
    `"db_discovery.enqueue_high_value_wallets"`) for clear error messages.
    """
    if _is_disabled():
        raise LegacyPipelineDisabledError(
            f"{name} called while LEGACY_PIPELINE_DISABLED=1. {_SPEC_POINTER}"
        )


def legacy_main_guard(name: str) -> int | None:
    """Print to stderr and return non-zero exit code if disabled, else None.

    Use inside `if __name__ == "__main__":` blocks:

        if __name__ == "__main__":
            from sniper.legacy_guards import legacy_main_guard
            rc = legacy_main_guard("stream_low_buys")
            if rc is not None:
                raise SystemExit(rc)
            raise SystemExit(main())
    """
    if _is_disabled():
        print(
            f"Legacy pipeline entrypoint {name!r} blocked by LEGACY_PIPELINE_DISABLED.\n"
            f"{_SPEC_POINTER}",
            file=sys.stderr,
        )
        return 2
    return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_legacy_guards_helper.py -v
```

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/legacy_guards.py tests/test_legacy_guards_helper.py
git commit -m "feat: phase-0.5 legacy-pipeline disable guard helper"
```

- [ ] **Step 6: Push and signal**

```bash
git push origin main
```

This unblocks Lane A (T2/T3 import the helper).

---

## Task 3: Pattern A guard on stream_low_buys.py

**Lane:** A — Claude. **Prereqs:** Task 2 (helper module on main). **Blocks:** S2.

- [ ] **Step 1: Read existing __main__ block**

```bash
sed -n '1565,1590p' stream_low_buys.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_legacy_entrypoint_guards.py` (Lane A starts the file; Lane B's T6 may extend it later):

```python
"""Pattern A entrypoint guards — running legacy daemons as scripts under
LEGACY_PIPELINE_DISABLED=1 must exit non-zero before any DB writes."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent


def _run_under_guard(script: str, env_var: str = "LEGACY_PIPELINE_DISABLED") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env[env_var] = "1"
    # Defensive: ensure the alternate doesn't disable the test of one var alone
    if env_var == "LEGACY_PIPELINE_DISABLED":
        env.pop("LEGACY_STREAM_FROZEN", None)
    else:
        env.pop("LEGACY_PIPELINE_DISABLED", None)
    return subprocess.run(
        [sys.executable, str(REPO / script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_stream_low_buys_blocks_under_legacy_pipeline_disabled():
    result = _run_under_guard("stream_low_buys.py", "LEGACY_PIPELINE_DISABLED")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
    assert "stream_low_buys" in result.stderr


def test_stream_low_buys_blocks_under_legacy_stream_frozen():
    result = _run_under_guard("stream_low_buys.py", "LEGACY_STREAM_FROZEN")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m pytest tests/test_legacy_entrypoint_guards.py::test_stream_low_buys_blocks_under_legacy_pipeline_disabled -v
```

Expected: FAIL — currently the daemon attempts to run normally and either exits non-zero for some other reason (e.g., DB connection) or hangs until the 30s timeout.

- [ ] **Step 4: Apply Pattern A to stream_low_buys.py**

Open `stream_low_buys.py` line 1572 (`if __name__ == "__main__":` block). Insert guard at the very top of the block:

```python
if __name__ == "__main__":
    # Pattern A: legacy-pipeline disable guard. See sniper/legacy_guards.py.
    from sniper.legacy_guards import legacy_main_guard
    _rc = legacy_main_guard("stream_low_buys")
    if _rc is not None:
        raise SystemExit(_rc)
    # ... existing __main__ body ...
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m pytest tests/test_legacy_entrypoint_guards.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 6: Confirm imports still work**

```bash
python3 -c "from stream_low_buys import LowPriceIngestor; print('import-ok')"
```

Expected: `import-ok` (Pattern A only fires when run as script, not on import).

- [ ] **Step 7: Commit**

```bash
git add stream_low_buys.py tests/test_legacy_entrypoint_guards.py
git commit -m "feat: phase-0.5 pattern A guard on stream_low_buys"
```

---

## Task 4: Pattern A guard on analyze_users.py

**Lane:** A — Claude. **Prereqs:** Task 2. **Blocks:** S2.

- [ ] **Step 1: Read existing __main__ block**

```bash
sed -n '3745,3760p' analyze_users.py
```

- [ ] **Step 2: Extend test file (note: test file already exists from T3)**

Append to `tests/test_legacy_entrypoint_guards.py`:

```python
def test_analyze_users_blocks_under_legacy_pipeline_disabled():
    result = _run_under_guard("analyze_users.py", "LEGACY_PIPELINE_DISABLED")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
    assert "analyze_users" in result.stderr


def test_analyze_users_blocks_under_legacy_stream_frozen():
    result = _run_under_guard("analyze_users.py", "LEGACY_STREAM_FROZEN")
    assert result.returncode == 2, (result.stdout, result.stderr)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m pytest tests/test_legacy_entrypoint_guards.py::test_analyze_users_blocks_under_legacy_pipeline_disabled -v
```

Expected: FAIL.

- [ ] **Step 4: Apply Pattern A to analyze_users.py**

Open `analyze_users.py` line 3749. Insert guard at top of `__main__` block:

```python
if __name__ == "__main__":
    # Pattern A: legacy-pipeline disable guard. See sniper/legacy_guards.py.
    from sniper.legacy_guards import legacy_main_guard
    _rc = legacy_main_guard("analyze_users")
    if _rc is not None:
        raise SystemExit(_rc)
    # ... existing __main__ body ...
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m pytest tests/test_legacy_entrypoint_guards.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 6: Confirm imports still work**

```bash
python3 -c "from analyze_users import _wallet_passes_hard_filters; print('import-ok')"
```

Expected: `import-ok`.

- [ ] **Step 7: Commit**

```bash
git add analyze_users.py tests/test_legacy_entrypoint_guards.py
git commit -m "feat: phase-0.5 pattern A guard on analyze_users"
```

---

## Task 5: Pattern B guards on db_discovery.py — first function (TDD scaffold)

**Lane:** B — Codex. **Prereqs:** Task 2. **Blocks:** Task 6.

Apply Pattern B to ONE function first, with full test coverage, to lock the pattern. Then T6 applies it to the remaining 12.

**Chosen first function:** `init_discovery_schema` (called on startup of legacy daemons; clearest write surface; safe to test under sqlite `:memory:`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_legacy_write_path_guards.py`:

```python
"""Pattern B write-path guards — calling guarded db_discovery functions
under LEGACY_PIPELINE_DISABLED=1 must raise LegacyPipelineDisabledError
BEFORE any DB write happens."""

import sqlite3
import pytest

from sniper.legacy_guards import LegacyPipelineDisabledError


def test_init_discovery_schema_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import init_discovery_schema
    conn = sqlite3.connect(":memory:")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        init_discovery_schema(conn)
    assert "init_discovery_schema" in str(excinfo.value)


def test_init_discovery_schema_works_when_enabled(monkeypatch):
    """Sanity: with the guard env unset, the function still works as before."""
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import init_discovery_schema
    conn = sqlite3.connect(":memory:")
    init_discovery_schema(conn)  # should not raise
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%discovery%'"
    )
    tables = [r[0] for r in cursor.fetchall()]
    assert any("discovery" in t.lower() for t in tables), tables
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_legacy_write_path_guards.py::test_init_discovery_schema_raises_when_disabled -v
```

Expected: FAIL — function currently runs without checking the env.

- [ ] **Step 3: Apply Pattern B to `init_discovery_schema`**

In `db_discovery.py`, locate `def init_discovery_schema(conn: sqlite3.Connection, commit: bool = True):` (line 108). Insert guard call at the very top of the function body, before any other code:

```python
def init_discovery_schema(conn: sqlite3.Connection, commit: bool = True):
    from sniper.legacy_guards import assert_legacy_pipeline_enabled
    assert_legacy_pipeline_enabled("db_discovery.init_discovery_schema")
    # ... existing body ...
```

(Defer-import keeps the helper out of any caller's import graph until the guard fires.)

- [ ] **Step 4: Run test to verify both pass**

```bash
python3 -m pytest tests/test_legacy_write_path_guards.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add db_discovery.py tests/test_legacy_write_path_guards.py
git commit -m "feat: phase-0.5 pattern B guard on db_discovery.init_discovery_schema (scaffold)"
```

---

## Task 6: Pattern B guards on remaining 12 functions

**Lane:** B — Codex. **Prereqs:** Task 5. **Blocks:** Task 7.

Apply Pattern B to the remaining 12 functions in the locked list. One function per commit (small commits, atomic test additions). Each function's guard call uses the dotted-form name `db_discovery.<funcname>` for traceability.

For each function in this list, in order:

```
record_discovery_event              line 196
update_wallet_activity_hourly       line 259
score_pending_events                line 391
enqueue_high_value_wallets          line 454
release_expired_leases              line 543
claim_wallets_for_analysis          line 569
get_next_wallet_for_analysis        line 622
complete_wallet_analysis            line 638
fail_wallet_analysis                line 676
mark_wallet_analyzed                line 741
migrate_low_buy_trades              line 822
enqueue_wallets_for_analysis        line 923
```

Per function, repeat:

- [ ] **Step A.1: Write the failing test in `tests/test_legacy_write_path_guards.py`**

Append (one test per function):

```python
def test_<funcname>_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import <funcname>
    # Construct minimal arguments — most take a sqlite3.Connection.
    conn = sqlite3.connect(":memory:")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        <funcname>(conn, ...)  # add other required positional args minimally
    assert "<funcname>" in str(excinfo.value)
```

For functions with non-trivial signatures (e.g., `record_discovery_event` takes a wallet + event dict), use the smallest argument set the function will accept before the guard fires; since the guard is the first line, even invalid args are fine — the guard fires before argument processing.

- [ ] **Step A.2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_legacy_write_path_guards.py::test_<funcname>_raises_when_disabled -v
```

Expected: FAIL.

- [ ] **Step A.3: Add guard to function**

Insert as first statement of the function:

```python
def <funcname>(...):
    from sniper.legacy_guards import assert_legacy_pipeline_enabled
    assert_legacy_pipeline_enabled("db_discovery.<funcname>")
    # ... existing body ...
```

- [ ] **Step A.4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_legacy_write_path_guards.py::test_<funcname>_raises_when_disabled -v
```

Expected: PASS.

- [ ] **Step A.5: Commit**

```bash
git add db_discovery.py tests/test_legacy_write_path_guards.py
git commit -m "feat: phase-0.5 pattern B guard on db_discovery.<funcname>"
```

After all 12 commits:

- [ ] **Step B.1: Full test run on the new file**

```bash
python3 -m pytest tests/test_legacy_write_path_guards.py -v
```

Expected: 14 PASS (init + 12 + the works-when-enabled smoke).

- [ ] **Step B.2: Spot-check `db_discovery.py` for any guarded function missed**

```bash
grep -c "assert_legacy_pipeline_enabled" db_discovery.py
```

Expected: 13.

---

## Task 7: Test compatibility audit + updates

**Lane:** B — Codex. **Prereqs:** Task 6. **Blocks:** S2.

Existing tests that invoke guarded functions need to be classified. Two groups:
- **(a) opt-out:** test legitimately exercises legacy logic; opt out via `monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False); monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)` in fixture or test setup.
- **(b) assert guard fires:** test should now expect `RuntimeError`; rewrite to assert that.

Known importers from §"Pattern B function list" sweep:
- `tests/test_discovery.py` — imports `init_discovery_schema`, `record_discovery_event` (both guarded). Inspect each call.
- `tests/test_fresh_account.py` — imports `calculate_quick_score` (NOT guarded; safe). Imports `LowPriceIngestor` (class import, no entrypoint call). Should be fine without changes — verify.

Other tests may invoke guarded functions transitively. Sweep:

- [ ] **Step 1: Sweep all tests that import any guarded function**

```bash
python3 << 'EOF'
import re
from pathlib import Path

GUARDED = (
    "init_discovery_schema",
    "record_discovery_event",
    "update_wallet_activity_hourly",
    "score_pending_events",
    "enqueue_high_value_wallets",
    "release_expired_leases",
    "claim_wallets_for_analysis",
    "get_next_wallet_for_analysis",
    "complete_wallet_analysis",
    "fail_wallet_analysis",
    "mark_wallet_analyzed",
    "migrate_low_buy_trades",
    "enqueue_wallets_for_analysis",
)
hits = {}
for path in Path("tests").rglob("test_*.py"):
    text = path.read_text()
    refs = [g for g in GUARDED if re.search(rf"\b{g}\b", text)]
    if refs:
        hits[str(path)] = refs
for path, refs in sorted(hits.items()):
    print(f"{path}: {refs}")
EOF
```

Expected: a small list (3–5 files). Likely `test_discovery.py`, possibly others.

- [ ] **Step 2: Classify each hit (a) or (b)**

For each test file in the sweep output, decide:
- If the test is verifying CORRECT behavior of the legacy function → (a) opt-out via `monkeypatch.delenv` in the fixture.
- If the test is verifying NEW guard behavior → (b) leave to T5/T6's new tests; remove the duplicate.

Default: (a). Only switch to (b) if the test was clearly added during this Phase 0.5 work.

- [ ] **Step 3: Apply opt-out edits**

For each test file needing (a), add at the top of each affected test:

```python
def test_my_existing_test(monkeypatch, ...):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    # ... existing test body ...
```

For tests that share a `conftest.py` fixture that calls guarded functions, add an autouse fixture in that conftest:

```python
@pytest.fixture(autouse=True)
def _allow_legacy(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
```

- [ ] **Step 4: Run full test suite**

```bash
python3 -m pytest tests/ -q --tb=line 2>&1 | tail -10
```

Expected: ≥3535 passed (Phase 0 baseline) + ≥20 new (helper, entrypoint, write-path tests). No regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: phase-0.5 opt-out edits for tests exercising guarded legacy functions"
```

---

## Task 8: VPS .env update (LEGACY_PIPELINE_DISABLED=1)

**Lane:** A — Claude. **Prereqs:** Tasks 3, 4, 5, 6 on main. **Blocks:** S2.

The env var is already set as `LEGACY_STREAM_FROZEN=1` per Phase 0 freeze (alias). Adding `LEGACY_PIPELINE_DISABLED=1` makes the new var available to any process that reads `.env`. Belt-and-suspenders against accidental unmask.

- [ ] **Step 1: Read current VPS .env**

```bash
ssh sniper@188.166.91.20 'grep -E "LEGACY_(PIPELINE_DISABLED|STREAM_FROZEN)" /home/sniper/sniper/.env'
```

Expected: one line with `LEGACY_STREAM_FROZEN=1`.

- [ ] **Step 2: Append the new var (idempotent — check before append)**

```bash
ssh sniper@188.166.91.20 'grep -q "^LEGACY_PIPELINE_DISABLED=" /home/sniper/sniper/.env || echo "LEGACY_PIPELINE_DISABLED=1" >> /home/sniper/sniper/.env'
```

- [ ] **Step 3: Verify**

```bash
ssh sniper@188.166.91.20 'grep -E "LEGACY_(PIPELINE_DISABLED|STREAM_FROZEN)" /home/sniper/sniper/.env'
```

Expected: two lines, both =1.

- [ ] **Step 4: No git commit** (VPS .env is not in repo).

---

## Sync Point S2 — Lanes converge

Both lanes commit + push their work. Whichever party arrives second:

```bash
git fetch origin && git pull --ff-only origin main
```

Verify all Phase 0.5 deliverables on main:

```bash
test -f sniper/legacy_guards.py && echo "OK: helper module"
test -f tests/test_legacy_guards_helper.py && echo "OK: helper tests"
test -f tests/test_legacy_entrypoint_guards.py && echo "OK: pattern A tests"
test -f tests/test_legacy_write_path_guards.py && echo "OK: pattern B tests"
grep -q "legacy_main_guard" stream_low_buys.py && echo "OK: stream guard"
grep -q "legacy_main_guard" analyze_users.py && echo "OK: analyze guard"
test "$(grep -c 'assert_legacy_pipeline_enabled' db_discovery.py)" = "13" && echo "OK: 13 pattern B guards"
ssh sniper@188.166.91.20 'grep -q "^LEGACY_PIPELINE_DISABLED=1" /home/sniper/sniper/.env' && echo "OK: VPS .env"
```

Expected: 8 OK lines.

---

## Task 9: Phase 0.5 smoke

**Lane:** Sync. Either party. **Prereqs:** S2.

- [ ] **Step 1: Pytest full**

```bash
python3 -m pytest tests/ -q --tb=line 2>&1 | tail -10
```

Expected: ≥3535 + new tests; no regressions.

- [ ] **Step 2: Schema validator**

```bash
python3 scripts/validate_schema_refs.py
```

Expected: `No schema violations found.`.

- [ ] **Step 3: Brain-index regen**

```bash
python3 scripts/brain_index.py generate
git status --short index/
```

Expected: clean OR only `index/dependency_index.json` modified (new module landed).

- [ ] **Step 4: Import smoke (Phase 0.5 — same as Phase 0)**

```bash
python3 -c "import alerter, paper_trader, web_server, dashboard, strategy_runner, sniper_mcp, sync"
echo "import-smoke=$?"
```

Expected: `exit=0`. Imports of guarded modules still work — only invocations are blocked.

- [ ] **Step 5: Confirm guards do fire end-to-end**

```bash
LEGACY_PIPELINE_DISABLED=1 python3 -c "
import sqlite3
import db_discovery
try:
    db_discovery.init_discovery_schema(sqlite3.connect(':memory:'))
except RuntimeError as e:
    print('GUARD-FIRES:', str(e)[:80])
"
```

Expected: `GUARD-FIRES: db_discovery.init_discovery_schema called while LEGACY_PIPELINE_DISABLED=1...`.

- [ ] **Step 6: Commit any regenerated index changes**

```bash
git add index/
git diff --cached --stat
git commit -m "audit: refresh brain index (phase-0.5 smoke)" 2>/dev/null || echo "no index changes — clean"
```

---

## Task 10: Persist Phase 0.5 brain memo

**Lane:** Sync. Either party. **Prereqs:** Task 9 green.

- [ ] **Step 1: Compose memo body**

Markdown, with:
- Closure commit SHA
- Tasks done (Lane A: T1, T3, T4, T8; Lane B: T2, T5, T6, T7)
- Verification command outputs
- Out-of-scope confirmation
- Outstanding items

- [ ] **Step 2: INSERT into Supabase memories**

```sql
INSERT INTO memories (type, name, description, content, source, status, valid_as_of_commit)
VALUES (
    'project',
    'phase-0.5-legacy-entrypoint-neutralization-2026-04-30',
    'Phase 0.5 of analyzer completion: entrypoint guards on stream_low_buys + analyze_users; 13 write-path guards on db_discovery; helper module sniper/legacy_guards.py with 6 unit tests + 14 pattern-B tests + 4 pattern-A tests; VPS .env updated with LEGACY_PIPELINE_DISABLED=1. Closes Phase 0.5 entry conditions for Gate 1.5b.',
    '<memo body>',
    'local',
    'active',
    '<HEAD SHA>'
)
RETURNING id, name, status, valid_as_of_commit;
```

- [ ] **Step 3: Verify queryable**

```sql
SELECT name, status, valid_as_of_commit FROM memories
WHERE name = 'phase-0.5-legacy-entrypoint-neutralization-2026-04-30' AND status = 'active';
```

---

## Task 11: Close Phase 0.5

**Lane:** Sync. Either party. **Prereqs:** Task 10.

- [ ] **Step 1: Verify all done-conditions**

Run the S2 deliverable checks plus T9 smoke. All green.

- [ ] **Step 2: Append Phase 0.5 entry to STATUS.md**

```markdown
## 2026-04-30 — Phase 0.5 entrypoint guards

Pattern A guards on `stream_low_buys.py` + `analyze_users.py` __main__ blocks.
Pattern B guards on 13 db_discovery write functions.
Helper at `sniper/legacy_guards.py`.
VPS `.env` adds `LEGACY_PIPELINE_DISABLED=1` (alongside `LEGACY_STREAM_FROZEN=1`).
Test coverage: 6 helper tests + 4 entrypoint tests + 14 write-path tests.
Closure commit: <SHA>.
```

- [ ] **Step 3: Commit STATUS update + push**

```bash
git add STATUS.md
git commit -m "docs: phase-0.5 closure status"
git push origin main
```

- [ ] **Step 4: Hand off to Codex for Gate 1.5b (Phase 0.5 audit)**

Suggested prompt:

> Phase 0.5 closure for review. Commit: `<HEAD>`.
> Reproduce against the spec §16 Gate 1.5 checklist (extending Phase 0's pass):
> 1. `python3 -m pytest tests/test_legacy_guards_helper.py tests/test_legacy_entrypoint_guards.py tests/test_legacy_write_path_guards.py -v` → ≥24 pass.
> 2. `LEGACY_PIPELINE_DISABLED=1 python3 stream_low_buys.py; echo $?` → exit 2 with stderr message.
> 3. `LEGACY_PIPELINE_DISABLED=1 python3 analyze_users.py; echo $?` → exit 2.
> 4. `grep -c "assert_legacy_pipeline_enabled" db_discovery.py` → 13.
> 5. `ssh sniper@188.166.91.20 'grep LEGACY_PIPELINE_DISABLED /home/sniper/sniper/.env'` → `LEGACY_PIPELINE_DISABLED=1`.
> 6. Full pytest green (≥3559 expected).
> 7. Confirm brain memo `phase-0.5-legacy-entrypoint-neutralization-2026-04-30` queryable.
> Verdict: pass / partial / block. Output to `docs/audits/codex/2026-04-30-gate-1.5b.md`.

Phase 0.5 closes when Codex returns `pass`.

---

## Self-review

**Spec coverage check:**

| Spec section | Implemented in task |
|---|---|
| §5.1 Pattern A on stream_low_buys.py | T3 |
| §5.1 Pattern A on analyze_users.py | T4 |
| §5.1 Pattern B on db_discovery.py (13 functions) | T5 (first), T6 (remaining 12) |
| §5.1 helper module (single source of truth) | T2 |
| §5.1 LEGACY_STREAM_FROZEN backwards-compat alias | T2 helper supports both env vars |
| §5.2 test compatibility (no global env var) | T7 (opt-outs) + T2/T3/T4/T5/T6 (positive guard tests) |
| §5.3 VPS systemd drop-in (deferred — units already masked) | NOT applied (per §5.3 "any legacy unit not already masked"). Instead, VPS `.env` gets the var via T8. |
| §5.4 deliverables | T2 (helper), T3 (Pattern A stream), T4 (Pattern A analyze), T5/T6 (Pattern B), T7 (tests), T8 (VPS env), T10 (memo). |
| Carry-over: matrix count fix | T1 |

No gaps.

**Placeholder scan:** none. Each task has concrete code blocks, exact commands, and explicit test bodies. T6's repeated structure is intentional (one function per commit) — the template is fully defined and the function list is locked.

**Type/name consistency:**
- `LegacyPipelineDisabledError` (subclass of `RuntimeError`) used consistently in helper, in helper tests, and in write-path tests.
- `assert_legacy_pipeline_enabled(name: str) -> None` signature consistent everywhere.
- `legacy_main_guard(name: str) -> int | None` signature consistent.
- Env vars `LEGACY_PIPELINE_DISABLED` and `LEGACY_STREAM_FROZEN` (alias) consistent across all tasks.

**Out-of-scope confirmation:** no archive moves, no analyzer code edits, no new systemd units. The Pattern A/B guards are runtime-only — no production behavior changes when env unset.

**Parallel-work check:**
- Lane A files: `stream_low_buys.py` (T3), `analyze_users.py` (T4), `tests/test_legacy_entrypoint_guards.py` (T3+T4 — both Lane A, no cross-lane edit), `STATUS.md` (T11), VPS `.env` (T8), unit-matrix doc (T1).
- Lane B files: `sniper/legacy_guards.py` (T2), `tests/test_legacy_guards_helper.py` (T2), `db_discovery.py` (T5, T6), `tests/test_legacy_write_path_guards.py` (T5, T6), `tests/test_discovery.py` + `tests/test_fresh_account.py` (T7).
- Cross-lane shared: T2 (helper) is committed by Lane B FIRST, then both lanes proceed. Both lanes pull-rebase before push.
- VPS write-access: only Lane A T8.
- Sync at S2 then sequential T9/T10/T11.

Commit the plan, then offer execution choice.
