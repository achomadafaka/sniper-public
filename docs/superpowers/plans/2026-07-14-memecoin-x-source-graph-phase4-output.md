# Memecoin X Source Graph Phase 4 Deterministic Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add display-only X source events, a read-only API/UI, bounded notification outbox, and deterministic draft-thesis text without coupling to positions or execution.

**Architecture:** A deterministic event builder consumes exact eligible X observations after an audited cursor and persists immutable display-only events. A separate Flask blueprint opens the X DB read-only per request. Notification delivery is an isolated, disabled-by-default outbox worker; the dashboard always renders deterministic evidence before any optional LLM annotation exists.

**Tech Stack:** Python 3.13, SQLite, Flask, vanilla JavaScript, pytest, Node syntax tests, systemd user units. No new dependencies.

## Global Constraints

- Phase 3 must be green before this plan starts.
- Do not import `api_memecoin.py`, real-runner, execution, strategy, position, wallet, or funded-runtime modules into any `memecoin_x` output module.
- Never query position or execution tables for deduplication, suppression, sizing, veto, or context.
- Every surfaced item is labeled `display_only`; no BUY/SELL buttons or execution verbs.
- Deterministic output must not wait for an LLM.
- New authors are review suggestions only. No automatic following.
- Official Robinhood accounts are context only; Robinhood `4663` coin evidence remains separate from official narrative.

---

### Task 1: Add deterministic event and notification audit tables

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_brain_index.py`

**Tables:**

```sql
CREATE TABLE memecoin_x_signal_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    after_observation_id INTEGER NOT NULL,
    through_observation_id INTEGER,
    events_created INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('running','ok','error')),
    error_detail TEXT
);

CREATE TABLE memecoin_x_signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_key TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('capture_tag','fast_consensus','tier2_scout','official_context')),
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    source_handle TEXT,
    evidence_observation_ids_json TEXT NOT NULL,
    actor_handles_json TEXT NOT NULL,
    relationship_counts_json TEXT NOT NULL,
    display_only INTEGER NOT NULL DEFAULT 1 CHECK (display_only=1),
    created_at TEXT NOT NULL
);

CREATE TABLE memecoin_x_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_key TEXT UNIQUE NOT NULL,
    signal_event_id INTEGER NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('dashboard','telegram')),
    state TEXT NOT NULL CHECK (state IN ('pending','sent','failed','suppressed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at TEXT,
    bounded_error TEXT
);

CREATE TABLE memecoin_x_notification_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('sent','failed','suppressed')),
    latency_ms REAL,
    bounded_error TEXT
);
```

- [ ] **Step 1: Write failing schema/CRUD tests**

Prove enum checks, unique logical keys, point-in-time JSON payload persistence, cursor advancement only on `ok`, and immutable `display_only=1`.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_store.py tests/test_brain_index.py -q`

- [ ] **Step 3: Implement minimal CRUD and indexes**

Add indexes on `(event_type, triggered_at)`, `(network_id, mint, triggered_at)`, and `(state, created_at)`.

- [ ] **Step 4: Verify, regenerate index, and commit**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_brain_index.py -q
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_store.py tests/test_brain_index.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): persist display-only signal events"
```

---

### Task 2: Build deterministic tags, scouts, and consensus

**Files:**
- Create: `sniper/analysis/memecoin_x/signals.py`
- Create: `sniper/services/memecoin_x_signal_builder.py`
- Create: `scripts/run_memecoin_x_signal_builder.py`
- Create: `tests/test_memecoin_x_signals.py`
- Create: `tests/test_memecoin_x_signal_builder.py`

**Interfaces:**
- `events_for_observation(conn, *, observation_id: int) -> list[dict]`
- `run_once(conn, *, run_id: str, observed_at: str, limit=1000) -> dict`

**Rules:**

- `capture_tag`: one exact eligible `(observation_id, network_id, mint)` fact.
- `tier2_scout`: exact eligible post whose source role at capture is `tier2_source`.
- `official_context`: exact eligible post whose source role is `official_context`; never enqueue Telegram.
- `fast_consensus`: the current observation is the second distinct point-in-time Tier-1 actor for the exact coin within the prior 60 minutes. Relationship may be direct/repost/quote/reply; count each actor once.
- Logical keys use immutable evidence, for example `fast_consensus:{network}:{mint}:{second_observation_id}`. A third actor does not rewrite the event.
- The source-graph measurement horizon remains six hours. The 60-minute rule affects alerts only.

- [ ] **Step 1: Write failing boundary tests**

Cover 59:59 inclusion, 60:00 boundary behavior fixed to `<=3600`, same-actor deduplication, two different coins, same address on `4663` and `8453`, official exclusion, delayed rerun idempotency, and cursor recovery after failure.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_signals.py tests/test_memecoin_x_signal_builder.py -q`

- [ ] **Step 3: Implement pure event construction and audited runner**

