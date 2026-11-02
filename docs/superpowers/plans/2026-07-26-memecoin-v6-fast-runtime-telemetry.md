# Memecoin V6 Fast Runtime and Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver new Solana and chain-4663 activity to V6 in under one second when the provider delivers it in that window, without making network quotes block event ingestion or changing funded state.

**Architecture:** Normalize both chain feeds into one V6 market-event type, incrementally maintain shared per-pool facts, and use a 50-ms dirty-position coalescer to persist observations and call the pure engine. Run quotes in a bounded asynchronous scheduler, keep a one-second health heartbeat and five-second recovery supervisor, and expose transport/engine/quote latency separately in the existing V6 Paper surface.

**Tech Stack:** Python 3 `asyncio`, `websockets==12.0`, existing JSON-RPC helpers and pure chain decoders, SQLite WAL, Flask JSON API, existing vanilla JavaScript UI, pytest fake providers.

## Global Constraints

- Plans A and B must be complete and green before this plan starts.
- V6 event adapters are read-only against RPC/providers and write only the dedicated V6 database.
- No V6 module may import funded signing, approval, broadcast, reservation, runner, or funded DB mutation code.
- Solana uses subscription delivery; 4663 uses subscriptions when supported and a 500-ms `eth_getLogs` fallback otherwise.
- Burst coalescing is 50 ms. Heartbeat is one second. Whole-bag quote freshness/refresh is five seconds.
- A cursor gap is persisted and affected rule families abstain until catch-up succeeds; no cursor advances past an unresolved gap.
- Warm local targets: receive-to-observation p95 <100 ms, observation-to-decision p99 <25 ms, pure `step()` p99 <5 ms.
- Provider latency is reported separately and has no fabricated hard SLO.
- V6 remains default OFF. Do not start a worker, call live RPC, deploy, write production DB/Supabase, or alter funded services.

---

## File map

- Create `sniper/services/memecoin_v6_events.py`: normalized market event/cursor/health types.
- Create `sniper/services/memecoin_v6_evm_feed.py`: 4663 subscription and bounded poll fallback.
- Create `sniper/services/memecoin_v6_solana_feed.py`: V6 pool/vault subscriptions, transaction fetch,
  pure decode, cursor catch-up.
- Create `sniper/services/memecoin_v6_incremental_state.py`: shared per-pool causal accumulator.
- Create `sniper/services/memecoin_v6_fast_runtime.py`: dirty queue, quote scheduler, heartbeat,
  position orchestration.
- Modify `scripts/memecoin_v6_paper_worker.py`: default-OFF async runtime entry and compatibility
  `--once`/`--smoke-no-rpc` paths.
- Modify `sniper/settings/memecoin.py`: validated V6-only transport timing settings.
- Modify `sniper/persistence/db_memecoin_v6_paper.py`: feed cursor/health/latency storage.
- Modify `sniper/services/memecoin_v6_paper_report.py`, `sniper/routes/api_memecoin.py`, and
  `static/app.js`: read-only operational evidence in the merged Plan 3 surface.
- Add focused tests for each new module and update existing worker/report/API/UI tests.

### Task 1: Lock event, cursor, and timing contracts

**Files:**
- Create: `sniper/services/memecoin_v6_events.py`
- Modify: `sniper/settings/memecoin.py:90-141`
- Create: `tests/test_memecoin_v6_events.py`
- Modify: `tests/test_memecoin_v6_paper_settings.py`

**Interfaces:**
- Produces: `V6MarketEvent`, `V6FeedCursor`, `V6FeedHealth`, `event_identity(event) -> str`, and
  validated settings constants.
- Consumes: no later-task interface.

- [ ] **Step 1: Write event identity and setting RED tests**

