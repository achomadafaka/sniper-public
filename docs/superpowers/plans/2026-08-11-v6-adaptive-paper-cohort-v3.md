# V6 Adaptive Paper Cohort v3 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an adaptive V6 exit policy and a tighter Radar entry rule on a fresh cohort clock, and keep Pump, Pons, Bags, and copy-source results apart.

**Architecture:** The shared engine `sniper/services/memecoin_v6_engine.py` gains a frozen `V6Policy` object. `v6-position-engine-v2` keeps its exact document bytes and its exact hash. `v6-position-engine-v3` is a second policy with take-profit, trail, exact-stop, creator-sale, and short time-cap rules. A new Radar lifecycle `radar-adaptive-v3` drives that policy over Radar paper fills and writes to new append-only tables.

**Tech Stack:** Python 3, SQLite, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-11-v6-adaptive-paper-cohort-v3-design.md`

The `op_*` tables live in the isolated Radar ledger `data/memecoin_opportunity.db`, which
`scripts/brain_index.py` does not index. The markers below declare that on purpose.

<!-- schema:external=op_paper_outcomes -->
<!-- schema:external=op_paper_fill_requests -->
<!-- schema:external=op_paper_fills -->
<!-- schema:external=op_paper_lifecycle_activations -->
<!-- schema:external=op_paper_exit_observations -->
<!-- schema:external=op_paper_position_actions -->
<!-- schema:external=op_paper_position_states -->
<!-- schema:external=op_paper_exit_followups -->
<!-- schema:external=op_schema_version -->
<!-- schema:external=op_state_snapshots -->
<!-- schema:external=op_forward_alerts -->
<!-- schema:external=op_forward_alert_features -->
<!-- schema:external=op_quote_observations -->
<!-- schema:external=op_launches -->
<!-- schema:external=op_canonical_events -->
<!-- schema:external=op_landmark_states -->
<!-- schema:external=op_creator_outcomes -->
<!-- schema:external=op_counterfactual_entries -->
<!-- schema:external=op_forward_alert_entries -->
<!-- schema:external=op_runtime_status -->
<!-- schema:external=op_predictions -->
<!-- schema:external=memecoin_fills -->
<!-- schema:external=v6_paper_positions -->
<!-- schema:external=v6_paper_exit_followup_schedules -->

## Global Constraints

- Write all prose, comments, docstrings, and commit messages in ASD-STE100 Simplified Technical English.
- The v2 policy hash must stay `a11a0d279d9cd1c29974faac5ae6df7799af39b6b28acbbbfa182080f3cb9902`.
- V6 is paper only. Add no funded authority, no signing, no reservation, no live approval.
- Do not touch `sniper_tracker.db`, the funded runner, or `.runtime/memecoin/current`.
- Never update and never delete a `radar-realistic-v2` row.
- Every price that triggers a rule must be an executable route quote for the exact open quantity.
- Missing evidence produces `HOLD` with `data_insufficient:<field>`. A provider failure is never a token failure.
- Run `python3 scripts/validate_schema_refs.py` before every commit that names a column.
- Use `datetime.now(timezone.utc)`, never `datetime.utcnow()`.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/services/memecoin_v6_engine.py` | modify — `V6Policy`, rule registry, v3 rules, `step(policy=...)` |
| `sniper/research/memecoin_opportunity/state.py` | modify — `liquidity_removed_fraction_bps` interval feature |
| `sniper/research/memecoin_opportunity/frenzy.py` | create — the venue-age acceleration ceiling |
| `sniper/research/memecoin_opportunity/directional.py` | modify — `evaluate_adaptive_v3` and the v3 arm registry |
| `sniper/research/memecoin_opportunity/store.py` | modify — schema 14, two new tables, four new outcome columns |
| `sniper/research/memecoin_opportunity/paper_lifecycle_v3.py` | create — the adaptive lifecycle driver |
| `sniper/research/memecoin_opportunity/engine.py` | modify — Pons completion binding, v3 arm wiring |
| `sniper/research/memecoin_opportunity/evm_probe.py` | modify — carry the requesting snapshot identity |
| `sniper/routes/api_memecoin_opportunity.py` | modify — one result block per lifecycle |
| `scripts/memecoin_opportunity_worker.py` | modify — drive the v3 lifecycle |

---

### Task 1: Freeze the v2 policy behind a policy object

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py:408-455`
- Test: `tests/test_memecoin_v6_policy_objects.py`

**Interfaces:**
- Produces: `V6Policy` dataclass with fields `policy_id: str`, `document: Mapping`, `required_evidence: tuple[str, ...]`, `ordered_rules: tuple[str, ...]`, `params: Mapping`; method `hash() -> str`. Module constant `POLICY_V2: V6Policy`. `policy_document()` and `policy_hash()` keep their current signatures and delegate to `POLICY_V2`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_v6_policy_objects.py
from sniper.services.memecoin_v6_engine import (
    POLICY_V2, policy_document, policy_hash,
)

V2_HASH = "a11a0d279d9cd1c29974faac5ae6df7799af39b6b28acbbbfa182080f3cb9902"


def test_v2_policy_hash_is_pinned():
    assert POLICY_V2.hash() == V2_HASH
    assert policy_hash() == V2_HASH


def test_v2_policy_document_is_unchanged():
    assert policy_document() == dict(POLICY_V2.document)
    assert POLICY_V2.policy_id == "v6-position-engine-v2"


def test_v2_required_evidence_is_the_five_field_set():
    assert POLICY_V2.required_evidence == (
        "chain_vol_p90", "route_degraded", "source_wallet_exited",
        "wallet_tier_adjustment", "executable_price",
    )


def test_v2_ordered_rules_match_the_document():
    assert list(POLICY_V2.ordered_rules) == POLICY_V2.document["ordered_rules"]


def test_a_stored_v2_decision_replays_byte_for_byte(v6_paper_conn, stored_decision):
    """The whole reason v2 keeps its hash. A stored decision must reproduce exactly."""
    from sniper.persistence.db_memecoin_v6_paper import load_decision_inputs
    from sniper.services.memecoin_v6_engine import step

    state, observation, expected = load_decision_inputs(
        v6_paper_conn, stored_decision)
    replayed = step(state, observation)
    assert replayed.policy_hash == expected.policy_hash
    assert replayed.action is expected.action
    assert replayed.fraction_ppm == expected.fraction_ppm
    assert replayed.reason_codes == expected.reason_codes
    assert replayed.action_token == expected.action_token
```

Add a `v6_paper_conn` fixture that opens a `tmp_path` V6 paper database, and a
`stored_decision` fixture that inserts one decision through the existing writer. If
`load_decision_inputs` does not exist, use the existing reader that
`tests/test_memecoin_v6_paper_db.py` already uses to rebuild a decision, and name it in
the fixture instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_objects.py -q`
Expected: FAIL with `ImportError: cannot import name 'POLICY_V2'`

- [ ] **Step 3: Write minimal implementation**

Add to `sniper/services/memecoin_v6_engine.py`, after `policy_document()`. Keep the
existing `policy_document()` body as `_V2_DOCUMENT`; do not retype the literal.

```python
@dataclass(frozen=True)
class V6Policy:
    """One frozen decision policy. ``document`` is the byte-stable identity."""
    policy_id: str
    document: Mapping[str, object]
    required_evidence: tuple[str, ...]
    ordered_rules: tuple[str, ...]
    params: Mapping[str, object]

    def hash(self) -> str:
        return fingerprint(dict(self.document))


_V2_DOCUMENT = policy_document()

POLICY_V2 = V6Policy(
    policy_id=POLICY_ID,
    document=_V2_DOCUMENT,
    required_evidence=REQUIRED_ENGINE_EVIDENCE,
    ordered_rules=tuple(_V2_DOCUMENT["ordered_rules"]),
    params={
        "catastrophe_loss_pct": CATASTROPHE_LOSS_PCT,
        "catastrophe_confirmations": CATASTROPHE_CONFIRMATIONS,
        "adverse_quote_ratio_limit": ADVERSE_QUOTE_RATIO_LIMIT,
        "lp_minor_fraction": LP_MINOR_FRACTION,
        "lp_catastrophic_fraction": LP_CATASTROPHIC_FRACTION,
        "time_expiry_s": TIME_EXPIRY_S,
        "route_absent_min_checks": ROUTE_ABSENT_MIN_CHECKS,
        "route_absent_min_s": ROUTE_ABSENT_MIN_S,
        "max_tranches": MAX_TRANCHES,
    },
)
```

Change `policy_hash()` to `return POLICY_V2.hash()` and keep its `lru_cache`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_objects.py tests/test_memecoin_v6_engine.py -q`
Expected: PASS, and the existing engine suite stays green.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py tests/test_memecoin_v6_policy_objects.py
git commit -m "refactor(v6): hold the v2 policy in a frozen policy object with a pinned hash"
```

---

### Task 2: Make the rules read the policy

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py:634-812`
- Test: `tests/test_memecoin_v6_policy_objects.py`

