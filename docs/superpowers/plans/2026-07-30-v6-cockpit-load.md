# V6 Cockpit Load Remediation Implementation Plan

> **Required subskill:** Execute this plan with `superpowers:executing-plans`. Codex subagents are not authorized for this task, so run the steps inline.

**Goal:** Make the V6 decision cockpit update on relevant engine/admission state changes without polling the full historical report or waking the V6 engine.

**Architecture:** Keep the full `/summary` endpoint as an explicit research report. Add a bounded cockpit snapshot and a cheap SQLite revision token, expose that snapshot through the existing positions endpoint and a V6-only SSE endpoint, then let the browser render stream events only while the Real tab is visible. The stream reads the isolated V6 SQLite book read-only and never calls providers or engine code.

**Tech stack:** Python 3, Flask, SQLite, vanilla JavaScript `EventSource`, pytest.

**Global constraints:** Preserve paper-only and funded-book separation; all GET/stream DB access uses `connect_existing()`; no provider calls, engine callbacks, new dependencies, production writes, service restarts, or deployment; retain the full summary endpoint for explicit research use.

---

## Task 1: Lock the bounded report contract with failing tests

**Files:**

- Modify: `tests/test_memecoin_v6_paper_report.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`

- [ ] Add `test_cockpit_revision_ignores_telemetry_and_changes_on_admission`. Capture `report.cockpit_revision(conn)`, insert a latency sample and market event through the ledger helpers, assert the token is unchanged, enqueue one valid admission, and assert the token changes.
- [ ] Add `test_cockpit_snapshot_bounds_terminal_positions_and_latency`. Seed more terminal positions than `COCKPIT_TERMINAL_LIMIT`, call `report.cockpit_snapshot(conn)`, and assert all open positions plus only the newest bounded terminal rows are returned. Assert the returned object includes `summary`, `runtime`, and paper-only labels.
- [ ] Add `test_cockpit_snapshot_does_not_call_full_summary`. Monkeypatch `report.summary` to raise and assert `report.cockpit_snapshot(conn)` succeeds.
- [ ] Run the new report tests and confirm they fail because `cockpit_revision`, `cockpit_snapshot`, and the limits do not exist.
- [ ] Implement:

```python
COCKPIT_TERMINAL_LIMIT = 20
COCKPIT_LATENCY_LIMIT = 5_000

def cockpit_snapshot(conn) -> dict:
    return {
        "mode": "v6_paper",
        "funded": False,
        "fill_label": FILL_LABEL,
        "positions": list_cockpit_positions(conn),
        "runtime": runtime_health(conn, sample_limit=COCKPIT_LATENCY_LIMIT),
        "summary": cockpit_summary(conn),
    }
```

  `cockpit_revision()` returns one tuple from a single scalar-aggregate query: position count/max `updated_at`, decision max `rowid`, admission count/state counts/attempt sum/max `updated_at`, and rejection count/max source ID. `list_cockpit_positions()` selects every open row plus the newest 20 terminal rows. `cockpit_summary()` derives current-policy counts, quote coverage, insufficient-decision count, and `ledger.admission_summary(conn)` using bounded aggregate SQL. `runtime_health(..., sample_limit=...)` limits only the cockpit path; its default remains unchanged for existing callers.
- [ ] Run the report tests and confirm they pass.
- [ ] Commit: `feat(v6): add bounded cockpit snapshot`

## Task 2: Add a decision-gated V6 stream

**Files:**

- Modify: `tests/test_memecoin_v6_paper_api.py`
- Modify: `sniper/routes/api_memecoin.py`

- [ ] Update the positions endpoint contract test to require embedded `summary` operational fields.
- [ ] Add `test_positions_uses_cockpit_snapshot_not_full_summary`. Monkeypatch `report.summary` to raise and assert `GET /api/memecoin/v6-paper/positions` still returns 200.
- [ ] Add `test_v6_stream_once_returns_scrubbed_snapshot`. Request `GET /api/memecoin/v6-paper/stream?once=1`, assert `text/event-stream`, one `event: snapshot`, paper-only labels, embedded summary, CSRF token, and no forbidden funded fields.
- [ ] Run the new API tests and confirm the missing stream/embedded summary failures.
- [ ] Change `/api/memecoin/v6-paper/positions` to return `rpt.cockpit_snapshot(v6c)` through the existing five-second fallback cache.
- [ ] Add `/api/memecoin/v6-paper/stream`. Open one read-only V6 connection, check `rpt.cockpit_revision(v6c)` at 0.5-second intervals, emit a JSON `snapshot` only when the revision changes, emit a comment keepalive at 15 seconds, close the connection in `finally`, and support `?once=1` for deterministic tests. Build each event from `rpt.cockpit_snapshot(v6c)` and `_v6_scrub`; do not use the full-summary cache or funded connection.
- [ ] Return an honest empty paper snapshot when the isolated book is absent.
- [ ] Run the API tests and confirm they pass.
- [ ] Commit: `feat(v6): stream cockpit state changes`

## Task 3: Render stream decisions and retain a slow fallback

**Files:**

- Modify: `tests/test_memecoin_v6_paper_ui.py`
- Modify: `static/app.js`

- [ ] Replace the old UI source contract that expects `/summary` polling. Assert the V6 block contains `/stream`, `EventSource`, `requestAnimationFrame`, `visibilitychange`, and a 30-second fallback; assert `/summary` is absent from `_loadV6Paper()`.
- [ ] Add source assertions that leaving the Real subtab closes the V6 stream and that censor success forces a fresh cockpit load.
- [ ] Run the UI tests and confirm the old implementation fails the new contract.
- [ ] Extract the existing DOM work into `_renderV6Paper(data)`, reading operational summary from `data.summary`.
- [ ] Add `_scheduleV6PaperRender(data)` to coalesce events through `requestAnimationFrame` and discard renders while hidden or outside the Real subtab.
- [ ] Add `_ensureV6PaperStream()` and `_closeV6PaperStream()`. Open one `EventSource('/api/memecoin/v6-paper/stream')` only on the visible Real tab; parse `snapshot` events, retain the last-good timestamp, and mark the current cockpit stale on errors. Close and clear scheduled renders on tab exit or document hide; rely on native EventSource reconnect while visible.
- [ ] Keep `_loadV6Paper(force = false)` as initial/fallback GET only. A healthy stream suppresses repeat GETs; missing/failed streams may fetch at most once every 30 seconds. A successful censor calls `_loadV6Paper(true)`.
- [ ] Change the zero-open copy to state that the engine is idle because there is no eligible accepted paper fill. Label terminal rows as recent history because the endpoint is bounded.
- [ ] Run the UI tests and a JavaScript syntax check.
- [ ] Commit: `feat(v6): render cockpit decisions from stream`

## Task 4: Verify scope and regression safety

**Files:**

- Inspect: `sniper/services/memecoin_v6_paper_report.py`
- Inspect: `sniper/routes/api_memecoin.py`
- Inspect: `static/app.js`

- [ ] Run:

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py -q
node --check static/app.js
git diff --check
git status --short
```

- [ ] Confirm the full `/api/memecoin/v6-paper/summary` endpoint still exists and no V6 stream path imports or calls funded execution/provider modules.
- [ ] Review `git diff --stat` and `git diff` for unrelated changes.
- [ ] Do not deploy or restart. Report the branch, commits, test results, files inspected/changed, preserved scope guards, remaining risks, unresolved questions, and the explicit follow-up needed for deployment.
