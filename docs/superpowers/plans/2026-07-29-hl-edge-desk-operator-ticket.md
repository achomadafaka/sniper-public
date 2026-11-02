# Hyperliquid Edge Desk Operator Ticket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cluttered Hyperliquid evidence dashboard with the approved current-arm operator ticket, then land and deploy the exact `main` commit.

**Architecture:** Keep the existing schema-v3 API and polling loop. Recompose the current payload in `static/app.js`, keep secondary evidence in the native audit disclosure, and replace only the Edge Desk override CSS and matching template IDs.

**Tech Stack:** Flask/Jinja, vanilla JavaScript, CSS, pytest, systemd, content-addressed Git worktree releases.

## Global Constraints

- No Playwright, browser automation, subagents, PR, new dependency, new API request, database write, strategy change, or collector restart.
- Promotion remains paused and paper-only; the page must never imply funded authority.
- Unknown values render as `—` and stale collection cannot render as running.
- Push directly to `origin/main` only after tests pass and the branch includes the latest remote main.

---

### Task 1: Lock the compact UI contract

**Files:**
- Modify: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: `templates/index.html`, `static/app.js`, and `static/styles.css` as text fixtures.
- Produces: a contract that requires the current-arm ticket and rejects the old primary copy-control/runway layout.

- [ ] **Step 1: Write the failing test**

Require `hl-edge-metrics`, `hl-edge-rule`, `No qualifying setup yet`, `complete closes`, `PROMOTION PAUSED`, and `Why not promoted`; reject `hl-edge-runway`, `hl-edge-summary`, `COPY CONTROL · TOTAL BOOK SNAPSHOT`, and `WHAT CHANGES THE VERDICT` from the primary renderer.

- [ ] **Step 2: Run the contract test and confirm RED**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py::test_polymarket_5m_maker_tab_archived_and_hyperliquid_edge_desk_wired -q`

Expected: FAIL because the old runway and summary remain wired.

### Task 2: Build the operator ticket

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Test: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: `edge_paper`, `evidence_debt`, `study`, `wallet_copy`, `positions`, and `edge_atlas` from `/api/hyperlive/edge-desk` schema v3.
- Produces: `_renderVerdict(d, staleDisplay)`, `_renderEdgeMetrics(d)`, `_renderEdgeRule(d)`, and `_renderAuditDetail(d)` output in `hl-edge-verdict`, `hl-edge-metrics`, `hl-edge-rule`, and `hl-edge-audit-body`.

- [ ] **Step 1: Replace primary DOM slots**

Keep `hl-edge-verdict`; replace `hl-edge-runway` and `hl-edge-summary` with `hl-edge-metrics` and `hl-edge-rule`. Rename the disclosure label to `Why not promoted`.

- [ ] **Step 2: Render current state and progress**

Render collection state, an active empty-state sentence when accepted/open/complete are zero, the exact `complete_net_closes / proof_target` rail, and compact universe/qualifying/open/captured-hour metrics.

- [ ] **Step 3: Render the frozen rule and authority boundary**

Derive run-up, lookback, funding skip, target, stop, cap, clip, and concurrency from `edge_paper.policy`; render `PROMOTION PAUSED · NO FUNDS · NO ORDERS` separately.

- [ ] **Step 4: Move secondary evidence into audit**

Keep history, source timing, capacity, forward proof, copy-book, runtime, concentration, and retained-state facts inside the collapsed disclosure. Remove them from the primary surface.

- [ ] **Step 5: Replace the Edge Desk override CSS**

Implement the approved palette, single evidence rail, restrained type scale, desktop two-column rule row, one-column mobile layout, and visible disclosure focus state. Do not add motion.

- [ ] **Step 6: Run GREEN verification**

Run: `node --check static/app.js && python3 -m pytest tests/test_api_hyperlive_edge_desk.py tests/test_hl_edge_desk.py tests/test_hl_edge_atlas.py tests/test_ui_algo_tabs.py tests/test_web_server_asset_paths.py -q && git diff --check`

Expected: JavaScript exit 0, all focused tests pass, diff check exit 0.

### Task 3: Main and live deployment

**Files:**
- No source changes.

**Interfaces:**
- Consumes: verified Git HEAD and the root-managed `sniper-web.service`.
- Produces: `origin/main`, `.runtime/web/current`, and the live process all owning the same SHA.

- [ ] **Step 1: Commit and fast-forward main**

Commit the UI change, fetch `origin/main`, merge only if necessary, verify ancestry, then run `git push origin HEAD:main`.

- [ ] **Step 2: Create and activate the exact release**

Add `.runtime/web/releases/<main-sha>` as a detached worktree, atomically update `previous` and `current`, then restart `sniper-web.service`.

- [ ] **Step 3: Verify live ownership and truth**

Verify `origin/main == current release == process-loaded release`, exactly one web process, authenticated `/health` and `/api/hyperlive/edge-desk` HTTP 200, served JS/CSS hashes match the release, current paper/tape heartbeats are fresh, and no warning-or-higher web logs appeared after restart.

- [ ] **Step 4: Report exact evidence**

Report the SHA, current-arm counts, tests, service/process state, scoped files, absence of Playwright/PR/DB writes, and any genuine remaining risk.
