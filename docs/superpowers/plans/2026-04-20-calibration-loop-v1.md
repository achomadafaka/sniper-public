# Calibration Loop v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `predict_tool` graded outcomes into `insider_copy_agent.py`'s prompt as belief weights, so the agent sees its own calibration stats at every run and is required to emit a structured calibration envelope per prediction.

**Architecture:**
- JIT calibration-stats function over `agent_predictions` joined to `agent_runs` via `run_id`, filtered to eligible runs (`status='success' AND turns > 0`). No materialization, no new tables — one new JSONB column on `agent_runs`.
- Three new `predict_tool` subcommands (`start-run`, `finish-run`, and `--run-id`/`--calibration-note` on `predict`) give the wrapper a complete lifecycle: open-run → emit-predictions-with-calibration-note → close-run.
- `insider_copy_agent.py` is the only agent wired in v1. Rollout to other agents is tracked in the spec, not this plan.

**Tech Stack:** Python 3.12, Supabase Postgres (via REST), SQLite (local), `claude-agent-sdk`, `pytest`. Committed migration SQL files. No new dependencies.

**Source spec:** [docs/superpowers/specs/2026-04-20-calibration-loop-v1-design.md](/home/bigpapi/Desktop/Sniper/docs/superpowers/specs/2026-04-20-calibration-loop-v1-design.md) — refer for rationale; this plan is implementation-only.

---

## Ordering & rationale

1. **Schema migrations first** (Tasks 1–2): tool code cannot land without the column and the relaxed CHECK.
2. **Settings** (Task 3): constants consumed by tool code and wrapper.
3. **Tool layer** (Tasks 4–7): `cmd_calibration_stats` (pure read), `cmd_start_run` (write, depends on stats), `cmd_finish_run` (write), `cmd_predict` upgrade. Each is TDD with a stub fixture for Supabase.
4. **Backfill script** (Task 8): one-shot, validated against `CALIBRATION_ENABLED_AGENTS`.
5. **Wrapper + prompt** (Tasks 9–10): insider_copy wired end-to-end.
6. **Observability** (Tasks 11–12): brain-health `CAL1`, schema-registry regen.
7. **Integration + activation** (Task 13): integration test, backfill execution, CAL1 enable.

---

## Task 1: Migration — add `agent_runs.calibration_context`

**Files:**
- Create: `migrations/supabase_013_agent_runs_calibration_context.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration: supabase_013_agent_runs_calibration_context.sql
-- Add calibration_context JSONB column to agent_runs for the v1 calibration loop.
--
-- Why this exists:
--   Calibration loop v1 writes per-run calibration stats (the stats the agent
--   saw at run start) as a JSONB blob on agent_runs. Stats themselves are
--   computed just-in-time from agent_predictions; only the snapshot shown to
--   the agent is persisted here for auditability.
--
-- Nullable because legacy runs and runs from agents not-yet-migrated do not
-- populate it. Consumers (brain_health CAL1) already handle NULL.
--
-- Safe to re-run: IF NOT EXISTS guard.

ALTER TABLE public.agent_runs
  ADD COLUMN IF NOT EXISTS calibration_context JSONB NULL;

COMMENT ON COLUMN public.agent_runs.calibration_context IS
'Calibration stats snapshot shown to the agent at run start. Populated by predict_tool start-run; null for runs from agents not in CALIBRATION_ENABLED_AGENTS.';
```

- [ ] **Step 2: Apply via MCP**

Run via the Supabase MCP tool `apply_migration` with `name="supabase_013_agent_runs_calibration_context"` and body = the SQL above.

Expected: migration succeeds. Verify with:
```
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='agent_runs' AND column_name='calibration_context';
```
Expected row: `calibration_context | jsonb`.

- [ ] **Step 3: Commit**

```bash
git add migrations/supabase_013_agent_runs_calibration_context.sql
git commit -m "migration 013: add agent_runs.calibration_context JSONB"
```

---

## Task 2: Migration — extend `agent_runs.status` CHECK to include `running`

**Files:**
- Create: `migrations/supabase_014_agent_runs_status_add_running.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration: supabase_014_agent_runs_status_add_running.sql
-- Extend agent_runs.status CHECK constraint to include 'running' for the
-- calibration loop v1. 'running' is a transient state written by
-- predict_tool start-run and replaced by a terminal status (success, error,
-- timeout, budget_exceeded) by predict_tool finish-run.
--
-- Original constraint at supabase_002_agent_runs.sql:31
--   CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded'))
--
-- Idempotent: dropping by name then re-adding with a stable name.

ALTER TABLE public.agent_runs
  DROP CONSTRAINT IF EXISTS agent_runs_status_check;

ALTER TABLE public.agent_runs
  ADD CONSTRAINT agent_runs_status_check
  CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded', 'running'));
```

- [ ] **Step 2: Apply via MCP**

Run via the Supabase MCP tool `apply_migration` with `name="supabase_014_agent_runs_status_add_running"` and body = the SQL above.

Expected: migration succeeds. Verify with:
```
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid='public.agent_runs'::regclass AND conname='agent_runs_status_check';
```
Expected: `CHECK (status = ANY (ARRAY['success'::text, 'error'::text, 'timeout'::text, 'budget_exceeded'::text, 'running'::text]))`.

- [ ] **Step 3: Commit**

```bash
git add migrations/supabase_014_agent_runs_status_add_running.sql
git commit -m "migration 014: allow agent_runs.status='running' for calibration loop"
```

---

## Task 3: Add calibration constants to `web_settings.py`

**Files:**
- Modify: `web_settings.py` (append at end, before or after the HL WS block)

- [ ] **Step 1: Write a failing test**

Create `tests/test_calibration_settings.py`:

```python
"""Tests for calibration loop constants in web_settings.py."""
import pytest


def test_calibration_constants_exist():
    import web_settings as ws
    assert ws.CALIBRATION_WINDOW_DAYS == 30
    assert ws.CALIBRATION_MIN_SAMPLE == 10
    assert ws.CALIBRATION_CATASTROPHIC_THRESHOLD == 0.20
    assert ws.CALIBRATION_COVERAGE_THRESHOLD == 0.60
    assert ws.CALIBRATION_HIGH_CONF_THRESHOLD == 0.50


def test_calibration_enabled_agents_is_list_with_insider_copy():
    import web_settings as ws
    assert isinstance(ws.CALIBRATION_ENABLED_AGENTS, list)
    assert "insider_copy" in ws.CALIBRATION_ENABLED_AGENTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calibration_settings.py -v`
Expected: FAIL with `AttributeError: module 'web_settings' has no attribute 'CALIBRATION_WINDOW_DAYS'`.

- [ ] **Step 3: Append constants to `web_settings.py`**

Append at the very end of the file:

```python

# --- Calibration loop v1 (see docs/superpowers/specs/2026-04-20-calibration-loop-v1-design.md) ---
CALIBRATION_WINDOW_DAYS = 30
CALIBRATION_MIN_SAMPLE = 10
CALIBRATION_CATASTROPHIC_THRESHOLD = 0.20
CALIBRATION_COVERAGE_THRESHOLD = 0.60
CALIBRATION_HIGH_CONF_THRESHOLD = 0.50
CALIBRATION_ENABLED_AGENTS = ["insider_copy"]  # extended one-at-a-time per spec §7.2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_calibration_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_settings.py tests/test_calibration_settings.py
git commit -m "feat: calibration loop settings (6 constants)"
```

---

## Task 4: `predict_tool.cmd_calibration_stats` — JIT stats function

**Files:**
- Modify: `agents/tools/predict_tool.py` (add `cmd_calibration_stats` function + subparser in `main`)
- Create: `tests/test_predict_tool_calibration.py`

**Output contract** (reproduced verbatim from spec §5.3 so the implementer does not re-derive it):

```json
{
  "agent_type": "insider_copy",
  "window_days": 30,
  "window_start": "2026-03-21T...",
  "computed_at": "2026-04-20T...",
  "total": 55,
  "graded": 52,
  "ungraded": 3,
  "graded_coverage_pct": 94.5,
  "verification_coverage_pct": 100.0,
  "accuracy_pct": 55.8,
  "by_confidence": {
    "high":   {"total": 15, "graded": 14, "correct": 9, "accuracy_pct": 64.3},
    "medium": {"total": 30, "graded": 28, "correct": 15, "accuracy_pct": 53.6},
    "low":    {"total": 10, "graded": 10, "correct": 5, "accuracy_pct": 50.0}
  },
  "red_flags": [],
  "sample_warning": false
}
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_predict_tool_calibration.py`:

