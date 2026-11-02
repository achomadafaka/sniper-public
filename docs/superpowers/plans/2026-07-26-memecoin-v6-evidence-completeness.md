# Memecoin V6 Evidence Completeness Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every V6 live-paper position receive continuous, production-shaped Solana/4663 market, executable-route, source-wallet, and depth evidence, with no false `not_triggered` result when a required input is unavailable.

**Architecture:** Enforce the already-approved V6 fast-runtime contract. Open positions bootstrap from durable tape before their market lane becomes current; subscription events then advance a per-pool cursor, while reconnect recovery rebuilds gaps from durable tape instead of restarting from an empty accumulator. Source-wallet state starts from the accepted opening event and advances only on exact post-entry holder evidence. Reporting combines observation-level and per-rule sufficiency so it cannot hide missing market evidence.

**Tech Stack:** Python 3.13, asyncio, SQLite WAL, Solana JSON-RPC/WebSocket read adapters, pytest, vanilla JavaScript UI.

## Global Constraints

- V6 paper only. No funded signing, approval, broadcast, reservation, policy, or runner change.
- V6 remains the only post-fill paper decision authority.
- Every engine input is causal and carries stable evidence identity; no future backfill or chart-price substitute.
- A feed is `current` only after startup/reconnect catch-up reaches a continuous cursor.
- New V6 entries do not open on a chain whose required evidence lane is not current.
- Existing positions remain open during recovery and resume from the same engine state.
- Focused tests only; do not run the full suite.

---

### Task 1: Durable Solana startup and reconnect recovery

**Files:**
- Modify: `sniper/services/memecoin_v6_incremental_state.py`
- Modify: `sniper/services/memecoin_v6_solana_feed.py`
- Modify: `scripts/memecoin_v6_paper_worker.py`
- Test: `tests/test_memecoin_v6_incremental_state.py`
- Test: `tests/test_memecoin_v6_solana_feed.py`
- Test: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: V6 open-position route bindings, read-only `memecoin_sol_tape.db`, normalized `V6MarketEvent`.
- Produces: `PoolAccumulator.replace_from(rebuilt)` and recovery callbacks that replace an incomplete accumulator only with a complete, cursor-consistent rebuild.

- [ ] Write tests proving startup binds every V6 Solana position from the shared tape and starts incomplete when no bounded history exists.
- [ ] Run the focused tests and confirm RED because dynamic websocket accumulators currently start `tape_complete=True` with no backfill.
- [ ] Implement complete-only accumulator replacement, persistent pool binding refresh, and per-pool feed health.
- [ ] Write tests proving a late notification schedules catch-up instead of permanently poisoning the accumulator.
- [ ] Run the focused tests and confirm RED because `PoolAccumulator.apply()` currently makes any older event a permanent gap.
- [ ] Implement a bounded reorder/catch-up boundary: duplicates remain no-ops, late events mark recovery-required, and a complete durable rebuild clears the gap.
- [ ] Write tests proving Solana RPC/WebSocket transient failures reconnect without terminating the whole V6 runtime and without declaring the lane current before catch-up.
- [ ] Implement supervised reconnect and catch-up; preserve the last complete cursor through provider failure.
- [ ] Run the three focused files and commit.

### Task 2: Gate entries on complete chain evidence

**Files:**
- Modify: `sniper/services/memecoin_v6_fast_runtime.py`
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Modify: `scripts/memecoin_v6_paper_worker.py`
- Test: `tests/test_memecoin_v6_fast_runtime.py`
- Test: `tests/test_memecoin_v6_fast_runtime_integration.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- Consumes: per-network feed readiness derived from all position-bound accumulators and persisted feed-health rows.
- Produces: `network_current(network_id) -> bool`; `FastRunnerAdapter.poll_entry()` returns no job while that network is recovering.

- [ ] Write tests proving a signal remains unconsumed while its chain lane is incomplete and opens once the lane becomes current.
- [ ] Run tests and confirm RED because entry polling currently ignores feed readiness.
- [ ] Add the readiness callback and cursor discipline without changing the accepted-signal predicate.
- [ ] Prove 4663 and Solana readiness are independent and one degraded chain does not stop the other.
- [ ] Run focused runtime/runner tests and commit.

### Task 3: Complete causal source-wallet evidence

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_observations.py`
- Test: `tests/test_memecoin_v6_engine.py`

**Interfaces:**
- Consumes: immutable accepted opening event plus exact `memecoin_holder_evidence_history` facts.
- Produces: `SourcePositionFact` for every V6 entry: initial `holding` from the exact opening event, later `holding|exited` only from explicit remaining-balance evidence; a generic sell never becomes a full exit.

- [ ] Write tests proving the source lane begins as `holding` with the origin event identity when no later holder snapshot exists.
- [ ] Run tests and confirm RED because `_position_context()` currently emits null source state.
- [ ] Implement the causal fallback bounded by signal/fill time and exact wallet/network/mint identity.
- [ ] Prove an explicit zero supersedes the opening fact, a positive snapshot stays holding, and an ambiguous sell does not become exited.
- [ ] Run focused runner/observation/engine tests and commit.

### Task 4: Honest complete decision context

**Files:**
- Modify: `sniper/services/memecoin_v6_engine.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_v6_engine.py`
- Test: `tests/test_memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_api.py`
- Test: `tests/test_memecoin_v6_paper_ui.py`

**Interfaces:**
- Consumes: observation `missing_fields`, per-rule results, semantic lane identities, capture status.
- Produces: a staged-break result that remains `insufficient` on quote-only wakes when its last market evidence was insufficient; decision context exposes every observation/rule gap and never renders insufficient as “no confirmed break.”

- [ ] Write tests reproducing ANSEM: incomplete tape plus a healthy quote must say market/depth evidence is recovering, not “no confirmed break.”
- [ ] Run tests and confirm RED.
- [ ] Preserve the last market-rule sufficiency across quote-only observations and union observation-level missing facts into the report.
- [ ] Render one root-cause recovery message with affected rules; retain detailed evidence expansion.
- [ ] Run focused engine/report/API/UI tests, `node --check static/app.js`, and commit.

### Task 5: Local integration and live-paper proof

**Files:**
- Modify only if a focused integration defect is reproduced by a RED test.

**Interfaces:**
- Consumes: merged Tasks 1-4.
- Produces: current live-paper observations for every open position with healthy route evidence and complete market/source/depth families whenever the chain has produced the required event class.

- [ ] Run focused V6, Solana-tape, adjacent route-quote, schema, syntax, and `git diff --check` verification.
- [ ] Fast-forward local `main`; do not push.
- [ ] Restart only `memecoin_sol_tape_worker.py` and `memecoin_v6_paper_worker.py`; leave the funded runner unchanged.
- [ ] Wait for bounded backfill/recovery using condition checks, not sleeps longer than 60 seconds.
- [ ] Inspect every open V6 position’s latest observation, rule results, feed health, cursor, quote status, and source evidence.
- [ ] Do not call the experiment evidence-complete unless observation inputs and rule contracts prove it. Report any externally unavailable fact as a concrete provider/capture blocker with the exact failed boundary.
