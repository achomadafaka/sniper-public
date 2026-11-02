# Memecoin Opportunity Radar Retention and Throttling — design

**Date:** 2026-07-26
**Status:** approved design supplement
**Scope:** isolated opportunity-capture storage, cleanup, and read-RPC pressure control
**Authority:** capture-only; no funded imports, credentials, tables, positions, signing, or execution
**Parent design:** `2026-07-26-memecoin-executable-launch-opportunity-engine-design.md`

## 1. Problem and measured baseline

The fresh opportunity ledger grew to 42 MB in about five minutes. A direct ten-second measurement
added 2.27 MB and 572 raw events, or about 57 raw events per second. Unbounded, that rate can approach
20 GB per day and exhaust a laptop even though the Radar is only a research capture surface.

The dominant storage consumers were `op_raw_events`, `op_venue_event_facts`,
`op_canonical_events`, and `op_state_snapshots`. Compression of raw payloads alone does not bound the
derived tables or indexes. Periodic full resets bound storage but destroy the matured outcomes needed
to discover an exploit.

The worker also needs one shared read-RPC pressure controller. WebSocket capture must never block on
recovery, persisted health writes must not occur per message, and a recovery backlog must not starve
confirmation or current-state reads.

## 2. Goals

1. Bound hot opportunity storage on a normal laptop.
2. Preserve compact, point-in-time evidence needed for the Insight Leaderboard.
3. Keep the WebSocket receive path independent from cleanup and read-RPC recovery.
4. Make storage pressure, cleanup, rate limiting, and data loss visible and fail closed.
5. Preserve the parent design's source-gap, confirmation, executability, and no-funded-authority
   contracts.

Not goals: indefinite raw replay, funded trading, paper entries, model training, X integration, or
changing the Radar's actionability rule.

## 3. Storage architecture

Use two isolated SQLite databases:

```text
data/memecoin_opportunity.db          hot detailed ledger
data/memecoin_opportunity_insights.db compact research evidence
```

The hot ledger remains the live worker's single-writer state. The insight ledger has the same worker
as its only writer and is read-only to API/reporting code. Neither database contains funded state.
Both files are created with owner-only permissions.

### 3.1 Hot detailed ledger

Normal retention is 30 minutes. It contains raw observations, canonical events/corrections, venue
facts, snapshots, landmarks, quotes, counterfactuals, predictions, outcomes, frontiers, gaps, runtime
health, and model metadata.

An episode is eligible for normal cleanup only when:

- its launch is older than 30 minutes;
- every counterfactual entry has a terminal outcome or explicit censor;
- it has no tentative canonical event lacking a finalized or orphaned correction;
- no unresolved source gap or reorg still needs the episode for repair; and
- its required compact insight rows have committed successfully.

Active, unresolved, unprojected, or repair-relevant episodes are never removed by normal cleanup.

### 3.2 Compact insight ledger

Keep seven days of individual samples and indefinite daily aggregates. This database stores no raw
payloads, full transaction logs, full state snapshots, or duplicate confirmation records.

`op_insight_samples` has one immutable row for each `WOULD_FIRE`, plus one deterministic closest-state
sample and one activity/progress-matched control per launch when available. Its identity includes the
launch, landmark, rule version, quote version, barrier contract, and sample role.

Each sample preserves:

```text
network_id, venue_id, lifecycle, launch_id, creator_entity_id
landmark kind/time/progress, age_ms, actionability zone/rank, would_fire
curve velocity/acceleration, capital velocity/acceleration, buy/sell/net flow
effective buyer count/velocity, entropy, HHI, top ownership, repeat/round-trip share
creator-linked allocation/buys/sells, migration distance
$25 entry impact, immediate round-trip return, exit capacity, route state, latency
outcome kind, terminal/last executable return, elapsed_ms, exit failure, censor reason
rule/model/decoder/feature versions, confirmation, completeness, source timestamps
```

Core fields are typed columns. Versioned optional features use a canonical JSON blob compressed with
the Python standard library's `zlib`; the blob is not required for the core leaderboard.

`op_insight_daily_aggregates` stores per UTC day, venue, lifecycle, rule version, sample role, zone,
and registered pattern ID:

```text
sample count, profit/loss/exit-failure/migration/timeout/censor counts
sum executable return, sum squared return, positive/negative return sums
time-to-profit/loss sums, quote/latency coverage, first/last observation
```