```python
def test_evm_event_identity_includes_reorg_anchor():
    event = V6MarketEvent(network_id="4663", pool="0xpool", block_height=10,
                          block_hash="0xblock", tx_id="0xtx", event_index=7,
                          source_event_at=NOW, received_at=LATER, kind="v2_swap", payload={})
    changed = replace(event, block_hash="0xreorg")
    assert event_identity(event) != event_identity(changed)

def test_fast_runtime_defaults():
    cfg = _load_v6_paper_settings({})
    assert cfg["event_coalesce_ms"] == 50
    assert cfg["heartbeat_s"] == 1.0
    assert cfg["evm_fallback_poll_ms"] == 500
    assert cfg["quote_refresh_s"] == 5.0
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_events.py tests/test_memecoin_v6_paper_settings.py -q`

Expected: FAIL because the types/settings do not exist.

- [ ] **Step 3: Implement immutable event types**

```python
@dataclass(frozen=True)
class V6MarketEvent:
    network_id: str
    pool: str
    block_height: int
    block_hash: str | None
    tx_id: str
    event_index: int
    source_event_at: str | None
    received_at: str
    kind: str
    payload: Mapping[str, object]

@dataclass(frozen=True)
class V6FeedCursor:
    network_id: str
    pool: str
    height: int
    event_identity: str

@dataclass(frozen=True)
class V6FeedHealth:
    mode: str
    current: bool
    gap: bool
    last_received_at: str | None
    reconnect_count: int
```

Validate aware timestamps, supported networks, non-empty identities, non-negative heights/indexes,
and canonical payloads. EVM identity hashes block hash + tx + log index; Solana identity hashes slot
+ signature + instruction/log index.

Add settings `MEMECOIN_V6_EVENT_COALESCE_MS=50`, `MEMECOIN_V6_HEARTBEAT_S=1`,
`MEMECOIN_V6_EVM_FALLBACK_POLL_MS=500`, `MEMECOIN_V6_QUOTE_REFRESH_S=5`, and
`MEMECOIN_V6_EVENT_CATCHUP_MAX=400`. Validate all are finite/positive and catch-up is a positive
integer.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_memecoin_v6_events.py tests/test_memecoin_v6_paper_settings.py -q`

Run: `git diff --check`

```bash
git add sniper/services/memecoin_v6_events.py sniper/settings/memecoin.py tests/test_memecoin_v6_events.py tests/test_memecoin_v6_paper_settings.py
git commit -m "feat: define v6 fast-feed contracts"
```

### Task 2: Build the read-only 4663 event feed

**Files:**
- Create: `sniper/services/memecoin_v6_evm_feed.py`
- Create: `tests/test_memecoin_v6_evm_feed.py`

**Interfaces:**
- Consumes: `V6MarketEvent`, `V6FeedCursor`; pure
  `sniper.services.memecoin_tick_stream.decode_evm_log`.
- Produces: `decode_4663_log(log, received_at) -> V6MarketEvent | None`,
  `poll_4663_once(rpc_call, pools, cursor, now_iso, max_span=400) -> EVMFeedBatch`, and
  `async stream_4663(...)` yielding `EVMFeedBatch`.

- [ ] **Step 1: Write decode, dedup, fallback, and gap RED tests**

```python
def test_poll_fallback_reads_from_cursor_and_dedups():
    batch = poll_4663_once(fake_rpc, {POOL}, V6FeedCursor("4663", POOL, 9, "old"), NOW)
    assert [e.event_index for e in batch.events] == [1, 2]
    assert batch.cursor.height == 10
    assert len({event_identity(e) for e in batch.events}) == 2

