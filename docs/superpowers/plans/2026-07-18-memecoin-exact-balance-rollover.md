# Memecoin Exact-Balance Rollover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the funded rollover engine on exact full-balance prices, preserve pre-cutover bags, and activate the corrected policy atomically in the pinned runtime.

**Architecture:** Persist a dedicated executable high-water and derive its unit price from exact proceeds divided by remaining open quantity. Select the complete default exit policy by an offset-aware rollout cutoff, while explicit position overrides remain authoritative. Extend the deploy script with an atomic activation/rollback path that writes the corrected-clock cutoff only after the old runner stops.

**Tech Stack:** Python 3.13, SQLite, pytest, Bash, pinned Git worktree deployment.

## Global Constraints

- No paper-evidence waiting gate; funded activation follows code, test, configuration, reservation, backup, and rollback gates only.
- Pre-cutover real positions retain legacy `TP=60`, `DERISK=0`, and default trail off.
- Post-cutover real positions use `TP=0`, `DERISK=40`, `TRAIL=30`, and `ARM=15`.
- Every active trail uses an exact full-balance preview on every sweep.
- No production DB mutation before the pinned deployment script reaches its normal cutover.
- No agents; execute inline in the current feature branch.

---

### Task 1: Whole-policy cohort and UTC validation

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Produces: `_position_in_rollover_cohort(pos) -> bool`
- Produces: `_default_exit_rails(pos) -> tuple[float, float, float, float]` containing TP, de-risk, trail, arm.
- Changes: `_effective_derisk_pct(pos, override=None)` and `effective_trail(pos, override=None)` consume the selected policy.

- [ ] **Step 1: Write failing cohort tests**

Add tests proving a pre-cutoff position returns `None` at +45% and `real_tp` at +61%, while a post-cutoff position returns `derisk` at +45% and has no hard TP after de-risk. Add an equivalent-instant offset test (`08:00:00Z` vs `04:00:01-04:00`).

- [ ] **Step 2: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q -k 'cohort or equivalent_instant'
```

Expected: current trail-only cohort lets pre-cutoff `DERISK=40` fire and compares offset timestamps as wall clocks.

- [ ] **Step 3: Implement whole-policy selection**

Add settings:

```python
MEMECOIN_REAL_LEGACY_TP_PCT = float(os.getenv("MEMECOIN_REAL_LEGACY_TP_PCT", "60"))
MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT = os.getenv(
    "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", ""
).strip()
```

Normalize aware timestamps to UTC before comparison. `_default_exit_rails` returns legacy `(legacy_tp, 0, 0, arm)` before cutoff and configured rails after cutoff. Apply that helper to full TP, de-risk, nomination, and confirmation. Explicit override fields continue to replace selected defaults.

- [ ] **Step 4: Add fail-closed validation tests and implementation**

When real execution is enabled and configured trail is positive, reject empty, naive, or unparseable cutoffs. Validate finite nonnegative legacy TP.

- [ ] **Step 5: Verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_risk_settings.py -q
```

- [ ] **Step 6: Commit**

```bash
git add sniper/settings/memecoin.py sniper/services/memecoin_real_runner.py tests/test_memecoin_real_runner.py
git commit -m "fix: cohort the complete funded rollover policy"
```

### Task 2: Persist and use executable high-water

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Adds column: `memecoin_positions.executable_peak_price REAL`
- Produces: `db_memecoin.update_position_executable_peak(conn, position_id, price, ts)`
- Produces: `_executable_unit_price(conn, pos, preview) -> float | None`

- [ ] **Step 1: Write failing exact-price regressions**

Add full-sweep tests for:

```text
4663: derisk at 1.4 -> exact peak 3.0 -> exact current 2.0 -> real_trail closes
priced: display peak 2.0, exact 1.3 -> executable peak remains 1.3
priced: exact peak 2.0 -> exact current 1.39 -> real_trail closes and audits allowed=1
```

- [ ] **Step 2: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q -k 'post_derisk or executable_peak'
```

Expected: 4663 stores `1.4` instead of `3.0`; priced venue has no executable high-water.

- [ ] **Step 3: Add schema and persistence helper**

Add `executable_peak_price` through the existing idempotent additive migration list. Add
`update_position_executable_peak(conn, position_id, price, ts)` beside
`update_position_peak`; it updates only the executable high-water and commits.

- [ ] **Step 4: Make exact preview the rollover input**

For any selected or explicit trail, request `preview_exit_fn` every sweep. Compute:

```python
qty = m.open_qty(conn, pos["id"])
exact_mid = expected_usdc / qty if qty > 0 else None
```

Advance only `executable_peak_price` from `exact_mid`. Use `exact_mid` for new-cohort TP/de-risk/trail decisions and reuse the same preview in confirmation. Blind 4663 no longer divides by original entry notional. Keep display `peak_price` for nominal UI history only.

- [ ] **Step 5: Verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_executable_exit_guard.py tests/test_memecoin_4663_exit_oracle.py tests/test_memecoin_derisk_recovery.py -q
```

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_real_runner.py tests/test_memecoin_real_runner.py
git commit -m "fix: trail funded exits on exact-balance peaks"
```

### Task 3: Complete exit-trigger audit coverage

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Audit detail code: `debounce_pending`
- Audit detail fields: `hits`, `required_hits`.

- [ ] **Step 1: Write failing debounce audit test**

Drive the first sweep of a two-sweep `b_stop` and assert one audit row with `allowed=0`, `code=debounce_pending`, `hits=1`, and `required_hits=2` while `exit_fn` remains uncalled.

- [ ] **Step 2: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q -k debounce_pending
```

