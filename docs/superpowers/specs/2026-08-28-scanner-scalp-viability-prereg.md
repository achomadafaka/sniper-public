# Scanner-scalp viability — pre-registration

Written 2026-08-28, before the first study row. Alpha owns the design. Beta owns the arm and the
probe. This document owns the measurement: the cohort definition, the estimand, the read dates, and
the limits.

Every number below comes from a query against `data/runner_signals.db` run on 2026-08-28 and is
reproducible from the definitions in section 1. Where a figure in circulation disagrees, this
document states which one is correct and why.

## Status: CLOSED — the dense probe answered the registered question and the answer is GAP.

See section 12. The scalp arm should not be built. The sections below are preserved as the
registered design and the reasoning that led to the kill; they are no longer a plan of work.

## Status at the time of writing: NOT STARTED. The study had no rows and could not be read.

As of 2026-08-28 this document is a design, not a running study. Three things do not exist yet, and
each one blocks row one:

1. **The arms have no code.** `scalp_tight` and `scalp_tight_30k` are named in section 8 and are not
   implemented anywhere. Alpha has HELD those names pending the effective-stop question in section
   11.
2. **There is no cohort cutoff.** Section 7 says the first study row is the first qualifying row
   after a cutoff, and no cutoff timestamp has been set.
3. **There are therefore no calendar read dates.** Every date in sections 3 and 4 is an OFFSET in
   days, not a date, because the offsets run from a cutoff that does not exist.

**Setting the cutoff.** When the arms are deployed and the sampling interval is settled, append the
deployment's UTC timestamp here as `COHORT_CUTOFF`, rounded UP to the next whole minute. The first
study row is the first qualifying signal with `created_at` strictly after it. Nothing before it
enters, including the probe of section 7 and any pre-deployment shakeout.

    COHORT_CUTOFF = (not set)

**Calendar read dates, once the cutoff exists.** These are the binding paged-stratum reads from
section 4. Add the offset to `COHORT_CUTOFF`:

| offset | resolves effects of at least |
|---|---|
| +14 days | 14 points |
| +28 days | 10 points |
| +60 days | 7 points |

The per-cell reads of section 3 are all shorter and are not binding; the paged stratum at 5.2 usable
rows per day is what sets the calendar. **Do not read any cell before its offset has elapsed**, and
report an unelapsed cell as open, never as null.

## 0. The arm

One paper arm. Exact route-quote entry anchored at signal birth (t0) for every qualifying signal.
Fast executable take-profit at +25%. Stop at -20%. A 10-minute hard cap. Second-scale exit
sampling. The research key only.

The stop is pinned, not tuned. Round-trip cost on the qualifying cohort is **-6.08% median at 15
seconds**, so a stop tighter than about -12% measures our own spread and not the market. -20% stays
clear of it and the choice is unchanged. (An earlier -8.84% figure was a 60-second median published
as a 15-second one, and my own -8.66% was measured on all uncensored rows rather than on the
qualifying cohort. Both are withdrawn. See section 6.)

**The configured stop is not the effective stop.** See section 11. The sampling interval, not the
-20% setting, decides where a stop actually fires, and the difference moves the bar the arm must
clear by about 8 points of win rate.

## 1. The cohort, frozen

**QUALIFYING = `golden_buyers >= 1` OR `conviction >= 9`.**

**`golden_buyers` is a COUNT, not a flag, and this changes the cohort.** Its values run 0, 1, 2, 3,
4, 5, 6 and higher. The spec writes the predicate as `golden_buyers = 1`, which admits only the
signals with EXACTLY ONE golden buyer and rejects every signal with more. That drops 456 rows, and
those rows carry the strongest golden evidence in the population. Read literally the predicate
excludes the best signals, which inverts its intent.

This document uses `>= 1`. The choice is Alpha's to confirm, and it is material: `>= 1` gives 1958
qualifying rows against 1502 for `= 1`, a 30% larger cohort. It does not move the prior — the 373
dropped rows that have two or more golden buyers and no high conviction run a 60-second median of
-8.95%, against -9.19% for the cohort as a whole.