def test_failed_getlogs_does_not_advance_cursor():
    batch = poll_4663_once(failing_rpc, {POOL}, CURSOR, NOW)
    assert batch.cursor == CURSOR
    assert batch.health.gap is True
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_evm_feed.py -q`

Expected: FAIL because the feed module does not exist.

- [ ] **Step 3: Implement normalized polling and subscriptions**

Use only read JSON-RPC methods: `eth_blockNumber`, `eth_getLogs`, and subscription equivalents
`eth_subscribe logs`/`newHeads`. Filter to open V6 pool addresses. Decode with the existing pure
decoder, then wrap with true provider timestamps when available. Sort by block height, tx id, and log
index. Deduplicate by `event_identity`.

When subscriptions fail or are unsupported, enter `poll_500ms` mode. Scan at most 400 blocks per
request. If the cursor is deeper than the configured catch-up bound, persist `gap=True` and keep the
old cursor; do not skip to tip or stamp historical rows as current.

- [ ] **Step 4: Run feed tests twice and commit**

Run: `python3 -m pytest tests/test_memecoin_v6_evm_feed.py -q`

Run the same command again to catch cursor/idempotency drift.

```bash
git add sniper/services/memecoin_v6_evm_feed.py tests/test_memecoin_v6_evm_feed.py
git commit -m "feat: add read-only v6 4663 event feed"
```

### Task 3: Build the read-only Solana transaction-grounded feed

**Files:**
- Create: `sniper/services/memecoin_v6_solana_feed.py`
- Create: `tests/test_memecoin_v6_solana_feed.py`

**Interfaces:**
- Consumes: `V6MarketEvent`, `V6FeedCursor`; pure Solana transaction decoders from
  `sniper.services.memecoin_solana_amounts`; pool/vault binding from V6 entry records.
- Produces: `parse_solana_notification(frame, subscriptions, received_at) -> SolanaArrival | None`,
  `decode_solana_arrival(arrival, get_transaction) -> tuple[V6MarketEvent, ...]`, and
  `async stream_solana(...)` yielding `SolanaFeedBatch`.

- [ ] **Step 1: Write notification, transaction, and gap RED tests**

```python
def test_notification_is_not_transaction_grounded_until_decode():
    arrival = parse_solana_notification(FRAME, {7: POOL_BINDING}, NOW)
    assert arrival.signature == "sig-1"
    assert not hasattr(arrival, "depth_pull_flag")
    events = decode_solana_arrival(arrival, get_transaction=fake_transaction)
    assert events[0].tx_id == "sig-1"
    assert events[0].payload["transaction_decoded"] is True

def test_missing_transaction_persists_gap_and_cursor_stays_put():
    batch = decode_batch([ARRIVAL], get_transaction=lambda _sig: None, cursor=CURSOR)
    assert batch.cursor == CURSOR
    assert batch.health.gap is True
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_solana_feed.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement V6 pool/vault subscriptions and decode**

Use `websockets` `logsSubscribe` against resolved open V6 pool/vault mentions. A notification is an
arrival only. Fetch its transaction through the configured read RPC, reuse pure amount/LP decoders,
and emit one or more transaction-grounded events with signature plus instruction/log index. Native
SOL Pump events require the existing Pump program provenance.

Refresh subscriptions when V6 positions open/close. On reconnect, use the persisted signature/slot
cursor for bounded catch-up. If pagination bounds or transaction fetch gaps are hit, keep the prior
cursor, set `gap=True`, and let depth/flow/volatility rules abstain.

- [ ] **Step 4: Run Solana feed and adjacent decoder tests**

Run: `python3 -m pytest tests/test_memecoin_v6_solana_feed.py tests/test_memecoin_solana_tape.py tests/test_memecoin_solana_amounts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_solana_feed.py tests/test_memecoin_v6_solana_feed.py
git commit -m "feat: add transaction-grounded v6 solana feed"
```

### Task 4: Add shared incremental pool aggregation

**Files:**
- Create: `sniper/services/memecoin_v6_incremental_state.py`
- Create: `tests/test_memecoin_v6_incremental_state.py`
- Modify: `sniper/services/memecoin_v6_observations.py`

**Interfaces:**
- Consumes: ordered `V6MarketEvent` objects.
- Produces: `PoolAccumulator.apply(event) -> bool`, `PoolAccumulator.snapshot(now_iso) -> PoolFacts`,
  and `rebuild_accumulator(events, cursor) -> PoolAccumulator`.

- [ ] **Step 1: Write parity, dedup, and reorg RED tests**

