# Radar V6 Measured Fill Contract Design

**Date:** 2026-08-07
**Status:** Approved
**Supersedes:** `docs/superpowers/specs/2026-08-06-radar-owned-realistic-paper-lifecycle-design.md`
**Scope:** Radar paper measurement only. Funded execution and V6 policy do not change.

## Why the previous contract measured nothing

`radar-realistic-v1` sold the entry `floor_out_atomic` and scored the exit
`floor_out_atomic`. Both of those are `expected_out_atomic * 0.97` exactly. The check:

- 81 of 81 entry fills: `floor_out_atomic / expected_out_atomic = 0.970000`.
- 103 of 103 exit observations: the same ratio.
- Every one of those rows carries `slippage_bps = 300`.

So the v1 return is:

```
return = expected_route_return * 0.97 * 0.97 - 1
```

The `0.9409` is `slippageBps` applied twice. It is a number the caller sets. A result
that moves with a caller-set tolerance and not with the market is not a measurement.
Set `slippageBps` to 50 and the same 51 positions improve by about 5.7 points with
nothing about any token changed.

The damage was not cosmetic. The `-800` bps stop reads that same number, which starts
near `-950` bps before the token moves. 26 of 51 positions stopped on their first quote.
The engine could not have detected an edge if one existed.

The decomposition reproduces the observed medians exactly:

| Measurement | Median return |
|---|---|
| V5 expected route at selection | -3.83% |
| One floor | -6.72% |
| Two floors (v1 as built) | -9.52% |
| First v1 quote at five seconds | -9.62% |

`0.97 * 0.9617 - 1 = -6.70%` and `0.9409 * 0.9617 - 1 = -9.52%`. The arithmetic is the
whole effect.

## What a real fill costs

31 funded Solana chain entries at $37.50 to $50 carry a realized fill-against-quote
deviation in `memecoin_fills.execution_slippage_pct`:

- median `+0.058%`
- mean `-0.017%`
- p10 `-1.12%`, p90 `+1.22%`

The provider threshold is a signer bound. The measured deviation is the fill.

**Coverage limit, stated and not closed:** the exit leg is not measured. Every chain exit
row in `memecoin_fills` carries a null `execution_slippage_pct`. The entry distribution is
applied to both legs because it is the only measurement that exists.

## The local curve model is too cheap, and its ordering is wrong

Over 5,026 alerts that carry both `modeled_round_trip_cost_bps` and the live
`observed_pair_return_bps` from two Jupiter quotes taken at the same instant:

| launch_mode | fee | modeled cost | measured (median) | error |
|---|---|---|---|---|
| standard | 125 bps | -250 | -448 | -198 |
| cashback | 95 bps | -191 | -456 | -265 |
| mayhem | 125 bps | -250 | -1,816 | -1,566 |

`insights._launch_mode_cost` recorded cashback as the cheap tier because its fee is
59 bps lower. Measured, cashback is 8 bps **worse** to round trip and its model error is
34% larger. The fee discount does not reach the taker. That docstring is now marked
RETRACTED and carries this table.

The admission gate reads `immediate_round_trip_return_bps`, which on the v4 receipt path
carries `modeled_round_trip_cost_bps` (`engine.py`). The measured pair sits beside it in
the same feature dict and the gate ignored it.

## Fixed contract

- The rule is `pump-confirmation-truth-v6`.
- The matched control is `route-control-v2`, in v6's own observable band, sampled 1-in-5
  on `alert_id`.
- The lifecycle is `radar-realistic-v2`.
- The clip is `$25`.
- The entry quantity is the Jupiter buy **expected** amount.
- The exit quantity is that same expected amount.
- The score is the exit **expected** amount less `122` bps per leg.
- The target is `+1200` bps, the stop is `-800` bps, the horizon is 60 seconds.
- Radar requests an exact-input sell quote every five seconds.

## V6 predicate

`pump-confirmation-truth-v6` is `pump-confirmation-truth-v5` plus:

1. `launch_mode == "standard"`.
2. `observed_pair_return_bps >= -500`.

v5 keeps its history and stops firing. Raising v5's bar in place would redefine what its
accrued evidence meant.

Condition 2 is a **solvency floor, not a forecast**. The correlation between the
decision-time round trip and the 5-second route mark is `-0.03` on standard launches, so
the band predicts nothing. It refuses an entry that already sits below the stop. 300 bps
of room covers p90 adverse execution on both legs.

## Why the control is sampled

The control fires about 57 times a day against v6's 11. One open paper position already
takes most of the 2-second global Jupiter pace that `solana_probe` enforces. The sample
is deterministic on `alert_id`, which is a content fingerprint, so it is reproducible and
independent of the outcome. The sample rate is logged in this spec, not hidden.

## What this recovers, and what it does not

Counterfactual on the 51 v1 positions: v6 admits 20, rejects 31 on cashback and 2 below
the floor. Under `radar-realistic-v2` those 20 run `+0.18%` mean and `-5.89%` median,
with 8 of 20 truncated because v1's own stop closed the observation series early.

That is a repaired measurement. It is **not** evidence of an edge. The mean is near zero,
the median is negative, and a third of the sample is truncated.

## Funding gate

Unchanged and pre-registered. Funding stays a no-go until forward `radar-realistic-v2`
results show a positive return **and** a positive matched-control advantage at 25, 50,
and 100 pairs. Retrospective recomputation of a past cohort does not count.

The gate and its immutable checkpoint receipts use valid `op_paper_outcomes` rows only.
The gate joins each outcome to its `op_paper_fill_requests.alert_id` value.
The stored alert supplies the decision-time features for the matched comparison.
The legacy `op_insight_alerts.terminal_return_bps` value cannot supply a gate return.
The gate excludes a missing, censored, or duplicate lifecycle outcome.

## Rejected

- **Widen the stop.** It hides the loss.
- **Tighten the round-trip band for edge.** The band does not predict the outcome
  (r=-0.03). Spending sample on it buys nothing.
- **Mutate v5 in place.** It would redefine what v5's accrued evidence meant.
- **Change `immediate_round_trip_return_bps` to the measured pair.** That field feeds
  every arm at once, so one edit would silently redefine v2, v3, v4, v5, ignition,
  creator-prior, and both controls. v6 reads the measured pair in its own predicate.
