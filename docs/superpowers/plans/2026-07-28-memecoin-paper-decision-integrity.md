# Memecoin Paper Decision Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the legacy Memecoin Algo Paper tab collect immutable forward evidence and render only honest exact-event comparisons without changing funded or V6 authority.

**Architecture:** Add immutable paper cohort fields to the existing position ledger, freeze every arm's entry-time policy, and project the ledger through one read-only report service. The UI consumes that projection at bounded cadence; the existing runner remains the only paper writer.

**Tech Stack:** Python 3.13, SQLite, Flask, vanilla JavaScript, pytest.

## Global Constraints

- Paper remains measurement-only and gains no signing, reservation, broadcast, or funded authority.
- V6 paper and funded entry/exit behavior are out of scope.
- Existing historical rows remain `legacy_unversioned`; never fabricate a backfill.
- Unknown quote identity, route, and age remain null.
- Use additive SQLite columns and existing dependencies only.
- TDD every behavioral change; one logical commit per task.

---

### Task 1: Immutable arm and cohort contracts

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_policy.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Test: `tests/test_memecoin_paper_policy_contract.py`
- Test: `tests/test_memecoin_observation_schema.py`

**Interfaces:**
- Produces: `resolve_paper_arm_policy(strategy, entry_mcap, network_id, opened_at)` returning `(policy_id, version, canonical_json)`.
- Produces: `paper_policy_value(position, name, fallback)` for legacy-safe exit reads.
- Produces: `build_paper_cohort(arms, assignments, code_sha)` returning `(cohort_id, canonical_json)`.
- Adds position fields `paper_cohort_id`, `paper_cohort_json`, and `experiment_status`.

- [ ] **Step 1: Write failing schema and policy tests**

```python
def test_every_active_paper_arm_resolves_a_canonical_frozen_policy():
    for arm in ("control", "momentum_a", "alpha_b", "real_shadow", "scalp_c2",
                "trail_d", "peak_lock_e", "combo_f"):
        policy_id, version, payload = resolve_paper_arm_policy(
            arm, entry_mcap=2_000_000, network_id="solana",
            opened_at="2026-07-28T00:00:00+00:00")
        assert policy_id and version == 1 and json.loads(payload)

def test_paper_cohort_hash_changes_when_policy_changes():
    first = build_paper_cohort(("control",), {"control": ("p", 1, '{"x":1}')}, "a" * 40)
    second = build_paper_cohort(("control",), {"control": ("p", 1, '{"x":2}')}, "a" * 40)
    assert first[0] != second[0]
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_observation_schema.py -q`

Expected: missing policy functions and schema fields.

- [ ] **Step 3: Add the minimal schema and canonical policy/cohort helpers**

Add the three columns through the existing additive migration loop. Resolve only settings each arm's current evaluator consumes. Canonicalize with `json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False)` and hash with `hashlib.sha256`.

- [ ] **Step 4: Fan out one resolved cohort and persist it on every automatic arm**

Resolve all arm assignments once after the shared entry quote. Pass the selected assignment and shared cohort into `_open_arm`. Set `eligible` for modeled DexScreener entries and `excluded_fill` for `fomo`; manual buys set `manual`.

- [ ] **Step 5: Run GREEN**

Run: `python3 -m pytest tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_observation_schema.py tests/test_memecoin_scalp_arm.py tests/test_memecoin_fanout.py -q`

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_policy.py \
  sniper/services/memecoin_copy_trader.py tests/test_memecoin_paper_policy_contract.py \
  tests/test_memecoin_observation_schema.py
git commit -m "fix(memecoin): freeze every paper arm cohort"
```

### Task 2: Frozen exits and entry provenance

**Files:**
- Modify: `sniper/services/memecoin_exits.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Test: `tests/test_memecoin_paper_policy_contract.py`
- Test: `tests/test_memecoin_fill_honesty.py`

**Interfaces:**
- Consumes: `paper_policy_value(position, name, fallback)`.
- Produces: frozen evaluation for every new eligible arm while retaining legacy fallbacks.
- Produces: populated source/quote/drift fields on paper positions and entry fills.

- [ ] **Step 1: Write failing frozen-setting and provenance tests**

