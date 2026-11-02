# Memecoin Band Policy Forward Cohort Preregistration

**Date:** 2026-08-19  
**Status:** SEALED before activation and before the kill switch clears

## Question

Does the nano/micro band policy remain acceptable on a new funded cohort?

The policy is `funded-nano-v2` or `funded-micro-v2`. It uses a 20% half de-risk, a 30% post-de-risk trail, and a 60% stop.

This study does not test the entry policy. It does not grant funded authority to a wallet.

## Frozen Cohort

The deployment writes `MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT`. This timestamp starts the cohort.

The funded cohort includes automatic REAL positions that meet all these conditions:

- `execution_mode='real'`;
- the frozen contract policy is `funded-nano-v2` or `funded-micro-v2`;
- the reservation and position occur at or after the cutoff;
- the origin event is present;
- the position has no manual entry.

The safety ledger retains manual exits, route failures, unknown submissions, and no-quote outcomes. The primary return report labels manual exits separately.

The paper comparison uses Solana only. Robinhood 4663 stays separate until it meets its own gate.

Each paper unit is one `origin_event_id`. Each funded unit is the earliest funded position for one `(network_id, origin_event_id)`.

The paper comparison uses all eight configured arms. It applies `measurable_outcome_sql()` and drops the whole origin after any missing, duplicate, open, invalid, or censored arm.

The comparison excludes `fomo`, manual, and operator-modified rows. It does not pool policy cohorts or chains.

## Read Gates

The primary read occurs after all these floors are met:

- 30 closed automatic Solana funded origins;
- 20 distinct funded mints;
- 7 distinct UTC entry dates;
- 30 clean paired Solana paper origins after the cutoff;
- 20 distinct paired paper mints.

Only counts, coverage, and safety faults are visible before this gate. Do not inspect return or arm-difference values.

If the primary result is `WEAK`, the final read occurs at 50 funded origins, 30 mints, and 14 UTC entry dates. There is no read between the two gates.

If the final floors do not occur within 45 days, report `NOT ESTIMABLE`. Do not change the gate after the cutoff.

## Metrics

The funded return is `100 * realized_pnl / entry_notional`. The report includes these metrics:

1. Total realized PnL.
2. Median return and win rate.
3. Total PnL after removal of the two best mints.
4. Result counts by funded exit reason.
5. Stop-class loss as a fraction of gross loss.
6. Manual-exit, no-quote, route-failure, and unknown-submission counts.

The paired paper result uses `combo_f - alpha_b` for each origin. It includes these two primary metrics:

1. The median paired return difference.
2. The paired win-rate difference.

Use a mint-cluster bootstrap with 10,000 samples and seed `20260819`. Apply Holm correction at `alpha=0.05` across the two primary metrics.

Do not use the arithmetic sum of percentage returns. Report concentration by mint and wallet.

## Decision Rules

Trip the kill switch immediately after any of these safety faults:

- a new nano/micro contract does not contain the exact frozen policy;
- a small, mid, or `out_of_scope` position receives a nano/micro band policy;
- an admitted band causes a policy resolver exception;
- one origin creates duplicate funded positions or broadcasts;
- contract reconciliation fails.

These faults stop new entries. They do not change an open position's frozen policy.

At the primary gate, report `REFUTED` and stop new band-policy entries after either condition:

- funded total PnL, drop-top-two-mint PnL, and median return are all negative;
- the Holm-adjusted upper 95% bound is at or below zero for either primary paper metric.

Report `ESTABLISHED` only when all three funded metrics are positive and both adjusted lower paper bounds exceed zero.

Report `WEAK` for every other result. Continue unchanged to the final gate.

At the final gate, stop new band-policy entries unless the result is `ESTABLISHED`. Use the kill switch first, then prepare an atomic policy rollback.

No reader or worker can clear the kill switch, change `.env`, or change a funded contract.

## Disclosure

The historical Solana read used 613 complete eight-arm events. It was not an independent or prospective sample.

The historical read showed a positive `combo_f` median and win rate against `alpha_b`. It did not show dominance on mean or sum.

Nano and micro formed 129 of the last 200 Solana positions. This 64.5% value is historical coverage, not a future allocation.

The entry-authority audit and kill-switch decision remain separate from this exit-policy study.
