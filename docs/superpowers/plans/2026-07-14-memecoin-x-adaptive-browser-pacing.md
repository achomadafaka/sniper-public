# Memecoin X Adaptive Browser Pacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rapid fixed browser loop with an audited adaptive 5-15 minute scheduler, readiness gate, operator quiet period, and DOM-aware bounded scroll.

**Architecture:** Keep the existing userscript/loopback-receiver boundary. Add pure scheduler helpers to the userscript, persist bounded pacing metadata on capture runs, and make `page_unready` a first-class zero-card diagnostic. Existing pending-delivery and sentinel idempotency remain unchanged.

**Tech Stack:** JavaScript userscript, Node `node:test`, Python 3.13, SQLite, Flask receiver, pytest. No new dependencies.

**Implementation status:** Complete through userscript v1.5, including cross-tab leader election,
receiver startup migration, explicit corrected-clock wiring, and fixture/static gates. The bounded
HTTP-403/blank-page freeze worked as designed; X later recovered without a forced retry, and the
fresh parser-v2 acceptance window is accumulating healthy runs.

## Global Constraints

- Work in `/home/bigpapi/Desktop/Sniper/.worktrees/x-source-graph` on `feat/x-source-graph-design`.
- TDD each behavior: focused failing test, minimal implementation, focused pass, then broader pass.
- Browser actions remain limited to DOM reads, bounded `scrollBy`, and healthy-pass reload.
- Never click, like, repost, reply, follow, message, submit forms, or bypass operator states.
- New cards use 5-8 minutes; no-new uses 10-15 minutes; only the first unresolved coverage gap receives a 5-8 minute recovery pass.
- Each scroll is 450 pixels, at most three times, with canonical-ID progress and stability waits.
- Page readiness waits at most 30 seconds; the first failure retries readiness after 15 minutes and the second freezes.
- Receiver transport retries occur after 60 seconds without another browser pass.
- All capture remains display-only and isolated from funded execution.

---

### Task 1: Persist pacing and page-readiness audit facts

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `sniper/services/memecoin_x_receiver.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_memecoin_x_receiver.py`
- Modify: `tests/test_brain_index.py`

**Interfaces:**
- Add status/diagnostic/stop reason `page_unready`.
- Add nullable capture-run columns `cadence_bucket TEXT`, `scroll_count INTEGER`, `dom_wait_ms INTEGER`, `reload_reason TEXT`, `recovery_used INTEGER`, and `operator_postponed INTEGER`.
- Extend receiver pass metadata with those six keys.

- [ ] **Step 1: Write failing persistence and protocol tests**

Add assertions that `ensure_memecoin_x_tables` exposes the six columns, `finish_capture_run` persists them, a zero-card `page_unready` payload commits, and invalid values reject:

```python
payload = {
    "run_id": "page-unready-1", "cards": [], "status": "page_unready",
    "diagnostics": ["page_unready"], "cards_scanned": 0,
    "stop_reason": "page_unready", "cadence_bucket": "page_unready_15m",
    "scroll_count": 0, "dom_wait_ms": 30000, "reload_reason": None,
    "recovery_used": False, "operator_postponed": False,
}
```

Reject negative counts, counts above `3`/`30000`, non-booleans, cadence strings outside
`new_5_8m|idle_10_15m|recovery_5_8m|gap_10_15m|page_unready_15m|frozen|delivery_retry_60s`, and reload reasons outside `healthy_ack|none`.

- [ ] **Step 2: Run red**

Run: `python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py tests/test_brain_index.py -q`

Expected: failures for missing columns and rejected `page_unready` status.

- [ ] **Step 3: Implement the schema and bounded receiver validation**

Use `_ensure_column` for compatibility. Add `page_unready` to the existing enum sets and pass the six values through `finish_capture_run`; do not add a second persistence path. Normalize booleans to `0/1` only after strict `isinstance(value, bool)` checks.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py tests/test_brain_index.py -q
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin_x.py sniper/services/memecoin_x_receiver.py tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py tests/test_brain_index.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): audit adaptive capture pacing"
```

---

### Task 2: Add a pure adaptive scheduling core

**Files:**
- Modify: `scripts/memecoin_x_capture.user.js`
- Modify: `tests/js/test_memecoin_x_capture_user.js`

**Interfaces:**
- `chooseSchedule({capture, acknowledged, recoveryUsed, pageUnreadyCount, random}) -> {delayMs, cadenceBucket, recoveryUsed, pageUnreadyCount, frozen}`
- `remainingQuietMs({nowMs, lastTrustedInputMs, quietMs=90000}) -> number`
- `canonicalIds(document) -> string[]`

- [ ] **Step 1: Write failing boundary tests**

Use deterministic `random` values `0` and `1` to prove inclusive windows:

```javascript
assert.deepEqual(chooseSchedule({
  capture: {status: 'ok', cards: [{id: '1'}], sentinel_found: true},
  acknowledged: true, recoveryUsed: false, pageUnreadyCount: 0, random: () => 0,
}), {delayMs: 300000, cadenceBucket: 'new_5_8m', recoveryUsed: false,
     pageUnreadyCount: 0, frozen: false});
