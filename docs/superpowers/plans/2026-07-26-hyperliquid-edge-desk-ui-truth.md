# Hyperliquid Edge Desk UI Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the obsolete Hyperliquid placeholder and unbounded analyzer with a truthful, read-only Edge Desk and bounded wallet console.

**Architecture:** A Flask-independent `sniper.hyperlive.edge_desk` read model opens `hl_intel.sqlite` read-only, verifies local evidence files, and classifies open paper rows from current provenance, source sync, and persisted marks. `api_hyperlive.py` exposes the read model and a bounded analyzer response; the existing single-page UI renders those contracts without destructive controls.

**Tech Stack:** Python 3.13, Flask, SQLite JSON1, vanilla JavaScript, existing dashboard CSS, pytest, Playwright CLI.

## Global Constraints

- UI/API work stays on `feature/hl-edge-desk-ui-truth`; the coin-key runner fix stays on `fix/hl-copy-coin-key-normalization`.
- No exchange, SDK, RPC, price-history, CLOB, runner, watchdog, cron, funded database, or production-service mutation.
- Open-copy counts, symbols, and wallet counts are derived at request time; none may be hardcoded.
- `hl_intel.sqlite` is opened with SQLite `mode=ro`; the endpoint does not call `ensure_schema()`.
- Current arm identity requires a fresh heartbeat that matches both manifest and persisted arm.
- Paper/source freshness is 300,000 ms; future tolerance is 60,000 ms; age uses `real_utc_now()`.
- Study numbers render only after SHA-256 verification of the manifest-referenced report.
- The passive full-period short result is not a headline.
- Legacy and uncertain rows are collapsed by default but remain counted and inspectable.
- No close, purge, promote, fund, override, deploy, or restart control is added.
- A completed branch does not authorize restarting the web service or `run_hl_copy.py`.

---

### Task 1: Repair the pre-existing cache-bust assertion

**Files:**
- Modify: `tests/test_ui_algo_tabs.py:86-92`

**Interfaces:**
- Consumes: the template contract `<script src="/static/app.js?v={{ asset_v.app_js }}" defer>`.
- Produces: a static assertion that accepts the current Jinja asset version and still rejects an unversioned script.

- [ ] **Step 1: Replace the stale literal-number assertion**

```python
assert re.search(
    r'/static/app\.js\?v=(?:\d+|\{\{\s*asset_v\.app_js\s*\}\})',
    html,
), "versioned app.js script tag missing"
assert '<script src="/static/app.js"' not in html
```

- [ ] **Step 2: Run the previously failing test**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py::test_hyperliquid_analyzer_splits_copy_book_tabs_and_cleaner_controls -q`

Expected: `1 passed`.

- [ ] **Step 3: Run the baseline UI/API suite**

Run: `python3 -m pytest tests/test_api_hyperlive.py tests/test_ui_algo_tabs.py -q`

Expected: all tests pass.

- [ ] **Step 4: Commit the isolated baseline repair**

```bash
git add tests/test_ui_algo_tabs.py
git commit -m "test(ui): accept dynamic asset cache version"
```

### Task 2: Build the read-only position classifier

**Files:**
- Create: `sniper/hyperlive/edge_desk.py`
- Create: `tests/test_hl_edge_desk.py`

**Interfaces:**
- Produces: `comparison_key(symbol: object) -> str`.
- Produces: `classify_open_positions(conn: sqlite3.Connection, *, now_ms: int, current_arm_id: str | None, copy_heartbeat: dict | None, stale_after_ms: int = 300_000, future_tolerance_ms: int = 60_000) -> dict`.
- Return contract: `{"counts": dict[str, int], "groups": dict[str, list[dict]], "total": int}` with group keys `current_lane`, `draining_legacy`, `reconcile_ready`, `reconcile_blocked`, and `source_unverified`.

- [ ] **Step 1: Write fixture helpers and failing classification tests**

Create a temporary intel DB with `IS.ensure_schema()`, then seed rows using existing store methods. Tests must cover these exact outcomes:

```python
def test_classifies_current_draining_ready_blocked_and_unverified(tmp_path):
    # current: source matches, active target, current-arm observation
    # draining: source matches, inactive target or no current-arm observation
    # ready: fresh source no longer matches and case-insensitive persisted mark exists
    # blocked: fresh source no longer matches and no usable mark exists
    # unverified: source sync older than 300_000 ms
    payload = ED.classify_open_positions(
        conn, now_ms=1_000_000, current_arm_id="wallet-copy-v2",
        copy_heartbeat={"ts_ms": 990_000},
    )
    assert payload["counts"] == {
        "current_lane": 1,
        "draining_legacy": 1,
        "reconcile_ready": 1,
        "reconcile_blocked": 1,
        "source_unverified": 1,
    }
    assert sum(payload["counts"].values()) == payload["total"]

