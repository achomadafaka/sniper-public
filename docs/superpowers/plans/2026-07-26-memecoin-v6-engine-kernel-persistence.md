# Memecoin V6 Engine Kernel and Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make V6 decisions depend on distinct semantic evidence, keep engine state bounded, and persist one crash-safe action intent without duplicating complete state on every hold.

**Architecture:** Add explicit market/quote/source evidence identities to the public observation, replace sweep-count state with bounded lane-aware state, and make one active action intent part of the transition contract. Upgrade the isolated SQLite ledger to store compact state deltas and successor data only for actions while preserving exact reconstruction and restart idempotency.

**Tech Stack:** Python 3 dataclasses/enums, standard-library `hashlib/json/statistics`, SQLite WAL, pytest.

## Global Constraints

- V6 is the only candidate engine and only post-fill authority for the V6 paper book.
- Runtime policy identity is `v6-position-engine-v1`; there is no selector and no silent migration of open positions with another hash.
- Wall-clock wake time cannot advance market, quote, or source evidence state.
- Volatility history contains at most 64 distinct market samples; serialized current state is at most 16 KiB.
- One active action intent exists per position; unfilled actions do not consume quantity or tranche budget.
- V6 remains default OFF. No live RPC, production DB write, funded import, service activation, deploy, or Supabase write is authorized.
- Use RED-first TDD. One logical change per commit. Run `git diff --check` before every commit.

---

## File map

- Modify `sniper/services/memecoin_v6_engine.py`: public V1 types, semantic identity validation,
  bounded state, deterministic transition, action-intent merge.
- Modify `sniper/services/memecoin_v6_policy.py`: bounded `V6State`, episode-local recovery, and
  distinct-market advancement while retaining current staged actions for this plan.
- Modify `sniper/services/memecoin_v6_observations.py`: construct semantic lane identities and stop
  using wake time as observation identity.
- Modify `sniper/persistence/db_memecoin_v6_paper.py`: schema V2 migration, compact decision deltas,
  active intents, state reconstruction.
- Modify `sniper/services/memecoin_v6_paper_runner.py`: semantic no-op handling and persistent intent
  recovery.
- Modify `scripts/memecoin_v6_paper_worker.py`: schema/version startup validation only.
- Modify `tests/test_memecoin_v6_engine.py`, `tests/test_memecoin_v6_observations.py`,
  `tests/test_memecoin_v6_paper_db.py`, `tests/test_memecoin_v6_paper_runner.py`, and
  `tests/test_memecoin_v6_paper_worker.py`: RED regressions and integration coverage.

### Task 1: Lock V1 public types and bounded serialization

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py:31-122`
- Modify: `sniper/services/memecoin_v6_policy.py:27-60`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Produces: `EvidenceIdentity`, `BreakEpisode`, `ActionIntent`, `V6PositionState`,
  `V6Observation`, `V6Decision`, `state_document(state) -> dict`, and
  `state_from_document(document) -> V6PositionState`.
- Consumes: no later-task interface.

- [ ] **Step 1: Write failing type-contract tests**

```python
def test_v1_state_document_is_bounded_and_round_trips():
    samples = tuple((f"market-{i}", float(i)) for i in range(64))
    state = _state(volatility_samples=samples, last_market_evidence_id="market-63")
    encoded = canonical_json(state_document(state))
    assert len(encoded.encode()) <= 16 * 1024
    assert state_from_document(json.loads(encoded)) == state

def test_v1_state_rejects_more_than_64_volatility_samples():
    with pytest.raises(ValueError, match="64"):
        _state(volatility_samples=tuple((str(i), 0.1) for i in range(65)))

def test_policy_identity_is_v1():
    assert POLICY_ID == "v6-position-engine-v1"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Expected: FAIL because the V1 types and serialization functions do not exist.

- [ ] **Step 3: Add the V1 dataclasses and validators**

Implement these exact shapes in `memecoin_v6_engine.py`:

```python
MAX_VOLATILITY_SAMPLES = 64
MAX_STATE_BYTES = 16 * 1024
POLICY_ID = "v6-position-engine-v1"

@dataclass(frozen=True)
class EvidenceIdentity:
    market: str | None = None
    quote: str | None = None
    source: str | None = None

@dataclass(frozen=True)
class BreakEpisode:
    episode_id: str
    break_level: float
    first_market_evidence_id: str
    neg_streak: int = 0
    status: str = "active"

@dataclass(frozen=True)
class ActionIntent:
    token: str
    action: V6Action
    fraction_ppm: int
    priority: int
    trigger_evidence_id: str

@dataclass(frozen=True)
class V6PositionState:
    position_id: str
    remaining_qty_atomic: int
    volatility_samples: tuple[tuple[str, float], ...] = ()
    last_market_evidence_id: str | None = None
    last_quote_evidence_id: str | None = None
    last_source_evidence_id: str | None = None
    break_episode: BreakEpisode | None = None
    filled_tranches: int = 0
    executable_high_water: float | None = None
    active_intent: ActionIntent | None = None
    last_action_outcome: str | None = None
```