```python
"""Tests for predict_tool.cmd_calibration_stats — JIT calibration metrics."""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(agent="insider_copy", window_days=30, mock_rows=None):
    """Helper: call the stats function with a mocked _request response."""
    from agents.tools import predict_tool

    captured = {}
    def fake_request(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        return mock_rows or []

    with patch.object(predict_tool, "_request", side_effect=fake_request):
        result = predict_tool.cmd_calibration_stats_fn(agent, window_days)
    return result, captured


def test_empty_returns_sample_warning():
    result, _ = _run(mock_rows=[])
    assert result["total"] == 0
    assert result["graded"] == 0
    assert result["sample_warning"] is True
    assert "small_sample" in result["red_flags"]


def test_all_graded_no_red_flags():
    # 15 predictions, all graded, 9 correct = 60% accuracy; all have verification_query
    rows = []
    for i in range(15):
        rows.append({
            "id": f"uuid-{i}",
            "was_correct": i < 9,          # first 9 correct
            "confidence": "high" if i < 5 else ("medium" if i < 10 else "low"),
            "verification_query": "SELECT 1",
            "created_at": "2026-04-15T00:00:00+00:00",
        })
    result, _ = _run(mock_rows=rows)
    assert result["total"] == 15
    assert result["graded"] == 15
    assert result["graded_coverage_pct"] == 100.0
    assert result["verification_coverage_pct"] == 100.0
    assert 59.0 <= result["accuracy_pct"] <= 61.0
    assert result["sample_warning"] is False
    assert "small_sample" not in result["red_flags"]
    assert "catastrophic_accuracy" not in result["red_flags"]
    assert "low_verification_coverage" not in result["red_flags"]


def test_catastrophic_accuracy_flag():
    # 10 predictions, 1 correct = 10% accuracy, all have queries
    rows = [
        {"id": f"uuid-{i}", "was_correct": i == 0, "confidence": "medium",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
        for i in range(10)
    ]
    result, _ = _run(mock_rows=rows)
    assert result["accuracy_pct"] == 10.0
    assert "catastrophic_accuracy" in result["red_flags"]


def test_low_verification_coverage_flag():
    # 12 predictions, half with query, half without
    rows = []
    for i in range(12):
        rows.append({
            "id": f"uuid-{i}", "was_correct": i % 2 == 0, "confidence": "medium",
            "verification_query": "SELECT 1" if i < 4 else None,  # only 4/12 = 33%
            "created_at": "2026-04-15T00:00:00+00:00",
        })
    result, _ = _run(mock_rows=rows)
    assert result["verification_coverage_pct"] < 60.0
    assert "low_verification_coverage" in result["red_flags"]


def test_high_confidence_miscalibrated_flag():
    # high 0/5 = 0% ; medium 5/10 = 50%  → high.accuracy < 50 AND <= medium
    rows = []
    for i in range(5):
        rows.append({"id": f"h-{i}", "was_correct": False, "confidence": "high",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    for i in range(10):
        rows.append({"id": f"m-{i}", "was_correct": i < 5, "confidence": "medium",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    result, _ = _run(mock_rows=rows)
    assert "high_confidence_miscalibrated" in result["red_flags"]


def test_high_confidence_beats_medium_no_flag():
    # high 5/5 = 100%; medium 5/10 = 50%. Monotonic → no flag even though overall is 10/15 = 66.7
    rows = []
    for i in range(5):
        rows.append({"id": f"h-{i}", "was_correct": True, "confidence": "high",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    for i in range(10):
        rows.append({"id": f"m-{i}", "was_correct": i < 5, "confidence": "medium",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    result, _ = _run(mock_rows=rows)
    assert "high_confidence_miscalibrated" not in result["red_flags"]


def test_small_sample_flag_even_if_accuracy_high():
    # 4 graded, all correct = 100% accuracy but below MIN_SAMPLE=10
    rows = [
        {"id": f"uuid-{i}", "was_correct": True, "confidence": "high",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
        for i in range(4)
    ]
    result, _ = _run(mock_rows=rows)
    assert result["sample_warning"] is True
    assert "small_sample" in result["red_flags"]


def test_request_path_applies_eligibility_filter():
    """The Supabase query must filter to run_id NOT NULL and inner-join successful runs with turns>0."""
    rows = [
        {"id": "uuid-0", "was_correct": True, "confidence": "medium",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
    ]
    _, captured = _run(mock_rows=rows)
    path = captured["path"]
    # Must include run_id NOT NULL filter (run_id=not.is.null) and join-filter on agent_runs.
    assert "run_id=not.is.null" in path or "agent_runs" in path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_predict_tool_calibration.py -v`
Expected: FAIL with `AttributeError` or `ImportError` — function does not yet exist.

- [ ] **Step 3: Implement `cmd_calibration_stats_fn` + subcommand**

Add the following **before** `def cmd_stats(args):` in `agents/tools/predict_tool.py`:

```python
def _load_calibration_settings():
    """Load CALIBRATION_* constants from web_settings. Importable lazily so
    tests that stub web_settings don't break the module at import time."""
    try:
        import web_settings as ws
    except ImportError:
        class _Defaults:
            CALIBRATION_WINDOW_DAYS = 30
            CALIBRATION_MIN_SAMPLE = 10
            CALIBRATION_CATASTROPHIC_THRESHOLD = 0.20
            CALIBRATION_COVERAGE_THRESHOLD = 0.60
            CALIBRATION_HIGH_CONF_THRESHOLD = 0.50
            CALIBRATION_ENABLED_AGENTS = ["insider_copy"]
        return _Defaults()
    return ws


def cmd_calibration_stats_fn(agent_type: str, window_days: int = 30) -> dict:
    """Compute calibration stats JIT from agent_predictions joined to agent_runs.

    Eligibility filter (spec §5.2):
      - agent_predictions.run_id IS NOT NULL
      - parent agent_runs.status = 'success'
      - parent agent_runs.turns > 0

    Returns the dict described in spec §5.3.
    """
    ws = _load_calibration_settings()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    # Use PostgREST embedding + filter on the embedded resource to apply the
    # eligibility filter. agent_runs!run_id is the FK relationship; the
    # inner-join modifier '!inner' narrows rows to those with matching parents.
    # We also filter on parent status and turns via dot syntax.
    path = (
        f"agent_predictions"
        f"?select=id,was_correct,confidence,verification_query,created_at,run_id,"
        f"agent_runs!inner(status,turns)"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&run_id=not.is.null"
        f"&agent_runs.status=eq.success"
        f"&agent_runs.turns=gt.0"
        f"&created_at=gte.{urllib.parse.quote(window_start.isoformat())}"
        f"&order=created_at.desc"
        f"&limit=1000"
    )
    rows = _request("GET", path) or []

    # Aggregate
    total = len(rows)
    graded_rows = [r for r in rows if r.get("was_correct") is not None]
    graded = len(graded_rows)
    ungraded = total - graded
    correct = sum(1 for r in graded_rows if r.get("was_correct"))
    accuracy_pct = round(correct / graded * 100, 1) if graded else 0.0
    with_query = sum(1 for r in rows if r.get("verification_query"))
    verification_coverage_pct = round(with_query / total * 100, 1) if total else 0.0
    graded_coverage_pct = round(graded / total * 100, 1) if total else 0.0

    # Per-confidence breakdown
    by_conf: dict = {}
    for r in rows:
        c = r.get("confidence", "medium") or "medium"
        b = by_conf.setdefault(c, {"total": 0, "graded": 0, "correct": 0})
        b["total"] += 1
        if r.get("was_correct") is not None:
            b["graded"] += 1
            if r.get("was_correct"):
                b["correct"] += 1
    for c, b in by_conf.items():
        b["accuracy_pct"] = round(b["correct"] / b["graded"] * 100, 1) if b["graded"] else 0.0

    # Red flags
    red_flags: list[str] = []
    sample_warning = graded < ws.CALIBRATION_MIN_SAMPLE
    if sample_warning:
        red_flags.append("small_sample")
    if graded >= ws.CALIBRATION_MIN_SAMPLE and accuracy_pct < ws.CALIBRATION_CATASTROPHIC_THRESHOLD * 100:
        red_flags.append("catastrophic_accuracy")
    if total >= ws.CALIBRATION_MIN_SAMPLE and verification_coverage_pct < ws.CALIBRATION_COVERAGE_THRESHOLD * 100:
        red_flags.append("low_verification_coverage")

    # high_confidence_miscalibrated: monotonicity failure
    hc = by_conf.get("high")
    if hc and hc["graded"] >= 5 and hc["accuracy_pct"] < ws.CALIBRATION_HIGH_CONF_THRESHOLD * 100:
        mc = by_conf.get("medium")
        if mc and mc["graded"] >= 1:
            if hc["accuracy_pct"] <= mc["accuracy_pct"]:
                red_flags.append("high_confidence_miscalibrated")
        elif hc["accuracy_pct"] <= accuracy_pct:
            red_flags.append("high_confidence_miscalibrated")

    return {
        "agent_type": agent_type,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "computed_at": now.isoformat(),
        "total": total,
        "graded": graded,
        "ungraded": ungraded,
        "graded_coverage_pct": graded_coverage_pct,
        "verification_coverage_pct": verification_coverage_pct,
        "accuracy_pct": accuracy_pct,
        "by_confidence": by_conf,
        "red_flags": red_flags,
        "sample_warning": sample_warning,
    }


def cmd_calibration_stats(args):
    """CLI wrapper: emit JSON to stdout."""
    result = cmd_calibration_stats_fn(args.agent, args.window_days)
    print(json.dumps(result, indent=2))
```

