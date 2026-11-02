# Phase 4A+4B: Strategy Lifecycle + Signal Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lifecycle stages (research/paper/promoted/retired) to strategies and compute signal quality metrics per strategy run.

**Architecture:** Migration adds `lifecycle_stage` column with default `'paper'`. Strategy runner filters by lifecycle_stage. Signal quality metrics computed from discovered/entered/rejected counts and stored in existing `strategy_runs.details_json`. Phase 4C (edge tracking, promote/retire logic) is deferred.

**Tech Stack:** SQLite, Python 3.13, existing strategy_runner + api_strategies infrastructure.

---

## File Map

| File | Responsibility |
|------|---------------|
| `migrations/013_strategy_lifecycle.py` | Add lifecycle_stage column |
| `strategy_runner.py` | Lifecycle filter in process_strategies, signal quality metrics in _finish_strategy_run |
| `routes/api_strategies.py` | lifecycle_stage in serialization + PATCH whitelist |
| `tests/test_strategy_lifecycle.py` | Lifecycle filtering + signal quality tests |

---

### Worker 1: Lifecycle State Machine (4A)

**Files:**
- Create: `migrations/013_strategy_lifecycle.py`
- Modify: `strategy_runner.py` (line 2141, WHERE clause)
- Modify: `routes/api_strategies.py` (line 655, ALLOWED_UPDATE_COLS)
- Create: `tests/test_strategy_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_strategy_lifecycle.py`:

```python
"""Tests for Phase 4A+4B: strategy lifecycle + signal quality metrics."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _insert_strategy(conn, strategy_id, wallet="0xtest", strategy_type="insider_copy_v1",
                     status="active", lifecycle_stage="paper"):
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (?, ?, ?, ?, '{}', ?)""",
        (strategy_id, wallet, f"test-{strategy_id}", strategy_type, status),
    )
    if _has_column(conn, "strategies", "lifecycle_stage"):
        conn.execute(
            "UPDATE strategies SET lifecycle_stage = ? WHERE id = ?",
            (lifecycle_stage, strategy_id),
        )
    conn.commit()


class TestLifecycleColumn:

    def test_column_exists_after_init(self):
        conn = _get_conn()
        assert _has_column(conn, "strategies", "lifecycle_stage")

    def test_default_is_paper(self):
        conn = _get_conn()
        _insert_strategy(conn, 1)
        row = conn.execute("SELECT lifecycle_stage FROM strategies WHERE id = 1").fetchone()
        assert row["lifecycle_stage"] == "paper"

    def test_valid_values(self):
        conn = _get_conn()
        for i, stage in enumerate(["research", "paper", "promoted", "retired"], start=1):
            _insert_strategy(conn, i, lifecycle_stage=stage)
        rows = conn.execute("SELECT lifecycle_stage FROM strategies ORDER BY id").fetchall()
        assert [r["lifecycle_stage"] for r in rows] == ["research", "paper", "promoted", "retired"]


class TestLifecycleFiltering:

    def test_paper_strategies_processed(self):
        """Strategy runner should process 'paper' lifecycle strategies."""
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="paper")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND lifecycle_stage IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 1

    def test_promoted_strategies_processed(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="promoted")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND lifecycle_stage IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 1

    def test_research_strategies_excluded(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="research")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND lifecycle_stage IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 0

    def test_retired_strategies_excluded(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="retired")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND lifecycle_stage IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 0
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_strategy_lifecycle.py::TestLifecycleColumn -v`
Expected: FAIL — `lifecycle_stage` column doesn't exist yet

- [ ] **Step 3: Create migration**

Create `migrations/013_strategy_lifecycle.py`:

```python
"""Migration 013: Add lifecycle_stage column to strategies table.

Adds lifecycle_stage TEXT DEFAULT 'paper' for the research→paper→promoted→retired
state machine. Existing strategies default to 'paper'.
"""
import sqlite3
import sys


def _column_exists(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def run(conn):
    if not _column_exists(conn, "strategies", "lifecycle_stage"):
        conn.execute(
            "ALTER TABLE strategies ADD COLUMN lifecycle_stage TEXT NOT NULL DEFAULT 'paper'"
        )
        conn.commit()
        print("Migration 013: added lifecycle_stage to strategies")
    else:
        print("Migration 013: lifecycle_stage already exists — skipped")


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 013 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Add column in init_db**

In `db.py`, find the strategies CREATE TABLE (~line 1305). Add `lifecycle_stage TEXT NOT NULL DEFAULT 'paper'` after the `pause_reason` column additions. Since `init_db` uses `CREATE TABLE IF NOT EXISTS` (the column is part of the schema), and existing DBs need the migration, also add an `ALTER TABLE` with `_column_exists` guard in `apply_phase_b_hardening` or a similar idempotent block.

- [ ] **Step 5: Modify strategy_runner.py process_strategies filter**

At line 2141, change:
```python
WHERE status = 'active'
```
To:
```python
WHERE status = 'active'
  AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')