**Interfaces:**
- Consumes: `V6Policy`, `POLICY_V2` from Task 1.
- Produces: `RULE_EVALUATORS: dict[str, Callable]` mapping a rule id to `fn(state, observation, policy) -> V6RuleResult`. `step(state, observation, *, policy: V6Policy = POLICY_V2) -> V6Decision`.

- [ ] **Step 1: Write the failing test**

```python
def test_step_accepts_an_explicit_policy_and_defaults_to_v2():
    from sniper.services.memecoin_v6_engine import (
        POLICY_V2, RULE_EVALUATORS, EvidenceIdentity, V6PositionState,
        make_observation, step,
    )
    state = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000,
        entry_unit_price="1.0", opened_at="2026-08-11T00:00:00+00:00",
    )
    payload = {
        "is_break": False, "held_s": 10.0, "arrival_rate": 0.0, "buy_share": 0.1,
        "realized_vol": 0.0, "prior_low": 1.0, "price": 1.0, "net": 1.0,
        "chain_vol_p90": 1.0, "route_degraded": False, "source_wallet_exited": False,
        "wallet_tier_adjustment": 0, "executable_price": 1.0, "quote_status": "healthy",
        "tape_complete": True, "depth_pull_flag": False,
    }
    observation = make_observation(
        observation_id="o1", observed_at="2026-08-11T00:00:10+00:00",
        network_id="solana", payload=payload,
        evidence=EvidenceIdentity(market="m1", quote="q1"),
    )
    default = step(state, observation)
    explicit = step(state, observation, policy=POLICY_V2)
    assert default.policy_hash == explicit.policy_hash == POLICY_V2.hash()
    assert set(POLICY_V2.ordered_rules) <= set(RULE_EVALUATORS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_objects.py::test_step_accepts_an_explicit_policy_and_defaults_to_v2 -q`
Expected: FAIL with `ImportError: cannot import name 'RULE_EVALUATORS'`

- [ ] **Step 3: Write minimal implementation**

Give every evaluator the same signature, and read the numbers from `policy.params`.

```python
def evaluate_lp_loss(state, observation, policy) -> V6RuleResult:
    ...
    severity = classify_lp_removal(
        fraction,
        minor_frac=policy.params["lp_minor_fraction"],
        catastrophic_frac=policy.params["lp_catastrophic_fraction"],
    )
    ...


def evaluate_time_expiry(state, observation, policy) -> V6RuleResult:
    held_s = observation.payload.get("held_s")
    if held_s is None:
        return _insufficient("time_expiry", "held_s")
    if float(held_s) >= policy.params["time_expiry_s"]:
        return V6RuleResult(
            "time_expiry", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 70, ("time_expiry",),
        )
    return V6RuleResult("time_expiry", RuleStatus.NOT_TRIGGERED)


RULE_EVALUATORS = {
    "lp_loss": evaluate_lp_loss,
    "catastrophe": evaluate_catastrophe,
    "route_loss": evaluate_route_loss,
    "time_expiry": evaluate_time_expiry,
}
```

`evaluate_catastrophe` and `evaluate_route_loss` gain the same third parameter and read
`policy.params["catastrophe_loss_pct"]`, `policy.params["catastrophe_confirmations"]`,
`policy.params["adverse_quote_ratio_limit"]`, `policy.params["route_absent_min_checks"]`,
and `policy.params["route_absent_min_s"]`.

In `step`, replace the fixed `rule_results` tuple:

```python
def step(state, observation, *, policy: V6Policy = POLICY_V2) -> V6Decision:
    ...
    p_hash = policy.hash()
    ...
    staged = _staged_result(staged_action, staged_fraction_ppm, seed_reasons, staged_missing)
    rule_results = tuple(
        RULE_EVALUATORS[rule_id](advanced_state, observation, policy)
        for rule_id in policy.ordered_rules if rule_id != "staged_break"
    ) + (staged,)
```

`_missing_capture` takes the policy and iterates `policy.required_evidence` in place of
the module constant.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_objects.py tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_db.py -q`
Expected: PASS. A stored v2 decision must still replay.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py tests/test_memecoin_v6_policy_objects.py
git commit -m "refactor(v6): dispatch rules from the policy and keep v2 behaviour identical"
```

---

### Task 3: Add the v3 take-profit and trail rules

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_policy_v3.py`

**Interfaces:**
- Consumes: `V6Policy`, `RULE_EVALUATORS` from Task 2.
- Produces: `evaluate_take_profit(state, observation, policy)`, `evaluate_trail(state, observation, policy)`, both registered in `RULE_EVALUATORS`. `_executable_return_bps(state, observation) -> int | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_v6_policy_v3.py
from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity, RuleStatus, V6Action, V6PositionState,
    evaluate_take_profit, evaluate_trail, make_observation,
)
from sniper.services.memecoin_v6_policy_v3 import POLICY_V3

BASE = {
    "is_break": False, "held_s": 30.0, "arrival_rate": 0.0, "buy_share": 0.1,
    "realized_vol": 0.0, "prior_low": 1.0, "price": 1.0, "net": 1.0,
    "chain_vol_p90": 1.0, "route_degraded": False, "creator_sale_since_entry": False,
    "quote_status": "healthy", "tape_complete": True, "depth_pull_flag": False,
}


def observation(price, *, quote="q1", **overrides):
    payload = {**BASE, "executable_price": price, **overrides}
    return make_observation(
        observation_id=f"o-{quote}-{price}", observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana", payload=payload,
        evidence=EvidenceIdentity(market="m1", quote=quote),
    )


def state(*, high_water=None, entry="1.0"):
    return V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000, entry_unit_price=entry,
        executable_high_water=high_water, opened_at="2026-08-11T00:00:00+00:00",
    )


