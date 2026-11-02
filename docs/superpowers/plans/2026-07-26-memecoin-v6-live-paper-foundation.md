# Memecoin V6 Live-Paper Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the immutable V6 post-fill transition API and its isolated, reconstructable live-paper ledger without connecting either to live signals or RPCs.

**Architecture:** `memecoin_v6_engine.py` converts immutable state plus one frozen observation into one deterministic decision. `db_memecoin_v6_paper.py` atomically records entries, observations, decisions, action outcomes, fills, and inventory in a dedicated SQLite database. The engine cannot call persistence or execution code; the ledger cannot import funded execution modules.

**Tech Stack:** Python 3 dataclasses and enums, canonical JSON, SHA-256 fingerprints, SQLite, pytest.

## Global Constraints

- Read `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md` first.
- Work in an isolated worktree created with `superpowers:using-git-worktrees`.
- Use RED-first TDD and one logical commit per task.
- V6 starts only after an immutable modeled entry fill exists.
- V6 is the only post-fill policy. No caller-owned TP, stop, trail, mirror, timecap, catastrophe, LP-pull, or unsellable actions.
- Missing evidence produces `HOLD` plus stable `data_insufficient:*` reasons.
- An unfilled action cannot reduce inventory, consume a tranche, or apply `state_if_filled`.
- The V6 database must contain no funded credentials, reservations, signed payloads, or transaction identities.
- Do not modify production databases, runtime state, services, `.env`, funded policy selection, or the pinned runtime.
- Do not build replay or a V5 comparison arm.

## File Map

- Create `sniper/services/memecoin_v6_engine.py`: complete pure V6 domain and transition.
- Modify `sniper/services/memecoin_v6_policy.py`: make its break manager an internal pure proposal seed.
- Create `sniper/persistence/db_memecoin_v6_paper.py`: isolated schema and atomic transitions.
- Modify `sniper/settings/memecoin.py`: default-OFF V6 paper settings.
- Create `tests/test_memecoin_v6_engine.py`, `tests/test_memecoin_v6_paper_settings.py`, and `tests/test_memecoin_v6_paper_db.py`.

## Locked Public Types

Later plans consume these names unchanged:

```python
class V6Action(str, Enum):
    HOLD = "HOLD"
    DERISK = "DERISK"
    EXIT_FULL = "EXIT_FULL"

@dataclass(frozen=True)
class V6PositionState:
    position_id: str
    remaining_qty_atomic: int
    break_state: V6State
    pending_action_token: str | None = None
    last_action_outcome: str | None = None

@dataclass(frozen=True)
class V6Observation:
    observation_id: str
    observed_at: str
    network_id: str
    payload: Mapping[str, object]
    input_fingerprint: str

@dataclass(frozen=True)
class V6Decision:
    action: V6Action
    fraction_ppm: int
    action_token: str
    state_if_filled: V6PositionState
    state_if_unfilled: V6PositionState
    reason_codes: tuple[str, ...]
    thought: str
    input_fingerprint: str
    policy_hash: str

def step(state: V6PositionState, observation: V6Observation) -> V6Decision: ...
```

`fraction_ppm` is integer millionths of remaining inventory: `0` for hold, `1..999_999` for derisk, and `1_000_000` for full exit. Inventory uses integer token atomic units.

---

### Task 1: Freeze the Complete V6 Transition Contract

**Files:**
- Create: `sniper/services/memecoin_v6_engine.py`
- Modify: `sniper/services/memecoin_v6_policy.py`
- Test: `tests/test_memecoin_v6_engine.py`
- Verify unchanged: `tests/test_memecoin_v6_action_replay.py`

**Interfaces:**
- Consumes: `V6State`, `missing_evidence`, and `v6_step` from `memecoin_v6_policy.py`.
- Produces: the locked types above plus `make_observation`, `canonical_json`, `fingerprint`, `policy_document`, and `policy_hash`.

