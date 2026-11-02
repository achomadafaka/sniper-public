# Memecoin Exit Hardening Round 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Solana completeness, de-risk broadcast, live authority flag, and funded-path audit blockers without enabling or deploying the engine.

**Architecture:** Keep the current modules and schemas, but make their states explicit: mode-tagged Solana frontiers, claim-owned de-risk transitions with pre-broadcast transaction identity, a fail-OFF state-root flag reader, and complete decision records flushed after all funded hooks. Preserve the existing executor routes and frozen v3 rules.

**Tech Stack:** Python 3.13, SQLite WAL, pytest, python-dotenv-compatible env syntax.

## Global Constraints

- Preserve every round-3 regression and extend it; do not replace tests with new scaffolding.
- No production DB writes, live APIs/RPC, flag edits, services, activation, deploy, or full test suite.
- Use the isolated `codex/exit-hardening` worktree and explicit-path staging.
- One logical fix per commit; run the focused regression before each commit.

---

### Task 1: Solana frontier modes

**Files:**
- Modify: `sniper/services/memecoin_solana_tape.py:33-253`
- Modify: `tests/test_memecoin_solana_tape.py:243-365`

**Interfaces:**
- Consumes: `sol_sig_cursor.frontier_before TEXT`, `_new_signatures(..., start_before=...)`.
- Produces: `_frontier(mode, signature) -> str`, `_parse_frontier(value) -> (mode, signature)`, and continuous `vault_tape_incomplete()` truth.

- [ ] **Step 1: Write failing regressions**

Add a 100-signature/budget-40 test that records every fetched signature after each pass and asserts `vault_tape_incomplete(tc, vault)` whenever any signature is missing. Add a deep enumeration/new-head case that asserts no transient complete state.

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_memecoin_solana_tape.py -q`

Expected: the 100/40 test fails on pass 3 with two missing signatures and `frontier_before IS NULL`.

- [ ] **Step 3: Implement mode-tagged frontiers**

Treat unprefixed stored values as `enum`. Resume only `enum` with `before=<sig>`. Re-enumerate `decode` without `before`. When an enumeration frontier reaches the cursor, retain `decode` even if the bottom batch fits the decode budget because the excluded frontier/head remains. Clear only after an unrestricted, reached walk is fully decoded.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_solana_tape.py -q`

- [ ] **Step 5: Commit explicit paths**

```bash
git add sniper/services/memecoin_solana_tape.py tests/test_memecoin_solana_tape.py
git commit -m "fix(memecoin): keep Solana tape incomplete through full drain"
```

### Task 2: Claim-owned de-risk submission

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:490-520,3227-3390`
- Modify: `sniper/execution/solana_exit_executor.py:108-187`
- Modify: `sniper/execution/evm_exit_executor.py:119-209`
- Modify: `sniper/services/memecoin_real_runner.py:939-1014`
- Modify: `sniper/settings/memecoin.py:210-217,870-930`
- Modify: `tests/test_memecoin_exit_executor.py`
- Modify: `tests/test_memecoin_evm_exit_executor.py`
- Modify: `tests/test_memecoin_derisk_recovery.py`
- Modify: `tests/test_memecoin_engine_authoritative_exit.py`

**Interfaces:**
- Produces: `claim_position_derisk(...)->str|None`, `mark_position_derisk_submitted(..., claim_id, sig)->bool`, `reset_position_derisk_submission(..., claim_id, sig)->bool`, and claim-qualified release/book/recovery helpers.
- Invariant: `send_fn` is unreachable until the signed identity is durably stored as unverified.

- [ ] **Step 1: Write failing crash/race regressions**

Cover Solana and EVM crash-after-broadcast. Assert `derisked_at` and `derisk_sig` remain set, stale-pending selection excludes the row, a second claim loses, and a second broadcast never occurs. Cover two stale recoverers where an old release cannot clear a replacement claim.

- [ ] **Step 2: Verify RED**

Run the individual new test nodes with `python3 -m pytest ... -q`.

Expected: current code leaves `derisk_sig=NULL` after the crash and permits a second broadcast.

- [ ] **Step 3: Implement the state machine**

Add `derisk_claim_id`. Generate an opaque UUID claim token. CAS every transition on the token; CAS submitted transitions on token+signature. Store `derisked_at + derisk_sig` before send. Explicit no-broadcast/failure may reset the same submission to pending; exceptions/ambiguity retain it for recovery. Require finite positive `MEMECOIN_DERISK_PENDING_STALE_S` in startup validation.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_exit_executor.py tests/test_memecoin_evm_exit_executor.py tests/test_memecoin_derisk_recovery.py tests/test_memecoin_engine_authoritative_exit.py -q`

- [ ] **Step 5: Commit explicit paths**