```

Cover 8-minute maximum, no-new 10/15-minute boundaries, exactly one accelerated coverage-gap retry, further gaps at 10-15 minutes, first/second `page_unready`, frozen operator states, 60-second unacknowledged retry, and remaining quiet time at `0`, `1`, `89999`, and `90000` milliseconds.

- [ ] **Step 2: Run red**

Run: `node --test tests/js/test_memecoin_x_capture_user.js`

Expected: import failures for the three helpers.

- [ ] **Step 3: Implement pure helpers**

Use named constants in milliseconds. Clamp the random draw into `[0,1]`, calculate
`min + draw * (max-min)`, and return explicit state rather than mutating storage. Treat
`wrong_feed`, `logged_out`, `challenge`, and `rate_limited` as frozen only after readiness has
classified the page.

- [ ] **Step 4: Verify and commit**

```bash
node --test tests/js/test_memecoin_x_capture_user.js
git add scripts/memecoin_x_capture.user.js tests/js/test_memecoin_x_capture_user.js
git commit -m "feat(memecoin-x): schedule adaptive capture passes"
```

---

### Task 3: Make readiness and scrolling DOM-aware

**Files:**
- Modify: `scripts/memecoin_x_capture.user.js`
- Modify: `tests/js/test_memecoin_x_capture_user.js`

**Interfaces:**
- `waitForReadiness({document, now, sleep, timeoutMs=30000}) -> Promise<{status, diagnostics}>`
- `createDomAwareScroll({root, readIds, sleep, progressTimeoutMs=5000, stableMs=1500}) -> Promise<{waitMs:number}>`
- `startBrowserCapture` registers trusted input listeners and passes pacing metadata to delivery.

- [ ] **Step 1: Write failing asynchronous tests**

Use injected `now`/`sleep`; do not wait in real time. Prove:

- an initially blank page that hydrates before 30 seconds becomes healthy;
- a permanently blank page becomes `page_unready`;
- an authenticated first tab remains `wrong_feed`;
- `scrollBy(0, 450)` occurs once per call;
- progress returns after a new canonical ID and 1.5-second stability;
- no progress returns after exactly five seconds;
- three-scroll/sentinel behavior remains bounded;
- trusted input postpones the pass and synthetic input does not;
- frozen/page-unready states never call `reload` or `scrollBy`; and
- a failed delivery retries without collecting another pass.

- [ ] **Step 2: Run red**

Run: `node --test tests/js/test_memecoin_x_capture_user.js`

- [ ] **Step 3: Implement the browser integration**

Increment userscript `@version` to `1.5`. Inject clock, sleep, and random dependencies into the
testable core. Add pacing metadata to the immutable payload before the first send. Preserve the
same payload and run ID across transport retries. Schedule reload only when `acknowledged`, the
capture status is `ok|no_new|coverage_gap`, and the scheduler is not frozen.

- [ ] **Step 4: Verify and commit**

```bash
node --test tests/js/test_memecoin_x_capture_user.js
git add scripts/memecoin_x_capture.user.js tests/js/test_memecoin_x_capture_user.js
git commit -m "fix(memecoin-x): wait for DOM progress before scrolling"
```

---

### Task 4: Report readiness failures and guard account actions

**Files:**
- Modify: `scripts/report_memecoin_x_capture.py`
- Modify: `tests/test_memecoin_x_capture_report.py`
- Modify: `docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md`

**Interfaces:**
- Capture report adds `pacing_counts`, `page_unready_runs`, `operator_postponed_runs`, and `max_dom_wait_ms`.
- Acceptance blocks on unresolved latest `page_unready`, any invalid pacing value, or a capture source action outside the allowlist.

- [ ] **Step 1: Write failing report and static-guard tests**

Prove historical recovered `page_unready` remains visible but does not block, latest unresolved
`page_unready` blocks, pacing counts reconcile to runs, and the source guard rejects `.click(`,
`.submit(`, native `fetch(`, `location.assign`, `location.replace`, direct `location=` writes,
and selectors for X like/follow/repost/reply controls while allowing `location.reload()`.

- [ ] **Step 2: Run red**

Run: `python3 -m pytest tests/test_memecoin_x_capture_report.py -q`

- [ ] **Step 3: Implement report fields and align the parent spec status**

Keep all denominators run-based. Do not reinterpret past 120-240 second rows as adaptive; the
new acceptance window begins after userscript `1.5` is installed and produces a healthy committed
pass. Earlier v1.4 rows remain historical diagnostics only.

- [ ] **Step 4: Run the adaptive-pacing gate and commit**

```bash
node --test tests/js/test_memecoin_x_capture_user.js
python3 -m pytest tests/test_memecoin_x_receiver.py tests/test_memecoin_x_store.py tests/test_memecoin_x_capture_report.py -q
python3 scripts/validate_schema_refs.py
git diff --check
git add scripts/report_memecoin_x_capture.py tests/test_memecoin_x_capture_report.py docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md
git commit -m "test(memecoin-x): gate adaptive capture readiness"
```

---

### Task 5: Fixture and live-runtime verification

- [ ] **Step 1: Run the complete capture suite**

```bash
node --test tests/js/test_memecoin_x_capture_user.js
python3 -m pytest \
  tests/test_memecoin_x_store.py \
  tests/test_memecoin_x_receiver.py \
  tests/test_memecoin_x_dom_parser.py \
  tests/test_memecoin_x_dom_fixture.py \
  tests/test_memecoin_x_capture_report.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 2: Update Tampermonkey and verify one pass**

Serve/install userscript `1.5`, reload the dedicated Following tab once, and require one committed
run with valid pacing metadata, no parser failure, no forbidden action, and no unresolved operator
state. Do not loop through a challenge or rate limit.

- [ ] **Step 3: Reset acceptance only after the verified pass**

Write a new corrected start marker and retain the previous report as a failed/superseded capture
window. The new 24-hour window is an operational gate, not a unit-test completion claim.