```python
def test_incremental_snapshot_matches_bounded_compute_state_fixture():
    acc = PoolAccumulator(POOL_META)
    for event in FIXTURE_EVENTS:
        assert acc.apply(event)
    assert acc.snapshot(NOW).scored_fields() == compute_expected_fixture(FIXTURE_EVENTS)

def test_duplicate_event_is_a_noop():
    acc = PoolAccumulator(POOL_META)
    assert acc.apply(EVENT) is True
    assert acc.apply(EVENT) is False
    assert acc.snapshot(NOW).market_evidence_id == event_identity(EVENT)

def test_conflicting_block_hash_marks_gap():
    acc = PoolAccumulator(POOL_META)
    acc.apply(EVENT)
    assert acc.apply(replace(EVENT, block_hash="0xreorg")) is False
    assert acc.health.gap is True
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_incremental_state.py -q`

Expected: FAIL because each observation currently rescans up to 5,000 rows.

- [ ] **Step 3: Implement the bounded accumulator**

Maintain the minimum causal state required for existing market features: ordered event cursor,
bounded event-time windows for buy/sell counts and volume, prior confirmed low, realized volatility,
arrival rate, reserve/depth state, LP-pull provenance, and tape completeness. Share one accumulator
per `(network_id, pool)` and expose immutable `PoolFacts` snapshots.

Reuse existing pure math/decoders; do not copy funded policy. The accumulator owns no position
quantity, entry economics, action, or quote. `build_observation` accepts `PoolFacts` and stops reading
5,000 tape rows on the current path. Keep bounded tape rebuild only for startup/reconnect.

- [ ] **Step 4: Run incremental and observation tests**

Run: `python3 -m pytest tests/test_memecoin_v6_incremental_state.py tests/test_memecoin_v6_observations.py tests/test_exit_compute_state.py tests/test_exit_compute_state_hardening.py -q`

Expected: PASS with fixture parity and duplicate no-op.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_incremental_state.py sniper/services/memecoin_v6_observations.py tests/test_memecoin_v6_incremental_state.py tests/test_memecoin_v6_observations.py
git commit -m "perf: aggregate v6 market facts incrementally"
```

### Task 5: Build dirty queue, quote scheduler, and fast runtime

**Files:**
- Create: `sniper/services/memecoin_v6_fast_runtime.py`
- Modify: `scripts/memecoin_v6_paper_worker.py:230-end`
- Create: `tests/test_memecoin_v6_fast_runtime.py`
- Modify: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: chain feed batches, `PoolAccumulator`, Plan B `advance_position`, and action retry claims.
- Produces: `DirtyPositionQueue`, `V6QuoteScheduler`,
  `async run_fast_runtime(context, stop_event) -> None`, and compatibility recovery supervisor.

- [ ] **Step 1: Write concurrency and coalescing RED tests**

```python
async def test_burst_coalesces_for_50ms_without_losing_latest_evidence():
    queue = DirtyPositionQueue(coalesce_ms=50, clock=fake_clock)
    for event in BURST:
        queue.mark("p1", event_identity(event))
    work = await queue.next_ready()
    assert work.position_id == "p1"
    assert work.latest_evidence_id == event_identity(BURST[-1])