```bash
git add sniper/persistence/db_memecoin.py sniper/execution/solana_exit_executor.py sniper/execution/evm_exit_executor.py sniper/services/memecoin_real_runner.py sniper/settings/memecoin.py tests/test_memecoin_exit_executor.py tests/test_memecoin_evm_exit_executor.py tests/test_memecoin_derisk_recovery.py tests/test_memecoin_engine_authoritative_exit.py
git commit -m "fix(memecoin): persist claim-owned derisk submission before broadcast"
```

### Task 3: State-root live authority flag

**Files:**
- Modify: `sniper/settings/memecoin.py:177-202`
- Modify: `tests/test_memecoin_engine_authoritative_exit.py`

**Interfaces:**
- Produces: `engine_authoritative_chains(*, env=None) -> set[str]` that reads `<state_root>/.env` without mutating `os.environ` and fails OFF.

- [ ] **Step 1: Write failing production-shaped test**

Create a temporary absolute `SNIPER_STATE_ROOT`, write `.env` with `solana`, call the reader, replace the file with a blank value, call again, and assert immediate demotion while `os.environ` remains unchanged. Add missing/unreadable/malformed fail-OFF cases.

- [ ] **Step 2: Verify RED**

Run the new nodes only. Expected: the second call still returns `{'solana'}`.

- [ ] **Step 3: Implement direct state-root parsing**

Read `.env` on every call. The first exact key line wins. Missing key/file, blank, invalid root, or read error returns `set()`. Do not call `load_dotenv` or mutate the supplied environment mapping.

- [ ] **Step 4: Verify GREEN and existing policy tests**

Run: `python3 -m pytest tests/test_memecoin_engine_authoritative_exit.py tests/test_memecoin_engine_entry.py -q`

- [ ] **Step 5: Commit explicit paths**

```bash
git add sniper/settings/memecoin.py tests/test_memecoin_engine_authoritative_exit.py
git commit -m "fix(memecoin): make engine authority flag live and fail off"
```

### Task 4: Deferred complete decision evidence

**Files:**
- Modify: `sniper/persistence/db_exit_state.py:63-145`
- Modify: `sniper/services/memecoin_real_runner.py:1030-1470`
- Modify: `tests/test_memecoin_engine_authoritative_exit.py`
- Modify: `tests/test_exit_state_persistence.py`

**Interfaces:**
- Produces: a decision record with `state_id`, `proposed_fraction`, `legacy_action`, `executable_confirmed`, and `outcome`; `_flush_engine_decisions(xc, records)` runs after the funded position loop.
- Natural key remains `(position_id, decided_at, engine_version)` and stores one row.

- [ ] **Step 1: Write failing ordering and row-shape regressions**

Hold the exit-state DB write lock, execute two funded CUTs, and assert both `exit_fn` calls happen before the audit write times out. After unlocking, run a sweep and assert one complete row per tick. Extend the round-3 evidence test with proposed fraction and confirmation/outcome assertions.

- [ ] **Step 2: Verify RED**

Run the new nodes only. Expected: current synchronous `_log_engine_decision` delays the first funded hook and the row lacks proposed fraction.

- [ ] **Step 3: Implement deferred records**

Return `(decision, audit_record)` from engine evaluation. Update the record in memory after executable confirmation/execution. Append RIDE/DEFER outcomes explicitly. Flush records after the funded loop using the existing short-timeout exit-state connection. Write a complete row once; remove pre-action writes and outcome-update writes from the funded path.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_engine_authoritative_exit.py tests/test_exit_state_persistence.py -q`

- [ ] **Step 5: Commit explicit paths**

```bash
git add sniper/persistence/db_exit_state.py sniper/services/memecoin_real_runner.py tests/test_memecoin_engine_authoritative_exit.py tests/test_exit_state_persistence.py
git commit -m "fix(memecoin): defer complete engine evidence until after funded hooks"
```

### Task 5: Knowledge sync and focused verification

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md`

**Interfaces:**
- Produces: explicit Knowledge Delta attestation and current storage/state-machine/audit behavior.

- [ ] **Step 1: Update current architecture text without changing frozen decision rules**
- [ ] **Step 2: Run focused verification**

```bash
python3 -m pytest tests/test_memecoin_solana_tape.py tests/test_memecoin_sol_tape_own_db.py tests/test_memecoin_exit_executor.py tests/test_memecoin_evm_exit_executor.py tests/test_memecoin_derisk_recovery.py tests/test_memecoin_engine_authoritative_exit.py tests/test_memecoin_engine_entry.py tests/test_memecoin_real_rails_v2.py tests/test_exit_materializer_wiring.py tests/test_exit_state_persistence.py -q
python3 scripts/validate_schema_refs.py
git diff --check 6128a5fe..HEAD
```

- [ ] **Step 3: Review the complete diff against the approved design and AGENTS.md**
- [ ] **Step 4: Commit explicit documentation paths**
- [ ] **Step 5: Merge to `main`, re-run the focused verification on merged `main`, push `origin/main`, and remove the isolated worktree/branch**
