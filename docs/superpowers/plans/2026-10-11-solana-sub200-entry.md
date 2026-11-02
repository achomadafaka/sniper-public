# Solana Sub-200 ms Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** remove the known cold preparation waits and TPU source mismatch, then measure successful processed execution within 200 ms of the original received signal, with exact confirmed evidence and an honest received-event denominator.

**Architecture:** extend existing pool and exotic-leg preparation off the entry path; preserve the cold fallback, both live workers, exact clips and all money gates. Capture TPU guard inputs once and select the approved isolated read endpoint through its existing setting. Reuse the separate timing DB and its existing drain for passive source/processed receipts; measurement never admits, signs or books a trade.

**Tech Stack:** existing Python threads/asyncio, SQLite, pytest/xdist, httpx, Rust/Tokio/serde and pinned Agave 4.1.2. No new dependency.

**Spec:** [Approved design](../specs/2026-10-11-solana-sub200-entry-design.md). Read it before this plan. Approval includes the isolated TPU read-provider exception; it does not prove speed or provider capacity.

## Global Constraints

- Base checkout: `b333a1616e8e34f4e20546ffd0e400b8aa23657c`. Create an isolated `codex/` worktree at execution time. Preserve unrelated main-checkout changes in `bot/brain_graph/nightly.py`, `tests/test_brain_graph_nightly.py` and untracked operational artifacts.
- No entry sizing, arming, source-proof, admission, safety, slippage/copy-price, fee, exit, custody-inventory or J7 authority change. Original proof deadline stays **500 ms**; never reset original receipt time.
- No added synchronous provider request, lock, retry, DNS lookup, measurement DB write or diagnostic log on receipt, entry building, signing or submission. Reuse existing locks only where already taken. Report added publisher cost as measured p50/p90, not an assumed zero.
- Preserve write-ahead signed identity, identical-byte sends, signed/unknown reservations and confirmed exact payment/acquisition accounting. Processed observations are measurement only.
- Exotic warmer: **two cache entries, two coalesced pending keys, one independent worker/client, 60 s retention, 1.5 s cadence, four actual Jupiter HTTP requests per rolling 1.5 s**, including initial fetch and transport failover. Do not consume either of the two live `OnDemandLegWorker` slots or run network work on the essential balance/blockhash refresher.
- Exact warm key: `(venue, quote_mint, usdc_atomic)`, plus payer/program/route-policy identity checks. No size snapping or larger-clip substitution. Keep **3 s** builder leg age, **1 s** warmer service margin and **300 s** quote-safety recheck. Failed reads never renew timestamps.
- TPU: keep **1000 ms** frozen-clock and **two-slot** guard, SDK outlier/schedule/cluster checks and native `HEAD` as a reject-only hint. Use `MEMECOIN_TPU_RPC_URL`, a separate same-provider/region read credential and verified isolated capacity. Do not add a WS setting unless this selected seam demonstrably cannot work.
- Observer: one pre-established custody-public-key processed subscription on `MEMECOIN_RESEARCH_HELIUS_RPC_URL`, default OFF behind `MEMECOIN_EXECUTION_OBSERVER_ENABLED`. No custody-RPC fallback, signer loading, source-roster insertion or per-order subscription. Missing observer cannot refuse an entry.
- Tests: `python3 -m pytest -n 2 ...` overrides repository worker default while retaining marker/timeout defaults. One full suite across chats, under `flock /home/bigpapi/.cache/codex-pytest-full.lock`. Rust build jobs and test threads: two. Never stop/cap funded services to make room.
- Runtime starts only from the immutable pinned release. Deployment uses `--defer-backup`; no backup, funded probe, historical retry or fabricated organic baseline.

## Review Focus

1. A replaced pool/config/clip while warming is in flight cannot publish stale facts or increase the signed stake; Tasks 5–6 test generation and exact-key mismatch.
2. Two Jupiter hosts after 429 can exceed a logical-call budget; Task 6 counts before each actual HTTP request, shares initial/refresh budget and preserves both cold workers.
3. Duplicate/malformed/routed source frames and a writer outage cannot shrink the universe to fills; Task 3 tests original clocks, unresolved identity, duplicate receipts, zero-skip drains and gap persistence.
4. Missing `err`, wrong subscription, processed forks or a delayed DB join cannot fabricate execution success; Task 4 tests strict parsing, first observation, exact confirmed joins and clock mismatch.
5. Frozen versus advancing-but-behind TPU clocks, mixed protocol versions and a lost response cannot authorize another signature; Tasks 2 and 7 test one captured guard, unchanged classification, isolated control and rollback.

---

## Execution order and ownership

Tasks 1–4 are passive diagnostics/measurement and can ship as a baseline release. Tasks 5–6 remove preparation waits. Task 7 selects the isolated TPU source only after capacity and no-send checks. Task 8 validates the pinned release and organic cohort. Keep one logical commit per task; changes sharing `memecoin_slot_lane.py`, `pump_curve_buy.py` or runner wiring execute sequentially.

| Task | Product files and callers | Tests / fixtures / configuration |
| --- | --- | --- |
| 1 | `sniper/execution/pump_curve_buy.py`, `raydium_launchlab_buy.py`; `sniper/services/memecoin_slot_lane.py::_traced` | `tests/test_slot_lane_leg_call_slow.py`, `test_slot_lane_exotic_warm_leg.py`, `test_memecoin_slot_lane.py`; existing timing sink and requeue callbacks |
| 2 | `rust/solana-tpu-sidecar/src/{delivery,main}.rs`; `sniper/execution/{solana_tpu_sidecar,submission_wiring}.py`; sidecar build script/Dockerfile | Existing Rust unit fixtures; `test_memecoin_solana_tpu_sidecar.py`, `test_memecoin_submission_transport.py`, `test_memecoin_tpu_sidecar_build.py` |
| 3 | `sniper/services/{memecoin_feed_arrivals,memecoin_slot_signal,memecoin_shyft_signal,memecoin_slot_lane,memecoin_slot_skips,memecoin_fill_timing}.py`; `scripts/memecoin_runner_loop.py` | Feed/signal/Shyft/skip/timing tests; `slot_signal_frames.json`; optional starter injection must stay compatible |
| 4 | Create `sniper/services/memecoin_execution_observer.py`; modify timing service, `solana_real_seams.py::after_settlement`, runner, settings and resolver | Observer/receipt/slot-executor/resolver/timing/runner tests; one default-OFF observer flag |
| 5 | `sniper/execution/pumpswap_buy.py`; background pool learner/slot-lane preparation callback | `test_slot_lane_pool_prewarm.py`, `test_pumpswap_buy.py`; `pump_migrate_frames.json`, `slot_handoff_54bzG3hj.json`, `pumpswap_buy_frames.json` |
| 6 | `raydium_launchlab_buy.py::WarmExoticLegs`, `pump_curve_buy.py::_funded_jup_call`, `memecoin_slot_lane.py::refresh_builder_legs` / roster owner / pool callback | Exotic warm/live/clip/refresh tests, role/client/HTTP transport tests; existing warm flag, no new clip knob |
| 7 | Create `scripts/research/memecoin_tpu_read_provider_control.py`; existing TPU setting/comment and exact read-policy docs | Create control tests; existing runtime verifier tests; approved endpoint configuration only at activation |
| 8 | Existing runtime builder/verifier/deployer; add final audit evidence in `docs/audits/2026-10-11-frank-entry-speed/` | Focused manifest + broader funded tests, locked full suite when needed, organic measurement |

No exports or dependencies need adding. New observer module is internal runner wiring. Preserve existing injected clients, clocks, fake starters, old trace fields, skip table and resolver state taxonomy.

## Task 1: Retain cold-route subcalls without activating slow-call logging

**Files:** modify `sniper/execution/pump_curve_buy.py:681–855`, `sniper/execution/raydium_launchlab_buy.py:977–1140`, `sniper/services/memecoin_slot_lane.py:1312–1370`; extend the Task 1 test files above.

**Interfaces:**

- Consumes existing `prefetch_timed(name)`, `prefetch_usdc_sol_leg(...)`, `OnDemandLegWorker.process(...)` and `EntryAttemptTrace.mark_substage(name, *, duration_ms=None, **metadata)`.
- Produces `leg_metrics(*, clock=time.monotonic)` context manager yielding `dict[str, float]` of milliseconds; `OnDemandLegWorker.process` signature stays unchanged. Requeue/result metadata adds optional `leg_timings_ms`, retaining original `recv_monotonic`.

- [ ] Add this failing test beside existing `Clock`, `_jup`, `_lines` helpers. Call `prefetch_usdc_sol_leg` directly; `_leg` clears TLS and must not wrap this collector.

```python
def test_worker_metrics_name_subcalls_without_slow_logging(monkeypatch, caplog):
    clock = Clock()
    def forbidden(*args, **kwargs):
        pytest.fail('worker telemetry performed an egress read')
    monkeypatch.setattr(pcb, '_leg_call_egress', forbidden)
    with pcb.leg_metrics(clock=clock) as timings:
        leg = pcb.prefetch_usdc_sol_leg(
            1_000_000, 50, user=USER,
            jup_call=_jup(clock, quote_s=1.2, swap_s=.1), rpc=None)
    assert leg is not None
    assert timings['quote'] == pytest.approx(1200)
    assert timings['swap_instructions'] == pytest.approx(100)
    assert _lines(caplog) == [] and pcb._SLOW_LAST == {}
```