async def test_slow_quote_does_not_block_other_position_decision():
    await runtime.inject(EVENT_P1)
    await runtime.inject(EVENT_P2)
    slow_quotes.release("p2")
    await eventually(lambda: decisions_for("p2") == 1, within_ms=100)
    assert decisions_for("p1") == 1
    assert fills_for("p1") == 0
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_fast_runtime.py -q`

Expected: FAIL because the runtime is a serial five-second loop.

- [ ] **Step 3: Implement the 50-ms dirty queue**

Track newest evidence id per position, one ready deadline, and no unbounded per-event queue. Pool
events mark every open V6 position bound to that pool. At deadline, freeze one observation from the
latest accumulator/quote/source snapshots and call `advance_position` on the single SQLite owner
task.

- [ ] **Step 4: Implement bounded asynchronous quotes**

Use a process-lifetime executor with maximum `min(4, max_open)` workers, one in-flight quote per
position, and newest-request coalescing. Schedule on open, new market evidence, quote age five
seconds, and retry eligibility. Never execute SQLite writes in quote threads. Before applying a quote
result on the owner loop, re-read the active intent token.

- [ ] **Step 5: Implement heartbeat and supervisor**

The one-second heartbeat updates feed health, expires quotes, makes retry deadlines ready, and checks
time-rule boundaries. Unchanged heartbeats write only the health row. Run existing `run_cycle` every
five seconds for cursor/entry/restart recovery; prevent it from re-observing a position already
current through the fast lane.

- [ ] **Step 6: Run runtime/worker tests**

Run: `python3 -m pytest tests/test_memecoin_v6_fast_runtime.py tests/test_memecoin_v6_paper_worker.py tests/test_memecoin_v6_paper_runner.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sniper/services/memecoin_v6_fast_runtime.py scripts/memecoin_v6_paper_worker.py tests/test_memecoin_v6_fast_runtime.py tests/test_memecoin_v6_paper_worker.py
git commit -m "feat: run v6 from subsecond dirty events"
```

### Task 6: Persist feed health and latency without per-heartbeat growth

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Create: `tests/test_memecoin_v6_runtime_telemetry.py`
- Modify: `tests/test_memecoin_v6_paper_db.py`
- Modify: `tests/test_memecoin_v6_paper_report.py`

**Interfaces:**
- Consumes: feed health and five latency timestamps from fast runtime.
- Produces: `upsert_feed_health`, `append_latency_sample`, `runtime_health`, and report fields
  `feed_health`/`latency`.

- [ ] **Step 1: Write storage-growth and percentile RED tests**

```python
def test_unchanged_heartbeats_update_one_health_row(tmp_path):
    conn = _conn(tmp_path)
    for i in range(86_400):
        db.upsert_feed_health(conn, "4663", "0xpool", healthy(now=i))
    assert _count(conn, "v6_paper_feed_health") == 1
    assert _count(conn, "v6_paper_observations") == 0

def test_latency_report_keeps_provider_and_local_stages_separate(v6_conn):
    data = report.runtime_health(v6_conn)
    assert data["provider_to_receive_ms"]["p95"] == 240
    assert data["receive_to_observation_ms"]["p95"] == 80
    assert data["observation_to_decision_ms"]["p99"] == 20
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_runtime_telemetry.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_report.py -q`

Expected: FAIL because health/latency tables do not exist.

- [ ] **Step 3: Add compact health and latency schema**

Add one upserted health row per network/pool and a bounded latency sample table with a 24-hour
retention/delete inside the same V6-only transaction. Store stage durations as integers in
microseconds and tags `network_id`, `provider`, and `feed_mode`; never store credentials/RPC URLs.

Report p50/p95/p99 by stage/network/provider plus gaps, reconnects, cursor, current mode, last event,
and accumulator freshness. A missing sample reports `None`, not zero.

- [ ] **Step 4: Run telemetry tests and commit**

Run: `python3 -m pytest tests/test_memecoin_v6_runtime_telemetry.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_report.py -q`

```bash
git add sniper/persistence/db_memecoin_v6_paper.py sniper/services/memecoin_v6_paper_report.py tests/test_memecoin_v6_runtime_telemetry.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_report.py
git commit -m "feat: persist v6 feed and latency health"
```

### Task 7: Extend the Plan 3 API/UI and harden censoring

**Files:**
- Modify: `sniper/routes/api_memecoin.py:1830-1910`
- Modify: `static/app.js` inside `V6_PAPER_SECTION_START`/`V6_PAPER_SECTION_END`
- Modify: `tests/test_memecoin_v6_paper_api.py`
- Modify: `tests/test_memecoin_v6_paper_ui.py`

**Interfaces:**
- Consumes: report `feed_health`, `latency`, rule results, evidence identities, and active intent.
- Produces: read-only operator cards/timeline and CSRF-protected censor mutation.

- [ ] **Step 1: Write API/UI RED tests**

```python
def test_positions_api_exposes_fast_runtime_health(client):
    body = client.get("/api/memecoin/v6-paper/positions").get_json()
    assert body["runtime"]["feed_health"]["4663"]["mode"] in {"subscription", "poll_500ms"}
    assert body["runtime"]["latency"]["observation_to_decision_ms"]["p99"] is not None

