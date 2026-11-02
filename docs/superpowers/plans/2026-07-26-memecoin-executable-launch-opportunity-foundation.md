# Memecoin Executable Launch Opportunity Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the capture-only, offline foundation for the Executable Launch Opportunity Engine: canonical event types, an isolated immutable ledger, recoverable source frontiers, deterministic landmark materialization, and executable competing-outcome labels.

**Architecture:** Add a new `sniper.research.memecoin_opportunity` package with no dependency on funded execution, V6, live RPC, web, or model code. Pure domain functions own identity, frontier transitions, landmark crossing, and first-passage evaluation. A small SQLite adapter persists immutable facts and mutable recovery cursors in `data/memecoin_opportunity.db`. Later source adapters and model workers consume these contracts; they are not part of this plan.

**Tech Stack:** Python 3, standard-library `dataclasses`, `enum`, `decimal`, `hashlib`, `json`, `sqlite3`, and `pytest`.

## Global Constraints

- Implement in an isolated worktree and one logical commit per task.
- Use RED-first TDD: add the specified failing test, confirm the expected failure, implement the minimum code, then rerun the focused test.
- Do not call live APIs, RPC endpoints, price-history endpoints, or CLOB endpoints.
- Do not write production databases, restart services, alter funded configuration, or touch `.runtime/memecoin/current`.
- The new package must not import `sniper.execution`, funded runner modules, signer/sender modules, or V6 services.
- Default behavior is capture-only. This phase must not select, enter, size, or exit paper or funded positions.
- Use Python integers for atomic amounts and basis points. Persist potentially unbounded chain atomics as canonical base-10 `TEXT`; do not persist floats for money, quantity, progress, return, or barriers.
- Normalize EVM addresses to lowercase at every identity boundary. Preserve Solana base58 identity exactly.
- Persist timestamps as aware UTC ISO-8601 strings ending in `Z`. Reject naive timestamps.
- Exact duplicate writes are idempotent. Reusing an immutable identity with different content raises `ImmutableConflict`.
- Raw events, canonical events, landmarks, counterfactual entries, and outcomes are append-only. Corrections append new facts linked to the superseded fact.
- A source frontier advances only across a contiguous, decoded, durable range. Any enumeration or decode gap holds the frontier and records a recoverable resume cursor.
- Data-source failure is not an economic exit failure. Missing intervals censor labels; they never fabricate `LOSS_FIRST` or `PERSISTENT_EXIT_FAILURE_FIRST`.
- Migration ends the pre-migration episode and preserves the last executable return. It is not automatically a loss.
- Primary v0 barriers are `+1200 bps`, `-800 bps`, and `60_000 ms`; callers may inject alternatives for sensitivity analysis.
- The authoritative design is `docs/superpowers/specs/2026-07-26-memecoin-executable-launch-opportunity-engine-design.md`.

---

## Scope Boundary

This plan delivers these contracts:

```text
canonical domain types
        ↓
append-only opportunity ledger
        ↓
recoverable source frontier state
        ↓
deterministic time/progress landmarks
        ↓
executable first-passage outcome labels
```

Deferred to later plans:

```text
Pump.fun/Bags Solana live adapters and decoders
Bags Robinhood 4663 live adapter and decoder
curve-state reducers and entity clustering
remote quote providers and self-impact simulators
capture worker, settings, watchdog, and health endpoints
model training, calibration, promotion gates, and registry
Radar API, WebSocket/SSE stream, and UI
V6 paper-position enrollment or any funded integration
```

---

### Task 1: Define canonical identities and immutable domain types

**Files:**

- Create: `sniper/research/memecoin_opportunity/__init__.py`
- Create: `sniper/research/memecoin_opportunity/identity.py`
- Create: `sniper/research/memecoin_opportunity/types.py`
- Test: `tests/test_memecoin_opportunity_types.py`

**Interfaces:**

