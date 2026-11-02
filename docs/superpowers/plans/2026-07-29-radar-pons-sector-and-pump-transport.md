# Radar Pons Sector and Pump Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a visible Pons quadrant to the Radar and isolate Pump WebSocket/recovery traffic from the provider producing intermittent 429 gaps.

**Architecture:** Keep the UI change static and explicit: four named angle ranges and four SVG labels. Keep transport isolation configuration-only by moving Pump WebSocket and recovery traffic to the already-configured PublicNode endpoint while leaving Bags, primary truth, pacing, and failure reporting unchanged.

**Tech Stack:** JavaScript SVG rendering, Python/pytest contract tests, Bash deployment, pinned Git worktrees, SQLite read-only verification.

## Global Constraints

- Pons remains raw evidence only; do not invent scored dots, outcomes, signals, or funded actions.
- Do not suppress real source gaps.
- Do not add provider fanout, polling, arms, scoring, or execution authority.
- Do not run Playwright, agents, or the full test suite.
- Keep every existing RPC governor rate unchanged.

---

### Task 1: Add the Pons radar sector

**Files:**
- Modify: `static/app.js:5149-5160,5255-5265`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: `_opRadarPoint(item)` with `item.venue_id` and `item.launch_id`.
- Produces: explicit sector mapping for all four `VenueId` strings and a visible `Pons · Robinhood` SVG label.

- [ ] **Step 1: Write the failing UI contract test**

Add assertions to `test_svg_radar_has_accessible_zones_dots_and_non_color_risk_labels`:

```python
assert "pons_v1_4663: [140, 220]" in APP
assert "pumpfun_solana: [-130, -50]" in APP
assert "bags_solana: [-40, 40]" in APP
assert "bags_robinhood_4663: [50, 130]" in APP
assert ">Pons · Robinhood</text>" in APP
assert "const span = sectors[item.venue_id] ||" not in APP
```

- [ ] **Step 2: Verify the test fails for the missing Pons sector**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py -q -p no:cacheprovider -n0 -k radar_stage
```

Expected: failure on the missing explicit Pons mapping or label.

- [ ] **Step 3: Implement the four explicit sectors**

Change the mapping in `_opRadarPoint` to:

```javascript
const sectors = {
  pumpfun_solana: [-130, -50],
  bags_solana: [-40, 40],
  bags_robinhood_4663: [50, 130],
  pons_v1_4663: [140, 220],
};
const span = sectors[item.venue_id];
```

Add the fourth SVG label and reposition the existing labels to the corresponding top, right, bottom, and left sectors. Do not change dot scoring or filtering.

- [ ] **Step 4: Run focused UI/API checks**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py tests/test_memecoin_opportunity_api.py -q -p no:cacheprovider -n0
node --check static/app.js
git diff --check
```

Expected: all focused tests pass and both syntax checks exit zero.

- [ ] **Step 5: Commit the UI change**

```bash
git add static/app.js tests/test_memecoin_opportunity_ui_contract.py
git commit -m "fix(radar): add Pons visual sector"
```

### Task 2: Merge and create pinned releases

**Files:**
- Modify: none in the repository.
- Runtime: `.runtime/opportunity/releases/$merge_sha`, `.runtime/web/releases/$merge_sha`.

**Interfaces:**
- Consumes: merged `origin/main` SHA containing the design, plan, UI, and tests.
- Produces: immutable opportunity/web worktrees and atomic `current` links.

- [ ] **Step 1: Push the branch and open a PR with the Knowledge Sync block**

Run focused checks again before pushing. The PR must record the new spec/plan as the knowledge delta and include the exact test results.

- [ ] **Step 2: Merge only after schema and knowledge-sync checks pass**

Record the merge SHA. Do not deploy a branch SHA.

- [ ] **Step 3: Create and validate both pinned release worktrees**

```bash
merge_sha=$(git rev-parse origin/main)
git worktree add --detach ".runtime/opportunity/releases/$merge_sha" "$merge_sha"
git worktree add --detach ".runtime/web/releases/$merge_sha" "$merge_sha"
SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper PYTHONPATH=".runtime/opportunity/releases/$merge_sha" \
  python3 ".runtime/opportunity/releases/$merge_sha/scripts/memecoin_opportunity_worker.py" --enabled-check
node --check ".runtime/web/releases/$merge_sha/static/app.js"
```

Expected: both worktrees resolve to the merge SHA and validation exits zero.

### Task 3: Cut over Pump transport and verify live

**Files:**
- Modify: `/home/bigpapi/Desktop/Sniper/.env`
- Runtime: `.runtime/opportunity/current`, `.runtime/web/current`.

**Interfaces:**
- Consumes: `MEMECOIN_SOL_TAPE_RPC_URL` already present in `.env`.
- Produces: Pump WebSocket and recovery URLs on the same PublicNode host, with Bags WebSocket and primary read RPC unchanged.

- [ ] **Step 1: Back up and patch only the approved environment variables**

Create a timestamped `.env` backup. Set:

```dotenv
MEMECOIN_OPPORTUNITY_SOLANA_WS_URL=wss://solana-rpc.publicnode.com
MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPC_URL=https://solana-rpc.publicnode.com
```

Resolve the exact values from `MEMECOIN_SOL_TAPE_RPC_URL`; do not expose credentials or alter any rate setting.

- [ ] **Step 2: Gracefully cut over the opportunity worker and web service**

Verify each old PID belongs to its old pinned release before sending `SIGTERM`. Atomically switch release links, start the opportunity worker through its watchdog, and restart the system-owned web service through its verified `Restart=always` owner if interactive sudo is unavailable.

- [ ] **Step 3: Verify bounded restart recovery**

Poll authenticated Radar health and the read-only SQLite frontiers until all are true:

```text
one opportunity worker on the merge SHA
one web process on the merge SHA
op_source_frontiers.has_open_gap count = 0
op_runtime_status blocker count = 0
op_data_gaps unresolved count = 0
rpc.status = healthy
rpc.throttled = false
storage.status = healthy
storage.pause_ingestion = false
```

- [ ] **Step 4: Verify the visible contract without Playwright**

Fetch authenticated `/static/app.js` and assert it contains the Pons mapping/label. Fetch `/api/memecoin/opportunity-radar` and confirm Pons is healthy and its captured-launch count is present under `insights.venues`.

- [ ] **Step 5: Check host and call pressure**

Use `pidstat`, `uptime`, and Radar RPC metrics. The cutover fails if it adds duplicate workers, raises configured RPS, leaves throttling active, or causes sustained host pressure beyond the existing single-core worker profile.

- [ ] **Step 6: Roll back on failure**

Restore the timestamped `.env` backup and prior pinned links, then restart only opportunity and web. Do not leave an open continuity gap or a mixed release.