Register the subcommand in `main()` before the dispatch dict:

```python
    p_cal = sub.add_parser("calibration-stats", help="Compute calibration stats JIT")
    p_cal.add_argument("--agent", required=True)
    p_cal.add_argument("--window-days", type=int, default=30)
```

And add to the dispatch dict:

```python
        "calibration-stats": cmd_calibration_stats,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_predict_tool_calibration.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/tools/predict_tool.py tests/test_predict_tool_calibration.py
git commit -m "feat: predict_tool calibration-stats JIT function + red-flag rules"
```

---

## Task 5: `predict_tool.cmd_start_run` — open an agent run with calibration_context

**Files:**
- Modify: `agents/tools/predict_tool.py` (add `cmd_start_run` + subparser)
- Create: `tests/test_predict_tool_start_run.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_predict_tool_start_run.py`:

```python
"""Tests for predict_tool.cmd_start_run — opens agent_runs row with calibration snapshot."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_start_run_posts_row_with_running_status_and_calibration_context(capsys):
    from agents.tools import predict_tool

    stats_stub = {
        "agent_type": "insider_copy",
        "total": 10, "graded": 10, "accuracy_pct": 60.0,
        "red_flags": [], "sample_warning": False,
    }
    inserted = {}

    def fake_request(method, path, body=None):
        if method == "POST" and path == "agent_runs":
            inserted["body"] = body
            return [{"id": "run-uuid-abc"}]
        pytest.fail(f"unexpected request: {method} {path}")

    with patch.object(predict_tool, "cmd_calibration_stats_fn", return_value=stats_stub), \
         patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            model = "claude-sonnet-4-6"
            window_days = 30
        predict_tool.cmd_start_run(Args())

    body = inserted["body"]
    assert body["agent_type"] == "insider_copy"
    assert body["status"] == "running"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["calibration_context"] == stats_stub

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["run_id"] == "run-uuid-abc"
    # Formatted calibration block must be present on stdout for wrapper capture
    assert "CALIBRATION STATE" in out["calibration_block"]


def test_start_run_block_lists_red_flags_when_present(capsys):
    from agents.tools import predict_tool

    stats_stub = {
        "agent_type": "insider_copy",
        "total": 18, "graded": 18, "accuracy_pct": 5.6,
        "red_flags": ["catastrophic_accuracy", "low_verification_coverage"],
        "sample_warning": False, "by_confidence": {},
        "verification_coverage_pct": 37.0, "graded_coverage_pct": 100.0,
    }

    def fake_request(method, path, body=None):
        return [{"id": "run-uuid-xyz"}]

    with patch.object(predict_tool, "cmd_calibration_stats_fn", return_value=stats_stub), \
         patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            model = None
            window_days = 30
        predict_tool.cmd_start_run(Args())

    out = json.loads(capsys.readouterr().out)
    block = out["calibration_block"]
    assert "catastrophic_accuracy" in block
    assert "low_verification_coverage" in block
    assert "RED FLAGS:" in block
    assert "none" not in block.lower()  # "RED FLAGS: none" must NOT appear when flags exist
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_predict_tool_start_run.py -v`
Expected: FAIL — `cmd_start_run` does not exist.

- [ ] **Step 3: Implement `cmd_start_run`**

Add the following **after** `cmd_calibration_stats` in `agents/tools/predict_tool.py`:

```python
def _format_calibration_block(stats: dict) -> str:
    """Render the calibration stats as the human-readable block the agent sees.

    Format is spec §6.2. Plain text, grep-able.
    """
    lines = []
    lines.append("## CALIBRATION STATE (last %d days)" % stats.get("window_days", 30))
    lines.append(
        "Predictions: %d total | %d graded (%.1f%%) | %d pending"
        % (stats.get("total", 0), stats.get("graded", 0),
           stats.get("graded_coverage_pct", 0.0),
           stats.get("total", 0) - stats.get("graded", 0))
    )
    lines.append("Overall accuracy: %.1f%%" % stats.get("accuracy_pct", 0.0))
    lines.append("Verification coverage: %.1f%%" % stats.get("verification_coverage_pct", 0.0))
    lines.append("")
    by_conf = stats.get("by_confidence", {}) or {}
    if by_conf:
        lines.append("By confidence:")
        for c in ("high", "medium", "low"):
            b = by_conf.get(c)
            if b:
                lines.append(
                    "  %-6s %d graded, %d correct (%.1f%%)"
                    % (c, b.get("graded", 0), b.get("correct", 0), b.get("accuracy_pct", 0.0))
                )
        lines.append("")

    flags = stats.get("red_flags", []) or []
    if flags:
        lines.append("RED FLAGS:")
        flag_rationales = {
            "catastrophic_accuracy":
                "%.1f%% is worse than useful; predictions likely hallucinated or grading broken"
                % stats.get("accuracy_pct", 0.0),
            "low_verification_coverage":
                "only %.1f%% auto-gradable; calibration itself is fragile"
                % stats.get("verification_coverage_pct", 0.0),
            "high_confidence_miscalibrated":
                'your "high" label does not monotonically beat other buckets',
            "small_sample":
                "only %d graded predictions — stats untrustable" % stats.get("graded", 0),
        }
        for f in flags:
            lines.append("  %-33s — %s" % (f, flag_rationales.get(f, "see spec")))
        lines.append("")
        lines.append("Before you make new predictions:")
        lines.append("  1. Provide verification_query for EVERY prediction (auto-gradable only)")
        lines.append("  2. Downgrade confidence unless you have stronger evidence than last time")
        lines.append("  3. If confidence=high, include calibration_note explaining why this is different")
    else:
        lines.append("RED FLAGS: none")
        lines.append("")
        lines.append("Before you make new predictions:")
        lines.append("  1. Provide verification_query for EVERY prediction (auto-gradable only)")
        lines.append("  2. Apply confidence monotonically: high > medium > low accuracy")
        lines.append("  3. If confidence=high, include calibration_note explaining why")

    return "\n".join(lines)


def cmd_start_run(args):
    """Open an agent_runs row with status='running' and calibration_context populated.

    Returns (via stdout JSON) the run UUID and the formatted calibration block
    for the wrapper to prepend to its prompt.
    """
    stats = cmd_calibration_stats_fn(args.agent, getattr(args, "window_days", 30))
    block = _format_calibration_block(stats)

    row = {
        "agent_type": args.agent,
        "status": "running",
        "calibration_context": stats,
    }
    if getattr(args, "model", None):
        row["model"] = args.model

    result = _request("POST", "agent_runs", row)
    run_id = result[0]["id"] if result else "unknown"

    print(json.dumps({
        "ok": True,
        "run_id": run_id,
        "calibration_block": block,
        "calibration_stats": stats,
    }, indent=2))
```

Register in `main()`:

```python
    p_start = sub.add_parser("start-run", help="Open an agent run with calibration snapshot")
    p_start.add_argument("--agent", required=True)
    p_start.add_argument("--model", default=None)
    p_start.add_argument("--window-days", type=int, default=30)
```

Dispatch dict:

```python
        "start-run": cmd_start_run,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_predict_tool_start_run.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/tools/predict_tool.py tests/test_predict_tool_start_run.py
git commit -m "feat: predict_tool start-run opens run + renders calibration block"
```

---

## Task 6: `predict_tool.cmd_finish_run` — close an agent run with terminal status

**Files:**
- Modify: `agents/tools/predict_tool.py`
- Create: `tests/test_predict_tool_finish_run.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_predict_tool_finish_run.py`:

```python
"""Tests for predict_tool.cmd_finish_run — closes agent_runs row with terminal status."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_finish_run_patches_row_with_terminal_status(capsys):
    from agents.tools import predict_tool

    patched = {}
    def fake_request(method, path, body=None):
        patched["method"] = method
        patched["path"] = path
        patched["body"] = body
        return [{"id": "run-uuid-abc"}]

    # Pretend the parent row exists and was created recently
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            run_id = "run-uuid-abc"
            status = "success"
            turns = 28
            cost_usd = 0.95
            findings_summary = "3 strategies healthy"
        predict_tool.cmd_finish_run(Args())

    assert patched["method"] == "PATCH"
    assert "agent_runs" in patched["path"]
    assert "id=eq.run-uuid-abc" in patched["path"]
    body = patched["body"]
    assert body["status"] == "success"
    assert body["turns"] == 28
    assert body["cost_usd"] == 0.95
    assert body["findings_summary"] == "3 strategies healthy"
    # duration_seconds is computed server-side by finish-run; must be present and >= 0
    assert "duration_seconds" in body
    assert body["duration_seconds"] is None or body["duration_seconds"] >= 0
    # Must NOT write accuracy_pct (spec §6.4)
    assert "accuracy_pct" not in body


def test_finish_run_rejects_invalid_status():
    from agents.tools import predict_tool

    class Args:
        run_id = "run-uuid-abc"
        status = "partial"  # not in allow-list
        turns = None
        cost_usd = None
        findings_summary = None

    with pytest.raises(SystemExit) as exc_info:
        predict_tool.cmd_finish_run(Args())
    assert exc_info.value.code == 2


def test_finish_run_accepts_all_terminal_statuses():
    """success, error, timeout, budget_exceeded are all valid. 'running' is NOT."""
    from agents.tools import predict_tool

    def fake_request(method, path, body=None):
        return [{"id": "r"}]

    with patch.object(predict_tool, "_request", side_effect=fake_request):
        for status in ("success", "error", "timeout", "budget_exceeded"):
            class Args:
                pass
            a = Args()
            a.run_id = "r"
            a.status = status
            a.turns = None
            a.cost_usd = None
            a.findings_summary = None
            # Should not raise
            predict_tool.cmd_finish_run(a)

        # 'running' must be rejected
        class RunningArgs:
            run_id = "r"; status = "running"; turns = None; cost_usd = None; findings_summary = None
        with pytest.raises(SystemExit):
            predict_tool.cmd_finish_run(RunningArgs())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_predict_tool_finish_run.py -v`