def test_censor_rejects_missing_csrf(client):
    response = client.post("/api/memecoin/v6-paper/positions/p1/censor", json={"reason": "operator test"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run API/UI tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py -q`

Expected: FAIL because runtime telemetry and CSRF enforcement are absent.

- [ ] **Step 3: Add read-only operational fields**

Show market/quote/source evidence ids, semantic transition flag, rule statuses, current break episode,
active/superseded intent, retry count/next retry, quote status, feed mode/cursor/gap/freshness, and
latency stages. Keep the `V6 PAPER — NO FUNDS` badge and omit every funded control.

- [ ] **Step 4: Harden censor mutation**

Require authenticated same-origin POST, the project CSRF token/header, a non-empty reason of at least
eight characters, and V6-only DB mutation. Return 403 before opening the writable V6 database when
CSRF fails. Preserve idempotent same-reason censor behavior.

- [ ] **Step 5: Run API/UI tests and commit**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py -q`

```bash
git add sniper/routes/api_memecoin.py static/app.js tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py
git commit -m "feat: show v6 fast-runtime evidence"
```

### Task 8: Run cross-chain latency and isolation gates

**Files:**
- Create: `tests/test_memecoin_v6_fast_runtime_integration.py`
- Modify: `scripts/memecoin_v6_paper_worker.py`
- Modify: `docs/superpowers/specs/2026-07-26-memecoin-v6-correctness-and-fast-runtime-design.md`

**Interfaces:**
- Consumes: complete fast runtime.
- Produces: fake-provider acceptance evidence and final default-OFF smoke.

- [ ] **Step 1: Add four-position fake-provider acceptance test**

Feed interleaved Solana and 4663 events into four open positions, hold one quote response, and
measure stage timestamps with `perf_counter_ns`. Warm the engine before measuring. Assert:

```python
assert percentile(receive_to_observation_ms, 95) < 100
assert percentile(observation_to_decision_ms, 99) < 25
assert percentile(step_ms, 99) < 5
assert decisions_for_slow_quote_position >= 1
assert decisions_for_other_positions == expected_decisions
assert duplicate_action_fills == 0
```

- [ ] **Step 2: Add import and write-boundary audit**

Parse the transitive V6 runtime imports and assert no funded signing, approval, broadcast,
reservation, `memecoin_real_runner`, or production DB writer is reachable. Open source/tape fixtures
with `PRAGMA query_only=1`; only the V6 database may change.

- [ ] **Step 3: Run full V6 and adjacent gate**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_engine_performance.py tests/test_memecoin_v6_events.py tests/test_memecoin_v6_evm_feed.py tests/test_memecoin_v6_solana_feed.py tests/test_memecoin_v6_incremental_state.py tests/test_memecoin_v6_fast_runtime.py tests/test_memecoin_v6_fast_runtime_integration.py tests/test_memecoin_v6_paper_settings.py tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_observations.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_signals.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_evm_dex_quote.py tests/test_memecoin_evm_v2_exit_quote.py tests/test_memecoin_solana_tape.py tests/test_memecoin_tick_stream.py -q`

Run: `python3 scripts/memecoin_v6_paper_worker.py --smoke-no-rpc --once`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Run: `git diff --check`

Expected: all pass, smoke exits 0, and no live provider is contacted.

- [ ] **Step 4: Update measured values in the design evidence section**

Append the exact fake-provider p50/p95/p99 results and test command under a dated implementation
evidence subsection. Do not claim live provider latency or activation readiness.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memecoin_v6_fast_runtime_integration.py scripts/memecoin_v6_paper_worker.py docs/superpowers/specs/2026-07-26-memecoin-v6-correctness-and-fast-runtime-design.md
git commit -m "test: gate v6 subsecond fast runtime"
```