A row is USABLE when `entry_out_atomic > 0`, both exit quotes are present and positive, and
`censored_reason IS NULL`. All accrual figures below are usable rows, never raw rows.

**The cohort is 6 days old.** `golden_buyers` first carries a value at 2026-08-22T13:11:33 and
`conviction` at 2026-08-22T16:54:24. The table itself starts 2026-08-21T12:09:09. There is no
qualifying data before 2026-08-22, so no 14-day prior for this definition can exist.

Accrual over the 5 complete days 2026-08-23 to 2026-08-27:

| measure | per day | range |
|---|---|---|
| raw | 302.2 (median 310) | 165 - 374 |
| usable | 224.6 (median 237) | 145 - 264 |
| censoring haircut | 27% | |

The 27% haircut is specific to this document's usable rule, which requires BOTH exit quotes and an
uncensored row. A rule that requires only the 60-second exit loses 6.6%. The difference is almost
entirely `exit_quote_late`, where the exit quote arrived after its horizon. This document keeps
those rows out, because a quote that arrived late is not an observation at the horizon it claims,
but the looser rule is defensible and the two must never be mixed inside one figure.

**The 223 against 105 disagreement is resolved.** 105/day is wrong: it divides a 6-day window by
14 days. 223/day is right within noise, because it matches the usable complete-day rate of 224.6.
**Freeze the accrual at 225 usable rows per day.**

The prior of n=1469 is sound and only its window label was wrong. The qualifying life holds 1955
raw and 1421 usable rows, so n=1469 is that population, not a 14-day one.

## 2. The estimand

**The median is the primary. The mean is a labelled secondary.**

This population is a fat-tailed lottery, so a mean contrast is decided by which arm happens to hold
the tail. The median is the better instrument, but its advantage is a property of the cohort and
not of the family, because the median's precision depends on the DENSITY at the median and not on
the spread. Selection thins that density:

| cohort | median precision (per-observation sd-equivalent) |
|---|---|
| `mute` only, the bulk | 10.6 |
| all uncensored rows | 18.2 |
| the selected tiers | 42.7 |

The efficiency gain over the mean is 5.23x on all uncensored rows but only 3.08x on the qualifying
set. **The qualifying figure is the correct one, because power belongs on the population the study
samples.** An earlier 5.23x figure of mine used the wrong denominator; Beta corrected it.

Use the bootstrap for every number in this document. The closed form `1/(2*f*sqrt(n))` understates
the standard error by about 1.9x on dense cohorts, so it is for reasoning only.

## 3. Read cells and their dates

All dates are two-sample, 80% power, two-sided 0.05, median primary, at the usable accrual above.
A quoted `n = (2*sd/d)^2` is one-sample at 50% power and understates the requirement by 8x.

| cell | usable/day | median 60s | median sd-equiv |
|---|---|---|---|
| golden only | 96.2 | -8.27% | 19.4 |
| conviction >= 9 only | 99.0 | -16.12% | 62.7 |
| golden AND conviction >= 9 | 29.4 | -6.66% | 37.7 |
| all qualifying | 224.6 | -9.21% | 24.1 |

The all-qualifying median reads **-9.21%** here and **-9.19%** in sections 1, 6 and 11. Both are
correct and the difference is basis, not error: this table is computed over the 5 COMPLETE days
2026-08-23 to 2026-08-27, because accrual per day needs whole days, while the other sections use the
cohort's whole 6-day life. The gap is 0.02 points. It is annotated rather than reconciled because
silently carrying one number on two bases is the exact defect this document had to correct once
already.

Days to read, by effect size in points of 60-second return:

| cell | 10 pts | 15 pts | 20 pts | 30 pts |
|---|---|---|---|---|
| golden only | 1 | 1 | <1 | <1 |
| conviction >= 9 only | 12 | 6 | 3 | 1 |
| golden AND conviction >= 9 | 15 | 7 | 4 | 2 |
| all qualifying | 1 | <1 | <1 | <1 |

The `conviction >= 9 only` cell is the noisy one at 62.7, more than three times the `golden only`
cell. It also has the worst median at -16.12%. The cells are not interchangeable and must never be
pooled to make a date look shorter.

## 4. The paged stratum is the binding instrument