Expected: FAIL — `cmd_finish_run` does not exist.

- [ ] **Step 3: Implement `cmd_finish_run`**

Add **after** `cmd_start_run` in `agents/tools/predict_tool.py`:

```python
_TERMINAL_STATUSES = {"success", "error", "timeout", "budget_exceeded"}


def cmd_finish_run(args):
    """Close an agent_runs row opened by start-run with a terminal status.

    Does NOT write accuracy_pct (schema-drift column; see spec §6.4 + §9 risk 6).
    """
    if args.status not in _TERMINAL_STATUSES:
        print(json.dumps({
            "ok": False,
            "error": f"status must be one of {sorted(_TERMINAL_STATUSES)}, got {args.status!r}",
        }))
        sys.exit(2)

    # Fetch the row's created_at so we can compute duration_seconds server-side.
    duration_seconds = None
    parent = _request("GET", f"agent_runs?select=created_at&id=eq.{args.run_id}&limit=1") or []
    if parent:
        created_raw = parent[0].get("created_at")
        try:
            created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            duration_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        except Exception:
            duration_seconds = None

    body = {"status": args.status}
    if args.turns is not None:
        body["turns"] = args.turns
    if args.cost_usd is not None:
        body["cost_usd"] = args.cost_usd
    if args.findings_summary is not None:
        body["findings_summary"] = args.findings_summary
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    else:
        body["duration_seconds"] = None

    _request("PATCH", f"agent_runs?id=eq.{args.run_id}", body)
    print(json.dumps({"ok": True, "run_id": args.run_id, "status": args.status}, indent=2))
```

Register in `main()`:

```python
    p_finish = sub.add_parser("finish-run", help="Close an agent run with terminal status")
    p_finish.add_argument("--run-id", required=True, dest="run_id")
    p_finish.add_argument("--status", required=True,
                          choices=["success", "error", "timeout", "budget_exceeded"])
    p_finish.add_argument("--turns", type=int, default=None)
    p_finish.add_argument("--cost-usd", type=float, default=None, dest="cost_usd")
    p_finish.add_argument("--findings-summary", default=None, dest="findings_summary")
```

Dispatch dict:

```python
        "finish-run": cmd_finish_run,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_predict_tool_finish_run.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/tools/predict_tool.py tests/test_predict_tool_finish_run.py
git commit -m "feat: predict_tool finish-run closes run with terminal status"
```

---

## Task 7: Upgrade `cmd_predict` with `--run-id` + `--calibration-note`

**Files:**
- Modify: `agents/tools/predict_tool.py` (`cmd_predict` at line 145 + its subparser in `main`)
- Create: `tests/test_predict_tool_predict_upgrade.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_predict_tool_predict_upgrade.py`:

```python
"""Tests for the upgraded cmd_predict — adds --run-id + --calibration-note."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _valid_envelope(red_flags=None, rationale=None):
    cal = {
        "saw_red_flags": red_flags or [],
        "verification_query_provided": True,
    }
    if rationale is not None:
        cal["confidence_rationale"] = rationale
    return {"schema_version": 1, "calibration": cal}


def test_predict_with_run_id_writes_run_id_column():
    from agents.tools import predict_tool

    posted = {}
    def fake_request(method, path, body=None):
        if method == "POST" and path == "agent_predictions":
            posted["body"] = body
            return [{"id": "pred-uuid-1"}]
        return []

    env = _valid_envelope()
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            prediction = "Wallet X score stays >=70 for 3d"
            condition = "Check user_analysis"
            confidence = "medium"
            verify_hours = 48
            verify_at = None
            query = "SELECT insider_score FROM user_analysis WHERE wallet='0xabc'"
            context = None
            run_id = "run-uuid-abc"
            calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())

    assert posted["body"]["run_id"] == "run-uuid-abc"
    # Envelope lands in context_snapshot as JSON text
    snap = json.loads(posted["body"]["context_snapshot"])
    assert snap["schema_version"] == 1
    assert snap["calibration"]["verification_query_provided"] is True


def test_predict_rejects_calibration_note_missing_schema_version():
    from agents.tools import predict_tool

    bad = json.dumps({"calibration": {"saw_red_flags": [], "verification_query_provided": True}})
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = bad
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_rejects_high_confidence_without_rationale():
    from agents.tools import predict_tool

    # High confidence + no rationale = invalid per spec §6.3
    env = _valid_envelope()  # no rationale
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "high"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_rejects_red_flags_present_without_rationale():
    from agents.tools import predict_tool

    env = _valid_envelope(red_flags=["catastrophic_accuracy"])  # flags but no rationale
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_accepts_high_confidence_with_rationale():
    from agents.tools import predict_tool

    env = _valid_envelope(rationale="first high-conf call this week")
    def fake_request(method, path, body=None):
        return [{"id": "p"}]
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "high"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        # Should not raise
        predict_tool.cmd_predict(Args())


def test_predict_without_run_id_still_works_legacy_mode():
    """Backward compat: CLI calls without --run-id keep working (writes NULL)."""
    from agents.tools import predict_tool

    posted = {}
    def fake_request(method, path, body=None):
        if method == "POST":
            posted["body"] = body
            return [{"id": "p"}]
        return []
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = None
            calibration_note = None
        predict_tool.cmd_predict(Args())
    # Body should not contain run_id (or should contain None)
    assert posted["body"].get("run_id") in (None, "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_predict_tool_predict_upgrade.py -v`
Expected: FAIL — `--run-id` and `--calibration-note` not supported yet.

- [ ] **Step 3: Upgrade `cmd_predict`**

Replace `cmd_predict` at `agents/tools/predict_tool.py:145` (the whole function) with:

```python
def _validate_calibration_note(raw: str, confidence: str) -> str:
    """Parse + validate the envelope per spec §6.3. Returns the serialized JSON
    (re-stringified to guarantee canonical form) or raises SystemExit(2)."""
    try:
        env = json.loads(raw)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"calibration-note is not valid JSON: {exc}"}))
        sys.exit(2)
    if not isinstance(env, dict) or "schema_version" not in env or "calibration" not in env:
        print(json.dumps({"ok": False, "error": "calibration-note missing schema_version or calibration"}))
        sys.exit(2)
    cal = env.get("calibration")
    if not isinstance(cal, dict):
        print(json.dumps({"ok": False, "error": "calibration must be an object"}))
        sys.exit(2)
    if "saw_red_flags" not in cal or "verification_query_provided" not in cal:
        print(json.dumps({"ok": False,
                          "error": "calibration missing saw_red_flags or verification_query_provided"}))
        sys.exit(2)
    saw = cal.get("saw_red_flags") or []
    rationale_required = bool(saw) or confidence == "high"
    if rationale_required and not cal.get("confidence_rationale"):
        print(json.dumps({"ok": False,
                          "error": "confidence_rationale required when saw_red_flags non-empty or confidence=high"}))
        sys.exit(2)
    return json.dumps(env)


def cmd_predict(args):
    """Make a verifiable prediction. Writes run_id when --run-id is provided."""
    try:
        verify_at = _resolve_verify_after(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(2)

    # Determine context_snapshot source:
    # - If --calibration-note provided, validate + use it (v1 wrapper-initiated path).
    # - Else, fall back to --context (legacy behavior).
    context_snapshot = None
    run_id = getattr(args, "run_id", None)
    cal_note = getattr(args, "calibration_note", None)

    if run_id and not cal_note:
        print(json.dumps({"ok": False, "error": "--calibration-note is required when --run-id is provided"}))
        sys.exit(2)

    if cal_note:
        context_snapshot = _validate_calibration_note(cal_note, args.confidence)
    else:
        context_snapshot = getattr(args, "context", None)

    row = {
        "agent_type": args.agent,
        "prediction": args.prediction,
        "condition": args.condition,
        "confidence": args.confidence,
        "verify_after": verify_at,
        "verification_query": args.query,
        "context_snapshot": context_snapshot,
    }
    if run_id:
        row["run_id"] = run_id

    result = _request("POST", "agent_predictions", row)
    pred_id = result[0]["id"] if result else "unknown"
    output = {
        "ok": True,
        "id": pred_id,
        "verify_after": verify_at,
        "message": f"Prediction logged. Will be graded after {verify_at}.",
    }
    if run_id:
        output["run_id"] = run_id
    if not args.query:
        output["manual_grade_required"] = True
        output["warning"] = "No --query provided. This prediction requires manual grading and cannot be auto-graded."
    print(json.dumps(output, indent=2))
```