- [ ] **Step 1: Write failing pure-transition tests**

```python
from dataclasses import replace


def _state(**changes):
    base = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000,
        break_state=V6State(),
    )
    return replace(base, **changes)


def complete_payload(**changes):
    base = {
        "executable_price": 0.00042,
        "chain_vol_p90": 0.18,
        "route_degraded": False,
        "source_wallet_exited": False,
        "wallet_tier_adjustment": 0,
        "is_break": False,
        "held_s": 60.0,
        "arrival_rate": 0.02,
        "buy_share": 0.60,
        "realized_vol": 0.05,
        "prior_low": 0.00040,
        "price": 0.00042,
        "net": 2.0,
    }
    base.update(changes)
    return base


def _observation(observation_id="o", **changes):
    return make_observation(
        observation_id=observation_id,
        observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana",
        payload=complete_payload(**changes),
    )


def test_missing_capture_holds_instead_of_raising():
    decision = step(_state(), make_observation(
        observation_id="o1", observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana",
        payload=complete_payload(executable_price=None, chain_vol_p90=None),
    ))
    assert decision.action is V6Action.HOLD
    assert decision.fraction_ppm == 0
    assert decision.reason_codes == (
        "data_insufficient:chain_vol_p90",
        "data_insufficient:executable_price",
    )


def test_unfilled_derisk_preserves_inventory_and_partial_budget():
    before = _state(remaining_qty_atomic=1_000_000)
    decision = step(before, make_observation(
        observation_id="o2",
        observed_at="2026-07-26T12:00:01+00:00",
        network_id="4663",
        payload=complete_payload(is_break=True, arrival_rate=0.01,
                                 buy_share=0.10, realized_vol=0.01),
    ))
    assert decision.action is V6Action.DERISK
    assert decision.state_if_unfilled.remaining_qty_atomic == 1_000_000
    assert decision.state_if_unfilled.break_state.tranches == before.break_state.tranches
    assert decision.state_if_unfilled.pending_action_token == decision.action_token
    assert decision.state_if_filled.break_state.tranches == before.break_state.tranches + 1


def test_transition_is_byte_stable():
    assert step(_state(), _observation()) == step(_state(), _observation())
```

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_engine.py -q
```

Expected: collection fails because `memecoin_v6_engine` does not exist.

- [ ] **Step 3: Implement canonical types and validation**

```python
def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def make_observation(*, observation_id: str, observed_at: str,
                     network_id: str, payload: Mapping[str, object]) -> V6Observation:
    body = {"observation_id": observation_id, "observed_at": observed_at,
            "network_id": network_id, "payload": dict(payload)}
    return V6Observation(input_fingerprint=fingerprint(body), **body)
```

Reject empty ids, unsupported networks, non-aware timestamps, non-finite numbers, and fingerprint mismatches. Supported networks are exactly `solana` and `4663`.

- [ ] **Step 4: Wrap the seed proposal without mutating prior state**

Normalize seed actions with:

```python
SEED_ACTION_MAP = {
    None: V6Action.HOLD,
    "derisk": V6Action.DERISK,
    "cut": V6Action.EXIT_FULL,
}
```

Call `v6_step()` on a copied `V6State`. `state_if_filled` takes its successor. `state_if_unfilled` preserves the prior action-budget fields while retaining only causal observation-history updates. Name the two budget fields explicitly; do not copy every proposed field back.

- [ ] **Step 5: Make decisions deterministic**

Sort missing reasons. Render thought with:

```python
def render_thought(action: V6Action, reasons: tuple[str, ...]) -> str:
    return f"{action.value}: {', '.join(reasons) if reasons else 'no_trigger'}"