def test_classifier_matches_k_token_case_without_rewriting_symbol(tmp_path):
    row = _classify_one(book_coin="kSHIB", source_positions={}, mark_coin="KSHIB")
    assert row["state"] == "reconcile_ready"
    assert row["coin"] == "kSHIB"
    assert row["current_mark"] > 0

def test_inactive_target_with_current_arm_provenance_is_draining(tmp_path):
    row = _classify_one(current_arm=True, target_active=False, source_matches=True)
    assert row["state"] == "draining_legacy"
    assert row["current_arm"] is True

def test_normalized_collision_fails_closed(tmp_path):
    row = _classify_one(source_positions={"kPEPE": {}, "KPEPE": {}})
    assert row["state"] == "source_unverified"
    assert row["reason"] == "ambiguous_source_coin_key"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `python3 -m pytest tests/test_hl_edge_desk.py -q`

Expected: collection failure because `sniper.hyperlive.edge_desk` does not exist.

- [ ] **Step 3: Implement normalized indexes and freshness helpers**

Add these private boundaries without mutating inputs:

```python
STALE_AFTER_MS = 300_000
FUTURE_TOLERANCE_MS = 60_000
POSITION_STATES = (
    "current_lane", "draining_legacy", "reconcile_ready",
    "reconcile_blocked", "source_unverified",
)

def comparison_key(symbol: object) -> str:
    return str(symbol or "").casefold()

def _unique_index(mapping: dict) -> tuple[dict[str, tuple[str, object]], set[str]]:
    index: dict[str, tuple[str, object]] = {}
    collisions: set[str] = set()
    for original, value in mapping.items():
        key = comparison_key(original)
        if not key or key in index:
            collisions.add(key)
            index.pop(key, None)
        elif key not in collisions:
            index[key] = (str(original), value)
    return index, collisions

def _fresh(ts_ms: object, *, now_ms: int, stale_after_ms: int, future_tolerance_ms: int) -> bool:
    try:
        age = int(now_ms) - int(ts_ms)
    except (TypeError, ValueError):
        return False
    return -int(future_tolerance_ms) <= age <= int(stale_after_ms)
```

- [ ] **Step 4: Implement one read-only query pass and priority classification**

Use `sqlite_master`/`PRAGMA table_info` checks before queries. Load open rows, targets, source state, current-arm observations, and the newest mark per comparison key. Build each result row with:

```python
{
    "id": int(row["id"]),
    "target": row["target"],
    "coin": row["coin"],
    "side": row["side"],
    "entry_mark": row["entry_mark"],
    "detect_ts_ms": row["detect_ts_ms"],
    "is_cold_start": bool(row["is_cold_start"]),
    "target_active": target_active,
    "current_arm": current_arm,
    "source_sync_ts_ms": source_sync_ts_ms,
    "source_fresh": source_fresh,
    "source_matches": source_matches,
    "mark_available": mark_available,
    "current_mark": current_mark,
    "unrealized_bps": unrealized_bps,
    "state": state,
    "reason": reason,
}
```