Aggregation is additive and idempotent by sample identity. Individual samples older than seven days
are deleted only after their daily aggregate is committed.

## 4. Cleanup authority and invariants

This supplement narrows the parent design's append-only promise to the hot retention window; compact
sample identities and daily aggregates preserve the long-lived evidence. Raw and canonical facts stay
immutable during normal operation. Retention deletes are permitted only
inside the worker's `OpportunityRetentionManager` and only on the worker connection.

SQLite delete triggers call the connection-local SQL function
`op_retention_delete_allowed()`. The function returns false by default. The retention manager enables
it only inside one explicit transaction and restores false in `finally`; a crash removes the process
and therefore the authority. Updates remain prohibited without exception.

Cleanup order follows foreign keys from leaves to roots. The manager selects explicit launch IDs,
deletes only rows reachable from that eligible set, removes unreferenced canonical corrections and
raw rows last, and verifies before commit:

```text
zero selected entries without compact insight rows
zero selected entries without terminal outcome/censor
zero selected tentative events lacking finalized/orphaned correction
zero selected rows referenced by retained episodes
zero foreign-key violations
```

Any mismatch rolls back the entire chunk, marks storage health degraded with
`RETENTION_INTEGRITY_FAILURE`, and stops cleanup. It does not broaden the delete set.

Cleanup runs every 60 seconds only when the ingest queue is below 25% of capacity. One transaction
deletes at most 25 launch episodes or 2,500 rows, whichever is reached first, then yields to ingest.

SQLite uses WAL mode, `wal_autocheckpoint=1000`, `auto_vacuum=INCREMENTAL`, and
`journal_size_limit=16777216`. The retention manager runs a passive checkpoint and a bounded
incremental vacuum after successful cleanup. It never runs a blocking full `VACUUM` while capture is
live.

`journal_size_limit` is load-bearing for the storage caps below, not a tuning detail (added
2026-07-29). A passive checkpoint recycles the WAL in place and never shrinks the file, so without a
bound the `-wal` file parks at its all-time peak and counts against the cap forever — observed at
64.9 MB on a 6.6 MB hot database, fully checkpointed (`58/58, blocked=0`) with nothing pinning it.
The limit is a floor SQLite trims *down to*, and the trim lands on the first write after a
checkpoint resets the WAL, so the bound must sit below the bloat it is meant to reclaim.

## 5. Storage caps

Storage usage is what `database_storage_bytes` measures: live pages (`page_count - freelist_count`,
times `page_size`) plus WAL frames a reader is currently preventing SQLite from checkpointing. It is
**not** the physical length of the `.db`, `-wal`, and `-shm` files — a preallocated WAL that is
already checkpointed is reusable capacity, and counting it would pause capture while the live
database was shrinking.

```text
normal retention: 30 minutes
soft cap:         128 MiB   (MEMECOIN_OPPORTUNITY_HOT_SOFT_CAP_MB, default 128)
hard cap:         256 MiB   (MEMECOIN_OPPORTUNITY_HOT_HARD_CAP_MB, default 256)
minimum emergency retention: 5 minutes
```

> Corrected 2026-07-29. This section previously read "400 MB / 750 MB" and described the caps as
> covering the raw files. Neither ever matched the implementation: the shipped defaults have been
> 128/256 MiB since `887516d1`, landed the same day as this spec, and never changed. The numbers
> here are now the code's, verified against a live `op_runtime_status` row
> (`hot_soft_cap_bytes: 134217728`, `hot_hard_cap_bytes: 268435456`).

At the soft cap, cleanup runs immediately and may remove eligible episodes older than 30 minutes in
additional bounded chunks.

At the hard cap, retention shortens progressively toward five minutes and deletes the oldest
completed/projected episodes first. It still never deletes active, unresolved, unprojected, or
repair-relevant episodes.

If the hard cap cannot be restored without violating those invariants, the worker stops accepting new
source events, records `STORAGE_CAP_REACHED`, closes source sockets, and leaves only the health and
maintenance loop active. The Radar remains `DATA INCOMPLETE`. It never silently
drops events, resets the ledger, reports healthy, or allows the database to grow without a bound.