Expected: zero audit rows.

- [ ] **Step 3: Record pending audit before continue**

Insert the audit row immediately after incrementing `stop_hits` and before the debounce `continue`. Do not mark it executable or call `exit_fn`.

- [ ] **Step 4: Verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q -k 'debounce or audit'
```

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_real_runner.py tests/test_memecoin_real_runner.py
git commit -m "fix: audit pending funded stop debounce"
```

### Task 4: Atomic funded rollover activation

**Files:**
- Modify: `scripts/deploy_memecoin_runtime.sh`
- Modify: `tests/test_memecoin_runtime_deploy.py`
- Modify: `.env.example`

**Interfaces:**
- CLI: `scripts/deploy_memecoin_runtime.sh <sha> --activate-rollover`
- Test seam: `MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE=<aware ISO>`
- Writes in state `.env`: legacy TP, active TP/de-risk/trail/arm, and rollout cutoff.

- [ ] **Step 1: Write failing activation and rollback tests**

Extend the deploy fixture with a state `.env`. Assert activation writes exactly:

```text
MEMECOIN_REAL_LEGACY_TP_PCT=60
MEMECOIN_REAL_TP_PCT=0
MEMECOIN_REAL_DERISK_PCT=40
MEMECOIN_REAL_TRAIL_PCT=30
MEMECOIN_REAL_TRAIL_ARM_PCT=15
MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT=<override>
```

Assert injected pre-switch and post-switch failures restore the prior `.env`, manifest, and release.

- [ ] **Step 2: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_runtime_deploy.py -q -k rollover
```

Expected: deploy CLI rejects `--activate-rollover`.

- [ ] **Step 3: Implement bounded cutover**

After release tests and the initial reservation check, but before switching: install rollback trapping; stop discovered funded runners; recheck reservations after the stop so an entry reserved during validation blocks cutover and restarts the old runner; back up state `.env`; generate the cutoff from the override seam or `real_utc_now_iso()` under the release/state root; rewrite the six keys atomically with `os.replace`; run exact-release configuration validation; switch/start/verify; restore `.env` and the old runtime/process on any failure. Dry-run must not mutate `.env` or stop a process.

- [ ] **Step 4: Verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py -q
```

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy_memecoin_runtime.sh tests/test_memecoin_runtime_deploy.py .env.example
git commit -m "feat: activate rollover atomically at pinned cutover"
```

### Task 5: Doctrine, full verification, PR, and production deploy

**Files:**
- Modify: `AGENTS.md`
- Modify: PR #171 body and Knowledge Delta Report
- Runtime-only: state `.env`, deployment manifest, pinned symlink, process.

- [ ] **Step 1: Correct doctrine**

Document executable high-water, whole-policy cohort, exact preview cadence, explicit override precedence, debounce audits, and atomic activation.

- [ ] **Step 2: Run fresh verification**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py -q
python3 -m pytest -q -k memecoin
python3 -m compileall -q sniper scripts/memecoin_runner_loop.py scripts/memecoin_runtime_verify.py
git diff --check
```

- [ ] **Step 3: Commit doctrine and push**

```bash
git add AGENTS.md
git commit -m "docs: lock exact-balance rollover invariants"
git push origin feat/memecoin-rollover-engine
```

- [ ] **Step 4: Update PR and Knowledge Sync**

Replace stale test counts, loss speculation, immediate-sale language, and display-price claims. Add the new design/spec knowledge delta and ensure required checks pass.

- [ ] **Step 5: Merge and refresh**

Merge PR #171 after green checks, fetch `origin/main`, and verify the merge SHA contains every implementation commit.

- [ ] **Step 6: Dry-run exact release**

```bash
scripts/deploy_memecoin_runtime.sh <merged-full-sha> --activate-rollover --dry-run
```

- [ ] **Step 7: Activate and deploy**

```bash
scripts/deploy_memecoin_runtime.sh <merged-full-sha> --activate-rollover
```

- [ ] **Step 8: Verify live state**

Confirm the pinned symlink and process use the merged SHA, heartbeat is fresh, runtime config reports legacy/new rails and an aware cutoff, the four pre-cutover positions resolve to legacy rails, and a synthetic read-only post-cutover evaluation resolves to rollover rails. Inspect the first live sweeps for exceptions and audit writes; do not submit manual trades.