- [ ] Add deterministic tests for exception timing and propagation, nested collector restoration, two simultaneous thread-local collectors, warm hit with absent network timings, and uncached/cached ALT parsing. Existing prefetch-only slow logs must still pass. Monkeypatch DNS, warning logger and globals to fail if worker collection reaches them.
- [ ] Run red: `python3 -m pytest -n 2 tests/test_slot_lane_leg_call_slow.py`. Expected new failure: missing `leg_metrics`; existing behavior stays green.
- [ ] Add the collector using only saved/restored **thread-local attributes**. Immutable sentinel is allowed; no capture/restore of module globals.

```python
@contextlib.contextmanager
def leg_metrics(*, clock=time.monotonic):
    missing = object()
    names = ('sink', 'clock', 'emit_slow')
    saved = {name: getattr(PREFETCH_TIMINGS, name, missing) for name in names}
    sink = {}
    PREFETCH_TIMINGS.sink, PREFETCH_TIMINGS.clock = sink, clock
    PREFETCH_TIMINGS.emit_slow = False
    try:
        yield sink
    finally:
        for name, value in saved.items():
            if value is missing:
                delattr(PREFETCH_TIMINGS, name)
            else:
                setattr(PREFETCH_TIMINGS, name, value)
```

- [ ] In `_leg_call`, retain timing but condition slow-line/global-egress work on `getattr(PREFETCH_TIMINGS, 'emit_slow', True)`. Apply the same condition to `_payload_bytes` serialization. Add `prefetch_timed('instruction_parse')` and `prefetch_timed('alt_parse')` around existing parsers; missing/unperformed work remains absent, not a fabricated zero.
- [ ] Keep public `process` as a narrow wrapper around its current body renamed `_process(self, buy, *, clip_usd=None, trial=None, quote_params=None, timings)`. Enter `leg_metrics(clock=self.clock.monotonic)`, time existing `_live_quote` with `prefetch_timed('quote_safety')`, and copy timings into the result and **before the existing requeue call**. Do not delay handback for telemetry. On refusal preserve completed timings without changing refusal reason.

```python
# At the existing handback; all current ondemand fields remain.
ondemand['leg_timings_ms'] = dict(timings)
self.requeue_fn(dataclasses.replace(buy, ondemand=ondemand))

# At _traced, after its existing slot_receipt mark; no new commit.
timings = (getattr(buy, 'ondemand', None) or {}).get('leg_timings_ms')
if isinstance(timings, dict):
    trace.mark_substage('slot_route_preparation', duration_ms=0.0,
                        leg_timings_ms=dict(timings), clock='monotonic')
```

- [ ] Add a worker integration test asserting the requeued buy keeps the original receipt time and the trace receives quote/read/GET/POST/ALT/parse durations. An early failure preserves completed stage timings; an absent field from an older producer is accepted. No synchronous audit call is added.
- [ ] Run green: `python3 -m pytest -n 2 tests/test_slot_lane_leg_call_slow.py tests/test_slot_lane_exotic_warm_leg.py tests/test_memecoin_slot_lane.py`.
- [ ] Commit only the Task 1 paths: `fix: retain off-thread route preparation timings`.

## Task 2: Capture the exact TPU guard and persist it asynchronously

**Files:** modify Rust `delivery.rs`, `main.rs`, `scripts/build_memecoin_tpu_sidecar.sh`, Rust Dockerfile, Python `solana_tpu_sidecar.py`, `submission_wiring.py`; extend their existing tests. Preserve `submission_transport.py::SubmissionAttempt.provider_timing` and `db_memecoin.record_submission_attempts` unchanged.

**Interfaces:** keep `SendOutcome` variants and response state/reason protocol. Change internal `WireDelivery::send_once(Vec<u8>, &str) -> SendDecision`; `SlotClock::fresh_with_age(slot, now, stale_after) -> (bool, Duration)` evaluates under its existing mutex once. Python `_attempt(..., provider_timing=None)` carries optional metadata; audit writer validates it off-path.

- [ ] Write Rust tests for first observation, exactly 1000 ms, +1 ns, regression without clock reset, advancement, two versus three slots behind, frozen+behind, `u64::MAX`, monotonic minimum, absent versus zero request minimum. Existing `FakeDelivery::new` and `SlowDelivery` fixtures stay usable.

```rust
#[test]
fn guard_age_uses_the_same_original_advance() {
    let clock = SlotClock::default();
    let start = Instant::now();
    let limit = Duration::from_millis(1000);
    assert_eq!(clock.fresh_with_age(100, start, limit), (true, Duration::ZERO));
    assert_eq!(clock.fresh_with_age(100, start + limit, limit), (true, limit));
    let later = limit + Duration::from_nanos(1);
    assert_eq!(clock.fresh_with_age(99, start + later, limit), (false, later));
    assert_eq!(clock.fresh_with_age(101, start + later, limit), (true, Duration::ZERO));
}
```
- [ ] Add a response fixture carrying this guard; assert the refusal remains `not_sent/stale_slot`, sends remain zero, and diagnostic socket count is independent from response `leader_count=0`.

```rust
#[derive(Debug, Clone, serde::Serialize)]
pub struct GuardSnapshot {
    pub version: u8,
    pub estimated_slot: u64,
    pub minimum_slot: u64,
    pub age_since_advance_ms: f64,
    pub clock_frozen: bool,
    pub behind_minimum: bool,
    pub available_leader_count: u64,
}

#[derive(Debug)]
pub struct SendDecision {
    pub outcome: SendOutcome,
    pub guard: Option<GuardSnapshot>,
}

impl From<SendOutcome> for SendDecision {
    fn from(outcome: SendOutcome) -> Self {
        Self { outcome, guard: None }
    }
}
```

- [ ] Add Python red coverage using existing `_req`, `FakeConn`, `_resp_frame`, `_conn_factory`:

```python
def test_stale_slot_keeps_state_and_carries_guard():
    guard = dict(version=1, estimated_slot=455637874, minimum_slot=455637877,
                 age_since_advance_ms=10.0, clock_frozen=False,
                 behind_minimum=True, available_leader_count=2)
    conn = FakeConn(_resp_frame(
        request_id='r', state='not_sent', tx_identity='SIG_TPU',
        reason_code='stale_slot', leader_count=0, guard=guard,
        request_min_context_slot=455637877))
    provider = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn))
    result = provider.submit(_req())
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == 'stale_slot'
    assert result.provider_timing['tpu_guard'] == guard
```

- [ ] Extend build `fake_docker` to capture argv; new test requires `CARGO_BUILD_JOBS=2`. Current script hardcodes four; exporting two cannot override it. Change script and Dockerfile defaults to two and Docker test-stage command to `--test-threads=2`.
- [ ] Run new Python tests red. Run Rust red with `CARGO_BUILD_JOBS=2 cargo test --locked -- --test-threads=2` if host toolchain is available; otherwise the Docker test stage below. Never run two compiles concurrently.
- [ ] Extract the current clock critical section without changing its predicate. A non-advancing/regressing slot uses original elapsed age; advancement sets `Duration::ZERO`. At `AgaveDelivery::send_once`, capture current client/list as today, read estimator and minimum once, call `fresh_with_age` once, and compute both predicates independently. Return the same captured guard after `pre_send` refusal or the existing same-bytes send. Do not recompute it from health.
- [ ] In `handle_request`, build the existing base response from `decision.outcome`, then append optional `guard` and nullable `request_min_context_slot`. Invalid signature/hash does not call delivery or produce a guard. Keep health protocol, deadline, reconnect, leader selection and Agave dependency unchanged.
- [ ] In Python `_classify`, attach dict guard and nullable request minimum only after existing request/transaction identity validation. Metadata cannot choose transport state. Missing/old/malformed metadata is unknown. `_AuditWriter._run` gets an independent TPU branch outside its HTTP-only timing condition, using the existing queue and DB-row write.
- [ ] Async writer allowlist: version 1; exact unsigned integers for slots/counts (reject bool), finite nonnegative age, exact bool predicates, nullable unsigned request minimum. Drop extra keys and invalid metadata. Persist one sanitized diagnostic process-log record joined by existing owner/signature/payload hash; no endpoint, key, address or payload. The money audit DB schema is unchanged; do not claim guard fields are stored there.
- [ ] Tests must cover old/new peers, unsupported versions, missing fields, NaN/inf/bool inputs, extra secret-valued fields, null/zero, identity mismatch, full/partial write and lost reply. Extend the existing late-provider audit test: caller connection is closed, both initial and late rows remain, and TPU metadata logs without HTTP timing fields. A crash/drop gives unknown evidence, never permission to release or re-sign.
- [ ] Run green Python tests and bounded Rust validation:

```bash
python3 -m pytest -n 2 tests/test_memecoin_solana_tpu_sidecar.py tests/test_memecoin_submission_transport.py tests/test_memecoin_tpu_sidecar_build.py
docker build --build-arg CARGO_BUILD_JOBS=2 --target test -t memecoin-tpu-test:entry-speed rust/solana-tpu-sidecar
docker run --rm --network none --cpus 2 memecoin-tpu-test:entry-speed cargo test --locked --offline -- --test-threads=2
```

