# Memecoin Source-Size Forward Shadow Preregistration

**Status:** SEALED
**Sealed at:** `2026-08-19T17:33:35.131664+00:00`
**Authority:** Measurement only. This protocol cannot change funded admission.

## Question

Does a source buy of at least `$500` predict a better `alpha_b` paper return than a smaller source buy?

The threshold came from retrospective data. All results before the cutoff are fit data.

## Fixed cohort

- Chain: `solana`.
- Paper cohort: `0e8fc8e02bac8bbe472d18597d18d63de09dd7af81455ed29e11c78baaf83cda`.
- Threshold: `$500`.
- Paper notional: `$100` per arm.
- Primary arm: `alpha_b`.
- Secondary arm: `combo_f`.
- Bootstrap iterations: `10,000`.
- Bootstrap seed: `20260819`.

The configured arms are `alpha_b`, `combo_f`, `control`, `momentum_a`, `peak_lock_e`, `real_shadow`, `scalp_c2`, and `trail_d`.

## Origin rules

The analysis unit is one exact `origin_event_id`. The source size comes from the immutable `memecoin_events` row.

An origin enters the cohort only when all configured arms have one valid row. Each row must use the frozen cohort policy.

Each row's cohort JSON must hash to the fixed cohort ID.

The source event must link to one entry snapshot and one immutable arm policy assignment. The position and policy-version JSON must match that assignment.

The report drops the complete origin when one arm is open, missing, duplicated, invalid, Fomo-filled, or censored.

The source event `created_at` controls the cutoff. A later arm open time cannot admit an earlier source event.

The report excludes an origin before the sealed cutoff. The report keeps Solana separate from all other chains.

The report assigns known sizes below `$500` to `below_500`. It assigns known sizes of `$500` or more to `at_least_500`.

The report excludes unknown sizes from the comparison. It reports unknown-size coverage separately.

A future funded gate must fail open on an unknown size. This protocol does not add that gate.

## Outcomes

The primary outcome is the `alpha_b` return percentage. `combo_f` is a secondary outcome.

Funded actual results are descriptive only. They cannot establish the paper prediction claim.

The report shows the origin count, mint count, UTC-day count, median return, mean return, and win rate.

The primary contrast is `at_least_500` minus `below_500`. The report clusters the bootstrap by mint.

Arithmetic sums do not grade the result. They are not portfolio returns.

## Stop rule

Each size group must contain at least 30 origins. Each group must also contain 20 mints and seven UTC days.

Before all floors pass, the report status is `COLLECTING`. The verdict stays empty.

After all floors pass:

- `ESTABLISHED`: the bootstrap lower bound is above zero, and the primary win-rate difference is positive.
- `REFUTED`: the bootstrap upper bound is zero or lower.
- `WEAK`: all other results.

The report cannot change the threshold, cutoff, cohort, arms, seed, or stop rule through command-line options.

## Operation

Run the report with a read-only database connection:

```bash
python3 scripts/research/memecoin_source_size_forward_shadow.py --db sniper_tracker.db
```

The script writes JSON to standard output. It does not write the database or change the runner.