```python
def test_control_eligible_position_keeps_entry_time_tp_after_setting_change(monkeypatch):
    c = _conn()
    monkeypatch.setattr(S, "MEMECOIN_TAKE_PROFIT_PCT", 50.0)
    policy_id, version, payload = resolve_paper_arm_policy(
        "control", entry_mcap=1_000_000, network_id="solana",
        opened_at="2026-07-28T00:00:00+00:00")
    pid = _open(c, "control", assigned_policy_id=policy_id,
                assigned_policy_version=version, assigned_policy_json=payload,
                experiment_status="eligible")
    monkeypatch.setattr(S, "MEMECOIN_TAKE_PROFIT_PCT", 5.0)
    sweep_exits(c, {"M": {"price": 1.10, "liquidity": 1e9}},
                "2026-07-28T00:00:15+00:00")
    assert m.get_position(c, pid)["is_open"] == 1

def test_paper_entry_persists_observed_provenance_without_fabricating_quote_identity():
    c = _conn()
    copy_event_to_arms(c, _event(their_price=1.0), 100.0,
                       "2026-07-28T00:00:00+00:00", arms=("control",),
                       stats_fn=lambda *_a, **_k: {"price": 1.02, "liquidity": 10_000.0})
    p = m.list_open_positions(c)[0]
    assert p["their_price"] == 1.0 and p["quote_price"] == 1.02
    assert p["source_drift_pct"] is not None
    assert p["execution_slippage_pct"] is not None
    assert p["quote_id"] is None and p["quote_route_target"] is None
    assert p["quote_age_ms"] is None
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_fill_honesty.py -q`

Expected: current settings close the frozen test and paper provenance fields are null.

- [ ] **Step 3: Read frozen parameters in each paper exit branch**

Replace direct paper-setting reads with `paper_policy_value` for control, momentum, alpha, trail,
peak-lock, combo, uniform hold, and paper real-shadow thresholds. Keep direct settings as the fallback
argument for `legacy_unversioned` rows.

- [ ] **Step 4: Populate only observed entry provenance**

Pass source price, shared mid, attribution, source drift, modeled impact, origin, network, and signal
source into `open_position` and `insert_fill`. Leave route identity and quote age null.

- [ ] **Step 5: Run GREEN**

Run: `python3 -m pytest tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_fill_honesty.py tests/test_memecoin_exits.py tests/test_memecoin_brackets.py -q`

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_exits.py sniper/services/memecoin_copy_trader.py \
  tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_fill_honesty.py
git commit -m "fix(memecoin): preserve paper policy and quote provenance"
```

### Task 3: Honest single-pass Paper report

**Files:**
- Create: `sniper/services/memecoin_paper_report.py`
- Modify: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_paper_report.py`

**Interfaces:**
- Produces: `build_paper_report(conn, arms, chain=None, episode_limit=200, closed_limit=200) -> dict`.
- Produces: `GET /api/memecoin/paper-report?chain=<all|solana|base|robin|bsc|eth>`.

- [ ] **Step 1: Write failing report tests**

