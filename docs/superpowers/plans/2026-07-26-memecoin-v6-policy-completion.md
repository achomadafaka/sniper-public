# Memecoin V6 Policy Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put LP loss, executable catastrophe, persistent route loss, time expiry, exact source exit, and staged break management inside one V6 rule evaluator with per-rule evidence contracts.

**Architecture:** Normalize quote and source outcomes before policy evaluation, have each V6 rule return a typed triggered/not-triggered/insufficient result, then resolve candidates by fixed precedence inside `step()`. Keep policy intent separate from paper fill availability so V6 can demand an exit even when the exact route is currently absent.

**Tech Stack:** Python 3 dataclasses/enums, Decimal/integer quote economics, SQLite, pytest.

## Global Constraints

- Plan A (`2026-07-26-memecoin-v6-engine-kernel-persistence.md`) must be complete and green first.
- The one runtime policy identity remains `v6-position-engine-v1`; every rule constant and precedence entry is included in `policy_hash`.
- Missing evidence abstains per rule; there is no global required-evidence gate.
- Terminal precedence is `EXIT_FULL` over `DERISK`; otherwise the largest de-risk fraction wins.
- Catastrophe is an 80% executable loss from modeled entry price. Time expiry is 43,200 seconds.
- Persistent route loss requires four distinct `route_absent` checks spanning at least 60 seconds.
- Provider unavailable and stale quote outcomes never count as route absence.
- No fixed take-profit or trailing rule is added.
- V6 remains default OFF. No live calls, production writes, funded changes, activation, deploy, or Supabase write.

---

## File map

- Modify `sniper/services/memecoin_v6_paper_quotes.py`: typed quote outcome and failure taxonomy.
- Modify `sniper/services/memecoin_v6_observations.py`: exact entry/current economics, structured route
  facts, trusted LP provenance, exact source evidence.
- Rewrite runtime-facing parts of `sniper/services/memecoin_v6_policy.py`: typed rule results and
  V6-native rules; historical replay is not a runtime consumer.
- Modify `sniper/services/memecoin_v6_engine.py`: rule collection, precedence, deterministic thought,
  policy document/hash.
- Modify `sniper/services/memecoin_v6_paper_runner.py`: exact source-full-exit enrichment and intent
  retry schedule.
- Modify `sniper/persistence/db_memecoin_v6_paper.py`: route-check and retry timestamps on the active
  intent/state.
- Modify `sniper/services/memecoin_v6_paper_report.py`: per-rule results and intent status.
- Test the changes in the matching V6 test modules.

### Task 1: Normalize executable quote outcomes

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_quotes.py:31-70,216-355`
- Modify: `sniper/services/memecoin_v6_observations.py:254-350`
- Test: `tests/test_memecoin_v6_paper_quotes.py`
- Test: `tests/test_memecoin_v6_observations.py`

**Interfaces:**
- Produces: `QuoteStatus`, `QuoteFailure`, and
  `quote_exit_outcome(position, qty_atomic, ...) -> ModeledLiveFill | QuoteFailure`.
- Consumes: semantic quote identity from Plan A.

- [ ] **Step 1: Write failing taxonomy tests**

```python
@pytest.mark.parametrize("status", [
    QuoteStatus.ROUTE_ABSENT,
    QuoteStatus.PROVIDER_UNAVAILABLE,
    QuoteStatus.STALE,
])
def test_quote_failure_has_stable_semantic_identity(status):
    first = QuoteFailure(status=status, provider="jupiter", observed_at=NOW,
                         detail_code="no_route")
    second = QuoteFailure(status=status, provider="jupiter", observed_at=NOW,
                          detail_code="no_route")
    assert first.evidence_id == second.evidence_id

def test_provider_timeout_is_not_route_absent():
    outcome = quote_exit_outcome(_position(), 100, adapter=lambda *_: (_ for _ in ()).throw(TimeoutError()))
    assert outcome.status is QuoteStatus.PROVIDER_UNAVAILABLE
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_observations.py -q`

Expected: FAIL because all quote failures currently collapse to `QuoteUnavailable`/`route_degraded`.

- [ ] **Step 3: Implement the exact taxonomy**

```python
class QuoteStatus(str, Enum):
    HEALTHY = "healthy"
    ROUTE_ABSENT = "route_absent"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    STALE = "stale"

