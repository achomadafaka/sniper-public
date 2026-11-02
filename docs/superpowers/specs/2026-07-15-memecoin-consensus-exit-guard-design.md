# Memecoin funded mirror-exit consensus guard

**Date:** 2026-07-15

**Status:** Approved for implementation by operator
**Scope:** Funded real-position mirror-on-sell decisions only

## Problem

`real_mirror_on_sell()` currently full-sells our funded position on any matching source-wallet sell.
It does not inspect whether the source still owns the token or whether another armed wallet still
holds the exact same `(network_id, mint)`.

The feed already preserves the facts needed to distinguish those cases in
`memecoin_feed_observations_v2.raw_json`. `body.currentSizeUsd` is the source wallet's remaining
position value after a `large_sell`; an explicit zero is a full exit. The normalized
`their_size_usd` field cannot be used: its `currentSizeUsd or inHumanAmount` fallback replaces a
valid zero with token quantity and therefore mixes remaining USD with sold token units.

Read-only production evidence at the design point:

- 27 funded positions had closed; 12 (44.4%) used `exit_reason='mirror'`.
- The 12 funded mirror exits realized `-$14.52` in aggregate.
- Three exact sell events reported a material source position still open: Index `$2,002.80`,
  BYCOCKET `$2,394.31`, and ANSEM `$280,669.59`. We nevertheless full-sold all three positions.
- Four additional exits had at least one other currently armed wallet with a fresh, exact-chain
  positive holding observation. Together with the partial-source cases, seven of 12 mirror exits
  would have been held; that selected subset realized `-$44.92`.
- The funded SOLdiers mirror exits realized `-$13.21` and `-$33.15`. Recorded marks later reached
  `$0.002905`, versus executable mirror fills of `$0.0007198` and `$0.0007329`.
- Global-feed sell coverage is incomplete: 426 of 1,391 classified large sells (30.6%) were missed.
  Stale holding observations therefore cannot be treated as current truth.

This evidence proves blind full-mirroring is an attribution bug. It does not prove that every old
buyer remains a holder.

## Decision

Before a funded mirror exit, evaluate two exact, local facts:

1. **Source remainder:** read the exact upstream sell observation by `fomo_event_id`. If it reports
   at least `$1.00` remaining after the sell, hold.
2. **Fresh armed holder:** find the latest trade observation, within the preceding 60 minutes, for
   each *other currently active armed wallet* on the exact `(network_id, mint)`. If at least one
   latest observation explicitly reports at least `$1.00` remaining, hold.

If neither fact exists, preserve the existing full mirror exit. Missing, malformed, stale, or
chain-ambiguous evidence never suppresses an exit.

The 60-minute window matches the existing consensus observation horizon and bounds the known
missed-sell problem. One other holder is sufficient: together with the source signal it establishes
two-wallet consensus before the source sale. The `$1.00` floor removes floating-point dust.

## Data flow

```text
exact SELL event
  -> locate funded position by (source_user_id, network_id, mint, real policy)
  -> exact-event source remainder lookup
  -> latest other-active-armed holdings for exact (network_id, mint), observed <= decision time
  -> source remainder >= $1 OR holder count >= 1
       yes: audit mirror hold; do not submit sell
       no:  audit mirror allowed; call existing venue exit executor
```

All lookups are indexed/local SQLite reads. No network request is added.

## Safety invariants

- Robinhood remains network `4663`; coin identity is `(network_id, mint)`.
- No EVM chain is inferred from address shape.
- An unresolved or mismatched network cannot borrow consensus from another chain.
- Only `real_mirror_on_sell()` changes. Paper arms and paper-strategy verdicts remain unchanged.
- Stop-loss, take-profit, mcap targets, de-risk, time-cap, unquotable, manual exit, kill switch, and
  stuck-bag handling remain unchanged and executable.
- The source sell does not create a new network request or static safety/balance cache.
- The existing one-position-per-coin, reservation, accounting, and duplicate-submit guards remain
  unchanged.
- The existing operator `mirror_disabled` override remains stronger and retains its current explicit
  log path.

## Audit contract

Every funded sell event that resolves to an open position and is not already suppressed by the
operator `mirror_disabled` override writes one `reason='mirror'` row through
`memecoin_exit_trigger_audit` before execution or evidence-based hold. `allowed=0` means held;
`allowed=1` means the existing executor was allowed. JSON detail records:

- exact upstream event ID;
- decision code: `source_still_holds`, `armed_consensus`, or `mirror_allowed`;
- source remaining USD, or `null` when unavailable;
- other holder count and handles;
- network ID, mint, 60-minute window, and `$1.00` floor.

A held result returns `status='mirror_held'` with the same decision fields. It is not a close and
must not trigger wallet demotion.

## Configuration

- `MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S=3600`
- `MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD=1`

Armed runtime validation requires both values to be finite and strictly positive.

## Tests

TDD acceptance cases:

1. Exact sell reports material source remainder -> no executor call; audited hold.
2. Exact sell reports explicit zero, one other fresh active armed holder remains -> no executor
   call; audited hold.
3. Other holding is stale, unarmed, paused, excluded, on another chain, superseded by a full sell,
   malformed, or dust -> it cannot suppress the mirror.
4. Exact sell is a full exit and no fresh armed holder exists -> existing full mirror path executes.
5. Missing v2 evidence fails open to the existing mirror behavior.
6. Paper mirror behavior is byte-identical.
7. Existing real exit rails and concurrency/double-submit tests remain green.

## Deployment gate

Merge and deploy only from a clean feature worktree after focused tests, the full memecoin suite,
schema validation, compileall, diff checks, exact-SHA runtime validation, zero pending reservations,
and a post-restart heartbeat/process check. No historical position or event rows are rewritten.