- [ ] Commit only Task 2 paths: `fix: preserve TPU guard decision evidence`.

## Task 3: Persist original source receipts before admission can hide them

**Files:** modify `memecoin_feed_arrivals.py`, `memecoin_slot_signal.py`, `memecoin_shyft_signal.py`, `memecoin_slot_lane.py`, `memecoin_slot_skips.py`, `memecoin_fill_timing.py`, `scripts/memecoin_runner_loop.py`; extend corresponding feed/signal/skip/timing tests.

**Interfaces:** add `ReceiptJournal(*, producer_run_id, boot_id, clock_domain, max_samples=4096)` using the existing single-writer immutable-record/`dict.copy` pattern. Methods: `record_source(**fields) -> None`, `record_processed(**fields) -> None`, `record_gap(**fields) -> None`, `snapshot() -> dict` with immutable rows, sequence bounds and eviction counters. One instance per writer; never share a mutable journal between source loop and custody daemon. Extend `run_drain(stop, *, interval_s=1.0, connect=None, journals=())`; source starters receive optional `measurement=None`. Existing callers without these arguments work unchanged.

**Record contract:** `producer_run_id`, sequence, `network_id`, nullable `source_sig/source_wallet/mint/pool`, venue/feed, original `recv_monotonic` seconds, connection generation, boot/domain identity, frozen roster/config generation and decode state. Gap contract: component, reason, generation, monotonic interval and nullable lost count. Freeze immutable roster/config metadata in its existing background owner; capture its reference at receipt, not current DB state at drain time.

- [ ] Add a failing journal/overflow test; Task 3 defines `record_source` accepting these exact fields:

```python
def test_receipts_survive_unattempted_and_duplicate_deliveries():
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    journal = ReceiptJournal(producer_run_id='source-A', boot_id='boot-A',
                             clock_domain='monotonic-A', max_samples=4)
    for feed, mono, mint, state in [
        ('helius', 10.0, 'MINT', 'decoded_buy'),
        ('shyft', 10.1, 'MINT', 'duplicate_buy'),
        ('helius', 10.2, None, 'pool_unresolved'),
    ]:
        journal.record_source(network_id='solana', source_sig='SOURCE',
            source_wallet='WALLET', mint=mint, pool='POOL', venue='pump_amm',
            feed=feed, recv_monotonic=mono, connection_generation=1,
            roster_generation='roster-1', config_generation='config-1',
            decode_state=state)
    rows = journal.snapshot()['rows']
    assert len(rows) == 3
    assert rows[0]['recv_monotonic'] == 10.0 and rows[2]['mint'] is None
```

- [ ] Extend the real dual-feed winner-first test and single-Helius fixture: order is `entry callback` then `receipt publication`; duplicates retain receipts but dispatch once. Routed candidates retain the first pair-half receipt, not metadata completion time. Native AMM fixture `mint=None` stays unresolved until exact validated pool/attempt binding. Decode error/metadata overflow/expired pair records unknown coverage; never infer no buy from an empty decode.
- [ ] Add strict timestamp and immutable-snapshot tests: zero valid; bool/NaN/inf/negative absent as typed invalid; mutations to caller dictionaries cannot change rows. Per-connection generation survives reconnect. A journal failure must not propagate through the funded callback, including callback-exception paths. Spies fail if producer performs DB, skip lock, network, synchronous log or new queue work.
- [ ] Add drain tests with **no skip rows**: source receipts still persist; a failed DB commit does not advance cursor; next pass retries off-path; producer overwrite before persistence emits interval/lost-count coverage. Source dispatch remains identical with failed/full/disabled measurement.
- [ ] Run red: `python3 -m pytest -n 2 tests/test_memecoin_feed_arrivals.py tests/test_memecoin_slot_signal.py tests/test_memecoin_shyft_signal.py tests/test_memecoin_slot_skips.py tests/test_memecoin_fill_timing.py`.
- [ ] Add three additive tables only to `memecoin_fill_timing.SCHEMA`; keep all legacy tables and state meanings. Use nullable identity fields and decimal monotonic `REAL`, not wall-clock substitutions.

```sql
CREATE TABLE IF NOT EXISTS memecoin_signal_receipts (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  network_id TEXT NOT NULL, source_sig TEXT, source_wallet TEXT,
  mint TEXT, pool TEXT, venue TEXT, feed TEXT,
  recv_monotonic REAL, boot_id TEXT NOT NULL, clock_domain TEXT NOT NULL,
  connection_generation INTEGER NOT NULL,
  roster_generation TEXT, config_generation TEXT, decode_state TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY (producer_run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_signal_receipt_identity
  ON memecoin_signal_receipts(network_id, source_sig, source_wallet, mint);
CREATE TABLE IF NOT EXISTS memecoin_processed_receipts (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  network_id TEXT NOT NULL, our_sig TEXT NOT NULL, slot INTEGER NOT NULL,
  error_state TEXT NOT NULL, recv_monotonic REAL NOT NULL,
  boot_id TEXT NOT NULL, clock_domain TEXT NOT NULL,
  connection_generation INTEGER NOT NULL, evidence_json TEXT NOT NULL,
  PRIMARY KEY (producer_run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_processed_receipt_identity
  ON memecoin_processed_receipts(network_id, our_sig, recv_monotonic);
CREATE TABLE IF NOT EXISTS memecoin_execution_coverage (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  component TEXT NOT NULL, reason TEXT NOT NULL,
  connection_generation INTEGER NOT NULL,
  mono_start REAL, mono_end REAL, lost_count INTEGER,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY (producer_run_id, sequence)
);
```

- [ ] Implement journal publication after existing native/routed dispatch for both signal modes. The snapshot rows are immutable scalar records, max 4096, with one-record cap eviction; no unbounded cleanup loop on receipt. Startup captures boot ID and a unique run ID once. Keep one monotonic clock domain across runner threads and carry original run/boot/domain/receipt time in `_traced`'s existing `source_evidence` without a new commit.
- [ ] Extend the existing background drain to persist bounded snapshots, at most 512 rows per journal/pass, independently of skip batches. Advance each per-run cursor only after its transaction commits. Retain errors/gaps visibly; malformed/schema-failed measurement stays off-path. Do not open a funded-ledger writer or `ATTACH` it.
- [ ] Retain current gate/disposition facts after decisions with exact source identity through existing skip/drain metadata; do not rerun gates. Classify `eligible`, `independent_policy_refused`, `eligibility_unknown`. Unknown pool/proof refusals without complete original inputs stay unknown. Receipt-time cached roster is provenance, not final funded authority. Never reconstruct arming from latest DB.
- [ ] Run the red command green, plus starter/runner regression tests touched by optional measurement wiring. Commit only Task 3 paths: `feat: journal original received entry candidates off-path`.

## Task 4: Observe processed execution and join exact confirmed evidence

**Files:** create `sniper/services/memecoin_execution_observer.py` and `tests/test_memecoin_execution_observer.py`; modify `memecoin_fill_timing.py`, `sniper/execution/solana_real_seams.py::slot_execute_fn.after_settlement`, `scripts/research/fill_timing_resolver.py`, `scripts/memecoin_runner_loop.py`, `sniper/settings/memecoin.py`; extend resolver/timing/runner/real-seams/slot-executor tests.

**Interfaces:**

- `processed_observation(frame, *, subscription_id, generation, received_generation, received_monotonic, boot_id, clock_domain) -> (dict | None, str | None)`.
- `start_execution_observer(*, custody_pubkey, ws_url, journal, status, stop, connect=None, clock=time) -> threading.Thread`.
- `receipt_execution_evidence(receipt, *, custody_pubkey, mint, payment_asset, native_accounting=None) -> dict` in observer module: pure cached-receipt extraction. Returns `state='verified'` with exact decimal-string atoms and receipt slot, or `state='unknown'` with a typed reason. Native accounting must be the current validated helper result for that same receipt; unknown is never reconstructed from floats.
- `execution_bound(source, observations, attempt, pending, fill, *, custody_pubkey) -> dict` in timing service; returns `state`, `reason`, nullable `observed_bound_ms`, nullable `sub200`, and explicitly unverified `canonical_execution_time`.
- `resolve_execution_pass(ledger, timing, *, max_rows=512, max_seconds=5.0, now=None) -> dict` in resolver; no RPC parameter and no money-state mutation. Keep existing `resolve_pass` and CLI unchanged; add result to its summary.

- [ ] Add parser red tests with existing real signature/frame fixture and explicit subscription:

```python
import copy
import json
from pathlib import Path
import pytest
from sniper.services import memecoin_execution_observer as obs

@pytest.fixture
def success_frame():
    frames = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_signal_frames.json').read_text())
    frame = copy.deepcopy(frames['curve_buy'])
    frame['method'] = 'logsNotification'
    frame['params']['subscription'] = 42
    return frame

def test_missing_err_is_unknown(success_frame):
    kw = dict(subscription_id=42, generation=3, received_generation=3,
              received_monotonic=10.190, boot_id='boot-A', clock_domain='mono-A')
    record, reason = obs.processed_observation(success_frame, **kw)
    assert reason is None and record['recv_monotonic'] == 10.190
    success_frame['params']['result']['value'].pop('err')
    record, reason = obs.processed_observation(success_frame, **kw)
    assert record is None and reason == 'missing_err'
```

