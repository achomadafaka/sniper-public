# V6 Residual Reliability Design

**Date:** 2026-07-28  
**Scope:** isolated V6 live-paper runtime only; funded execution is unchanged.

## Goal

Close the remaining correctness defects that can be repaired without changing the open
`v6-position-engine-v2` policy cohort:

1. A confirmed Solana notification survives provider lag and process restart until its exact
   transaction is decoded or proven to contain no relevant event.
2. One unresolved Solana signature degrades only its bound pool. It cannot advance that pool's
   frontier or poison unrelated pools.
3. Successful modeled fills are reflected in the historical decision outcome.
4. Runtime reporting separates feed health for current open-position pools from retained history.

## Durable Solana arrival contract

The isolated V6 ledger owns an append-only arrival identity `(pool, signature)`. The worker writes
the notification before the first `getTransaction` call. Each row retains slot, mint, immutable
binding, receive time, retry count, last error, and resolution state.

`pending` rows retry in `(slot, signature)` order. Provider unavailability never becomes a no-op and
never advances the pool frontier. A decoded transaction resolves the row only after normalized
events have been persisted. A healthy decoded transaction with no relevant rows resolves as
`no_relevant_event`. Retries are bounded in cadence, not count, and resume after restart.

Health and cursors are per pool. An unresolved row marks only that pool incomplete. Other Solana
pools and the EVM lane continue normally.

## Decision truth repair

`applied_outcome` is mutable application state, not immutable decision content. Schema migration
backfills `filled` for action decisions with an existing modeled fill. Future fills already update
the same field atomically.

## Feed reporting contract

The report retains all health rows for audit, but exposes two explicit groups:

- `active`: pools bound to open positions.
- `draining`: historical pools with no open position.

The compatibility `feed_health` map remains present so existing UI/API clients do not break.

## Deferred by policy safety

- Removing wallet-prior action authority changes the frozen policy hash. It must not run under the
  current open cohort.
- Event-prefix eviction and HOLD-decision compaction change reconstruction semantics. They require a
  separately versioned checkpoint contract; simple row deletion or synthetic events are rejected.

Those changes are not part of this live repair.

## Verification

RED-first tests cover provider lag, retry, restart, per-pool isolation, no-op resolution, immutable
arrival conflicts, migration backfill, and active/draining reporting. Then run the full V6 suite,
shared exit tests, schema validation, smoke checks, and bounded report/API latency probes before and
after pinning the immutable release.
