# Radar Active Capture Venues Design

**Status:** Approved on 2026-08-07.

## Goal

Run capture only for venues that can produce useful research data. Preserve all historical evidence and decoder support.

The active set is:

- `pumpfun_solana`: `scoreable`;
- `bags_robinhood_4663`: `capture_only`;
- `pons_v1_4663`: `capture_only`; and
- `bags_solana`: `historical_only`.

This change does not add an arm, a threshold, a funded action, or V6 authority.
The 2026-08-07 venue-scoring design later adds separate paper arms for Bags and Pons.

## Venue authority

One module owns the venue authority map. Each known `VenueId` has exactly one mode.

`scoreable` permits raw capture, canonical capture, state updates, exact quote work, and existing venue-specific arms.

`capture_only` permits raw and canonical capture. It cannot create a Radar arm or inherit Pump rules.

`historical_only` permits decode, restore, report, and replay of stored evidence. It cannot start a source or create new evidence.

The worker, source health report, pricing readiness, and scoring gates use this map. They do not iterate all `VenueId` values as active.

## Bags Solana retirement

The worker does not start these Bags Solana parts:

- the Bags launch-feed coverage loop;
- the Bags pool WebSocket shards;
- the Bags Solana recovery lane;
- the Bags Solana confirmation path;
- the Bags Solana pricing row; and
- the Bags coverage and recovery health rows.

Legacy environment values cannot restore these parts. The worker ignores the old Bags Solana URL and coverage settings.

The code keeps `VenueId.BAGS_SOLANA`, its decoder, its schema values, and its historical projection support. Existing evidence stays immutable.

The API excludes old Bags Solana runtime rows from current source health. Historical launch and sample views retain their venue labels.

## Active capture contracts

### Pump.fun Solana

Pump remains the only scoreable venue. A source gap quarantines the affected launch state and starts a new capture epoch.

The source must have a fresh event clock, no open gap, current pricing, and an exact quote before it can score an outcome.

The bounded recovery rule remains unchanged. An incomplete recovery stays explicit and cannot enter scoreable evidence.

### Bags Robinhood 4663

The shared paced 4663 poll keeps Bags factory events.

Amendment 2026-08-07: `2026-08-07-radar-venue-scoring-design.md` adds a separate Bags paper arm.

Health uses poll progress against the chain head. Event silence does not fail a low-cadence venue.

The venue cannot use Pump thresholds. A later entry rule needs a separate design, evaluator version, and forward clock.

### Pons V1 Robinhood 4663

The shared paced 4663 poll keeps Pons V1 factory launches and registered Uniswap V3 pool swaps.

Health requires poll progress and the existing five-minute event expectation. Unknown factories and unknown pools remain excluded.

Amendment 2026-08-07: `2026-08-07-radar-venue-scoring-design.md` supplies the required V3 design.
Pons now has separate paper scoring and no funded authority.

## State and report behavior

Current health contains only sources that support the active set. A retired stored row cannot reduce or increase the health count.

Each venue source row exposes its authority mode. The operator can distinguish `scoreable` from `capture_only` without reading a specification.

The source summary must not call capture complete from process health alone. It keeps gap, event-age, and poll-progress facts separate.

## Error behavior

An unknown venue mode fails startup. A source cannot start when its venue is `historical_only`.

A gap affects only its venue. The worker keeps other active venues live.

A capture-only venue cannot create a score receipt. A policy error fails closed before receipt creation.

Historical Bags Solana rows remain readable even when old runtime status rows exist.

## Test contract

Focused tests must prove these behaviors:

1. The authority map covers every `VenueId` exactly once.
2. The active capture set contains Pump, Bags 4663, and Pons V1 only.
3. The scoreable set contains Pump only.
4. Legacy Bags Solana settings cannot start a source.
5. Worker task construction starts one Pump lane and one shared 4663 lane.
6. Worker task construction starts no Bags Solana task.
7. Current health excludes stored Bags Solana status rows.
8. Historical Bags Solana decode and projection tests still pass.
9. Capture-only venues cannot create Pump receipts.
10. Pump gap quarantine and Pons poll-progress tests still pass.

Use focused tests only. Do not run the full suite.

## Deployment contract

The release must descend from merged `main`. The opportunity symlink and the single worker process must use the exact release SHA.

After the cutover, current health must contain no Bags Solana source, pricing, coverage, or recovery row.

Pump must show a fresh event clock. Pons and Bags 4663 must show current poll progress. No active venue can have an open gap.

The deploy does not delete data or change funded services.

## Knowledge delta

Radar now separates active capture authority from historical venue identity. Bags Solana becomes historical-only.