```

Freeze every effective policy parameter in `policy_document()`. Compute the action token from position id, observation fingerprint, policy hash, action, and fraction. Do not catch arbitrary policy bugs; only recognized missing evidence becomes `HOLD`.

- [ ] **Step 6: Correct the seed module narrative**

State that `memecoin_v6_policy.py` is an internal proposal seed used by the complete engine, has no funded authority, and does not permit caller-owned post-fill rails. Keep its historical replay API only for compatibility.

- [ ] **Step 7: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_action_replay.py -q
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy.py tests/test_memecoin_v6_engine.py
git commit -m "feat: define complete v6 position transition"
```

### Task 2: Add Inert V6 Paper Settings

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_v6_paper_settings.py`

**Interfaces:**
- Produces: `MEMECOIN_V6_PAPER_ENABLED`, `MEMECOIN_V6_PAPER_DB`, `MEMECOIN_V6_PAPER_NOTIONAL_USD`, `MEMECOIN_V6_PAPER_MAX_OPEN`, `MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S`, `MEMECOIN_V6_PAPER_SWEEP_S`, and `MEMECOIN_V6_PAPER_NETWORKS`.

- [ ] **Step 1: Write failing defaults and validation tests**

```python
assert settings.MEMECOIN_V6_PAPER_ENABLED is False
assert settings.MEMECOIN_V6_PAPER_NOTIONAL_USD == 25.0
assert settings.MEMECOIN_V6_PAPER_MAX_OPEN == 4
assert settings.MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S == 10.0
assert settings.MEMECOIN_V6_PAPER_SWEEP_S == 5.0
assert settings.MEMECOIN_V6_PAPER_NETWORKS == ("solana", "4663")
```

Also assert non-positive notional/cap/age/interval and any other network raise `ValueError`.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_settings.py -q
```

- [ ] **Step 3: Add exact parsers and defaults**

Reuse current setting helpers. Keep the DB override optional; persistence resolves the default under the mutable state root. Do not add this enable flag to any funded selector.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_settings.py -q
git add sniper/settings/memecoin.py tests/test_memecoin_v6_paper_settings.py
git commit -m "feat: add inert v6 paper settings"
```

### Task 3: Create the Dedicated V6 Paper Ledger

**Files:**
- Create: `sniper/persistence/db_memecoin_v6_paper.py`
- Test: `tests/test_memecoin_v6_paper_db.py`

**Interfaces:**
- Consumes: Task 1 types and canonical serialization.
- Produces: `connect`, `connect_existing`, `record_signal_attempt`, `record_entry_fill`, `append_observation`, `append_decision`, `apply_hold_decision`, `apply_action_fill`, `record_action_unfilled`, `unapplied_decisions`, `open_positions`, `position_timeline`, `reconstruct_position`, `set_cursor`, and `get_cursor`.

- [ ] **Step 1: Write failing schema and isolation tests**

Assert exactly eight application tables exist:

```python
assert names == {
    "v6_paper_signal_attempts", "v6_paper_entries", "v6_paper_positions",
    "v6_paper_observations", "v6_paper_decisions", "v6_paper_action_claims",
    "v6_paper_fills", "v6_paper_cursors",
}
```

Assert their DDL contains none of `private_key`, `signed_payload`, `tx_sig`, or `reservation`. Assert `connect_existing()` uses `mode=ro` and fails when the file is absent.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_db.py -q
```

- [ ] **Step 3: Implement the schema**

Use these ownership rules and keys:

- `v6_paper_signal_attempts`: one immutable outcome per source event/policy; outcome `filled|quote_unavailable|quote_stale|capacity_full|identity_invalid`; corrected attempt time and detail JSON; optional entry id.
- `v6_paper_entries`: immutable entry evidence; unique `(origin_event_id, network_id, mint, policy_id)`; policy id fixed to `v6`; token and `quote_in_*`/`quote_out_*` atomic amounts/assets/decimals; canonical quote payload/fingerprint, route binding, costs, and honest-net flag; USD integer micros; prices canonical decimal strings; fill kind fixed to `modeled_live_quote`.
- `v6_paper_positions`: unique entry id; status `open|closed|censored_operator`; remaining atomic quantity; engine-state JSON/fingerprint; latest observation/decision/action; terminal/censor fields.
- `v6_paper_observations`: append-only id, position, corrected timestamp, network, block identity, payload/fingerprint, capture version.
- `v6_paper_decisions`: append-only action token, action, fraction ppm, before/filled/unfilled states, reasons, thought, input/policy hashes.
- `v6_paper_action_claims`: unique action token; status `claimed|filled|unfilled`; resolution/failure fields.
- `v6_paper_fills`: entry or action fill; token quantity plus `quote_in_*`/`quote_out_*` atomic amounts/assets/decimals; canonical quote payload/fingerprint, provider, route binding, costs, honest-net flag, and corrected fill time; unique action token; fill kind fixed to `modeled_live_quote`.
- `v6_paper_cursors`: cursor name/value/corrected timestamp.

Enable foreign keys. Default path is `<state_root>/data/memecoin_v6_paper.sqlite`. Only `connect()` initializes; `connect_existing()` is read-only.

- [ ] **Step 4: Test immutable entry dedup**

Assert `record_signal_attempt()` is idempotent on exact content and rejects conflicting outcomes. Assert first `record_entry_fill()` creates one entry, one position, and one entry fill and links the signal attempt. Exact redelivery returns the existing position. Same identity with changed immutable content raises `ImmutableConflict`. No decision exists before an observation and decision are appended.

- [ ] **Step 5: Implement immutable append semantics**

Use `BEGIN IMMEDIATE`. An id/fingerprint conflict is idempotent only when all immutable content is byte-equal; otherwise raise `ImmutableConflict`.

- [ ] **Step 6: Test atomic filled and unfilled outcomes**

```python
filled = apply_action_fill(conn, decision, modeled_fill)
assert filled.remaining_qty_atomic == 600_000
assert apply_action_fill(conn, decision, modeled_fill) == filled

unfilled = record_action_unfilled(conn, next_decision, "quote_unavailable", NOW)
assert unfilled.remaining_qty_atomic == 600_000
assert unfilled.engine_state == next_decision.state_if_unfilled
assert fill_count(conn, next_decision.action_token) == 0
```

Inject failure after fill insert and assert claim, fill, inventory, and state all roll back.

- [ ] **Step 7: Implement one-transaction action application**

Require the immutable decision to exist first. Claim the action token, validate current state fingerprint, calculate exact requested atomic quantity from `fraction_ppm`, require the fill quantity to match, append one fill, subtract inventory, apply `state_if_filled`, close only at zero/dust, and resolve the claim with the modeled quote timestamp. Unfilled application changes no quantity/accounting column and applies only `state_if_unfilled`. `apply_hold_decision()` applies causal HOLD state without a claim/fill.

- [ ] **Step 8: Test reconstruction and concurrency**

Assert entry quantity equals remaining quantity plus applied exit quantities. Interleave two positions, reconnect as a restarted worker, recover any unapplied decision before accepting another decision for that position, and reconstruct inventory plus engine state exactly from immutable decisions/fills. Censoring creates no fake fill and excludes the row from engine outcomes.

- [ ] **Step 9: Run foundation verification and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_settings.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_action_replay.py -q
python3 -m compileall -q sniper/services/memecoin_v6_engine.py sniper/persistence/db_memecoin_v6_paper.py
git diff --check
git add sniper/persistence/db_memecoin_v6_paper.py tests/test_memecoin_v6_paper_db.py
git commit -m "feat: add isolated v6 live-paper ledger"
```

## Foundation Review Gate

- `step()` is pure, deterministic, and the sole post-fill decision source.
- Unfilled transitions preserve inventory and partial budget.
- The database has no production/funded tables or credential-bearing columns.
- Redelivery cannot duplicate an entry, decision, claim, or fill.
- Multiple positions reconstruct independently.
- No live API or production database was touched.
