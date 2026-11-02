# Hyperliquid Operator Evidence Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live Hyperliquid UI lead with the paused verdict and operate free, honest collection paths for each evidence debt.

**Architecture:** Extend the read-only Edge Desk payload with one structured `evidence_debt` object derived from sealed files and bounded SQL. Add a separate strict historical event-time backfill that cannot enter latency statistics. Replace the default atlas/ledger rendering with a compact evidence runway and one collapsed audit disclosure.

**Tech Stack:** Python 3.13, Flask, SQLite, vanilla JavaScript, CSS, pytest, existing Hyperliquid public API helpers.

## Global Constraints

- Paper/research only; funded authority remains unchanged and no funded worker is started.
- No paid archive, new dependency, cross-venue candle substitution, synthetic timestamp, or strategy relaxation.
- Historical reconstructed rows never receive `source_received_ts_ms` and never enter latency summaries.
- Runtime launches from immutable `.runtime/hyperliquid/current` and web launches from immutable `.runtime/web/current`.
- Use TDD: failing test, observed failure, minimum implementation, focused regression.
- Preserve unrelated checkout WIP and production-funded databases.

---

### Task 1: Evidence-debt API and prospective depth health

**Files:**
- Modify: `sniper/hyperlive/edge_desk.py`
- Modify: `scripts/run_hl_copy.py`
- Modify: `tests/test_api_hyperlive_edge_desk.py`
- Modify: `tests/test_run_hl_copy_selection.py`

**Interfaces:**
- Consumes: sealed `_cache_seal.json`, read-only `hl_copy_book`, `hl_copy_source_events`, copy and edge-paper heartbeats.
- Produces: Edge Desk schema 3 `evidence_debt`; heartbeat fields `depth_capture_format_version=2` and `depth_capture_scope='provider_returned_snapshot'`.

- [ ] **Step 1: Write failing API and heartbeat tests**

```python
assert body["schema_version"] == 3
assert body["evidence_debt"]["verdict"]["status"] == "paused"
assert body["evidence_debt"]["history"] == {
    "status": "blocked", "provider_symbols": 3,
    "available_candles": 2, "missing_candles": 1,
    "free_archive": "unavailable",
}
assert heartbeat["depth_capture_format_version"] == 2
assert heartbeat["depth_capture_scope"] == "provider_returned_snapshot"
```

- [ ] **Step 2: Run the focused tests and observe contract failures**

Run: `python3 -m pytest tests/test_api_hyperlive_edge_desk.py tests/test_run_hl_copy_selection.py -q`

Expected: schema remains 2, `evidence_debt` is absent, and heartbeat depth fields are absent.

- [ ] **Step 3: Implement bounded evidence-debt assembly**

```python
def _evidence_debt(*, conn, data_dir, study, wallet_copy, edge_paper):
    return {
        "verdict": _evidence_verdict(study, edge_paper),
        "history": _history_coverage(data_dir, study),
        "source_timing": _source_timing(conn, wallet_copy),
        "capacity": _capacity_scope(conn, wallet_copy),
        "forward": _forward_progress(edge_paper),
    }
```

Use aggregate SQL only. Verify the cache seal SHA against the sealed report before exposing counts. Missing inputs return `status='unknown'`, not zero.

- [ ] **Step 4: Run focused tests and schema validation**

Run: `python3 -m pytest tests/test_api_hyperlive_edge_desk.py tests/test_run_hl_copy_selection.py -q`

Run: `python3 scripts/validate_schema_refs.py`

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/edge_desk.py scripts/run_hl_copy.py \
  tests/test_api_hyperlive_edge_desk.py tests/test_run_hl_copy_selection.py
git commit -m "feat(hl): expose operator evidence debt"
```

### Task 2: Strict no-cost historical event-time backfill

**Files:**
- Create: `sniper/hyperlive/source_backfill.py`
- Create: `scripts/backfill_hl_copy_source_events.py`
- Create: `tests/test_hl_source_backfill.py`
- Modify: `sniper/hyperlive/intel_store.py`
- Modify: `tests/test_hyperlive_copy_capture.py`

**Interfaces:**
- Consumes: legacy copy rows, provider-exact `userFillsByTime` rows, existing shared API limiter.
- Produces: `reconstruct_entry_event(copy_row, fills, after_ts_ms) -> dict`; observational updates with `source_latency_status='historical_event_reconstructed'`.

- [ ] **Step 1: Write failing pure matching tests**

```python
assert reconstruct_entry_event(short_row, exact_fills, after_ts_ms=900)["status"] == "matched"
assert reconstruct_entry_event(cold_start_row, exact_fills, after_ts_ms=900)["status"] == "censored_cold_start"
assert reconstruct_entry_event(short_row, ambiguous_fills, after_ts_ms=900)["status"] == "ambiguous"
assert reconstruct_entry_event(short_row, wrong_case_fills, after_ts_ms=900)["status"] == "unmatched"
```

- [ ] **Step 2: Run tests and observe the missing module failure**

Run: `python3 -m pytest tests/test_hl_source_backfill.py -q`

Expected: import failure because `source_backfill.py` does not exist.

- [ ] **Step 3: Implement strict matching and guarded persistence**

```python
def backfill_copy_source_event(conn, *, copy_id, source_event_ts_ms, tids):
    cursor = conn.execute(
        "UPDATE hl_copy_book SET source_event_ts_ms=?,source_event_tid=?,"
        "source_latency_status='historical_event_reconstructed' "
        "WHERE id=? AND source_latency_status='historical_not_captured' "
        "AND source_received_ts_ms IS NULL",
        (source_event_ts_ms, json.dumps(tids), copy_id),
    )
    return cursor.rowcount == 1