@dataclass(frozen=True)
class QuoteFailure:
    status: QuoteStatus
    provider: str
    observed_at: str
    detail_code: str
    evidence_id: str = field(init=False)
```

Map a successful provider/RPC response with no supported route to `ROUTE_ABSENT`; timeouts, rate
limits, transport errors, and malformed provider responses to `PROVIDER_UNAVAILABLE`; over-age
otherwise-valid responses to `STALE`. Preserve the existing exception API for entry quoting, which
still opens no position on any failure.

Observation payload must carry `quote_status`, `quote_failure_code`, and quote evidence identity.
Keep compatibility fields `sellable` and `route_degraded` as diagnostics until all Plan 3 consumers
move, but V6 rules use `quote_status` only.

- [ ] **Step 4: Run quote/observation tests**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_observations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_paper_quotes.py sniper/services/memecoin_v6_observations.py tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_observations.py
git commit -m "feat: classify v6 executable quote outcomes"
```

### Task 2: Replace the global gate with typed per-rule evidence

**Files:**
- Modify: `sniper/services/memecoin_v6_policy.py`
- Modify: `sniper/services/memecoin_v6_engine.py:120-310`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Produces: `RuleStatus`, `V6RuleResult`, `evaluate_rules(state, observation) -> tuple[V6RuleResult, ...]`,
  and `resolve_rule_results(results) -> tuple[V6Action, int, tuple[str, ...]]`.
- Consumes: bounded state and structured quote status.

- [ ] **Step 1: Write failing per-rule evidence tests**

```python
def test_missing_chain_p90_does_not_block_lp_rule():
    decision = step(_state(), _observation(chain_vol_p90=None, executable_price=None,
                                           depth_pull_flag=True, tape_complete=True,
                                           depth_provenance="trusted"))
    assert decision.action is V6Action.EXIT_FULL
    assert "lp_loss" in decision.reason_codes
    assert "volatility:insufficient" in decision.reason_codes

def test_all_rules_report_their_status():
    decision = step(_state(), _observation())
    assert {r.rule_id for r in decision.rule_results} == {
        "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break"
    }
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -k 'rule or chain_p90' -q`

Expected: FAIL because `_missing_capture` currently returns a global HOLD.

- [ ] **Step 3: Implement typed rule results**

```python
class RuleStatus(str, Enum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    INSUFFICIENT = "insufficient"

@dataclass(frozen=True)
class V6RuleResult:
    rule_id: str
    status: RuleStatus
    action: V6Action = V6Action.HOLD
    fraction_ppm: int = 0
    priority: int = 0
    reason_codes: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
```

Delete the engine-wide `REQUIRED_ENGINE_EVIDENCE`/`_missing_capture` decision gate. Validate value
types globally, but let each rule name only its own missing inputs. `resolve_rule_results` chooses
full exit first, then largest de-risk, and renders insufficient codes without changing a supported
action.

- [ ] **Step 4: Run engine tests**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy.py tests/test_memecoin_v6_engine.py
git commit -m "refactor: evaluate v6 evidence per rule"
```

### Task 3: Add V6 terminal decision rules

**Files:**
- Modify: `sniper/services/memecoin_v6_policy.py`
- Modify: `sniper/services/memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Produces: `evaluate_lp_loss`, `evaluate_catastrophe`, `evaluate_route_loss`, and
  `evaluate_time_expiry`, each returning `V6RuleResult`.
- Consumes: `RuleStatus`, bounded route state, immutable entry price, current exact quote, held time.

- [ ] **Step 1: Write terminal-rule RED tests**