Keep the enum values `HOLD`, `DERISK`, and `EXIT_FULL`. Validate status values, positive quantities,
finite floats, unique sample ids, the 64-sample cap, and the 16-KiB serialized limit. Centralize
serialization in the engine so the ledger no longer imports the historical seed type.

- [ ] **Step 4: Run focused tests and check hygiene**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Run: `git diff --check`

Expected: PASS and no whitespace errors.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy.py tests/test_memecoin_v6_engine.py
git commit -m "refactor: lock bounded v6 engine state"
```

### Task 2: Add semantic observation identities

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py:72-211`
- Modify: `sniper/services/memecoin_v6_observations.py:254-350`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_observations.py`

**Interfaces:**
- Consumes: `EvidenceIdentity` from Task 1.
- Produces: `make_observation(..., evidence: EvidenceIdentity, ...) -> V6Observation` and
  `semantic_observation_id(position_id, evidence) -> str`.

- [ ] **Step 1: Write cadence-identity regressions**

```python
def test_wake_time_does_not_change_semantic_observation_id():
    a = build_observation(_position(), now_iso="2026-07-26T12:00:00.000+00:00", **_sources())
    b = build_observation(_position(), now_iso="2026-07-26T12:00:00.100+00:00", **_sources())
    assert a.evidence.market == b.evidence.market == "solana:sig-1:3"
    assert a.observation_id == b.observation_id

def test_new_log_identity_changes_market_evidence():
    a = _observation(block_identity="4663:0xblock:0xtx:7")
    b = _observation(block_identity="4663:0xblock:0xtx:8")
    assert a.evidence.market != b.evidence.market
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_observations.py -q`

Expected: FAIL because `V6Observation` has no evidence lanes and observation ids include `now`.

- [ ] **Step 3: Implement canonical lane identities**

Add:

```python
def semantic_observation_id(position_id: str, evidence: EvidenceIdentity) -> str:
    return "obs-" + fingerprint({"position_id": position_id, "evidence": asdict(evidence)})[:24]
```

Build `market` from network, pool, capture version, and `block_identity`; build `quote` from provider,
route binding, exact input/output, quote fingerprint, and quote-observed time; build `source` from the
exact source event id. Exclude worker wake time. Keep `observed_at` and receive/process timestamps in
the payload for audit only.

Reject an observation with no semantic lane identity. Do not manufacture a market id from
`observed_at`; a missing chain cursor remains `None`.

- [ ] **Step 4: Run focused tests and existing observation contract**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_observations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_observations.py tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_observations.py
git commit -m "fix: key v6 transitions to semantic evidence"
```

### Task 3: Make market advancement cadence-invariant and recovery episode-local

**Files:**
- Modify: `sniper/services/memecoin_v6_policy.py`
- Modify: `sniper/services/memecoin_v6_engine.py:225-310`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Consumes: V1 state and `observation.evidence.market`.
- Produces: `advance_market_state(state, observation) -> V6PositionState` and current staged
  `step()` behavior over distinct evidence.

- [ ] **Step 1: Write the exact reproduced failure and episode tests**

```python
def test_same_break_evidence_at_10hz_cannot_consume_multiple_tranches():
    state = _state()
    first = step(state, _break_observation("market-1", observed_at="2026-07-26T12:00:00+00:00"))
    assert first.action is V6Action.DERISK
    state = first.state_if_filled
    for tenth in range(1, 600):
        again = step(state, _break_observation("market-1", observed_at=_plus_100ms(tenth)))
        assert again.action is V6Action.HOLD
        assert again.semantic_transition is False
        assert again.state_if_filled == state
    assert state.filled_tranches == 1

def test_recovery_only_closes_current_episode():
    after_first = step(_state(), _break_observation("market-1")).state_if_filled
    recovered = step(after_first, _recovery_observation("market-2")).state_if_filled
    assert recovered.break_episode.status == "recovered"
    later = step(recovered, _break_observation("market-3", prior_low=0.7))
    assert later.action is V6Action.DERISK
    assert later.state_if_filled.break_episode.episode_id != recovered.break_episode.episode_id
```

