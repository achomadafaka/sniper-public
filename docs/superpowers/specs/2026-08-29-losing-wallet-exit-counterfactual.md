# Would a different exit policy have saved the losing wallets?

Read on 2026-08-29 for the profile study. **Answer: no, and not because the search was
underpowered — because the route never reached the levels those knobs act on.** The losses are a
selection problem, not an exit problem.

Route-space evidence only, per the standing constraint. `memecoin_route_series` and nothing else.
Chart marks are the retired instrument and are not used anywhere below.

## 1. Coverage, stated first, and the refusal

`memecoin_route_series` holds 10,386 rows over 247 positions from 2026-08-26T13:08:38. That bounds
this read to closes inside that window.

Losing cohort wallets — every wallet whose funded honest closes net negative — hold **274 closes**.
**25 of them carry at least 3 route observations. 9%.** Per wallet, only five clear a 3-close floor:

| wallet | funded closes | net PnL | covered |
|---|---|---|---|
| CryptoTalkMan | 10 | -71.03 | 7 |
| MrChillGuy | 5 | -56.63 | 4 |
| randomuser123 | 4 | -36.14 | 3 |
| smol_intern | 15 | -32.57 | 3 |
| y1111 | 6 | -28.66 | 3 |

**The other 42 losing wallets are refused, not estimated.** jester (9 closes, -$155.44), sambaxbt
(12, -$80.80), fibs, remusofmars, ovoimballin and the rest carry zero route observations. Nothing
below speaks to them, and no chart substitute was used to make them speak.

## 2. The policy-family search is not worth running, and here is the number

A per-wallet search over a family of K exit policies, at 80% power, two-sided, Bonferroni-corrected
for the family, on the observed spread of realised return across covered closes (sd 0.36):

| n | K = 10 | K = 50 |
|---|---|---|
| 3 | +76% of notional per close | +86% |
| 5 | +59% | +66% |
| 8 | +46% | +52% |

The best-covered wallet has n=7. **To resolve anything, a policy would have to improve realised
return by roughly half the notional on every close.** The entire observed range across these wallets
is -83% to +45%. No exit knob delivers +46% per close, so any winner such a search returned would be
the best of a family fitted to 7 points.

**This is a finding, not a failed measurement.** The search is not run.

## 3. What the route series does answer, without a search

One statistic, one threshold, no family: **did the position ever reach, in route space, the level
the exit knobs act on?** The funded take-profit is +25%, fixed long before this read.

Of the **17 losing closes** with route coverage:

| route peak reached before the loss | count | share |
|---|---|---|
| **>= +25%** (the funded TP) | **0** | **0%** |
| >= +40% | 0 | 0% |
| >= +15% | 4 | 24% |
| never exceeded +5% | **9** | **53%** |

**Not one losing position ever offered the funded take-profit in route space.** Over half never rose
5% above entry at all. The knobs the question asks about — TP, derisk trigger, trail arm — are all
thresholds above entry, and a threshold cannot fire on a price that never arrives.

Per wallet, the same result holds everywhere it can be checked:

| wallet | covered | losses | never +5% | reached +25% |
|---|---|---|---|---|
| CryptoTalkMan | 7 | 4 | 3 | 0 |
| MrChillGuy | 4 | 2 | 1 | 0 |
| randomuser123 | 3 | 3 | 2 | 0 |
| smol_intern | 3 | 2 | 1 | 0 |
| y1111 | 3 | 2 | 1 | 0 |

## 4. The one policy that would have changed anything, and why it still loses

A take-profit at **+15%** would have caught the 4 losses that reached that level. +15% is the floor
of the admissible clamp registered in the fitting addendum — the first 5% step clear of the
6.1-point round-trip cost.

It is still the wrong trade, and the fitting addendum already contains the reason. Expected realised
return over the censored-aware cohort peak distribution peaks at **+25% (EV +0.096)** and falls
monotonically below it. Catching 4 of 17 losses costs exiting every winner at +15%, and that trade
was evaluated and rejected on the full cohort before this read existed.

## 5. Verdict for the re-arm question

**A fitted exit profile does not justify re-arming these wallets.** The losses are not exits taken
too late on positions that ran; they are positions that never ran. Fitting TP, derisk or trail
per-wallet cannot address a coin that never rose 5%.

This feeds the profile study as a negative: `profile_matched_v1` should not be expected to rescue
the negative-PnL wallets, and if it appears to, the mechanism needs checking before it is believed.

## 6. Honest limits on this read

1. **The descriptive statistic in section 3 was specified after I had seen four individual
   positions**, in the per-wallet dump that produced section 1's coverage table. It is not
   pre-registered in the strict sense. What limits the damage is that its threshold is the funded
   TP of +25%, fixed long before this exercise and not chosen from these data — and that the result
   is 0 of 17, not a marginal count that a threshold choice could move.
2. n=17 losing closes across 5 wallets. The direction is unambiguous at 0 of 17, but the SHARE that
   never exceeded +5% (53%) carries a wide interval on this sample and should not be quoted as a
   rate.
3. The window is 3 days. A wallet whose behaviour differs outside 2026-08-26 to 08-29 is not
   described here.
4. Route coverage is not random. Positions get series when the collector is running and the
   position is open long enough to be sampled, so very fast closes are under-represented. That
   biases toward slower positions, which if anything favours finding an exit problem — and none was
   found.