```python
def test_report_primary_uses_only_same_cohort_exact_events():
    c = _conn()
    for arm, pnl in (("control", 1.0), ("alpha_b", 3.0)):
        _closed(c, arm, event="E1", cohort="C1", pnl=pnl, status="eligible")
    _closed(c, "alpha_b", event="E2", cohort="C1", pnl=100.0, status="eligible")
    report = build_paper_report(c, arms=("control", "alpha_b"))
    assert report["forward"]["matched_events"] == 1
    assert {row["strategy"]: row["total_pnl"] for row in report["forward"]["arms"]} == {
        "control": 1.0, "alpha_b": 3.0,
    }

def test_report_censors_null_and_includes_booked_partial_in_open_pnl():
    c = _conn()
    _closed(c, "control", event="E0", cohort="C1", pnl=None, status="eligible")
    pid = _open(c, "control", event="E1", cohort="C1", status="eligible")
    m.insert_fill(c, position_id=pid, kind="partial_exit", price=2.0, qty=40.0,
                  notional=80.0, fill_source="dexscreener", fraction=0.4, created_at="t2")
    m.update_position_last_quote(c, pid, 1.5, "2026-07-28T00:01:00+00:00")
    report = build_paper_report(c, arms=("control",))
    assert report["coverage"]["censored_outcomes"] == 1
    assert report["forward"]["arms"][0]["total_pnl"] == 70.0

def test_report_chain_filter_scopes_every_aggregate():
    c = _conn()
    _closed(c, "control", event="SOL", cohort="C1", pnl=5.0,
            status="eligible", network_id="solana")
    _closed(c, "control", event="ROB", cohort="C1", pnl=-5.0,
            status="eligible", network_id="4663")
    report = build_paper_report(c, arms=("control",), chain="robin")
    assert report["coverage"]["positions"] == 1
    assert report["forward"]["arms"][0]["total_pnl"] == -5.0
    assert {row["network_id"] for row in report["closed"]} == {"4663"}

def test_report_returns_all_companion_arms_for_recent_episode():
    c = _conn()
    _open(c, "control", event="TARGET", cohort="C1", status="eligible")
    _closed(c, "alpha_b", event="TARGET", cohort="C1", pnl=2.0, status="eligible")
    for index in range(205):
        _closed(c, "control", event=f"OTHER-{index}", cohort="C1", pnl=0.0,
                status="eligible", closed_at=f"2026-07-28T01:{index % 60:02d}:00+00:00")
    report = build_paper_report(c, arms=("control", "alpha_b"), episode_limit=250)
    target = next(row for row in report["episodes"] if row["origin_event_id"] == "TARGET")
    assert {arm["strategy"] for arm in target["arms"]} == {"control", "alpha_b"}
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_paper_report.py -q`

Expected: report module missing.

- [ ] **Step 3: Implement one projected position/fill query and pure aggregation**

Group by `(paper_cohort_id, origin_event_id)` for primary results and `origin_event_id` for legacy
diagnostics. Match only when the configured arm set is complete after exclusion. Derive percentiles
with `statistics` and literal interpolation; do not add NumPy.

- [ ] **Step 4: Add the read-only route with validated chain input**

Reject unknown chain values with HTTP 400. Return the report directly; do not call pricing services.

- [ ] **Step 5: Run GREEN**

Run: `python3 -m pytest tests/test_memecoin_paper_report.py tests/test_memecoin_api.py -q`

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_paper_report.py sniper/routes/api_memecoin.py \
  tests/test_memecoin_paper_report.py
git commit -m "feat(memecoin): add honest paired paper report"
```

### Task 4: DB-only open marks and operator isolation

**Files:**
- Modify: `sniper/services/memecoin_marks.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_api.py`
- Test: `tests/test_memecoin_partial_sell.py`
- Test: `tests/test_memecoin_manual_close_mode_guard.py`

**Interfaces:**
- Produces: `stored_position_marks(conn, positions, now_iso) -> list[dict]` with one quantity query.
- Produces: `set_experiment_status(conn, position_id, status)`.

- [ ] **Step 1: Write failing endpoint and manual-isolation tests**

```python
def test_open_positions_uses_stored_mark_when_live_pricer_raises(monkeypatch):
    monkeypatch.setattr(solana_quote, "dexscreener_stats_batch_keyed", raising_pricer)
    assert client.get("/api/memecoin/positions/open").status_code == 200

def test_manual_paper_partial_marks_position_operator_modified():
    response = client.post("/api/memecoin/positions/2/sell", json={"fraction": 0.5})
    assert response.status_code == 200
    assert get_position(conn, 2)["experiment_status"] == "operator_modified"
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_api.py tests/test_memecoin_partial_sell.py tests/test_memecoin_manual_close_mode_guard.py -q`

Expected: live pricer is called and experiment status does not change.

- [ ] **Step 3: Replace open-route pricing with persisted marks**

Compute all open quantities in one grouped fill query. Mark quote age and stale state from
`last_quote_ts`; never issue an outbound request.

- [ ] **Step 4: Mark paper operator mutations before booking fills**

Manual close and partial routes set `operator_modified`; manual buys persist `manual`. Real routes are
unchanged.

- [ ] **Step 5: Run GREEN and commit**

Run: `python3 -m pytest tests/test_memecoin_api.py tests/test_memecoin_partial_sell.py tests/test_memecoin_manual_close_mode_guard.py -q`

```bash
git add sniper/services/memecoin_marks.py sniper/persistence/db_memecoin.py \
  sniper/routes/api_memecoin.py tests/test_memecoin_api.py tests/test_memecoin_partial_sell.py \
  tests/test_memecoin_manual_close_mode_guard.py
