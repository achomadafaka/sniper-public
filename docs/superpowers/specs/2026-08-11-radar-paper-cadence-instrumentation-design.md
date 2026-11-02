# Radar Paper Sampler Completion Design

**Date:** 2026-08-11
**Status:** Approved direction
**Scope:** Radar v2 paper observations and bounded runtime measurement only

<!-- schema:external=op_runtime_status -->

## 1. Decision

Radar will bind each asynchronous paper quote completion to its requested bucket.

Radar will measure quote round-trip time and concurrent buckets for each position.

The change will not add an arm, a provider call, or a trajectory after an outcome.

The change will not alter the v2 policy constants or the adaptive v3 gate.

## 2. Verified Defect

The worker runs the paper lifecycle every 100 ms. The loop period does not cause the missing buckets.

The lifecycle selects one current 5-second bucket. It stops polling the old bucket after the clock advances.

Each probe stores the old completion under the old `observation_id`. The lifecycle does not consume that result later.

This defect loses middle buckets when a quote completes after its bucket boundary.

The probes do not key paper work by the entry request alone. Both probes use this key now:

```text
("paper", observation_id)
```

The `observation_id` includes the entry request, lifecycle version, and requested bucket.

Therefore, bucket N does not block bucket N+1 by identity. Queue capacity can still defer a new bucket.

## 3. Completion Contract

Each probe will add a bounded paper-completion queue.

Each completion will contain these fields:

```text
observation_id
venue_id
status
economics
failure_reason
requested_at
completed_at
```

The `status` value is `filled` or `rejected`.

The completion queue will copy the result. The current result cache will remain for compatibility.

The worker will keep bounded request context by `observation_id`.

The context will contain the entry request ID, venue, requested bucket, and immutable quote input.

The worker will drain paper completions before it drives the v2 lifecycle.

The worker will write each completion to its requested bucket. It will not use the current bucket.

The drain will never submit or retry a quote.

If the observation already exists, the drain will treat the completion as consumed.

If context is absent, the worker will count an orphan completion. It will not infer a bucket.

## 4. Outcome Boundary

The current v2 decision path will stay unchanged.

A completion for the current bucket can produce the same outcome that the current poll produces now.

A late completion for an old bucket will add an observation only. It will not create a retroactive outcome.

This rule prevents a policy change inside the current `radar-realistic-v2` cohort.

The worker will continue to stop new quotes after an outcome.

The drain can store a completion that the probe requested before the outcome.

## 5. Concurrency Contract

The existing probe key is correct and will remain unchanged.

Tests will prove that two bucket-specific `observation_id` values can remain in flight for one entry.

The Solana paper queue will keep its current bound of four.

The EVM shared queue will keep its current bound of 16.

This change will not raise a queue bound or change a queue priority.

## 6. Runtime Measurement

The worker will write one bounded status row under this source key:

```text
opportunity:paper_sampler
```

The detail version will be:

```text
paper-sampler-v1
```

The worker will store a fixed 24-hour window. It will restore an unfinished window after a restart.

The status will update at most once each minute.

The measurement will contain these values for all venues and for each venue:

- The request count.
- The completion count.
- The orphan completion count.
- The context eviction count.
- The current and maximum in-flight bucket count.
- The current and maximum in-flight bucket count for one position.
- The quote round-trip time histogram.
- The queue wait and provider time histograms when the probe supplies both times.
- The completion status count.
- The first and latest probe metric snapshots.

The fixed round-trip time bounds are:

```text
1000, 2000, 5000, 10000, 15000, 30000, 60000, overflow
```

The active request-context map will hold at most 1,024 items.

The status will show the sample count beside each percentile.

The measurement will not use the worker loop period as a cadence proxy.

## 7. Failure Rules

A telemetry error cannot change a quote, observation, or outcome.

A full completion queue will remove its oldest completion and increment a drop count.

A full context map will remove its oldest context and increment an eviction count.

The worker will not attach an orphan completion to the current bucket.

A rejected quote will write `unavailable` to its requested bucket when context exists.

## 8. Tests

The implementation requires these tests:

1. A late completion writes the requested 5-second bucket after the clock reaches 10 seconds.
2. A late 5-second completion does not create a retroactive stop or target.
3. A current-bucket completion keeps the existing v2 outcome behavior.
4. Solana accepts two bucket-specific IDs for one entry while the first request stays pending.
5. EVM accepts two bucket-specific IDs for one entry while the first request stays pending.
6. Each probe completion keeps its exact `observation_id`.
7. The completion drain adds no provider call or queue item.
8. A duplicate completion does not add a second observation.
9. An orphan completion does not create an observation.
10. A rejected completion writes an unavailable observation to the requested bucket.
11. The round-trip histogram uses the request and completion times.
12. The metric records the maximum concurrent bucket count for one position.
13. The request context never exceeds 1,024 items.
14. The worker restores an unfinished 24-hour status window.
15. The worker preserves a completed status window.
16. v2 target, stop, cadence, horizon, and result tests stay unchanged.

## 9. Excluded Work

This change will not capture quotes after an outcome.

This change will not add skipped-bucket rows or a trajectory coverage score.

This change will not simulate a stop, target, trail, tranche, or horizon.

This change will not enable adaptive v3.

This change will not change Pons entry binding or funded execution.

## 10. Release Gate

This change needs a separate merge and a pinned Radar release.

Before the cutover, verify that the adaptive v3 setting stays false.

After the cutover, verify these facts:

1. One worker runs from the new release.
2. Adaptive v3 activation, alert, and request counts stay zero.
3. V2 outcomes continue to increase.
4. Pons status stays healthy.
5. `opportunity:paper_sampler` uses `paper-sampler-v1`.
6. The status reports quote round-trip samples and concurrent buckets.
