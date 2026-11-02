# Memecoin Two-Day Audit and Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit every file merged on July 18-19, repair every proven defect, and deploy one verified pinned release for funded trading.

**Architecture:** Review the full first-parent range `ee9e8a1aa679a81debb788fb153d8d884ddc51bf..456e7c16025cd8960076758eb3c318747a3f8050` by risk surface, starting with funded execution and ending with read-only discovery/UI. Every behavioral repair uses a failing focused regression before the smallest implementation, one logical commit, and wider verification. Deployment uses `scripts/deploy_memecoin_runtime.sh`; the funded runner must execute only from `.runtime/memecoin/current`.

**Tech Stack:** Python 3.13, Flask, SQLite, pytest/xdist, vanilla JavaScript/CSS, Bash, GitHub Actions.

## Global Constraints

- Do not create paper-first or data-wait gates for already-authorized funded policy behavior.
- Do not manually place a funded test trade.
- Preserve fail-closed quote, confirmation, chain-identity, and reconciliation behavior.
- Preserve existing pre-cutover bags and the funded cohort cutoff.
- Use exact executable proceeds for funded exit decisions; display prices remain observational.
- Keep the mutable web checkout separate from the pinned funded runtime.
- Do not mutate analyzer, Polymarket, Hyperliquid, or legacy/frozen pipelines.
- Run the Knowledge Sync Gate before every PR; bugfix-only changes may state `Knowledge delta: none` with a reason.

---

### Task 1: Establish the exact audit surface and clean baseline

**Files:**
- Inspect: every path returned by `git diff --name-status ee9e8a1a..origin/main`
- Record: `docs/superpowers/plans/2026-07-19-memecoin-two-day-audit-hardening.md`

**Interfaces:**
- Consumes: first-parent merge history and current `origin/main`
- Produces: exact 78-file review inventory and baseline test evidence

- [ ] **Step 1: Confirm branch and full changed-file inventory**

Run:

```bash
git status --short --branch
git log origin/main --first-parent --since='2026-07-18 00:00:00 -0400' --oneline
git diff --name-status ee9e8a1a..origin/main
git diff --check ee9e8a1a..origin/main
```

Expected: isolated `review/jul18-19` worktree; 78 changed files; no whitespace errors.

- [ ] **Step 2: Run the baseline memecoin suite and static checks**

Run:

```bash
python3 -m pytest tests/test_memecoin*.py -q
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts
node --check static/app.js
bash -n scripts/deploy_memecoin_runtime.sh scripts/memecoin_watchdog.sh
```

Expected: zero test failures, zero schema violations, and all syntax checks exit 0.

### Task 2: Preserve the real on-chain close cause

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/execution/solana_exit_executor.py`
- Modify: `sniper/execution/evm_exit_executor.py`
- Test: `tests/test_memecoin_exit_executor.py`
- Test: `tests/test_memecoin_evm_exit_executor.py`

**Interfaces:**
- Consumes: executor `exit_reason` values `manual`, `mirror`, and funded policy reasons such as `b_stop`
- Produces: `full_exit_fill_kind(exit_reason) -> manual_close | mirror_exit | bracket_exit`

- [ ] **Step 1: Verify the regression fails on current main**

Add cases asserting `manual -> manual_close`, `mirror -> mirror_exit`, and `b_stop -> bracket_exit` for both executors, then run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_exit_executor.py tests/test_memecoin_evm_exit_executor.py -q
```

Expected before the repair: manual and policy cases fail because both are stored as `mirror_exit`.

- [ ] **Step 2: Implement the shared mapping and use it in both executors**

Implement in `db_memecoin.py`:

```python
def full_exit_fill_kind(exit_reason):
    reason = str(exit_reason or "").strip().lower()
    if reason == "manual" or reason.startswith("manual_"):
        return "manual_close"
    if reason in ("mirror", "b_mirror", "mirror_exit"):
        return "mirror_exit"
    return "bracket_exit"
```

Pass its return value as `kind` when Solana and EVM confirmed full exits write their fill.

- [ ] **Step 3: Verify and commit the repair**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_exit_executor.py tests/test_memecoin_evm_exit_executor.py tests/test_memecoin_manual_close_mode_guard.py -q
git diff --check
git commit -am 'fix(memecoin): preserve real exit fill cause'
```

Expected: all focused tests pass and one logical commit is created.

### Task 3: Audit the funded entry-to-exit path end to end

**Files:**
- Inspect: `sniper/services/memecoin_market_state.py`
- Inspect: `sniper/services/memecoin_funded_policy.py`
- Inspect: `sniper/services/memecoin_real_runner.py`
- Inspect: `sniper/services/memecoin_funded_reconcile.py`
- Inspect: `sniper/execution/solana_swap_executor.py`
- Inspect: `sniper/execution/evm_swap_executor.py`
- Inspect: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_market_state.py`
- Test: `tests/test_memecoin_funded_policy.py`
- Test: `tests/test_memecoin_funded_contract_storage.py`
- Test: `tests/test_memecoin_funded_reconcile.py`
- Test: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Consumes: normalized event identity, exact entry quote, current portfolio reservations, and immutable funded contract bytes
- Produces: a funded entry whose size and exit rails cannot drift after reservation or restart

- [ ] **Step 1: Trace each invariant through nomination, reservation, execution, persistence, restart, and exit**

Verify directly in code that contract hashes cover classification, sizing, policy, identity, expectation, invalidations, and source; global cap checks include open and reserved exposure; chain-local funds/gas remain independent; exact post-peak confirmation occurs immediately before broadcast; and legacy positions remain outside the cohort gate.