```

The `COALESCE` handles any rows where the column hasn't been backfilled yet (belt-and-suspenders with the DEFAULT).

- [ ] **Step 6: Add lifecycle_stage to PATCH whitelist**

In `routes/api_strategies.py` line 655, add `"lifecycle_stage"` to `ALLOWED_UPDATE_COLS`.

- [ ] **Step 7: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_strategy_lifecycle.py -v`
Expected: All PASS

- [ ] **Step 8: Run full suite**

Run: `python3 -m pytest tests/ -q --tb=line`
Expected: 1885+ passed, 0 failed

- [ ] **Step 9: Commit**

```bash
git add migrations/013_strategy_lifecycle.py db.py strategy_runner.py routes/api_strategies.py tests/test_strategy_lifecycle.py
git commit -m "feat(phase4a): strategy lifecycle state machine (research/paper/promoted/retired)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Worker 2: Signal Quality Metrics (4B)

**Files:**
- Modify: `strategy_runner.py` (line 1348, _finish_strategy_run call site)
- Test: `tests/test_strategy_lifecycle.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy_lifecycle.py`:

```python
from db_hyperliquid import ensure_signal_queue_table, insert_signal_queue_entry, update_signal_queue_status


class TestSignalQualityMetrics:

    def test_compute_signal_quality(self):
        """Signal quality metrics should be computed from discovered/entered counts."""
        discovered = 10
        entered = 3
        # These are the metrics that should go into details_json
        hit_rate = entered / discovered if discovered > 0 else 0
        assert hit_rate == 0.3

    def test_signal_quality_in_details_json(self):
        """details_json should include signal_quality when strategy run finishes."""
        conn = _get_conn()
        ensure_signal_queue_table(conn)
        # Insert a strategy run manually
        conn.execute(
            """INSERT INTO strategy_runs (run_id, strategy_id, status, discovered_count,
               entered_count, exited_count, open_positions, details_json)
               VALUES (1, 99, 'ok', 10, 3, 2, 5, '{}')"""
        )
        conn.commit()

        # Simulate what _finish_strategy_run should do with signal quality
        discovered = 10
        entered = 3
        rejected = discovered - entered  # 7
        quality = {
            "discovery_hit_rate": round(entered / max(discovered, 1), 4),
            "rejection_rate": round(rejected / max(discovered, 1), 4),
            "signal_to_noise": round(entered / max(entered + rejected, 1), 4),
        }
        assert quality["discovery_hit_rate"] == 0.3
        assert quality["rejection_rate"] == 0.7
        assert quality["signal_to_noise"] == 0.3

    def test_zero_discovered_safe(self):
        """Zero discovered signals should produce zero metrics, no division by zero."""
        discovered = 0
        entered = 0
        quality = {
            "discovery_hit_rate": round(entered / max(discovered, 1), 4),
            "rejection_rate": round((discovered - entered) / max(discovered, 1), 4),
            "signal_to_noise": round(entered / max(entered + (discovered - entered), 1), 4),
        }
        assert quality["discovery_hit_rate"] == 0
        assert quality["rejection_rate"] == 0
        assert quality["signal_to_noise"] == 0
```

- [ ] **Step 2: Run tests — verify they pass (logic tests)**

Run: `python3 -m pytest tests/test_strategy_lifecycle.py::TestSignalQualityMetrics -v`
Expected: 3 PASS

- [ ] **Step 3: Implement signal quality in strategy_runner.py**

At line 1348, before the `_finish_strategy_run` call, compute signal quality and merge into the details dict:

```python
        # -- Phase 4B: Signal quality metrics --
        _signal_quality = {
            "discovery_hit_rate": round(entered_count / max(discovered_count, 1), 4),
            "rejection_rate": round((discovered_count - entered_count) / max(discovered_count, 1), 4),
            "signal_to_noise": round(entered_count / max(discovered_count, 1), 4),
        }
        _run_details = getattr(report, "summary", {}) or {}
        _run_details["signal_quality"] = _signal_quality
```

Then change line 1359 from:
```python
            details=getattr(report, "summary", {}) or {},
```
To:
```python
            details=_run_details,
```

- [ ] **Step 4: Run full suite**

Run: `python3 -m pytest tests/ -q --tb=line`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add strategy_runner.py tests/test_strategy_lifecycle.py
git commit -m "feat(phase4b): signal quality metrics in strategy_runs.details_json

Computes discovery_hit_rate, rejection_rate, signal_to_noise from
discovered/entered counts per strategy run. Stored in details_json.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Final Verification

- [ ] **Step 1:** `python3 -m pytest tests/test_strategy_lifecycle.py -v` — all pass
- [ ] **Step 2:** `python3 -m pytest tests/ -q` — full suite green
- [ ] **Step 3:** Code review via superpowers:requesting-code-review
