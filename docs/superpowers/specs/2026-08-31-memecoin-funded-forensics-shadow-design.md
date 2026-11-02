# Funded Memecoin Forensics and Entry Shadows

**Date:** 2026-08-31  
**Status:** approved for a research-only build  
**Scope:** funded positions with `opened_recorded_at >= 2026-08-23T00:00:00+00:00`

## Objective

Give the operator one exact funded-history report.

The report must show each trader, coin, position, realized result, and valid executable-route chart.

The system must also record two entry shadows.

The shadows must not change a reservation, a safety result, a quote, a transaction, a policy, or a funded position.

## Evidence That Starts This Work

The local snapshot at `2026-08-31T15:31:24Z` has 485 closed funded positions, 324 coins, and 46 recorded sources.

The automatic cohort has 235 positions from 36 source traders.

Manual entries have 250 positions and a `-$248.30` result.

Automatic entries have a `+$388.96` result.

Manual entries bypass normal selection gates.

The report must always show manual and automatic results in separate groups.

Automatic `small` bands lost in both chronological groups.

The 2026-08-23 through 2026-08-27 group has 10 `small` Solana positions with `-16.13%` return.

The 2026-08-28 through 2026-08-31 group has 10 `small` Solana positions with `-11.62%` return.

`mid` has six automatic positions only.

No active rule may use `mid` from this evidence.

The trader-prior rule had 16 chain-scoped rows with five earlier resolved automatic positions and non-positive return.

Those rows had `-11.64%` return.

This result is an exploratory shadow only.

`DegenCapitalLLC` has one automatic `small` position, `Quake`, with `-39.62%` return.

One position cannot support a Degen-specific rule.

The report must state this limit.

Source buy size has 233 values in the 235-position automatic cohort.

The fixed chronological source-size test uses `2026-08-23` through `2026-08-27` as the discovery group.

It uses `2026-08-28` onward as the holdout group.

The discovery group has a `+4.19%` return at source size at or above `$250`.

The holdout group has a `+1.29%` return at source size at or above `$250`.

The discovery result falls to `+2.00%` at or above `$500`.

The holdout result rises to `+5.76%` at or above `$500`.

This direction conflict does not support a source-size floor.

Only two wallet-chain groups have six discovery observations and three holdout observations for source size.

No wallet source-size minimum is robust in that test.

Effective market cap has 212 values in the automatic cohort.

The `$50,000` boundary maps to the existing `nano` and `micro` band boundary.

The market-cap-at-or-above-`$50,000` cohort lost in both chronological groups.

It includes `micro` entries that returned `+7.38%` in the holdout group.

A raw `$50,000` market-cap block would remove those `micro` entries.

The existing `small` band shadow is the precise `$250,000` through below-`$1,000,000` market-cap test.

No raw market-cap rule follows from this evidence.

## Truth Boundaries

`memecoin_route_series` is the only source for an executable-route chart.

The report uses only rows where `seam='exit_decision'`.

One chart needs at least two finite positive `unit_price` observations.

One chart needs a finite positive position `entry_price`.

One chart uses only route observations where `observed_at > opened_recorded_at`.

The report excludes a chart when `opened_recorded_at` is absent.

The report must label every other position `no_exact_route_series`.

The report must never read `memecoin_position_marks` for a chart.

The report must never call a price API, a chart API, a CLOB API, or a chain API.

Each chart shows our full-position route quotes after our entry.

The report must not call this a continuous price chart.

The report must not call this the source trader's fill chart.

`closed_recorded_at` is the only close clock for a historical trader prior.

`decision_started_at` is the only decision clock for that prior.

The prior query must exclude rows with absent `closed_recorded_at`.

The prior query must exclude `signal_source='manual'`.

The prior query must use the same `source_user_id` and `network_id`.

The prior query accepts only `execution_mode='real'` and `is_open=0`.

The prior query accepts only finite `realized_pnl` and finite positive `entry_notional`.

The prior query excludes the current `position_id`.

## Report

Add `scripts/render_memecoin_funded_forensics.py`.

The script uses `get_readonly_connection()`.

The script requires `--since` and `--output`.

The preregistered `--since` value is `2026-08-23T00:00:00+00:00`.

The report labels another `--since` value `exploratory`.

The script writes one self-contained HTML file.

The file contains these sections:

1. Cohort bounds, position count, coin count, source count, and exact-chart coverage.
2. Separate all-funded, automatic, and manual outcome summaries.
3. Automatic trader dossiers with count, distinct coin count, return, PnL, result counts, and coin links.
4. Coin dossiers with each position as a separate route chart.
5. Automatic source-size and effective-market-cap distributions, plus the fixed chronological comparison.
6. The two shadow results for each automatic position.
7. Data gaps and excluded rows.

The script groups a coin by `(network_id, mint)`.

The script must not combine route points from different positions.

The report reads source size from `memecoin_positions.their_size_usd`.

The report reads market cap from the frozen attempt `classification_json.facts.effective_mcap`.

The report must show coverage before it shows a source-size or market-cap result.

The report must not select a wallet threshold from outcome data.

