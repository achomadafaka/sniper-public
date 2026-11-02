# TP confirmation window — pre-registration

Written 2026-08-29, before the instrument writes a row. The hypothesis: instead of selling instantly
at TP +25%, ride while the route price stays at or above TP after the touch, and sell when it stops.
Window candidates `w` in {3, 5, 10} seconds. Measurement only; no rail changes from this document.

The instrument: 60 seconds of 1-second route-space sellback quotes on the research key after every
funded `real_tp` exit, clip-sized.

## 0. The confound decides what this study can conclude — read this first

**The followup begins after OUR exit. The hypothesis is about the path had we NOT exited.** Those
differ by our own market impact, and no amount of data collected this way removes the difference.

Our sell pushes the price DOWN, so the observed post-exit path **understates** the counterfactual
continuation. That asymmetry is the useful part:

- **A POSITIVE result is conservative and can be trusted.** If the confirm window wins against a
  path depressed by our own sale, it would win by more against the true counterfactual.
- **A NULL result is UNINFORMATIVE and must not be reported as a refutation.** It cannot be
  separated from our own footprint.

Register that now, because a null is the likely outcome and it will be tempting to read it as an
answer.

**The footprint is probably small, and it must be measured rather than assumed.** On 126 of 138
funded TP exits, `execution_slippage_pct` runs a median of **+0.04%** with p10 -4.32% and p90 +3.11%;
the clip is a median 0.006% of entry market cap. Typical impact is negligible and the tails are a
few percent. But market cap is not book depth, and thin books are exactly where the tails live.

**Required of the instrument:** record the last pre-exit mark and the first post-exit mark on every
followup, so the impact and its recovery profile are measured per exit rather than argued about.
Stratify the read by that measured impact. If the first-second drop correlates with the outcome, the
confound is live and the study is bounded by it.

## 1. The estimand

For each funded `real_tp` exit and each window `w`:

    confirm(w) = exit at the FIRST post-touch mark strictly below TP,
                 or at w if the price is still >= TP at w

Compared per exit against the realised instant exit, in route space, paired.

**Gap-through is priced at the mark that actually broke the floor, never at TP.** This is the
stop-overshoot lesson applied forward: a threshold does not fire at its own level, it fires at the
first observation past it. A confirm window is a floor, and floors leak.

**Registered prior on how much it leaks.** On the scanner population at 1-second sampling the median
decisive step was **14.6 points**, and it was nearly invariant to a 3x finer interval — the price
arrives on the far side rather than passing through. If funded copy coins behave the same way, the
confirm's floor leaks by something of that order and the leak is not recoverable by sampling faster.
That population is not this one, so this is a prior to be tested and not a result. **The instrument
must report the realised leak distribution, since it is the whole downside of the design.**

## 2. The mixture statistic, and why A12 does not already answer this

Report, per window: `P(route price >= TP at w)` and, conditional on that, the continuation magnitude.

**A confirm window is not a higher target, and the distinction is the reason this hypothesis is
still open after A12.**

| design | behaviour after touching +25% and falling back |
|---|---|
| higher target (+50%) | never exits at +25%; rides down to the stop |
| **confirm window** | exits at the first mark below +25%; keeps the floor |

A12 found expected value falls monotonically above +25% — but A12 varied the THRESHOLD, which
removes the floor. The confirm KEEPS the floor and adds conditional continuation on top. Its payoff
is

    P(continue past w) x E[extra gain | continue]   -   P(gap) x E[leak]

which A12 never evaluated. **A12 does not bear on this hypothesis and must not be cited against it.**

**A prior from existing data, with its own limit stated.** Among 121 funded `real_tp` exits carrying
an executable peak, the median route peak is **+30.4%** against a +25% trigger, with p90 +61.3%. So
the route reached about 5 points past the trigger on the median TP exit. That is consistent with
real continuation and it **cannot distinguish a peak reached BEFORE the trigger fired from one
reached after**. The new instrument separates them; this figure only says the question is worth
asking.

## 3. Censoring

Every mark carries the capture lane's `quality`. The observed vocabulary is `ok`, `provider_error`,
`no_route`, and NULL.

- **A window containing any non-`ok` mark is CENSORED, never resolved.** A `no_route` is not "the
  price fell"; it is an unobserved price.
- **The censored rate is reported beside every result.** `no_route` correlates with dying coins —
  the same missing-not-at-random pattern that made `executable_peak_price` absent on 37% of closes
  with those rows the losers. Dropping censored windows therefore biases TOWARD continuation, which
  is the direction that favours the hypothesis, so silence about the rate is not neutral.
- A window that ends because the position's coin became unroutable is a distinct outcome from one
  that ends because the price fell, and the two must never be pooled.

