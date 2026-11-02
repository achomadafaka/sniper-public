# Wallet Dossier Descriptor Surfaces v5

**Date:** 2026-05-27
**Status:** design locked; ready for /ship
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v4_thesis_lifecycle"` to `"v5_behavioral_descriptors"`
**Parent specs:** `2026-05-25-wallet-dossier-semantics-v0.md`, `2026-05-27-wallet-dossier-thesis-lifecycle-v4.md`
**Motivation doc:** the chat audit on 2026-05-27 — operator asked what the analyzer can describe without making assumptions

## Purpose

The dossier currently classifies *what* a wallet did (mechanism, grain, episodes). It does not yet describe *how* they did it: when in the day they traded, how fast, with what sizes, for how long.

These descriptors are pure derivations from data already in the fixture — no new fetches. They give the SPA-adapter and downstream copy/replay surfaces a way to distinguish two wallets that share `sports × binary_named × p_50_75 × held` but where one is a 5-second-decision bot and the other waits weeks per pick.

This is **not a thesis layer**. Descriptors are evidence about a wallet's operating style. They never imply edge.

## Tables

Five new per-wallet tables. All purely derived from `activity_rows` + `closed_positions`. No protocol-report dependency.

### 1. `descriptor_hour_distribution`

One row per hour bucket (0–23 UTC) per side per category. Counts BUYs and SELLs separately because a wallet may buy on a schedule and sell reactively (or vice versa).

| Column | Type | Rule |
| --- | --- | --- |
| `hour_utc` | INTEGER | 0–23. Derived from `activity_row.timestamp` mod 86400 / 3600. |
| `side` | TEXT | `BUY` or `SELL`. |
| `market_category` | TEXT | From `market_semantics.market_category` joined by conditionId, or `data_unavailable`. |
| `n_trades` | INTEGER | Count of activity_rows in this (hour, side, category) bucket. |
| `total_size_usdc` | REAL | Sum of `size * price` for kept rows. |

Honesty rule: if `len(activity_rows) < 50`, emit a `wallet_too_sparse_for_hour_distribution` gap and zero rows.

### 2. `descriptor_cadence`

One row per side per category. Summarizes time-between-trade gaps.

| Column | Type | Rule |
| --- | --- | --- |
| `side` | TEXT | `BUY` or `SELL`. |
| `market_category` | TEXT | category or `data_unavailable`. |
| `n_intervals` | INTEGER | trades − 1 in this bucket. |
| `median_seconds_between_trades` | INTEGER | Median of consecutive-trade time deltas. |
| `p10_seconds_between_trades` | INTEGER | 10th percentile (fastest 10%). |
| `p90_seconds_between_trades` | INTEGER | 90th percentile (slowest 10%). |
| `burst_share` | REAL | Fraction of intervals < 60 seconds. Distinguishes bot bursts from human cadence. |

Honesty rule: `n_intervals < 10` → row dropped + gap.

### 3. `descriptor_sizing`

One row per side per category. Position-size fingerprint.

| Column | Type | Rule |
| --- | --- | --- |
| `side` | TEXT | `BUY` or `SELL`. |
| `market_category` | TEXT | category. |
| `n_fills` | INTEGER | Activity rows in bucket. |
| `median_size_usdc` | REAL | Median fill notional (`size * price`). |
| `p10_size_usdc` | REAL | p10. |
| `p90_size_usdc` | REAL | p90. |
| `max_size_usdc` | REAL | Max single fill. |
| `coeff_of_variation` | REAL | stdev / mean. <0.1 → uniform sizing (algorithmic-looking); >1.0 → variable. |

Honesty rule: `n_fills < 10` → row dropped + gap.

### 4. `descriptor_time_in_position`

One row per `exit_mode` (`held`, `pre_resolution`, `still_open`, `unresolved`). How long positions live before exit.

| Column | Type | Rule |
| --- | --- | --- |
| `exit_mode` | TEXT | the position's exit_mode. |
| `n_positions` | INTEGER | count. |
| `median_seconds_in_position` | INTEGER | exit_ts − entry_ts. For `still_open` and `unresolved`: `fetched_at − entry_ts`. |
| `p10_seconds_in_position` | INTEGER | |
| `p90_seconds_in_position` | INTEGER | |

Honesty rule: `n_positions < 5` → row dropped + gap.

### 5. `descriptor_fills_per_position`

One row per market_category. Position-scale fragmentation fingerprint.

| Column | Type | Rule |
| --- | --- | --- |
| `market_category` | TEXT | category. |
| `n_positions` | INTEGER | positions in category. |
| `median_fills` | INTEGER | Median activity_row count per (conditionId, asset). |
| `p10_fills` | INTEGER | |
| `p90_fills` | INTEGER | |
| `single_fill_share` | REAL | Fraction of positions with exactly 1 fill. Distinguishes one-shot conviction from scaled entries. |

Honesty rule: `n_positions < 5` → row dropped + gap.

## dossier_meta

Add five counts:
- `n_descriptor_hour_rows`
- `n_descriptor_cadence_rows`
- `n_descriptor_sizing_rows`
- `n_descriptor_time_in_position_rows`
- `n_descriptor_fills_per_position_rows`

## Lineage

Each non-key cell in the 5 new tables gets a `data_lineage` row.

| Column source | Lineage |
| --- | --- |
| `hour_utc`, side, n_trades, total_size_usdc | `activity_rows[i].timestamp / side / size / price` |
| `median/p10/p90_seconds_between_trades`, `burst_share` | derived from `activity_rows[i].timestamp` ordering |
| `median/p10/p90/max_size_usdc`, `coeff_of_variation` | derived from `activity_rows[i].size * price` |
| `median/p10/p90_seconds_in_position` | derived from `positions.entry_ts / exit_ts / fetched_at` |
| `median/p10/p90_fills`, `single_fill_share` | derived from `activity_rows` grouped by `(conditionId, asset_id)` |

## Acceptance

1. `DOSSIER_SCHEMA_VERSION == "v5_behavioral_descriptors"`.
2. All 5 descriptor tables in `all_dossier_tables()`, sinks (SQLite + JSON), and lineage coverage.
3. Synthetic wallet with no activity emits zero descriptor rows + 5 sparse-data gaps.
4. Synthetic wallet with adequate activity emits one row per relevant (side, category) or (exit_mode) or (category) bucket.
5. Determinism: identical inputs produce byte-identical descriptor tables across runs.
6. dossier_meta carries all 5 new counts.
7. Real-data smoke on SwissMiss: descriptors populate and percentile fields make sense (median time-in-position for `held` ≥ days; for `pre_resolution` likely < days).

## Out of scope

- No edge claims, no thesis emission.
- No cross-wallet aggregation (that's the SPA-adapter, separate surface).
- No new fetches (no `market_metadata`, no `price_history`).
- Position-level descriptor breakouts (e.g. per-grain hour distribution) — deferred. v5 keeps things at wallet × category × side granularity.