- [ ] **Step 2: Run the two tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -k 'same_break_evidence or recovery_only' -q`

Expected: FAIL with repeated tranche/terminal behavior and permanent recovery.

- [ ] **Step 3: Implement distinct-market advancement**

On `market_evidence_id == state.last_market_evidence_id`, return a semantic no-op unless a quote,
source, or time-boundary rule changed. Append one `(market_id, realized_vol)` pair only for a new
market id, then retain `samples[-64:]`. Compute p90 with:

```python
def percentile(values: tuple[float, ...], pct: int) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return ordered[lo] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
```

Replace permanent `recovered: bool` with `BreakEpisode.status`. Start a new episode only from a
distinct nominated-break market id. Count negative-flow confirmation only on distinct market ids.

- [ ] **Step 4: Run engine tests and bounded stress**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Expected: PASS, including 100,000 generated transitions with 64 retained samples.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy.py tests/test_memecoin_v6_engine.py
git commit -m "fix: make v6 state advancement cadence invariant"
```

### Task 4: Add stable action-intent merge semantics

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Consumes: `ActionIntent` and semantic evidence from Tasks 1–3.
- Produces: `merge_action_intent(state, candidate) -> ActionIntent | None`,
  `V6Decision.action_intent_token`, and `V6Decision.semantic_transition`.

- [ ] **Step 1: Write failing intent tests**

```python
def test_equivalent_action_reuses_active_intent():
    first = step(_state(), _break_observation("market-1"))
    pending = first.state_if_unfilled
    repeated = step(pending, _break_observation("market-1"))
    assert repeated.action_intent_token == first.action_intent_token
    assert repeated.semantic_transition is False

def test_exit_full_supersedes_pending_partial_once():
    partial = step(_state(), _low_break("market-1")).state_if_unfilled
    terminal = step(partial, _high_break("market-2"))
    assert terminal.action is V6Action.EXIT_FULL
    assert terminal.supersedes_token == partial.active_intent.token
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -k 'intent or supersedes' -q`

Expected: FAIL because each observation creates an independent action token.

- [ ] **Step 3: Implement deterministic merge rules**

Use priority `HOLD=0`, `DERISK=1`, `EXIT_FULL=2`; for equal action priority, a larger fraction is
stronger. Equivalent/weaker candidates retain the active token. A stronger candidate creates one new
token and names `supersedes_token`. HOLD never clears an active intent. Filled successors clear the
intent and consume the tranche; unfilled successors retain it and consume neither.

- [ ] **Step 4: Run engine tests**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py tests/test_memecoin_v6_engine.py
git commit -m "feat: persist one mergeable v6 action intent"
```

### Task 5: Migrate the isolated ledger to compact V2 decisions

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py:40-205,286-370,620-905`
- Test: `tests/test_memecoin_v6_paper_db.py`

**Interfaces:**
- Consumes: `state_document`, `state_from_document`, V1 decisions, and action intents.
- Produces: schema user version `2`, `append_transition(conn, position_id, observation, decision)`,
  `active_intent(conn, position_id)`, `complete_intent_fill(...)`, and
  `record_intent_unfilled(...)`.

- [ ] **Step 1: Write schema and compact-storage tests**

```python
def test_schema_v2_has_compact_decision_and_intent_columns(tmp_path):
    conn = _conn(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    cols = {row[1] for row in conn.execute("PRAGMA table_info(v6_paper_decisions)")}
    assert {"state_before_hash", "state_after_hash", "state_delta_json", "semantic_transition"} <= cols
    assert "state_if_filled_json" not in cols

def test_repeated_semantic_noop_does_not_append_decision(tmp_path):
    conn, position, obs = _opened_position(tmp_path)
    first = step(position.engine_state, obs)
    db.append_transition(conn, position.entry_id, obs, first)
    current = db.open_positions(conn)[0]
    second = step(current.engine_state, obs)
    db.append_transition(conn, position.entry_id, obs, second)
    assert second.semantic_transition is False
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_decisions").fetchone()[0] == 1
```

- [ ] **Step 2: Run DB tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q`

Expected: FAIL because schema V1 stores three full state JSON documents per decision.

- [ ] **Step 3: Implement an atomic idempotent migration**

Create schema V2 in a transaction. Because V6 is unactivated, reject any legacy database containing
an open V0-policy position before migration. Copy closed/censored audit rows into the compact table,
preserving observations, decisions, fills, and fingerprints. Set `PRAGMA user_version=2` only after
all invariants pass.

The compact decision row stores hashes plus an exact JSON merge patch from before to after. Store
filled/unfilled successor documents only in `v6_paper_action_intents`, because only actionable
transitions need a post-network recovery fork. Enforce one active intent with:

```sql
CREATE UNIQUE INDEX idx_v6_one_active_intent
ON v6_paper_action_intents(position_id) WHERE status='active';
```

- [ ] **Step 4: Implement atomic transition persistence and reconstruction**

`append_transition` must insert a new semantic observation, compact decision, state update, and
intent/supersession in one `BEGIN IMMEDIATE` transaction. Exact redelivery returns the existing row.
Semantic no-op redelivery writes nothing. Reconstruction applies ordered state deltas, verifies every
before/after hash, and cross-checks remaining quantity against fills.

- [ ] **Step 5: Run DB tests, schema validation, and hygiene**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Run: `git diff --check`

Expected: PASS; no unknown V6 columns and no hygiene errors.

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin_v6_paper.py tests/test_memecoin_v6_paper_db.py
git commit -m "refactor: compact v6 transition persistence"
```