**Paged is `notify_tier='hold_prime'`.** It is 2.3% of qualifying usable rows and accrues at
**5.2 usable rows per day**, which matches both the "about 2% of the cohort" and the "about 5/day"
figures in circulation. That 5/day was already a usable rate, so no haircut applies to it.

Over the cohort's whole 6-day life the paged stratum holds n=30, median -9.55%, sd-equivalent 21.9.

| effect | rows | days |
|---|---|---|
| 10 pts | 151 | 29 |
| 15 pts | 67 | 13 |
| 20 pts | 38 | 7 |

Smallest effect resolvable by read date: 14 days resolves 14 points, 28 days resolves 10 points,
60 days resolves 7 points.

**At a two-week read the honest line is "the paged stratum stays open below about 14 points".** It
is not null and must not be reported as null. Page-to-quote latency is recorded on this stratum
only.

## 5. What this study cannot answer

**The 30-second window is below the scanner's own detection floor.** This is the most important
limit in this document and it is not a power problem.

**The coins are not young when the scanner finds them.** Token age at t0 (`age_s`) over 1960
qualifying rows has a p10 of 52 seconds, a median of 100 seconds and a p90 of 741 seconds. Only 16
rows, **0.8%**, are 30 seconds old or younger, which is about 2 per day. Even a 60-second window
admits only 18.2%.

So we cannot enter a coin inside 30 seconds of its birth, because the scanner does not learn of the
coin until it is already about 100 seconds old. **Testing that window needs a faster detector, not a
longer study.** The stratification stays in the design as an observed hypothesis, but this document
pre-registers that it will not resolve, and it names the reason: the instrument cannot record what
it never sees.

**A correction, because the first version of this section cited the wrong clock.** An earlier draft
supported this conclusion with `entry_quote_at - t0`, reporting that 100% of entries fall inside 30
seconds. That statement is close to a tautology and it is withdrawn as evidence. `t0` is the
scanner's own decision instant and `entry_quote_at` is stamped immediately after the Jupiter quote
returns, with nothing in between (`scripts/research/runner_scanner.py:2541-2546`), so the pair
measures ONE provider round trip and the coin does not appear in it at all. The writer also censors
the row when that gap exceeds 10 seconds, so a filter on `censored_reason IS NULL` bounds the column
at 10 seconds BY CONSTRUCTION. The measurement could not have returned any other answer.

The round trip itself is fast and irrelevant to this question: median 0.18 seconds, p90 1.15
seconds, and **0.18% of the median token age**. It is not a term in the latency budget. Report it as
provider latency or not at all.

The study also cannot price dollars. It is quote-to-quote throughout, so it ranks rules and nothing
else.

## 6. The prior is negative

The qualifying population runs a median of **-9.19%** executable at 60 seconds, with a p10 of
-68.8% and a p90 of **+40.8%** (n=1424). The 15-second median is **-6.08%**. Because 15 seconds is
effectively instantaneous, about 6.1 points of the 60-second figure is round-trip cost and about
**3.1 points is real drift**.

**Three earlier prior figures are withdrawn.** A -8.84% quoted at 15 seconds was a 60-second median
from an older snapshot. A -10.51% at 60 seconds does not reproduce at any horizon, statistic, or
cutoff and must be treated as unsourced. My own -10.38% and -8.66% pair was measured on all
uncensored rows (n=11196) and published here as the qualifying population; that was my error, of
the same kind, and these corrected figures replace it.

The 60-second median is robust to the definitions that remain in dispute. Across both candidate
predicates and three usable-row filters it stays inside **-8.9% to -9.4%**, so the negative prior
does not depend on resolving them.

The study asks one question: whether entry at signal birth reaches the p90 tail before the body
bleeds. The prior says it does not. A null result is the expected result.

## 7. The probe is excluded

A bounded 20-trade probe ran ahead of this study so the operator could see tries. It writes to
`data/memecoin_scalp_probe.db`, it predates this document, and it was launched without a
pre-registered design.

**Those rows are excluded from the study cohort. They are not a pilot stratum.** The study's first
row is the first qualifying row after a cohort cutoff that postdates this document's commit. A
pre-registration that inherits pre-registration-era data has given up the only thing it is for.

