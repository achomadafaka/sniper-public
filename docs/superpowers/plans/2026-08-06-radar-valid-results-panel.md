# Radar Valid Results Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rejected Radar research surfaces with one valid-results panel and remove the false global partial-source verdict.

**Architecture:** The browser reads the existing `forward_primary` object. The renderer shows five valid-result values and keeps source failure local to affected venue rows.

**Tech Stack:** Vanilla JavaScript, CSS, pytest, Node syntax checks.

## Global Constraints

- Do not change API data, research results, Radar rules, V6, or funded behavior.
- Do not show invalid results, pending results, gates, blockers, controls, diagnostics, venues, or entry-test state.
- Keep the Radar graphic and launch lists.
- Use focused tests before deployment.

---

### Task 1: Results-Only Radar Surface

**Files:**
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: `insights.confirmation_microscope.forward_primary`.
- Produces: `#mc-op-radar-results`.

- [ ] **Step 1: Write the failing UI contract test**

Assert the new host and these labels:

```python
assert 'id="mc-op-radar-results"' in APP
assert "valid paper results" in radar_renderer
assert "average net return" in radar_renderer
assert "target first" in radar_renderer
assert "stop first" in radar_renderer
assert "closed at 60 seconds" in radar_renderer
```

Assert the rejected hosts and labels are absent.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py -q
```

Expected: the new host assertion fails.

- [ ] **Step 3: Implement the small panel**

Replace the entry-test host, score row, and detail block with `#mc-op-radar-results`.

Render only `valid`, `mean_return_bps`, `profit_hits`, `losses`, and `time_exits`.

Remove the rejected renderer code and its CSS.

- [ ] **Step 4: Run the test and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

---

### Task 2: Global Source-State Scope

**Files:**
- Modify: `static/app.js`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`
- Test: `tests/test_memecoin_opportunity_projection.py`
- Test: `tests/test_memecoin_opportunity_api.py`

**Interfaces:**
- Consumes: `allSourcesBad` and `hardIncomplete`.
- Produces: a global verdict that ignores one failed source.
- Produces: a global API `stale` field that requires all capture sources to fail.

- [ ] **Step 1: Write the failing source-state test**

Assert the Radar renderer has no `PARTIAL — SOURCE STALE` branch. Assert `hardIncomplete` still uses `allSourcesBad`.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py -q -k source
```

Expected: the old partial verdict assertion fails.

- [ ] **Step 3: Remove the global partial state**

Remove the visible source telemetry host. Remove `sourceBad` from the global verdict class, title, and copy.

Keep `hardIncomplete` for a full capture failure.

- [ ] **Step 4: Run focused verification**

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py tests/test_memecoin_opportunity_api.py tests/test_memecoin_opportunity_projection.py -q
node --check static/app.js
git diff --check
```

Expected: all commands exit zero.

---

### Task 3: Commit and Deploy the Web Release

**Files:**
- Runtime state: `.runtime/web/current` and `.runtime/web/previous`.

**Interfaces:**
- Consumes: the final exact commit.
- Produces: one web process from the pinned release.

- [ ] **Step 1: Commit the scoped change**

Commit only the design, plan, UI, CSS, and test files.

- [ ] **Step 2: Publish through the repository workflow**

Push the feature branch. Merge through the approved repository path. Do not push directly to `main`.

- [ ] **Step 3: Create the immutable web release**

Create `.runtime/web/releases/<full-sha>` from the exact merged commit. Switch `previous` and `current` atomically.

- [ ] **Step 4: Restart and verify the web process**

Restart only the existing web process. Verify one process, the exact path, HTTP 200, served asset content, and the Radar API.