```python
def test_trusted_lp_loss_exits_without_quote():
    d = step(_state(), _observation(depth_pull_flag=True, tape_complete=True,
                                    depth_provenance="trusted", executable_price=None))
    assert (d.action, d.fraction_ppm) == (V6Action.EXIT_FULL, 1_000_000)

def test_executable_catastrophe_uses_entry_not_chart_price():
    state = _state(entry_unit_price="1.0")
    d = step(state, _observation(executable_price=0.2, price=0.9, quote_status="healthy"))
    assert d.action is V6Action.EXIT_FULL

def test_route_loss_requires_four_checks_and_sixty_seconds():
    state = _route_absent_state(checks=3, first_at=NOW)
    assert step(state, _route_absent_observation(at=plus_s(NOW, 59))).action is V6Action.HOLD
    state = _route_absent_state(checks=3, first_at=NOW)
    assert step(state, _route_absent_observation(at=plus_s(NOW, 60))).action is V6Action.EXIT_FULL

def test_time_expiry_is_inside_v6_and_can_be_unfilled():
    d = step(_state(), _observation(held_s=43_200, executable_price=None))
    assert d.action is V6Action.EXIT_FULL
    assert d.state_if_unfilled.remaining_qty_atomic == d.state_if_filled.remaining_qty_atomic + _requested(d)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -k 'lp_loss or catastrophe or route_loss or time_expiry' -q`

Expected: FAIL because these observations are diagnostic-only or globally blocked.

- [ ] **Step 3: Implement exact terminal constants and provenance checks**

```python
CATASTROPHE_LOSS_PCT = 80.0
TIME_EXPIRY_S = 43_200.0
ROUTE_ABSENT_MIN_CHECKS = 4
ROUTE_ABSENT_MIN_S = 60.0
```

LP loss requires `depth_pull_flag is True`, `tape_complete is True`, supported capture version,
trusted depth provenance, and a market evidence id. Catastrophe requires a fresh healthy exact
whole-bag quote in the entry quote asset and compares `Decimal(current) <= Decimal(entry) * 0.20`.
Route absence advances only for distinct quote identities with status `route_absent`; healthy resets
it, provider/stale neither increments nor resets. Time expiry derives from fill time/current corrected
time and accepts no caller action.

- [ ] **Step 4: Freeze terminal policy document and precedence**

Add all four constants, supported provenance/capture versions, and the ordered rule list to
`policy_document()`. Test that changing any copied document value changes `fingerprint(document)`.

- [ ] **Step 5: Run engine tests**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_v6_engine.py sniper/services/memecoin_v6_policy.py tests/test_memecoin_v6_engine.py
git commit -m "feat: add v6 terminal decision rules"
```

### Task 4: Complete staged-break and exact-source semantics

**Files:**
- Modify: `sniper/services/memecoin_v6_policy.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py:61-186`
- Modify: `sniper/services/memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- Produces: `SourcePositionFact(event_id, status, remaining_atomic, observed_at)`,
  `exact_source_position_fact(...) -> SourcePositionFact | None`, and complete
  `evaluate_staged_break(...) -> V6RuleResult`.
- Consumes: Plan A break episodes and source evidence lane.

- [ ] **Step 1: Write source and staged-break RED tests**

```python
def test_source_partial_sell_is_not_full_exit(source_conn):
    _insert_source_sell(source_conn, amount_atomic=40, remaining_atomic=60, event_id="sell-1")
    fact = exact_source_position_fact(source_conn, "wallet", "mint", after=NOW)
    assert fact.status == "holding"

def test_exact_source_full_exit_confirms_second_tranche():
    first = step(_state(), _low_break("market-1")).state_if_filled
    d = step(first, _observation(source_status="exited", source_evidence_id="close-1"))
    assert d.action is V6Action.DERISK
    assert "source_full_exit" in d.reason_codes

def test_negative_streak_needs_two_distinct_market_ids():
    first = step(_state(), _low_break("market-1")).state_if_filled
    once = step(first, _negative_flow("market-2")).state_if_filled
    repeated = step(once, _negative_flow("market-2"))
    assert repeated.action is V6Action.HOLD
    assert step(repeated.state_if_filled, _negative_flow("market-3")).action is V6Action.DERISK
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_runner.py -k 'source or streak' -q`

Expected: FAIL because any sell/closed row currently becomes `source_wallet_exited=True`.

- [ ] **Step 3: Implement exact source fact extraction**

Read a stable source event id and explicit post-event remaining holding. Return `exited` only when an
explicit full-close event or exact zero remaining quantity is present after the V6 fill. Return
`holding` for partial sales. If exact remaining state is unavailable, return `None`; do not infer full
exit from side=`sell`.

- [ ] **Step 4: Complete the staged-break evaluator**