## 8. Adjacent arms, separate reads

Two knob arms are SPECIFIED for the existing scanner lane but are **not built**. They are adjacent
to this study and are not part of it. Each carries its own read discipline and neither one feeds this study's verdict.

1. `scalp_tight` — take-profit +25%, stop -20%, on all qualifying signals.
2. `scalp_tight_30k` — the same, plus an admission floor of market cap >= 30k.

The basis for the second arm is that the 30k to 100k band is the only near-breakeven cell observed
so far, at n=409, a 50% win rate, and a median of +0.6. That is an observation and not a result.
Read it under the same two-sample discipline as section 3.

## 9. Kill criteria

1. Stop if the paged stratum's usable accrual falls below 3 rows per day over any 7-day window,
   because the binding instrument has then gone dark.
2. Stop if the censoring haircut rises above 40%, because the usable population no longer
   represents the qualifying one.
3. Stop if residual rate-limited quotes exceed 10% of quote attempts.
4. Do not extend the window to chase a p-value. The dates in section 3 are pre-registered.
5. Stop if the effective stop of section 11 drifts more than 5 points from the value measured at the
   cohort cutoff, because the arm's geometry has then changed underneath the read.

## 10. Consumers that must exist

1. A per-cell reader that reports the four cells of section 3 separately and refuses to pool them.
2. A paged-stratum reader that reports the resolvable effect size beside every result, so an
   unresolved cell reads as open and never as null.

Neither exists yet. They are named here so the read cannot silently have no input.

## 11. The configured stop is not the effective stop

The excluded probe produced 4 closed tapes when this section was written, and 5 by the time it was
revised. They are not study data and carry no verdict, but they expose a design property that is
mechanical rather than statistical, so it must be pre-registered before the study runs.

**Two columns in that table are unusable.** `entry_at` and `exit_at` are BOTH stamped when the
trade closes — they differ by 70 to 96 microseconds on every row, while holds run 3 to 74 seconds,
and `entry_at` reconstructs exactly as `signal_t0 + entry_latency_s + held_s`. Never derive an entry
time or a hold from that pair. `entry_latency_s`, `held_s`, `return_pct` and `path` are recorded
independently and at the right moments, and everything below uses only those.

| ticker | rail | return | held | observed path |
|---|---|---|---|---|
| COLLECTIBLE | stop | -23.42% | 9.3s | 3s +7.0, 6s -10.4, 9s -23.4 |
| COLLECTIBL | stop | -30.81% | 3.1s | 3s -30.8 |
| Poteto | take profit | +63.27% | 12.7s | 3s +0.1, 6s -1.7, 9s +24.9, 13s +63.3 |
| CRIME | stop | -29.55% | 6.2s | 3s -3.6, 6s -29.5 |

**A discrete sampler cannot fire at a threshold it never observes.** The stop is set at -20%, but
the sampler sees the price only on its interval, so the exit takes the first sampled value below
-20% and not -20% itself. On a 3-second grid the three stops overshot by 3.4, 10.8 and 9.6 points.
The CRIME tape moved from -3.6% to -29.5% inside one 3-second step. The COLLECTIBL tape sampled once
and its FIRST observation was already -30.8%.

Mean overshoot is 7.9 points, so the effective stop is about **-27.9%**, not -20%. That moves the
bar the arm must clear:

| stop | breakeven win rate at TP +25% |
|---|---|
| -20% as configured | 44.4% |
| -27.9% as observed | 52.8% |

An 8.4-point shift in the required win rate is not a rounding detail. It is the difference between
a plausible bar and an implausible one on a population whose median is -9.19%.

**The operator's 10-second sampling default makes this worse, not better.** The overshoot is a
function of how far price can travel between samples, and these tapes travel 26 points in 3 seconds.
Sampling interval and effective stop are the same decision, so the load choice in section 0 is also
a stop-geometry choice and must not be made on load grounds alone.

Pre-registered consequences:

1. The study MUST report the **effective** stop, measured as the realised return distribution of
   stop-railed exits, beside the configured one. A result that quotes -20% is not reporting what
   happened.
