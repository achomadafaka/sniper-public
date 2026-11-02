# Pre-registration: what the +25% take-profit cap costs

Registered 2026-08-30 by Gamma, BEFORE any post-TP outcome was computed.
Requested by Alpha. Read-only study. No config, rail, or `.env` change is proposed by it.

## 0. Boundary held while writing this

Instrument coverage was measured before registration (does the tape exist, how long, what n).
No quantity involving a post-TP **return** was computed before this document was committed. The
motivating payoff statistics in §1 were reproduced from Alpha's own published figures, which
are already in circulation, and they do not depend on the estimand.

## 1. Motivation, reproduced and corrected

Alpha's figures reproduce exactly on all funded closes:

| chain | n | win | avg win | avg loss | breakeven | gap |
|---|---|---|---|---|---|---|
| solana | 652 | 52.8% | +$12.59 | -$15.69 | 55.5% | -2.7 pts |
| 4663 | 79 | 55.7% | +$12.81 | -$20.70 | 61.8% | -6.1 pts |

**Correction that matters for this study.** 28% of solana closes and 44% of 4663 closes are
operator-decided (`manual` / `mirror`), not rail outcomes. A cap study is about rails. Rail-only:

| chain | n | win | avg win | avg loss | breakeven | gap |
|---|---|---|---|---|---|---|
| solana | 469 | 55.9% | +$13.71 | -$18.79 | 57.8% | -1.9 pts |
| 4663 | 44 | 52.3% | +$18.35 | -$27.00 | 59.5% | -7.3 pts |

The deficit survives the exclusion on both chains, so the premise holds. The rail-only figures
are the reference for this study; the pooled ones are not.

## 2. Estimand

For a cap `c > 25`, on the RAIL-EXITED funded population, per chain, with every other rail
frozen at its cohort value:

    Delta(c) = E[ realized return | TP cap = c ] - E[ realized return | TP cap = 25 ]

Stated as the full rail interaction, NOT as a peak distribution. Raising the cap does not merely
move the exit level; it changes WHICH RAIL FIRES. A position that took +25% may, under c = 40,
instead trail out at +12% or reach the -35% stop. The counterfactual therefore requires the
post-exit PATH, not the post-exit peak.

## 3. The identification failure, stated as a proof

Every `real_tp` exit censors the peak at the same threshold. In the funded rail-exited
subpopulation there is **no observation whatsoever in the open interval (25%, infinity)** that
arose from a TP-exited position: the rail ends the position at the boundary by construction.

A survival estimator (Kaplan-Meier, Turnbull) placed on this sample is undefined above the
censoring point, because all censoring is at a single shared value and no event is observed
beyond it. Therefore `E[return | c]` for `c > 25` is **not identified** from funded outcomes
alone.

**Corollary, pre-registered as a refusal.** Any grid search over `tp_pct` fitted on this data
that reports an argmax is reporting the shape of its own extrapolation, not a property of the
market. This study will not produce such a number. See
`docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md`, where EV-argmax was
withdrawn for this exact reason: fit the OFFSET, not the level.

## 4. Where information above the cap can come from

Five candidate sources. (a) and (c) are the ones this study will use; the others are scoped.

- **(a) Post-close mark tape.** `memecoin_position_marks` is keyed `(chain_id, mint)`, not by
  position, so it keeps marking while any lane holds the coin. MEASURED: 145 of 257 funded TP
  closes (56%) carry at least one mark after `closed_at`; median post-close tape length 55.1
  min (p25 10.1, p75 710.5), median 242 marks. At least 60 min of tape on 72 (28%); at least
  240 min on 45 (18%).
- **(b) Paper arms as a held-longer control.** Same signals, different rails. Not primary:
  paper fills are modelled and a paper round trip pays two spreads (2026-08-28), so paper
  LEVELS are not comparable to funded levels. Offsets may be. Scoped as a robustness check only.
- **(c) Gap-through TP exits.** A coin that jumps past the cap between marks realizes ABOVE
  nominal, so the exit itself is an observation of peak >= realized > 25%. These are genuine
  uncensored draws from the region of interest and cost nothing to collect.
