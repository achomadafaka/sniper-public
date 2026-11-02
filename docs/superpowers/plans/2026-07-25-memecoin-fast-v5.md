# Memecoin Fast v5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a selectable v5 exit policy that makes a decision on every Solana hot tick and remains identical to the 30-second fallback for the same state.

**Architecture:** Refactor the existing v3 rule tree into one private quant-policy function parameterized only by partial fraction and edge duration. Expose `propose_v3`, `propose_v5`, and a strict version selector; inject the selected proposer into hot, materialized, funded, and replay paths. Defaults remain hot OFF and v3 authority.

**Tech Stack:** Python 3, dataclasses, pytest, asyncio hot-lane services, SQLite evidence store.

## Global Constraints

- Preserve `CUT > TAKE_PROFIT > DERISK > RIDE > DEFER_TO_LEGACY`.
- Preserve funded envelope, exact confirmation, durable claims, reason mapping, chain allowlist, and five-position bound.
- `MEMECOIN_EXIT_ENGINE_VERSION` defaults to `v3`; only `v3` and `v5` are valid.
- No `.env` edit, flag activation, deployment, service restart, live API/RPC call, or production DB mutation.
- TDD: every production behavior must be observed RED first.
- Explicit-path staging and focused tests; no full suite.

---

### Task 1: Pure v5 policy and strict selector

**Files:**
- Modify: `sniper/services/memecoin_exit_brain.py`
- Modify: `sniper/analysis/exit_flex_v5.py`
- Modify: `tests/test_exit_engine_v3.py`
- Modify: `tests/test_exit_flex_v5.py`

**Interfaces:**
- Produces: `ENGINE_VERSION_V5 = "exit-brain-v5"`.
- Produces: `propose_v5(contract, state, cfg=None) -> CandidateDecision`.
- Produces: `exit_engine(version) -> tuple[str, Callable]`, raising `ValueError` outside `{"v3", "v5"}`.
- Preserves: `propose_v3(contract, state, cfg=None)` output.

- [ ] Add RED tests proving v5 bounded fractions, per-band elapsed DERISK, one-shot behavior, v3 parity outside registered changes, and strict selection.
- [ ] Run `python3 -m pytest tests/test_exit_engine_v3.py tests/test_exit_flex_v5.py -q` and confirm failures name missing `propose_v5`/selector behavior.
- [ ] Extract one private rule function from `propose_v3`; pass fixed `0.5`/10,800 seconds for v3 and registered fraction/band duration for v5.
- [ ] Make `shadow_comparison()` call `propose_v5()` and compare executable decision fields instead of reconstructing a fraction-only approximation.
- [ ] Rerun the focused tests and commit only the four explicit paths.

### Task 2: Configuration and event-driven hot v5

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/exit_engine_hot.py`
- Modify: `sniper/services/memecoin_hot_supervisor.py`
- Modify: `tests/test_memecoin_real_policy_validation.py`
- Modify: `tests/test_exit_engine_hot.py`
- Modify: `tests/test_memecoin_hot_supervisor.py`

**Interfaces:**
- Produces: `MEMECOIN_EXIT_ENGINE_VERSION`, default `"v3"`.
- Changes: `HotPositionEngine(..., decision_fn=propose_v3, engine_version=ENGINE_VERSION_V3)`.
- Hot contracts include `coin_band`, `opened_at`, and recompute `held_s` from `now_ts` per tick.

- [ ] Add RED tests for default v3, invalid-version validation, per-tick v5 calls, dynamic `held_s`, history warm-up v5, and unchanged default behavior.
- [ ] Run the three focused test files and confirm the new assertions fail for direct `propose_v3` wiring.
- [ ] Add strict settings validation and inject `exit_engine(cfg.MEMECOIN_EXIT_ENGINE_VERSION)` into `build_hot_book()`.
- [ ] Refresh `held_s` in the per-position engine contract from corrected tick time and `opened_at` before every decision.
- [ ] Express live time windows from v5 horizon constants without converting them to block counts.
- [ ] Rerun the focused tests and commit explicit paths.

### Task 3: Funded fallback and one-row evidence parity

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_exit_materializer.py`
- Modify: `tests/test_memecoin_engine_authoritative_exit.py`
- Modify: `tests/test_exit_materializer_wiring.py`

**Interfaces:**
- The funded adapter and materializer obtain `(engine_version, proposer)` from the same strict selector as the hot book.
- `_engine_audit_record(..., engine_version)` persists the actual decision engine.
- `record_shadow_decision()` persists one row for the selected baseline and retains `v5_shadow` when baseline is v3.

- [ ] Add RED tests showing identical hot/fallback decisions, v5-funded proposed fractions, actual engine version on the single evidence row, and v3 default compatibility.
- [ ] Run both test files and confirm failures come from hard-coded `propose_v3`/`ENGINE_VERSION_V3`.
- [ ] Replace direct funded/materializer calls with strict selector calls; add `coin_band` to the funded contract.
- [ ] Keep the existing adapter mapping and deferred one-row flush unchanged apart from the actual engine version.
- [ ] Rerun focused tests plus `tests/test_crash_windows.py` and commit explicit paths.

### Task 4: Deterministic replay and same-tape review

**Files:**
- Modify: `scripts/research/memecoin_v5_blind_replay.py` (renamed from `..._v3_...`; V5-only per the
  2026-07-25 retirement — see `docs/superpowers/specs/2026-07-25-memecoin-v5-only-runtime-retirement.md`)
- Modify: `tests/test_memecoin_v5_blind_replay.py` (renamed from `..._v3_...`)
- Create: `tests/test_memecoin_fast_v5_parity.py`

**Interfaces:**
- `replay_position(..., engine_version="v3")` uses the production selector and records versioned PnL keys.
- Same-tape parity test feeds identical states to hot and fallback proposers with no RPC or DB write.

- [ ] Add RED tests for v5 replay selection, variable partial accounting, one-shot DERISK, and same-state hot/fallback equality.
- [ ] Run the three replay/parity files and confirm the old hard-coded v3 path fails them.
- [ ] Parameterize the replay with the production selector and decision-provided fraction; retain v3 defaults and existing output keys.
- [ ] Run deterministic fixture backtests for both versions and report decision/action differences without claiming live superiority.
- [ ] Commit explicit paths.

### Task 5: Close gates and integrate

**Files:**
- Modify only if required by verified failures: files already listed above.
- Knowledge files: `docs/superpowers/specs/2026-07-25-memecoin-fast-v5-design.md`, this plan, and the required Knowledge Sync record.

**Interfaces:**
- Branch is mergeable only if all focused funded, hot, materializer, replay, crash, schema, and diff checks pass.

- [ ] Run changed-surface suites plus `tests/test_crash_windows.py`, `tests/test_memecoin_exit_executor.py`, and `tests/test_memecoin_evm_exit_executor.py`.
- [ ] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables` and `git diff --check main...HEAD`.
- [ ] Review all `main...HEAD` hunks for default-ON behavior, direct `propose_v3` calls in authority paths, evidence-row splits, activation/deploy changes, and unrelated files.
- [ ] Complete the Knowledge Sync Gate. If an external memory write is required but not authorized, stop before merge and report the exact blocker.
- [ ] Merge to `main`, push `origin/main`, and remove the worktree only after every gate passes and push authority is confirmed by the existing user instruction.
