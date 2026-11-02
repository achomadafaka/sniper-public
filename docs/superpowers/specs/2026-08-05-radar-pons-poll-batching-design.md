# Radar Pons Poll Batching Design

## Problem

The Robinhood 4663 HTTP fallback receives dense Pons logs, but
`_evm_poll_range()` drains one event at a time. Each drain runs the full capture
pipeline and updates runtime status, while the persistence helpers commit their
own SQLite transactions. Live throughput is approximately the chain production
rate, leaving the Pons event clock permanently more than 30 minutes behind and
correctly grading Radar as `CAPTURE PARTIAL`.

**SUPERSEDED 2026-08-19.** The Pons five-minute event expectation no longer grades health.
Event age is activity data only; capture health is transport, finalized scan lag, typed failures,
and open gaps. See `2026-07-29-radar-pons-readiness-design.md`.

The expectation is still declared and still reported, so silence stays visible. It simply cannot
mark a lane unhealthy, because a quiet venue and a broken one are not the same thing and backfill
cannot clear an event-age bound.

## Design

Keep the existing bounded runtime queue and event order. Accumulate at most 256
polled events, further capped by the configured runtime queue capacity. Drain each
batch inside one outer SQLite transaction. Existing nested transaction helpers
therefore share one durable commit, and `OpportunityCaptureRuntime.drain()` writes
runtime status once per touched source per batch.

The final short batch is always drained. A queue capacity below 256 remains valid.
The poll range still retries after an unexpected failure; the enclosing batch
transaction rolls back that batch. Per-event decode failures retain the runtime's
existing typed gap behavior and commit with the rest of the batch.

Do not call `_source_heartbeat()` once per event. The existing `on_progress`
callback remains progress-coupled and one-second throttled, so a working dense
poll stays alive without one heartbeat write per log. A stalled poll still emits
no progress heartbeat.

## Verification

A dense-range regression must prove:

- every accepted event is submitted in native chain order;
- drains are bounded by both 256 and `queue_max`;
- the final partial batch is drained;
- persistence occurs under an active outer transaction;
- direct per-event heartbeats are absent;
- existing phase, timeout, zero-flow, recovery, and source-health tests remain green.

No schema, retention, strategy, paper, V6, or funded behavior changes.

## Restart Health Correction

Deployment exposed an adjacent restart bug. Runtime initialization treated every
persisted `blocker_code` as a durable source gap, although only `_GAP_BLOCKERS`
carry that meaning. A transient transport blocker such as
`BAGS_SUBSCRIPTIONS_EMPTY` could therefore survive restart in `gapped_sources` and
prevent healthy transport heartbeats from clearing it despite no open frontier gap.

On restart, restore status-derived gaps only for `_GAP_BLOCKERS`; continue restoring
every durable `op_source_frontiers.has_open_gap=1` row. Tests must prove a transient
Bags blocker clears on a healthy heartbeat while an integrity blocker remains gapped.