git commit -m "fix(memecoin): make paper reads local and isolate manual rows"
```

### Task 5: Paper UI truth and bounded polling

**Files:**
- Modify: `static/app.js`
- Modify: `tests/test_memecoin_ui.py`
- Modify: `tests/test_memecoin_ui_poll_stability.py`

**Interfaces:**
- Consumes: `/api/memecoin/paper-report` and persisted `/positions/open` marks.
- Produces: Strategy Lab and Closed views with forward/legacy separation and scoped coverage.

- [ ] **Step 1: Write failing UI contract tests**

Add assertions that the Paper loaders use `/paper-report`, no longer use `arm.rows`, remove manual
controls from Strategy Lab, render `Forward matched cohort` and `Legacy matched diagnostic`, and use
15s/30s Paper cadence with an in-flight guard.

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_ui.py tests/test_memecoin_ui_poll_stability.py -q`

Expected: old routes, controls, labels and cadence remain.

- [ ] **Step 3: Render the report and remove misleading surfaces**

Generate arm filters from `report.arms_configured`, render coverage before outcomes, show full-book
PnL and stale age, rename the peak proxy, and keep complete recent episodes including fully closed.

- [ ] **Step 4: Bound Paper polling**

Keep live surfaces at four seconds. Gate Strategy Lab at 15 seconds and Closed at 30 seconds. Make
`_loadSection` return and track its promise; chain changes reload only the active section.

- [ ] **Step 5: Run GREEN and commit**

Run: `python3 -m pytest tests/test_memecoin_ui.py tests/test_memecoin_ui_poll_stability.py -q && node --check static/app.js`

```bash
git add static/app.js tests/test_memecoin_ui.py tests/test_memecoin_ui_poll_stability.py
git commit -m "fix(ui): make paper decisions honest and bounded"
```

### Task 6: Verification, knowledge sync, and pinned rollout

**Files:**
- Verify: `docs/superpowers/specs/2026-07-28-memecoin-paper-decision-integrity-design.md`
- Verify: `docs/superpowers/plans/2026-07-28-memecoin-paper-decision-integrity.md`

**Interfaces:**
- Produces: merged commit on `origin/main`, immutable memecoin/web releases, and live evidence.

- [ ] **Step 1: Run focused and broad verification**

```bash
python3 scripts/validate_schema_refs.py --warn-unknown-tables
python3 -m pytest tests/test_memecoin_paper_policy_contract.py tests/test_memecoin_paper_report.py \
  tests/test_memecoin_strategy_summary.py tests/test_memecoin_api.py tests/test_memecoin_ui.py \
  tests/test_memecoin_ui_poll_stability.py -q
python3 -m pytest $(find tests -maxdepth 1 -name 'test_memecoin*.py' -print | sort) -q
python3 -m compileall -q sniper scripts/memecoin_runner_loop.py
node --check static/app.js
git diff --check
```

- [ ] **Step 2: Run read-only current-ledger report checks**

Use `sqlite3` URI `mode=ro` and `PRAGMA query_only=ON`. Confirm report latency, chain scoping,
exclusion counts, no writes, and current legacy matched totals.

- [ ] **Step 3: Complete the Knowledge Sync Gate**

The new spec and plan are the knowledge delta. The PR body ends with the repository's 11-question
Knowledge Delta Report and states funded/V6 authority unchanged.

- [ ] **Step 4: Push, open PR, pass checks, and merge**

Never push directly to `origin/main`. Merge only the reviewed branch after checks pass.

- [ ] **Step 5: Deploy exact merged SHA**

Run the repository's immutable memecoin deploy script with normal validation. Deploy the web server
from an immutable release path using the current runtime ownership pattern. Do not activate rollover
or change funded environment settings.

- [ ] **Step 6: Verify live state**

Check runtime symlinks, process count/commands, authenticated health, pending reservations, kill
switch, fresh heartbeat, Paper endpoint latency/payload, DB-only open route behavior, and browser
Strategy Lab/Closed rendering at desktop and mobile widths.