Add to the existing `p_predict` subparser in `main()` (right after the `--context` argument):

```python
    p_predict.add_argument("--run-id", dest="run_id", default=None,
                           help="Parent agent_runs UUID. Required for wrapper-initiated predictions.")
    p_predict.add_argument("--calibration-note", dest="calibration_note", default=None,
                           help="JSON envelope (spec §6.3). Required when --run-id is provided.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_predict_tool_predict_upgrade.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Run full predict_tool test suite to check no regression**

Run: `python3 -m pytest tests/test_predict_tool.py tests/test_predict_tool_calibration.py tests/test_predict_tool_start_run.py tests/test_predict_tool_finish_run.py tests/test_predict_tool_predict_upgrade.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/tools/predict_tool.py tests/test_predict_tool_predict_upgrade.py
git commit -m "feat: predict_tool predict accepts --run-id + --calibration-note envelope"
```

---

## Task 8: Historical linkage backfill script

**Files:**
- Create: `scripts/backfill_agent_predictions_run_id.py`
- Create: `tests/test_backfill_agent_predictions_run_id.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_backfill_agent_predictions_run_id.py`:

```python
"""Tests for scripts/backfill_agent_predictions_run_id.py — one-shot linkage backfill."""
import json
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_match_exactly_one_updates_row():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_ts = now - timedelta(hours=1, minutes=2)

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [{"id": "r-1", "created_at": run_ts.isoformat()}]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH" and "agent_predictions" in path:
            patched.append((path, body))
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["matched"] == 1
    assert result["ambiguous"] == 0
    assert result["orphaned"] == 0
    assert len(patched) == 1
    assert "id=eq.p-1" in patched[0][0]
    assert patched[0][1] == {"run_id": "r-1"}


def test_multi_match_skips_as_ambiguous():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_a = now - timedelta(hours=1, minutes=2)
    run_b = now - timedelta(hours=1, minutes=30)  # still within 1h window

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [
        {"id": "r-a", "created_at": run_a.isoformat()},
        {"id": "r-b", "created_at": run_b.isoformat()},
    ]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH":
            patched.append(True)
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["matched"] == 0
    assert result["ambiguous"] == 1
    assert len(patched) == 0  # no UPDATE issued


def test_zero_match_skips_as_orphaned():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_too_old = now - timedelta(hours=10)  # outside 1h window

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [{"id": "r-old", "created_at": run_too_old.isoformat()}]

    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["orphaned"] == 1
    assert result["matched"] == 0


def test_rejects_agent_not_in_allow_list(capsys):
    import scripts.backfill_agent_predictions_run_id as bf

    with pytest.raises(SystemExit) as exc_info:
        bf.backfill("wallet_scout")  # not in CALIBRATION_ENABLED_AGENTS for v1
    assert exc_info.value.code == 2
    err = capsys.readouterr().out + capsys.readouterr().err
    assert "wallet_scout" in err or "not in CALIBRATION_ENABLED_AGENTS" in err


