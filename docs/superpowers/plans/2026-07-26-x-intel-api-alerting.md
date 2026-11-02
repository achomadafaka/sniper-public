# X Intel API Capture and Cashtag Alerting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move X intelligence into its own Intel subtab and build a default-OFF official X API capture, recovery, deterministic cashtag-event, and Telegram-alert pipeline that needs only operator credentials and explicit enablement.

**Architecture:** A dedicated X API worker compiles Filtered Stream rules from the active curated roster, normalizes structured Post entities, and atomically persists observations, ticker-native signal events, resource accounting, cursor state, and notification outbox rows in `memecoin_x.db`. A separate notifier sends idempotent Telegram messages, while canonical `/api/intel/x/*` routes render the same isolated evidence under a new Intel/X UI with no funded coupling.

**Tech Stack:** Python 3, SQLite, Flask, stdlib `urllib`, pytest, vanilla JavaScript/CSS, user-level systemd.

## Global Constraints

- Work only in the isolated `feat/x-intel-api` worktree.
- Follow RED-GREEN TDD for every production behavior; watch each new test fail for the intended missing behavior before implementing.
- Never call live X, Telegram, CLOB, quote, RPC, or price-history endpoints during implementation or tests.
- X capture and Telegram delivery remain independently default OFF.
- Only active curated X accounts may enter stream rules; never discover or follow accounts automatically.
- Only Posts with structured X cashtags are eligible; a text-regex fallback is diagnostic-only.
- Never call a cashtag mention a promotion, and never infer `(network_id, mint)` from a ticker alone.
- The only allowed X POST is project-scoped Filtered Stream rule management. No account/content writes.
- Preserve corrected real-UTC `observed_at`; X `created_at` stays source metadata.
- Persist evidence and notification work before external delivery. Never advance a frontier past uncommitted work.
- No funded runner, reservation, position, entry, exit, execution, production DB, or pinned-runtime changes.
- Do not enable services or write credentials. Handoff must require only the bearer token, Telegram values, and explicit operator enablement.

## File Structure

- `sniper/persistence/db_memecoin_x.py`: versioned schema migration and atomic X API event/state/resource/outbox persistence.
- `sniper/analysis/memecoin_x/api_rules.py`: pure curated-roster Filtered Stream rule compiler.
- `sniper/analysis/memecoin_x/api_normalize.py`: pure X API payload normalization and mention/amplification classification.
- `sniper/services/memecoin_x_api_client.py`: narrow stdlib HTTP/stream transport with authorization redaction.
- `sniper/services/memecoin_x_api_capture.py`: rule reconciliation, capture, recovery, frontier, resource cap, and health state machine.
- `sniper/settings/x_intel.py`: strict default-OFF environment contract.
- `scripts/run_memecoin_x_api_capture.py`: fixture smoke, bounded once/dry-run, and continuous worker CLI.
- `sniper/services/memecoin_x_notifications.py`: cashtag/consensus/health rendering, stale digest, and burst policy.
- `scripts/run_memecoin_x_notifications.py`: persistent default-OFF notifier CLI.
- `sniper/routes/api_memecoin_x.py`: shared read implementation registered under canonical Intel routes.
- `templates/index.html`, `static/app.js`, `static/styles.css`: dedicated Intel/X view and removal from Memecoin.
- `systemd/sniper-x-intel-{capture,notifications}.service`: disabled-by-default user services.
- `scripts/install_memecoin_x_services.sh`: install and preflight new units without enabling them by default.
- `tests/fixtures/memecoin_x/api/`: complete sanitized API payload fixtures.

---

