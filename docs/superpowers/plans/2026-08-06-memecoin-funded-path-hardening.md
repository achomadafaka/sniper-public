# Memecoin Funded Path Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan.

**Goal:** Keep funded entries and exits correct when optional paper or infrastructure work fails.

**Architecture:** Keep the existing funded policy and entry seams. Add typed native-SOL truth,
funded-first hook order, and one supervised fastlane state. Serialize all exit sweeps with one lock.

**Tech Stack:** Python 3, stdlib `dataclasses` and `threading`, SQLite, Flask, pytest.

## Global Constraints

- Do not change a funded exit-policy value.
- Do not change wallet authority.
- Do not call a live API.
- Do not write the production database.
- Do not restart or deploy a service.
- Use production-shaped tests for funded behavior.
- Use TDD for each task.

---

### Task 1: Freeze the correct `scalp_c2` policy

**Files:**
- Modify: `tests/test_memecoin_funded_contract_storage.py`
- Modify: `sniper/services/memecoin_risk.py`

- [ ] Add a funded contract test with `effective_mcap=1_500_000` and a selected `scalp_c2` arm.
- [ ] Assert that the frozen TP is 35 percent for Solana.
- [ ] Run the test and confirm that it fails with the default TP.
- [ ] Pass `entry_mcap` to `funded_policy_for_arm`.
- [ ] Run the focused contract and copy-arm tests.
- [ ] Commit this logical change.

### Task 2: Make the fastlane supervised and fail-safe

**Files:**
- Modify: `sniper/services/memecoin_fastlane.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `tests/test_memecoin_fastlane_decides.py`
- Modify: `tests/test_memecoin_sweep_liveness.py`
- Create: `tests/test_memecoin_fastlane_supervision.py`

- [ ] Add tests for setup recovery, worker restart, stale fallback, and sweep exclusion.
- [ ] Add tests for heartbeat and API status publication.
- [ ] Run the tests and confirm that they fail.
- [ ] Add the shared fastlane state and sanitized snapshot.
- [ ] Put worker setup inside its retry boundary.
- [ ] Add the worker supervisor and shared exit lock.
- [ ] Run the slow sweep when the fastlane state is stale or failed.
- [ ] Publish the fastlane snapshot in the heartbeat and status APIs.
- [ ] Run focused fastlane, liveness, runner, and status tests.
- [ ] Commit this logical change.

### Task 3: Run funded hooks before paper hooks

**Files:**
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `sniper/services/memecoin_follow_activity.py`
- Modify: `tests/test_memecoin_runner.py`
- Modify: `tests/test_memecoin_follow_activity.py`

- [ ] Add order tests for the global and follow entry paths.
- [ ] Add a global test for a paper exception after a funded call.
- [ ] Assert that a retry does not call the funded hook again.
- [ ] Run the tests and confirm that they fail.
- [ ] Move both funded hooks before paper fan-out.
- [ ] Retain the dedup row after a funded call when paper fails.
- [ ] Keep the paper-only retry behavior when no funded hook exists.
- [ ] Run focused global and follow tests.
- [ ] Commit this logical change.

### Task 4: Separate failed native-SOL reads from zero

**Files:**
- Modify: `sniper/execution/solana_wallet.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `tests/test_memecoin_solana_wallet.py`
- Modify: `tests/test_memecoin_real_runner.py`
- Modify: `tests/test_memecoin_real_alerts.py`

- [ ] Add typed-result tests for success, valid zero, RPC error, JSON-RPC error, and malformed data.
- [ ] Add entry and gas-alert tests for a failed read.
- [ ] Run the tests and confirm that they fail.
- [ ] Add `NativeSolBalanceRead` and `sol_balance_read()`.
- [ ] Keep `sol_balance()` as the compatibility wrapper.
- [ ] Use the typed function in the funded context.
- [ ] Reject an unavailable entry balance without a low-funds claim.
- [ ] Suppress GAS-LOW alerts during read failures.
- [ ] Run focused wallet, risk, runner, and alert tests.
- [ ] Commit this logical change.

### Task 5: Print the chain-aware startup authority

**Files:**
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `tests/test_memecoin_runner_loop.py`

- [ ] Add a helper test that counts each funded chain separately.
- [ ] Run the test and confirm that it fails.
- [ ] Add the helper and use it in the startup log.
- [ ] Run focused runner-loop tests.
- [ ] Commit this logical change.

### Task 6: Verify the complete branch

- [ ] Run all focused and adjacent memecoin tests for the changed modules.
- [ ] Run `python3 scripts/validate_schema_refs.py`.
- [ ] Run `python3 -m compileall -q sniper scripts`.
- [ ] Run `git diff --check`.
- [ ] Inspect every commit and the full branch diff against `origin/main`.
- [ ] Complete the Knowledge Sync Gate in the final handoff.
- [ ] Do not push, merge, deploy, or change wallet authority without operator approval.
