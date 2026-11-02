# Radar Adaptive v3 Default-Off Design

**Date:** 2026-08-11
**Status:** Approved
**Scope:** Opportunity Radar paper measurement only. Funded execution does not change.

## 1. Decision

The system keeps the adaptive v3 code, schema, result separation, and Pons quote fix.

The system disables all adaptive v3 behavior by default.

This design amends this candidate contract:

- `docs/superpowers/specs/2026-08-11-v6-adaptive-paper-cohort-v3-design.md`

That contract stays available as an inactive candidate design.

## 2. Current Risk

The merged v3 code has three unconditional paths:

1. The worker activates the v3 lifecycle at startup.
2. The engine evaluates the v3 entry rule after each exact quote completion.
3. The paper cycle drives the v3 lifecycle beside v2.

An unrelated Radar deployment can start the v3 clock.

The v3 arm also uses the shared exact-quote lane. It can reduce v2 exit-observation capacity.

An operator instruction to avoid v3 deployment is not a runtime control.

The live release was `03525fa2` during this review. It had no `radar-adaptive-v3` activation row.

## 3. Setting

Add this field to `MemecoinOpportunitySettings`:

```python
adaptive_v3_enabled: bool
```

Read it from this environment variable:

```text
MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED
```

The default value is `0`.

Accepted true values are `1`, `true`, `yes`, and `on`, without case sensitivity.

If v3 is enabled while the paper adapter is disabled, settings validation fails startup.

No deployment in this work sets the value to `1`.

## 4. Engine Gate

Add this constructor argument to `OpportunityEngine`:

```python
adaptive_v3_enabled: bool = False
```

When the value is false, the engine does not call `evaluate_adaptive_v3`.

The engine cannot create a `pump-adaptive-v3`, `bags-adaptive-v3`, or `pons-adaptive-v3` alert.

The engine cannot create a v3 paper fill request.

The Pons request-snapshot quote fix stays active in both setting states.

## 5. Worker Gates

When the setting is false, the worker does not call these functions:

```python
activate_adaptive_lifecycle(...)
drive_adaptive_lifecycle(...)
```

The v2 activation, paper adapter, v2 lifecycle, Pons probe, and Pons completion path stay active.

When the setting is true, all three v3 paths use one setting value from the same settings object.

## 6. Tests

The implementation requires these tests:

1. Default settings set `adaptive_v3_enabled` to false.
2. An explicit true environment value sets `adaptive_v3_enabled` to true.
3. v3 enablement without the paper adapter fails startup.
4. A default engine never calls `evaluate_adaptive_v3`.
5. A default worker start writes no `radar-adaptive-v3` activation row.
6. A default worker cycle does not call `drive_adaptive_lifecycle`.
7. A default v6 alert creates no `pump-adaptive-v3` fill request.
8. An enabled fixture can activate, evaluate, and drive v3.
9. The Pons request-snapshot tests pass in both setting states.

## 7. Deployment

1. Merge the default-off gate.
2. Deploy the pinned opportunity release.
3. Confirm that the worker uses the merged release.
4. Confirm that no `radar-adaptive-v3` activation row exists.
5. Confirm that no adaptive v3 alert or fill request exists.
6. Confirm that the v2 lifecycle and Pons completion path stay active.

Do not set `MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED=1` during this deployment.

## 8. Deferred Trajectory Work

This branch does not change v2 trajectory capture.

The live ledger had 76 `time_exit` trajectories during the follow-up review.

The median 5-second bucket coverage was 33 percent. No trajectory had all 12 buckets.

The current observations do not support an unbiased policy simulation.

A separate branch will measure the paper cycle and each quote stage for one day.

That work must measure request, queue, provider, completion, replacement, defer, and drop times.

The next trajectory design must use a complete grid that matches measured capacity.

The design must also require these controls:

- The completion test uses an observation age greater than or equal to `EXIT_AGE_MS`.
- Each skipped bucket has a typed row.
- Each trajectory stores its coverage.
- Trajectory quotes use the lowest queue priority.
- Each policy simulation reports coverage with its result.
- A partial-size exit uses a matching quote size or states the conservative approximation.

## 9. Non-Goals

- Do not change the v6 entry predicate.
- Do not change the v2 target, stop, cadence, or horizon.
- Do not capture post-outcome trajectories.
- Do not select a new stop.
- Do not activate a new paper arm.
- Do not change funded authority.
- Do not gate the Pons quote-binding fix.

## 10. Knowledge Sync

| question | answer |
|---|---|
| New doctrine? | Yes. Adaptive v3 is default off behind one setting. |
| Superseded doc? | No. This design makes the v3 candidate contract inactive by default. |
| Brain entry? | No. This gate is a paper-runtime safety correction. |
| `AGENTS.md` update? | Yes. Record the default-off v3 gate. |