```python
# sniper/research/memecoin_opportunity/identity.py
def canonical_json(value: object) -> str: ...
def content_fingerprint(value: object) -> str: ...
def normalize_identity(network_id: str, value: str) -> str: ...
def require_utc_iso(value: str) -> str: ...

# sniper/research/memecoin_opportunity/types.py
class NetworkId(str, Enum):
    SOLANA = "solana"
    ROBINHOOD_4663 = "4663"

class VenueId(str, Enum):
    PUMPFUN_SOLANA = "pumpfun_solana"
    BAGS_SOLANA = "bags_solana"
    BAGS_ROBINHOOD_4663 = "bags_robinhood_4663"

class ConfirmationState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    ORPHANED = "orphaned"

class LifecycleState(str, Enum):
    PRE_MIGRATION = "pre_migration"
    MIGRATED = "migrated"
    UNRESOLVED = "unresolved"

class EventKind(str, Enum):
    LAUNCH = "launch"
    BUY = "buy"
    SELL = "sell"
    TRANSFER = "transfer"
    FEE_SHARE_CHANGE = "fee_share_change"
    MIGRATION = "migration"
    CORRECTION = "correction"

@dataclass(frozen=True)
class ChainOrder:
    block_or_slot: int
    transaction_index: int | None
    event_index: int
    block_hash: str | None

@dataclass(frozen=True)
class RawEvent:
    raw_event_id: str
    network_id: NetworkId
    venue_id: VenueId
    source_key: str
    source_provider: str
    source_cursor: str
    token_id: str
    transaction_id: str
    chain_order: ChainOrder
    chain_timestamp: str
    received_at: str
    confirmation: ConfirmationState
    raw_payload_json: str
    raw_payload_hash: str

@dataclass(frozen=True)
class CanonicalEvent:
    canonical_event_id: str
    raw_event_id: str
    launch_id: str
    network_id: NetworkId
    venue_id: VenueId
    token_id: str
    lifecycle: LifecycleState
    event_kind: EventKind
    transaction_id: str
    chain_order: ChainOrder
    chain_timestamp: str
    received_at: str
    decoded_at: str
    state_applied_at: str
    confirmation: ConfirmationState
    actor_id: str | None
    quote_delta_atomic: int | None
    token_delta_atomic: int | None
    progress_bps: int | None
    creator_linked: bool | None
    decoder_version: str
    correction_of: str | None = None
```

- [ ] **Step 1: Write validation and determinism tests**

Create tests proving:

```python
def test_evm_identity_is_lowercased_but_solana_is_preserved():
    assert normalize_identity("4663", "0xAbCd") == "0xabcd"
    assert normalize_identity("solana", "AbCd") == "AbCd"

def test_canonical_json_and_fingerprint_ignore_dict_insertion_order():
    left = {"b": 2, "a": 1}
    right = {"a": 1, "b": 2}
    assert canonical_json(left) == canonical_json(right)
    assert content_fingerprint(left) == content_fingerprint(right)

@pytest.mark.parametrize("value", ["2026-07-26T10:00:00", "bad"])
def test_require_utc_iso_rejects_naive_or_invalid_values(value):
    with pytest.raises(ValueError):
        require_utc_iso(value)

def test_raw_event_rejects_payload_hash_mismatch():
    with pytest.raises(ValueError, match="raw_payload_hash"):
        RawEvent(..., raw_payload_json='{"x":1}', raw_payload_hash="wrong")

def test_canonical_event_rejects_cross_venue_network_pair():
    with pytest.raises(ValueError, match="venue_id"):
        CanonicalEvent(
            ...,
            network_id=NetworkId.ROBINHOOD_4663,
            venue_id=VenueId.PUMPFUN_SOLANA,
        )
```

Also cover negative ordering fields, progress outside `0..10_000`, blank IDs, invalid atomic deltas, and a correction without `correction_of`.

- [ ] **Step 2: Run the test and confirm RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_types.py -q
```

Expected: collection fails because `sniper.research.memecoin_opportunity` does not exist.

- [ ] **Step 3: Implement deterministic identity helpers**

Implementation requirements:

- `canonical_json()` uses sorted keys, compact separators, UTF-8 text, and rejects NaN/Infinity.
- `content_fingerprint()` returns lowercase SHA-256 hex over canonical UTF-8 JSON.
- `require_utc_iso()` accepts aware UTC only and canonicalizes `+00:00` to `Z`.
- `normalize_identity()` lowercases Robinhood `0x` identities and leaves Solana strings untouched.
- Dataclass `__post_init__` methods validate invariants and normalize immutable fields with `object.__setattr__`.
- `RawEvent` recomputes SHA-256 over the exact `raw_payload_json` bytes and rejects mismatch.
- Venue/network compatibility is explicit, not inferred from string prefixes.
- Canonical event observability timestamps must satisfy `decoded_at >= received_at` and `state_applied_at >= decoded_at`. Do not compare chain time to corrected host time for ordering.
- Potentially unbounded atomic values remain Python `int` in domain types and serialize to canonical base-10 strings at the SQLite boundary.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_opportunity_types.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add sniper/research/memecoin_opportunity tests/test_memecoin_opportunity_types.py
git commit -m "feat: define memecoin opportunity domain types"
```

---

### Task 2: Create the isolated append-only SQLite ledger

**Files:**

- Create: `sniper/research/memecoin_opportunity/store.py`
- Test: `tests/test_memecoin_opportunity_store.py`

**Interfaces:**

```python
DEFAULT_DB_PATH = "data/memecoin_opportunity.db"

class ImmutableConflict(RuntimeError): ...
class MissingOpportunityDatabase(RuntimeError): ...

def opportunity_db_path(path: str | Path | None = None) -> Path: ...
def connect(path: str | Path | None = None) -> sqlite3.Connection: ...
def connect_existing(path: str | Path | None = None) -> sqlite3.Connection: ...
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]: ...

def append_raw_event(conn: sqlite3.Connection, event: RawEvent) -> bool: ...
def append_canonical_event(conn: sqlite3.Connection, event: CanonicalEvent) -> bool: ...
def upsert_launch_identity(conn: sqlite3.Connection, launch: LaunchIdentity) -> bool: ...
```