def test_does_not_overwrite_existing_run_id():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_ts = now - timedelta(hours=1, minutes=2)

    # Prediction already has a run_id — must be skipped
    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": "existing-uuid"}]
    runs = [{"id": "r-1", "created_at": run_ts.isoformat()}]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH":
            patched.append(True)
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert len(patched) == 0
    assert result["already_linked"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_backfill_agent_predictions_run_id.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write the backfill script**

Create `scripts/backfill_agent_predictions_run_id.py`:

```python
#!/usr/bin/env python3
"""One-shot backfill: link historical agent_predictions to their parent agent_runs.

Required for calibration loop v1 eligibility filter (spec §5.2.1). Reads only
predictions with run_id IS NULL; matches to runs by (agent_type, created_at)
within a 1h bracket window; updates only on unambiguous single-match.

Scoped to CALIBRATION_ENABLED_AGENTS in web_settings.py — rejects any other
agent_type to prevent wasted ambiguity work on out-of-scope agents.

Usage:
    python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy
    python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy --dry-run

Exit codes:
    0 — success
    2 — agent_type not in CALIBRATION_ENABLED_AGENTS allow-list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")

# 1h window bracketing the prediction's created_at — predictions are emitted
# during a run that started earlier. Spec §5.2.1.
WINDOW_SECONDS = 3600


def _request(method: str, path: str, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()) if resp.length != 0 else []


def _parse_ts(value: str):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def backfill(agent_type: str, dry_run: bool = False) -> dict:
    """Run the backfill for a single agent_type. Returns stats dict."""
    try:
        import web_settings as ws
        enabled = ws.CALIBRATION_ENABLED_AGENTS
    except Exception:
        enabled = ["insider_copy"]

    if agent_type not in enabled:
        print(json.dumps({
            "ok": False,
            "error": f"agent_type {agent_type!r} not in CALIBRATION_ENABLED_AGENTS ({enabled}); "
                     f"add it to web_settings before running backfill",
        }))
        sys.exit(2)

    # Fetch all predictions for the agent (we filter run_id IS NULL locally so
    # we can also count already-linked rows for reporting).
    preds_path = (
        f"agent_predictions?select=id,created_at,run_id"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&order=created_at.asc&limit=10000"
    )
    preds = _request("GET", preds_path) or []

    # Fetch all runs for the agent — bounded because only one agent at a time
    # and ~hundreds of rows typical.
    runs_path = (
        f"agent_runs?select=id,created_at"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&order=created_at.asc&limit=10000"
    )
    runs = _request("GET", runs_path) or []

    matched = 0
    ambiguous = 0
    orphaned = 0
    already_linked = 0

    for p in preds:
        if p.get("run_id"):
            already_linked += 1
            continue
        try:
            p_ts = _parse_ts(p["created_at"])
        except Exception:
            orphaned += 1
            continue

        # Match runs whose created_at is in [p_ts - 1h, p_ts].
        candidates = []
        for r in runs:
            try:
                r_ts = _parse_ts(r["created_at"])
            except Exception:
                continue
            delta = (p_ts - r_ts).total_seconds()
            if 0 <= delta <= WINDOW_SECONDS:
                candidates.append(r)

        if len(candidates) == 1:
            if not dry_run:
                _request("PATCH",
                         f"agent_predictions?id=eq.{p['id']}",
                         body={"run_id": candidates[0]["id"]})
            matched += 1
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            orphaned += 1

    return {
        "ok": True,
        "agent_type": agent_type,
        "dry_run": dry_run,
        "matched": matched,
        "ambiguous": ambiguous,
        "orphaned": orphaned,
        "already_linked": already_linked,
        "total_scanned": len(preds),
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill agent_predictions.run_id")
    parser.add_argument("--agent-type", required=True,
                        help="Only processes agents in CALIBRATION_ENABLED_AGENTS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report matches without issuing UPDATEs.")
    args = parser.parse_args()
    result = backfill(args.agent_type, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_backfill_agent_predictions_run_id.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_agent_predictions_run_id.py tests/test_backfill_agent_predictions_run_id.py
git commit -m "feat: backfill_agent_predictions_run_id.py (one-shot linkage)"
```

---

## Task 9: Wire `insider_copy_agent.py` wrapper to `start-run` / `finish-run`

**Files:**
- Modify: `agents/insider_copy_agent.py` (replace the `run` function)

The wrapper must:
1. Call `predict_tool start-run --agent insider_copy` before the SDK query, capture the run UUID and the calibration block.
2. Prepend the calibration block to the initial user prompt.
3. After the SDK session ends, call `predict_tool finish-run --run-id <uuid> --status success|error ...`.
4. On any exception, call finish-run with `--status error` to guarantee the row closes out.

- [ ] **Step 1: Write failing tests**

Create `tests/test_insider_copy_wrapper.py`:

```python
"""Tests for insider_copy_agent.py wrapper lifecycle: start-run → query → finish-run."""
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_wrapper_invokes_start_run_finish_run(monkeypatch):
    """Wrapper must: call start-run → prepend block to prompt → query → call finish-run."""
    from agents import insider_copy_agent as mod

    called = {"start": False, "finish": False, "prompt_with_block": None}

    def fake_run_predict_tool(args: list[str]) -> dict:
        if "start-run" in args:
            called["start"] = True
            return {
                "ok": True,
                "run_id": "run-uuid-xyz",
                "calibration_block": "## CALIBRATION STATE (last 30 days)\n...\nRED FLAGS: none",
                "calibration_stats": {"graded": 20, "accuracy_pct": 55.0},
            }
        if "finish-run" in args:
            called["finish"] = True
            called["finish_args"] = args
            return {"ok": True, "run_id": "run-uuid-xyz", "status": "success"}
        raise AssertionError(f"unexpected tool call: {args}")

    async def fake_query(prompt, options):
        # Capture prompt so we can assert the calibration block is prepended.
        called["prompt_with_block"] = prompt
        # Yield a ResultMessage-like object.
        class _Sys:
            subtype = "init"
            data = {"session_id": "s-1"}
        class _Result:
            result = "done"
            session_id = "s-1"
            stop_reason = "end_turn"
            total_cost_usd = 0.50
        # Importing real classes to make isinstance work:
        from claude_agent_sdk import SystemMessage, ResultMessage
        yield SystemMessage(subtype="init", data={"session_id": "s-1"})
        yield ResultMessage(result="done", session_id="s-1", stop_reason="end_turn",
                            total_cost_usd=0.50, num_turns=10, duration_ms=1000,
                            duration_api_ms=800, total_tokens=None)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "query", fake_query)

    import anyio
    anyio.run(mod.run)

    assert called["start"] is True
    assert called["finish"] is True
    # Block must appear at the top of the prompt shown to the SDK
    assert called["prompt_with_block"].startswith("## CALIBRATION STATE")
    # Run id must be passed through to finish-run
    finish = called["finish_args"]
    assert "--run-id" in finish and "run-uuid-xyz" in finish
    assert "--status" in finish and "success" in finish


def test_wrapper_finish_with_error_on_exception(monkeypatch):
    """If query raises, wrapper must still call finish-run with status=error."""
    from agents import insider_copy_agent as mod

    called = {"finish_status": None}

    def fake_run_predict_tool(args: list[str]) -> dict:
        if "start-run" in args:
            return {
                "ok": True, "run_id": "r",
                "calibration_block": "## CALIBRATION STATE\n",
                "calibration_stats": {},
            }
        if "finish-run" in args:
            # Locate --status value
            i = args.index("--status")
            called["finish_status"] = args[i + 1]
            return {"ok": True}
        raise AssertionError(args)

    async def failing_query(prompt, options):
        raise RuntimeError("SDK crashed")
        yield  # pragma: no cover  (satisfies async generator contract)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "query", failing_query)

    import anyio
    with pytest.raises(RuntimeError):
        anyio.run(mod.run)

    assert called["finish_status"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insider_copy_wrapper.py -v`
Expected: FAIL — `_run_predict_tool` does not exist; `run()` doesn't use start-run/finish-run.

- [ ] **Step 3: Modify `agents/insider_copy_agent.py`**

Replace the `run` function (lines 355–386) with the following, and add a helper above it:

```python
import subprocess
import sys


def _run_predict_tool(args: list[str]) -> dict:
    """Invoke predict_tool.py as a subprocess and parse its JSON stdout.

    Any non-zero exit propagates as RuntimeError. The caller handles
    user-visible messaging (we're writing to agent audit trail, not Telegram).
    """
    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, str(root / "agents" / "tools" / "predict_tool.py"), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(root))
    if proc.returncode != 0:
        raise RuntimeError(f"predict_tool {args[0] if args else ''} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"predict_tool stdout not JSON: {proc.stdout[:500]}") from exc


async def run(custom_prompt: str | None = None, verbose: bool = False) -> str:
    """Run the InsiderCopy agent with calibration loop wiring.

    Lifecycle:
      1. start-run (gets run_id + calibration block)
      2. prepend calibration block to the user prompt
      3. query() the SDK
      4. finish-run (status=success or status=error on exception)
    """
    cwd = str(Path(__file__).resolve().parent.parent)

    # Step 1: open run
    start = _run_predict_tool(["start-run", "--agent", "insider_copy",
                               "--model", "claude-sonnet-4-6"])
    run_id = start["run_id"]
    calibration_block = start["calibration_block"]

    base_prompt = custom_prompt or build_run_prompt()
    prompt = f"{calibration_block}\n\n---\n\n{base_prompt}"

    result_text = ""
    session_id = None
    turns = 0
    cost_usd = 0.0
    status = "success"
    findings = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                model="claude-sonnet-4-6",
                cwd=cwd,
                allowed_tools=["WebSearch", "WebFetch", "Bash", "Read"],
                permission_mode="bypassPermissions",
                system_prompt=SYSTEM_PROMPT,
                max_turns=60,
                setting_sources=[],
            ),
        ):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id") if isinstance(message.data, dict) else None
                if verbose:
                    print(f"[agent] Session: {session_id}")
            elif isinstance(message, ResultMessage):
                result_text = message.result or ""
                session_id = message.session_id
                cost_usd = float(message.total_cost_usd or 0.0)
                turns = int(getattr(message, "num_turns", 0) or 0)
                findings = (result_text[:500] if result_text else None)
                if verbose:
                    print(f"[agent] Stop reason: {message.stop_reason}, cost: ${cost_usd:.4f}")
    except BaseException:
        status = "error"
        raise
    finally:
        finish_args = ["finish-run", "--run-id", run_id, "--status", status]
        if turns:
            finish_args.extend(["--turns", str(turns)])
        if cost_usd:
            finish_args.extend(["--cost-usd", f"{cost_usd:.4f}"])
        if findings:
            finish_args.extend(["--findings-summary", findings])
        try:
            _run_predict_tool(finish_args)
        except Exception as exc:
            # Never swallow the original exception. Log and move on.
            print(f"[agent] warning: finish-run failed: {exc}", file=sys.stderr)

    return result_text
```

You do **not** need to add any new imports beyond `subprocess`, `sys`, and `json` at the module level (all already transitively available but explicit imports are cleaner — add `import subprocess`, `import sys`, `import json` at the top of the module if not already present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_insider_copy_wrapper.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/insider_copy_agent.py tests/test_insider_copy_wrapper.py
git commit -m "feat: insider_copy wrapper — start-run/finish-run lifecycle with calibration block"
```

---

## Task 10: Update `insider_copy_agent.py` system prompt for calibration-note

**Files:**
- Modify: `agents/insider_copy_agent.py` — `SYSTEM_PROMPT` block and the STEP 8 block in `build_run_prompt`.

Goals (exactly these changes, no more):
1. System prompt: new section describing the `--calibration-note` requirement and the envelope shape.
2. STEP 8 in `build_run_prompt`: predict invocation must include `--run-id` and `--calibration-note`.

- [ ] **Step 1: Add a calibration-note section to `SYSTEM_PROMPT`**

In `agents/insider_copy_agent.py`, find the section `═══ CRITICAL RULES ═══` (around line 191) and **insert immediately before it**:

```
═══ CALIBRATION ENVELOPE (required with every prediction this run) ═══

At the top of this run's prompt you saw a CALIBRATION STATE block. Every prediction
you make must be accompanied by a --calibration-note flag carrying this JSON envelope:

  {
    "schema_version": 1,
    "calibration": {
      "saw_red_flags": [<list of red-flag names from the block, or empty []>],
      "confidence_rationale": "<required if saw_red_flags non-empty OR confidence=high>",
      "differs_from_prior": "<optional: what makes this call different from recent misses>",
      "verification_query_provided": <true|false>
    }
  }

Pass it as a single-quoted argument to predict_tool predict:
  python3 agents/tools/predict_tool.py predict \
    --agent insider_copy \
    --run-id $RUN_ID \
    --calibration-note '{"schema_version":1,"calibration":{...}}' \
    --prediction "..." \
    --condition "..." \
    --confidence medium \
    --verify-hours 48 \
    --query "SELECT ..."

$RUN_ID is exported by the wrapper into your environment. If it is empty the wrapper
is broken — skip this run's prediction step rather than logging an orphaned prediction.

If red flags are present in the block you saw, you MUST either:
  (a) downgrade confidence from high to medium/low, AND explain why in confidence_rationale; or
  (b) keep confidence=high ONLY if differs_from_prior convincingly argues this query targets
      different data than the ones that historically failed.

```

- [ ] **Step 2: Update STEP 8 in `build_run_prompt` to require `--run-id` and `--calibration-note`**

Find the STEP 8 block (starts with `STEP 8 — Make predictions`) in `build_run_prompt()` and replace it with:

```
STEP 8 — Make predictions (exactly 1 high-quality prediction):
  Make exactly 1 high-quality verifiable prediction about what will happen in the next 48-72 hours.

  REQUIRED FLAGS for this run (calibration loop v1 is active for insider_copy):
    --run-id "$RUN_ID"                         (from wrapper env; empty means wrapper is broken → skip predict)
    --calibration-note '{"schema_version":1,"calibration":{...}}'   (spec §6.3 envelope)
    --query "SELECT ..."                       (auto-gradable; without this the prediction is wasted)

  Red-flag handling:
    If CALIBRATION STATE block showed any red flags, your calibration.saw_red_flags must list them
    AND you must EITHER downgrade confidence OR provide differs_from_prior explaining why this
    prediction is structurally different from the failing ones.

  Good predictions are SPECIFIC, VERIFIABLE, and have --query:
    ✓ "Wallet 0xde9f insider_score will stay above 70 for 3 days" (verify via SQL)
    ✓ "Strategy #7 will have positive PnL in next 48h" (verify via strategy_positions)
    ✗ "Markets will be volatile" (too vague, not verifiable)
    ✗ Any prediction without --query (permanently ungradeable)

  Example:
    python3 agents/tools/predict_tool.py predict \
      --agent insider_copy \
      --run-id "$RUN_ID" \
      --calibration-note '{"schema_version":1,"calibration":{"saw_red_flags":[],"verification_query_provided":true}}' \
      --prediction "Wallet 0xde9f insider_score stays >= 70 for 3 days" \
      --condition "Check user_analysis.insider_score for wallet 0xde9f" \
      --confidence medium \
      --verify-hours 72 \
      --query "SELECT insider_score FROM user_analysis WHERE wallet LIKE '0xde9f%'"
```

- [ ] **Step 3: Pass `RUN_ID` into the SDK session's bash env**

Modify the `run()` function you wrote in Task 9: when constructing `ClaudeAgentOptions`, the SDK does not accept arbitrary env injection, but the prompt uses `"$RUN_ID"`. The simplest reliable mechanism is to inline the actual UUID into the prompt text **before** handing it to the SDK.

Replace this line in `run()`:

```python
    prompt = f"{calibration_block}\n\n---\n\n{base_prompt}"
```

With:

```python
    # Inline RUN_ID directly into the prompt so Bash steps can reference it literally.
    prompt_with_run_id = base_prompt.replace('"$RUN_ID"', f'"{run_id}"').replace("$RUN_ID", run_id)
    prompt = f"{calibration_block}\n\n---\n\n{prompt_with_run_id}"
```

- [ ] **Step 4: Write a test that the prompt contains the inlined run_id**

Add to `tests/test_insider_copy_wrapper.py`:

```python
def test_wrapper_inlines_run_id_into_prompt(monkeypatch):
    from agents import insider_copy_agent as mod

    captured = {}

    def fake_run_predict_tool(args):
        if "start-run" in args:
            return {"ok": True, "run_id": "RID-XYZ", "calibration_block": "## CALIBRATION STATE\n", "calibration_stats": {}}
        return {"ok": True}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        from claude_agent_sdk import ResultMessage
        yield ResultMessage(result="done", session_id="s", stop_reason="end_turn",
                            total_cost_usd=0.0, num_turns=1, duration_ms=1, duration_api_ms=1, total_tokens=None)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "query", fake_query)

    import anyio
    anyio.run(mod.run)

    # The prompt the SDK receives must have the literal run_id, not the $RUN_ID placeholder.
    assert "$RUN_ID" not in captured["prompt"]
    assert "RID-XYZ" in captured["prompt"]
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_insider_copy_wrapper.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/insider_copy_agent.py tests/test_insider_copy_wrapper.py
git commit -m "feat: insider_copy prompt — require --run-id + --calibration-note per prediction"
```

---

## Task 11: Brain health `CAL1` check — scoped to `CALIBRATION_ENABLED_AGENTS`

**Files:**
- Modify: `scripts/brain_health.py` (add `check_cal1_calibration_context_populated`, add to `ALL_CHECKS` list)
- Modify: `tests/test_brain_health.py` (bump `test_all_checks_registered` to 45; add CAL1-specific test)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_brain_health.py`:

```python
# ══════════════════════════════════════════════════════════════════════════
# CAL1 — calibration_context populated for rollout-enabled agents
# ══════════════════════════════════════════════════════════════════════════

class TestCAL1:
    def test_pass_when_recent_run_has_calibration_context(self):
        from scripts import brain_health as bh
        from datetime import datetime, timezone

        recent = datetime.now(timezone.utc).isoformat()

        def fake_sb_get(path):
            if "agent_runs" in path and "insider_copy" in path:
                return [{
                    "id": "r1", "agent_type": "insider_copy",
                    "created_at": recent,
                    "calibration_context": {"accuracy_pct": 55.0, "red_flags": []},
                }]
            return []

        from unittest.mock import patch
        report = bh.HealthReport(timestamp="t")
        with patch.object(bh, "sb_get", side_effect=fake_sb_get), \
             patch.object(bh, "CALIBRATION_ENABLED_AGENTS", ["insider_copy"], create=True):
            bh.check_cal1_calibration_context_populated(report)

        cal1 = [c for c in report.checks if c.check_id == "CAL1"][0]
        assert cal1.severity == "PASS"

    def test_warn_when_recent_run_missing_calibration_context(self):
        from scripts import brain_health as bh
        from datetime import datetime, timezone

        recent = datetime.now(timezone.utc).isoformat()

        def fake_sb_get(path):
            if "agent_runs" in path and "insider_copy" in path:
                return [{"id": "r1", "agent_type": "insider_copy",
                         "created_at": recent, "calibration_context": None}]
            return []

        from unittest.mock import patch
        report = bh.HealthReport(timestamp="t")
        with patch.object(bh, "sb_get", side_effect=fake_sb_get), \
             patch.object(bh, "CALIBRATION_ENABLED_AGENTS", ["insider_copy"], create=True):
            bh.check_cal1_calibration_context_populated(report)

        cal1 = [c for c in report.checks if c.check_id == "CAL1"][0]
        assert cal1.severity == "WARN"

    def test_ignores_agents_not_in_allow_list(self):
        """Silently ignores agents outside CALIBRATION_ENABLED_AGENTS."""
        from scripts import brain_health as bh
        from unittest.mock import patch

        report = bh.HealthReport(timestamp="t")
        with patch.object(bh, "sb_get", return_value=[]), \
             patch.object(bh, "CALIBRATION_ENABLED_AGENTS", [], create=True):
            bh.check_cal1_calibration_context_populated(report)

        # When no agents are enabled, CAL1 should emit a single PASS noting nothing to check.
        cal1 = [c for c in report.checks if c.check_id == "CAL1"]
        assert len(cal1) == 1
        assert cal1[0].severity == "PASS"

    def test_never_fails_in_v1(self):
        """CAL1 must only emit PASS or WARN, never FAIL — single missed run is not critical."""
        from scripts import brain_health as bh
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch

        # No recent run at all
        def fake_sb_get(path):
            return []

        report = bh.HealthReport(timestamp="t")
        with patch.object(bh, "sb_get", side_effect=fake_sb_get), \
             patch.object(bh, "CALIBRATION_ENABLED_AGENTS", ["insider_copy"], create=True):
            bh.check_cal1_calibration_context_populated(report)

        for c in report.checks:
            if c.check_id == "CAL1":
                assert c.severity in ("PASS", "WARN"), f"CAL1 should never FAIL in v1, got {c.severity}"
```

Also update `test_all_checks_registered`:

```python
    def test_all_checks_registered(self):
        """Guard against drift between ALL_CHECKS and what ships."""
        from scripts.brain_health import ALL_CHECKS
        assert len(ALL_CHECKS) == 45, f"Expected 45 checks, got {len(ALL_CHECKS)}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_brain_health.py -v -k "CAL1 or all_checks_registered"`
Expected: FAIL — `check_cal1_calibration_context_populated` does not exist; count mismatch.

- [ ] **Step 3: Add CAL1 check to `scripts/brain_health.py`**

Insert before the `ALL_CHECKS = [...]` definition (around line 1475) the following, and import the settings at the top with the other env-var loads (after `TG_CHAT = os.getenv(...)`, around line 52):

Near the top of the file (after existing env loads, before `@dataclass` section), add:

```python
# Calibration loop v1 — CAL1 check scope
try:
    import web_settings as _ws
    CALIBRATION_ENABLED_AGENTS = list(getattr(_ws, "CALIBRATION_ENABLED_AGENTS", ["insider_copy"]))
except Exception:
    CALIBRATION_ENABLED_AGENTS = ["insider_copy"]
```

And immediately before `ALL_CHECKS = [...]`:

```python
# ══════════════════════════════════════════════════════════════════════════
# Category CAL: Calibration Loop (v1)
# ══════════════════════════════════════════════════════════════════════════

def check_cal1_calibration_context_populated(report: HealthReport):
    """CAL1: most recent agent_runs row for each enabled agent has calibration_context populated.

    PASS  — context present (or no enabled agents).
    WARN  — most recent run exists but calibration_context is NULL.
    WARN  — no recent run found at all (cadence missed).
    Never FAIL in v1 — see spec §8.2.
    """
    if not CALIBRATION_ENABLED_AGENTS:
        report.add(CheckResult("CAL1", "Calibration context populated", "PASS",
                               "No agents in CALIBRATION_ENABLED_AGENTS — nothing to check", "CAL"))
        return

    stale_or_missing: list[str] = []
    ok: list[str] = []
    for agent_type in CALIBRATION_ENABLED_AGENTS:
        rows = sb_get(
            f"agent_runs?select=id,created_at,calibration_context"
            f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
            f"&order=created_at.desc&limit=1"
        )
        if not rows:
            stale_or_missing.append(f"{agent_type} (no runs found)")
            continue
        ctx = rows[0].get("calibration_context")
        if ctx is None:
            stale_or_missing.append(f"{agent_type} (latest run has NULL calibration_context)")
        else:
            ok.append(agent_type)

    if stale_or_missing:
        report.add(CheckResult("CAL1", "Calibration context populated", "WARN",
                               "; ".join(stale_or_missing), "CAL"))
    else:
        report.add(CheckResult("CAL1", "Calibration context populated", "PASS",
                               f"all enabled agents have calibration_context ({', '.join(ok)})", "CAL"))
```

Add `check_cal1_calibration_context_populated` to `ALL_CHECKS` — place it right after the `V` block so the category ordering reads A, B, C, D, E, F, G, V, CAL:

```python
    # CAL: Calibration Loop (v1)
    check_cal1_calibration_context_populated,
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_brain_health.py -v`
Expected: all tests PASS, including the 4 CAL1 tests and the updated `test_all_checks_registered` (now expecting 45).

- [ ] **Step 5: Commit**

```bash
git add scripts/brain_health.py tests/test_brain_health.py
git commit -m "feat: brain_health CAL1 — calibration_context populated (scoped, WARN-only)"
```

---

## Task 12: Regenerate `index/schema_registry.json`

**Files:**
- Modify: `index/schema_registry.json` (regenerated by `scripts/brain_index.py generate`)
- Modify: `index/dependency_index.json` (same)

- [ ] **Step 1: Regenerate**

Run: `python3 scripts/brain_index.py generate`
Expected: succeeds; `index/schema_registry.json` now includes `calibration_context` under `agent_runs`.

- [ ] **Step 2: Verify freshness check passes**

Run: `python3 scripts/validate_schema_refs.py`
Expected: no errors. (CI enforces this — the index must be current.)

- [ ] **Step 3: Commit**

```bash
git add index/schema_registry.json index/dependency_index.json
git commit -m "chore(index): regenerate for agent_runs.calibration_context"
```

---

## Task 13: Integration test + execute backfill + enable end-to-end

**Files:**
- Create: `tests/test_calibration_loop.py`

- [ ] **Step 1: Write the integration test**

Create `tests/test_calibration_loop.py`:

```python
"""Integration test for calibration loop v1 — exercises the full linkage path.

Not run in CI (tagged `integration`). Validates the machine-checkable success
criterion from spec §8.1 against live Supabase state after a real insider_copy
run has completed.

Usage:
    python3 -m pytest tests/test_calibration_loop.py -v -m integration
"""
import json
import os
import pytest
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

pytestmark = pytest.mark.integration


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")


def _sb_get(path: str):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def test_recent_insider_copy_runs_have_calibration_context():
    """§8.1(1): For all migrated-agent agent_runs rows created in last 24h,
    calibration_context IS NOT NULL."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    runs = _sb_get(
        f"agent_runs?select=id,created_at,calibration_context"
        f"&agent_type=eq.insider_copy"
        f"&created_at=gte.{urllib.parse.quote(cutoff)}"
    )
    assert runs, "no insider_copy runs in last 24h — trigger a run before this test"
    missing = [r for r in runs if r.get("calibration_context") is None]
    assert not missing, f"{len(missing)} insider_copy runs in last 24h have NULL calibration_context"


def test_linked_predictions_have_valid_envelope():
    """§8.1(2): For any agent_predictions.run_id = <recent run>, context_snapshot
    parses as JSON AND contains calibration.saw_red_flags."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    preds = _sb_get(
        f"agent_predictions?select=id,run_id,context_snapshot"
        f"&agent_type=eq.insider_copy"
        f"&run_id=not.is.null"
        f"&created_at=gte.{urllib.parse.quote(cutoff)}"
    )
    assert preds, "no linked insider_copy predictions in last 24h — the integration path was not exercised"
    bad = []
    for p in preds:
        snap = p.get("context_snapshot")
        try:
            env = json.loads(snap) if isinstance(snap, str) else snap
            assert isinstance(env, dict)
            assert "calibration" in env
            assert "saw_red_flags" in env["calibration"]
        except Exception as exc:
            bad.append((p["id"], str(exc)))
    assert not bad, f"{len(bad)} predictions have invalid calibration envelopes: {bad[:3]}"
```

- [ ] **Step 2: Run the unit portion — confirm no import errors**

Run: `python3 -m pytest tests/test_calibration_loop.py --collect-only`
Expected: tests collected successfully (integration marker means they're skipped by default unless `-m integration`).

- [ ] **Step 3: Run full test suite to confirm no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: full suite still green (the new integration test is skipped by default). Baseline of 2029+ passing tests holds.

- [ ] **Step 4: Commit the integration test**

```bash
git add tests/test_calibration_loop.py
git commit -m "test: integration test for calibration loop v1 success criterion"
```

- [ ] **Step 5: Execute the backfill (dry-run first, then real)**

On the VPS (or local with prod env):

```bash
python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy --dry-run
```

Expected output shape:
```json
{
  "ok": true, "agent_type": "insider_copy", "dry_run": true,
  "matched": <N>, "ambiguous": <K>, "orphaned": <M>, "already_linked": <L>, "total_scanned": <T>
}
```

If `matched` is reasonable (spec §5.2.1: "most of the 55 predictions match to one of ~30 runs"; we expect `matched` to be a substantial share of `total_scanned`), proceed. If `matched` is zero, stop and investigate before applying.

Then run for real:

```bash
python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy
```

Save the output JSON — paste it into the commit message for Step 7.

- [ ] **Step 6: Trigger one insider_copy run end-to-end**

```bash
systemctl --user start sniper-agent-insider-copy.service
```

Wait for it to finish (`systemctl --user is-active` returns `inactive`), then query Supabase:

```sql
SELECT id, status, calibration_context IS NOT NULL AS has_ctx, turns, cost_usd
FROM agent_runs
WHERE agent_type='insider_copy'
ORDER BY created_at DESC
LIMIT 1;
```
Expected: `has_ctx = true`, `status = 'success'`, `turns > 0`.

And:
```sql
SELECT id, run_id, context_snapshot::text LIKE '%saw_red_flags%' AS envelope_present
FROM agent_predictions
WHERE agent_type='insider_copy'
  AND created_at > now() - interval '30 minutes'
ORDER BY created_at DESC;
```
Expected: at least one row, `envelope_present = true`, `run_id` matches the run from the previous query.

- [ ] **Step 7: Run the integration test on live data**

```bash
python3 -m pytest tests/test_calibration_loop.py -v -m integration
```

Expected: both tests PASS.

- [ ] **Step 8: Final commit with backfill stats in the message**

```bash
git commit --allow-empty -m "chore: enable calibration loop for insider_copy

Backfill stats for insider_copy:
  matched: <N>
  ambiguous: <K>
  orphaned: <M>
  already_linked: <L>
  total_scanned: <T>

Integration test tests/test_calibration_loop.py passed against live data.
CAL1 brain-health check: PASS on most recent insider_copy run."
```

Push:

```bash
git push origin main
```

---

## Self-review

**Spec coverage check** — every section of the spec maps to at least one task:

| Spec section | Implemented by |
|---|---|
| §4.2 new column | Task 1 |
| §5.5 `running` status | Task 2 |
| §5.5 constants | Task 3 |
| §5.1–5.4 calibration math | Task 4 |
| §5.2.1 backfill | Task 8 + Task 13 Step 5 |
| §6.1–6.2 injection + block | Task 5 (`_format_calibration_block`) + Task 9 |
| §6.3 envelope + validation | Task 7 |
| §6.4 `start-run` | Task 5 |
| §6.4 `finish-run` | Task 6 |
| §6.4 `predict --run-id --calibration-note` | Task 7 |
| §6.5 writes-per-run | Tasks 5, 7, 6 together |
| §7.1 insider_copy wrapper | Tasks 9, 10 |
| §7.1 schema registry regen | Task 12 |
| §8.1 tests | Task 13 |
| §8.2 CAL1 brain health | Task 11 |
| §8.3 eyeballed checks | Task 13 Steps 6–7 |

**Placeholder scan:** no TBD, no "TODO", no "implement later", no vague "add error handling." Every step has exact commands or code.

**Type consistency check:**
- `cmd_calibration_stats_fn` is the name across Tasks 4–5 (function), `cmd_calibration_stats` is the CLI shim that prints ✓
- `_run_predict_tool` used consistently in Task 9 and Task 10 ✓
- `run_id` (Python snake_case) passed as `--run-id` (CLI kebab) consistently ✓
- `CALIBRATION_ENABLED_AGENTS` referenced in Tasks 3, 8, 11 ✓
- Envelope keys (`schema_version`, `calibration.saw_red_flags`, `calibration.verification_query_provided`, `calibration.confidence_rationale`) match between Tasks 7, 10, 13 ✓