Apply the spec's priority order exactly. Calculate long/short unrealized bps without exit fees; leave it null when entry or current mark is unusable.

- [ ] **Step 5: Prove the read model does not write**

Add a test that opens the fixture with `file:<path>?mode=ro`, calls the classifier, and compares `conn.total_changes` before/after. It must remain zero.

- [ ] **Step 6: Run focused tests**

Run: `python3 -m pytest tests/test_hl_edge_desk.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add sniper/hyperlive/edge_desk.py tests/test_hl_edge_desk.py
git commit -m "feat(hl): classify paper book truthfully"
```

### Task 3: Assemble and expose the Edge Desk payload

**Files:**
- Modify: `sniper/hyperlive/edge_desk.py`
- Modify: `sniper/routes/api_hyperlive.py`
- Create: `tests/test_api_hyperlive_edge_desk.py`

**Interfaces:**
- Consumes: `classify_open_positions(...)` from Task 2.
- Produces: `build_payload(*, db_path: str, data_dir: Path, now_ms: int) -> dict`.
- Produces: `GET /api/hyperlive/edge-desk`.

- [ ] **Step 1: Write failing API tests with a temporary data directory**

Monkeypatch `api_hyperlive.INTEL_DB`, `api_hyperlive.DATA_DIR`, and `api_hyperlive.real_utc_now`. Seed matching manifests, heartbeats, persisted arms, a report file, and its SHA. Assert:

```python
body = client.get("/api/hyperlive/edge-desk").get_json()
assert body["available"] is True
assert body["as_of_ts_ms"] == 1_000_000
assert body["standalone"]["arm_id"] == "standalone-v2"
assert body["wallet_copy"]["arm_id"] == "wallet-copy-v2"
assert body["real"]["dry_run"] is True
assert body["study"]["integrity"] == "verified"
assert body["study"]["events"]["mean_residual_bps"] == 154.76
assert body["study"]["regime_replication"]["episode_count"] == 1
assert body["positions"]["total"] == sum(body["positions"]["counts"].values())
```

Add separate tests for report SHA mismatch, stale/malformed heartbeat, missing DB, schema missing, and future raw-host heartbeat. A SHA mismatch must set `study.integrity="failed"` and omit numerical study fields.

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `python3 -m pytest tests/test_api_hyperlive_edge_desk.py -q`

Expected: 404 or import failure for the missing endpoint/builder.

- [ ] **Step 3: Implement bounded JSON and SHA helpers**

```python
MAX_JSON_BYTES = 2_000_000

def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None, "too_large"
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value, None) if isinstance(value, dict) else (None, "not_object")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError):
        return None, "malformed"
```

Resolve the report path under the repository/data root, reject traversal, compute SHA-256, and compare it to `retrospective.report_sha256` before copying allowed v3 fields.

- [ ] **Step 4: Implement current-arm and heartbeat validation**

For standalone and wallet-copy separately, require heartbeat freshness, heartbeat arm ID equal to manifest arm ID, and a persisted `hl_altshort_arms` row with the same arm ID/policy hash. Return source-local `state`, `fresh`, `age_ms`, `reason`, and counts. Preserve `enabled`/`dry_run` fields from a parseable real heartbeat even when its timestamp is invalid, but label freshness invalid.

- [ ] **Step 5: Implement the read-only database boundary**

Open with:

```python
conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

Do not import or call `IS.ensure_schema`. Catch missing schema as a source-specific error, then continue returning file-backed runtime blocks.

- [ ] **Step 6: Add the Flask route**

```python
@bp.get("/api/hyperlive/edge-desk")
def edge_desk():
    from pathlib import Path
    from sniper.hyperlive import edge_desk as ED
    from sniper.util.clock import real_utc_now
    return jsonify(ED.build_payload(
        db_path=INTEL_DB,
        data_dir=Path(DATA_DIR),
        now_ms=int(real_utc_now() * 1000),
    ))