Process `memecoin_x_card_observations.id` in ascending order. Persist the signal event before inserting any notification outbox row. Enqueue dashboard rows for every event, Telegram rows only for `fast_consensus` and `tier2_scout`.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_signals.py tests/test_memecoin_x_signal_builder.py -q
git add sniper/analysis/memecoin_x/signals.py sniper/services/memecoin_x_signal_builder.py scripts/run_memecoin_x_signal_builder.py tests/test_memecoin_x_signals.py tests/test_memecoin_x_signal_builder.py
git commit -m "feat(memecoin-x): derive deterministic source events"
```

---

### Task 3: Add an isolated read-only Flask blueprint

**Files:**
- Create: `sniper/routes/api_memecoin_x.py`
- Modify: `sniper/web/server.py`
- Create: `tests/test_api_memecoin_x.py`

**Endpoints:**

- `GET /api/memecoin/x/status`
- `GET /api/memecoin/x/sources?window=24h|7d|30d&network_id=4663`
- `GET /api/memecoin/x/events?limit=100&network_id=4663`
- `GET /api/memecoin/x/suggestions?window=7d`
- `GET /api/memecoin/x/context?window=24h`

**Context contract:**

```python
create_api_memecoin_x_blueprint({
    "db_path": os.getenv("MEMECOIN_X_DB_PATH", "data/memecoin_x.db"),
    "clock_offset_path": os.getenv("SNIPER_CLOCK_OFFSET_FILE"),
})
```

- [ ] **Step 1: Write failing endpoint tests**

Tests must use a temporary X DB and prove missing DB/table, unhealthy clock, and stale capture return HTTP 200 with `available=false` plus a reason; healthy empty data returns `available=true` with empty arrays. Reject invalid windows/networks/limits with HTTP 400. Assert every response includes `display_only=true`.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_api_memecoin_x.py -q`

- [ ] **Step 3: Implement separate blueprint and registration**

Open a fresh `get_readonly_connection` per request and close it in `finally`. Do not attach the X DB to the main web connection. Registration failure must log loudly and leave only this blueprint unavailable; it must not disable the existing memecoin dashboard.

- [ ] **Step 4: Add an isolation source test**

Read `api_memecoin_x.py` and its imports; fail if it contains `execution`, `real_runner`, `position`, `manual_buy`, `strategy`, or `funded`.

- [ ] **Step 5: Verify and commit**

```bash
python3 -m pytest tests/test_api_memecoin_x.py tests/test_memecoin_api.py -q
git add sniper/routes/api_memecoin_x.py sniper/web/server.py tests/test_api_memecoin_x.py
git commit -m "feat(memecoin-x): expose read-only source graph API"
```

---

### Task 4: Add the X Sources dashboard subtab

**Files:**
- Modify: `static/app.js`
- Create: `tests/test_memecoin_x_ui_contract.py`

**UI contract:**

- Add subtab `X Sources` inside the existing memecoin tab.
- Header shows capture status, last healthy run, coverage gaps, clock health, and `display-only` badge.
- Filters: `24h`, `7d`, `30d`; network filter labels `solana`, `Robinhood 4663`, `Base 8453`, `Ethereum 1`, `BSC 56`, and any observed unknown ID verbatim.
- Tables: source rankings, recent deterministic events, review suggestions, official context.
- Source table shows separate exact calls, followed calls/rate, amplified links, coincidence links, distinct Tier-1 buyers, and direct/repost/quote/reply counts.
- Event rows show source, network, address, X observed time, Fomo observed time when linked, lead seconds, evidence lane, relationship, and coverage state.
- No trade action, BUY button, sizing, veto, safety verdict, auto-follow, or expected-return text.

- [ ] **Step 1: Write failing static contract tests**

Assert the subtab, endpoint paths, Robinhood label, display-only copy, empty/unavailable copy, and absence of trade-action markup inside the X renderer.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_ui_contract.py -q`

- [ ] **Step 3: Implement escaped rendering**

Use existing `esc`, `_ageRealIso`, table, pill, and subtab patterns. Fetch only when the X subtab is active. Render unavailable state distinctly from a healthy empty state.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_ui_contract.py tests/test_web_server_asset_paths.py -q
node --check static/app.js
git add static/app.js tests/test_memecoin_x_ui_contract.py
git commit -m "feat(memecoin-x): add display-only source graph panel"
```

---

### Task 5: Add bounded notification delivery, disabled by default

**Files:**
- Create: `sniper/services/memecoin_x_notifications.py`
- Create: `scripts/run_memecoin_x_notifications.py`
- Create: `tests/test_memecoin_x_notifications.py`

**Interfaces:**
- `render_notification(event: dict, evidence: dict) -> str`
- `deliver_pending(conn, *, sender, observed_at, enabled=False, limit=20) -> dict`
- `sender(text: str) -> tuple[bool, float | None, str | None]`

- [ ] **Step 1: Write failing delivery tests**

Prove deterministic alert is already persisted before delivery, disabled mode suppresses no rows and performs zero calls, success sends once, lost response retry is deduped by outbox logical key, failures append attempts and retain pending until three tries, third failure sets `failed`, and no position/execution query occurs.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_notifications.py -q`

- [ ] **Step 3: Implement disabled-by-default worker**

Default CLI is `--dry-run`. Actual Telegram delivery requires both `--send` and `MEMECOIN_X_NOTIFICATIONS_ENABLED=1`. Read credentials from environment at send time; never persist or log tokens. Use `urllib.request` in this isolated module rather than importing a trading runner's sender.

Notification language must say `observed correlation` or `source candidate`, never `alpha`, `entry`, `buy`, `safe`, `veto`, `size`, or a return forecast.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_notifications.py -q
git add sniper/services/memecoin_x_notifications.py scripts/run_memecoin_x_notifications.py tests/test_memecoin_x_notifications.py
git commit -m "feat(memecoin-x): add gated informational notifications"
```

---

### Task 6: Phase-4 deterministic gate

- [ ] **Step 1: Run focused tests and static guards**

```bash
python3 -m pytest \
  tests/test_memecoin_x_store.py \
  tests/test_memecoin_x_read_models.py \
  tests/test_memecoin_x_signals.py \
  tests/test_memecoin_x_signal_builder.py \
  tests/test_api_memecoin_x.py \
  tests/test_memecoin_x_ui_contract.py \
  tests/test_memecoin_x_notifications.py -q
node --check static/app.js
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 2: Manual dry-run**

Use a disposable copy of the X DB. Build signals, open the dashboard, compare every visible count to direct SQL, and run the notification worker without `--send`. Record the report; do not enable Telegram under this plan.