The compact insight database has a 1 GB soft cap and 1.5 GB hard cap. It first deletes individual
samples older than seven days whose aggregates exist. If still above the hard cap, it stops capture
with `INSIGHT_STORAGE_CAP_REACHED`; daily aggregates are never automatically deleted.

## 6. Read-RPC pressure control

One `OpportunityRpcGovernor` is shared by Pump.fun recovery, Bags recovery, Solana confirmation, and
read-only quote/enrichment calls for the same provider.

Priority is:

```text
confirmation/current truth > current quote probe > historical recovery
```

Default limits:

```text
maximum concurrent calls: 2
initial and maximum rate:  2 requests/second (live no-throttle floor; raise only after measurement)
minimum adaptive rate:    1 request/second
maximum adaptive rate:    2 requests/second
recovery page:             10 signatures
```

HTTP `429` honors a valid `Retry-After` value capped at 30 seconds. Otherwise it uses exponential
full-jitter backoff from 0.5 to 8 seconds. A `429` halves the current rate. Thirty consecutive
successful calls increase it by 0.5 requests/second up to the maximum. Recovery persists its page
cursor before requesting the next page and yields at least 250 ms between pages.

Provider timeout, malformed result, and missing confirmed transaction remain visible recovery gaps;
they are not treated as successful dedupe. A repeated raw observation may be stored, but one canonical
event identity is applied once. WebSocket receipt and in-memory transport liveness never wait for an
RPC permit.

## 7. Write coalescing

Persist source health at most once per second per unchanged source state. Status or blocker changes
write immediately. `OpportunityCaptureRuntime.drain()` writes runtime depth/last-event state once per
source per drain batch, not once per event.

Canonical, landmark, counterfactual, and outcome identities remain idempotent. Repeated confirmation
polls do not append a second identical correction. Cleanup and insight projection use immutable
identity inserts and tolerate exact replay, but conflicting content fails closed.

## 8. Health and UI

The API exposes a storage/pressure object:

```text
hot_bytes, insight_bytes, wal_bytes
hot_retention_minutes, insight_retention_days
last_cleanup_at, deleted_episodes, deleted_rows, projected_samples
soft_cap_active, hard_cap_active, storage_blocker
rpc_rate, rpc_inflight, rpc_429_count, rpc_backoff_until
```

The Radar shows compact chips for storage and RPC health. `STORAGE_CAP_REACHED`,
`INSIGHT_STORAGE_CAP_REACHED`, and `RETENTION_INTEGRITY_FAILURE` produce `DATA INCOMPLETE` and freeze
only affected opportunity movement. No cleanup metric is presented as trading edge.

## 9. Activation and migration

Activation is a controlled restart of only the isolated opportunity worker. The current hot ledger is
projected into the compact insight database before retention is enabled. Because incremental vacuum
must be selected before normal table growth, activation creates one fresh hot ledger after projection;
per the operator's current instruction, no backup copy is required.

The activation gate requires:

```text
focused schema/projection/cleanup/RPC/runtime/UI tests pass
insight projection count and fingerprints reconcile
fresh hot schema has auto_vacuum=INCREMENTAL and foreign_keys=ON
worker command/path/PID are exact
authenticated API returns 200
all enabled sources are healthy and zero open gaps after warm-up
receive queue remains bounded
measured growth remains below the configured caps
```

The web service and funded TPU sidecar are not restarted.

## 10. Verification

Focused tests cover:

- projection-before-delete and idempotent insight replay;
- active/unresolved/tentative/gapped episodes retained;
- eligible completed episodes deleted in dependency order;
- retention authority defaults false and restores after exceptions;
- foreign-key and fingerprint mismatch rollback;
- 30-minute, soft-cap, hard-cap, and five-minute emergency behavior;
- storage-cap fail-closed status;
- seven-day sample retention and aggregate preservation;
- one-second health and per-batch drain coalescing;
- shared RPC concurrency/rate priority, `429`, `Retry-After`, jitter, and adaptive recovery;
- WebSocket receipt remains independent from cleanup/RPC waits;
- API/UI storage truth and default-OFF/funded-boundary guards.

Live verification samples file growth, row growth, WAL size, receive queue, RPC backoff, source ages,
open gaps, and authenticated API output. A short steady-state sample is operational evidence, not proof
that the cap can never fail; cap enforcement tests remain authoritative.
