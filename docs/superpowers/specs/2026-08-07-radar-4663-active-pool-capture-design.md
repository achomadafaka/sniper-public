# Radar 4663 Active-Pool Capture Design

**Date:** 2026-08-07

**Status:** approved emergency repair

**Scope:** Radar research capture only

## Problem

The shared Robinhood poll keeps every Pons pool in memory until the worker stops.
Retention removes completed launch rows, but the poll still requests swaps for their pools.

The live worker recorded 101 recent launches while only three launch rows remained.
It processed dense expired-pool swaps at about `84%` CPU.

The Robinhood scan reached `6,232` blocks behind a `600`-block limit.
The synchronous persistence batches also blocked the Pump task.
Pump then opened an `enumeration_bound` gap with `RECOVERY_INCOMPLETE`.

## Design

The poll will request stored Pons pools only when `op_launches` retains their token.
The factory request remains unchanged and finds every new Pons V1 launch.

The poll will add each new pool to the same-range swap request.
Thus, the first swaps remain complete for a new launch.

The poll will keep the full identity map in memory.
The active request filter will not delete historical identity facts.

The poll will yield to the shared event loop after each bounded persistence batch.
The existing batch size, transaction, event order, and queue limits remain unchanged.

## Failure Behavior

If the active-pool query fails, the poll fails and retries the same block range.
It must not use the unfiltered identity map after a query failure.

An absent launch row excludes that pool from later swap requests.
Retention already keeps launch rows beyond the 300-second candidate capture window.

The existing scan-lag limit remains `600` blocks.
The existing Pump gap logic remains fail closed.

## Recovery

Deployment restarts only the pinned opportunity worker.
The existing Pump recovery path must close or quarantine the open gap.

The live gate requires one Pump worker and one shared Robinhood poll.
All active sources must show fresh heartbeats and zero open gaps.

The Robinhood frontier must reduce its lag across two samples.
It must reach at most `600` blocks before the repair is complete.

## Verification

Focused tests must prove these behaviors:

1. The request includes a retained Pons pool.
2. The request excludes a pool without a retained launch row.
3. A new factory pool receives a same-range swap request.
4. Two persistence batches permit another event-loop task to run.
5. Native event order and transaction batches remain unchanged.
6. Pump recovery and source-health tests remain green.

No schema, scoring rule, paper policy, V6, or funded behavior changes.