```

- [ ] **Step 7: Run focused API/read-model tests**

Run: `python3 -m pytest tests/test_hl_edge_desk.py tests/test_api_hyperlive_edge_desk.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add sniper/hyperlive/edge_desk.py sniper/routes/api_hyperlive.py tests/test_api_hyperlive_edge_desk.py
git commit -m "feat(hl): expose edge desk read model"
```

### Task 4: Bound the analyzer API

**Files:**
- Modify: `sniper/routes/api_hyperlive.py:742-774`
- Modify: `tests/test_api_hyperlive.py`

**Interfaces:**
- Produces query parameters `view=attention|archive|all`, `q`, `verdict`, `page`, and `page_size`.
- Produces response fields `traders`, `page`, `page_size`, `total`, `has_more`, and `counts` while preserving existing copy/screen fields.

- [ ] **Step 1: Add failing pagination/filter tests**

Seed at least 70 screened rows across active tracked, culled, proven, watch, and rejected states. Assert:

```python
first = client.get("/api/hyperlive/analyzer").get_json()
assert first["page"] == 1
assert first["page_size"] == 25
assert len(first["traders"]) <= 25
assert first["total"] > len(first["traders"])
assert first["has_more"] is True
assert set(first["counts"]) >= {"attention", "archive", "all"}

archive = client.get("/api/hyperlive/analyzer?view=archive&page=2&page_size=10").get_json()
assert archive["page"] == 2 and len(archive["traders"]) <= 10

exact = client.get("/api/hyperlive/analyzer?view=attention&q=0xculledexact").get_json()
assert [row["wallet"] for row in exact["traders"]] == ["0xculledexact"]

capped = client.get("/api/hyperlive/analyzer?page_size=9999").get_json()
assert capped["page_size"] == 100
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `python3 -m pytest tests/test_api_hyperlive.py -k 'analyzer and (page or filter or exact)' -q`

Expected: failures because the route returns every trader and no page metadata.

- [ ] **Step 3: Implement argument parsing and stable filtering**

Parse integers fail-soft with page minimum 1, default page size 25, maximum 100. Decorate rows before filtering. Define views:

```python
def _analyzer_view(row: dict) -> str:
    attention = (
        row.get("effective_verdict") == "proven"
        or bool(row.get("tracked") and row.get("lifecycle_status") == "active")
        or bool(row.get("copy"))
    )
    return "attention" if attention else "archive"
```

An exact wallet query bypasses the view boundary. Other queries search wallet, display/effective verdict, archetype, reason, lifecycle state, and lifecycle reason. Apply verdict after view/query. Sort copy targets first, proven second, active tracked third, then newest `ts_ms`, then wallet. Slice only after filtering and compute all page metadata from the filtered sequence.

- [ ] **Step 4: Preserve compatibility and summary truth**

Keep `traders` as the page item key so current consumers do not crash. Return unfiltered `counts` separately. Keep `copy_targets`, `copy_target_rows`, `blocked_copy_targets`, `copy`, `copy_engine`, and screen freshness.

- [ ] **Step 5: Run analyzer/API tests**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q`

Expected: all tests pass after updating old full-list assertions to request `view=all&page_size=100` where their fixture semantics require it.

- [ ] **Step 6: Commit**

```bash
git add sniper/routes/api_hyperlive.py tests/test_api_hyperlive.py
git commit -m "feat(hl): paginate analyzer wallets"
```

### Task 5: Render the HL Edge Desk

**Files:**
- Modify: `templates/index.html:519-535`
- Modify: `static/app.js:1350-1515`
- Modify: `static/styles.css`
- Modify: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: `GET /api/hyperlive/edge-desk` from Task 3.
- Produces: top-level DOM IDs `hl-edge-status`, `hl-edge-evidence`, `hl-edge-positions`, and `hl-edge-updated`.

- [ ] **Step 1: Replace obsolete static assertions with failing Edge Desk assertions**

```python
assert "No active Hyperliquid lane" not in html
assert 'id="hl-edge-status"' in html
assert 'id="hl-edge-evidence"' in html
assert 'id="hl-edge-positions"' in html
assert "fetchJson('/api/hyperlive/edge-desk'" in js
for forbidden in ("Fund account", "Restart runner", "Promote", "Purge positions"):
    assert forbidden not in html
