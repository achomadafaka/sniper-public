# V6 Post-Exit Follow-Up Design

**Status:** Approved 2026-07-27

## Goal

Measure whether V6's modeled exits avoided further loss or abandoned later recovery without adding
another engine, strategy arm, or post-exit decision authority.

## Contract

- V6 remains the only post-fill decision authority while a position is open.
- Every modeled action fill schedules exact sold-quantity quotes at `30`, `120`, `300`, `900`,
  `3600`, `10800`, and `43200` seconds after that fill.
- A follow-up never changes inventory, engine state, decisions, policy hash, source cursor, or funded
  state. It is measurement only.
- Schedules and results live only in the isolated V6 paper database. One immutable result exists per
  `(action_token, horizon_s)`; restarts may retry an unfinished schedule but cannot overwrite a
  result.
- Both Solana and chain 4663 use the existing read-only exact-quantity quote adapters. Missing,
  stale, and unavailable routes are stored as typed outcomes, never replaced with chart prices.
- Reports show target horizon, actual elapsed time, result status, hypothetical proceeds, and the
  delta from the actual modeled exit proceeds. A late sample is labeled by its actual elapsed time.
- Existing historical exits are not backfilled because a current quote cannot reconstruct a past
  executable route. Only fills committed after this schema lands receive schedules.

## Runtime

The existing async quote scheduler adds a low-priority follow-up lane. Funded execution remains
unreachable. Priority stays: active V6 action `100`, entry `50`, follow-up `20`, open-position
refresh `10`. At most one follow-up job is outstanding, preventing an old schedule from flooding the
provider. A crash before persistence leaves the schedule eligible; a committed result deduplicates
the retry.

## Evaluation

The operator surface attributes staged-break scores, including the `early` component, and reports
post-exit executable deltas. This can show that an early full exit later quoted higher or lower; it
does not claim the later quote was attainable at the original exit time and does not alter V6.

## Verification

RED-first tests cover schedule creation, exact sold quantity, immutable result deduplication,
restart retry, provider failures, runtime priority/isolation, both-chain reporting, and funded-source
scans. Focused V6 and funded-boundary suites must pass; no broad suite is required.