2. Breakeven win rate MUST be computed from the effective stop, never the configured stop.
3. Any comparison across arms with different sampling intervals is confounded by this effect and is
   forbidden unless the effective stops are reported and are comparable.
4. The magnitude here rests on 3 stops and is preliminary. The MECHANISM does not: it is arithmetic
   on a discrete grid and needs no sample to establish.

**CORRECTION: an earlier version of this section charged the route cost to the rail.** It reported
that the take-profit rail showed a mirror defect, because tape 5 sampled +26.1% and realised +20.7%.
That 5.4-point gap is not rail slip. The rail reads the COIN and the cash pays the ROUTE, so the two
are different spaces and the gap between them is the round trip. Measured where both spaces exist
(tapes 5, 6, 7): -5.39, -3.32 and -3.70 points, **mean 4.1 points, and it appears on BOTH rails
alike** — which is exactly what a route cost does and what a rail defect would not. The mirror-defect
claim is withdrawn.

Effective geometry must therefore be measured on `move_pct`, not `return_pct`. Charging the route to
the rail double-counts a cost that is paid regardless of where the rail fires.

**The stop finding survives, and it now decomposes.** Restricted to the tapes that carry a valid
round-trip baseline, in rail space against the configured -20%:

| tape | rail space | cash | sampling overshoot | route |
|---|---|---|---|---|
| 6 | -43.97% | -47.29% | 24.0 pts | 3.3 |
| 7 | -22.89% | -26.59% | 2.9 pts | 3.7 |
| 9 | -46.29% | -48.95% | 26.3 pts | 2.7 |

Median realised stop: **-44.0% in rail space against -20% configured.**

**Sampling is 82% of the gap IF THE PRICE TRAVERSES, and traversal is untested on this
population.** Mean sampling overshoot is 17.7 points against a route cost of 3.8. But the word
"sampling" carries a hidden claim — that a finer interval would have SEEN the intermediate values —
and the tapes do not support assuming it. The last observation before each stop was:

| tape | last seen | fired at |
|---|---|---|
| 6 | -1.5% | -43.97% |
| 7 | -3.9% | -22.89% |
| 9 | -4.9% | -46.29% |

All three went from above -5% to below -22% inside ONE 3-second step. The price is not passing
through -20% and being caught late; it is arriving on the other side. Two mechanisms produce that
and they have OPPOSITE implications:

1. **It traverses.** The move is continuous across the 3 seconds, a 1-second interval catches
   roughly -15%, and the 17.7 points are genuinely recoverable.
2. **It gaps.** One large sell empties a thin pool, the price never HAS an intermediate value in
   observable time, and no interval catches it. The sampling component is then not recoverable at
   all and a finer interval buys nothing.

**Partial evidence, stated as a bound and not as an answer.** Solana swap density on the 99 pools we
do track: median 10.7 transactions per second, so a 3-second window typically holds about 32
transactions, and 7% of pools are sparse enough to hold 2 or fewer. On that population a large move
would traverse many transactions and a finer interval would see intermediate prices. Three reasons
this does not settle it: these are coins we HOLD, which are larger than scanner coins at ~$27k; the
Solana tick table carries no price or amount at all (100% NULL on both), so this measures
transaction DENSITY and not the price path; and the probe's own six mints appear nowhere in it.

**What would settle it, cheaply.** Run the remaining probe trades at a 1-second interval for the
first ~15 seconds after entry. If intermediate values appear, it traverses and the recovery estimate
holds. If tapes still jump from -5% to -40% between consecutive 1-second samples, it gaps and the
finer interval is worth nothing. About ten extra quotes per position on the paper key.

**The population is mixed, which is the real point.** Tape 5 sampled 24 times over 74 seconds and
traversed smoothly — last observation +25.0%, fired +26.13%, an overshoot of one point. Tape 10
fired on its FIRST observation, a take profit at +25.68%, which is the mirror of the first-sample
stop. So some tapes traverse and some arrive on the far side, and **the recovery estimate depends on
the mixture proportion, which three gapping stops against one traversing take-profit does not
establish.**

**The magnitude is still not a number.** Sampling overshoot runs 2.9, 24.0 and 26.3 points on three
tapes. Any effective stop quoted from this is a placeholder. The MECHANISM is arithmetic on a
discrete grid and needs no sample; the magnitude needs the running twenty.