Keep constants `EARLY_S=10_800`, `FRENZY_RATE=0.10`, `CROWDED_SHARE=1/3`, `VOL_P=90`,
`VOL_MIN_OBS=12`, `MAX_TRANCHES=2`, and `NEG_STREAK_CONFIRM=2`. Optional score facts abstain
individually. Missing wallet prior leaves the base tier unchanged and adds
`wallet_prior_unavailable`. Route confirmation uses only the persistent route-loss trigger from
Task 3, not one failed quote.

- [ ] **Step 5: Run engine/runner tests**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_runner.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_v6_policy.py sniper/services/memecoin_v6_paper_runner.py sniper/services/memecoin_v6_observations.py tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_runner.py
git commit -m "fix: make v6 break confirmation evidence exact"
```

### Task 5: Persist retry schedule without quote storms

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py:187-320`
- Test: `tests/test_memecoin_v6_paper_db.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- Produces: `retry_delay_s(attempt_count) -> float`,
  `claim_retry(conn, intent_token, now) -> bool`, and persisted `attempt_count`,
  `last_attempt_at`, `next_attempt_at`, `last_quote_status`.
- Consumes: active intent from Plan A and typed quote outcomes from Task 1.

- [ ] **Step 1: Write retry RED tests**

```python
def test_retry_schedule_is_bounded():
    assert [retry_delay_s(i) for i in range(8)] == [0, 1, 2, 5, 10, 30, 30, 30]

def test_same_intent_at_10hz_does_not_quote_storm(tmp_path):
    clock = FakeClock()
    for _ in range(600):
        advance_position(v6, "p1", same_observation(), now=clock.now(), quote_fn=record_call)
        clock.advance(0.1)
    assert call_times == [0, 1, 3, 8, 18, 48]
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py -k 'retry or quote_storm' -q`

Expected: FAIL because each unfilled decision can generate a new token/quote.

- [ ] **Step 3: Implement atomic retry claims**

Use delays `(0, 1, 2, 5, 10, 30)` indexed by completed attempt count and cap later attempts at 30.
`claim_retry` performs a conditional update under `BEGIN IMMEDIATE`; only one worker can claim an
eligible retry. New evidence does not reset attempts. A healthy quote clears route absence; a
stronger superseding action creates its own schedule.

After quote return, re-read the active token before recording a fill. A superseded token cannot
write a fill.

- [ ] **Step 4: Run DB/runner tests**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_memecoin_v6_paper.py sniper/services/memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py
git commit -m "fix: bound v6 action quote retries"
```

### Task 6: Expose policy evidence and run the policy gate

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py:52-200`
- Modify: `tests/test_memecoin_v6_paper_report.py`
- Modify: `tests/test_memecoin_v6_paper_api.py`

**Interfaces:**
- Consumes: rule results, break episode, active intent, and structured quote status.
- Produces: read-only position/detail JSON fields `rule_results`, `break_episode`, `active_intent`,
  and `quote_status`.

- [ ] **Step 1: Write report/API contract tests**

```python
def test_detail_exposes_rule_evidence_and_active_intent(v6_conn):
    detail = report.position_detail(v6_conn, "p1")
    assert detail["latest_thought"]["rule_results"]["lp_loss"]["status"] in {
        "triggered", "not_triggered", "insufficient"
    }
    assert detail["active_intent"]["token"]
    assert detail["active_intent"]["next_attempt_at"]
```

- [ ] **Step 2: Run report/API tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py -q`

Expected: FAIL because these fields are not serialized.

- [ ] **Step 3: Add read-only serialization**

Return stable machine fields and deterministic thoughts. Do not expose full quote payloads,
credentials, RPC URLs, or funded controls. Preserve existing API top-level keys.

- [ ] **Step 4: Run Plan B regression gate**

Run: `python3 -m pytest tests/test_memecoin_v6_engine.py tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_observations.py tests/test_memecoin_v6_paper_db.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py tests/test_memecoin_v6_paper_worker.py -q`

Run: `python3 scripts/memecoin_v6_paper_worker.py --smoke-no-rpc --once`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Run: `git diff --check`

Expected: all pass; smoke performs no RPC and reports no funds.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py
git commit -m "feat: report complete v6 policy evidence"
```
