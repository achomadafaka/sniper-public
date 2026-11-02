# Memecoin Strategy Engine v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute immutable paper candidates identically live and in replay, with controls, fixed gates, and a data-locked exit optimizer.

**Architecture:** A pure event evaluator and typed DSL sit above live/replay adapters. Separate candidate ledgers and import guards make real execution unreachable.

**Tech Stack:** Python 3.13, dataclasses, SQLite, pytest, SHA-256, deterministic randomization tests

## Global Constraints

- Candidate modules cannot import real execution modules.
- Candidate positions are paper-only and require `timecap_s > 0`.
- Five active candidates maximum; parameters are immutable.
- No promotion or generated spawn reuses discovery evidence.

---

### Task 1: Registry and Immutable Ledgers

**Files:**
- Create: `sniper/research/memecoin_strategy/__init__.py`
- Create: `sniper/research/memecoin_strategy/models.py`
- Create: `sniper/research/memecoin_strategy/store.py`
- Create: `tests/test_memecoin_strategy_registry_v0.py`

**Interfaces:**
- Produces: `canonical_params_hash(predicate, exit_policy, sizing, costs, controls, gates) -> str`
- Produces: `enroll_candidate(conn, spec, now_iso) -> Candidate`
- Produces: `ensure_strategy_tables(conn) -> None`

- [ ] Add tests for stable canonical hash, immutable-trigger rejection, positive time cap, paper-only mode, spawn index, idempotent insert, and transactional five-slot cap.
- [ ] Run the test and confirm the package/tables do not exist.
- [ ] Implement dataclasses, schemas, constraints, triggers, and transactional enrollment.
- [ ] Rerun registry tests; commit `feat(memecoin): add immutable paper candidate registry`.

### Task 2: Typed DSL and Live/Replay Parity

**Files:**
- Create: `sniper/research/memecoin_strategy/dsl.py`
- Create: `sniper/research/memecoin_strategy/evaluator.py`
- Create: `tests/test_memecoin_strategy_evaluator_v0.py`

**Interfaces:**
- Produces: `compile_predicate(raw: dict) -> Predicate`
- Produces: `process_observation(candidate, observation, history, mode, now_iso) -> Evaluation`

- [ ] Add allowlist, type, operator, AND-only, null, tie-order, temporal-window, stale-live, and replay-parity tests.
- [ ] Run tests and confirm missing modules.
- [ ] Implement immutable compiled clauses and a pure history-based feature builder. Order by observed time, source time, then observation id.
- [ ] Rerun tests; commit `feat(memecoin): evaluate strategy rows deterministically`.

### Task 3: Seeds, Episodes, and Controls

**Files:**
- Create: `sniper/research/memecoin_strategy/seeds.py`
- Create: `sniper/research/memecoin_strategy/controls.py`
- Modify: `sniper/research/memecoin_strategy/store.py`
- Create: `tests/test_memecoin_strategy_seeds_v0.py`

**Interfaces:**
- Produces: `seed_v0_candidates(conn, now_iso) -> list[Candidate]`
- Produces: `select_past_control(signal, eligible_observations) -> ControlMatch | None`

- [ ] Add cluster distinct-wallet/same-chain/mcap/stale/daily-dedup tests; thesis chronology/score/cohort tests; and past-only control/missing-control tests.
- [ ] Run tests and confirm no seeds exist.
- [ ] Encode the two frozen specs, deterministic episode keys, source-observation lineage, and explicit `control_missing` records.
- [ ] Rerun seed tests; commit `feat(memecoin): preregister cluster and thesis candidates`.

### Task 4: Paper Position Engine

**Files:**
- Create: `sniper/research/memecoin_strategy/paper.py`
- Create: `sniper/research/memecoin_strategy/live.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Create: `tests/test_memecoin_strategy_paper_v0.py`

**Interfaces:**
- Produces: `open_candidate_position(conn, candidate, signal, quote, now_iso) -> dict`
- Produces: `sweep_candidate_positions(conn, quotes, now_iso) -> list[dict]`
- Produces: `CandidateLiveEngine.process_observations(...)` and `.sweep(...)`

- [ ] Add costs, TP-first/stop-first ordering, four-stop confirmation, time cap, missing quote, episode dedup, candidate/control fill, and no-real-import tests.
- [ ] Run tests and confirm no paper engine exists.
- [ ] Implement modeled fills from v2 selected-pair observations and separate paper ledgers. Exits use chronological observations only.
- [ ] Add optional runner hooks, construct the engine at startup, seed idempotently, and register candidate/control capture cohorts.
- [ ] Rerun runner and candidate tests; commit `feat(memecoin): run row strategies in forward paper`.

### Task 5: Kill and Promotion Reads

**Files:**
- Create: `sniper/research/memecoin_strategy/statistics.py`
- Modify: `sniper/research/memecoin_strategy/store.py`
- Create: `tests/test_memecoin_strategy_statistics_v0.py`

**Interfaces:**
- Produces: `candidate_read(conn, candidate_id, asof) -> CandidateRead`
- Produces: `apply_candidate_kills(conn, asof) -> list[str]`
- Produces: `approve_first_class_paper(conn, candidate_id, approved_at, read_id) -> CandidateStage`

- [ ] Add coverage conjunction, starvation, loss/futility kill, alpha allocation, paired sign-flip, clustered confidence bound, missing-control, immutable approval-read linkage, and fresh stage-cutoff tests.
- [ ] Run tests and confirm no read machinery exists.
- [ ] Implement deterministic per-coin aggregation, immutable read manifests, and an operator-only first-class-paper stage whose evidence cutoff starts at approval. Never promote a candidate automatically.
- [ ] Rerun statistics tests; commit `feat(memecoin): enforce candidate evidence gates`.

### Task 6: Locked Offline Exit Search

**Files:**
- Create: `sniper/research/memecoin_strategy/exit_search.py`
- Create: `scripts/research/memecoin_exit_search.py`
- Create: `tests/test_memecoin_strategy_exit_search_v0.py`

**Interfaces:**
- Produces: `data_gate(paths, coverage) -> GateResult`
- Produces: `simulate_exit_path(path, policy, costs) -> ExitResult`
- Produces: `walk_forward_search(paths, candidate, asof) -> SearchResult`

- [ ] Add observation ordering, same-timestamp conservative ordering, stop confirmation, costs, incomplete paths, every data deficit, chronological split, shrinkage, and no-spawn tests.
- [ ] Run tests and confirm no search module exists.
- [ ] Implement the frozen grid, training-only selection, forward scoring, stratum shrinkage, and `accruing` deficits. Enrollment occurs only after all gates and an active-slot transaction.
- [ ] Run all candidate tests and the full focused memecoin suite; commit `feat(memecoin): add evidence-locked exit search`.
