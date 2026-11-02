# Memecoin Live Safety v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reservation recovery, market-price identity, real exit triggers, and terminal holds fail closed.

**Architecture:** Add typed chain status and quote records at the venue seams, persist recovery proof, and require executable proceeds before price-triggered exits. Keep realized PnL authoritative in the existing chain-delta executors.

**Tech Stack:** Python 3.13, SQLite, pytest, Jupiter, Solana JSON-RPC, 0x, DexScreener

## Global Constraints

- Do not restart or mutate the live runner while implementing.
- No production RPC/API calls in tests.
- Write the failing test and observe the intended failure before production edits.
- Real mode requires `MEMECOIN_REAL_MAX_HOLD_S > 0`.

---

### Task 1: Typed Solana Confirmation

**Files:**
- Modify: `sniper/execution/solana_real_seams.py`
- Modify: `tests/test_memecoin_real_seams.py`

**Interfaces:**
- Produces: `rpc_confirm(..., max_attempts: int) -> Literal['confirmed','failed','pending','not_found','unavailable']`

- [ ] Add response-shape tests for explicit null, processed, confirmed, failed, non-200, JSON-RPC error, malformed result, and transport exception.
- [ ] Run `python3 -m pytest tests/test_memecoin_real_seams.py -q`; confirm new cases fail because `unknown` conflates states.
- [ ] Parse only HTTP-200 valid `result.value[0]`; retain the strongest observed state during multi-poll confirmation and return `unavailable` only when no valid response was received.
- [ ] Rerun the focused test and commit `fix(memecoin): classify Solana confirmation states`.

### Task 2: Persist Expiry Proof

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `tests/test_memecoin_reservation_recovery.py`

**Interfaces:**
- Produces: `record_pending_recovery_check(conn, pending_id, status, checked_at) -> dict`
- Consumes: typed status from Task 1

- [ ] Add tests for three spaced `not_found` checks, too-fast checks, `pending` reset, `unavailable` no-op, EVM exclusion, confirmed orphan, failed release, and `broadcast_at` without signature.
- [ ] Run the focused test and confirm release still occurs on the first generic `unknown`.
- [ ] Add idempotent columns `not_found_checks`, `first_not_found_at`, and `last_recovery_check_at`; implement atomic counter/reset updates.
- [ ] Change recovery to release Solana only after age >=1200 seconds, count >=3, and proof span >=120 seconds. Release no-signature rows only when `broadcast_at IS NULL`.
- [ ] Rerun reservation, pending-ledger, and real-runner tests; commit `fix(memecoin): require proof before reservation release`.

### Task 3: Chain-Safe Pair Selection

**Files:**
- Modify: `sniper/execution/solana_quote.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `sniper/services/memecoin_runner.py`
- Create: `tests/test_memecoin_quote_identity.py`

**Interfaces:**
- Produces: `select_dex_pair(pairs, mint, network_id) -> dict | None`
- Produces: `dexscreener_stats(mint, http=None, network_id=None) -> dict | None`
- Produces: `dexscreener_stats_batch_keyed(keys, http=None) -> dict[tuple[str,str], dict]`

- [ ] Add wrong-chain, quote-side, shallow-first, deterministic-tie, zero/NaN, Solana inference, and EVM-case tests.
- [ ] Run the new test and confirm current first-pair behavior fails.
- [ ] Add `FOMO_TO_DEX`, strict base/chain filtering, deterministic liquidity ranking, pair metadata, and keyed batch results. Keep `resolve_evm_chain` as the only multi-chain discovery call.
- [ ] Thread known `network_id` through copy, pump, paper mark, and real mark callers without changing fallback behavior for legacy Solana rows.
- [ ] Run all quote/copy/network tests; commit `fix(memecoin): bind prices to chain and pair`.

### Task 4: Executable-Proceeds Guard

**Files:**
- Modify: `sniper/execution/solana_real_seams.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Create: `tests/test_memecoin_executable_exit_guard.py`

**Interfaces:**
- Produces: `ExitPreview(expected_usdc: float, amount_atomic: int, route_id: str | None, quoted_at: str)`
- Produces on each venue context: `preview_exit_fn(position) -> ExitPreview | None`
- Produces: `executable_exit_allowed(conn, position, reason, preview) -> tuple[bool, str]`

- [ ] Add tests for TP/stop agreement and disagreement, unavailable preview, prior partial proceeds, rejected-stop reset, and executable de-risk fraction.
- [ ] Run the new test and confirm the runner currently calls `exit_fn` solely from display mid.
- [ ] Build preview functions from exact raw token balances and quote `outAmount`/0x USDC output; reject nonfinite/nonpositive values.
- [ ] Sum prior chain exit fill notionals with preview proceeds. Gate only `real_tp`, `b_stop`, and `derisk`; leave mirror/manual/time/unquotable unchanged.
- [ ] Use preview proceeds to calculate de-risk fraction and record skipped-trigger reasons for audit.
- [ ] Run real runner, Solana exit, and EVM exit suites; commit `fix(memecoin): validate price exits against executable proceeds`.

### Task 5: Terminal Policy Validation

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `tests/test_memecoin_risk_settings.py`
- Modify: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Produces: `validate_real_exit_policy() -> None`, raising `RuntimeError` only when real mode is enabled and max hold is nonpositive.

- [ ] Add armed/disarmed and zero/positive time-cap tests.
- [ ] Run tests and confirm no startup validator exists.
- [ ] Implement validation and call it before building real contexts.
- [ ] Run `python3 -m pytest tests/test_memecoin*.py tests/test_fomo_account*.py tests/test_clock_offset.py -q` and `python3 scripts/validate_schema_refs.py`.
- [ ] Commit `fix(memecoin): require a terminal real exit`.
