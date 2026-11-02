# Current System Health Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the global UI from failing a retired stream while preserving and clearing the real WAL warning.

**Architecture:** Remove the legacy `low_buy_trades` stream component at the health producer, leaving all consumers with an honest current-component set. Keep the WAL warning contract unchanged and use the existing live-safe checkpoint script for the one-time operational repair.

**Tech Stack:** Python 3, Flask JSON health endpoints, pytest, SQLite WAL.

## Global Constraints

- Do not read execution health from retired `low_buy_trades`.
- Do not repurpose memecoin runner health as global Polymarket stream health.
- Do not restart or redeploy services.
- Keep the WAL warning threshold at 200 MB.
- Use TDD and keep the code change limited to the health producer.

---

### Task 1: Remove the retired stream from current health

**Files:**
- Modify: `sniper/mcp_tools/read_tools.py:412-459`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `get_health_summary() -> dict` for current analyzer, paper, integrity, DB, WAL, and alerter facts.
- Produces: `_populate_health(result: dict) -> None` with no `health_details["stream"]` entry.

- [ ] **Step 1: Write the failing test**

Add this test with `from unittest.mock import patch`:

```python
def test_populate_health_does_not_fail_retired_low_buy_stream(self):
    from sniper.mcp_tools.read_tools import _populate_health

    health = {
        "db_path": self.db_path,
        "db_size_mb": 1.0,
        "table_counts": {"user_analysis": 0},
        "last_trade_ts": None,
        "paper_summary": {"open_count": 0},
        "checks": [{"name": "orphaned_paper_trades", "passed": True, "count": 0}],
    }
    with (
        patch("sniper.mcp_tools.read_tools.get_health_summary", return_value=health),
        patch("sniper.mcp_tools.read_tools.get_telegram_health_summary", return_value={"status": "ok", "detail": "configured"}),
    ):
        result = {"criteria": {"health": {}}}
        _populate_health(result)

    self.assertNotIn("stream", result["health_details"])
    self.assertEqual(result["criteria"]["health"]["status"], "pass")
```

- [ ] **Step 2: Run the test to verify RED**

Run: `python3 -m pytest tests/test_mcp_tools.py::TestHealthSummary::test_populate_health_does_not_fail_retired_low_buy_stream -q`

Expected: FAIL because `stream` exists and has status `fail`.

- [ ] **Step 3: Implement the minimal producer fix**

Delete the `last_trade_ts`/age calculation and the `"stream"` entry from `_populate_health()`. Leave every other component and all WAL thresholds unchanged.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_mcp_tools.py tests/test_dashboard_legacy_schema.py -q`

Expected: 86 passed.

- [ ] **Step 5: Commit**

```bash
git add sniper/mcp_tools/read_tools.py tests/test_mcp_tools.py
git commit -m "fix(ui): retire legacy stream health failure"
```

### Task 2: Clear and verify the live WAL warning

**Files:**
- Runtime maintenance only: `sniper_tracker.db`, `sniper_tracker.db-wal`

**Interfaces:**
- Consumes: `scripts/wal_checkpoint.py` and the live SQLite database.
- Produces: WAL below the 200 MB UI warning threshold without a service restart.

- [ ] **Step 1: Record pre-maintenance state**

Run: `stat -c '%s %n' sniper_tracker.db-wal && tail -n 3 logs/memecoin_runner.log`

Expected: WAL greater than 209715200 bytes and a recent runner cycle.

- [ ] **Step 2: Run approved maintenance**

Run: `python3 scripts/wal_checkpoint.py`

Expected: PASSIVE/RESTART/TRUNCATE results and exit 0 once WAL is below 100 MB.

- [ ] **Step 3: Verify runtime continuity**

Run: `stat -c '%s %n' sniper_tracker.db-wal && tail -n 5 logs/memecoin_runner.log`

Expected: WAL below 209715200 bytes and runner cycles continue advancing.

### Task 3: Final verification

**Files:**
- Inspect: branch diff only.

**Interfaces:**
- Consumes: committed Task 1 and live Task 2 evidence.
- Produces: review-ready branch with no execution changes.

- [ ] **Step 1: Run focused health tests**

Run: `python3 -m pytest tests/test_mcp_tools.py tests/test_dashboard_legacy_schema.py tests/test_ui_live_audit_manifest.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run repository checks**

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables && git diff --check main...HEAD`

Expected: no schema violations and no whitespace errors.

- [ ] **Step 3: Review scope**

Run: `git status --short --branch && git diff --stat main...HEAD && git log --oneline main..HEAD`

Expected: only the approved health spec/plan, test, and producer change; no runtime or strategy files.