- [ ] **Step 2: Exercise the full funded path**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_market_state.py tests/test_memecoin_funded_policy.py tests/test_memecoin_funded_contract_storage.py tests/test_memecoin_funded_reconcile.py tests/test_memecoin_real_runner.py tests/test_memecoin_real_deploy.py tests/test_memecoin_real_rails_v2.py -q
```

Expected: zero failures. Any proven invariant break must first receive a focused failing regression, then the smallest repair and a separate commit.

### Task 4: Audit deployment, startup, and runtime reconciliation

**Files:**
- Inspect: `scripts/deploy_memecoin_runtime.sh`
- Inspect: `scripts/memecoin_runner_loop.py`
- Inspect: `scripts/memecoin_watchdog.sh`
- Inspect: `scripts/validate_memecoin_funded_contracts.py`
- Test: `tests/test_memecoin_runtime_deploy.py`
- Test: `tests/test_memecoin_runtime_verify.py`
- Test: `tests/test_memecoin_runner_loop_cadence.py`
- Test: `tests/test_memecoin_reservation_recovery.py`

**Interfaces:**
- Consumes: merged SHA, funded DB, deployment manifest, cutoff, and process heartbeat
- Produces: atomic pinned release activation with backup, validation, reconciliation, and rollback

- [ ] **Step 1: Trace failure and rollback paths**

Verify that validation cannot mutate the funded DB, startup budget covers a slow first sweep, failed health/reconciliation restores both symlink and environment, and deploy cutoffs cannot race a reservation.

- [ ] **Step 2: Run deployment tests and shell checks**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py tests/test_memecoin_runner_loop_cadence.py tests/test_memecoin_reservation_recovery.py -q
bash -n scripts/deploy_memecoin_runtime.sh scripts/memecoin_watchdog.sh
```

Expected: zero failures and zero shell syntax errors.

### Task 5: Audit Coin Radar discovery, triage, API, and UI boundaries

**Files:**
- Inspect: `sniper/services/memecoin_universe.py`
- Inspect: `sniper/services/memecoin_discovery_external.py`
- Inspect: `sniper/services/memecoin_radar_triage.py`
- Inspect: `scripts/memecoin_discovery_worker.py`
- Inspect: `sniper/routes/api_memecoin.py`
- Inspect: `sniper/web/server.py`
- Inspect: `static/app.js`
- Inspect: `static/styles.css`
- Test: `tests/test_memecoin_universe.py`
- Test: `tests/test_memecoin_discovery_external.py`
- Test: `tests/test_memecoin_radar_triage.py`
- Test: `tests/test_memecoin_incoming.py`
- Test: `tests/test_memecoin_ui.py`

**Interfaces:**
- Consumes: observed funded signals and external discovery candidates
- Produces: read-only operator radar state; it must not bypass funded entry gates or create trades

- [ ] **Step 1: Trace write and trade boundaries**

Verify external candidates remain discovery-only, LLM output cannot authorize or size a funded entry, stale/failed external fetches remain explicit, API pagination/filtering is bounded, and UI actions target chain-normalized identities.

- [ ] **Step 2: Run radar/API/UI tests and JavaScript syntax**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_universe.py tests/test_memecoin_discovery_external.py tests/test_memecoin_radar_triage.py tests/test_memecoin_incoming.py tests/test_memecoin_ui.py tests/test_memecoin_api.py tests/test_memecoin_api_actions.py -q
node --check static/app.js
```

Expected: zero failures and no JavaScript syntax errors.

### Task 6: Integrate, verify, merge, deploy, and reconcile

**Files:**
- Verify: all paths changed on the audit branch
- Runtime: `.runtime/memecoin/current`
- Manifest: `data/memecoin_deploy.json`
- Ledger: `sniper_tracker.db`

**Interfaces:**
- Consumes: clean review branch and green CI
- Produces: merged `origin/main`, exact pinned funded runtime, healthy web/runtime processes, and corrected ledger provenance

- [ ] **Step 1: Run the complete validation gate**

Run:

```bash
python3 -m pytest tests/test_memecoin*.py -q
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts
node --check static/app.js
bash -n scripts/deploy_memecoin_runtime.sh scripts/memecoin_watchdog.sh
git diff --check origin/main...HEAD
```

Expected: zero failures or syntax/schema violations.

- [ ] **Step 2: Push, open the PR, and satisfy the Knowledge Sync Gate**

The PR body must end with the 11-question Knowledge Delta Report. For the exit taxonomy bugfix, attest: `Knowledge delta: none. Reason: bugfix-only; existing exit semantics are unchanged and ledger provenance is corrected.` Wait for required CI checks and merge only when green.

- [ ] **Step 3: Deploy the exact merged SHA**

Run the repository deployment script against the merged SHA. Confirm backup creation, release manifest verification, funded reconciliation, symlink activation, watchdog health, and authenticated status endpoint `200`.

- [ ] **Step 4: Correct the one proven historical provenance row**

After a fresh SQLite backup, update only fill transaction `3fRcVTKVd8h2tLvX22WUf5fx4pMopiqX2Aytg9GA6yvgUMrccreMgLLBr9RHEbsz4Brybt15iNhsFGNRe1dCb7F3` for position `8081` from `mirror_exit` to `manual_close`, guarded by `exit_reason='manual'`. Verify exactly one row changed and realized PnL remains `16.35051200000001`.

- [ ] **Step 5: Verify live state**

Confirm `origin/main`, release manifest SHA, and `.runtime/memecoin/current` agree; runner and web process start times are current; authenticated status/positions endpoints return `200`; funded reconciliation reports no orphaned reservations or contract mismatch; and the six pre-cutover bags remain excluded from the new cohort.