```

- [ ] **Step 2: Run the static test and confirm RED**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py::test_polymarket_5m_maker_tab_archived_and_hyperliquid_tab_empty -q`

Expected: failure on the old empty-state contract.

- [ ] **Step 3: Replace the template section**

Use a semantic shell only; all values remain JS-rendered:

```html
<section id="tab-hl-algo" class="tab-panel">
  <div class="panel-header hl-edge-header">
    <div><div class="hl-edge-kicker">HYPERLIQUID / PAPER RESEARCH</div><h1 class="panel-title">HL Edge Desk</h1>
      <p class="panel-subtitle">Current evidence, regime, paper arms, and provenance. No funded controls.</p></div>
    <span id="hl-edge-updated" class="hl-edge-updated">Loading…</span>
  </div>
  <div id="hyperlive-board" class="hl-edge-desk">
    <section id="hl-edge-status" class="hl-edge-status" aria-label="Hyperliquid runtime status"></section>
    <section id="hl-edge-evidence" class="hl-edge-evidence"></section>
    <section id="hl-edge-positions" class="hl-edge-positions"></section>
  </div>
  <div id="btc-algo-board" hidden></div>
</section>
```

- [ ] **Step 4: Replace `hyperlive.load()` with the Edge Desk fetch/render loop**

Fetch once on tab activation, retain `_lastGood`, and poll every 30 seconds only while `hl-algo` is active. On failure, keep `_lastGood` and add a stale banner; with no prior payload, show a bounded unavailable state.

Implement escaped render helpers for:

- five status cells: regime, standalone, wallet copy, promotion gate, real dry-run;
- the sealed evidence receipt;
- one count strip and `<details>` per position state;
- row columns: coin/side, target, provenance, source sync, entry/current, unrealized, cold-start, reason.

The `current_lane` details element is open by default. All other states are closed. Never render passive-short performance as a headline.

- [ ] **Step 5: Add the operator-ledger visual system**

Define `.hl-edge-*` classes using existing CSS variables. Required behavior:

- status strip uses one-pixel separators and tabular numbers;
- evidence receipt has one amber `ONE BEARISH EPISODE` stamp;
- `PAPER`, `DRY_RUN`, `STALE`, and `UNKNOWN` use text plus color, never color alone;
- position groups expose counts even when closed;
- at `max-width: 768px`, status cells stack two columns then one column and position rows become labeled blocks;
- no horizontal page overflow at 390 px.

- [ ] **Step 6: Run static UI tests**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html static/app.js static/styles.css tests/test_ui_algo_tabs.py
git commit -m "feat(hl): render edge desk truth"
```

### Task 6: Make analyzer pagination interactive

**Files:**
- Modify: `templates/index.html:537-625`
- Modify: `static/app.js:9599-10060`
- Modify: `static/styles.css:7193-7310`
- Modify: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: bounded analyzer contract from Task 4.
- Produces: `Attention`/`Archive` view controls plus previous/next pagination.

- [ ] **Step 1: Write failing static interaction assertions**

Assert the template includes `hl-analyzer-view`, `hl-analyzer-prev`, `hl-analyzer-next`, and `hl-analyzer-page`; JS must build `URLSearchParams` with `page_size=25` and must not filter a cached 964-row array for normal navigation.

- [ ] **Step 2: Run the focused static test and confirm RED**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py -k analyzer -q`

Expected: failure for missing controls/bounded request.

- [ ] **Step 3: Add bounded controls**

Add `Attention` and `Archive` options, previous/next buttons, page label, visible range, and total. Keep exact wallet paste/deep analysis unchanged.