- [ ] Parametrize bool/string/float/nonpositive slot/subscription, wrong generation/request/ACK/subscription, missing/non-null/false `err`, invalid signature length, list instead of object, `receivedSignature`, bool/negative/NaN/inf clock. None creates a successful witness. Non-null error is retained as a failed observation.
- [ ] Fake socket test stamps `clock.monotonic()` immediately after `recv`, before JSON parsing. Establish exactly one processed mention subscription before notifications; enforce typed ACK matching request and generation, not truthy coercion. Reconnect/timeout/ACK refusal/overflow records gaps; observer stop affects only its daemon. Disabled/failing observer never blocks startup or entry.
- [ ] Pure bound red test explicitly defines the normalized persisted-evidence interface consumed by `execution_bound`:

```python
def test_bound_keeps_first_receipt_and_requires_confirmed_identity():
    from sniper.services.memecoin_fill_timing import execution_bound
    source = dict(network_id='solana', source_sig='SRC', source_wallet='TRADER',
                  mint='MINT', recv_monotonic=10.0, boot_id='B', clock_domain='C')
    observations = [dict(network_id='solana', our_sig='OUR', slot=100,
                         error_state='success', recv_monotonic=t,
                         boot_id='B', clock_domain='C') for t in (10.190, 10.500)]
    attempt = dict(attempt_id='a', pending_id=1, terminal_status='filled',
                   network_id='solana', tx_sig='OUR', mint='MINT',
                   source_sig='SRC', source_wallet='TRADER', landing_slot=100)
    pending = dict(pending_id=1, attempt_id='a', state='confirmed',
                   tx_sig='OUR', custody_pubkey='PAYER', network_id='solana', mint='MINT')
    fill = dict(pending_id=1, attempt_id='a', network_id='solana', tx_sig='OUR',
                mint='MINT', side='buy', chain_confirmed=True,
                custody_pubkey='PAYER', payment_atomic='1', acquired_atomic='1')
    result = execution_bound(source, observations, attempt, pending, fill,
                             custody_pubkey='PAYER')
    assert result['observed_bound_ms'] == pytest.approx(190)
    assert result['sub200'] is True
    assert result['canonical_execution_time'] == 'unverified'
    pending['state'] = 'submitted_unknown'
    assert execution_bound(source, observations, attempt, pending, fill,
                           custody_pubkey='PAYER')['sub200'] is None
```

- [ ] Add parameterized bound tests for absent fill, failed attempt, nonpositive/nonfinite payment/acquisition, wrong wallet/mint/signature/chain/attempt/pending, slot mismatch, earlier error/fork evidence, negative interval and boot/domain mismatch. `201 ms` is observed over target, not proof that execution itself was late. Same slot does not supply bank identity.
- [ ] Add pure receipt-atom tests using existing real receipt fixtures: owner/mint/account-index/decimals binding; explicit zero prebalance; exact positive acquired atoms and USDC decrease through an exotic leg; ownerless/missing/malformed rows; duplicate account index; zero payment/acquisition; missing/nonnull `meta.err`; and a receipt slot different from status-poll landing slot. Missing token-balance row is not zero unless the same complete receipt proves that account was created/closed with the corresponding native pre/post balance zero. Native tests require the same validated native accounting and reject absent accounting. No round-trip through `qty`/`notional` floats.
- [ ] Run parser/bound tests red. Then implement narrow strict parser; do **not** change shared `memecoin_opportunity_live.parse_solana_log_notification`, which currently coerces slots and treats missing `err` as `None`.
- [ ] Add the one default-OFF setting. Runner starts observer when public Solana context is available, using `str(contexts['solana']['pubkey'])` and the existing separate research read URL converted by current URL helper. Failures are typed/off-path; never load a keypair or fall back to custody. Add its journal to the Task 3 drain. Reuse existing daemon lifecycle; no new watchdog/process/shutdown overhaul.
- [ ] Implement pure bound validation before subtraction. Keep first receipt across duplicate/reordered arrivals and late DB join. Persist orphan observations immediately in timing DB; in-memory cap4096 and explicit eviction gaps. Normalized dictionaries in the bound test are an adapter contract, **not database column names**. Map actual rows as follows; do not add guessed columns:

| Evidence | Actual persisted fields / checks |
| --- | --- |
| Attempt | `attempt_id`, `pending_id`, `position_id`, `fill_id`, exact-case `tx_sig`, `mint`, `network_id`, `terminal_status='filled'`, `trace_json` |
| Pending | `id` maps to normalized `pending_id`; `status='confirmed'` maps to normalized state; exact `tx_sig`, `signed_payload_b64`, `signed_payload_sha256`. No payer/state column. |
| Fill | `id`, `position_id`, `attempt_id`, exact `tx_sig`, `network_id`, `kind='entry'`, `fill_source='chain'`; bind through attempt's `fill_id`. No mint/payment-atom/acquired-atom/payer column. |
| Position | `id`, exact mint/network from bound fill position; `execution_mode='real'`. No direct fill mint guess. |
| Payer / signed identity | Check `submission_transport.sha256_hex(pending['signed_payload_b64'])` equals stored hash; this hashes the **base64 string**, not decoded transaction bytes. Decode locally with solders; first signature equals exact attempt/pending/fill signature; first message account is custody signer. |
| Exact receipt evidence | New `receipt_execution_evidence` trace substage from the already-decoded confirmed receipt; payment/acquired atoms and `receipt['slot']`. Require observed processed slot, status-poll landing slot and actual receipt slot all agree. |

- [ ] Do not use `entry_fill_for_attempt`'s case-folded signature comparison as Solana identity proof. Do not infer payment from credit-adjusted `notional`, `fill.qty` REAL or normally-null `funding_spent_atomic`. Missing raw evidence/payer/chain proof is unknown, including historical rows.
- [ ] Extend existing `slot_execute_fn.after_settlement` only after successful fill. Retain the **same cached full receipt** already loaded for `receipt_cu`; append extracted evidence before existing deferred `_complete`/`_finish_traced` terminal trace commit. No new RPC/commit, no booking gate and no entry-thread observer wait. Select `payment_asset='sol' if native else 'usdc'` from the actual executor branch; USDC→exotic→mint still pays USDC, not intermediate tokens.

```python
# Inside the existing successful after_settlement receipt block.
full_receipt = receipt(str(result.get('sig') or tx.signatures[0])) or {}
meta = full_receipt.get('meta') or {}
trace.mark_substage('receipt_cu', consumed=meta.get('computeUnitsConsumed'))
native_accounting = None
if native:
    native_accounting = receipt_sol_entry_fill(
        full_receipt, signature=str(tx.signatures[0]), owner=pubkey, mint=pending['mint'],
        maximum_lamports=int(pending['funding_amount_atomic']),
        tip_lamports=tip_lamports + nozomi_tip)
evidence = receipt_execution_evidence(
    full_receipt, custody_pubkey=pubkey, mint=pending['mint'],
    payment_asset='sol' if native else 'usdc', native_accounting=native_accounting)
trace.mark_substage('receipt_execution_evidence', duration_ms=0.0, evidence=evidence)
```

- [ ] Import pure extraction helper only at the existing deferred accounting seam. Reuse validated native result if already available; otherwise its existing pure helper above reads no provider. Diagnostic extraction failure records unknown and cannot change filled state. Preserve existing result/receipt/accounting parser contracts.
- [ ] Extract atoms strictly from complete raw `preTokenBalances`/`postTokenBalances`, unique `accountIndex`, matching custody/mint and consistent decimals. For USDC use custody's net exact USDC decrease; for native use exact integer `sol_spent_lamports`, separate fee/rent/tips and same receipt provenance. Acquisition is exact raw token delta, never native helper's float `got`. Validate successful receipt, exact signature/payer and positive integer slot; keep atom strings in trace JSON. Array absence/unknown owner/account creation ambiguity stays typed unknown.
- [ ] Extend resolver with indexed, bounded source-receipt pages and independently retried pending joins; no main-ledger write, no RPC, no existing terminal-attempt cursor replacement. Revisit unresolved receipt joins as fills appear. Raw tables remain append-only; no extra materialized-result schema. Report unknown/unattempted/refused rows even with no `tx_sig`. Include per-run source/config identity and coverage intervals.
- [ ] Temporary DB integration tests use `start_entry_attempt`, `reserve_pending`, `bind_entry_submission`, then atomic `finalize_pending_entry`, then `finish_entry_attempt` with exact returned IDs/signature/trace. Do not confirm pending first. Create valid locally signed test-only transaction bytes, not `AA==`. Record Tp before attempt exists; first resolve unknown, confirmed row plus exact receipt-evidence trace later yields190 ms. Writer outage, zero skip rows, unresolved mint and restart retain unknowns. Spy fails on any resolver RPC or settlement/recovery call.
- [ ] Report (a) p50/p90 and counts for complete witnessed confirmed fills, (b) verified sub-200 count / **all eligible received events**, (c) unknown eligibility/identity/observer coverage and every cold/proof/failure/ambiguity outcome. Key cohort by exact source signature/wallet/mint after off-path resolution; never merge wallets by signature alone or replace earlier unknown receipts with later success.
- [ ] Run green: `python3 -m pytest -n 2 tests/test_memecoin_execution_observer.py tests/test_fill_timing_resolver.py tests/test_memecoin_fill_timing.py tests/test_memecoin_slot_skips.py tests/test_memecoin_real_seams.py tests/test_memecoin_solana_slot_executor.py`, plus touched runner tests. Commit only Task 4 paths: `feat: witness processed entry execution without gating trades`.