### Task 6: Integrate intents and semantic no-ops into the runner

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_runner.py:187-375`
- Modify: `scripts/memecoin_v6_paper_worker.py:24-190`
- Test: `tests/test_memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: `append_transition`, `active_intent`, `complete_intent_fill`, and
  `record_intent_unfilled` from Task 5.
- Produces: `advance_position(...) -> AdvanceResult`; existing `run_cycle` remains a compatibility
  recovery supervisor for Plan C.

- [ ] **Step 1: Write restart, no-op, and supersession tests**

```python
def test_same_evidence_across_cycles_writes_one_transition_and_one_quote(tmp_path):
    calls = []
    for _ in range(20):
        run_cycle(src, v6, now_fn=clock.tick_100ms, observation_fn=same_market_observation,
                  exit_quote_fn=lambda *a: calls.append(a) or unavailable())
    assert _count(v6, "v6_paper_decisions") == 1
    assert len(calls) == 1

def test_restart_recovers_single_active_intent_before_new_fill(tmp_path):
    _persist_pending_partial(v6)
    run_cycle(src, v6, observation_fn=new_terminal_evidence, exit_quote_fn=successful_quote)
    assert _action_fill_count(v6) == 1
    assert _active_intent_count(v6) == 0
```

- [ ] **Step 2: Run runner/worker tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py -q`

Expected: FAIL because the runner drains a decision by blocking observation and appends each wake.

- [ ] **Step 3: Implement `advance_position`**

Always pass new semantic evidence through V6 even with an active unfilled intent. Persist transition
and intent merge first. Quote only the one active retry-eligible intent. Apply a modeled fill only
when the token remains current after the quote returns. If it was superseded, discard the stale quote
without a fill.

Keep `run_cycle` ordering for crash recovery and new entries. It becomes a bounded supervisor rather
than the normal fast clock.

- [ ] **Step 4: Validate startup schema and policy identity**

Require schema user version 2 and `v6-position-engine-v1` policy hash. Fail closed on any open legacy
position. Preserve `--smoke-no-rpc --once` behavior.

- [ ] **Step 5: Run the kernel integration suite**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_observations.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py -q`

Run: `python3 scripts/memecoin_v6_paper_worker.py --smoke-no-rpc --once`

Expected: all tests pass and smoke exits 0 with `V6 PAPER — NO FUNDS`.

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_v6_paper_runner.py scripts/memecoin_v6_paper_worker.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py
git commit -m "fix: advance v6 positions by semantic evidence"
```

### Task 7: Kernel performance and boundary gate

**Files:**
- Create: `tests/test_memecoin_v6_engine_performance.py`
- Modify: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: complete Plan A public/runtime interfaces.
- Produces: repeatable warm local latency and import-boundary evidence.

- [ ] **Step 1: Add deterministic performance and forbidden-import tests**

Warm the engine once, execute 10,000 distinct in-memory transitions, and assert p99 below 5 ms using
`time.perf_counter_ns`. Mark the test `@pytest.mark.performance` only if the repository already
excludes performance tests by default; otherwise keep it in the focused file. Parse the AST/import
graph and assert the engine, V6 ledger, runner, and worker cannot reach funded signing, approval,
broadcast, reservation, or `memecoin_real_runner` modules.

- [ ] **Step 2: Run the new gate twice**

Run: `python3 -m pytest tests/test_memecoin_v6_engine_performance.py -q`

Run the same command again.

Expected: both runs pass; report measured p50/p95/p99 in assertion failure text.

- [ ] **Step 3: Run Plan A regression gate**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_observations.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_v6_engine_performance.py -q`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Run: `git diff --check`

Expected: all pass. Do not activate the worker.

- [ ] **Step 4: Commit**

```bash
git add tests/test_memecoin_v6_engine_performance.py tests/test_memecoin_v6_paper_worker.py
git commit -m "test: gate v6 kernel latency and isolation"
```
