# Radar Confirmation Microscope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce fee- and quote-asset-correct Pump.fun Confirmation paper outcomes, compare immediate and delayed independent-demand variants, and expose the result through a bounded stable Radar UI.

**Architecture:** Extend immutable `LaunchState` with launch mechanics and independent-flow counters, then persist compact alert-time feature snapshots in the isolated opportunity ledger. Generate all shadow receipts from the one verified remote completion, compute exact due-time policy exits locally from captured finalized reserves, project paired policy aggregates, and patch keyed browser nodes without increasing RPC work.

**Tech Stack:** Python 3, SQLite, Flask JSON/SSE, vanilla JavaScript, pytest.

## Global Constraints

- Paper research only; no funded imports, signing, submission, reservations, positions, or sizing.
- New economic truth is forward-only under `opportunity-path-v3` and new rule versions.
- No new RPC call for features, shadow arms, or 60-second due computations.
- Compact endpoint stays below 150 KiB and exposes at most 16 recent receipts.
- Use TDD for every behavior change and keep commits scoped by task.

---

### Task 1: Quote asset, mode, and fee-aware launch state

**Files:**
- Modify: `sniper/research/memecoin_opportunity/state.py`
- Modify: `sniper/research/memecoin_opportunity/market_inputs.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Test: `tests/test_memecoin_opportunity_state.py`
- Test: `tests/test_memecoin_opportunity_settings.py`
- Test: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Produces: `LaunchState.quote_mint`, `mayhem_mode`, `cashback_enabled`, `effective_fee_bps`; `OpportunityEngine._market_input(state)`.
- Consumes: decoder facts `quote_mint`, `mayhem_mode`, `cashback_enabled`, `fee_basis_points`, and `creator_fee_basis_points`.

- [ ] Add failing state tests proving launch facts survive JSON round-trip and a normal trade changes effective fee to protocol plus creator fee while a known Mayhem-agent zero-fee trade does not.
- [ ] Run the focused state tests and confirm failures are missing fields/behavior.
- [ ] Add immutable defaults for backward snapshot restoration, Pump SOL/USDC quote constants, the published Mayhem agent identity, and fee update rules.
- [ ] Add failing market-input and engine tests proving Pump defaults to 125 bps, USDC uses six decimals and one USD, and an unknown quote mint blocks executable math.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Implement asset-specific cached market inputs and engine lookup without adding network calls.
- [ ] Run the three focused test files and commit `feat(radar): make Pump economics asset and fee aware`.

### Task 2: Independent-demand point-in-time features

**Files:**
- Modify: `sniper/research/memecoin_opportunity/state.py`
- Test: `tests/test_memecoin_opportunity_state.py`

**Interfaces:**
- Produces: `independent_buyer_count`, `independent_buyer_hhi_bps`, `new_independent_buyers`, `independent_capital_velocity_atomic_per_s`, `protocol_agent_flow_atomic`, and `buyer_retention_bps` in `point_in_time_features()`.
- Consumes: `LaunchState` actor balances and protocol-agent counters from Task 1.

- [ ] Add failing tests with mixed human, router, creator, and Mayhem-agent events proving only humans count toward independent breadth/HHI/velocity and reserve totals still include every trade.
- [ ] Run the focused tests and verify RED on independent feature assertions.
- [ ] Implement separate independent counters/balances and interval-safe retention derived only from current and prior states.
- [ ] Run focused state tests and commit `feat(radar): isolate independent launch demand`.

### Task 3: Immutable compact alert feature snapshots

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Modify: `sniper/research/memecoin_opportunity/outcomes.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/retention.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Test: `tests/test_memecoin_opportunity_store.py`
- Test: `tests/test_memecoin_opportunity_engine.py`
- Test: `tests/test_memecoin_opportunity_retention.py`
- Test: `tests/test_memecoin_opportunity_insights.py`

**Interfaces:**
- Produces: `op_forward_alert_features(alert_id PRIMARY KEY, feature_version, feature_json, row_fingerprint, inserted_at)` and `append_forward_alert_features(conn, alert_id, features, version)`.
- Enforces: canonical JSON no larger than 4,096 encoded bytes; immutable conflict detection; launch-scoped retention.

- [ ] Add failing store tests for schema migration, immutable insert/redelivery, conflict rejection, and oversize rejection.
- [ ] Run focused store tests and verify RED.
- [ ] Add schema version 9, migration 8-to-9, immutable triggers, append/read helpers, and alert/launch retention ordering.
- [ ] Add failing engine/insight tests proving the exact alert-time feature JSON is stored and projected instead of reconstructing a later state.
- [ ] Run focused tests and verify RED.
- [ ] Persist the compact allowlisted feature snapshot during the same transaction as each alert; project it into insights.
- [ ] Run the four focused files and commit `feat(radar): retain immutable alert features`.

### Task 4: Confirmation truth and paired shadow receipts