## Task 5: Learn verified exotic migration pools and retain quote safety

**Files:** modify `sniper/execution/pumpswap_buy.py`, `sniper/services/memecoin_slot_signal.py`, shared stamped-value handling in `raydium_launchlab_buy.py::_live_quote`; extend `tests/test_slot_lane_pool_prewarm.py`, `test_slot_lane_pump_nonsol.py`, `test_slot_lane_exotic_live.py`, `test_pumpswap_buy.py`.

**Interfaces:**

- `create_pool_event` adds `pump_migrated: bool`; `canonical` retains its current wSOL route meaning. Migration listener uses the new field only to select read targets.
- `PumpSwapPoolCache.__init__` adds optional `on_pool_known=None`, preserving existing arguments/defaults. Callback `on_pool_known(pool, facts)` runs outside the existing cache lock after complete streamed/miss learn, not for the full persisted restart set. It queues only bounded preparation work.
- `publish_quote_snapshot(pool, *, expected_facts, value, read_at) -> dict | None` compares the exact pool snapshot before atomic replacement. Successful complete parsed reads retain their actual safety verdict; usable candidates require safe zero-fee facts. Shape/identity failures return `None`; successful unsafe reads invalidate older safe eligibility.

- [ ] Add the event predicate regression beside existing migration fixtures:

```python
def test_exotic_migration_is_a_read_target_without_changing_canonical():
    wsol = ps.create_pool_event(MIGRATIONS['wsol']['logs'])
    usdc = ps.create_pool_event(MIGRATIONS['usdc']['logs'])
    assert wsol['canonical'] is True and wsol['pump_migrated'] is True
    assert usdc['canonical'] is False and usdc['pump_migrated'] is True
```

- [ ] Extend the existing `_MigrationWS` listener test to receive both verified wSOL and USDC migration pool names, once each, while failed transactions, nonzero index, foreign creator and duplicates do not create targets. Mutate existing event bytes using its decoder offsets; do not invent a new event fixture/parser. `_store` must still derive Pump identity from the actual pool account, not trust the event.
- [ ] Add coherent publication regression in `test_slot_lane_pump_nonsol.py` using actual binary SPCX pool facts and existing parsed helper:

```python
def test_quote_snapshot_uses_one_value_and_original_read_time():
    from test_slot_lane_cpmm import _parsed_mint
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.0
    old = cache.get(SPCX['pool'])
    value = _parsed_mint(decimals=6)
    value['owner'] = old['quote_token_program']
    value['data']['parsed']['type'] = 'mint'
    new = cache.publish_quote_snapshot(SPCX['pool'], expected_facts=old,
                                      value=value, read_at=999.0)
    assert new is cache.get(SPCX['pool']) and new is not old
    assert new['quote_read_at'] == 999.0
    value['data']['parsed']['info']['decimals'] = 9
    assert new['mint_values'][SPCX['quote_mint']]['data']['parsed']['info']['decimals'] == 6
    assert cache.publish_quote_snapshot(SPCX['pool'], expected_facts=old,
                                       value=value, read_at=999.5) is None
```

- [ ] Add cases for binary base64 input, wrong owner/type/decimals, unknown mint/epoch, bool/string/NaN/inf/future/expired stamp, hook/nontransferable/paused/frozen/fee-bearing mint and pool replacement during read. None creates a usable preparation candidate. New unsafe parsed evidence must outrank an old passing `_quote_seen` tuple without extra I/O. A failed transport retains old safe evidence only under its original age; successful unsafe evidence does not.
- [ ] Run red: `python3 -m pytest -n 2 tests/test_slot_lane_pool_prewarm.py tests/test_slot_lane_pump_nonsol.py tests/test_slot_lane_exotic_live.py`. Expected missing migration field/publication API and stale-safe precedence regressions.
- [ ] Implement `pump_migrated = index == 0 and creator == expected_authority`; `canonical = pump_migrated and quote_mint == WSOL_MINT`. Preserve current fan-in and graduation authority. Mark existing learn origin as streamed/miss/persisted; only first two can offer candidates.
- [ ] Keep pool/both-mint binary validation in `_store`. Parsed safety must be fetched by the independent Task 6 warmer, never the entry callback: `getAccountInfo(quote_mint, {'encoding': 'jsonParsed', 'commitment': 'confirmed'})`, stamped immediately **before** the request. Reuse `raydium_cpmm_buy.quote_facts(value, epoch=epoch_now())`; no new binary extension decoder. Require exact account owner and parsed decimals matching validated pool facts.
- [ ] Publication uses a deep-copied/frozen parsed value plus `quote_read_at` in one new pool snapshot. Under the existing writer lock, reject when `self._pools[pool] is not expected_facts`. Safe snapshots can seed; valid parsed unsafe snapshots retain the refusal and new read stamp so `_live_quote` rejects instead of consulting older safe state. Static-identity-preserving rereads may carry old parsed facts only with their old timestamp; changed identity drops them.
- [ ] `_live_quote` considers newer bound parsed snapshot/refusal before `_quote_seen`. It retains the existing 300 s reread and fee/token checks. Do not directly seed another worker's dictionary. Update existing stamped-value tests for newer unsafe data, failed refresh and unchanged old stamp.
- [ ] Run green plus `tests/test_pumpswap_buy.py`. Commit only Task 5 paths: `fix: prepare verified exotic Pump migration facts`.

## Task 6: Seed exact clips on one bounded independent warmer

**Files:** modify `sniper/execution/raydium_launchlab_buy.py::WarmExoticLegs`, `pump_curve_buy.py::_funded_jup_call`, `sniper/services/memecoin_slot_lane.py` startup/roster/preparation wiring. Extend `test_slot_lane_exotic_warm_leg.py`, `test_slot_lane_pump_nonsol.py`, `test_slot_lane_exotic_live.py`, `test_slot_lane_warm_clip_pair.py`, `test_memecoin_slot_lane_prefetch_telemetry.py`, `test_memecoin_jupiter_host_failover.py`.

**Interfaces:**

- Keep existing `WarmExoticLegs` arguments; add optional `http=None`. Add `set_clip_inventory(atoms: tuple[int, ...]) -> None`, `seed(venue, quote_mint, usdc_atomic, *, builder, facts)`, `start()`, `wake()`. Extend `get(..., *, builder=None, facts=None, quote_params=None)` for current identity checks; old injected test callers remain valid.
- `exotic_preparation_clips(clip, scale, armed_ids, *, trials=None) -> tuple[int, ...]` in slot lane runs only on background roster/clip owner. It produces exact USDC atoms, never sizing authority.
- `_funded_jup_call(method, path, body=None, *, http=None)` forwards the optional caller-owned transport to existing `solana_real_seams._jup_call(..., http=http)` under existing funded role/counters. Default cold callers are unchanged.
- Private `_warm_http(self, method, url, *, headers=None, body=None)` debits before each actual request through the warmer-owned `PooledHttpClient`. Private `WarmHttpBudget(RuntimeError)` is local speculative-capacity exhaustion, not a fabricated provider 429.

- [ ] Add saturation test with existing helpers:

```python
def test_seed_saturation_preserves_both_live_worker_slots(monkeypatch):
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    live, b, funded, requeued = _worker(monkeypatch, on=True, warm=warm)
    facts = b.pools.get(PEPE_EV.pool)
    warm.set_clip_inventory((10_000_000, 11_000_000, 12_000_000))
    for atoms in (10_000_000, 11_000_000, 12_000_000):
        warm.seed('raydium_cpmm', QUOTE, atoms, builder=b, facts=facts)
    assert len(warm.keys()) == 2 and fetches == []
    assert live.offer(_buy()) is True
    assert live.offer(_buy()) is True
    assert live.offer(_buy()) is False
    assert live._in_flight == 2 and live.max_in_flight == 2
```

- [ ] Add late-publication regression using current injected fetch seam:

```python
def test_late_refresh_cannot_overwrite_newer_live_cold_leg():
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    old, newer = _leg(clock.t), _leg(clock.t + 1)
    warm = None
    def fetch(entry):
        warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=newer, builder=b)
        return _leg(clock.t + 2), [200, 200]
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=fetch)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=old, builder=b)
    clock.t += 2
    warm.refresh_once()
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is newer
```

