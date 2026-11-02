# Memecoin Gate Audit Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore measured Base route coverage, eliminate blind-chain capture starvation without new requests, surface funded config drift, and correct audit clock domains.

**Architecture:** Extend the existing direct V3 quoter with configured two-hop connector candidates used symmetrically by entry and exit. Compare candidates in one bounded JSON-RPC batch and keep Base funded-disabled until a fresh two-way route passes. Reuse fresh shared position marks inside capture v2 before spending the existing blind-probe budget. Keep operational diagnostics at startup and timestamp fixes inside persistence helpers.

**Tech Stack:** Python 3.13, SQLite, Uniswap V3 QuoterV2/SwapRouter02 ABI, pytest.

## Global Constraints

- Coin identity remains `(network_id, mint)`.
- Robinhood is network `4663`; Base is `8453`.
- No gate threshold, reservation, sizing, accounting, or paper-strategy behavior changes.
- No new hot-path provider or unbounded request fan-out.
- TDD for every production behavior change.

---

### Task 1: Base V3 connector route and startup truth

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/execution/evm_dex_quote.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_alerts.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Test: `tests/test_memecoin_evm_dex_quote.py`
- Test: `tests/test_memecoin_multi_venue.py`
- Test: `tests/test_memecoin_real_alerts.py`
- Test: `tests/test_memecoin_runner_loop_cadence.py`

**Interfaces:**
- Consumes: existing `dex_v3_quote(...)` quote contract and `build_all_contexts()` result.
- Produces: configured V3 connector paths and `missing_evm_context_chains(contexts, allowlist=None)`.

- [x] Write failing tests for a direct-miss/two-hop-hit quote, reverse route symmetry, no-route fail-closed behavior, sorted missing-context diagnostics, and one formatted config alert.
- [x] Run the focused tests and confirm failures are due to missing connector/diagnostic behavior.
- [x] Add Base WETH plus its USDC/WETH fee to immutable chain config.
- [x] Implement QuoterV2 `quoteExactInput(bytes,uint256)` and SwapRouter02 `exactInput((bytes,address,uint256,uint256))`; compare direct and connector outputs in one request.
- [x] Add startup log plus asynchronous operational alert for missing allowlisted contexts.
- [x] Rerun focused tests and the EVM safety/execution suite.
- [x] Commit the logical route/config change.

### Task 2: Shared-mark reuse for blind capture

**Files:**
- Modify: `sniper/services/memecoin_capture_v2.py`
- Test: `tests/test_memecoin_capture_blind.py`

**Interfaces:**
- Consumes: `memecoin_position_marks(chain_id, mint, sweep_ts, mid, liquidity, mcap, source, quality)`.
- Produces: v2 `probe_ok` observations with exact position-mark provenance before the existing network probe.

- [x] Write a failing test with three eligible cohorts: one fresh shared mark, one stale mark, and one missing mark under a one-mint probe budget.
- [x] Assert the fresh mark is reused with its original timestamp, only the stale/missing set reaches the probe, and the remaining missing cohort is the only budget skip.
- [x] Run the test and confirm the current implementation probes by priority and ignores shared marks.
- [x] Implement one-query latest-mark loading, exact provenance writes, existing ratio guard, and remaining-only probe selection.
- [x] Rerun blind capture plus horizon/capture-v2 tests.
- [x] Commit the capture change separately.

### Task 3: Correct approval and override clocks

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_real_deploy.py`
- Test: `tests/test_memecoin_real_exit_override.py`

**Interfaces:**
- Consumes: `sniper.util.clock.real_utc_now_iso()`.
- Produces: corrected observed-UTC default audit timestamps while preserving explicit `now_iso`.

- [x] Write failing tests that monkeypatch `real_utc_now_iso()` and call approval/override helpers without `now_iso`.
- [x] Confirm current helpers write host wall time instead of the patched corrected time.
- [x] Replace only the default timestamp sources; preserve explicit timestamps and transaction behavior.
- [x] Rerun focused persistence, approval, override, and clock tests.
- [x] Commit the clock fix separately.

### Task 4: Runtime configuration and release validation

**Files:**
- Modify outside Git: `/home/bigpapi/Desktop/Sniper/.env`

**Interfaces:**
- Consumes: merged exact SHA and the existing pinned deployment script.
- Produces: a non-contradictory funded allowlist and an exact pinned runtime release.

- [x] Run schema validation, full `tests/test_memecoin*.py`, relevant concurrency/runtime tests, compileall, and diff checks.
- [x] Run a fresh read-only two-way Base connector quote against recent routed tokens. Result: 29 checked, two buy-routable, zero two-way; keep Base funded-disabled and remove it from the allowlist.
- [x] Run the Knowledge Sync Gate in `audit_only` mode and attach the required PR block.
- [ ] Push the branch, open/merge the PR, then remove `8453` from `MEMECOIN_EVM_ALLOWLIST` without printing secrets.
- [ ] Deploy the exact merged SHA with `scripts/deploy_memecoin_runtime.sh`.
- [ ] Verify one pinned process, exact release SHA, healthy heartbeat/auth/sweep, contexts `solana` and `4663`, and improved blind-capture reuse in logs/DB.
- [ ] Confirm Base remains funded-disabled pending a fresh two-way route and enough USDC to clear the unchanged $5 floor.