- [ ] **Step 4: Refactor analyzer state and load**

Store `_view`, `_query`, `_filter`, `_page`, `_pageSize=25`, and `_pageMeta`. Build the request with `URLSearchParams`; debounce search by 200 ms; reset page to 1 when view/query/verdict changes. `_renderAnalyzerList()` renders only `response.traders`. Previous/next updates `_page` and refetches. Disable controls at boundaries.

- [ ] **Step 5: Update summary copy**

Show `attention`, `archive`, and all-store counts from the response summary. Do not label the entire screened store as an actionable list. Remove “pick one to mirror 24/7” copy from the page subtitle; wallet output remains research evidence, not advice.

- [ ] **Step 6: Run UI and API suites**

Run: `python3 -m pytest tests/test_ui_algo_tabs.py tests/test_api_hyperlive.py tests/test_api_hyperlive_edge_desk.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html static/app.js static/styles.css tests/test_ui_algo_tabs.py
git commit -m "feat(hl): bound analyzer wallet console"
```

### Task 7: Browser verification and branch-close evidence

**Files:**
- Modify only if defects are found: files from Tasks 3-6.
- Create artifacts under ignored `output/playwright/` only.

**Interfaces:**
- Consumes: completed Edge Desk and analyzer UI.
- Produces: desktop/mobile screenshots and final verification evidence.

- [ ] **Step 1: Run all focused tests**

Run:

```bash
python3 -m pytest tests/test_hl_edge_desk.py tests/test_api_hyperlive_edge_desk.py tests/test_api_hyperlive.py tests/test_ui_algo_tabs.py -q
```

Expected: all pass.

- [ ] **Step 2: Run hygiene checks**

Run: `python3 scripts/validate_schema_refs.py`

Expected: no schema violations.

Run: `git diff --check main...HEAD`

Expected: no output.

- [ ] **Step 3: Start an isolated development web process**

Use a non-production port and the existing dashboard auth configuration. Do not stop or replace the production PID. Point the development process at fixture/read-only data or the existing absolute read-only intel DB.

- [ ] **Step 4: Verify desktop in a real browser**

At 1440×1000, capture `output/playwright/hl-edge-desk-desktop.png`. Confirm:

- today's standalone and wallet-copy arms are visible;
- real is visibly `DRY_RUN`;
- one-episode warning is above the fold;
- group counts sum to total;
- only current lane is expanded;
- no destructive/funded controls exist;
- analyzer first response renders at most 25 wallet rows.

- [ ] **Step 5: Verify mobile**

At 390×844, capture `output/playwright/hl-edge-desk-mobile.png`. Confirm no horizontal page overflow, readable status labels, expandable position rows, and usable analyzer pagination.

- [ ] **Step 6: Inspect browser console/network**

Confirm zero uncaught errors, one bounded analyzer request per interaction, no exchange/CLOB requests, and the Edge Desk's only data request is `/api/hyperlive/edge-desk`.

- [ ] **Step 7: Apply only proven browser defects and rerun checks**

Every correction receives a failing regression test first where practical. Repeat Steps 1, 2, 4, and 5 after changes.

- [ ] **Step 8: Commit final verified corrections if any**

```bash
git add sniper/hyperlive/edge_desk.py sniper/routes/api_hyperlive.py templates/index.html static/app.js static/styles.css tests/test_hl_edge_desk.py tests/test_api_hyperlive_edge_desk.py tests/test_api_hyperlive.py tests/test_ui_algo_tabs.py
git commit -m "fix(hl): close edge desk verification gaps"
```

Skip this commit if the worktree is already clean.

- [ ] **Step 9: Prepare Knowledge Sync Gate evidence**

State `Knowledge delta: implementation of approved Hyperliquid Edge Desk design` and reference
`docs/superpowers/specs/2026-07-26-hyperliquid-edge-desk-ui-truth-design.md`. Do not write Supabase,
open a PR, push, merge, or restart services without separate operator instruction.
