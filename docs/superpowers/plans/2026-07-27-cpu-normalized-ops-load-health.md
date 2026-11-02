# CPU-Normalized Ops Load Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop normal research load on a 32-CPU host from producing a false global `WARN` while preserving real load and WAL alerts.

**Architecture:** Put CPU-capacity detection and load classification in a small pure helper module. `GET /api/ops/health` will expose the measured capacity and threshold, append only the helper's explicit issue, and leave all other health checks unchanged.

**Tech Stack:** Python 3.13, Flask, pytest, Linux `sched_getaffinity`, Playwright CLI

## Global Constraints

- Prefer `os.sched_getaffinity(0)` and fall back to `os.cpu_count()`.
- Warn above 90% of usable CPU capacity; preserve `3.0` only when capacity is unavailable.
- Return `cpu_count` and `load_warn_threshold` from `/api/ops/health`.
- Do not change WAL, RAM, swap, disk, funded, V6 paper, Radar, tape, or database behavior.
- Restart only the web service after focused verification.

---

### Task 1: CPU-normalized load classification

**Files:**
- Create: `sniper/web/ops_health.py`
- Create: `tests/test_ops_health.py`
- Modify: `sniper/web/server.py:807-821`

**Interfaces:**
- Produces: `usable_cpu_count() -> int | None`
- Produces: `classify_load(load_1min: float, cpu_count: int | None) -> tuple[float, str | None]`
- Consumes: `/proc/loadavg` values already read by `sniper.web.server.ops_health`

- [ ] **Step 1: Write the failing tests**

```python
from sniper.web.ops_health import classify_load, usable_cpu_count


def test_load_five_is_healthy_on_32_cpu():
    threshold, issue = classify_load(5.0, 32)
    assert threshold == 28.8
    assert issue is None


def test_load_above_ninety_percent_warns():
    threshold, issue = classify_load(29.0, 32)
    assert threshold == 28.8
    assert issue == "load_high:29.0"


def test_unknown_cpu_capacity_preserves_legacy_fallback():
    threshold, issue = classify_load(3.1, None)
    assert threshold == 3.0
    assert issue == "load_high:3.1"


def test_usable_cpu_count_prefers_affinity(monkeypatch):
    monkeypatch.setattr("sniper.web.ops_health.os.sched_getaffinity", lambda _pid: set(range(6)))
    monkeypatch.setattr("sniper.web.ops_health.os.cpu_count", lambda: 32)
    assert usable_cpu_count() == 6
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python3 -m pytest tests/test_ops_health.py -q`

Expected: collection fails because `sniper.web.ops_health` does not exist.

- [ ] **Step 3: Implement the pure helper**

```python
import os


def usable_cpu_count() -> int | None:
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass
    count = os.cpu_count()
    return int(count) if count and count > 0 else None


def classify_load(load_1min: float, cpu_count: int | None) -> tuple[float, str | None]:
    threshold = round(max(1.0, cpu_count * 0.9), 2) if cpu_count else 3.0
    issue = f"load_high:{load_1min}" if load_1min > threshold else None
    return threshold, issue
```

- [ ] **Step 4: Wire the helper into `/api/ops/health`**

Import both helpers. Set `cpu_count` and `load_warn_threshold` in the response. Append `issue` only when non-null. Do not alter any other issue source.

- [ ] **Step 5: Run focused verification**

Run:

```bash
python3 -m pytest tests/test_ops_health.py tests/test_ui_live_audit_manifest.py -q
python3 scripts/validate_schema_refs.py
node --check static/app.js
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add sniper/web/ops_health.py sniper/web/server.py tests/test_ops_health.py
git commit -m "fix(web): normalize load health by CPU capacity"
```

### Task 2: Deploy and verify the live UI

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: authenticated `/api/ops/health`
- Produces: live shell health chip `OK` when no real issue exists

- [ ] **Step 1: Verify service ownership before restart**

Run:

```bash
systemctl status sniper-web.service --no-pager -l
ss -ltnp | rg ':5000'
```

Expected: one `sniper.web.server` process owns port 5000 under `sniper-web.service`.

- [ ] **Step 2: Restart only the web service**

Run: `sudo systemctl restart sniper-web.service`

Expected: service becomes active with one port owner. Do not restart any other service.

- [ ] **Step 3: Verify operational truth**

Run `python3 scripts/wal_health.py`, then fetch authenticated `/api/ops/health` without printing credentials.

Expected: WAL verdict `OK`; payload contains `cpu_count: 32`, `load_warn_threshold: 28.8`, `wal_mb <= 100`, and no `load_high` or `wal_bloat` issue.

- [ ] **Step 4: Verify the rendered UI**

Open the dashboard with Playwright, wait for the health poll, and snapshot `#shell-health`.

Expected: chip value `OK`; title `All systems OK`; no WAL or load warning.