- **(d) Positions whose cohort ran `tp_pct = 0` or a different policy.** A natural experiment,
  but assignment is not random (an mcap target is why the cap was zeroed), so it is confounded.
  Scoped as descriptive only.
- **(e) Forward capture.** `MEMECOIN_POST_TP_FOLLOWUP_ENABLED` and
  `MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED` are both OFF. The continuation instrument was
  repaired on 2026-08-29 (frozen `token_decimals`; it had been hardcoded `10**6` and was wrong
  by 1000x on nine-decimal tokens) and is idle. Arming these is a RECOMMENDATION this study may
  make. It is not actioned here.

## 5. The selection threat that decides the study

The tape survives a funded close only while **some lane still holds the coin**. Holding is
outcome-dependent: a lane with a stop exits when the coin dumps. So a coin that collapsed after
our TP plausibly loses its tape sooner than one that kept running.

If so, post-close coverage is **missing not at random, biased toward continuation** — the exact
direction that makes "raise the cap" look good. This is the same shape as
`finding-a-shadow-graded-on-its-own-admission-window-2026-08-28`.

**Pre-registered gate, run BEFORE any Delta(c) is computed:**

- **G1** Spearman rho between post-close tape LENGTH and post-close maximum return.
- **G2** Covered vs uncovered TP closes compared on PRE-close observables only (entry mcap,
  entry liquidity, hold duration, realized return, wallet, chain, cohort).

**Pre-committed decision rule:**

| G1 / G2 result | consequence |
|---|---|
| No dependence detected at n | Delta(c) reported as an estimate WITH its interval, and the null result of the gate reported alongside it. |
| Dependence toward continuation | Delta(c) is an **UPPER bound only**. No point estimate is published. |
| Dependence away from continuation | Delta(c) is a **LOWER bound only**. |
| Coverage too thin to test (< 30 covered) | Instrument (a) is abandoned and the study proceeds on (c) alone. |

Absence of a detected dependence is NOT evidence of independence; the gate's power at the
realised n will be stated with the result.

## 6. The two known biases oppose, so the point estimate is not conservatively signed

- **Chart space understates.** Marks are chart, not route. 17 of 19 funded TPs on 2026-08-26
  realized ABOVE the maximum mid their own feed ever showed. In a chart replay a higher TP is
  reached LATER than it truly would be and the stop is reached EARLIER, so a chart replay
  **understates** Delta(c).
- **Selection overstates.** Per §5, if coverage favours continuation the sample **overstates**
  Delta(c).

These push in OPPOSITE directions. Consequently the naive estimate cannot be defended as
conservative in either direction, and only bounding BOTH magnitudes yields a usable interval.
This is registered here so the result cannot later be presented as "conservative because chart
space".

The 2026-08-26 exit-rail replay was ruled UNRANKABLE because ordering flipped between censor
settings, root-caused to the input series being the wrong quantity. This study therefore
pre-commits to reporting a **bound, never a ranking**, and to re-running under at least two
censor settings; if ordering flips, that instability is the finding and no ordering is published.

## 7. Multiplicity control

Grid: `c in {30, 35, 40, 50, 60, 80, 100}` percent, against the reference `c = 25`.

The reported statistic is `max_c Delta(c)` over ELIGIBLE cells (§8). Because the grid is
searched, the null distribution is built on the **MAX statistic** by permutation, not by
Bonferroni: the cells are strongly correlated (they share positions and paths) and Bonferroni
would be both wrong and needlessly conservative.

- **Permutation null:** shuffle the assignment of post-close paths to positions, breaking the
  coin-specific link while preserving both marginals; recompute the whole `Delta(c)` curve;
  retain `max_c Delta(c)`. 10,000 permutations. This tests whether continuation is
  coin-specific rather than a property of the pooled tape.
- **Interval:** block bootstrap over POSITIONS (not marks; marks within a position are
  dependent), 10,000 resamples, recomputing the full curve each time.

## 8. Size floor, enforced inside the search

