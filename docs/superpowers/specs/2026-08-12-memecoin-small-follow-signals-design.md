# Small Follow Signal Admission Design

**Date:** 2026-08-12

**Status:** Operator approved for design.

## Goal

The funded follow lane must process source buys of at least `$100`.

The non-curated discovery lane must keep its `$499` minimum.

The funded market-cap maximum must stay at `$1,000,000`.

## Current Problem

`MEMECOIN_DISCOVERY_MIN_USD` controls two different paths.

The discovery path uses it for non-curated wallet candidates.

The follow path also uses it for armed-wallet `/swaps` responses.

The current `$499` value removes smaller armed-wallet buys before the funded hook.

## Design

Add `MEMECOIN_FOLLOW_ACTIVITY_MIN_USD` to `sniper/settings/memecoin.py`.

Its default value must use `MEMECOIN_DISCOVERY_MIN_USD`.

This default keeps current behavior when the new setting is absent.

Set `MEMECOIN_FOLLOW_ACTIVITY_MIN_USD=100` in the operator environment.

`follow_activity_pass()` must use the new setting when `min_usd` is not supplied.

The explicit `min_usd` test and caller interface must stay unchanged.

The new value must apply to follow buys and follow sell coverage.

The discovery path must continue to use `MEMECOIN_DISCOVERY_MIN_USD=499`.

The global feed path must not change.

## Funded Controls

The funded hook can receive the new `$100` to `$498.99` follow signals.

The wallet must still have authority on the exact chain.

The `$1,000,000` market-cap maximum must stay unchanged.

The token-safety, sell-route, quote, stale, and first-seen chase controls must stay unchanged.

The funded clip must still use bankroll sizing and its `$25` minimum.

The position and exposure limits must stay unchanged.

## Runtime

The settings module reads the environment at process start.

The pinned funded runner must restart through the deployment script.

The deployment must skip a new backup because the operator requested no backup.

The deployment must stop if a funded reservation exists.

The active release, process path, heartbeat, status API, kill switch, and reservations require verification.

## Tests

One test must prove that the default follow minimum uses the new setting.

One test must prove that a `$100` follow buy reaches the funded hook.

One test must prove that a buy below `$100` does not reach the funded hook.

Existing tests must prove that an explicit `min_usd` value still overrides the setting.

Focused follow-activity and funded market-cap tests must pass.

## Non-Goals

This change does not increase or remove the market-cap maximum.

This change does not lower the funded clip minimum.

This change does not remove a safety, route, stale, or chase control.

This change does not change wallet authority or the armed roster.

This change does not add a database table or column.
