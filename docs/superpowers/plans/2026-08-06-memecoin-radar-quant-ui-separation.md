# Memecoin Radar and V6 Quant UI Separation Implementation Plan

The V6 tasks remain current. The Radar entry-test UI task is superseded by
`2026-08-06-radar-valid-results-panel.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate the Radar realistic-entry measurement from the V6 quant-engine cockpit and make both current-state surfaces understandable without internal telemetry.

**Architecture:** The Opportunity Radar projection reads its own immutable request/receipt tables and exposes `paper_entry_test`. The existing V6 endpoint stays exact-current-cohort and gains a compact bounded outcome summary. The browser renders Radar entry coverage in the Radar workspace and renders post-fill V6 engine state in Live without changing either runtime.

**Tech Stack:** Python 3.13, SQLite, Flask, vanilla JavaScript/CSS, pytest, Playwright CLI.

## Global Constraints

- No full test suite.
- No funded authority, runner, policy, signal, quote, fill, exit, retention, or historical-row mutation.
- Do not pool Radar/Fomo sources, launch modes, policy hashes, or old/current cohorts.
- Default UI copy must not say `Radar v5 → V6` or expose adapter/version telemetry.
- Keep existing SSE and snapshot caching; add no polling loop, endpoint, dependency, chart, or navigation level.
- Preserve censor as the only V6 paper-position mutation.

---

### Task 1: Radar-Owned Realistic-Entry Projection

**Files:**
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_projection.py`
- Test: `tests/test_memecoin_opportunity_api.py`

**Interfaces:**
- Produces: `paper_entry_test_summary(conn) -> dict[str, object]`
- Produces in Radar snapshots: `paper_entry_test` with `state`, `signals_tested`, `paper_buys`, `could_not_buy`, `awaiting_quote`, `unfilled_reasons`, and `latest_receipt_at`.

- [ ] **Step 1: Write failing projection tests**

Create requests covering filled, typed unfilled, and pending states with existing paper-adapter helpers. Assert exact-current contract counts and assert an unrelated request version is excluded.

```python
summary = paper_entry_test_summary(conn)
assert summary == {
    "signals_tested": 3,
    "paper_buys": 1,
    "could_not_buy": 1,
    "awaiting_quote": 1,
    "unfilled_reasons": {"no_route": 1},
    "latest_receipt_at": "2026-08-06T12:00:03Z",
}
```

- [ ] **Step 2: Run the projection test and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_projection.py -q -k paper_entry_test
```

Expected: import or key assertion failure because the projection does not exist.

- [ ] **Step 3: Implement the bounded Radar-owned query**

Add `paper_entry_test_summary(conn)` using two aggregate queries over
`op_paper_fill_requests LEFT JOIN op_paper_fills`, filtered to Solana Pump.fun, `$25`,
`pump-confirmation-truth-v5`, `alert-outcome-v4`, `radar-paper-adapter-v1`, and
`route-floor-v1`. Keep exact stored reason codes in the API.

- [ ] **Step 4: Run the projection test and verify GREEN**

Run the Step 2 command. Expected: selected tests pass.

- [ ] **Step 5: Write failing API/SSE tests**

Assert `/api/memecoin/opportunity-radar` includes the summary and derives state from
`opportunity:paper_adapter`: missing is `NOT RUNNING`, healthy is `COLLECTING`, degraded is
`STALE`. Assert `_stream_fingerprint` changes when receipt counts change.

- [ ] **Step 6: Run the API tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_api.py -q -k 'paper_entry_test or stream_fingerprint'
```

Expected: missing `paper_entry_test` or unchanged fingerprint assertion failure.

- [ ] **Step 7: Wire the projection into the compact Radar snapshot**

Call `paper_entry_test_summary(conn)` before internal `opportunity:*` statuses are removed. Add
the derived state and retain the payload through `_compact_snapshot`. Include the compact object in
the SSE fingerprint. The unavailable payload uses zero counts and `NOT RUNNING`.

- [ ] **Step 8: Run Task 1 tests**

```bash
python3 -m pytest tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_api.py -q
```

Expected: all selected files pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add sniper/research/memecoin_opportunity/projection.py sniper/routes/api_memecoin_opportunity.py tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_api.py
git commit -m "feat(radar): expose realistic entry test state"
```

---

### Task 2: Compact Exact-Cohort V6 Outcome Summary

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_report.py`

**Interfaces:**
- Extends: `cockpit_summary(conn) -> dict`
- Adds: `outcomes_by_launch_mode`, with `graded_lifecycles`, `winner_rate`, `gross_modeled_pnl_usd`, and `net_modeled_pnl_usd=None` inside each exact launch-mode cohort.

- [ ] **Step 1: Write a failing exact-cohort outcome test**

Insert one Standard winner, one Cashback loser, and one legacy/Fomo winner. Assert the cockpit
reports the Standard and Cashback outcomes separately and excludes the legacy row. There must be no
top-level pooled win rate or gross PnL.

- [ ] **Step 2: Run the outcome test and verify RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py -q -k cockpit_outcome
```

Expected: missing outcome fields.

- [ ] **Step 3: Implement two bounded exact-cohort queries**

Read only closed positions plus their entries under `_current_radar_where()`, then read their action
fills in one joined query. Reuse `_economics(..., action_fills=...)` in memory and group results by
`launch_mode`. Do not call the unbounded full-report `_summary_facts()` path and do not emit a pooled
performance number.

- [ ] **Step 4: Run Task 2 tests**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py -q -k 'cockpit or current_radar or cohort'
```