## 4. Accrual, in live days

Funded `real_tp` exits: 138 all-time, **99 in the last 14 days = 7.1 per wall-clock day**.

| host uptime | real_tp exits per LIVE day |
|---|---|
| 21% (the 2026-08-29 capture window) | 33.7 |
| 59% (the 15-day figure) | 12.0 |

**State every read date in LIVE days and convert by the uptime that actually holds.** A wall-clock
figure here is an accident of the laptop's habits, which is the error this desk made on the refusal
accrual earlier today and had to correct by a factor of five.

## 5. Kill and adopt, named now

The paired difference will be **zero on most exits** — when the price falls away immediately, both
designs exit in the same place. It is non-zero only on continuations and on gaps, so its
distribution is a spike at zero with two tails, and its variance cannot be guessed from the funded
return spread. **Estimate the paired-difference sd from the first 30 uncensored windows and state
the resolvable effect before reading any result.**

Multiplicity: three window candidates are three tests. Control with **Holm at 0.05 across the three**,
and report the best window's raw p-value beside its corrected one. A best-of-three winner reported
uncorrected is the multiplicity error this desk has flagged twice this week.

**ADOPT** requires all of:
1. Mean paired improvement of at least **+2 percentage points per TP exit** — 8% relative on a +25%
   target, which is the smallest change worth the added gap exposure and rail complexity.
2. The Holm-corrected interval excludes zero.
3. The measured leak in section 1 does not consume the gain — reported as gain and leak separately,
   never as a net figure alone.
4. The confound stratification in section 0 shows the result is not concentrated in the exits with
   the largest measured own-impact.

**CLOSE THE QUESTION** if `P(route price >= TP at w=3s)` is below 50%. Below that the window fires
too rarely for the mechanism to exist, and no larger sample changes it.

**DO NOT** extend the window set beyond {3, 5, 10} after seeing results, and do not read any window
before its accrual target is met.

## 6. What this study cannot answer

1. Whether a confirm window helps had we not sold — section 0. It bounds the answer from below.
2. Anything about the stop side. This measures the TP rail only.
3. Whether the result transfers to a different clip size. The followup is clip-sized, so it measures
   the impact of the size we actually trade and no other.

## 7. INSTRUMENT HEALTH CHECK, 2026-08-29 — first run audited the WRONG COLLECTOR

> **CORRECTION.** The failure below is real and belongs to the LADDERED in-runner collector
> (`memecoin_post_tp_schedules` / `_results` in the funded ledger `sniper_tracker.db`), which ran
> 10:45-12:30 under a shared flag. **It is not the registered instrument.** The registered one is
> `memecoin_post_tp_followups` in `data/memecoin_refusal_captures.db`, written by its own worker
> (`memecoin_tp_continuation_capture_loop.py`), and it PASSES on resolution — see section 8.
>
> **Everything below still stands as a ruling about the laddered rows**: they answer a different
> question, they must never enter this study, and the do-not-amend-the-windows doctrine holds.
>
> **The incident.** Two collectors, near-identical names, different databases. I searched
> `sniper_tracker.db` alone, matched on the name, and audited the twin — finding a real defect on
> the wrong instrument. Both this desk and Alpha only resolved it by checking TABLES rather than
> NAMES. That is the second time the two-collectors-one-name hazard has cost a read.

### The laddered collector cannot serve this read

Run before any outcome was examined, per section 5's requirement to validate before reading. No
outcome statistic was computed: this section reports instrument properties and the section 0
nuisance parameter only.

### The finding: resolution mismatch, and it is structural

| | registered here | actually built |
|---|---|---|
| sampling | 60s of **1-second** quotes | **one** quote per horizon |
| horizons | windows w in {3, 5, 10}s | 60, 300, 900, 1800s |
| finest resolution | 3s | **60.2s** |

Measured: 526 results across 526 followups — **exactly 1.00 observations each** — with a minimum
`elapsed_s` of **60.2s** and **zero observations below 60s**.

**The registered estimand cannot be computed from this instrument.** Windows of 3, 5 and 10 seconds
require sub-10-second resolution that no row possesses. This is not a calibration gap that more rows
close; every additional row is collected at the wrong resolution.

### Section 0's confound measurement also fails

Section 0 requires the first post-exit mark to measure our own footprint and its recovery. The first
available post-exit observation is at 60 seconds, by which time a thin book has largely recovered.
That measures market movement, not our impact, so the confound stratification section 0 makes
mandatory is currently unserved.

### What IS healthy

The pre-exit side. `memecoin_route_series` covers **132 of 133** followup positions with a median of
**10 pre-exit observations at a 1.3-second median cadence**. Section 0's pre-exit requirement needs
no new column and no new collection — it is satisfied by a join, as already relayed.

