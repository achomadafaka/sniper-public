# Memecoin Admission Unlock Design

**Date:** 2026-08-07
**Status:** Approved by the operator for immediate activation
**Supersedes:** The consensus admission gate and the nano/micro funded floor

## Goal

Restore funded entry volume without removing execution or loss controls.
Automatic copies will admit nano, micro, small, and mid coin bands.
The consensus count will remain a recorded feature, but it will not block an entry.

## Authority State

A wallet has one of three operator states.

- Armed: paper collection and funded entry authority are active for the selected chains.
- Watch-only: paper collection is active and funded entry authority is absent.
- Paused: paper collection and funded entry authority are both inactive.

An arm action clears `paused` in the same transaction.
A final disarm keeps `paused=0` and returns the wallet to watch-only.
A pause or exclusion clears `real_approved` and `real_chains` in the same transaction.
The database must not retain configured funded authority on a paused or excluded wallet.

## Admission Policy

`MEMECOIN_REAL_RESERVABLE_BANDS` defaults to `nano,micro,small,mid`.
`assumed_nano` follows the nano decision and is also admitted.
`out_of_scope` remains refused.

`MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP` defaults to `0`.
The runner does not refuse a solo buy because of the prior-buyer count.
The runner does not request the prior-buyer count when the gate is off.
The existing configuration seam permits a rollback without a code change.

## Preserved Controls

The change does not alter these controls:

- The per-chain arm check.
- The 60-second global-feed age limit.
- The 120-second follow-activity age limit.
- The trader-price chase limit.
- The six-hour first-seen chase limit.
- The `$2M` automatic band ceiling.
- The one-position and re-entry checks.
- The bankroll, exposure, gas, and daily-loss checks.
- The token-safety and executable sell-route checks.
- The EVM route-loss limit.
- The Solana quote-impact limit.

Current executable paper evidence does not support removal of these controls.
The active cohort shows negative average results for chase and out-of-scope refusals.
Safety controls also prevent entries that have no verified exit.

## Activation

The release must update both repository defaults and the live state environment.
The deployment must use an exact commit that is reachable from `origin/main`.
The deployment must restart the pinned memecoin runner and the pinned web service.

After deployment, a reconciliation will find paused rows with configured chains.
Each successful resume will use the normal arm API and its Fomo follow check.
No direct database update will bypass that check.

## Tests

Tests will prove these behaviors:

1. The default policy admits nano, assumed-nano, micro, small, and mid.
2. The default policy refuses out-of-scope entries.
3. A solo mid-band buy reaches the funded entry function.
4. An arm action clears pause atomically.
5. A pause clears all funded authority atomically.
6. An exclusion clears all funded authority atomically.
7. A final disarm leaves the wallet active for paper collection.

## Rollback

Set `MEMECOIN_REAL_RESERVABLE_BANDS=small,mid` to restore the funded floor.
Set `MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP=1000000` to restore the consensus gate.
Deploy the prior merged commit if code rollback is also required.