Expected: selected tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add sniper/services/memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_report.py
git commit -m "feat(v6): summarize current quant paper outcomes"
```

---

### Task 3: Separate and Simplify the Two Operator Surfaces

**Files:**
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_memecoin_v6_paper_ui.py`
- Modify: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: Radar snapshot `paper_entry_test` from Task 1.
- Consumes: V6 `summary` outcome fields from Task 2.
- Keeps: `/api/memecoin/v6-paper/positions`, `/api/memecoin/v6-paper/stream`, and the existing Radar snapshot/stream.

- [ ] **Step 1: Write failing placement and copy tests**

Assert:

```python
assert 'id="mc-radar-paper-entry-test"' in APP
assert "Realistic entry test" in radar_renderer
assert "Signals tested" in radar_renderer
assert "Paper buys" in radar_renderer
assert "Could not buy" in radar_renderer
assert "Awaiting quote" in radar_renderer
assert "Radar v5 → V6" not in APP
assert "V6 Quant Engine" in v6_block
assert "After a realistic paper buy" in v6_block
```

Also assert the default V6 health strip omits request/fill counts, feed/gap counts, latency
percentiles, and insufficient-decision telemetry.

- [ ] **Step 2: Run both UI contract files and verify RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_opportunity_ui_contract.py -q
```

Expected: placement/copy assertions fail.

- [ ] **Step 3: Add the Radar entry-test host and renderer**

Place `#mc-radar-paper-entry-test` below the Radar verdict. Add
`_renderRadarPaperEntryTest(data.paper_entry_test)` with the four counters and one state sentence.
Map typed reason codes to the seven approved plain labels and map unknown values to `Other`.
Use the existing Radar render/SSE path; add no new fetch or stream.

- [ ] **Step 4: Rewrite the V6 default header and summary**

Render:

```text
V6 Quant Engine
Paper test — no money
After a realistic paper buy, this engine decides when to hold, reduce, or exit.
Input: Radar-qualified $25 paper buys
```

Show only state, open, completed, per-launch-mode results, and quote coverage. When no current
cohort exists, say `Waiting for the first realistic paper buy.` Translate actions to `Hold`,
`Reduce`, and `Exit`; keep exact action codes only in technical details. Preserve position cards,
censor control, lazy detail, exact-cohort filtering, and stale-last-good behavior.

- [ ] **Step 5: Reduce CSS to the two focused layouts**

Use the existing slate/navy palette and monospace only for data. Give the Radar block a short
left-to-right handoff rail (`Radar signal → $25 quote → Quant engine`) as the single visual
signature. Keep keyboard focus, non-color state markers, and the existing narrow breakpoint.

- [ ] **Step 6: Run UI tests and verify GREEN**

Run the Step 2 command. Expected: both files pass.

- [ ] **Step 7: Run targeted integration checks**

```bash
python3 -m pytest tests/test_memecoin_opportunity_api.py tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_ui_contract.py tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_v6_radar_signals.py -q
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts
git diff --check
```

Expected: all targeted tests pass, schema references are clean, compilation exits zero, and no
whitespace errors exist.

- [ ] **Step 8: Commit Task 3**

```bash
git add static/app.js static/styles.css tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_opportunity_ui_contract.py
git commit -m "fix(ui): separate Radar from the V6 quant engine"
```

---

### Task 4: Pin, Deploy, and Verify the Exact Release

**Files:**
- Runtime state only: `.env`, `.runtime/opportunity`, `.runtime/v6`, `.runtime/web`
- Browser artifact: `output/playwright/memecoin-radar-quant-ui-separation.png`

**Interfaces:**
- Consumes: the final branch commit from Tasks 1–3.
- Produces: three pinned runtime worktrees at the same immutable SHA.

- [ ] **Step 1: Push the feature branch without opening a PR**

Verify clean branch state, then push `fix/memecoin-radar-quant-ui-separation` to origin. Do not merge,
rebase, amend, force-push, or open a PR.

- [ ] **Step 2: Back up mutable configuration and both paper databases**

Create timestamped `.env`, Opportunity DB, and V6 DB backups using `sqlite3.backup()`. Verify both
backup integrity checks return `ok`.

- [ ] **Step 3: Build three detached release worktrees and smoke them**

Create the same SHA under `.runtime/opportunity/releases/<short-sha>`,
`.runtime/v6/releases/<short-sha>`, and `.runtime/web/releases/<short-sha>`. Run Opportunity
`--enabled-check`, V6 `--smoke-no-rpc`, and import/asset checks from the exact release.

- [ ] **Step 4: Repoint and restart only the three authorized services**

Repoint `current`/`previous` for Opportunity, V6, and web. Restart Opportunity through its
worker-held watchdog-lock sequence, V6 through its watchdog, and web through systemd restart-on-exit.
Do not touch the funded memecoin runtime.

- [ ] **Step 5: Verify live runtime truth**

Prove one Opportunity worker, one V6 worker, and one web process; exact release ancestry; fresh
heartbeats; DB integrity; Radar healthy/non-stale; zero open gaps; authenticated API HTTP 200; and
funded `false` on the V6 endpoint.

- [ ] **Step 6: Browser-check both placements**

Use Playwright against the authenticated local dashboard. Capture Live and Radar snapshots at
desktop and narrow width. Assert `Radar v5 → V6` is absent, V6 Quant appears only in Live, Realistic
Entry Test appears only in Radar, and both empty/current states read plainly.

- [ ] **Step 7: Remove only this task's scratch worktree after live proof**

Keep the pinned runtime worktrees and backups. Remove `.worktrees/memecoin-radar-quant-ui-separation`
only after the branch is clean, pushed, and deployed.
