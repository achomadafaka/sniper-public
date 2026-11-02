# Execution-premium threshold — results

Date: 2026-09-02
Author: Gamma
Pre-registration: `2026-09-02-exec-premium-threshold-preregistration.md` (`b6c56d1a`),
committed BEFORE the outcome data was read. The grid, bands, minimum n, metric and
decision rule below are those, unchanged.

## Population

Funded closed positions joined to their entry attempt: reference A (display) n=493 over
2026-07-18..2026-09-02, reference B (executed) n=537 over 2026-07-15..2026-09-02.

## Bands

| premium | A display n | A med ROI | A win | B executed n | B med ROI | B win |
|---|---|---|---|---|---|---|
| <1.15 | 197 | -0.6% | 50% | 342 | +3.8% | 56% |
| 1.15-1.50 | 212 | +5.1% | 56% | 158 | -1.2% | 49% |
| 1.50-2.00 | 63 | -2.7% | 48% | 25 | -33.4% | 36% |
| 2.00-3.00 | 11 | -39.7% | 45% | 7 | -39.3% | 43% |
| >=3.00 | 10 | -79.5% | 10% | 5 | -94.4% | 20% |

The last two rows are below the pre-registered n>=15 floor and are reported, not used.

## The grid

Bootstrap 95% CI on (median ROI below - median ROI above), 6000 samples.

Reference A (display) — the reference the gate actually uses:

| cut | n below | n above | med below | med above | CI | n>=30 |
|---|---|---|---|---|---|---|
| 1.15 | 197 | 296 | -0.6% | +1.2% | [-10.4, +8.1] | yes |
| 1.30 | 328 | 165 | +0.7% | -0.6% | [-12.7, +15.5] | yes |
| 1.50 | 409 | 84 | +1.7% | -21.1% | [-11.5, +38.3] | yes |
| **1.75** | 453 | 40 | +1.7% | -38.4% | **[+20.8, +53.2]** | **yes** |
| 1.95 | 472 | 21 | +1.2% | -52.3% | [+23.4, +78.3] | NO |
| 2.25 | 477 | 16 | +1.0% | -53.9% | [+21.7, +86.9] | NO |
| 2.50 | 480 | 13 | +1.2% | -59.9% | [+49.5, +96.7] | NO |

Reference B (executed):

| cut | n below | n above | med below | med above | CI | n>=30 |
|---|---|---|---|---|---|---|
| 1.15 | 342 | 195 | +3.8% | -3.8% | [-0.7, +19.5] | yes |
| **1.30** | 463 | 74 | +3.8% | -24.8% | **[+4.4, +44.7]** | **yes** |
| **1.50** | 500 | 37 | +2.8% | -37.3% | **[+2.4, +59.1]** | **yes** |
| 1.75 | 519 | 18 | +2.3% | -51.7% | [-0.8, +90.1] | NO |
| 1.95 | 524 | 13 | +2.0% | -52.3% | [-52.4, +97.5] | NO |
| 2.25 | 528 | 9 | +2.0% | -84.4% | [-49.1, +101.1] | NO |
| 2.50 | 529 | 8 | +2.0% | -89.4% | [-50.0, +101.0] | NO |

Separating cuts are CONTIGUOUS on both references — 1.75 upward on A, 1.30 and 1.50 on B.
A contiguous run is the signature of a cliff; an isolated separating cut among seven looks
would have been discounted as multiplicity.

## The pre-registered decision rule, applied

1. A cut on reference B separates with the CI excluding zero: YES, at 1.30 and 1.50.
2. n above that cut >= 30: YES, 74 and 37.
3. A neighbouring grid cut agrees: YES, 1.30 and 1.50 are adjacent and both separate.
4. Reference A agrees within one band: YES. A's only separating cut meeting n>=30 is 1.75,
   which sits inside the 1.50-2.00 band that B's 1.50 opens.

All four hold. **The rule says recommend a change.**

## The recommendation

On the gate's own reference, the evidence-supported cut is **1.75, not 1.95**. The current
threshold sits above where both references show deterioration.

Retrospective counterfactual on the realized book, stated as a counterfactual and not a
forecast:

| cap | positions blocked | their PnL | book without them | blocked win rate |
|---|---|---|---|---|
| 1.50 | 84 | -$310.91 | +$209.61 | 43% |
| **1.75** | **40** | **-$326.85** | **+$225.55** | **32%** |
| 1.95 (current) | 21 | -$289.13 | +$187.84 | 29% |
| 2.25 | 16 | -$256.80 | +$155.50 | 25% |

The book over this population is **-$101.30**. Blocking at 1.75 would have left **+$225.55**.

Not one trade: of the 40 blocked, 27 lost money, the median was -$10.10, and dropping the
three worst still leaves -$211.84.

## What this does NOT establish

1. **The counterfactual assumes nothing else changes.** Blocked capital is not reallocated
   in this arithmetic. Real refusals free a slot and may enter elsewhere.
2. **Exit rails produce part of the outcome.** Pre-registered as a confound; unresolved. A
   high-premium entry may exit worse because the premium is real, or because a rail treats
   it differently. This design cannot separate those.
3. **Reference A is partially truncated, as predicted.** Only 21 of 493 sit above 1.95, and
   they exist through fail-open, waiver, or a cap that differed earlier in the window. The
   region above the current threshold is not a clean sample on that reference.
4. **The 43% fail-open is untouched by any threshold.** On 4663, `display_price` is absent
   on 43% of attempts and the premium gate abstains there. Alpha's judgement that this
   should be fixed ahead of any threshold change is correct and this result does not
   displace it. A gate that abstains on nearly half the traffic cannot be tuned into
   correctness.

## Status

A recommendation to the operator. Not a deploy. Alpha has stated they will not change a
funded premium gate on an inferred threshold, and that is the right call.