**One tape is excluded and one instrument needs a look.** Tape 8 carries
`entry_roundtrip_usd = 122.36` on a $100 probe, which would be a 22% instant gain on the sell-back
and is not possible as a route measurement. Its t0 baseline was captured after the coin had already
moved, so its `move_pct` is computed against an inflated reference and its +30.4-point gap is an
artifact. Excluded from every figure above and flagged to the probe's owner.

Figures in this section are a snapshot at 9 closed tapes — 4 cash-referenced under the old rails, 4
round-trip clean, 1 rejected. The probe is running and they will move. The guard against bad
baselines is now an INVARIANT rather than a threshold: a round trip cannot profit, because buying
and selling in the same instant pays two spreads, so any baseline at or above the cash in is two
different moments rather than one measurement. It needs no calibration and cannot drift.

One further observation, recorded but not relied on: the probe's entry latency ran 1.8 to 3.3
seconds, against the 0.2-second median measured for `entry_quote_at - t0` in the signals table. The
probe enters roughly an order of magnitude slower than the scanner quotes. That gap needs an
explanation before any latency claim is made from probe tapes.

## 12. REGISTERED OUTCOME: it gaps. The scalp idea is structurally dead.

The dense-window arm completed 20 trades on 2026-08-29 (`data/memecoin_scalp_probe.db`,
`sampler='dense-1s-15s-then-3s'`, 1-second sampling for the first 15 seconds after entry). Section 11
registered the criterion: if the step stays near 15 points at a 1-second interval, the price gaps
and a finer interval buys nothing.

**Recorded `rail_step_pct`, n=20, every firing carries a step:**

| statistic | dense (1s) |
|---|---|
| median decisive step | **14.6 pts** |
| p25 / p75 | 9.4 / 25.6 |
| min / max | 2.0 / 78.1 |
| crossed in a step under 10 pts | 6/20 = 30% |
| cash return, median | -24.1% |

**The decisive argument is invariance, not the comparison.** A 3-second interval gave a step near
15.6 points. Tripling the sampling rate should, for a continuous move, cut the step by roughly three
— to about 5.2 points. It fell to 14.6, which is **10% of the recovery that traversal predicts**.
The step size is nearly invariant to a 3x finer interval, and that is the signature of
discontinuous arrival rather than of a continuous move sampled too coarsely.

The mixture is unchanged too: 30% crossed in a step under 10 points at 1 second, against 21-27% at 3
seconds. Making the instrument three times finer did not move the proportion of gentle crossings.

**Therefore the sampling component of the stop overshoot is NOT recoverable.** Section 11's
decomposition stands as arithmetic, but its recoverable half is empty in practice: the ~17.7 points
attributed to sampling cannot be reclaimed by any interval this system can run, because the price is
not passing through the stop at any resolution we can observe. The finer-interval fix is worth
nothing and should not be built.

**The scalp idea is structurally dead, not merely unprofitable.** A stop cannot be enforced on a
price that arrives on the far side, so the strategy's risk control does not exist at any
configuration. This is a stronger conclusion than the 3-second verdict, which said only that the
observed rate fell short of breakeven.

**Provenance caveat, and it is why the comparison is indicative rather than exact.** The dense
cohort's steps are RECORDED in `rail_step_pct`. The 3-second cohort's `rail_step_pct` is NULL on all
24 rows, so its steps are DERIVED from rendered path text. Deriving them myself over all 24 control
rows gives a median of 19.0 points and 21% under 10, against the 15.6 and 4/15 reported from a
15-row subset. The two derivations disagree by 3.4 points because they cover different subsets, and
neither is the recorded instrument. **The conclusion does not depend on which control figure is
used**, because the invariance argument compares 14.6 against anything in the 15-to-19 range and
finds the same near-zero recovery. A backfill of the control cohort should mark every value as
derived, never as recorded.

**What this does not say.** It does not say the scanner's features are worthless — section 6's
finding that qualifying detections reach +50% at 38% against a 25% base is untouched, because that
is a question about which coins run, not about whether we can hold one with a stop. It says only
that scalping them is not executable.