- [ ] Add exact-clip tests for wallet caps, alternate existing regime tier, trial-only clip, changed cap/disarm, nonfinite/zero/unknown inputs and unsupported final scaling. More than two exact keys yields counted cold misses. Repeated seeds/wakes/refreshes do not renew unused candidate lifetime; actual live use alone changes `last_use`. Used entries take priority over speculative replacement.
- [ ] Add real host-loop HTTP-budget tests using `httpx.MockTransport` and `PooledHttpClient`, not a fake semantic Jupiter call: two healthy legs make four requests; replacement/wakes at +0/+1.49 s make none; +1.5 s permits next bounded cycle; first-host429/fallback200 still triggers warmer backoff and no POST/next candidate; all-host429 counts two; timeout counts one; near-boundary fallback cannot create a fifth. Exhausted speculative budget does not affect default `_funded_jup_call` on a live cold worker.
- [ ] Add essential-refresh test: block the speculative HTTP adapter with an Event, complete balance/blockhash refresh and offer two live cold buys before releasing it. No shared client/ALT cache, no queue occupancy, no wait on prefetch. Existing essential freshness counters and warm TTL tests must still pass.
- [ ] Run these new cases red with `python3 -m pytest -n 2 tests/test_slot_lane_exotic_warm_leg.py tests/test_memecoin_jupiter_host_failover.py tests/test_memecoin_slot_lane_prefetch_telemetry.py tests/test_slot_lane_warm_clip_pair.py`.
- [ ] Derive clips from current `leg_plan(ro, prefetcher.snapshot(), now_iso)` and its existing risk context/ceiling. For the already supported alternate tier, reuse this exact current expression and its guards; do not invent new denominations:

```python
target = 1.0 if scale < 1 else float(cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE)
alternate = scaled_funded_clip(
    base_clip_usd=clip / scale, risk_scale=target,
    floor_usd=float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD))
# Only when existing scaled-SOL switch is on, scale/target are finite valid
# values, and target differs from scale. Wallet cap applies to each tier.
```

- [ ] For each armed `uid`, apply `entry_cap_usd(uid, 'solana', ceiling_usd=solana_flat_clip_ceiling_usd())`; missing cap is uncapped, zero remains zero. A trial uses existing `marker(m.open_trial(ro, uid, 'solana'))['clip_usd']` instead of normal tiers, then the same cap. Read this only on existing background roster pass; rollback its read-only transaction before provider work. Reservation/trial/exposure revalidation stays authoritative.
- [ ] Publish immutable exact clip inventory with config generation through `set_clip_inventory`. Reconcile at inventory or validated streamed/miss pool change; no persisted-pool sweep or tick-based reseed. Without current inventory, retain at most two candidate identities; no quote fetch. Reject new seed atoms outside inventory. A changed inventory removes unusable seeds/increments their generation without canceling a live buy. Add explicit test: remove one allowed clip during its blocked fetch, then release fetch; no old key/leg is published.
- [ ] Extend existing entries with generation, immutable binding, `candidate_at`, nullable `last_use`, original pool reference and leg. Binding includes network/venue/pool/base/quote/programs/decimals/payer/route query/slippage. Ready/in-flight/pending keys share the two-key ceiling. Same identity cannot reset the 60 s window. Before I/O copy binding/generation/reference; compare all three before publish. Live `note`, replacement, pool/config/payer changes invalidate an old completion.
- [ ] Never hold pool/warm cache locks during provider I/O, response parsing or instruction/ALT decoding. Existing short critical sections only snapshot/compare/publish at most two cache keys. Blocked-fetch tests must prove concurrent warm lookup and live note still finish before the provider Event is released.
- [ ] Make `get` a pure lookup under its existing lock: hit does not update `last_use` or priority. Call existing `note(..., leg=leg, builder=builder)` only after live admission succeeds, for cold and warm hits. A refused warm hit cannot keep a speculative entry alive. Add test: 59 s old candidate, warm lookup followed by admission refusal, then +2 s; expired despite hit. A successful admission renews actual-use window with the same original leg stamp.
- [ ] Start exactly one daemon after current signal/startup validation succeeds. `refresh_builder_legs` calls `warm.wake()` instead of `warm.refresh_once()`. Warm fetch/JSONparsed safety/ALT reads run serially on its own client and private ALT cache. `wake` never waits or fetches; existing `refresh_once` remains deterministic/injectable for tests.
- [ ] Implement the local rolling budget at the actual HTTP boundary. The transport accepts existing header/body arguments and preserves returned `(status, payload, third_value)` shape:

```python
class WarmHttpBudget(RuntimeError):
    pass

def _warm_http(self, method, url, *, headers=None, body=None):
    now = self.clock.monotonic()
    while self._http_starts and self._http_starts[0] <= now - 1.5:
        self._http_starts.popleft()
    if len(self._http_starts) >= 4:
        raise WarmHttpBudget('warm_http_budget')
    self._http_starts.append(now)
    result = self._http.request(method, url, headers=headers, body=body)
    self._actual_statuses.append(result[0])
    return result
```

- [ ] Deque is one-owner only; create it with `collections.deque()` and `_actual_statuses=[]`. Use one cycle start at least 1.5 s after prior initial/refresh start, never reset by replacement/wake. Do not begin GET with fewer than two tokens remaining. Capture actual 401/429 even when fallback returns200; finish bounded host selection, then stop before POST/next candidate and apply existing warmer backoff. Budget exhaustion/transport/403/malformed failures publish no partial leg and never retry immediately. Live cold calls have no new budget.
- [ ] `_funded_jup_call` change is only `http` forwarding; preserve existing role accounting/error behavior. Create `PooledHttpClient(max_connections=2, max_keepalive=2)` once on warmer owner, retaining bounded timeouts/no retries. RPC adapter is caller-owned/read-only. Never replace global HTTP, DNS, environment, role state or `builder._alt_cache`.
- [ ] Validate the speculative GET before POST: exact USDC/quote/amount/ExactIn/slippage, positive atomic output/floor, floor≤output, nonempty valid routePlan. Bind original GET start as leg freshness; publication/completion cannot restamp it. Existing instruction decoder validates mandatory swap/setup/cleanup; all signer keys must match expected payer. Validate each ALT owner/address/data with existing decoder, cache privately and return complete decoded table tuples. No weaker fallback response or inferred decimals.
- [ ] Add prepared-first-buy regression in `test_slot_lane_pump_nonsol.py`. Existing `_leg` here constructs a local no-ALT leg; separate real ALT test below covers caching.

```python
def test_first_prepared_pump_buy_builds_and_signs_without_provider_calls(pump_listed, monkeypatch):
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from sniper.execution import raydium_launchlab_buy as LL
    from test_slot_lane_cpmm import _parsed_mint
    from test_slot_lane_exotic_warm_leg import Clock
    clock, b, kp = Clock(50.0), _builder(SPCX), Keypair()
    b.user, b.clock, b.pools.clock = str(kp.pubkey()), clock.monotonic, clock.monotonic
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True, raising=False)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 30.0)
    old = b.pools.get(SPCX['pool'])
    value = _parsed_mint(decimals=6)
    value['owner'] = old['quote_token_program']
    value['data']['parsed']['type'] = 'mint'
    prepared = b.pools.publish_quote_snapshot(SPCX['pool'], expected_facts=old,
                                               value=value, read_at=49.9)
    assert prepared is not None
    leg = _leg(SPCX['quote_mint'], 40_000)
    leg['quote'] = dict(inputMint=pcb.USDC_MINT, outputMint=SPCX['quote_mint'],
        inAmount='10000000', outAmount='40000', otherAmountThreshold='39400',
        swapMode='ExactIn', slippageBps=pcb.DEFAULT_LEG_SLIPPAGE_BPS,
        routePlan=[{'swapInfo': {'label': 'fixture', 'ammKey': SPCX['pool']}}])
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=lambda entry: (leg, [200, 200]))
    warm.set_clip_inventory((10_000_000,))
    warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b, facts=prepared)
    warm.refresh_once()
    requeued = []
    worker = LL.OnDemandLegWorker(
        builders={'pump_amm': b}, admission_fn=lambda buy, quote: None,
        snapshot_fn=lambda: SNAP, clip_fn=lambda: 10.0, offer_fn=lambda buy, result: None,
        start=False, clock=clock, live_fn=lambda venue: True, requeue_fn=requeued.append,
        refuse_fn=lambda buy, reason: pytest.fail(reason), warm=warm)
    def forbidden(*args, **kwargs):
        pytest.fail('provider call after a completely prepared source buy')
    b.pools.rpc = b.rpc = b.read_rpc = forbidden
    warm.fetch_fn = forbidden
    monkeypatch.setattr(pcb, '_funded_jup_call', forbidden)
    buy = _slot_buy(SPCX, recv_monotonic=clock.monotonic())
    assert worker.process(buy)['warm_leg'] is True
    assert requeued[0].recv_monotonic == buy.recv_monotonic
    message, detail = L.dispatch_build({'pump_amm': b})(requeued[0], 10.0, SNAP)
    assert message is not None, detail
    assert bytes(VersionedTransaction(message, [kp]))
```