The shadow summary covers automatic positions only.

The shadow summary lists every defined state with source and coin counts.

The trader result counts separate positive, negative, zero, and invalid outcomes.

Each trader coin link targets its `(network_id, mint)` coin dossier.

The source-size distribution uses fixed bins below $250, $250 through below $500, $500 through below $1,000, and $1,000 or more.

The effective-market-cap distribution uses fixed bins below $50,000, $50,000 through below $250,000, $250,000 through below $1,000,000, and $1,000,000 or more.

The script uses the Python standard library only.

The script emits inline SVG for each valid route chart.

The chart x-axis is `observed_at` minus the position entry time.

The chart y-axis is `100 * (unit_price / entry_price - 1)`.

The chart title includes the position ID, source handle, exit reason, and realized return.

The script uses `opened_recorded_at` when it exists.

The script uses `opened_at` only for report display when `opened_recorded_at` is absent.

## Entry Shadows

Add `sniper/services/memecoin_entry_shadow.py`.

The module returns a versioned JSON-safe value for one completed entry trace.

The module must not change a funded decision.

The module returns these two shadows.

### `small_band_v1`

For an automatic entry, this shadow returns `would_block` when the frozen classification has `coin_band='small'`.

For another automatic band, this shadow returns `would_allow`.

For a manual entry, this shadow returns `not_applicable`.

For an absent classification, this shadow returns `unknown`.

The payload includes the frozen `coin_band`.

### `prior_5_negative_v1`

For an automatic entry with a valid source, network, and decision clock, read earlier closed automatic funded positions.

The lookup is chain-scoped by `(source_user_id, network_id)`.

The lookup accepts only `closed_recorded_at <= decision_started_at`.

The lookup requires five earlier positions.

The shadow returns `would_block` when the prior realized ROI is at or below zero.

The shadow returns `would_allow` when the prior realized ROI is above zero.

The shadow returns `insufficient_history` below five earlier positions.

The shadow returns `not_applicable` for a manual entry.

The shadow returns `unknown` for an absent source, network, decision clock, or query failure.

The payload includes `prior_closed_n`, `prior_realized_pnl`, `prior_entry_notional`, `prior_roi_pct`, and `as_of_decision_at`.

## Trace Integration

`classification_json` is part of the funded contract hash.

The build must not modify it.

The runner already writes observation-only data to `EntryAttemptTrace.mark_substage()`.

The build records one `funded_entry_shadow_v1` substage event before `trace.finish()`.

The event contains the versioned output from both shadows.

The evaluator reads the stored attempt classification and the supplied resolved identity.

The evaluator runs after the entry result is known and before trace finalization.

The evaluator uses `decision_started_at` in its historical query.

The evaluator never reads the current position as prior evidence.

The evaluator catches every error.

The evaluator failure records `unknown` where possible.

The evaluator failure must not change the entry result or stop trace finalization.

The runner must not branch on a shadow result.

The runner must not add a quote, a network request, a signing step, or a transaction step for a shadow.

## Evaluation Contract

The first read is descriptive only.

The report uses fixed chronological groups: 2026-08-23 through 2026-08-27, then 2026-08-28 onward.

The report defines realized return as `SUM(realized_pnl) / SUM(entry_notional)`.

The report ranks a coin group by descending realized PnL, then `(network_id, mint)` ascending.

The report removes the first two ranked coin groups for the best-two-removed result.

The report groups outcome uncertainty by `(network_id, mint)`.

The report shows total PnL after removal of the two best coin groups.

The report shows the source count and coin count for every shadow state.

The report never combine chains.

The report never use a manual position as automatic-trader evidence.

No shadow result grants authority, changes clip size, or changes an exit.

No future funded rule follows from this work without a separate preregistration, forward cohort, and human approval.

## Tests

Add focused tests for these conditions:

1. `small_band_v1` returns each defined state.
2. `prior_5_negative_v1` excludes manual rows, paper rows, open rows, non-finite values, future closes, another network, and the current entry.
3. `prior_5_negative_v1` returns `insufficient_history` below five earlier closes.
4. A finished trace contains one immutable `funded_entry_shadow_v1` event.
5. A shadow evaluator error leaves the funded result unchanged.
6. The report excludes `memecoin_position_marks`.
7. The report labels a one-point route series `no_exact_route_series`.
8. The report keeps two positions for one coin as two separate charts.
9. The report separates manual from automatic summaries.
10. The report labels absent source size and effective market cap as unavailable.
11. The report shows automatic shadow states with source and coin counts.
12. The trader dossier shows result counts and working coin links.
13. The report shows fixed source-size and effective-market-cap distributions.
14. A source guard prevents a read from `memecoin_position_marks`.

## Non-Goals

This work does not change `MEMECOIN_REAL_RESERVABLE_BANDS`.

This work does not block `small`, `mid`, DegenCapitalLLC, or another trader.

This work does not set a source-size floor or a raw market-cap rule.

This work does not add a model, a score, a new data provider, or a live API call.

This work does not change exit rails, sizing, wallet approval, safety checks, reservations, or submission.

This work does not start a service or deploy a release.
