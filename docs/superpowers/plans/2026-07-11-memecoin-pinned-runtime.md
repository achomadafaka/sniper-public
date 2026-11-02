# Memecoin Pinned Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch the live memecoin service only from an immutable verified release SHA with mutable state kept outside the release.

**Architecture:** A Python verifier owns manifest/path checks; shell wrappers perform atomic release and process operations. The watchdog resolves its own pinned release and never falls back to the source checkout.

**Tech Stack:** Bash, Python 3.13, Git worktrees, JSON, pytest, cron

## Global Constraints

- Deploy only full SHAs reachable from `origin/main`.
- Do not change cron or restart live service until code is merged, DB is backed up, pending count is zero, and verification passes.
- Failure restores the previous release.

---

### Task 1: Explicit State Paths

**Files:**
- Create: `sniper/util/state_paths.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/feeds/fomo_feed.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `sniper/services/memecoin_arming.py`
- Modify: `sniper/settings/memecoin.py`
- Modify: `tests/test_memecoin_run_lock.py`
- Create: `tests/test_memecoin_state_paths.py`

**Interfaces:**
- Produces: `state_root() -> Path` and `state_path(*parts) -> Path`

- [ ] Add default-root, explicit-root, DB, lock, heartbeat, feed snapshot, token-store, kill-switch, keypair-default, and env-load tests.
- [ ] Run tests and confirm paths currently bind to code CWD.
- [ ] Route memecoin DB, lock, heartbeat, feed snapshot, token store, kill switch, keypair defaults, logs, PID, and env-file load through `SNIPER_STATE_ROOT` while leaving other services unchanged.
- [ ] Rerun runner/health/settings tests; commit `refactor(memecoin): separate runtime state from code`.

### Task 2: Release Verifier and Pinned Watchdog

**Files:**
- Create: `scripts/memecoin_runtime_verify.py`
- Modify: `scripts/memecoin_watchdog.sh`
- Create: `tests/test_memecoin_runtime_verify.py`

**Interfaces:**
- Produces: `verify_release(code_root, state_root, manifest) -> Verification`

- [ ] Add manifest missing, dangling path, SHA mismatch, dirty release, missing env, invalid real policy, and healthy cases using temporary Git repositories.
- [ ] Run tests and confirm current watchdog hardcodes mutable main.
- [ ] Make watchdog derive resolved code root, export absolute state paths, verify before launch, and include release path in process match.
- [ ] Rerun watchdog/health tests; commit `fix(memecoin): launch watchdog from pinned release`.

### Task 3: Atomic Deploy and Rollback

**Files:**
- Modify: `.gitignore`
- Create: `scripts/deploy_memecoin_runtime.sh`
- Create: `tests/test_memecoin_runtime_deploy.py`

**Interfaces:**
- Command: `scripts/deploy_memecoin_runtime.sh <full-sha> [--restart] [--dry-run]`

- [ ] Add temporary-repo tests for unreachable SHA, tracked dirt, pending reservations, pre-switch failure, post-switch rollback, same-SHA idempotence, and protected current/previous releases.
- [ ] Run tests and confirm deploy command is absent.
- [ ] Ignore `.runtime/`; implement release worktree creation, validation commands, atomic manifest/symlink switch, rollback trap, deliberate restart, and heartbeat/SHA verification.
- [ ] Rerun deploy tests and shell syntax checks; commit `feat(memecoin): add atomic pinned runtime deploy`.

### Task 4: Pre-Cutover Verification and Operator State

**Files:**
- Modify at cutover only: user crontab
- Modify at cutover only: `.fomo.env` (`MEMECOIN_REAL_MAX_HOLD_S=86400`)
- Create at cutover only: timestamped SQLite backup under `data/backups/`

**Interfaces:**
- Cron command: `/usr/bin/env bash /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_watchdog.sh`

- [ ] Verify feature branch full tests, schema refs, compile, diff check, and Knowledge Sync Gate.
- [ ] Merge through the approved branch-close flow; fetch the resulting `origin/main` SHA.
- [ ] Confirm zero pending reservations and reconcile every open real position.
- [ ] Back up the database and set the positive real time cap.
- [ ] Dry-run deploy, deploy merged SHA, verify heartbeat/auth/sweep/PID/SHA, then change both cron entries to the pinned path.
- [ ] Wait through two watchdog intervals and recheck one runner, current heartbeat, current sweep, no new orphan, and manifest SHA.