- [ ] Add real mocked ALT response using existing ALT fixture/helper: first refresh decodes once, second same-table refresh forbids ALT RPC; wrong/new owner/address/table cannot borrow old identity. Parameterize payer/quote/program/clip/query/slippage mismatches and stale stamp; every mismatch is existing cold/refusal, never warm oversizing. Preserve proof/copy-floor/chase/trial/resize/packet-fit acceptance tests; the stub admission above proves preparation only.
- [ ] Run green preparation files together, then all changed shared-helper caller tests. Record request starts, cache identity, original stamps and blocked-worker evidence. Commit only Task 6 paths: `fix: prewarm exact entry legs within bounded provider capacity`.

## Task 7: Validate and select the isolated TPU read source

**Files:** create `scripts/research/memecoin_tpu_read_provider_control.py`, `tests/test_memecoin_tpu_read_provider_control.py`; update comment at `sniper/settings/memecoin.py::MEMECOIN_TPU_RPC_URL` and precise read-policy paragraphs in `AGENTS.md`, `docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md`, `2026-07-29-memecoin-solana-rpc-credit-rotation-design.md`. Existing `memecoin_real_runner.py` passes this setting already; no new SDK/WS setting/leader adapter.

**Interface:** control CLI accepts `--code-root`, `--manifest`, `--rpc-env-name`, `--duration-s` (hard maximum20), `--output`. Internal `run_control(*, code_root, manifest_path, rpc_env_name, duration_s, output_path, spawn=None, connect=None, http=None, clock=time) -> dict`; test injections never spawn production children. `verify_control_identity(rpc_url, *, http) -> None` raises a sanitized `RuntimeError` on failed identity. Child socket receives health frames only; identity validation makes one isolated HTTPS read before child startup.

- [ ] Add fake-child/socket tests: only `health=True` frames, no signed bytes/submit count; mismatch manifest/hash, unavailable schedule/zero leaders, frozen/unready child, timeout, wrong cluster and RPC/WS errors fail; cleanup terminates/waits only its spawned PID and cannot unlink the live socket. Child env contains only `SNIPER_TPU_RPC_URL`, `SNIPER_TPU_SOCKET_PATH`, `SNIPER_TPU_PARENT_PID` and minimum OS loader variables required by current launcher; secrets/endpoints never enter argv/output/error text.
- [ ] Add this actual single-request identity test in the new module; then parameterize wrong genesis,429, timeout, RPC error, malformed object and mismatched ID. Each failure must occur before the injected spawn can run.

```python
def test_control_identity_uses_one_isolated_read():
    import json
    from scripts.research import memecoin_tpu_read_provider_control as c
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    calls = []
    def http(method, url, *, body=None, timeout=None):
        request = json.loads(body)
        calls.append((method, url, request))
        return 200, {'jsonrpc': '2.0', 'id': request['id'],
                     'result': SOLANA_MAINNET_GENESIS}, None
    assert c.verify_control_identity('https://isolated.invalid', http=http) is None
    assert len(calls) == 1
    assert calls[0][0] == 'POST' and calls[0][1] == 'https://isolated.invalid'
    assert calls[0][2] == {'jsonrpc': '2.0', 'id': 'tpu-control-genesis',
                           'method': 'getGenesisHash', 'params': []}
```

- [ ] Run red: `python3 -m pytest -n 2 tests/test_memecoin_tpu_read_provider_control.py`. Then implement bounded health-only control using existing verifier/protocol/launch primitives, not a second production service.
- [ ] Verify exact active code root/SHA and call `scripts.memecoin_runtime_verify.verify_sidecar(manifest['sidecar'], code_root, manifest['sha'])` before spawning. Do not use `verify_release` default validator; it opens writable DBs/constructs funded contexts. Use a unique private temporary dir/socket, manifest-bound immutable binary and bounded child lifecycle. No live socket, keypair, funded context or production state log.
- [ ] Identity helper sends exactly `getGenesisHash` to that same isolated endpoint, bounded2 s per transport phase/response body, no redirect/retry, matching JSON-RPC ID, no error and exact `free_endpoint_registry.SOLANA_MAINNET_GENESIS`. Do not reuse `probe_solana_identity`, which adds `getHealth`. Include this read within the20 s overall control deadline; it creates zero custody/main-quote/read-head requests. Wrong or unavailable identity blocks child startup.
- [ ] Default identity transport uses existing installed httpx streaming with `timeout=2.0`, `follow_redirects=False`; cap decoded response body at4096 bytes before JSON parsing, close it on every path and sanitize exceptions. Injected `http` keeps the tested status/payload tuple. Do not use an unbounded pooled JSON response as proof of the body cap. Tests include4097-byte body and redirect rejection with no second request.
- [ ] Endpoint comes from named env only. Rust derives WS normally; Agave owns bootstrap/schedule/reconnect/preconnection. Existing health proves cached leader sockets and clock freshness; it **does not** verify mainnet/genesis or independently validate epoch/schedule correctness. Genesis helper establishes endpoint-reported cluster identity; schedule remains trusted to that verified source. Capture sanitized readiness/count/advance intervals and binary/source/lock identities. Without already retained independent native-head timestamps, no-send control cannot certify the two-slot relation; organic captured guards must do that.
- [ ] Amend only the approved read-policy exception: separate same-provider/region read credential and capacity; no custody/watch key reuse; public fallback policy elsewhere unchanged. Do not reinterpret this as approval of paid submission, new tips or new copy authority.
- [ ] Run green control/sidecar/runtime-verifier tests. Commit only Task 7 paths: `fix: validate isolated TPU read-source alignment`.
- [ ] At activation, supply isolated endpoint securely and run the control against the exact pinned artifact:

```bash
python3 scripts/research/memecoin_tpu_read_provider_control.py \
  --code-root "$(readlink -f .runtime/memecoin/current)" \
  --manifest data/memecoin_deploy.json \
  --rpc-env-name SNIPER_TPU_CONTROL_RPC_URL --duration-s 20 \
  --output docs/audits/2026-10-11-frank-entry-speed/tpu-no-send-control.json
```

- [ ] Validate actual current manifest path before invocation. Require endpoint-reported mainnet identity, successful SDK epoch/schedule bootstrap, cached leader availability and typed ACKs/errors; independent schedule correctness remains unverified. Separate key text is insufficient proof of account quota isolation. Measure HTTP+WS capacity, redaction and **zero extra custody/write/main-quote requests**; compare existing provider latency p50/p90 and essential freshness without adding high-rate probes. Isolated errors/429/shared capacity/frozen schedule block activation.
- [ ] Select endpoint using existing `MEMECOIN_TPU_RPC_URL` only in pinned rollout, verify actual child env/source label without printing key and verify PID/binary hash. Preserve 1000 ms/two-slot guards and reject-only native head. If selected HTTP+WS capacity fails, stop this activation and report concrete cause; an extra WS-only setting requires a separate scoped decision.

## Task 8: Validate, pin and measure the organic cohort

**Files:** existing runtime scripts and regression modules; add `implementation-validation.md`, `passive-baseline.md`, `organic-results.md` under `docs/audits/2026-10-11-frank-entry-speed/` as evidence becomes available. No synthetic funded trade.

**Interfaces:** unchanged `scripts/deploy_memecoin_runtime.sh <merged-sha> --defer-backup`; new timing summary includes source universe and processed bounds, existing slot/block diagnostics remain separate.

- [ ] Before any product edit, report the approved edge-case table to the operator: wrong identity/unsolicited tokens, absent-zero-unknown, unsafe/stale quote facts, clip/config replacement, worker saturation/provider failures, duplicates/late proof, signed ambiguity, fork/slot/clock mismatch and observer/writer gaps. State exclusions: first unseen routes, late proof, unavailable providers and leader phase can still miss200; historical Frank subcall/initial guard snapshots cannot be recovered.
- [ ] At execution start, read worktree/TDD/verification skills, inspect attached worktrees and create/reuse an isolated clean checkout through managed worktree tooling. Query current brain/spec/code before relying on historic notes. Do not move unrelated WIP into commits.
- [ ] Copy only this approved spec/plan and its named evidence documents from the main checkout into the isolated worktree; these are currently untracked and a Git worktree will not inherit them. Verify document links, then commit the task-owned documentation before product changes. Do not copy unrelated `docs/audits`, `data`, `.env` or dirty tracked files. This final plan's explicit interfaces override exploratory notes where they differ.
- [ ] Prove each new regression fails on its pre-fix code and passes afterward. Keep actual failure/output summaries with exact commit IDs. Check all callers before changing optional arguments; preserve legacy traces, cached paths, injected clocks/clients and old protocol peers.
- [ ] Run focused acceptance grouped once: signal/receipt/observer/timing, pool/leg/clips/admission, TPU/transport/runtime. Include exact source-initiation/copy-price/duplicate/reservation/settlement/unknown-recovery tests for all changed shared seams. No redundant reruns after strong unchanged checks.
- [ ] Run one broader full suite if shared money-path changes require it; this change does. Override only workers, preserving normal markers/timeouts:

```bash
flock /home/bigpapi/.cache/codex-pytest-full.lock python3 -m pytest -n 2
```

