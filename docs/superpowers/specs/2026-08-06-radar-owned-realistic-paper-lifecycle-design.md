# Radar-Owned Realistic Paper Lifecycle Design

**Date:** 2026-08-06  
**Status:** SUPERSEDED 2026-08-07 by
`docs/superpowers/specs/2026-08-07-radar-v6-measured-fill-contract-design.md`. The
"executable quote floor" this design scores is `expected_out_atomic * 0.97` on both legs,
which is `slippageBps = 300` applied twice and not a fill. Its results are a config
constant, and its -800 bps stop read a number that started near -950.  
**Scope:** Radar paper measurement only. Funded execution and V6 policy do not change.

## Goal

Radar measures its V5 rule with executable quote floors on both trade legs.

The lifecycle is:

`V5 signal -> $25 buy floor -> exact-quantity sell floors -> +12% / -8% / 60s result`

V6 does not read, own, reject, close, or report a Radar paper fill.

## Fixed Contract

- The rule is `pump-confirmation-truth-v5`.
- The clip is `$25`.
- The entry quantity is the Jupiter buy `otherAmountThreshold`.
- The target is `+1200 bps` of the entry quote amount.
- The stop is `-800 bps` of the entry quote amount.
- The final exit time is 60 seconds after the entry receipt.
- Radar requests an exact-input sell quote each five seconds.
- Radar scores the sell `otherAmountThreshold`.
- Radar keeps a 30-second route-recovery period after the final exit time.
- If no final quote exists by 90 seconds, Radar records a censored result.

A quote floor is not a chain fill. It is a conservative executable quote measurement.

## Forward-Only Activation

Radar writes one immutable activation row before it accepts lifecycle entries.

Only entry receipts at or after that activation time are eligible.

The old entry receipts have no matching exit quote history. Radar must not create results for them.

## Immutable Data

Radar adds three retention-excluded tables:

- `op_paper_lifecycle_activations` stores the forward cohort start.
- `op_paper_exit_observations` stores each exact sell quote or typed quote failure.
- `op_paper_outcomes` stores one valid or censored terminal result.

The worker derives the next due quote from the entry receipt and stored observations.

It does not create missed historical quote rows after a worker pause.

## Result Rules

Before 60 seconds, the first quote at or above `+1200 bps` records `target_first`.

Before 60 seconds, the first quote at or below `-800 bps` records `stop_first`.

At or after 60 seconds, the first executable quote records `time_exit`.

A quote after 60 seconds cannot change the result to a target or stop result.

Provider failures stay as observations. They do not become zero returns.

## Runtime and UI

The opportunity worker owns the lifecycle and uses the existing paced Jupiter lane.

The worker publishes `opportunity:paper_lifecycle` status.

The Radar API publishes only the current lifecycle version and activation cohort.

The simple results panel shows:

- valid results;
- average net return;
- target-first results;
- stop-first results;
- 60-second exits.

The panel does not use the older modeled V5 outcome summary.

## Source State

One stale source remains a per-source fact. It does not set the full Radar status to partial.

The full Radar becomes stale only when all capture sources are stale or unhealthy.

## V6 Isolation

The V6 worker removes the Radar database cursor and all Radar receipt calls.

The V6 settings remove the `MEMECOIN_V6_RADAR_*` options.

Historical V6 Radar rows stay immutable. The disabled V6 process stays disabled.

## Safety

- Radar remains paper only.
- Radar does not sign or send a transaction.
- Radar does not write the funded database.
- The funded runner does not restart during this deployment.
- The activation does not backfill old results.