A cap `c` is INELIGIBLE to be reported as the argmax, or to enter the max statistic, unless at
least **20 positions** carry post-close tape reaching the horizon required to evaluate `c`.
Enforced inside the search, not applied afterwards: a floor applied after an argmax is chosen
has already let the thinnest cell decide the answer.

Cells excluded by the floor are REPORTED with their n, never silently dropped.

## 9. `executable_peak_price` is MNAR and is not a population peak

Absent on 19% of closes and those rows are 95% losers
(`finding-executable-peak-price-missing-not-at-random-2026-08-27`). It will NOT be used as a
peak measurement on the population. Where used at all, it is confined to the subpopulation
where it is present, and the bound implied by imputing the missing 19% at both extremes is
reported with it.

## 10. Regime windows

The stop contract moved 50 -> 35 mid-day on 2026-08-25 and cohort cutoffs live in `.env`. A
window spanning a rail change mixes two systems.

Pre-registered: the primary window opens at the LAST rail change affecting TP or stop, and the
window is defined on **`closed_at`**, because the rail that fired is a property of the exit, not
of the entry. A position opened before and closed after a rail change is EXCLUDED from the
primary read and reported separately. Naming the timestamp field is mandatory here
(`finding-... 2026-08-29`: a cutoff that names no field silently mixed 108 rows).

## 11. Pre-registered outcomes, including the negative

One of these will be the deliverable. All three are acceptable results.

1. **Bounded positive.** "Raising the cap from 25% to c is worth between X and Y per position,
   n = N, and here is what remains unobserved."
2. **Bounded negative.** "Over the observable range the cap costs no more than Z, which is
   smaller than the -1.9 pt (solana) / -7.3 pt (4663) breakeven gap, so the cap is not the
   binding cause of the deficit."
3. **Unidentified.** "The retrospective instruments cannot separate continuation from selection
   at this n. The forward captures in §4(e) would identify it; here is the design."

Outcome 3 is a success, not a failure. It is registered in advance so it cannot later be
presented as a disappointment.

## 12. Kill criteria

The study is abandoned, and said so plainly, if any of these hold:

- Fewer than 30 TP closes carry post-close tape after the §10 window is applied.
- G1/G2 show dependence so strong that the upper bound is uninformative (bound exceeds the
  largest plausible Delta by an order of magnitude).
- Ordering flips between censor settings (§6), in which case the instability is reported and no
  cap recommendation is made.

## 13. Out of scope, explicitly

No new `tp_pct` will be recommended as a config value from this study alone. No rail, `.env`, or
runtime change. No funded RPC or Jupiter quota will be spent: Jupiter is the binding provider
(162:1 over RPC, no API key, IP-limited, shared with funded) and the 4663 endpoint is already
rate-limited. Stored tapes only.

---

# Amendment 1 — gate result, and a better instrument found during it

Appended 2026-08-30, after running the §5 gate and BEFORE computing any outcome on the new
instrument registered below.

## A1.1 The §5 gate FIRED. Instrument (a) is upper-bound-only, permanently.

**G1**: Spearman rho between post-close tape length and post-close maximum return is
**+0.284, t = +3.55, n = 145**. The gradient is monotone:

| post-close tape | n | median post-close max return |
|---|---|---|
| < 15 min | 45 | +43.9% |
| 15-60 min | 28 | +75.5% |
| > 60 min | 72 | +109.3% |

Longer tape means higher continuation, which is the predicted direction: the tape survives while
a lane holds the coin, and lanes hold coins that keep running.

Per the §5 pre-committed rule, `Delta(c)` from instrument (a) is an **UPPER BOUND ONLY** and no
point estimate will be published from it. This is now binding.

**The number this kills.** The raw post-close maximum over covered TP closes has median **+79.7%**
(p25 +11.4%, p75 +274.7%). Read naively that says every +25% TP leaves ~80% on the table. It is
an artifact of the selection above and must not be quoted as an opportunity cost.