### The fix is cheap, because the capability already exists

`memecoin_route_series` ALREADY samples at ~1.3s during the hold and stops at the exit — post-exit
observations are a median of 1. **Extending that series to continue for roughly 60 seconds past the
exit produces exactly the registered resolution using machinery that already works**, rather than
building a second one-second sampler beside it.

### What must NOT happen

**Do not amend the window set to {60, 300, 900, 1800} to match the instrument.** That is a different
hypothesis: a 60-second check means holding through a full minute of decline before reacting, which
is not the operator's proposal and not what section 2's payoff describes. Fitting the question to the
data available is the failure this document exists to prevent, and it would be especially tempting
here because 526 rows already exist.

The current instrument answers a legitimate but **different** question — does the coin keep running
at 1, 5, 15 and 30 minutes after we sell. Keep it, label it as that, and do not let its rows enter
this study.

### One vocabulary note for section 3

The instrument's `status` field reads `healthy` (524) and `route_absent` (2), not the capture lane's
`ok` / `provider_error` / `no_route`. Section 3's censoring rule must map onto the vocabulary that
actually lands: `route_absent` is the `no_route` case and censors the window.

## 8. THE REGISTERED INSTRUMENT — audited 2026-08-29, three defects, one serious

`memecoin_post_tp_followups`, 227 rows, 4 completed followups. No outcome statistic computed.

### It PASSES on resolution

| | registered | measured |
|---|---|---|
| cadence | 1 second | 51-59 observations over ~59s |
| first observation | as early as possible | **2.04 / 2.30 / 2.62 / 3.85s** |
| sub-5s observations | needed for w=3 | 11 |

The 1-second resolution the laddered twin lacked is present and accruing.

### DEFECT 1 (serious): `exit_price` and `quote_price` are on DIFFERENT BASES

On 1 of 4 followups the two columns disagree by **exactly 1000x**. `exit_price` is derived using the
token's true decimals (1e9 for that mint); `quote_price` is derived at 1e6. Verified by implied
quantity: `quote_value_usd / quote_price` over `exit_value_usd / exit_price` gives 1000.0000 on that
followup and 1.0000 on the other three. Six-decimal tokens agree by luck; nine-decimal tokens do not.

**This lands on the study's core operation.** The read compares `quote_price` against
`TP x exit_price`. On a mismatched row that comparison is wrong by three orders of magnitude and
reads as an instantaneous collapse, so the confirm window would exit at the first mark on EVERY such
token — a systematic false negative that looks exactly like the hypothesis failing.

**The sound basis is the USD values.** `exit_value_usd` and `quote_value_usd` are both correct and
both in USD on every row. Compare VALUES, or derive both prices from one decimals source. This is
the `entry_out_atomic` / `exit_out_atomic` unit trap again, one table over.

### DEFECT 2: the shortest window is not evaluable on every followup

Detection lag runs 1.79 to 3.62 seconds, so the first observation lands at 2.04 to 3.85s. Then:

| window | evaluable |
|---|---|
| w = 3s | **3 of 4** |
| w = 5s | 4 of 4 |
| w = 10s | 4 of 4 |

**This is selection, not merely missing data.** The followups where w=3 is unevaluable are selected
on detection lag, and detection lag plausibly correlates with load and therefore with market
conditions. Report the w=3 evaluable rate beside every w=3 result, and treat w=3 as reported on a
subset rather than on the cohort.

### DEFECT 3 (already being fixed): `post_exit_mark` is structurally dead

0 of 227 rows carry one, because the capture reads marks at SCHEDULE time — moments after the close,
before any post-close mark exists. A column that can only be NULL reads later as a measurement that
came back empty.

**Section 0 does not need it.** `pre_exit_mark` is 100% populated, and the instrument's own FIRST
OBSERVATION at 2.0-3.9s IS the first post-exit mark. Section 0's requirement is served by different
columns than the schema anticipated, which is the right outcome and worth stating so nobody
re-adds the column.

### Censoring vocabulary is correct

`quality` reads `ok` (218), `no_route` (5), NULL (4) — matching section 3's vocabulary. The
`censored` flag is populated only on completed followups, which is consistent with computing it at
completion.

## 9. READ, 2026-09-27

The read ran on 232 exits: `docs/audits/2026-09-27-tp-confirmation-window-read.md`
(`scripts/research/tp_confirm_window_read.py`). ADOPT is not met on any window. The observed
effect is negative on all three (-2.0 to -2.6 pp) and not significant after Holm. Per section 0,
this is not a refutation, because the own-impact confound is live.