### Task 1: Migrate the X Store for API Identity, Events, State, and Resources

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`
- Create: `tests/test_memecoin_x_api_store.py`

**Interfaces:**
- Consumes: existing `ensure_memecoin_x_tables(conn)`, `persist_card(conn, normalized, *, observed_at, clock_domain, capture_run_id, now_iso, commit=True)`, `persist_signal_event(conn, event, *, created_at, commit=True)`, and notification outbox helpers.
- Produces:
  - `active_api_roster(conn) -> list[dict]`
  - `api_capture_state(conn) -> dict`
  - `update_api_capture_state(conn, *, expected_revision: int, **updates) -> dict`
  - `count_api_resource(conn, *, utc_day: str, resource_id: str, source: str, counted_at: str, commit=True) -> bool`
  - generalized `persist_signal_event(conn, event, *, created_at, commit=True) -> int`
  - `persist_api_event(conn, normalized, *, run_id: str, observed_at: str, clock_domain: str, created_at: str) -> dict`

- [ ] **Step 1: Write migration and identity tests that fail against the legacy schema**

Add these concrete tests:

<!-- schema-validator: ignore-next-block -->
```python
def test_legacy_exact_signal_rows_migrate_with_ids_and_outbox_links_preserved(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE memecoin_x_signal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, logical_key TEXT UNIQUE NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN
          ('capture_tag','fast_consensus','tier2_scout','official_context')),
        network_id TEXT NOT NULL, mint TEXT NOT NULL, triggered_at TEXT NOT NULL,
        source_handle TEXT, evidence_observation_ids_json TEXT NOT NULL,
        actor_handles_json TEXT NOT NULL, relationship_counts_json TEXT NOT NULL,
        display_only INTEGER NOT NULL DEFAULT 1 CHECK(display_only=1),
        created_at TEXT NOT NULL
      );
      CREATE TABLE memecoin_x_notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, logical_key TEXT UNIQUE NOT NULL,
        signal_event_id INTEGER NOT NULL, channel TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, last_attempt_at TEXT, sent_at TEXT,
        bounded_error TEXT
      );
      INSERT INTO memecoin_x_signal_events VALUES
        (7,'legacy:7','capture_tag','4663',
         '0x1111111111111111111111111111111111111111',
         '2026-07-26T12:00:00+00:00','alpha','[]','[]','{}',1,
         '2026-07-26T12:00:00+00:00');
      INSERT INTO memecoin_x_notification_outbox
        (id,logical_key,signal_event_id,channel,state,created_at)
      VALUES (9,'telegram:legacy:7',7,'telegram','pending',
              '2026-07-26T12:00:00+00:00');
    """)
    X.ensure_memecoin_x_tables(conn)
    X.ensure_memecoin_x_tables(conn)
    row = conn.execute(
        "SELECT id,identity_state,network_id,mint FROM memecoin_x_signal_events WHERE id=7"
    ).fetchone()
    assert tuple(row) == (
        7, "exact_coin", "4663", "0x1111111111111111111111111111111111111111")
    assert conn.execute(
        "SELECT signal_event_id FROM memecoin_x_notification_outbox WHERE id=9"
    ).fetchone()[0] == 7

def test_ticker_only_signal_requires_ticker_and_forbids_fake_coin_identity():
    event = {
        "logical_key": "x:post-1:user-1:MEME",
        "event_type": "cashtag_mention",
        "identity_state": "ticker_only",
        "ticker": "MEME",
        "network_id": None,
        "mint": None,
        "triggered_at": "2026-07-26T12:00:00+00:00",
        "post_id": "100",
        "post_url": "https://x.com/alpha/status/100",
        "relationship": "direct",
        "cashtag_source": "actor_text",
    }
    event_id = X.persist_signal_event(conn, event, created_at=event["triggered_at"])
    row = conn.execute(
        "SELECT ticker,identity_state,network_id,mint FROM memecoin_x_signal_events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert tuple(row) == ("MEME", "ticker_only", None, None)

def test_roster_uses_stable_user_id_and_excludes_inactive_accounts():
    conn.execute("INSERT INTO memecoin_x_accounts(handle,x_user_id,role,active) "
                 "VALUES ('alpha','100','tier1_trader',1),"
                 "('sleeping','200','tier2_source',0)")
    assert X.active_api_roster(conn) == [{
        "x_user_id": "100", "handle": "alpha", "display_handle": "alpha",
        "role": "tier1_trader",
    }]
    conn.execute("UPDATE memecoin_x_accounts SET handle='alpha_new' WHERE x_user_id='100'")
    assert X.active_api_roster(conn)[0]["x_user_id"] == "100"
    assert X.active_api_roster(conn)[0]["handle"] == "alpha_new"

def test_api_resource_is_counted_once_per_utc_day():
    assert X.count_api_resource(conn, utc_day="2026-07-26", resource_id="100",
                                source="stream", counted_at=NOW) is True
    assert X.count_api_resource(conn, utc_day="2026-07-26", resource_id="100",
                                source="recovery", counted_at=NOW) is False
```

- [ ] **Step 2: Run the new store tests and confirm the intended RED failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_api_store.py tests/test_memecoin_x_store.py -q
```

Expected: failures for missing `x_user_id`, API tables/state helpers, new signal fields/types, and legacy migration.

- [ ] **Step 3: Implement the versioned migration and persistence contracts**

Add `x_user_id` and `display_handle` compatibility columns to `memecoin_x_accounts`. Rebuild
`memecoin_x_signal_events` when its SQL lacks `cashtag_mention` or still declares exact identity
columns as unconditionally `NOT NULL`. Preserve explicit event IDs before restoring the outbox.

The new table contract must enforce:

```sql
CHECK (
  (identity_state='ticker_only' AND ticker IS NOT NULL AND network_id IS NULL AND mint IS NULL)
  OR (identity_state='exact_coin' AND network_id IS NOT NULL AND mint IS NOT NULL)
  OR (identity_state='not_applicable' AND event_type='capture_health')
)
```

Add:

```sql
CREATE TABLE IF NOT EXISTS memecoin_x_api_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  revision INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  roster_hash TEXT,
  rule_hash TEXT,
  frontier_post_id TEXT,
  recovery_since_id TEXT,
  recovery_next_token TEXT,
  recovery_max_post_id TEXT,
  last_healthy_at TEXT,
  last_recovery_at TEXT,
  updated_at TEXT NOT NULL,
  bounded_detail TEXT
);

CREATE TABLE IF NOT EXISTS memecoin_x_api_resources (
  utc_day TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('stream','recovery','reference','fixture')),
  counted_at TEXT NOT NULL,
  PRIMARY KEY(utc_day, resource_id)
);
```

Implement compare-and-swap state updates using `revision`. Keep `persist_api_event()` one
transaction: persist normalized card/relationship/mentions, all cashtag events, dashboard and
Telegram outbox rows, resource identity, and state changes; rollback all on any exception.

- [ ] **Step 4: Run store tests GREEN, then the existing store/signal/notifier tests**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_x_api_store.py \
  tests/test_memecoin_x_store.py \
  tests/test_memecoin_x_signal_builder.py \
  tests/test_memecoin_x_notifications.py -q
```

Expected: all pass with legacy exact events unchanged.

- [ ] **Step 5: Commit the schema and persistence slice**

```bash
git add sniper/persistence/db_memecoin_x.py \
  tests/test_memecoin_x_store.py tests/test_memecoin_x_api_store.py
git commit -m "feat(x-intel): add ticker-native event store"
```

---

### Task 2: Compile Curated Rules and Normalize Complete X API Payloads

**Files:**
- Create: `sniper/analysis/memecoin_x/api_rules.py`
- Create: `sniper/analysis/memecoin_x/api_normalize.py`
- Create: `tests/test_memecoin_x_api_rules.py`
- Create: `tests/test_memecoin_x_api_normalize.py`
- Create: `tests/fixtures/memecoin_x/api/direct.json`
- Create: `tests/fixtures/memecoin_x/api/reply.json`
- Create: `tests/fixtures/memecoin_x/api/quote_actor_tag.json`
- Create: `tests/fixtures/memecoin_x/api/quote_reference_tag.json`
- Create: `tests/fixtures/memecoin_x/api/repost_reference_tag.json`
- Create: `tests/fixtures/memecoin_x/api/edit_add_tag.json`

**Interfaces:**
- Consumes: rows from `active_api_roster(conn)` and `extract_coin_mentions(text, urls)`.
- Produces:
  - `compile_stream_rules(accounts: list[dict], *, max_rule_length=1024) -> list[dict]`
  - `roster_hash(accounts: list[dict]) -> str`
  - `normalize_api_payload(payload: dict, *, roster_by_user_id: dict[str, dict], observed_at: str) -> dict | None`

- [ ] **Step 1: Add RED tests for rule stability, bounds, and roster exclusion**

Use hand-derived expectations:

```python
def test_rules_are_stable_bounded_and_cashtag_only():
    accounts = [
        {"x_user_id": "2", "handle": "Beta", "role": "tier2_source"},
        {"x_user_id": "1", "handle": "alpha", "role": "tier1_trader"},
    ]
    rules = compile_stream_rules(accounts, max_rule_length=80)
    assert [r["value"] for r in rules] == [
        "(from:alpha OR from:beta) has:cashtags"
    ]
    assert rules[0]["tag"].startswith("sniper-x-intel:")

def test_rule_compiler_rejects_empty_or_unsafe_handles():
    with pytest.raises(ValueError, match="active curated roster"):
        compile_stream_rules([])
    with pytest.raises(ValueError, match="unsafe X handle"):
        compile_stream_rules([{
            "x_user_id": "1", "handle": "alpha OR from:evil",
            "role": "tier1_trader",
        }])
```

- [ ] **Step 2: Run rule tests and verify RED**

Run `python3 -m pytest tests/test_memecoin_x_api_rules.py -q`.

Expected: import failure because `api_rules.py` does not exist.

- [ ] **Step 3: Implement the pure rule compiler**

Normalize handles without `@`, validate `^[A-Za-z0-9_]{1,15}$`, sort case-insensitively, pack
whole `from:` terms without exceeding the passed rule limit, suffix every group with
`has:cashtags`, and tag it `sniper-x-intel:<roster-hash-prefix>:<group-index>`.

- [ ] **Step 4: Add RED normalization tests using full sanitized X fixtures**

Each fixture must contain the complete endpoint shape used in production: `data`,
`matching_rules`, and `includes.tweets/users`. Assert observable normalized output:

```python
def test_quote_with_tag_only_in_reference_is_amplification():
    event = normalize_api_payload(load("quote_reference_tag.json"),
                                  roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["event_type"] == "cashtag_amplification"
    assert event["relationship"] == "quote"
    assert event["cashtag_source"] == "referenced_post"
    assert event["cashtags"] == ["MEME"]
    assert event["post_url"] == "https://x.com/alpha/status/103"

def test_actor_entities_are_authoritative_over_text_regex():
    payload = copy.deepcopy(DIRECT)
    payload["data"]["text"] = "$500 $FAKE"
    payload["data"]["entities"] = {"cashtags": []}
    assert normalize_api_payload(
        payload, roster_by_user_id=ROSTER, observed_at=NOW) is None

def test_multiple_cashtags_are_uppercase_deduped_and_one_post_event():
    payload = copy.deepcopy(DIRECT)
    payload["data"]["entities"]["cashtags"] = [
        {"start": 0, "end": 5, "tag": "meme"},
        {"start": 6, "end": 11, "tag": "MEME"},
        {"start": 12, "end": 18, "tag": "other"},
    ]
    event = normalize_api_payload(payload, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["cashtags"] == ["MEME", "OTHER"]
    assert event["post_id"] == "101"

def test_handle_change_keeps_actor_user_id_and_new_display_handle():
    payload = copy.deepcopy(DIRECT)
    payload["includes"]["users"][0]["username"] = "alpha_new"
    event = normalize_api_payload(payload, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["actor_user_id"] == "1001"
    assert event["actor_handle"] == "alpha_new"
```

- [ ] **Step 5: Run normalization tests and verify RED**

Run `python3 -m pytest tests/test_memecoin_x_api_normalize.py -q`.

Expected: import failure because `api_normalize.py` does not exist.

- [ ] **Step 6: Implement normalization without network or persistence**

Resolve referenced Post objects from `includes.tweets`; choose relationship from
`referenced_tweets.type`; take cashtags only from structured entities; extract URLs from structured
entities; use existing contract/URL extraction only to establish exact identity when there is one
unambiguous eligible coin mention. Return `None` for inactive/unknown actors or no structured
cashtag evidence. Bound excerpt to 280 characters and construct the actor Post URL.

- [ ] **Step 7: Run rules and normalization tests GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_api_rules.py tests/test_memecoin_x_api_normalize.py -q
```

- [ ] **Step 8: Commit the pure API adapters**

```bash
git add sniper/analysis/memecoin_x/api_rules.py \
  sniper/analysis/memecoin_x/api_normalize.py \
  tests/test_memecoin_x_api_rules.py tests/test_memecoin_x_api_normalize.py \
  tests/fixtures/memecoin_x/api
git commit -m "feat(x-intel): normalize curated cashtag posts"
```

---

### Task 3: Build the Narrow X API Client and Recovery State Machine

**Files:**
- Create: `sniper/services/memecoin_x_api_client.py`
- Create: `sniper/services/memecoin_x_api_capture.py`
- Create: `tests/test_memecoin_x_api_client.py`
- Create: `tests/test_memecoin_x_api_capture.py`

**Interfaces:**
- Consumes: compiled rules, normalized payloads, corrected clock, store state/resource/event helpers.
- Produces:
  - `XApiClient(bearer_token, *, open_fn=urlopen)`
  - `XApiClient.list_rules() -> list[dict]`
  - `XApiClient.replace_rules(*, existing: list[dict], desired: list[dict]) -> None`
  - `XApiClient.recent_search(*, query: str, since_id: str | None, next_token: str | None, max_results=100) -> dict`
  - `XApiClient.stream(*, fields: str, expansions: str) -> Iterator[dict]`
  - `run_recovery(conn, client, *, settings, observed_at_fn) -> dict`
  - `ingest_payload(conn, payload, *, source, settings, observed_at_fn) -> dict`
  - `run_stream(conn, client, *, settings, observed_at_fn, stop_event) -> dict`

- [ ] **Step 1: Add RED client-boundary tests**

Tests must pass complete fake `urllib` responses and assert real request objects:

```python
def test_recent_search_uses_bearer_get_and_bounded_fields():
    client = XApiClient("secret", open_fn=fake_open)
    result = client.recent_search(query="from:alpha has:cashtags", since_id="100",
                                  next_token=None)
    assert result == FULL_RECENT_RESPONSE
    assert captured.method == "GET"
    assert captured.headers["Authorization"] == "Bearer secret"
    assert "since_id=100" in captured.full_url

def test_transport_errors_never_expose_bearer_token():
    with pytest.raises(XApiError) as exc:
        XApiClient("secret", open_fn=raise_url_error).list_rules()
    assert "secret" not in str(exc.value)
```

Add separate tests named `test_replace_rules_adds_desired_before_deleting_stale`,
`test_stream_ignores_keepalives_and_yields_complete_json`,
`test_stream_rejects_malformed_json`, `test_non_2xx_body_is_bounded`,
`test_rate_limit_exposes_retry_after_without_secret`, and
`test_response_larger_than_limit_is_rejected`. Each uses a complete fake response and asserts the
returned value or exact `XApiError.kind`.

- [ ] **Step 2: Run client tests RED**

Run `python3 -m pytest tests/test_memecoin_x_api_client.py -q`.

- [ ] **Step 3: Implement the minimal stdlib client**

Use only `https://api.x.com/2/tweets/search/stream`, `/rules`, and `/tweets/search/recent`.
Request `created_at,author_id,edit_history_tweet_ids,entities,public_metrics,referenced_tweets` plus
author/reference expansions. Bound error bodies, never interpolate request URLs containing secrets,
and classify auth, rate limit, transport, malformed payload, and server errors.

- [ ] **Step 4: Add RED state-machine tests for atomicity, recovery, and budget**

Add these exact state-machine tests:

```python
def test_stream_event_commits_post_event_outbox_resource_and_frontier_atomically():
    result = ingest_payload(conn, DIRECT, source="stream", settings=SETTINGS,
                            observed_at_fn=healthy_clock)
    assert result["events_created"] == 1
    assert state(conn)["frontier_post_id"] == "101"
    assert pending_telegram(conn) == 1

def test_persistence_failure_does_not_advance_frontier(monkeypatch):
    monkeypatch.setattr(X, "persist_signal_event", raise_after_post)
    with pytest.raises(RuntimeError):
        ingest_payload(conn, DIRECT, source="stream", settings=SETTINGS,
                       observed_at_fn=healthy_clock)
    assert state(conn)["frontier_post_id"] is None
    assert event_count(conn) == 0

def test_bounded_recovery_keeps_gap_and_resumes_next_token_without_advancing_frontier():
    client = FakeRecentClient([
        {**RECENT_PAGE, "meta": {"next_token": "page-2", "newest_id": "105"}},
        {**RECENT_LAST_PAGE, "meta": {"newest_id": "105"}},
    ])
    settings = dataclasses.replace(SETTINGS, recovery_max_pages=1)
    first = run_recovery(conn, client, settings=settings, observed_at_fn=healthy_clock)
    assert first["complete"] is False
    assert state(conn)["frontier_post_id"] == "100"
    assert state(conn)["recovery_next_token"] == "page-2"
    second = run_recovery(conn, client, settings=settings, observed_at_fn=healthy_clock)
    assert second["complete"] is True
    assert state(conn)["frontier_post_id"] == "105"
    assert state(conn)["recovery_next_token"] is None

def test_resource_cap_stops_after_persisting_the_resource_that_reaches_the_cap():
    settings = dataclasses.replace(SETTINGS, max_post_reads_per_day=1)
    stop = threading.Event()
    result = run_stream(conn, FakeStreamClient([DIRECT, REPLY]), settings=settings,
                        observed_at_fn=healthy_clock, stop_event=stop)
    assert result["state"] == "budget_exhausted"
    assert state(conn)["frontier_post_id"] == "101"
    assert resource_count(conn, "2026-07-26") == 1
    assert result["payloads_consumed"] == 1
```

Also cover stream/recovery overlap, oldest-first processing, UTC-day rollover, missing reference,
auth failure, reconnect backoff bounds, and health-event cooldown.

- [ ] **Step 5: Run capture tests RED**

Run `python3 -m pytest tests/test_memecoin_x_api_capture.py -q`.

- [ ] **Step 6: Implement rule reconciliation, ingest, recovery, and stream loops**

Process one payload inside one explicit transaction. Count unique Post resource IDs before deciding
whether to disconnect at the cap. Recovery must persist `recovery_since_id`, `recovery_next_token`,
and `recovery_max_post_id`; do not move `frontier_post_id` until pagination is exhausted. Sort each
complete recovery batch by numeric actor Post ID before ingest. Use bounded exponential backoff with
injected wait/stop functions so tests never sleep.

- [ ] **Step 7: Run client/capture/store tests GREEN**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_x_api_client.py \
  tests/test_memecoin_x_api_capture.py \
  tests/test_memecoin_x_api_store.py -q
```

- [ ] **Step 8: Commit the API transport and capture engine**

```bash
git add sniper/services/memecoin_x_api_client.py \
  sniper/services/memecoin_x_api_capture.py \
  tests/test_memecoin_x_api_client.py tests/test_memecoin_x_api_capture.py
git commit -m "feat(x-intel): capture and recover X API stream"
```

---

### Task 4: Add Strict Settings, Fixture Smoke, and Disabled Runtime Services

**Files:**
- Create: `sniper/settings/x_intel.py`
- Create: `scripts/run_memecoin_x_api_capture.py`
- Create: `systemd/sniper-x-intel-capture.service`
- Create: `tests/test_x_intel_settings.py`
- Create: `tests/test_memecoin_x_api_worker.py`
- Modify: `tests/test_memecoin_x_systemd_units.py`
- Modify: `scripts/install_memecoin_x_services.sh`

**Interfaces:**
- Consumes: API client/capture service and fixture payloads.
- Produces:
  - `XIntelSettings.from_env(env=os.environ, *, require_enabled=False) -> XIntelSettings`
  - CLI modes `--fixture <path>`, `--once`, and continuous default.

- [ ] **Step 1: Add RED settings tests**

Assert:

```python
def test_disabled_settings_need_no_secret_and_open_no_database_or_network():
    cfg = XIntelSettings.from_env({})
    assert cfg.enabled is False

def test_enabled_settings_require_token_healthy_paths_and_positive_bounds():
    with pytest.raises(ValueError, match="bearer token"):
        XIntelSettings.from_env({"X_INTEL_API_ENABLED": "1"})

def test_repr_redacts_bearer_token():
    cfg = XIntelSettings.from_env({
        "X_INTEL_API_ENABLED": "1",
        "X_INTEL_BEARER_TOKEN": "secret",
        "X_INTEL_DB_PATH": "/tmp/x-intel-test.db",
        "X_INTEL_MAX_POST_READS_PER_DAY": "500",
        "X_INTEL_RECOVERY_MAX_PAGES": "10",
    })
    assert "secret" not in repr(cfg)
```

- [ ] **Step 2: Run settings tests RED, then implement strict parsing**

Run `python3 -m pytest tests/test_x_intel_settings.py -q`, then implement:

- `X_INTEL_API_ENABLED=0`
- `X_INTEL_BEARER_TOKEN`
- `X_INTEL_DB_PATH=data/memecoin_x.db`
- `X_INTEL_MAX_POST_READS_PER_DAY=500`
- `X_INTEL_RECOVERY_MAX_PAGES=10`
- `X_INTEL_ALERT_MAX_AGE_S=900`
- `SNIPER_CLOCK_OFFSET_FILE`

Reject booleans outside `0|1`, nonpositive caps/pages, DB paths equal to known production/tape DBs,
and tokens containing whitespace/control characters.

- [ ] **Step 3: Add RED worker CLI tests by running the real script**

Assert disabled invocation exits `0` before DB/network construction, fixture mode uses a temporary
DB and prints deterministic JSON, enabled-without-token exits `2`, and exception output never
contains a supplied token. Patch only `urlopen`; run the real CLI entry point.

- [ ] **Step 4: Implement the CLI and fixture-only smoke**

`--fixture` must force source `fixture`, use injected file payloads, and never instantiate
`XApiClient`. `--once` performs rule reconciliation plus one bounded recovery pass and exits.
Continuous mode owns SIGTERM/SIGINT and passes a stop event to the capture loop.

- [ ] **Step 5: Add RED service/installer tests**

Assert the new capture unit is present, default disabled, uses a dedicated environment file path,
contains no funded runner command, and the installer installs but does not enable it without
`--enable-api`. Existing browser receiver units remain installable for rollback but are not enabled
by the new flag.

- [ ] **Step 6: Implement the service and installer flags**

Add `--enable-api` separately from existing `--enable`; require the environment file to exist with
mode `0600` before enabling API capture. `--check` remains credential-free and validates imports.

- [ ] **Step 7: Run settings, worker, and service tests GREEN**

```bash
python3 -m pytest \
  tests/test_x_intel_settings.py \
  tests/test_memecoin_x_api_worker.py \
  tests/test_memecoin_x_systemd_units.py -q
bash scripts/install_memecoin_x_services.sh --check
```

- [ ] **Step 8: Commit the default-OFF runtime slice**

```bash
git add sniper/settings/x_intel.py scripts/run_memecoin_x_api_capture.py \
  systemd/sniper-x-intel-capture.service scripts/install_memecoin_x_services.sh \
  tests/test_x_intel_settings.py tests/test_memecoin_x_api_worker.py \
  tests/test_memecoin_x_systemd_units.py
git commit -m "feat(x-intel): add disabled API capture runtime"
```

---

### Task 5: Deliver High-Recall Cashtag, Consensus, Digest, and Health Alerts

**Files:**
- Modify: `sniper/services/memecoin_x_notifications.py`
- Modify: `scripts/run_memecoin_x_notifications.py`
- Create: `systemd/sniper-x-intel-notifications.service`
- Modify: `tests/test_memecoin_x_notifications.py`
- Modify: `tests/test_memecoin_x_systemd_units.py`

**Interfaces:**
- Consumes: ticker-native signal events and the existing outbox.
- Produces:
  - `render_notification(event: dict, evidence: dict) -> str`
  - `select_notification_mode(conn, event: dict, *, observed_at: str, max_age_s=900) -> str`
  - `deliver_pending(conn, *, sender, observed_at, enabled=False, limit=20) -> dict`
  - persistent CLI `--loop` with interruptible polling.

- [ ] **Step 1: Add RED rendering tests for every relationship and identity state**

Use literal expected message fragments and assert:

- direct/reply actor tags say `mentioned in actor text`;
- quote/repost reference tags say `amplified from referenced Post`;
- ticker-only says `Identity: ticker only` and contains no `None` or fake mint;
- exact coin shows network and full mint;
- multiple tags render once as `$MEME $OTHER`;
- exact actor Post URL is present;
- negative text is not suppressed and the word `promotion` is absent; and
- dynamic text is bounded and contains no bearer or Telegram token.

- [ ] **Step 2: Run notification tests RED**

Run `python3 -m pytest tests/test_memecoin_x_notifications.py -q`.

- [ ] **Step 3: Implement deterministic rendering and delivery selection**

Use event fields only; no LLM or market lookup. Individual delivery applies to events at most 900
seconds old. Older recovery events enter one digest logical key per recovery run. Track actor burst
windows from persisted events: after five eligible Posts in ten minutes, send one burst warning and
suppress individual outbox rows into a digest without deleting evidence. Consensus events remain
one per ticker cluster.

- [ ] **Step 4: Add RED CLI/service tests**

Run the script with disabled, dry-run, once-send, and loop-stop fixtures. Assert the new notifier
unit uses its dedicated environment file, starts only when `--enable-telegram` is passed to the
installer, and never shares the funded memecoin service.

- [ ] **Step 5: Implement persistent notifier mode and installer gate**

Keep the existing three-attempt audit contract. Poll interruptibly; do not hold a transaction during
Telegram I/O. Use `X_INTEL_TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. Disabled
mode exits before opening the writable DB.

- [ ] **Step 6: Run notifier and service tests GREEN**

```bash
python3 -m pytest tests/test_memecoin_x_notifications.py tests/test_memecoin_x_systemd_units.py -q
```

- [ ] **Step 7: Commit Telegram delivery**

```bash
git add sniper/services/memecoin_x_notifications.py \
  scripts/run_memecoin_x_notifications.py \
  systemd/sniper-x-intel-notifications.service \
  scripts/install_memecoin_x_services.sh \
  tests/test_memecoin_x_notifications.py tests/test_memecoin_x_systemd_units.py
git commit -m "feat(x-intel): alert curated cashtag activity"
```

---

### Task 6: Move X into Intelligence and Expose Canonical Intel Read Routes

**Files:**
- Modify: `sniper/routes/api_memecoin_x.py`
- Modify: `sniper/web/server.py`
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_api_memecoin_x.py`
- Modify: `tests/test_memecoin_x_ui_contract.py`
- Modify: `tests/test_finance_ui_contract.py`

**Interfaces:**
- Consumes: isolated X database/read models and ticker-native events.
- Produces:
  - canonical `/api/intel/x/status`, `/activity`, `/sources`, `/suggestions`, `/context`, and `/annotations` reads;
  - `intelligence.loadX()` and `intelligence.switchView('x')`;
  - a standalone `#intel-x-view`.

- [ ] **Step 1: Add RED route tests for canonical Intel endpoints**

Assert `/api/intel/x/status` and `/api/intel/x/activity` return display-only payloads, ticker-only
events serialize nullable identity honestly, filters reject invalid values, and capture-health state
does not masquerade as an empty healthy feed. Audit route consumers with `rg`; add old aliases only
if a real non-UI consumer exists.

- [ ] **Step 2: Run route tests RED**

Run `python3 -m pytest tests/test_api_memecoin_x.py -q`.

- [ ] **Step 3: Implement shared canonical Intel routes**

Keep all DB opens read-only except the existing explicit LLM queue POST. Return activity rows with
cashtags, identity state, relationship, source, excerpt, consensus count, Post URL, observed time,
and X-created time. Bound lists and escape nothing server-side beyond JSON serialization.

- [ ] **Step 4: Add RED UI contract tests before moving markup**

Tests must assert:

- an `X` button exists beside Geopolitics and Finance;
- `#intel-x-view` exists and loads only canonical Intel X endpoints;
- `data-subtab="x-sources"` and `data-section="x-sources"` are absent from Memecoin;
- the X renderer is owned by `intelligence`, not `memecoin`;
- all dynamic Post content uses `esc()` and external Post links use safe attributes;
- health, activity, account, relationship, cashtag, identity, and window controls exist;
- no buy/sell/follow/funded action is present; and
- mobile CSS collapses the activity grid to one column with visible keyboard focus.

- [ ] **Step 5: Run UI tests RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_ui_contract.py tests/test_finance_ui_contract.py -q
```

- [ ] **Step 6: Implement the Intel/X surface using the frontend-design direction**

Subject: a live source-tape for one operator. Single job: scan a cashtag event and open the exact X
Post quickly. Preserve the dashboard's existing dark palette and typography. Use one signature
element: a narrow left relationship rail whose color encodes direct/reply/quote/repost. Keep cards
flat and data-dense; do not add gradients, hero marketing, or decorative animation. Add visible
focus, reduced-motion-safe behavior, responsive one-column layout, explicit unavailable/empty
states, and a compact health strip.

Move the existing source-evidence tables below the new activity tape. Do not duplicate X styles or
renderer code under the Memecoin component.

- [ ] **Step 7: Run route/UI tests GREEN plus JavaScript syntax**

```bash
python3 -m pytest \
  tests/test_api_memecoin_x.py \
  tests/test_memecoin_x_ui_contract.py \
  tests/test_finance_ui_contract.py -q
node --check static/app.js
```

- [ ] **Step 8: Commit the Intel relocation**

```bash
git add sniper/routes/api_memecoin_x.py sniper/web/server.py \
  templates/index.html static/app.js static/styles.css \
  tests/test_api_memecoin_x.py tests/test_memecoin_x_ui_contract.py \
  tests/test_finance_ui_contract.py
git commit -m "feat(x-intel): move X source tape into Intelligence"
```

---

### Task 7: Integrate, Document Handoff, and Run the Full Knowledge/Verification Gates

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-x-intel-api-alerting-design.md` only if implementation changed an approved interface.
- Create: `docs/runbooks/x-intel-api-activation.md`
- Modify: `AGENTS.md` and `CLAUDE.md` only if implementation changes doctrine or project navigation materially beyond the approved spec.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: credential-ready default-OFF runtime and an exact operator activation/rollback runbook.

- [ ] **Step 1: Write the activation runbook with no credentials or live commands executed**

Document:

1. required environment keys and an owner-only dedicated environment-file example with placeholder
   values;
2. fixture smoke command;
3. bounded API dry-capture command;
4. SQL/read-only checks for stream state, resource count, frontier, ticker-only identity, and outbox;
5. Telegram preview and one explicit test-message command;
6. separate API and Telegram enable commands;
7. process/log/health verification;
8. browser receiver retirement only after API health; and
9. rollback that disables Telegram first, then API, without deleting the DB/outbox.

- [ ] **Step 2: Run the focused X/Intel suite**

```bash
python3 -m pytest $(rg --files tests | rg '/?test_memecoin_x_.*\.py$' | sort) \
  tests/test_api_memecoin_x.py tests/test_x_intel_settings.py \
  tests/test_finance_ui_contract.py -q
```

Expected: all pass, with no network access.

- [ ] **Step 3: Run static and schema verification**

```bash
python3 scripts/validate_schema_refs.py
python3 -m py_compile \
  sniper/persistence/db_memecoin_x.py \
  sniper/analysis/memecoin_x/api_rules.py \
  sniper/analysis/memecoin_x/api_normalize.py \
  sniper/services/memecoin_x_api_client.py \
  sniper/services/memecoin_x_api_capture.py \
  sniper/services/memecoin_x_notifications.py \
  sniper/settings/x_intel.py \
  scripts/run_memecoin_x_api_capture.py \
  scripts/run_memecoin_x_notifications.py
node --check static/app.js
bash -n scripts/install_memecoin_x_services.sh
git diff --check origin/main...HEAD
```

- [ ] **Step 4: Run fixture smoke twice and prove idempotency**

Use a temporary DB outside tracked paths:

```bash
tmp_dir=$(mktemp -d)
python3 scripts/run_memecoin_x_api_capture.py \
  --fixture tests/fixtures/memecoin_x/api/direct.json --db "$tmp_dir/x.db"
python3 scripts/run_memecoin_x_api_capture.py \
  --fixture tests/fixtures/memecoin_x/api/direct.json --db "$tmp_dir/x.db"
python3 scripts/run_memecoin_x_notifications.py --db "$tmp_dir/x.db" --dry-run
```

Assert the second capture reports zero new logical alerts and the preview contains one exact Post
link. Do not remove any user or repository data; the temporary directory may be left for OS cleanup
or removed only after validating its resolved path is the `mktemp` result.

- [ ] **Step 5: Review the entire branch diff against the spec**

Verify every goal and non-goal line-by-line. Confirm no token-shaped literal, live endpoint call in
tests, funded import, production DB path, hidden browser fallback, fake mint, or `promotion` label
entered the capture/alert path. Run:

```bash
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- \
  sniper/persistence/db_memecoin_x.py \
  sniper/analysis/memecoin_x \
  sniper/services/memecoin_x_api_client.py \
  sniper/services/memecoin_x_api_capture.py \
  sniper/services/memecoin_x_notifications.py \
  sniper/routes/api_memecoin_x.py \
  templates/index.html static/app.js static/styles.css \
  scripts systemd tests
```

- [ ] **Step 6: Apply the Knowledge Sync Gate honestly**

This branch adds a new spec, modules, runtime workflow, and UI location. Update tracked doctrine only
where the implementation makes current instructions wrong. Do not write Supabase memory without
explicit operator approval. If no PR is opened, record the required Knowledge Delta facts in the
handoff; if a PR is later opened, its body must include the complete 11-question Knowledge Sync
block.

- [ ] **Step 7: Commit the runbook and any verified contract sync**

```bash
git add docs/runbooks/x-intel-api-activation.md
git add AGENTS.md CLAUDE.md docs/superpowers/specs/2026-07-26-x-intel-api-alerting-design.md 2>/dev/null || true
git commit -m "docs(x-intel): add API activation runbook"
```

- [ ] **Step 8: Run the final verification commands fresh after the last commit**

Repeat Steps 2–4. Completion requires fresh exit-zero output after the final commit, a clean tracked
worktree, and explicit reporting of any untracked fixture/runtime artifacts.
