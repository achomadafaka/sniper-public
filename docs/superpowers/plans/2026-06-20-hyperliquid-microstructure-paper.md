# Hyperliquid Microstructure Paper Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live Hyperliquid paper-action harness that records a decision or skip reason on every interval.

**Architecture:** Add `sniper/algos/hl_microstructure/` with isolated book, feature, policy, paper execution, and persistence modules. Add `scripts/run_hl_microstructure_paper.py` as the operator entrypoint. Keep it paper-only with no private-key or live-order surface.

**Tech Stack:** Python stdlib, SQLite, `websocket-client` for public WebSocket real mode, pytest.

## Global Constraints

- Paper only.
- No private keys.
- No live order endpoint.
- No production DB mutation.
- Fixture mode must run without network.
- Every decision interval records a decision or explicit skip reason.
- Known-at-time: observations with `receive_ts_ms > decision_ts_ms` must not affect features.
- Full-notional taker fill only; partial fills become `insufficient_depth`.
- Fee rate is a persisted assumption.

---

### Task 1: Book State and Visible-Depth Fill

**Files:**
- Create: `sniper/algos/hl_microstructure/book.py`
- Create: `tests/test_hl_microstructure_book.py`

**Interfaces:**
- Produces: `BookState.from_l2(asset, levels, exchange_ts_ms, receive_ts_ms) -> BookState`
- Produces: `BookState.taker_fill(action, notional_usdc) -> FillEstimate`

- [ ] Write failing tests for unordered levels, best bid/ask, long/short fill direction, and insufficient depth.
- [ ] Run: `python3 -m pytest tests/test_hl_microstructure_book.py -q` and verify failure.
- [ ] Implement `BookState` and `FillEstimate`.
- [ ] Rerun the focused test and verify pass.

### Task 2: Rolling Known-At-Time Features

**Files:**
- Create: `sniper/algos/hl_microstructure/models.py`
- Create: `sniper/algos/hl_microstructure/features.py`
- Create: `tests/test_hl_microstructure_features.py`

**Interfaces:**
- Consumes: `BookState`
- Produces: `RollingMarketState.ingest_trade(...)`
- Produces: `RollingMarketState.update_book(book)`
- Produces: `RollingMarketState.snapshot(asset, decision_ts_ms) -> FeatureSnapshot`

- [ ] Write failing tests for trade-flow imbalance and future-received observation exclusion.
- [ ] Run focused tests and verify failure.
- [ ] Implement model dataclasses and rolling state.
- [ ] Rerun focused tests and verify pass.

### Task 3: Policy Lanes

**Files:**
- Create: `sniper/algos/hl_microstructure/policies.py`
- Create: `tests/test_hl_microstructure_policies.py`

**Interfaces:**
- Consumes: `FeatureSnapshot`
- Produces: `PolicyDecision(policy_id, action, side, reason, confidence)`
- Produces: `evaluate_policies(snapshot, config) -> list[PolicyDecision]`

- [ ] Write failing tests for `baseline_no_trade`, `flow_momentum`, `book_pressure`, and explicit skip reasons.
- [ ] Run focused tests and verify failure.
- [ ] Implement policy evaluator.
- [ ] Rerun focused tests and verify pass.

### Task 4: Persistence and Paper Execution

**Files:**
- Create: `sniper/algos/hl_microstructure/persistence.py`
- Create: `sniper/algos/hl_microstructure/paper.py`
- Create: `tests/test_hl_microstructure_paper.py`

**Interfaces:**
- Consumes: `BookState`, `FeatureSnapshot`, `PolicyDecision`
- Produces: `ensure_schema(conn)`
- Produces: `PaperEngine.on_decision(...) -> PaperResult`

- [ ] Write failing tests proving entries/skips/fills persist and full-depth failure records `insufficient_depth`.
- [ ] Run focused tests and verify failure.
- [ ] Implement schema and paper engine.
- [ ] Rerun focused tests and verify pass.

### Task 5: Live Paper CLI

**Files:**
- Create: `scripts/run_hl_microstructure_paper.py`
- Create: `tests/test_hl_microstructure_cli.py`

**Interfaces:**
- Consumes: all prior modules.
- Produces: fixture-mode and live-mode CLI.

- [ ] Write failing CLI fixture-mode test that creates a DB and records decisions without network.
- [ ] Run focused test and verify failure.
- [ ] Implement CLI fixture mode and public WebSocket real mode.
- [ ] Rerun focused test and verify pass.

### Task 6: Verification

**Files:**
- No new files.

- [ ] Run: `python3 -m pytest tests/test_hl_microstructure_*.py -q`
- [ ] Run: `python3 scripts/run_hl_microstructure_paper.py --fixture-mode --db-path /tmp/hlm_smoke.sqlite --duration-seconds 1`
- [ ] Run: `python3 scripts/validate_schema_refs.py`
- [ ] Run: `git diff --check`
- [ ] Confirm no live-order/private-key strings in the new package or CLI.
