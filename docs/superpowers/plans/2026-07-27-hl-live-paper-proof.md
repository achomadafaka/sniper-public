# Hyperliquid Live Paper Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land and activate one honest forward paper test for the frozen funding-filtered 30%/72h Hyperliquid alt-short candidate.

**Architecture:** Preserve the provider-exact wallet-copy runtime on `main`, add a separate paper-only SQLite ledger for the standalone candidate, and expose it read-only through Edge Desk. The runner records every completed-bar nomination and exact-quantity dual-size L2 fills; the UI separates this proof from wallet-copy and retired standalone evidence.

**Tech Stack:** Python 3, SQLite WAL, Flask API, vanilla JavaScript UI, pytest, bash watchdog.

## Global Constraints

- No funded imports, SDK orders, keys, account mutation, or funded DB changes.
- Preserve provider symbol casing exactly.
- Use only completed 1h candles with `close_time_ms < now_ms`.
- Policy stays 30%/72h, funding percentile `<= 0.94993`, 15% TP, 20% stop, 24h cap.
- Primary event evidence is uncensored by five-slot capacity.
- Missing funding produces `NULL` net PnL and explicit incomplete provenance.
- Exactly one web server remains on port 5000.
- Focused tests only; no full suite.

---

### Task 1: Land sealed research provenance

**Files:**
- Add: `data/research/hl_squeeze_study/PREREG.v2.md`
- Add: `data/research/hl_squeeze_study/funding.v1.json`
- Add: `data/research/hl_squeeze_study/funding_signal.v1.json`
- Add: `data/research/hl_squeeze_study/PREREG.impact.v1.md`
- Add: `data/research/hl_squeeze_study/impact_cost.v1.json`
- Add: `scripts/research/hl_funding_study.py`
- Add: `scripts/research/hl_funding_signal.py`
- Add: `scripts/research/hl_impact_cost_study.py`
- Add: `tests/test_hl_impact_cost_study.py`

**Interfaces:**
- Consumes: existing `hl_full_history` manifest/candle/funding cache and squeeze tape.
- Produces: committed discovery result and deterministic cost-falsification receipt.

- [x] Cherry-pick the funding correction and impact-test commits onto current `main`, resolving only duplicated research artifacts.
- [ ] Run `python3 -m pytest tests/test_hl_impact_cost_study.py -q`; expect all tests to pass.
- [x] Commit the resolved provenance as one research integration change if cherry-pick resolution requires a new commit.

### Task 2: Correct the live paper runner with TDD

**Files:**
- Create: `scripts/run_hl_altshort_paper.py`
- Create: `scripts/hl_altshort_paper_watchdog.sh`
- Create: `tests/test_hl_altshort_paper.py`

**Interfaces:**
- Produces: `completed_hourly_closes(rows, now_ms)`, `walk_book_notional(levels, usd)`,
  `walk_book_quantity(levels, qty)`, `ensure_arm(...)`, `scan_and_open(...)`, `manage_open(...)`.

- [x] Write failing tests proving incomplete candles are excluded, exact entry quantity is closed,
  all coins are scanned after the portfolio reaches five slots, missing funding leaves net fields
  null, cap fill delta compares against mid, and clip/reference execution semantics affect arm ID.
- [x] Run `python3 -m pytest tests/test_hl_altshort_paper.py -q`; verify those tests fail for the
  expected missing or incorrect behavior.
- [x] Implement schema v2 and the minimum runner changes. Entry and exit fill helpers return
  `{"px": float, "qty": float, "levels": int}`; close paths accept exact stored quantity.
- [ ] Run `python3 -m pytest tests/test_hl_altshort_paper.py tests/test_hyperliquid_api_helpers.py -q`.
- [x] Commit as `fix(hl): make live paper proof execution honest`.

### Task 3: Expose the proof in Edge Desk

**Files:**
- Modify: `sniper/hyperlive/edge_desk.py`
- Modify: `sniper/routes/api_hyperlive.py`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_api_hyperlive_edge_desk.py`
- Modify: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Produces: `read_edge_paper(db_path, heartbeat_path, now_ms) -> dict` and Edge Desk
  `edge_paper` payload block.

- [x] Write failing tests using a real temporary SQLite ledger. Assert fresh status, honest null
  results for zero closes, skip/reject counts, fill summaries, milestone progress, and unavailable
  state for schema mismatch.
- [x] Run the two focused files and verify RED.
- [x] Implement read-only aggregation; bump Edge Desk schema version; render `EDGE PAPER TEST` as
  the primary standalone status and a compact proof receipt with 25/200 milestones.
- [x] Run `python3 -m pytest tests/test_api_hyperlive_edge_desk.py tests/test_ui_algo_tabs.py -q`.
- [x] Commit as `feat(hl): expose live paper proof in Edge Desk`.

### Task 4: Merge and activate the paper-only runtime

**Files:**
- Modify: user crontab only to remove the obsolete standalone-exhaustion watchdog line.
- Runtime writes: `data/hl_altshort_paper.db`, heartbeat, PID, and logs.

**Interfaces:**
- Consumes: merged `main` checkout and watchdog command.
- Produces: one fresh edge-paper process and UI payload.

- [ ] Run focused runner, Edge Desk, API, provider-casing, watchdog, and schema tests; run
  `git diff --check` and `python3 scripts/validate_schema_refs.py`.
- [ ] Merge the feature branch into `main` without rebasing or force operations.
- [ ] Point the shared checkout at merged `main`, stop the obsolete standalone process, remove only
  its cron line, and restart the new paper watchdog once.
- [ ] Verify one `run_hl_altshort_paper.py`, one wallet-copy runner, one squeeze tape, one web server,
  fresh heartbeats, no watchdog failure loop, no pre-activation paper rows, provider-case symbols,
  authenticated `/api/hyperlive/edge-desk`, port 5000 only, and funded heartbeat `dry_run=true`.