- [ ] Reproduce baseline failures separately on pinned base with the same command/fixtures before attributing them. Existing two baseline failures do not authorize swallowing a new one. Record exact failures and limits; do not claim an all-green suite if it stopped.
- [ ] Measure publisher cost with local real-fixture replay and an injected no-op funded callback: at least10000 receipts each for native/routed and duplicate/gap paths, report p50/p90 microseconds and interpreter/resource conditions. Do not assert noisy wall-clock thresholds in unit tests. Compare prepared dispatch baseline/measurement branch; reject an introduced synchronous dependency or regression beyond measurement uncertainty.
- [ ] Build exact immutable source-SHA sidecar artifact after Task 2 budget fix; retain mode0555, source/binary/Cargo.lock/build-image digest/target identity. Never overwrite existing SHA artifact or skip TPU validation. Run normal candidate manifest/config/reservation/reconciliation gates.
- [ ] First pin Tasks 1–4 with existing TPU source and preparation behavior, enable passive observer only after separate read capacity is verified, and collect organic baseline where available. Do not wait on observer before entries. If no baseline is available, label later observations absolute post-change only; no historical processed execution comparison is possible.
- [ ] Activate preparation and TPU source as separately identified pinned releases/config steps, preserving runtime atomic rollback and signed/unknown reservation handling. Use normal deployer with `--defer-backup`; no direct mutable-checkout runner launch. Verify actual runner/dashboard/watchers/exits and sidecar release/PID/hash/config, authenticated status, ledger reservations/reconciliation and heartbeat freshness independently.
- [ ] Freeze observed cohort/release/config/roster provenance before evaluating. Count every received supported armed source event: eligible, independent policy refusal, eligibility unknown, cold, proof-late, failed, submitted unknown, confirmed with observer gap and verified sub200. Deduplicate only exact source signature/wallet/mint off-path, retaining every raw feed receipt and earliest valid original T0. No denominator from attempts/fills/cache hits.
- [ ] Separate metrics: original signal→dispatch; first acknowledgment; successful processed observation later confirmed same signature/slot; landing-slot delta; confirmation recognition; receipt/accounting. Never multiply slot gap by400 ms for execution latency. Original Frank total1068 ms includes recognition/accounting and is not a known execution timestamp.

| Organic gate | Required evidence |
| --- | --- |
| Completely warm, proof-ready dispatch | p50≤20 ms; p90≤40 ms from original socket T0 |
| Same-stratum acknowledgment | p50≤35 ms; p90≤60 ms; transport evidence only |
| Complete processed/confirmed witness distribution | p50 target120 ms; p90≤200 ms, with counts and censored outcomes |
| All admissible received-event criterion | ≥90% have successful ≤200 ms processed witnesses later confirmed same signature/slot; cold/proof-late/failures/unknowns count unmet |
| Sample | ≥100 unique organic submitted entries; ≥30 complete witnesses per reported warm/cold stratum; ≥95% witness coverage among confirmed fills |
| Safety/resources | unchanged money behavior, essential freshness and prepared dispatch; actual warmer HTTP budget and isolated TPU/observer capacity verified |

- [ ] Report unknown eligibility explicitly; it prevents a full-universe verdict until coverage is resolved. Fewer organic cold examples means cold performance remains unproved. Same signature/slot lacks bank hash: label metric **successful processed observation within200 ms, later confirmed same signature and slot**, not exact canonical execution time. A delayed observation over200 is not proof of late execution.
- [ ] If verified warm dispatch is still slow, use retained Task 1 stages before further optimization. If dispatch meets budget but execution witness misses, retain TPU initial guard/participation, source phase and provider evidence before any fee/feed/policy proposal. Do not substitute fast acknowledgment for the requested fill goal.
- [ ] Roll back failed preparation with existing warm switch and prior pinned release; keep cold deadline. Replaced warm generations cannot publish. Observer stop/failure retains raw timing evidence/gaps and money behavior. TPU producer/consumer/source rollback is coordinated through prior manifest/config; no release/re-sign of ambiguous reservations.
- [ ] Final report gives implementation commit/pinned release, actually executed checks, organic counts/coverage, measured p50/p90, all-event success fraction, unknowns and remaining root causes. Mark the200 ms goal unproved until organic gates pass; even then it is an observed-cohort criterion, not a universal guarantee.

## Evidence and implementation references

- [Frank incident and timing boundaries](../../audits/2026-10-11-frank-entry-speed/incident.md).
- [Bounded current-route probe](../../audits/2026-10-11-frank-entry-speed/probe-readme.md); its109.523 ms analog is not the historical135 ms split or a p50/p90 sample.
- [Paired provider/SDK evidence](../../audits/2026-10-11-frank-entry-speed/slot-clock-readme.md) and [pinned SDK replay](../../audits/2026-10-11-frank-entry-speed/slot-clock-sdk-replay.json); initial historical guard remains unknown.
- [Exact preparation caller/fixture notes](../../audits/2026-10-11-frank-entry-speed/preparation-plan-notes.md).
- [TPU protocol/build/control notes](../../audits/2026-10-11-frank-entry-speed/tpu-plan-notes.md).
- [Observer/universe/storage notes](../../audits/2026-10-11-frank-entry-speed/observer-plan-notes.md).

## Plan review performed before handoff

Coverage is Tasks 1–2 diagnostic retention, Tasks 3–4 receipt universe and execution witness, Tasks 5–6 complete bounded preparation, Task 7 selected TPU source and Task 8 organic acceptance/rollback. Review Focus items each have an owning red regression. Public interfaces are defined above; no later task may quietly introduce new sizing/proof/provider authority. Planned code/tests/control/deployment have not run while writing this plan.

## Scoped commit commands

Run each block only after its task's red/green checks and review. Use the isolated worktree; no `git add .`, main-checkout WIP or runtime secrets.

```bash
git add sniper/execution/pump_curve_buy.py sniper/execution/raydium_launchlab_buy.py sniper/services/memecoin_slot_lane.py tests/test_slot_lane_leg_call_slow.py tests/test_slot_lane_exotic_warm_leg.py tests/test_memecoin_slot_lane.py
git commit -m "fix: retain off-thread route preparation timings"
```

```bash
git add rust/solana-tpu-sidecar/src/delivery.rs rust/solana-tpu-sidecar/src/main.rs rust/solana-tpu-sidecar/Dockerfile scripts/build_memecoin_tpu_sidecar.sh sniper/execution/solana_tpu_sidecar.py sniper/execution/submission_wiring.py tests/test_memecoin_solana_tpu_sidecar.py tests/test_memecoin_submission_transport.py tests/test_memecoin_tpu_sidecar_build.py
git commit -m "fix: preserve TPU guard decision evidence"
```

```bash
git add sniper/services/memecoin_feed_arrivals.py sniper/services/memecoin_slot_signal.py sniper/services/memecoin_shyft_signal.py sniper/services/memecoin_slot_lane.py sniper/services/memecoin_slot_skips.py sniper/services/memecoin_fill_timing.py scripts/memecoin_runner_loop.py tests/test_memecoin_feed_arrivals.py tests/test_memecoin_slot_signal.py tests/test_memecoin_shyft_signal.py tests/test_memecoin_slot_skips.py tests/test_memecoin_fill_timing.py
git commit -m "feat: journal original received entry candidates off-path"
```

```bash
git add sniper/services/memecoin_execution_observer.py sniper/services/memecoin_fill_timing.py sniper/execution/solana_real_seams.py scripts/research/fill_timing_resolver.py scripts/memecoin_runner_loop.py sniper/settings/memecoin.py tests/test_memecoin_execution_observer.py tests/test_fill_timing_resolver.py tests/test_memecoin_fill_timing.py tests/test_memecoin_slot_skips.py tests/test_memecoin_slot_lane.py tests/test_memecoin_real_seams.py tests/test_memecoin_solana_slot_executor.py
git commit -m "feat: witness processed entry execution without gating trades"
```

```bash
git add sniper/execution/pumpswap_buy.py sniper/execution/raydium_launchlab_buy.py sniper/services/memecoin_slot_signal.py tests/test_slot_lane_pool_prewarm.py tests/test_slot_lane_pump_nonsol.py tests/test_slot_lane_exotic_live.py tests/test_pumpswap_buy.py
git commit -m "fix: prepare verified exotic Pump migration facts"
```

```bash
git add sniper/execution/raydium_launchlab_buy.py sniper/execution/pump_curve_buy.py sniper/services/memecoin_slot_lane.py tests/test_slot_lane_exotic_warm_leg.py tests/test_slot_lane_pump_nonsol.py tests/test_slot_lane_exotic_live.py tests/test_slot_lane_warm_clip_pair.py tests/test_memecoin_slot_lane_prefetch_telemetry.py tests/test_memecoin_jupiter_host_failover.py
git commit -m "fix: prewarm exact entry legs within bounded provider capacity"
```

```bash
git add scripts/research/memecoin_tpu_read_provider_control.py tests/test_memecoin_tpu_read_provider_control.py sniper/settings/memecoin.py AGENTS.md docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md docs/superpowers/specs/2026-07-29-memecoin-solana-rpc-credit-rotation-design.md
git commit -m "fix: validate isolated TPU read-source alignment"
```

```bash
git add docs/audits/2026-10-11-frank-entry-speed/implementation-validation.md docs/audits/2026-10-11-frank-entry-speed/passive-baseline.md docs/audits/2026-10-11-frank-entry-speed/organic-results.md
git commit -m "docs: record entry latency validation and remaining evidence gaps"
```
