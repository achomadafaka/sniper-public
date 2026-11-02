# Memecoin Pinned Runtime - Design

**Date:** 2026-07-11
**Status:** Approved for implementation by operator directive
**Scope:** Immutable deploy ref, state/code separation, watchdog validation, and atomic cutover

## Goal

Ensure a branch checkout, agent worktree, or local edit cannot silently change the code launched by the real-money watchdog.

## Layout

- Source repository: `/home/bigpapi/Desktop/Sniper`
- Release worktrees: `/home/bigpapi/Desktop/Sniper/.runtime/memecoin/releases/<full-sha>`
- Atomic current symlink: `/home/bigpapi/Desktop/Sniper/.runtime/memecoin/current`
- Mutable state root: `/home/bigpapi/Desktop/Sniper`
- Deploy manifest: `/home/bigpapi/Desktop/Sniper/data/memecoin_deploy.json`

The release is a detached, clean Git worktree. Mutable DBs, logs, credentials, locks, heartbeat, token store, and kill switch remain under the state root and are passed through explicit environment variables. No release worktree owns mutable trading state.

## Watchdog

The watchdog derives code root from its own resolved path and reads the deploy manifest from `SNIPER_STATE_ROOT`. Before launch it verifies:

- resolved code root equals the manifest release path;
- release `HEAD` equals manifest SHA;
- release worktree has no tracked changes;
- required state paths and environment files exist;
- real configuration validation passes.

Any mismatch logs a loud failure and sends a best-effort alert. It does not fall back to the source checkout. Process matching includes the resolved release path so an old and new release cannot both count as the same healthy runner. The existing run lock remains the final double-submit guard.

## Deploy Command

### 2026-10-09 host resource amendment

Validation selects only `test_memecoin*.py` files, including the mandatory funded core.
Unknown live SHA falls back to the full memecoin suite. Shared settings imports do
not pull Polymarket or Hyperliquid tests into this gate. Pytest runs with explicit
`-n 2`, CPU nice level 15, idle I/O priority, and the shared host lock
`/home/bigpapi/.cache/codex-pytest-full.lock`. The previous worker environment
override is removed because it did not override `pytest.ini`'s fixed worker count.
Schema, compile, reservation, reconciliation and rollback checks remain required.
Trading service resources are not changed. Individual test memory use is not capped.

`scripts/deploy_memecoin_runtime.sh <sha>`:

1. requires a full commit SHA reachable from `origin/main`;
2. requires a clean source repository for tracked files;
3. runs the focused memecoin suite, schema validation, and compile checks;
4. requires a current database backup and zero pending reservations;
5. creates or verifies the detached release worktree;
6. writes the manifest to a temporary file and atomically replaces it;
7. atomically switches the `current` symlink;
8. deliberately terminates the old runner only after the new release is ready;
9. invokes the pinned watchdog and verifies PID, SHA, heartbeat, auth, and exit-sweep freshness.

Failure before symlink switch leaves the old runner untouched. Failure after switch but before verified launch restores the previous symlink and relaunches the prior release. Deploy output records both SHAs and verification evidence.

## Cron

Cron invokes `.runtime/memecoin/current/scripts/memecoin_watchdog.sh`, never `Sniper/scripts/memecoin_watchdog.sh`. The `@reboot` entry uses the same pinned path. Updating cron is a one-time operator-state migration performed only after the first pinned release verifies healthy.

## Edge Cases

- Existing runner continues while a release is prepared.
- A missing or dangling current symlink fails closed.
- Garbage collection never removes current or previous release.
- A deploy of the current SHA is idempotent and does not restart unless `--restart` is supplied.
- A feature-branch SHA cannot be deployed.
- Untracked runtime files in the source checkout do not block deployment; tracked modifications do.
- Rollback uses the prior manifest SHA and the same verification path.

## Tests

- Manifest/path/SHA/dirty-tree validation.
- Atomic symlink switch and rollback using temporary repositories.
- Failure before and after switch.
- Process match includes release path.
- State paths resolve outside the release.
- Idempotent same-SHA deploy.
- Cron lines reference only the pinned current path.