`append_*()` returns `True` for a new row, `False` for an exact idempotent duplicate, and raises `ImmutableConflict` when the same primary identity has a different fingerprint.

**Phase-1 schema:**

```sql
CREATE TABLE op_raw_events (
    raw_event_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_cursor TEXT NOT NULL,
    token_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    block_or_slot INTEGER NOT NULL,
    transaction_index INTEGER,
    event_index INTEGER NOT NULL,
    block_hash TEXT,
    chain_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE op_canonical_events (
    canonical_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL REFERENCES op_raw_events(raw_event_id),
    launch_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    block_or_slot INTEGER NOT NULL,
    transaction_index INTEGER,
    event_index INTEGER NOT NULL,
    block_hash TEXT,
    chain_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    decoded_at TEXT NOT NULL,
    state_applied_at TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    actor_id TEXT,
    quote_delta_atomic TEXT,
    token_delta_atomic TEXT,
    progress_bps INTEGER,
    creator_linked INTEGER,
    decoder_version TEXT NOT NULL,
    correction_of TEXT REFERENCES op_canonical_events(canonical_event_id),
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE op_launches (
    launch_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    creator_id TEXT,
    launch_event_id TEXT NOT NULL REFERENCES op_canonical_events(canonical_event_id),
    launched_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(network_id, venue_id, token_id)
);
```

Create indexes on canonical launch order, canonical token order, and `(source_key, source_cursor)`. Add `BEFORE UPDATE` and `BEFORE DELETE` abort triggers to all three tables.

- [ ] **Step 1: Write store lifecycle and immutability tests**

Tests must prove:

```python
def test_connect_initializes_an_isolated_database(tmp_path): ...
def test_connect_existing_never_creates_a_missing_database(tmp_path): ...
def test_append_raw_event_is_exactly_idempotent(tmp_path): ...
def test_same_raw_event_id_with_different_content_conflicts(tmp_path): ...
def test_canonical_event_requires_existing_raw_event(tmp_path): ...
def test_launch_identity_requires_existing_launch_event(tmp_path): ...
def test_update_and_delete_of_immutable_facts_are_rejected(tmp_path): ...
def test_correction_appends_and_preserves_original_event(tmp_path): ...
def test_wal_and_foreign_keys_are_enabled(tmp_path): ...
```

Use only `tmp_path`; never open the default database in tests.

- [ ] **Step 2: Run the test and confirm RED**

```bash
python3 -m pytest tests/test_memecoin_opportunity_store.py -q
```

Expected: import failure for `memecoin_opportunity.store`.

- [ ] **Step 3: Implement schema ownership and immutable inserts**

Implementation requirements:

- `connect()` creates only its explicit target and initializes schema under `BEGIN IMMEDIATE`.
- `connect_existing()` checks existence, opens SQLite URI `mode=rw`, and does not initialize or migrate.
- Both connections set `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, and a bounded busy timeout.
- Writer connection uses WAL; no helper changes journal mode on a read-only/existing inspection path.
- Store a single integer schema version with explicit failure on a newer/unknown version.
- `inserted_at` comes from an injected clock whose production default wraps `sniper.util.clock.real_utc_now_iso()`, then passes through `require_utc_iso()`; never use raw host time or SQLite local time.
- Serialize dataclass facts to a canonical dict, compute `row_fingerprint`, then use `INSERT OR IGNORE` followed by exact fingerprint comparison.
- Roll back every failed transaction. Do not swallow `sqlite3.IntegrityError` other than to wrap it with a precise domain error.

- [ ] **Step 4: Run focused tests and schema validation**

```bash
python3 -m pytest tests/test_memecoin_opportunity_store.py -q
python3 scripts/validate_schema_refs.py
```

Expected: tests pass; schema reference validation passes.

- [ ] **Step 5: Commit Task 2**

```bash
git add sniper/research/memecoin_opportunity/store.py tests/test_memecoin_opportunity_store.py
git commit -m "feat: add immutable opportunity event ledger"
```

---

### Task 3: Add recoverable source frontiers and explicit data gaps

**Files:**

- Create: `sniper/research/memecoin_opportunity/frontier.py`
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Test: `tests/test_memecoin_opportunity_frontier.py`

**Interfaces:**

```python
class GapKind(str, Enum):
    ENUMERATION_BOUND = "enumeration_bound"
    FETCH_FAILURE = "fetch_failure"
    DECODE_FAILURE = "decode_failure"
    ORDERING_DISCONTINUITY = "ordering_discontinuity"
    REORG = "reorg"