```

The matcher requires non-cold-start, provider-exact coin spelling, exact side, a bounded prior-scan window, present target size, and a unique final signed position within 2%. It never sets receive, decision, book, or mirror timestamps.

- [ ] **Step 4: Implement paginated dry-run/apply CLI**

```bash
python3 scripts/backfill_hl_copy_source_events.py \
  --db-path /home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite
python3 scripts/backfill_hl_copy_source_events.py \
  --db-path /home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite --apply
```

Paginate each wallet from the earliest eligible row to the provider's 10,000-fill ceiling. Commit once per wallet in apply mode and print exact matched/ambiguous/unavailable counts.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest tests/test_hl_source_backfill.py tests/test_hyperlive_copy_capture.py tests/test_hyperlive_copy_scorecard.py -q`

```bash
git add sniper/hyperlive/source_backfill.py sniper/hyperlive/intel_store.py \
  scripts/backfill_hl_copy_source_events.py tests/test_hl_source_backfill.py \
  tests/test_hyperlive_copy_capture.py
git commit -m "feat(hl): recover strict historical event times"
```

### Task 3: Compact paused-verdict UI

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: Edge Desk schema 3 `evidence_debt`, existing copy/study/atlas/positions detail.
- Produces: default paused verdict, four-stop evidence runway, two compact operator cards, one collapsed audit detail.

- [ ] **Step 1: Write failing UI contract assertions**

```python
assert 'id="hl-edge-verdict"' in html
assert 'id="hl-edge-runway"' in html
assert 'id="hl-edge-audit"' in html
assert 'LIVE EDGE PROOF' not in js
assert 'ALT-SHORT PAUSED' in js
assert "include_open=0" in js
```

- [ ] **Step 2: Run the UI test and observe missing compact surface**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py -q`

Expected: compact IDs and paused copy are absent; the stale copy fetch assertion also fails.

- [ ] **Step 3: Implement the minimum DOM/render replacement**

```html
<section id="hl-edge-verdict" aria-live="polite"></section>
<section id="hl-edge-runway" aria-label="Evidence collection progress"></section>
<section id="hl-edge-summary"></section>
<details id="hl-edge-audit"><summary>Audit detail</summary><div id="hl-edge-audit-body"></div></details>
```

Render only the verdict, four debt stops, copy total-book state, and next falsification outside the disclosure. Audit detail contains historical inference, runtime identity, concentration, and state counts; legacy position rows are omitted from the default DOM.

- [ ] **Step 4: Add restrained responsive styling**

Use the six design tokens from the spec, visible focus, native `<details>`, no animation, no external font or dependency, and one-column runway/card fallbacks below 720px.

- [ ] **Step 5: Run UI/API checks and commit**

Run: `node --check static/app.js`

Run: `python3 -m pytest tests/test_ui_algo_tabs.py tests/test_api_hyperlive_edge_desk.py -q`

```bash
git add templates/index.html static/app.js static/styles.css tests/test_ui_algo_tabs.py
git commit -m "feat(hl): make paused evidence verdict primary"
```

### Task 4: Immutable arms, verification, merge, and deployment

**Files:**
- Create: `data/research/altshort_study/forward_arm.v14.json`
- Modify: `scripts/hl_copy_watchdog.sh`
- Modify: `tests/test_hl_copy_watchdog.py`
- Modify: `tests/test_run_hl_copy_selection.py`

**Interfaces:**
- Consumes: exact core code commit SHA and v13 semantics.
- Produces: v14 deployment identity with byte-equal policy/targets/taxonomy/benchmark/regime fields.

- [ ] **Step 1: Add failing v14 rollover/watchdog tests**

Assert v14 rolls from v13, pins the exact core code commit, preserves every semantic field, and is the watchdog manifest.

- [ ] **Step 2: Run tests and observe missing v14 failures**

Run: `python3 -m pytest tests/test_hl_copy_watchdog.py tests/test_run_hl_copy_selection.py -q`

- [ ] **Step 3: Add v14 manifest and watchdog pin**

Set `rollover_reason` to `operator evidence reporting and depth-capture health; frozen cohort and strategy semantics unchanged` and use the exact `git rev-parse HEAD` from the core implementation commit.

- [ ] **Step 4: Run full focused verification**

Run: `python3 -m pytest tests/test_api_hyperlive_edge_desk.py tests/test_hl_source_backfill.py tests/test_hl_source_fills.py tests/test_hl_copy_watchdog.py tests/test_hyperlive_copy_capture.py tests/test_hyperlive_copy_scorecard.py tests/test_run_hl_copy_selection.py tests/test_ui_algo_tabs.py -q`

Run: `node --check static/app.js`

Run: `python3 scripts/validate_schema_refs.py`

Run: `git diff --check`

- [ ] **Step 5: Complete Knowledge Sync, push, merge, and deploy**

Create a PR whose body ends with the 11-question Knowledge Sync block. After required checks pass, merge to `main`, create immutable Hyperliquid and web release worktrees, atomically switch both symlinks, restart only verified paper/web PIDs, and preserve rollback symlinks.

- [ ] **Step 6: Run the free backfill and live proof**

Run dry-run first, back up `hl_intel.sqlite`, then run `--apply`. Verify SQLite integrity, reconstructed rows with receive time still `NULL`, exact worker counts, no funded worker, authenticated Edge Desk HTTP 200/no-store/schema 3, paused verdict, debt counts, source health, and unchanged strategy semantics.
