# Memecoin Capture v2 - Design

**Date:** 2026-07-11
**Status:** Approved for implementation by operator directive
**Scope:** Forward-only raw feed, quote path, horizon, clock, and coverage truth

## Goal

Produce replayable point-in-time evidence without mixing chains, clock domains, capture regimes, late marks, or mutable wallet cohorts.

## Non-Claim

The upstream Fomo endpoint is a filtered product feed. Capture v2 stores every event returned by that endpoint; it is not an exchange-level or chain-level full tape.

## Versioned Tables

New v2 tables are additive. Legacy tables remain readable but are never silently pooled with v2.

### `memecoin_feed_observations_v2`

One row per upstream event observation:

- upstream event id and raw event type;
- normalized trade side when applicable;
- source user, handle, network, mint, ticker, price, market cap, and size;
- raw source timestamp, parsed source timestamp, corrected observation timestamp, and measured lag;
- `was_curated` and `was_real_approved` captured before any roster mutation;
- raw JSON payload, capture version, and clock domain.

Indexes support `(network_id, mint, source_ts)`, `(source_user_id, source_ts)`, and `(raw_type, source_ts)`. Pre-v2 cohort membership stays unknown.

### `memecoin_capture_cohorts_v2`

Tracks active `(network_id, mint)` capture episodes with context flags (`signal`, `candidate`, `control`, `position`), first/last observation, expiry, priority, next-due time, and skipped-sweep count. Contexts merge; duplicate sightings do not create duplicate price calls.

### `memecoin_quote_observations_v2`

One row per actual selected-pair observation, not one row per minute:

- network, mint, pair, DEX, exact observation time, source time;
- price, liquidity, market cap, volume;
- quality code and capture version;
- context bitset and sweep id.

The unique key is `(sweep_id, network_id, normalized_mint)`. Suspect observations are retained but excluded from decisions and research. No `INSERT OR IGNORE` hides separate sweeps.

### `memecoin_signal_outcomes_v2` and `memecoin_horizon_marks_v2`

The outcome stores the entry observation, capture version, network, reference price, target-horizon manifest, and terminal coverage status. Each horizon is a separate row with target seconds, observation id, actual age, lateness, price, and status `pending|observed|missed|abandoned`.

An observation is assignable only when it is at or after the target and no more than `max(90 seconds, 2 * configured capture period)` late. One observation may satisfy at most one horizon. Once the tolerance passes without a valid quote, the horizon becomes `missed`; it is never filled later. Outcome completion distinguishes complete from partial coverage.

## Capture Budget

Position and candidate/control cohorts have priority over general signal cohorts. Each sweep has an explicit maximum mint count. Due cohorts are ordered by priority, oldest next-due time, then stable `(network_id, mint)` order. Cohorts skipped because of budget increment `skipped_sweep_count`; the gap is visible in coverage reports. The system never reports 90% coverage by omitting budget-skipped cohorts from the denominator.

## Quality

Quality codes include `ok`, `wrong_chain`, `wrong_base`, `missing_pair`, `nonfinite`, `nonpositive`, `liquidity_missing`, `pair_changed`, and `ratio_outlier`. Pair changes are stored and require two consecutive observations before becoming the new canonical pair unless the prior pair disappears. A price-ratio outlier is stored but cannot fill a horizon or trigger an exit.

## Clock Contract

- `source_ts_raw`: exact upstream value.
- `source_ts`: parsed UTC when trustworthy.
- `observed_at`: `real_utc_now()` at receipt.
- `clock_domain`: `source_utc`, `observed_real_utc`, or `legacy_unknown`.

No v2 time is derived from SQLite `CURRENT_TIMESTAMP` or host wall time. The legacy normalization command becomes an idempotent classifier: provable rows receive an explicit normalized domain; ambiguous rows remain `legacy_unknown`. Research defaults to v2 and fails when capture versions are mixed without an explicit stratification flag.

## Replay and Coverage

Replay orders by `observed_at`, then source timestamp, then upstream id. This matches information availability and prevents future leakage. The capture manifest reports row counts, date span, networks, distinct coins, cohort coverage, horizon coverage, quality exclusions, budget gaps, and legacy exclusions.

## Tests

- All upstream raw types persist even when they are not normalized trades.
- Cohort flags remain point-in-time after the roster changes.
- Same mint on two chains remains separate.
- Separate sweeps persist; one sweep cannot duplicate a key.
- Budget ordering and gap denominator.
- Pair-change confirmation and outlier exclusion.
- Horizon on-time, late, reused-observation, missing, and abandonment cases.
- Replay order and mixed-version rejection.
- Legacy clock classification is idempotent and non-destructive.