@dataclass(frozen=True)
class SourceFrontier:
    source_key: str
    durable_cursor: str | None
    resume_cursor: str | None
    latest_observed_cursor: str | None
    has_open_gap: bool
    version: int
    updated_at: str

@dataclass(frozen=True)
class FrontierBatch:
    source_key: str
    expected_version: int
    previous_durable_cursor: str | None
    newest_observed_cursor: str | None
    contiguous_durable_cursor: str | None
    resume_cursor: str | None
    reached_previous_frontier: bool
    enumeration_complete: bool
    decode_complete: bool
    observed_at: str
    gap_kind: GapKind | None = None
    gap_detail: str | None = None

def decide_frontier(current: SourceFrontier | None, batch: FrontierBatch) -> FrontierDecision: ...
def load_frontier(conn: sqlite3.Connection, source_key: str) -> SourceFrontier | None: ...
def apply_frontier_batch(conn: sqlite3.Connection, batch: FrontierBatch) -> SourceFrontier: ...
def resolve_gap(conn: sqlite3.Connection, gap_id: str, *, resolved_at: str) -> None: ...
```

Add mutable recovery tables:

```sql
CREATE TABLE op_source_frontiers (
    source_key TEXT PRIMARY KEY,
    durable_cursor TEXT,
    resume_cursor TEXT,
    latest_observed_cursor TEXT,
    has_open_gap INTEGER NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE op_data_gaps (
    gap_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES op_source_frontiers(source_key),
    gap_kind TEXT NOT NULL,
    previous_durable_cursor TEXT,
    resume_cursor TEXT,
    newest_observed_cursor TEXT,
    detail TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    row_fingerprint TEXT NOT NULL
);
```

`op_source_frontiers` is deliberately mutable recovery state. `op_data_gaps` is append-only except for one guarded `NULL -> resolved_at` transition.

- [ ] **Step 1: Write pure transition tests first**

Cover these exact cases:

```python
def test_complete_contiguous_batch_advances_frontier(): ...
def test_enumeration_bound_holds_frontier_and_sets_resume_cursor(): ...
def test_decode_failure_holds_frontier_before_failed_event(): ...
def test_newer_observations_do_not_skip_an_existing_gap(): ...
def test_recovery_batch_advances_only_after_reaching_old_frontier(): ...
def test_stale_frontier_version_is_rejected(): ...
def test_gap_resolution_does_not_itself_advance_frontier(): ...
def test_frontier_and_gap_write_commit_atomically(): ...
```

The load-bearing assertion is:

```python
assert after_gap.durable_cursor == before.durable_cursor
assert after_gap.resume_cursor == batch.resume_cursor
assert after_gap.has_open_gap is True
```

- [ ] **Step 2: Run the frontier test and confirm RED**

```bash
python3 -m pytest tests/test_memecoin_opportunity_frontier.py -q
```

Expected: import failure for `memecoin_opportunity.frontier` or missing tables.

- [ ] **Step 3: Implement pure frontier decisions**

Rules:

1. Reject `expected_version` mismatch before any write.
2. Advance only when `enumeration_complete`, `decode_complete`, and `reached_previous_frontier` are all true and no open earlier gap remains.
3. On any incomplete batch, keep `durable_cursor` unchanged, persist `resume_cursor`, set `has_open_gap`, and append a deterministic gap fact.
4. Replaying the same incomplete batch is idempotent; it must not create duplicate gaps.
5. A recovered interval may clear `has_open_gap` only when every earlier open gap is resolved in the same transaction and the contiguous cursor covers the interval.
6. A caller cannot set a durable cursor that is absent from the durable raw ledger for the same `source_key`. Check `(source_key, source_cursor)` in `op_raw_events`.

- [ ] **Step 4: Implement atomic store persistence**

- Use optimistic `version` matching in the `UPDATE` predicate.
- Generate deterministic `gap_id` from `source_key`, kind, previous durable cursor, and resume cursor.
- Restrict `op_data_gaps` updates with a trigger: only `resolved_at` may change, only from `NULL` to a valid UTC value, and all other columns must be identical.
- Expose open gaps through a read helper; do not hide or auto-expire them.

- [ ] **Step 5: Run focused and adjacent tape tests**

```bash
python3 -m pytest tests/test_memecoin_opportunity_frontier.py -q
python3 -m pytest tests/test_memecoin_solana_tape.py -q
```

Expected: new tests pass; existing Solana tape recovery behavior remains unchanged.

- [ ] **Step 6: Commit Task 3**

```bash
git add sniper/research/memecoin_opportunity/frontier.py sniper/research/memecoin_opportunity/store.py tests/test_memecoin_opportunity_frontier.py
git commit -m "feat: persist recoverable opportunity frontiers"
```

---

### Task 4: Materialize deterministic time and progress landmarks

**Files:**

- Create: `sniper/research/memecoin_opportunity/landmarks.py`
- Modify: `sniper/research/memecoin_opportunity/types.py`
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Test: `tests/test_memecoin_opportunity_landmarks.py`

**Interfaces:**

```python
TIME_LANDMARKS_MS = (1_000, 2_000, 5_000, 10_000, 20_000, 30_000, 60_000)
PROGRESS_LANDMARKS_BPS = (200, 500, 1_000, 2_000, 4_000, 6_000, 8_000)

class LandmarkStatus(str, Enum):
    TENTATIVE = "tentative"
    CANONICAL = "canonical"
    ORPHANED = "orphaned"
    INELIGIBLE = "ineligible"

@dataclass(frozen=True)
class LaunchSnapshot:
    launch_id: str
    observed_event_id: str
    observed_at: str
    age_ms: int
    progress_bps: int
    confirmation: ConfirmationState
    state_fingerprint: str
    source_gap_open: bool

@dataclass(frozen=True)
class LandmarkState:
    landmark_id: str
    launch_id: str
    observed_event_id: str
    observed_at: str
    age_ms: int
    progress_bps: int
    crossed_time_ms: tuple[int, ...]
    crossed_progress_bps: tuple[int, ...]
    status: LandmarkStatus
    state_fingerprint: str
    supersedes_landmark_id: str | None = None
    ineligible_reason: str | None = None

def crossed_landmarks(
    previous: LaunchSnapshot | None,
    current: LaunchSnapshot,
) -> tuple[tuple[int, ...], tuple[int, ...]]: ...

def materialize_landmarks(
    previous: LaunchSnapshot | None,
    current: LaunchSnapshot,
) -> LandmarkState | None: ...

def append_landmark(conn: sqlite3.Connection, landmark: LandmarkState) -> bool: ...
```

Add:

```sql
CREATE TABLE op_landmark_states (
    landmark_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL REFERENCES op_launches(launch_id),
    observed_event_id TEXT NOT NULL REFERENCES op_canonical_events(canonical_event_id),
    observed_at TEXT NOT NULL,
    age_ms INTEGER NOT NULL,
    progress_bps INTEGER NOT NULL,
    crossed_time_ms_json TEXT NOT NULL,
    crossed_progress_bps_json TEXT NOT NULL,
    status TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    supersedes_landmark_id TEXT REFERENCES op_landmark_states(landmark_id),
    ineligible_reason TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE UNIQUE INDEX op_landmark_observed_state
ON op_landmark_states(launch_id, observed_event_id, status, state_fingerprint);
```

- [ ] **Step 1: Write crossing, eligibility, and correction tests**

Cover:

```python
def test_first_snapshot_tags_every_already_crossed_threshold_in_one_state(): ...
def test_one_event_crossing_multiple_time_and_progress_thresholds_emits_one_state(): ...
def test_age_jump_tags_all_crossed_time_thresholds_in_order(): ...
def test_progress_jump_tags_all_crossed_progress_thresholds_in_order(): ...
def test_snapshot_without_a_new_crossing_emits_no_state(): ...
def test_progress_regression_is_rejected_without_reorg_correction(): ...
def test_open_source_gap_emits_ineligible_landmark_with_reason(): ...
def test_tentative_landmark_can_be_superseded_by_canonical_landmark(): ...
def test_orphaned_tentative_landmark_is_preserved_for_chain_reorg_label(): ...
def test_replayed_materialization_is_idempotent_in_store(): ...
```

Both tag tuples are strictly ascending. One observed state creates at most one row and therefore at most one hypothetical entry, even when it crosses several thresholds.

- [ ] **Step 2: Run landmark tests and confirm RED**

```bash
python3 -m pytest tests/test_memecoin_opportunity_landmarks.py -q
```

Expected: import failure for `memecoin_opportunity.landmarks` or missing schema.

- [ ] **Step 3: Implement pure crossing and status rules**

Rules:

- First snapshot may tag all thresholds at or below its point-in-time state; this records detection delay rather than pretending the system saw earlier states.
- A state uses the first observed event that crosses one or more new thresholds.
- If one event crosses several time/progress thresholds, persist one state row with all new tags. Never create correlated pseudo-rows or multiple entries at the same observable state.
- Tentative chain state produces `TENTATIVE`; confirmed/finalized produces `CANONICAL`.
- Any open source gap produces `INELIGIBLE` with `source_gap_open`; do not silently omit the row.
- Orphaning appends an `ORPHANED` successor linked through `supersedes_landmark_id`.
- Canonical replacement appends a new row linked to the tentative row. It never edits the tentative fact.
- `landmark_id` is a deterministic fingerprint of launch, observed event, ordered tag tuples, status, and state fingerprint.
- A state fingerprint is opaque to this layer. The future state reducer owns its contents.

- [ ] **Step 4: Implement append-only landmark persistence**

- Add immutable update/delete triggers.
- Validate that `observed_event_id` belongs to the same launch.
- Reject `CANONICAL` landmarks rooted in tentative/orphaned events.
- `TENTATIVE` and `CANONICAL` rows are entry-eligible when all point-in-time requirements are satisfied. `INELIGIBLE` and `ORPHANED` rows are never entry-eligible.
- A tentative entry becomes outcome-ready only after its landmark has a canonical successor or an orphaning successor. Canonical successors are labelable; orphaning produces `CHAIN_REORG`.
- Add `list_entry_eligible_landmarks(conn, launch_id)` and `list_outcome_ready_entries(conn, launch_id)`, both ordered by observed chain order rather than insertion time.

- [ ] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_memecoin_opportunity_landmarks.py tests/test_memecoin_opportunity_store.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add sniper/research/memecoin_opportunity/landmarks.py sniper/research/memecoin_opportunity/types.py sniper/research/memecoin_opportunity/store.py tests/test_memecoin_opportunity_landmarks.py
git commit -m "feat: materialize opportunity landmarks"
```

---

### Task 5: Evaluate executable competing outcomes without fabricating data

**Files:**

- Create: `sniper/research/memecoin_opportunity/outcomes.py`
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Test: `tests/test_memecoin_opportunity_outcomes.py`

**Interfaces:**

```python
class ExitObservationKind(str, Enum):
    EXECUTABLE = "executable"
    DETERMINISTIC_FAILURE = "deterministic_failure"
    TRANSIENT_FAILURE = "transient_failure"

class OutcomeKind(str, Enum):
    PROFIT_FIRST = "profit_first"
    LOSS_FIRST = "loss_first"
    PERSISTENT_EXIT_FAILURE_FIRST = "persistent_exit_failure_first"
    MIGRATION_FIRST = "migration_first"
    TIMEOUT = "timeout"
    CENSORED_DATA_GAP = "censored_data_gap"
    CHAIN_REORG = "chain_reorg"

@dataclass(frozen=True)
class CounterfactualEntry:
    entry_id: str
    landmark_id: str
    clip_usd_atomic: int
    entry_latency_ms: int
    entry_observed_at: str
    entry_effective_at: str
    acquired_token_atomic: int
    entry_cost_usd_atomic: int
    entry_price_impact_bps: int
    quote_provider: str
    quote_version: str

@dataclass(frozen=True)
class ExecutableObservation:
    observation_id: str
    entry_id: str
    elapsed_ms: int
    canonical_state_id: str
    observed_at: str
    exit_kind: ExitObservationKind
    net_return_bps: int | None
    net_proceeds_usd_atomic: int | None
    source_healthy: bool
    migration: bool = False
    orphaned: bool = False

@dataclass(frozen=True)
class CompetingOutcome:
    outcome_id: str
    entry_id: str
    outcome_kind: OutcomeKind
    elapsed_ms: int
    terminal_observation_id: str
    terminal_net_return_bps: int | None
    profit_barrier_bps: int
    loss_barrier_bps: int
    horizon_ms: int
    last_executable_observation_id: str | None
    last_executable_return_bps: int | None

def evaluate_competing_outcome(
    entry: CounterfactualEntry,
    observations: Sequence[ExecutableObservation],
    *,
    profit_barrier_bps: int = 1_200,
    loss_barrier_bps: int = -800,
    horizon_ms: int = 60_000,
    transient_failure_states: int = 2,
) -> CompetingOutcome: ...

def append_counterfactual_entry(conn, entry: CounterfactualEntry) -> bool: ...
def append_competing_outcome(conn, outcome: CompetingOutcome) -> bool: ...
```

Add immutable tables:

```sql
CREATE TABLE op_counterfactual_entries (
    entry_id TEXT PRIMARY KEY,
    landmark_id TEXT NOT NULL REFERENCES op_landmark_states(landmark_id),
    clip_usd_atomic TEXT NOT NULL,
    entry_latency_ms INTEGER NOT NULL,
    entry_observed_at TEXT NOT NULL,
    entry_effective_at TEXT NOT NULL,
    acquired_token_atomic TEXT NOT NULL,
    entry_cost_usd_atomic TEXT NOT NULL,
    entry_price_impact_bps INTEGER NOT NULL,
    quote_provider TEXT NOT NULL,
    quote_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE op_competing_outcomes (
    outcome_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES op_counterfactual_entries(entry_id),
    outcome_kind TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    terminal_observation_id TEXT NOT NULL,
    terminal_net_return_bps INTEGER,
    profit_barrier_bps INTEGER NOT NULL,
    loss_barrier_bps INTEGER NOT NULL,
    horizon_ms INTEGER NOT NULL,
    last_executable_observation_id TEXT,
    last_executable_return_bps INTEGER,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(entry_id, profit_barrier_bps, loss_barrier_bps, horizon_ms)
);
```

- [ ] **Step 1: Write first-passage tests**

Cover these exact semantics:

```python
def test_profit_crossed_before_loss_is_profit_first(): ...
def test_loss_crossed_before_profit_is_loss_first(): ...
def test_input_order_does_not_change_chronological_result(): ...
def test_duplicate_elapsed_state_is_rejected_as_ambiguous(): ...
def test_deterministic_route_failure_is_terminal_immediately(): ...
def test_one_transient_failure_then_recovery_is_not_exit_failure(): ...
def test_two_distinct_healthy_transient_failures_are_persistent_exit_failure(): ...
def test_unhealthy_source_censors_instead_of_calling_exit_failure(): ...
def test_gap_between_executable_observations_censors_first_passage(): ...
def test_migration_preserves_last_executable_return(): ...
def test_migration_without_prior_executable_quote_has_none_return(): ...
def test_orphaned_state_returns_chain_reorg(): ...
def test_no_barrier_by_horizon_is_timeout(): ...
def test_observations_after_horizon_do_not_change_timeout(): ...
def test_default_barriers_are_plus_1200_minus_800_and_60_seconds(): ...
def test_alternative_barriers_produce_distinct_outcome_identity(): ...
```

For a missing interval, represent the first unavailable canonical state as `source_healthy=False`; the evaluator must stop there because barrier order inside the gap is unknowable.

- [ ] **Step 2: Run outcome tests and confirm RED**

```bash
python3 -m pytest tests/test_memecoin_opportunity_outcomes.py -q
```

Expected: import failure for `memecoin_opportunity.outcomes`.

- [ ] **Step 3: Implement chronological first-passage evaluation**

Evaluation order at each canonical observation:

1. `orphaned` -> `CHAIN_REORG`.
2. unhealthy/missing source -> `CENSORED_DATA_GAP`.
3. `elapsed_ms > horizon_ms` -> `TIMEOUT` at the horizon using the last executable value; the late observation cannot win.
4. `migration` -> `MIGRATION_FIRST`, carrying the prior or current executable value when present.
5. deterministic exit failure -> `PERSISTENT_EXIT_FAILURE_FIRST`.
6. repeated transient failures on distinct canonical states while source healthy -> `PERSISTENT_EXIT_FAILURE_FIRST`.
7. executable `net_return_bps >= profit_barrier_bps` -> `PROFIT_FIRST`.
8. executable `net_return_bps <= loss_barrier_bps` -> `LOSS_FIRST`.
9. an observation exactly at the horizon with no earlier event -> `TIMEOUT` after testing outcomes 4-8, so a barrier reached at `60_000 ms` counts.

Additional rules:

- Sort by `(elapsed_ms, canonical_state_id)` but reject multiple observations for one canonical state or ambiguous different states at the same elapsed time.
- Require a non-empty observation sequence and proof of healthy canonical coverage through the horizon for `TIMEOUT`. If the last healthy observation is before the horizon and no later coverage marker exists, return `CENSORED_DATA_GAP` rather than assuming nothing happened.
- Reject negative elapsed time, non-positive clip/acquired quantity, inverted barriers, and observations for another `entry_id`.
- An executable observation requires both net return and net proceeds.
- A failure observation must not carry a fabricated return or proceeds.
- The evaluator consumes already self-impact-aware observations. It must not infer quotes from market price or pool midpoint.
- Outcome identity includes entry identity, barrier set, horizon, terminal observation, and evaluator version.

- [ ] **Step 4: Implement immutable entry/outcome persistence**

- `TENTATIVE` and `CANONICAL` entry-eligible landmarks may receive counterfactual entries because the live fast path can act on tentative state. `INELIGIBLE` and `ORPHANED` landmarks may not.
- A tentative entry may receive a final outcome only after canonical reconciliation. Its permitted results are the canonically evaluated economic outcome or `CHAIN_REORG` if its observed state was orphaned.
- Reject entry timestamps before the landmark timestamp and effective timestamps before observation timestamps.
- Store the exact configured clip and latency; no implicit defaults in persistence.
- Permit multiple sensitivity outcomes for one entry only when barrier/horizon tuples differ.
- Add immutable update/delete triggers and exact-idempotency checks.

- [ ] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_memecoin_opportunity_outcomes.py tests/test_memecoin_opportunity_landmarks.py tests/test_memecoin_opportunity_store.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add sniper/research/memecoin_opportunity/outcomes.py sniper/research/memecoin_opportunity/store.py tests/test_memecoin_opportunity_outcomes.py
git commit -m "feat: label executable opportunity outcomes"
```

---

### Task 6: Enforce research-only boundaries and verify the foundation

**Files:**

- Create: `tests/test_memecoin_opportunity_boundaries.py`
- Modify: `sniper/research/memecoin_opportunity/__init__.py`

**Boundary test:**

```python
FORBIDDEN_PREFIXES = (
    "aiohttp",
    "httpx",
    "requests",
    "solana",
    "web3",
    "websocket",
    "sniper.execution",
    "sniper.services.memecoin_v6",
    "sniper.services.memecoin_real",
    "sniper.services.memecoin_sender",
    "sniper.services.memecoin_signer",
)

def test_opportunity_package_has_no_funded_or_v6_imports():
    package_root = Path("sniper/research/memecoin_opportunity")
    violations = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported = imported_module_name(node)
            if imported and imported.startswith(FORBIDDEN_PREFIXES):
                violations.append((str(path), imported))
    assert violations == []
```

- [ ] **Step 1: Write boundary and public-export tests**

Tests must also prove:

- Importing the package does not create `data/memecoin_opportunity.db`.
- Importing the package does not read environment variables or initialize logging.
- `__init__.py` exports only stable domain types and pure evaluators; callers import storage explicitly.
- No package file contains `requests`, `httpx`, `websocket`, `solana`, `web3`, signing, broadcasting, V6, or funded-runner imports.

- [ ] **Step 2: Run boundary test and confirm RED if a violation exists**

```bash
python3 -m pytest tests/test_memecoin_opportunity_boundaries.py -q
```

Expected before final exports: failure for missing public contracts; any forbidden import is a hard failure.

- [ ] **Step 3: Finalize narrow public exports**

Export:

```python
from .landmarks import crossed_landmarks, materialize_landmarks
from .outcomes import evaluate_competing_outcome
from .types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    NetworkId,
    RawEvent,
    VenueId,
)
```

Do not export database connectors from package root.

- [ ] **Step 4: Run complete foundation verification**

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_types.py \
  tests/test_memecoin_opportunity_store.py \
  tests/test_memecoin_opportunity_frontier.py \
  tests/test_memecoin_opportunity_landmarks.py \
  tests/test_memecoin_opportunity_outcomes.py \
  tests/test_memecoin_opportunity_boundaries.py \
  -q
python3 -m pytest tests/test_memecoin_solana_tape.py -q
python3 scripts/validate_schema_refs.py
python3 scripts/validate_schema_refs.py --warn-unknown-tables
git diff --check
git status --short
```

Expected:

- All new focused tests pass.
- Existing Solana tape tests pass.
- Schema reference validation has no errors. Existing unrelated warnings may remain but must be listed in the handoff.
- `git diff --check` emits no output.
- Only intended foundation files are modified.

- [ ] **Step 5: Review against the approved design**

Check each statement directly:

```text
[ ] Separate network and venue identity are explicit.
[ ] Raw and canonical event truth preserve chain/source/decode timestamps.
[ ] Immutable facts cannot be updated or deleted.
[ ] Corrections and orphaning append successors.
[ ] A bounded or failed source scan cannot skip the durable frontier.
[ ] Open gaps make landmarks ineligible and outcomes censored.
[ ] Time and progress landmarks are deterministic and deduplicated.
[ ] Tentative fast-path entries are labeled only after canonical or orphaned reconciliation.
[ ] Returns are configured-clip executable returns, not chart returns.
[ ] Entry latency, entry cost, acquired quantity, and self-impact are persisted.
[ ] Data failure is distinct from exit failure.
[ ] Migration preserves last executable value.
[ ] No live, UI, model, V6, paper-entry, or funded behavior was added.
```

- [ ] **Step 6: Commit Task 6**

```bash
git add sniper/research/memecoin_opportunity/__init__.py tests/test_memecoin_opportunity_boundaries.py
git commit -m "test: enforce opportunity research boundaries"
```

---

## Completion Gate

This foundation is complete only when:

```text
focused tests are green
existing Solana tape tests are green
schema validation passes
git diff --check is clean
the worktree contains no unrelated changes
no default database was created during tests
no live endpoint was called
no production or funded state changed
```

Do not claim that this phase detects an edge. It creates the point-in-time truth and labeling contracts required to test one honestly.

## Next Plans After This Merge

Write and review these separately, in order:

1. `memecoin-opportunity-solana-capture`: Pump.fun and Bags program sources, decoders, recovery, confirmation/reorg reconciliation.
2. `memecoin-opportunity-4663-capture`: Bags lifecycle and Robinhood provider/sequencer benchmark, decoder, recovery, reorg handling.
3. `memecoin-opportunity-state-quotes`: curve reducers, entity evidence, creator linkage, quote ladders, self-impact simulation, and Level-1 directional Radar states.
4. `memecoin-opportunity-runtime`: default-OFF capture worker, corrected clock, health, watchdog, retention, and replay.
5. `memecoin-opportunity-models`: M0-M3 datasets, chronological evaluation, calibration, soft evidence levels, and registry.
6. `memecoin-opportunity-radar-ui`: snapshot/delta API, stale-state behavior, circular visualization, filters, and drill-down evidence.