**G2 was the weaker test and would have falsely reassured.** Covered and uncovered TP closes are
similar on every PRE-close observable: realized_pnl median 9.58 vs 10.00, entry_notional 40.00 vs
32.86, hold 1.5 vs 1.1 min. A pre-close balance check finds nothing because the selection acts
AFTER the close. Recorded so the next reader does not substitute G2 for G1.

## A1.2 Funded exit policy is FROZEN PER POSITION, not global

`MEMECOIN_REAL_TP_PCT` and `MEMECOIN_REAL_STOP_PCT` are both `0` in `.env`. The +25%/-35% comes
from each position's immutable funded contract (`funded_contract_json.policy`), read through
`frozen_policy_from_position`. `assigned_policy_id` is NULL on all 731 funded closes — that
column belongs to the paper arms and must not be used here.

Measured across 731 funded closes (672 carry a contract):

| policy | tp | stop | closes | TP exits |
|---|---|---|---|---|
| follow-30-50-v2 | 25 | 35 | 316 | 152 |
| follow-30-50-v1 | 25 | 35 | 107 | 49 |
| follow-30-50-v1 | 30 | 50 | 71 | 14 |
| follow-30-50-v1 | 30 | 0 | 54 | 9 |
| follow-30-50-v1 | 30 | 70 | 34 | 9 |
| follow-30-50-v1 | 25 | 50 | 18 | 7 |
| funded-{nano,micro,small,mid}-v1 | 0 | 60 | ~49 | 4 |
| (legacy, no contract) | - | - | 59 | 12 |

So "the cap is 25" is true of the dominant cohort and false of the population.

## A1.3 Source (d) is PROMOTED to primary: the cap itself varies across cohorts

§4(d) was scoped "descriptive only". That was wrong, and the table above is why. The cap is not
constant, so its variation is a natural experiment, and — unlike instrument (a) — the assignment
happens at ENTRY, from band and cutover date, and cannot be caused by the coin's later path.
It is not vulnerable to the G1 selection at all.

**Primary contrast, pre-registered before its outcome is computed.** The only pair that varies
the cap while HOLDING THE STOP FIXED:

    follow-30-50-v1, tp=25, stop=50   (n=18 closes)
      versus
    follow-30-50-v1, tp=30, stop=50   (n=71 closes)

Every other pair moves tp and stop together and is not an identified cap contrast. This is a
5-point difference in the cap, not the 25-vs-100 question, so it bounds a LOCAL slope only.

**Secondary, uncapped cohorts.** `funded-{nano,micro,small,mid}-v1` run `tp = 0` with trail and
derisk. These are UNCENSORED with respect to the cap and give the peak distribution the cap
truncates. n is small (~49 closes, 4 TP-labelled) and band is confounded with policy after the
band cutover.

**Registered warning against the obvious misreading**, from
`finding-memecoin-mcap-target-override-round-trips-2026-08-27`: uncapped positions peaked at
98%/97%/78% and then ROUND-TRIPPED into the stop, losing 3 of 3. A high peak in an uncapped
cohort is not a foregone gain. Only realized return counts, which is §2's point restated.

## A1.4 Confounds carried by the promoted instrument, and what is done about them

Cohort assignment is not random. tp=25/stop=50 and tp=30/stop=50 differ in WHEN they ran and
possibly in band. Pre-committed handling:

1. Report the calendar overlap of the two cohorts. If they do not overlap, the contrast is
   confounded with regime and is reported as such, not as a cap effect.
2. Report band composition of each cohort. If disjoint, same.
3. With n=18 on one arm, the §8 size floor of 20 is NOT met. Per §8 this cell is therefore
   INELIGIBLE to be an argmax and is reported with its n as an excluded cell. It may still be
   reported as a descriptive contrast, explicitly labelled underpowered.

Point 3 is registered in advance precisely because the temptation will be to relax the floor once
the number is seen.

## A1.5 Revised expected deliverable

Given A1.1 and A1.4, outcome 3 of §11 ("unidentified, and here is the forward capture") is now
the most likely result, with outcome 2 (bounded negative) possible from the uncapped cohorts.
Outcome 1 is unlikely to be reachable at this n. Recorded before the numbers are seen.