def test_take_profit_derisks_half_at_plus_1200_bps():
    result = evaluate_take_profit(state(), observation(1.12), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.DERISK
    assert result.fraction_ppm == 500_000


def test_take_profit_does_not_trigger_below_the_threshold():
    result = evaluate_take_profit(state(), observation(1.1199), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_is_inert_before_the_take_profit_tranche():
    # An 8% fall from a high-water of 1.05, which never reached +12%.
    result = evaluate_trail(state(high_water=1.05), observation(0.96), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_exits_full_after_the_high_water_armed_it():
    result = evaluate_trail(state(high_water=1.20), observation(1.104), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL
    assert result.fraction_ppm == 1_000_000


def test_trail_holds_one_tick_above_the_stop_line():
    result = evaluate_trail(state(high_water=1.20), observation(1.105), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_needs_a_healthy_quote():
    result = evaluate_trail(
        state(high_water=1.20),
        observation(1.104, quote="q2", quote_status="provider_unavailable"),
        POLICY_V3,
    )
    assert result.status is RuleStatus.INSUFFICIENT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.services.memecoin_v6_policy_v3'`

- [ ] **Step 3: Write minimal implementation**

Add to `sniper/services/memecoin_v6_engine.py`:

```python
def _executable_return_bps(state: V6PositionState,
                           observation: V6Observation) -> int | None:
    """Return the exact executable return against the entry unit price."""
    price = observation.payload.get("executable_price")
    if price is None or state.entry_unit_price is None:
        return None
    entry = Decimal(state.entry_unit_price)
    if entry <= 0:
        return None
    return int((Decimal(str(price)) - entry) * 10_000 / entry)


def evaluate_take_profit(state, observation, policy) -> V6RuleResult:
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("take_profit", "healthy_quote")
    current = _executable_return_bps(state, observation)
    if current is None:
        return _insufficient("take_profit", "executable_price")
    if current < policy.params["take_profit_bps"]:
        return V6RuleResult("take_profit", RuleStatus.NOT_TRIGGERED)
    if state.filled_tranches >= 1:
        return V6RuleResult(
            "take_profit", RuleStatus.NOT_TRIGGERED,
            reason_codes=("take_profit:already_taken",))
    return V6RuleResult(
        "take_profit", RuleStatus.TRIGGERED, V6Action.DERISK,
        policy.params["take_profit_fraction_ppm"], 30, ("take_profit",),
    )


def evaluate_trail(state, observation, policy) -> V6RuleResult:
    """Arm on the executable high-water alone. The high-water can only reach the
    take-profit level through the same executable quote the tranche triggers on, so no
    extra state field is needed and the v2 state document stays unchanged."""
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("trail", "healthy_quote")
    price = observation.payload.get("executable_price")
    if price is None or state.entry_unit_price is None \
            or state.executable_high_water is None:
        return _insufficient("trail", "executable_price")
    entry = Decimal(state.entry_unit_price)
    arm_level = entry * (1 + Decimal(policy.params["take_profit_bps"]) / 10_000)
    if Decimal(str(state.executable_high_water)) < arm_level:
        return V6RuleResult("trail", RuleStatus.NOT_TRIGGERED,
                            reason_codes=("trail:unarmed",))
    giveback = Decimal(str(policy.params["trail_giveback_fraction"]))
    stop_level = Decimal(str(state.executable_high_water)) * (1 - giveback)
    if Decimal(str(price)) > stop_level:
        return V6RuleResult("trail", RuleStatus.NOT_TRIGGERED)
    return V6RuleResult(
        "trail", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 40, ("trail",),
    )


RULE_EVALUATORS["take_profit"] = evaluate_take_profit
RULE_EVALUATORS["trail"] = evaluate_trail
```

Create `sniper/services/memecoin_v6_policy_v3.py` with a first draft of `POLICY_V3`.
Task 5 completes it; this task needs only the parameters the two rules read.

```python
"""The v3 adaptive policy for the Radar paper cohort. Paper only, no funded authority."""
from __future__ import annotations

from sniper.services.memecoin_v6_engine import V6Policy

POLICY_V3_ID = "v6-position-engine-v3"

V3_PARAMS = {
    "take_profit_bps": 1_200,
    "take_profit_fraction_ppm": 500_000,
    "trail_giveback_fraction": 0.08,
    "exact_stop_bps": -1_000,
    "exact_stop_confirmations": 2,
    "time_cap_s": 120.0,
    "time_extend_s": 300.0,
    "lp_minor_fraction": 0.05,
    "lp_catastrophic_fraction": 0.30,
    "adverse_quote_ratio_limit": 10.0,
    "route_absent_min_checks": 4,
    "route_absent_min_s": 60.0,
    "max_tranches": 2,
}

V3_ORDERED_RULES = (
    "lp_loss", "creator_sale", "exact_stop", "route_loss", "time_cap",
    "trail", "take_profit", "staged_break",
)

V3_REQUIRED_EVIDENCE = (
    "executable_price", "quote_status", "creator_sale_since_entry",
    "route_degraded", "chain_vol_p90",
)

_V3_DOCUMENT = {
    "engine_policy_id": POLICY_V3_ID,
    "engine_evidence_mode": "per_rule",
    "engine_supported_networks": ["4663", "solana"],
    "evidence_semantics": {
        "route_degraded_statuses": ["route_absent"],
        "cancel_active_intent_on_healthy_no_break": True,
        "observation_identity": "semantic_sequence_v1",
        "unknown_quote_units_fail_closed": True,
        "trail_arms_on_executable_high_water": True,
    },
    "ordered_rules": list(V3_ORDERED_RULES),
    "required_evidence": list(V3_REQUIRED_EVIDENCE),
    "terminal_parameters": dict(sorted(V3_PARAMS.items())),
}

POLICY_V3 = V6Policy(
    policy_id=POLICY_V3_ID,
    document=_V3_DOCUMENT,
    required_evidence=V3_REQUIRED_EVIDENCE,
    ordered_rules=V3_ORDERED_RULES,
    params=V3_PARAMS,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py tests/test_memecoin_v6_policy_objects.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy_v3.py tests/test_memecoin_v6_policy_v3.py
git commit -m "feat(v6): add the v3 take-profit tranche and the executable high-water trail"
```

---

### Task 4: Add the v3 exact stop, creator sale, and time cap

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_policy_v3.py`

**Interfaces:**
- Consumes: `_executable_return_bps`, `RULE_EVALUATORS` from Task 3.
- Produces: `evaluate_exact_stop`, `evaluate_creator_sale`, `evaluate_time_cap`, all registered in `RULE_EVALUATORS`. `_advance_executable_quote` gains a `policy` parameter and uses `policy.params["exact_stop_bps"]` in place of the fixed 20% level.

- [ ] **Step 1: Write the failing test**

```python
def test_exact_stop_needs_two_distinct_healthy_quotes():
    from sniper.services.memecoin_v6_engine import evaluate_exact_stop
    first = state()
    assert evaluate_exact_stop(first, observation(0.90, quote="q1"), POLICY_V3).status \
        is RuleStatus.NOT_TRIGGERED
    confirmed = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000, entry_unit_price="1.0",
        catastrophe_candidate_price=0.90, catastrophe_confirmations=2,
        opened_at="2026-08-11T00:00:00+00:00",
    )
    result = evaluate_exact_stop(confirmed, observation(0.90, quote="q2"), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL


def test_provider_failure_never_confirms_the_stop():
    from sniper.services.memecoin_v6_engine import _advance_executable_quote
    before = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000, entry_unit_price="1.0",
        catastrophe_confirmations=1, catastrophe_candidate_price=0.90,
    )
    after = _advance_executable_quote(
        before, observation(0.90, quote="q9", quote_status="provider_unavailable"),
        True, POLICY_V3,
    )
    assert after.catastrophe_confirmations == 1


def test_creator_sale_exits_full():
    from sniper.services.memecoin_v6_engine import evaluate_creator_sale
    result = evaluate_creator_sale(
        state(), observation(1.0, creator_sale_since_entry=True), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL
    assert result.priority == 95


def test_creator_sale_is_insufficient_when_absent():
    from sniper.services.memecoin_v6_engine import evaluate_creator_sale
    payload = dict(BASE)
    payload.pop("creator_sale_since_entry")
    payload["executable_price"] = 1.0
    obs = make_observation(
        observation_id="o-nc", observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana", payload=payload,
        evidence=EvidenceIdentity(market="m1", quote="q1"))
    assert evaluate_creator_sale(state(), obs, POLICY_V3).status \
        is RuleStatus.INSUFFICIENT


def test_time_cap_holds_a_strong_position_to_300_s():
    from sniper.services.memecoin_v6_engine import evaluate_time_cap
    strong = observation(1.05, held_s=125.0, net=1.0, is_break=False)
    assert evaluate_time_cap(state(), strong, POLICY_V3).status \
        is RuleStatus.NOT_TRIGGERED
    weak = observation(0.98, held_s=125.0, net=-1.0, is_break=False, quote="q3")
    assert evaluate_time_cap(state(), weak, POLICY_V3).status is RuleStatus.TRIGGERED
    late = observation(1.05, held_s=301.0, net=1.0, is_break=False, quote="q4")
    assert evaluate_time_cap(state(), late, POLICY_V3).status is RuleStatus.TRIGGERED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py -q`
Expected: FAIL with `ImportError: cannot import name 'evaluate_exact_stop'`

- [ ] **Step 3: Write minimal implementation**

```python
def evaluate_exact_stop(state, observation, policy) -> V6RuleResult:
    """A confirmed exact loss. Two distinct healthy quotes, never one."""
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("exact_stop", "healthy_quote")
    current = _executable_return_bps(state, observation)
    if current is None:
        return _insufficient("exact_stop", "executable_price")
    if current > policy.params["exact_stop_bps"]:
        return V6RuleResult("exact_stop", RuleStatus.NOT_TRIGGERED)
    if state.catastrophe_confirmations < policy.params["exact_stop_confirmations"]:
        return V6RuleResult(
            "exact_stop", RuleStatus.NOT_TRIGGERED,
            reason_codes=("exact_stop:confirmation_pending",))
    return V6RuleResult(
        "exact_stop", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 90, ("exact_stop",),
    )


def evaluate_creator_sale(state, observation, policy) -> V6RuleResult:
    sold = observation.payload.get("creator_sale_since_entry")
    if sold is None:
        return _insufficient("creator_sale", "creator_sale_since_entry")
    if sold is not True:
        return V6RuleResult("creator_sale", RuleStatus.NOT_TRIGGERED)
    return V6RuleResult(
        "creator_sale", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 95, ("creator_sale",),
    )


def evaluate_time_cap(state, observation, policy) -> V6RuleResult:
    """Close at the cap. Strong demand extends the cap once, and no further."""
    held_s = observation.payload.get("held_s")
    if held_s is None:
        return _insufficient("time_cap", "held_s")
    held = float(held_s)
    if held >= policy.params["time_extend_s"]:
        return V6RuleResult(
            "time_cap", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 70, ("time_cap:hard",))
    if held < policy.params["time_cap_s"]:
        return V6RuleResult("time_cap", RuleStatus.NOT_TRIGGERED)
    net = observation.payload.get("net")
    current = _executable_return_bps(state, observation)
    strong = (
        observation.payload.get("is_break") is False
        and net is not None and float(net) > 0
        and current is not None and current >= 0
    )
    if strong:
        return V6RuleResult(
            "time_cap", RuleStatus.NOT_TRIGGERED,
            reason_codes=("time_cap:extended",))
    return V6RuleResult(
        "time_cap", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 70, ("time_cap",),
    )


RULE_EVALUATORS["exact_stop"] = evaluate_exact_stop
RULE_EVALUATORS["creator_sale"] = evaluate_creator_sale
RULE_EVALUATORS["time_cap"] = evaluate_time_cap
```

Change `_advance_executable_quote(state, observation, quote_is_new, policy)` so the loss
level comes from the policy. For v2 the level stays `entry * 0.20`; for v3 it is
`entry * (1 + exact_stop_bps / 10_000)`.

```python
def _loss_level(state: V6PositionState, policy: V6Policy) -> Decimal | None:
    if state.entry_unit_price is None:
        return None
    entry = Decimal(state.entry_unit_price)
    stop_bps = policy.params.get("exact_stop_bps")
    if stop_bps is None:
        return entry * Decimal(str(1 - policy.params["catastrophe_loss_pct"] / 100))
    return entry * (1 + Decimal(str(stop_bps)) / 10_000)
```

Every caller of `_advance_executable_quote` passes the policy. `evaluate_catastrophe`
uses `_loss_level` too, so v2 keeps its 20% level and v3 gets its −10% level.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py tests/test_memecoin_v6_policy_objects.py tests/test_memecoin_v6_engine.py -q`
Expected: PASS, and the v2 hash test still passes.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py tests/test_memecoin_v6_policy_v3.py
git commit -m "feat(v6): add the v3 confirmed exact stop, creator-sale exit, and short time cap"
```

---

### Task 5: Wire the v3 policy through step and bound the tranche budget

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py:815-1043`
- Modify: `sniper/services/memecoin_v6_policy_v3.py`
- Test: `tests/test_memecoin_v6_policy_v3.py`

**Interfaces:**
- Consumes: every rule from Tasks 2 to 4.
- Produces: `step(state, observation, policy=POLICY_V3)` returns v3 decisions whose `policy_hash` equals `POLICY_V3.hash()`.

- [ ] **Step 1: Write the failing test**

```python
def test_v3_step_takes_profit_then_exits_the_remainder_on_a_break():
    from sniper.services.memecoin_v6_engine import step
    opened = state()
    first = step(opened, observation(1.15, quote="qa"), policy=POLICY_V3)
    assert first.action is V6Action.DERISK
    assert first.fraction_ppm == 500_000
    assert first.policy_hash == POLICY_V3.hash()
    after = first.state_if_filled
    assert after.filled_tranches == 1
    broken = observation(
        1.05, quote="qb", is_break=True, prior_low=1.10, net=-5.0,
        arrival_rate=0.5, buy_share=0.9, realized_vol=9.0,
    )
    second = step(after, broken, policy=POLICY_V3)
    assert second.action is V6Action.EXIT_FULL
    assert second.fraction_ppm == 1_000_000


def test_v3_missing_evidence_holds_with_sorted_reason_codes():
    from sniper.services.memecoin_v6_engine import step
    payload = dict(BASE)
    payload.pop("creator_sale_since_entry")
    payload.pop("chain_vol_p90")
    payload["executable_price"] = 1.0
    obs = make_observation(
        observation_id="o-missing", observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana", payload=payload,
        evidence=EvidenceIdentity(market="m9", quote="q9"))
    decision = step(state(), obs, policy=POLICY_V3)
    assert decision.action is V6Action.HOLD
    codes = [c for c in decision.reason_codes if c.startswith("creator_sale:insufficient")]
    assert codes


def test_v2_and_v3_hashes_differ():
    from sniper.services.memecoin_v6_engine import POLICY_V2
    assert POLICY_V2.hash() != POLICY_V3.hash()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py -q`
Expected: FAIL — `step` rejects the `policy` keyword for v3, or the tranche budget is wrong.

- [ ] **Step 3: Write minimal implementation**

In `step`, replace the module `MAX_TRANCHES` reference with `policy.params["max_tranches"]`
where the seed successor is built, and pass the policy to `_advance_executable_quote` and
to `_missing_capture`. `V6PositionState.__post_init__` keeps the module `MAX_TRANCHES` of
2, because both policies use 2.

The break rule after a filled take-profit tranche already exits in full: `v6_step` with
`tranches == 1` gives a `derisk` at 0.5 of the remainder, and the `staged_break` result
carries priority 10. Add one v3 line so a break with a spent budget exits full:

```python
    if policy.params.get("take_profit_bps") is not None \
            and state.filled_tranches >= 1 \
            and staged.status is RuleStatus.TRIGGERED:
        staged = V6RuleResult(
            "staged_break", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 20, (*staged.reason_codes, "budget_exhausted"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_v6_policy_v3.py tests/test_memecoin_v6_policy_objects.py tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_db.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy_v3.py tests/test_memecoin_v6_policy_v3.py
git commit -m "feat(v6): drive the v3 policy through step with a bounded tranche budget"
```

---

### Task 6: Add the LP-loss interval feature and the frenzy ceiling

**Files:**
- Modify: `sniper/research/memecoin_opportunity/state.py:431-504`
- Create: `sniper/research/memecoin_opportunity/frenzy.py`
- Test: `tests/test_radar_v3_entry_features.py`

**Interfaces:**
- Produces: interval feature key `liquidity_removed_fraction_bps: int | None`.
- Produces: public helper `liquidity_removed_fraction_bps(current: LaunchState, prior: LaunchState) -> int | None` in `state.py`. Task 10 imports it, so it must not be private.
- Produces: `FrenzyCeiling` class with `observe(venue_id: str, launch_age_ms: int, acceleration: int | None) -> None` and `ceiling(venue_id: str, launch_age_ms: int) -> int | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_v3_entry_features.py
from sniper.research.memecoin_opportunity.frenzy import (
    FRENZY_MIN_SAMPLES, FrenzyCeiling,
)


def test_liquidity_removed_fraction_is_the_reserve_drop_in_bps(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features
    prior = make_state(real_quote_reserves=1_000_000, observed_at="2026-08-11T00:00:00Z")
    current = make_state(real_quote_reserves=940_000, observed_at="2026-08-11T00:00:05Z")
    features = point_in_time_features(current, prior=prior)
    assert features["liquidity_removed_fraction_bps"] == 600


def test_liquidity_removed_fraction_is_none_without_a_prior_reserve(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features
    prior = make_state(real_quote_reserves=None, observed_at="2026-08-11T00:00:00Z")
    current = make_state(real_quote_reserves=940_000, observed_at="2026-08-11T00:00:05Z")
    features = point_in_time_features(current, prior=prior)
    assert features["liquidity_removed_fraction_bps"] is None


def test_liquidity_growth_reads_as_zero_removal(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features
    prior = make_state(real_quote_reserves=1_000_000, observed_at="2026-08-11T00:00:00Z")
    current = make_state(real_quote_reserves=1_500_000, observed_at="2026-08-11T00:00:05Z")
    assert point_in_time_features(current, prior=prior)[
        "liquidity_removed_fraction_bps"] == 0


def test_frenzy_ceiling_is_none_below_the_minimum_sample():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES - 1):
        ceiling.observe("pumpfun_solana", 17_000, index)
    assert ceiling.ceiling("pumpfun_solana", 17_000) is None


def test_frenzy_ceiling_is_the_p75_of_its_own_age_bucket():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, index)
        ceiling.observe("pumpfun_solana", 33_000, 1_000_000 + index)
    low = ceiling.ceiling("pumpfun_solana", 17_400)
    high = ceiling.ceiling("pumpfun_solana", 33_400)
    assert low is not None and high is not None and high > low


def test_frenzy_ceiling_separates_venues():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, index)
    assert ceiling.ceiling("pons_v1_4663", 17_000) is None


def test_frenzy_ceiling_ignores_a_missing_acceleration():
    ceiling = FrenzyCeiling()
    for _ in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, None)
    assert ceiling.ceiling("pumpfun_solana", 17_000) is None
```

Add a `make_state` fixture to the test file that builds a `LaunchState` with the fields
each test names and safe defaults for the rest.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_v3_entry_features.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...frenzy'`

- [ ] **Step 3: Write minimal implementation**

Create `sniper/research/memecoin_opportunity/frenzy.py`:

```python
"""The venue-age acceleration ceiling for the v3 entry gate.

The ceiling is a live percentile over recent launches on the same venue in the same
5-second age bucket. It is deliberately in memory only. After a restart the gate has no
ceiling and it fails closed until the bucket refills. A gate that opens on an empty
history is worse than a gate that waits.
"""
from __future__ import annotations

from collections import defaultdict, deque

from sniper.analysis.memecoin_v6_statistics import linear_percentile

FRENZY_AGE_BUCKET_MS = 5_000
FRENZY_PERCENTILE = 75
FRENZY_MIN_SAMPLES = 40
FRENZY_MAX_SAMPLES = 200


def _bucket(launch_age_ms: int) -> int:
    return int(launch_age_ms) // FRENZY_AGE_BUCKET_MS


class FrenzyCeiling:
    def __init__(self) -> None:
        self._samples = defaultdict(lambda: deque(maxlen=FRENZY_MAX_SAMPLES))

    def observe(self, venue_id: str, launch_age_ms: int,
                acceleration: int | None) -> None:
        if acceleration is None:
            return
        self._samples[(str(venue_id), _bucket(launch_age_ms))].append(int(acceleration))

    def ceiling(self, venue_id: str, launch_age_ms: int) -> int | None:
        samples = self._samples.get((str(venue_id), _bucket(launch_age_ms)))
        if samples is None or len(samples) < FRENZY_MIN_SAMPLES:
            return None
        return int(linear_percentile(tuple(samples), FRENZY_PERCENTILE))
```

In `state.py`, add the reserve helper and the feature to all three branches of
`point_in_time_features`. The two early-return branches set it to `None`.

```python
def _reserve(state: LaunchState) -> int | None:
    """One reserve number per venue. Solana uses the curve; EVM uses pool liquidity."""
    if state.real_quote_reserves is not None:
        return state.real_quote_reserves
    return state.v3_liquidity


def liquidity_removed_fraction_bps(current: LaunchState,
                                    prior: LaunchState) -> int | None:
    """Public: the Pons completion check in `engine.py` calls this too."""
    prior_reserve = _reserve(prior)
    current_reserve = _reserve(current)
    if not prior_reserve or current_reserve is None:
        return None
    return max(0, (prior_reserve - current_reserve) * 10_000 // prior_reserve)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_v3_entry_features.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/frenzy.py sniper/research/memecoin_opportunity/state.py tests/test_radar_v3_entry_features.py
git commit -m "feat(radar): add the LP-loss interval feature and the venue-age frenzy ceiling"
```

---

### Task 7: Add the v3 entry predicate

**Files:**
- Modify: `sniper/research/memecoin_opportunity/directional.py:413-460`
- Test: `tests/test_radar_v3_entry_rule.py`

**Interfaces:**
- Consumes: `liquidity_removed_fraction_bps` and `FrenzyCeiling` from Task 6.
- Produces: `ADAPTIVE_V3_RULES: tuple[str, ...]`, `ADAPTIVE_V3_BY_VENUE: dict[str, str]`, and `evaluate_adaptive_v3(current, *, prior) -> DirectionalVerdict | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_v3_entry_rule.py
import pytest

from sniper.research.memecoin_opportunity.directional import (
    ADAPTIVE_V3_BY_VENUE, evaluate_adaptive_v3,
)

PRIOR = {
    "effective_buyer_count": 8, "capital_velocity_atomic_per_s": 100,
    "buyer_velocity_per_s_micros": 100, "effective_buyer_hhi_bps": 2_000,
}
CURRENT = {
    "venue_id": "pumpfun_solana", "eligible": True, "lifecycle": "pre_migration",
    "effective_buyer_count": 12, "capital_velocity_atomic_per_s": 200,
    "buyer_velocity_per_s_micros": 200, "effective_buyer_hhi_bps": 1_500,
    "new_eligible_buyers": 4, "sell_pressure_bps": 1_000,
    "creator_sale_since_prior": False, "local_exit_exists": True,
    "remote_entry_quote_ok": True, "remote_sell_simulation_ok": True,
    "launch_age_ms": 20_000, "observed_pair_return_bps": -300,
    "liquidity_removed_fraction_bps": 100,
    "capital_acceleration_atomic_per_s2": 500, "frenzy_ceiling_atomic_per_s2": 900,
    "quote_age_ms": 800, "progress_bps": 3_000,
}


def verdict(**overrides):
    return evaluate_adaptive_v3({**CURRENT, **overrides}, prior=PRIOR)


def test_the_baseline_case_accepts():
    result = verdict()
    assert result is not None
    assert result.rule_version == ADAPTIVE_V3_BY_VENUE["pumpfun_solana"]


@pytest.mark.parametrize("age_ms,accepted", [
    (14_900, False), (15_100, True), (34_900, True), (35_100, False)])
def test_the_age_window_is_15_to_35_seconds(age_ms, accepted):
    assert (verdict(launch_age_ms=age_ms) is not None) is accepted


def test_a_material_lp_loss_rejects():
    assert verdict(liquidity_removed_fraction_bps=500) is None


def test_a_missing_lp_feature_rejects():
    assert verdict(liquidity_removed_fraction_bps=None) is None


def test_acceleration_at_or_below_the_ceiling_accepts():
    assert verdict(capital_acceleration_atomic_per_s2=900) is not None
    assert verdict(capital_acceleration_atomic_per_s2=901) is None


def test_a_missing_ceiling_rejects():
    assert verdict(frenzy_ceiling_atomic_per_s2=None) is None


@pytest.mark.parametrize("quote_age_ms,accepted", [(1_999, True), (2_000, False)])
def test_the_quote_must_be_under_two_seconds(quote_age_ms, accepted):
    assert (verdict(quote_age_ms=quote_age_ms) is not None) is accepted


def test_no_prior_observation_rejects():
    assert evaluate_adaptive_v3(CURRENT, prior=None) is None


def test_the_solvency_floor_still_applies():
    assert verdict(observed_pair_return_bps=-501) is None


def test_a_creator_sale_rejects():
    assert verdict(creator_sale_since_prior=True) is None


def test_rising_concentration_rejects():
    assert verdict(effective_buyer_hhi_bps=2_500) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_v3_entry_rule.py -q`
Expected: FAIL with `ImportError: cannot import name 'evaluate_adaptive_v3'`

- [ ] **Step 3: Write minimal implementation**

```python
# The v3 arm. It applies the venue-confirmation predicate to every venue, narrows the
# age window to 15-35 s, and adds three fail-closed gates: LP loss, the acceleration
# ceiling, and quote age. The window comes from 54 v6 results whose band difference is
# +2,173 bps at p=0.081. That is a prior, not a measured edge. The matched control is
# the only thing that can settle it.
ADAPTIVE_V3_BY_VENUE = {
    "pumpfun_solana": "pump-adaptive-v3",
    "bags_robinhood_4663": "bags-adaptive-v3",
    "pons_v1_4663": "pons-adaptive-v3",
}
ADAPTIVE_V3_RULES = tuple(sorted(ADAPTIVE_V3_BY_VENUE.values()))
ADAPTIVE_V3_MIN_AGE_MS = 15_000
ADAPTIVE_V3_MAX_AGE_MS = 35_000
ADAPTIVE_V3_MAX_LP_REMOVED_BPS = 500
ADAPTIVE_V3_MAX_QUOTE_AGE_MS = 2_000


def evaluate_adaptive_v3(current, *, prior):
    """v3: the venue-confirmation predicate, a narrow age window, three new gates."""
    venue = str(current.get("venue_id") or "")
    rule = ADAPTIVE_V3_BY_VENUE.get(venue)
    if rule is None or not current.get("eligible") or prior is None:
        return None
    if venue == "bags_robinhood_4663" and int(current.get("progress_bps") or 0) < 2_000:
        return None
    if venue == "pons_v1_4663" and current.get("lifecycle") != "active_pool":
        return None
    age_ms = int(current.get("launch_age_ms") or 0)
    if not ADAPTIVE_V3_MIN_AGE_MS < age_ms <= ADAPTIVE_V3_MAX_AGE_MS:
        return None
    removed = current.get("liquidity_removed_fraction_bps")
    if removed is None or int(removed) >= ADAPTIVE_V3_MAX_LP_REMOVED_BPS:
        return None
    acceleration = current.get("capital_acceleration_atomic_per_s2")
    ceiling = current.get("frenzy_ceiling_atomic_per_s2")
    if acceleration is None or ceiling is None or int(acceleration) > int(ceiling):
        return None
    quote_age_ms = current.get("quote_age_ms")
    if quote_age_ms is None or int(quote_age_ms) >= ADAPTIVE_V3_MAX_QUOTE_AGE_MS:
        return None
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    observed = current.get("observed_pair_return_bps")
    sell_pressure = current.get("sell_pressure_bps")
    if not (
        int(current.get("capital_velocity_atomic_per_s") or 0)
        > int(prior.get("capital_velocity_atomic_per_s") or 0)
        and int(current.get("buyer_velocity_per_s_micros") or 0)
        > int(prior.get("buyer_velocity_per_s_micros") or 0)
        and current_hhi is not None and prior_hhi is not None
        and int(current_hhi) <= int(prior_hhi)
        and int(current.get("effective_buyer_count") or 0) >= 10
        and int(current.get("new_eligible_buyers") or 0) >= 2
        and sell_pressure is not None and int(sell_pressure) <= 2_500
        and not bool(current.get("creator_sale_since_prior"))
        and bool(current.get("local_exit_exists"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
        and observed is not None
        and int(observed) >= CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS
    ):
        return None
    return DirectionalVerdict(
        RadarZone.CENTER, True, ("ADAPTIVE_V3",),
        rule_version=rule, truth_label="ADAPTIVE V3 — DIAGNOSTIC PAPER",
    )
```

Add `*ADAPTIVE_V3_RULES` to `EXTENDED_HORIZON_RULES`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_v3_entry_rule.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/directional.py tests/test_radar_v3_entry_rule.py
git commit -m "feat(radar): add the v3 adaptive entry predicate with three fail-closed gates"
```

---

### Task 8: Migrate the Radar store to schema 14

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py:16,223-227,724-775`
- Test: `tests/test_radar_store_schema_14.py`

**Interfaces:**
- Produces: `SCHEMA_VERSION = 14`, `_PAPER_V3_STATEMENTS: tuple[str, ...]`, tables
  `op_paper_position_actions`, `op_paper_position_states`, and `op_paper_exit_followups`,
  and four nullable columns on `op_paper_outcomes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_store_schema_14.py
from sniper.research.memecoin_opportunity.store import SCHEMA_VERSION, connect


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_schema_version_is_14(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert SCHEMA_VERSION == 14
    assert conn.execute("SELECT version FROM op_schema_version").fetchone()[0] == 14


def test_the_two_v3_tables_exist(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert columns(conn, "op_paper_position_actions") >= {
        "action_id", "request_id", "lifecycle_version", "sequence", "action",
        "fraction_ppm", "quantity_atomic", "expected_out_atomic", "scored_out_atomic",
        "reason_codes_json", "policy_hash", "observed_at",
    }
    assert columns(conn, "op_paper_position_states") >= {
        "state_id", "request_id", "lifecycle_version", "sequence", "state_json",
        "policy_hash", "observed_at",
    }
    assert columns(conn, "op_paper_exit_followups") >= {
        "followup_id", "request_id", "lifecycle_version", "horizon_s", "due_at",
        "status", "expected_out_atomic", "return_bps", "observed_at",
    }


def test_outcomes_gain_four_nullable_columns(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert columns(conn, "op_paper_outcomes") >= {
        "origin_kind", "policy_hash", "decision_facts_json", "exit_reason_codes_json",
    }


def test_the_new_result_codes_are_accepted(tmp_path):
    conn = connect(tmp_path / "radar.db")
    for code in ("adaptive_exit", "trail_exit", "creator_exit"):
        conn.execute(
            "INSERT INTO op_paper_outcomes (outcome_id,request_id,lifecycle_version,"
            "status,result_code,entry_quote_atomic,return_bps,outcome_at,"
            "row_fingerprint,inserted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, f"r-{code}", "radar-adaptive-v3", "valid", code, "1000", 10,
             "2026-08-11T00:00:00Z", "f", "2026-08-11T00:00:00Z"),
        )


def test_a_version_13_database_migrates_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.execute("UPDATE op_schema_version SET version=13")
    conn.execute(
        "INSERT INTO op_paper_outcomes (outcome_id,request_id,lifecycle_version,"
        "status,result_code,entry_quote_atomic,return_bps,outcome_at,"
        "row_fingerprint,inserted_at) VALUES "
        "('o1','r1','radar-realistic-v2','valid','stop_first','1000',-800,"
        "'2026-08-07T00:00:00Z','f','2026-08-07T00:00:00Z')")
    conn.commit()
    conn.close()
    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 14
    row = reopened.execute(
        "SELECT return_bps,origin_kind FROM op_paper_outcomes WHERE outcome_id='o1'"
    ).fetchone()
    assert row[0] == -800 and row[1] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_store_schema_14.py -q`
Expected: FAIL with `assert 13 == 14`

- [ ] **Step 3: Write minimal implementation**

1. Set `SCHEMA_VERSION = 14`.
2. Add `_PAPER_V3_STATEMENTS` with the three `CREATE TABLE IF NOT EXISTS` bodies —
   `op_paper_position_actions` and `op_paper_position_states` from spec section 8.1, and
   `op_paper_exit_followups` from Task 9 — plus one
   `CREATE TRIGGER IF NOT EXISTS <table>_no_update` per append-only table.
   `op_paper_exit_followups` is the one exception: its row is written as `scheduled` and
   later resolved, so it takes a `_no_delete` trigger instead of a `_no_update` trigger.
3. Append `_PAPER_V3_STATEMENTS` to the `SCHEMA` literal, next to
   `_PAPER_LIFECYCLE_STATEMENTS`.
4. In `_migrate_schema`, accept version 13 and add the replay:

```python
        for statement in _PAPER_V3_STATEMENTS:
            conn.execute(statement)
        outcome_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(op_paper_outcomes)")
        }
        for column in ("origin_kind", "policy_hash", "decision_facts_json",
                       "exit_reason_codes_json"):
            if column not in outcome_columns:
                conn.execute(f"ALTER TABLE op_paper_outcomes ADD COLUMN {column} TEXT")
```

5. The `result_code` CHECK is part of the table body. SQLite cannot alter a CHECK in
   place, so widen it with the same table-rebuild helper `_migrate_paper_atomic_columns`
   already uses. Add `op_paper_outcomes` to its `table_specs` with the new body.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_store_schema_14.py -q && python3 scripts/validate_schema_refs.py`
Expected: PASS and no schema violation.

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/store.py tests/test_radar_store_schema_14.py
git commit -m "feat(radar): migrate the store to schema 14 for the v3 adaptive cohort"
```

---

### Task 9: Build the adaptive lifecycle driver

**Files:**
- Create: `sniper/research/memecoin_opportunity/paper_lifecycle_v3.py`
- Test: `tests/test_radar_adaptive_lifecycle.py`

**Interfaces:**
- Consumes: `POLICY_V3`, `step` from Task 5; the schema-14 tables from Task 8.
- Produces: `LIFECYCLE_V3 = "radar-adaptive-v3"`, `ADAPTIVE_ARMS: tuple[str, ...]`,
  `activate_adaptive_lifecycle(conn, *, now_iso) -> str`,
  `drive_adaptive_lifecycle(conn, *, quote_fn, observation_fn, now_iso) -> dict[str, int]`,
  `adaptive_result_summary(conn) -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_adaptive_lifecycle.py
from sniper.research.memecoin_opportunity.paper_lifecycle_v3 import (
    ADAPTIVE_ARMS, LIFECYCLE_V3, activate_adaptive_lifecycle,
    adaptive_result_summary, drive_adaptive_lifecycle,
)


def test_the_arms_are_the_v3_rules_and_the_baseline_control():
    assert "pump-adaptive-v3" in ADAPTIVE_ARMS
    assert "pump-confirmation-truth-v6" in ADAPTIVE_ARMS


def test_activation_is_idempotent(radar_conn):
    first = activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    second = activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T01:00:00Z")
    assert first == second == LIFECYCLE_V3
    assert radar_conn.execute(
        "SELECT COUNT(*) FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?", (LIFECYCLE_V3,)).fetchone()[0] == 1


def test_a_take_profit_writes_one_action_row_and_leaves_the_position_open(
        radar_conn, filled_entry, rising_quotes):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn, quote_fn=rising_quotes, observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z")
    actions = radar_conn.execute(
        "SELECT action,fraction_ppm FROM op_paper_position_actions "
        "WHERE request_id=?", (filled_entry,)).fetchall()
    assert [tuple(row) for row in actions] == [("DERISK", 500_000)]
    assert radar_conn.execute(
        "SELECT COUNT(*) FROM op_paper_outcomes WHERE request_id=? "
        "AND lifecycle_version=?", (filled_entry, LIFECYCLE_V3)).fetchone()[0] == 0


def test_the_outcome_sums_every_tranche(radar_conn, filled_entry, two_tranche_quotes):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    for tick in ("00:00:10", "00:02:30"):
        drive_adaptive_lifecycle(
            radar_conn, quote_fn=two_tranche_quotes,
            observation_fn=lambda *_, **__: {}, now_iso=f"2026-08-11T{tick}Z")
    row = radar_conn.execute(
        "SELECT status,result_code,return_bps,origin_kind,policy_hash "
        "FROM op_paper_outcomes WHERE request_id=? AND lifecycle_version=?",
        (filled_entry, LIFECYCLE_V3)).fetchone()
    total = radar_conn.execute(
        "SELECT SUM(CAST(scored_out_atomic AS INTEGER)) "
        "FROM op_paper_position_actions WHERE request_id=?", (filled_entry,)
    ).fetchone()[0]
    entry_atomic = radar_conn.execute(
        "SELECT CAST(quote_in_atomic AS INTEGER) FROM op_paper_fills "
        "WHERE request_id=?", (filled_entry,)).fetchone()[0]
    assert row["status"] == "valid"
    assert row["return_bps"] == (total - entry_atomic) * 10_000 // entry_atomic
    assert row["origin_kind"] == "radar_pump"
    assert row["policy_hash"]


def test_the_realistic_v2_rows_are_untouched(radar_conn, v2_outcome):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn, quote_fn=lambda *_: None, observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z")
    assert radar_conn.execute(
        "SELECT return_bps FROM op_paper_outcomes WHERE outcome_id=?",
        (v2_outcome,)).fetchone()[0] == -800


def test_the_summary_never_pools_two_venues(radar_conn):
    summary = adaptive_result_summary(radar_conn)
    assert set(summary["by_venue"]) == {
        "pumpfun_solana", "bags_robinhood_4663", "pons_v1_4663"}
    assert "average_net_return_bps" not in summary or summary.get("pooled") is None


def test_a_terminal_exit_schedules_four_follow_up_horizons(
        radar_conn, filled_entry, stop_quotes):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn, quote_fn=stop_quotes, observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z")
    horizons = [row[0] for row in radar_conn.execute(
        "SELECT horizon_s FROM op_paper_exit_followups WHERE request_id=? "
        "ORDER BY horizon_s", (filled_entry,))]
    assert horizons == [30, 120, 300, 900]


def test_a_follow_up_quote_changes_no_state_and_no_return(
        radar_conn, filled_entry, stop_quotes, followup_quotes):
    """Follow-up rows are measurement only. They must never feed back."""
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn, quote_fn=stop_quotes, observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z")

    def snapshot():
        return (
            radar_conn.execute(
                "SELECT return_bps FROM op_paper_outcomes WHERE request_id=? "
                "AND lifecycle_version=?", (filled_entry, LIFECYCLE_V3)).fetchone()[0],
            radar_conn.execute(
                "SELECT COUNT(*),MAX(sequence) FROM op_paper_position_states "
                "WHERE request_id=?", (filled_entry,)).fetchone()[0:2],
            radar_conn.execute(
                "SELECT COUNT(*) FROM op_paper_position_actions "
                "WHERE request_id=?", (filled_entry,)).fetchone()[0],
        )

    before = snapshot()
    drive_adaptive_lifecycle(
        radar_conn, quote_fn=followup_quotes, observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:16:00Z")
    assert snapshot() == before
```

The follow-up rows need one more table. Add it to Task 8's `_PAPER_V3_STATEMENTS`:

```sql
CREATE TABLE IF NOT EXISTS op_paper_exit_followups (
    followup_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    horizon_s INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','executable','unavailable')),
    expected_out_atomic TEXT,
    return_bps INTEGER,
    failure_reason TEXT,
    observed_at TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, horizon_s)
);
```

Add the `radar_conn`, `filled_entry`, `rising_quotes`, `two_tranche_quotes`, and
`v2_outcome` fixtures to the test file. Build them on a `tmp_path` database through
`store.connect`, not on a production path.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_adaptive_lifecycle.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...paper_lifecycle_v3'`

- [ ] **Step 3: Write minimal implementation**

The per-entry body is the part that must be exact. Write it first:

```python
LIFECYCLE_V3 = "radar-adaptive-v3"
ADAPTIVE_ARMS = (*ADAPTIVE_V3_RULES, *PAPER_RULE_BY_VENUE.values())
FOLLOWUP_HORIZONS_S = (30, 120, 300, 900)
ORIGIN_BY_VENUE = {
    "pumpfun_solana": "radar_pump",
    "bags_robinhood_4663": "radar_bags",
    "pons_v1_4663": "radar_pons",
}
TERMINAL_CODE_BY_REASON = {
    "trail": "trail_exit",
    "creator_sale": "creator_exit",
    "exact_stop": "stop_first",
    "time_cap": "time_exit",
    "time_cap:hard": "time_exit",
}


def _result_code(reason_codes: tuple[str, ...]) -> str:
    for reason in reason_codes:
        code = TERMINAL_CODE_BY_REASON.get(reason)
        if code is not None:
            return code
    return "adaptive_exit"


def _advance_one(conn, entry, *, quote_fn, observation_fn, now_iso) -> str:
    """One engine tick for one open entry. Returns the counter name to increment."""
    sequence, state = _load_state(conn, entry)
    observation = _build_observation(
        entry, state, facts=observation_fn(entry, now_iso=now_iso), now_iso=now_iso)
    decision = step(state, observation, policy=POLICY_V3)

    if decision.action is V6Action.HOLD:
        _append_state(conn, entry, sequence + 1, decision.state_if_filled, now_iso)
        return "held"

    requested = min(
        state.remaining_qty_atomic,
        max(1, state.remaining_qty_atomic * decision.fraction_ppm
            // FRACTION_PPM_SCALE),
    )
    try:
        economics = quote_fn(_exit_request(entry, requested, now_iso))
    except (QuoteDeferred, QuoteRejected):
        # An unfilled action consumes no tranche budget and reduces no inventory.
        _append_state(conn, entry, sequence + 1, decision.state_if_unfilled, now_iso)
        return "unfilled"

    scored = _scored_out_atomic(int(economics["expected_out_atomic"]))
    _append_action(
        conn, entry, sequence + 1, decision,
        quantity_atomic=requested, expected_out_atomic=economics["expected_out_atomic"],
        scored_out_atomic=scored, observed_at=now_iso)
    filled = decision.state_if_filled
    _append_state(conn, entry, sequence + 1, filled, now_iso)
    if filled.remaining_qty_atomic > 0:
        return "derisked"

    total = _sum_scored_proceeds(conn, entry)
    _append_outcome_v3(
        conn, entry=entry, status="valid",
        result_code=_result_code(decision.reason_codes),
        return_bps=_return_bps(total, entry["quote_in_atomic"]),
        exit_floor_atomic=total,
        origin_kind=ORIGIN_BY_VENUE[entry["venue_id"]],
        policy_hash=decision.policy_hash,
        decision_facts_json=canonical_json(dict(observation.payload)),
        exit_reason_codes_json=canonical_json(list(decision.reason_codes)),
        outcome_at=now_iso)
    _schedule_followups(conn, entry, terminal_at=now_iso)
    return "completed"
```

`_schedule_followups` writes one `op_paper_exit_followups` row per horizon with
`status='scheduled'`. A separate pass resolves a due row. That pass writes only to
`op_paper_exit_followups`. It must not touch `op_paper_position_states`,
`op_paper_position_actions`, or `op_paper_outcomes`.

Write the rest of `paper_lifecycle_v3.py` on the shape of `paper_lifecycle.py`. The
differences from v2 are:

1. `LIFECYCLE_V3 = "radar-adaptive-v3"`.
2. `ADAPTIVE_ARMS = (*ADAPTIVE_V3_RULES, *PAPER_RULE_BY_VENUE.values())` — the v3 arms
   and the current per-venue rules, which serve as the matched controls.
3. Per open entry, load the latest `op_paper_position_states` row, or start a fresh
   `V6PositionState` from the entry receipt.
4. Build a `V6Observation` with `make_observation`. The market lane identity is the
   canonical event id; the quote lane identity is the `op_paper_exit_observations`
   `observation_id`.
5. Call `step(state, observation, policy=POLICY_V3)`.
6. `HOLD` appends only a state row.
7. `DERISK` or `EXIT_FULL` quotes the exact tranche quantity, appends one
   `op_paper_position_actions` row and one state row from `decision.state_if_filled`.
   A rejected quote appends the state row from `decision.state_if_unfilled`.
8. A terminal action appends the `op_paper_outcomes` row with the summed return,
   `origin_kind`, `policy_hash`, `decision_facts_json`, and `exit_reason_codes_json`.
9. After the terminal row, schedule follow-up quotes at 30, 120, 300, and 900 seconds.
10. `adaptive_result_summary` returns `by_venue` and `by_arm` only. It never returns a
    mean across venues and never across lifecycles.

Map the terminal reason to `result_code`:

| reason code | `result_code` |
|---|---|
| `trail` | `trail_exit` |
| `creator_sale` | `creator_exit` |
| `exact_stop` | `stop_first` |
| `time_cap`, `time_cap:hard` | `time_exit` |
| every other terminal reason | `adaptive_exit` |

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_adaptive_lifecycle.py tests/test_radar_store_schema_14.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/paper_lifecycle_v3.py tests/test_radar_adaptive_lifecycle.py
git commit -m "feat(radar): drive the v3 adaptive exit policy on its own lifecycle cohort"
```

---

### Task 10: Repair the Pons quote binding

**Files:**
- Modify: `sniper/research/memecoin_opportunity/evm_probe.py:16-21,123-148,260-284`
- Modify: `sniper/research/memecoin_opportunity/engine.py:639-724`
- Test: `tests/test_radar_pons_quote_binding.py`

**Interfaces:**
- Produces: `RemoteRoundTripProbe` gains `requested_state_fingerprint: str | None` and
  `requested_block_identity: str | None`. `observe_remote_completion` compares against the
  requesting snapshot, not `self.last_observations[launch_id]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_pons_quote_binding.py
def test_a_later_normal_trade_does_not_discard_a_completed_quote(engine, pons_launch):
    """The defect: `point` is the LATEST observation, so any trade that lands while the
    background quote runs makes the completion fail its own fingerprint check."""
    requested = engine.last_observations[pons_launch].state_fingerprint
    engine.observe(normal_trade_event(pons_launch))          # `point` advances
    assert engine.last_observations[pons_launch].state_fingerprint != requested
    completion = pons_completion(
        pons_launch, requested_state_fingerprint=requested)
    assert engine.observe_remote_completion(
        completion, observed_at="2026-08-11T00:00:20Z") is not None


def test_a_creator_sale_after_the_request_rejects(engine, pons_launch):
    requested = engine.last_observations[pons_launch].state_fingerprint
    engine.observe(creator_sale_event(pons_launch))
    completion = pons_completion(
        pons_launch, requested_state_fingerprint=requested)
    assert engine.observe_remote_completion(
        completion, observed_at="2026-08-11T00:00:20Z") is None


def test_a_route_change_after_the_request_rejects(engine, pons_launch):
    requested = engine.last_observations[pons_launch].state_fingerprint
    engine.observe(pool_change_event(pons_launch))
    completion = pons_completion(
        pons_launch, requested_state_fingerprint=requested)
    assert engine.observe_remote_completion(
        completion, observed_at="2026-08-11T00:00:20Z") is None


def test_a_material_lp_loss_after_the_request_rejects(engine, pons_launch):
    requested = engine.last_observations[pons_launch].state_fingerprint
    engine.observe(liquidity_pull_event(pons_launch, removed_bps=600))
    completion = pons_completion(
        pons_launch, requested_state_fingerprint=requested)
    assert engine.observe_remote_completion(
        completion, observed_at="2026-08-11T00:00:20Z") is None


def test_an_over_age_completion_rejects(engine, pons_launch):
    requested = engine.last_observations[pons_launch].state_fingerprint
    completion = pons_completion(
        pons_launch, requested_state_fingerprint=requested,
        completed_at="2026-08-11T00:00:00Z")
    assert engine.observe_remote_completion(
        completion, observed_at="2026-08-11T00:10:00Z") is None


def test_the_probe_carries_the_requesting_identity_back():
    from sniper.research.memecoin_opportunity.evm_probe import OpportunityEvmProbe
    probe = OpportunityEvmProbe({}, start_worker=False)
    probe(fake_state(), fake_quote(state_fingerprint="fp-1"))
    probe.run_one()
    result = probe.drain_completions()[0]
    assert result.requested_state_fingerprint == "fp-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_pons_quote_binding.py -q`
Expected: FAIL — the first test returns `None`, and `requested_state_fingerprint` does not
exist.

- [ ] **Step 3: Write minimal implementation**

1. Add `requested_state_fingerprint` and `requested_block_identity` to
   `RemoteRoundTripProbe`, both defaulting to `None`.
2. In `OpportunityEvmProbe._blocked` and `_execute`, set both fields from the task.
3. In `observe_remote_completion`, replace the fingerprint comparison:

```python
    requested_fingerprint = (
        result.requested_state_fingerprint or result.state_fingerprint)
    requested_point = self.observation_by_fingerprint.get(requested_fingerprint)
    ...
        or (
            result.provider in {"bags_curve_eth_call_v2", "pons_v3_quoter_v1"}
            and (result.quote_mint != state.quote_mint
                 or requested_point is None)
        )
```

4. Add `self.observation_by_fingerprint`, a bounded `OrderedDict` of the last 256
   observations per engine, written wherever `self.last_observations` is written.
5. Reject the completion when any of the four conditions in spec section 7.3 holds
   between `requested_point` and the current state:

```python
    removed = (
        liquidity_removed_fraction_bps(state, requested_point)
        if requested_point is not None else None
    )
    if requested_point is not None and (
        state.creator_sale_count > requested_point.creator_sale_count
        or state.pool_id != requested_point.pool_id
        or removed is None
        or int(removed) >= 500
    ):
        return None
```

6. Change the time check to compare against the requesting observation:
   `if _parse(completed) < _parse(requested_point.observed_at): return None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_pons_quote_binding.py tests/test_memecoin_opportunity_engine.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/evm_probe.py sniper/research/memecoin_opportunity/engine.py tests/test_radar_pons_quote_binding.py
git commit -m "fix(radar): bind an EVM quote completion to the snapshot that requested it"
```

---

### Task 11: Separate the reports

**Files:**
- Modify: `sniper/research/memecoin_opportunity/paper_lifecycle.py:398-511`
- Modify: `sniper/routes/api_memecoin_opportunity.py:110-135,280-295`
- Test: `tests/test_radar_result_separation.py`

**Interfaces:**
- Consumes: `adaptive_result_summary` from Task 9.
- Produces: `realistic_result_summary(conn, *, lifecycle_version=LIFECYCLE_VERSION)`. The
  API payload key `paper_results` becomes
  `{"radar-realistic-v2": {...}, "radar-adaptive-v3": {...}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_result_separation.py
def test_the_payload_holds_one_block_per_lifecycle(client, radar_with_both_cohorts):
    payload = client.get("/api/memecoin-opportunity/status").get_json()
    assert set(payload["paper_results"]) == {
        "radar-realistic-v2", "radar-adaptive-v3"}


def test_no_block_pools_two_lifecycles(client, radar_with_both_cohorts):
    payload = client.get("/api/memecoin-opportunity/status").get_json()
    v2 = payload["paper_results"]["radar-realistic-v2"]
    v3 = payload["paper_results"]["radar-adaptive-v3"]
    assert v2["valid_results"] + v3["valid_results"] == 3
    assert v2["valid_results"] == 2 and v3["valid_results"] == 1


def test_no_block_pools_two_venues(client, radar_with_both_cohorts):
    payload = client.get("/api/memecoin-opportunity/status").get_json()
    for block in payload["paper_results"].values():
        assert set(block["by_venue"]) == {
            "pumpfun_solana", "bags_robinhood_4663", "pons_v1_4663"}


def test_the_copy_source_cohort_is_absent(client, radar_with_both_cohorts):
    payload = client.get("/api/memecoin-opportunity/status").get_json()
    assert "global_feed" not in str(payload["paper_results"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_result_separation.py -q`
Expected: FAIL with `KeyError: 'radar-adaptive-v3'`

- [ ] **Step 3: Write minimal implementation**

Give `realistic_result_summary` a `lifecycle_version` keyword and thread it through every
query in the function body. In `api_memecoin_opportunity.py`:

```python
            payload["paper_results"] = {
                LIFECYCLE_VERSION: realistic_result_summary(conn),
                LIFECYCLE_V3: adaptive_result_summary(conn),
            }
```

Update the empty-state block at line 110 to the same two-key shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_result_separation.py tests/test_memecoin_opportunity_projection.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/paper_lifecycle.py sniper/routes/api_memecoin_opportunity.py tests/test_radar_result_separation.py
git commit -m "feat(radar): report one result block per lifecycle and never pool venues"
```

---

### Task 12: Wire the v3 arm into the worker

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py:900-1100`
- Test: `tests/test_radar_v3_worker_wiring.py`

**Interfaces:**
- Consumes: every earlier task.
- Produces: the worker holds one `FrenzyCeiling`, calls `evaluate_adaptive_v3`, and drives
  `drive_adaptive_lifecycle` on each cycle.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_v3_worker_wiring.py
def test_the_engine_feeds_the_frenzy_ceiling_and_the_quote_age(engine, snapshot_stream):
    for snapshot in snapshot_stream:
        engine.observe(snapshot)
    features = engine.last_interval_features[snapshot_stream[-1].launch_id]
    assert "frenzy_ceiling_atomic_per_s2" in features
    assert "quote_age_ms" in features
    assert "liquidity_removed_fraction_bps" in features


def test_the_worker_cycle_drives_both_lifecycles(worker_runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.memecoin_opportunity_worker.drive_paper_lifecycle",
        lambda *a, **k: calls.append("v2") or {})
    monkeypatch.setattr(
        "scripts.memecoin_opportunity_worker.drive_adaptive_lifecycle",
        lambda *a, **k: calls.append("v3") or {})
    worker_runtime.run_paper_cycle()
    assert calls == ["v2", "v3"]


def test_a_v3_alert_writes_a_fill_request_with_its_own_rule_version(worker_runtime):
    request = worker_runtime.conn.execute(
        "SELECT rule_version FROM op_paper_fill_requests "
        "WHERE rule_version='pump-adaptive-v3'").fetchone()
    assert request is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_radar_v3_worker_wiring.py -q`
Expected: FAIL with `KeyError: 'frenzy_ceiling_atomic_per_s2'`

- [ ] **Step 3: Write minimal implementation**

1. Construct one `FrenzyCeiling` on the engine.
2. In the interval-feature path, call `observe` with the venue, the launch age, and the
   acceleration, then read `ceiling` back into
   `current["frenzy_ceiling_atomic_per_s2"]`. Observe BEFORE the read, so a launch can
   contribute to its own bucket only through earlier launches, never through its own
   later value.
3. Put `quote_age_ms` into `current` at the decision site, from the `age_ms` value that
   `_alert_features` already receives.
4. Add `evaluate_adaptive_v3` to the arm list at `engine.py:978-1004`, next to
   `evaluate_confirmation_v6`.
5. Add `ADAPTIVE_V3_RULES` to `_paper_cohort_eligible`, so a v3 arm always joins.
6. In the worker paper cycle, call `activate_adaptive_lifecycle` once at start, then
   `drive_paper_lifecycle` and `drive_adaptive_lifecycle` in that order.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_radar_v3_worker_wiring.py tests/test_memecoin_opportunity_engine.py tests/test_memecoin_v6_radar_isolation.py -q`
Expected: PASS

- [ ] **Step 5: Run the full touched surface and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_policy_objects.py tests/test_memecoin_v6_policy_v3.py \
  tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_db.py \
  tests/test_radar_v3_entry_features.py tests/test_radar_v3_entry_rule.py \
  tests/test_radar_store_schema_14.py tests/test_radar_adaptive_lifecycle.py \
  tests/test_radar_pons_quote_binding.py tests/test_radar_result_separation.py \
  tests/test_radar_v3_worker_wiring.py tests/test_memecoin_opportunity_engine.py -q
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate
git add -A
git commit -m "feat(radar): activate the v3 adaptive arm and its matched control cohort"
```

---

## Deployment

1. Back up `data/memecoin_opportunity.db` before the first run at schema 14.
2. Restart the opportunity worker. The migration runs on connect.
3. Confirm one `op_paper_lifecycle_activations` row for `radar-adaptive-v3`.
4. Confirm the `radar-realistic-v2` row count is unchanged at 217.
5. Watch the v3 fire rate for 24 hours. Below about 3 fires a day, widen the age window
   before the clock accrues, and record the change as a new rule version.

## Do not read the result early

The v3 arm needs about 30 to 50 valid results per venue before any verdict. Do not read a
mean before that count. The pre-registered comparison is the pair, never the arm alone:

1. v3 entry against the baseline entry, both on `radar-adaptive-v3`;
2. the baseline entry on `radar-adaptive-v3` against the same entry on
   `radar-realistic-v2`.
