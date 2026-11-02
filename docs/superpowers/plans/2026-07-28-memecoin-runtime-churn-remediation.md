# Memecoin Runtime Churn Remediation Implementation Plan

> **For Codex:** Use TDD for each behavior change and verify the exact merged runtime after deployment.

**Goal:** Stop measured holder-history, V6 web/admission, Telegram, proxy-auth, backup, and log churn while preserving funded and paper authority boundaries.

**Architecture:** Stable fact identity and current-only unknown evidence bound the main ledger. Batched V6 snapshots plus locked TTL caching bound report work. Durable admission checks enforce one open paper position per coin. Shared alert pacing, repo-relative backups, and user-owned rotation handle operational churn.

**Tech Stack:** Python 3.13, SQLite, Flask, JavaScript, pytest, Bash, logrotate.

---

### Task 1: Holder evidence history and cleanup

- [ ] Add failing tests for stable fact deduplication, unknown current-only storage, and the source-user query index.
- [ ] Implement the stable evidence hash, unknown-history suppression, and schema index.
- [ ] Add a dry-run-by-default bounded cleanup tool with fixture tests.
- [ ] Run focused holder and schema tests.
- [ ] Commit the logical change.

### Task 2: V6 report performance and browser overlap

- [ ] Add failing API cache, constant-query-count, and UI in-flight guard tests.
- [ ] Batch-load report facts and add locked five-second route snapshots.
- [ ] Add the browser `try/finally` guard and cache invalidation after censor.
- [ ] Run focused V6 API, report, and UI tests.
- [ ] Commit the logical change.

### Task 3: V6 one-open-coin admission

- [ ] Add failing tests for existing-open rejection, same-coin claim serialization, and finalization race handling.
- [ ] Add schema version 8 and the partial open-coin lookup index.
- [ ] Enforce the invariant at queued and direct admission boundaries.
- [ ] Run focused admission, ledger, engine, and migration tests.
- [ ] Commit the logical change.

### Task 4: Telegram flood control

- [ ] Add failing tests for per-chat pacing and one 429 `retry_after` retry without traceback logging.
- [ ] Implement the shared limiter, typed flood-wait parsing, counter, and retry.
- [ ] Run Telegram and funded alert queue tests.
- [ ] Commit the logical change.

### Task 5: Proxy auth and local operations

- [ ] Add failing browser-stream auth tests; issue a bounded signed session cookie after valid Basic Auth and retain the loopback trusted-proxy guard for optional local proxies.
- [ ] Replace the stale backup job with a tested repo-relative online-backup implementation.
- [ ] Add and dry-run a bounded local logrotate configuration.
- [ ] Run focused integration and operations tests.
- [ ] Commit the logical changes separately.

### Task 6: Broad verification and branch close

- [ ] Run focused suites, schema validation, JavaScript syntax checks, shell syntax checks, and `git diff --check`.
- [ ] Run the Knowledge Sync Gate and prepare the required Knowledge Delta Report.
- [ ] Review the complete branch diff and commit state.
- [ ] Fast-forward `main`, push `origin/main`, and record the merged SHA.

### Task 7: Controlled deployment and cleanup

- [ ] Verify the pre-change running release, process ownership, authenticated health, reservations, kill switch, backup, and sidecar.
- [ ] Deploy the merged SHA with explicitly deferred backup, then stop writers/watchdogs for maintenance.
- [ ] Delete unknown holder history in bounded batches, vacuum, and verify database integrity and size.
- [ ] Censor the three newer duplicate V6 paper positions while retaining the oldest.
- [ ] Create and verify a compact backup before removing the orphan oversized backup.
- [ ] Remove inactive `logs/stream.log`, rotate active logs, and stop exact stale Playwright process groups.
- [ ] Install the user backup/rotation schedules.
- [ ] Restart exact pinned funded, V6, and web releases.
- [ ] Verify post-deploy health, latency, concurrency, WAL, Telegram/proxy logs, database/index state, and runtime identity.
