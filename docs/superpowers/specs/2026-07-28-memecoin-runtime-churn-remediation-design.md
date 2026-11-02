# Memecoin Runtime Churn Remediation Design

**Date:** 2026-07-28
**Status:** Approved by operator request

## Scope

Remove the measured database, web, admission, alert, authentication, backup, and log churn without changing funded trading policy or giving V6 funded authority.

## Verified failures

- `memecoin_holder_evidence_history` stores the poll clock in `evidence_hash`. Re-reading one upstream swap therefore creates a new history row every poll. Unknown evidence accounts for about 98.4% of the table and has no historical transition value.
- `mark_holder_poll_success()` filters by `source_user_id`, but the current-table indexes do not support that predicate.
- `/api/memecoin/v6-paper/summary` rebuilds the same report several times, performs position-shaped query loops, has no request cache, and is polled every four seconds without an in-flight browser guard.
- V6 admission deduplicates only by origin event. Different wallet events can create multiple open paper positions for one `(network_id, mint)`.
- Telegram pacing is absent from the shared send path. The dedicated funded alert queue bypasses `_TelegramDispatcher`, so fixing that dispatcher alone would be incomplete. HTTP 429 responses are logged with tracebacks and dropped.
- The browser WebSocket `/stream` can loop on 401 behind a direct Cloudflared-to-Flask proxy. There is no nginx hop to inject the existing trusted-proxy marker, and the WebSocket constructor cannot attach a Basic Auth header itself.
- `scripts/daily_backup.sh` contains stale host paths and service names and is not scheduled. Active logs have no local rotation policy.

## Corrections to the initial diagnosis

- The WAL is not currently pinned. `scripts/wal_health.py` reports `stalled=0`; read-only connections already end transactions between sweeps. No `--reclaim` checkpoint or connection rewrite is justified.
- Tick retention already exists: unlinked tick data is kept for seven days and position-linked pools are retained by design. The current database is younger than that horizon. Do not shorten it or delete linked evidence.

## Holder evidence

History identity represents a stable observed fact, not collection time. The hash includes network, mint, source user, evidence state, normalized remaining value, source, upstream event identity/time, and reason. It excludes `observed_at` and `successful_poll_at`.

Unknown evidence remains in the current mirror because admission must fail closed. It is not appended to history. Positive and zero transitions remain auditable.

Add `idx_mc_holder_evidence_user(source_user_id)` for poll-success updates. Remove existing unknown history rows in bounded primary-key ranges while writers are stopped, checkpoint between batches, then vacuum and run integrity checks.

## V6 report path

Load entries, observations, decisions, action intents, and action fills once per report build. Group and decode them once in memory, then compute current/all/cohort summaries from the shared facts.

Protect summary and positions snapshots with one process-local lock and a five-second monotonic TTL. The first caller rebuilds; concurrent callers reuse the result. Censor mutations invalidate both snapshots.

The browser keeps a `_v6PaperLoading` guard in `try/finally`, preventing an interval tick from overlapping an outstanding V6 request.

## V6 one-open-coin admission

V6 remains paper-only. Admission permits at most one open position for `(network_id, mint)`.

- Existing-open candidates become terminal with `duplicate_open_coin` before quoting.
- One candidate per coin may be claimed concurrently. A second queued candidate waits while the first is in flight because the first may fail.
- Finalization rechecks the open coin inside the write transaction. A race loser becomes terminal `duplicate_open_coin` and does not create an entry.
- Low-level ledger fixture methods remain capable of representing historical duplicate data. The invariant is enforced at admission boundaries and indexed for fast lookup.
- Existing production duplicates are censored, newest first, preserving the oldest position and the full audit trail.

Schema version advances to 8 with a partial lookup index on open position coin identity. This is not a unique index because historical databases may contain duplicates during migration.

## Telegram

Apply a shared per-chat limiter in `send_telegram()`, covering every caller. The default minimum gap is three seconds. Parse Telegram 429 JSON, sleep for `retry_after`, increment a flood-wait counter, log one warning without a traceback, and retry once. Other failures preserve current fail-soft behavior.

## Browser stream authentication

A successful Basic Auth request issues a bounded `HttpOnly`, `SameSite=Strict` dashboard-session cookie. Its value is an HMAC over the configured dashboard identity and a 12-hour time bucket, so password rotation and expiry invalidate it without server-side session state. Browser WebSocket and EventSource handshakes carry the cookie automatically and may pass the Flask basic-auth guard. Tampered or expired cookies remain unauthorized.

The exact trusted-proxy marker from a loopback peer remains an optional bypass for a future authenticated local proxy. Missing markers and non-loopback peers remain unauthorized.

## Backups and logs

Replace the stale backup script with a repo-relative SQLite online-backup job. Write to a temporary file, run `integrity_check`, atomically rename, and retain two daily backups. Schedule it in the operator crontab after deployment.

Add a user-owned logrotate configuration with bounded rotation for active large logs. Use `copytruncate` because existing processes own their descriptors. Remove the inactive February `logs/stream.log` and the orphan post-health database copy only after a new compact, verified backup exists.

## Activation and recovery

1. Merge and push verified code to `main`.
2. Deploy the exact merged SHA with deferred pre-deploy backup because the known-bad database is oversized; this is explicitly recorded as not a fresh backup.
3. Stop database writers and their watchdogs, run the bounded holder-history cleanup, vacuum, and verify integrity.
4. Create and verify a new compact online backup before removing old oversized copies.
5. Deploy/restart funded, V6 paper, and web runtimes from immutable release paths.
6. Verify manifest, symlink, actual process command, authenticated health, heartbeat, reservations, kill switch, backup, TPU sidecar, V6 worker, web latency, WAL health, and logs.

Rollback uses the prior immutable runtime release and preserved pre-cleanup backup. V6 duplicate censors are auditable state changes and are not automatically reversed.

## Non-goals

- No funded policy, sizing, route, signing, or broadcast change.
- No V6 funded authority.
- No tick-retention change.
- No aggressive WAL checkpoint.
- No production analyzer batch or live research fetch.