**Files:**
- Modify: `sniper/research/memecoin_opportunity/directional.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Test: `tests/test_memecoin_opportunity_directional.py`
- Test: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Produces: `evaluate_confirmation_arms(current, prior) -> tuple[DirectionalVerdict, ...]` for truth, independent, clean, and low-friction; delayed receipt materialization on the next eligible event.
- Reuses: one remote quote result and one entry object per rule; zero extra remote probes.

- [ ] Add failing directional tests for each arm boundary and fee-relative low-friction threshold.
- [ ] Run focused directional tests and verify RED.
- [ ] Implement pure multi-arm classification while retaining legacy single-arm behavior for old rule versions.
- [ ] Add failing engine tests for once-per-launch receipts, shared quote completion, next-event delayed pairing, and unchanged remote-probe count.
- [ ] Run focused engine tests and verify RED.
- [ ] Materialize forward-only receipts with deterministic IDs and delayed linkage fields in feature JSON.
- [ ] Run both focused files and commit `feat(radar): add paired confirmation microscope arms`.

### Task 5: Exact 60-second policy result

**Files:**
- Modify: `sniper/research/memecoin_opportunity/outcome_tracker.py`
- Modify: `sniper/research/memecoin_opportunity/outcomes.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Test: `tests/test_memecoin_opportunity_outcomes.py`
- Test: `tests/test_memecoin_opportunity_engine.py`
- Test: `tests/test_memecoin_opportunity_insights.py`

**Interfaces:**
- Produces: `opportunity-path-v3` observations and policy projection fields `realized_policy_return_bps`, `policy_exit_reason`, `policy_exit_elapsed_ms`.
- Requires: a newly computed observation at elapsed 60,000 ms from the latest finalized healthy state; no reuse of an earlier blank terminal mark.

- [ ] Add failing outcome tests for exact 60-second profitable, losing, flat, route-failed, and data-invalid exits under 125 bps fees.
- [ ] Run focused outcomes tests and verify RED.
- [ ] Version path evaluation, clamp due observations to the exact horizon, require executable due return for a `TIME_EXIT`, and preserve old v2 rows.
- [ ] Add failing insight tests for paired return sum/mean/median, result counts, immediate-minus-delayed delta, and evaluator isolation.
- [ ] Run focused insight tests and verify RED.
- [ ] Project plain policy results and paired aggregates from v3 only.
- [ ] Run the three focused files and commit `feat(radar): score exact confirmation policy exits`.

### Task 6: Bounded API and stable operator UI

**Files:**
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_opportunity_api.py`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Produces: compact `confirmation_microscope` aggregate and at most 16 recent policy receipts.
- Browser contract: keyed patching for `[data-op-radar-dot-id]` and `[data-op-policy-result-id]`; no Radar-root replacement after wiring.

- [ ] Add failing API tests for aggregate shape, evaluator filtering, 16-row limit, and representative payload below 150 KiB.
- [ ] Run focused API tests and verify RED.
- [ ] Add compact projection fields without full feature/history arrays and keep stream fingerprints market-semantic.
- [ ] Add failing UI contract tests for plain result labels, immediate/delayed comparison, keyed nodes, preserved details, 250 ms minimum render cadence, and hidden-tab cancellation.
- [ ] Run UI contract tests and verify RED.
- [ ] Patch stable keyed nodes, coalesce renders with `requestAnimationFrame` plus a 250 ms minimum, preserve the last good frame on errors, and show checkpoint/return/failure truth above the map.
- [ ] Run API/UI tests, `node --check static/app.js`, and commit `feat(radar): show bounded confirmation policy truth`.

### Task 7: Performance, retention, and boundary verification

**Files:**
- Modify if a failing assertion proves necessary: `tests/test_memecoin_opportunity_retention.py`
- Modify if a failing assertion proves necessary: `tests/test_memecoin_opportunity_api.py`
- Modify: `docs/superpowers/specs/2026-07-28-radar-confirmation-microscope-design.md` only for verified implementation clarifications.

**Interfaces:**
- Verifies all global constraints; produces no new runtime behavior unless a RED test exposes a contract violation.

- [ ] Run the focused Radar suite: `python3 -m pytest tests/test_memecoin_opportunity_*.py tests/test_prepare_memecoin_opportunity_retention.py -q`.
- [ ] Run `python3 -m compileall -q sniper/research/memecoin_opportunity sniper/routes/api_memecoin_opportunity.py scripts/memecoin_opportunity_worker.py`.
- [ ] Run `node --check static/app.js`, `python3 scripts/validate_schema_refs.py`, boundary tests, and `git diff --check`.
- [ ] Generate one representative compact snapshot and record byte size; verify no additional quote/RPC calls in the shadow-arm engine test.
- [ ] Run a browser smoke check against the local app if its existing authenticated test harness is available; otherwise report the exact unavailable prerequisite without claiming browser verification.
- [ ] Perform the Knowledge Sync Gate. Record the research direction as a `research_note` memory only if the configured Supabase write surface is available and explicitly safe; otherwise include the exact manual follow-up in the PR body.
- [ ] Commit any verification-only corrections as `test(radar): verify confirmation microscope budgets`.
