# Does any signal-time feature carve a positive subset? — pre-registration

Registered 2026-08-29 **before any feature-outcome relationship was computed by this desk.** The
only queries run to write this were structural: column inventory, row counts, and date span. No
candidate was scored.

The question: **does any signal-time feature in `runner_signals` carve out a subset of scanner paper
signals with positive forward expectancy, at a size worth trading?** And what is the honest bar for
saying so, given a large candidate set and a three-feature look already spent.

## 0. The prior, and the outcome this design exists to be able to CONFIRM

Every arm is negative over 7 days: `candidate_scalp` -1.0%/trade (n=30), `conviction_scalp` -5.0
(393), `hold` -6.1 (142), `golden_scalp` -6.3 (1288), `conviction` -11.3 (394), `scalp` -12.9 (53),
`golden` -14.1 (1288), `delayed` -14.5 (30), `confirmed` -15.6 (141).

**And the lane's own gating variable does not predict.** On the golden arm, mean return by
`golden_buyers` is flat-to-noisy and negative in every band with n>=20 (gb=1 -10.7 n=576; gb=2 -13.8
n=266; gb=3 -22.9 n=122; gb=4 -25.8 n=82; gb=5 -8.3 n=49); the only positive bands carry n=1..3.

**That is the strongest evidence available for the null**, and it is why this is registered as a
falsifiable question rather than a feature hunt: *if the variable a lane was BUILT on does not
separate, the prior that no other column will is a serious one.*

**The most likely registered outcome is that the population is unconditionally negative and no gate
rescues it.** That is a publishable answer and it is the one that stops the tuning loop. This design
is built to be able to state it with a bound, not merely to fail to find something.

## 1. Population and outcome

- **Unit**: one closed scanner paper trade. Outcomes live in `runner_paper` (3,896 rows); features
  live in `runner_signals` (17,949 rows, 2026-08-21 to 2026-08-29), joined on the signal.
- **Outcome**: realised return per trade, the same quantity the arm means above are computed on.
- **Arms are analysed SEPARATELY, never pooled.** They differ in entry rule and in n by a factor of
  40, and a pooled search would find the largest arm's structure and call it general.

## 2. The candidate set, fixed now

`runner_signals` has 107 columns. **24 are post-signal or outcome-bearing and are INELIGIBLE** —
`track_*`, `exit*_out_atomic`, `entry_out_atomic`, `censored_reason`, `candidate_*`,
`golden_entry_*`, `confirmed_*`, `*_survival_*`, `*_alerted_at`. Using any of them is leakage, not a
feature.

**77 signal-time columns remain and are the candidate set.** It is fixed at registration; no column
may be added after a result is seen.

**Three are already SPENT** — coin age, market cap, and rugcheck score were looked at before
registration. They stay in the search, but **they cannot be reported as discoveries**: any result on
them is a confirmation of a prior look and must be labelled as such.

## 3. The multiplicity control: a permutation null on the MAX statistic

Bonferroni is the wrong instrument here. The 77 columns are heavily correlated by construction —
`mcap_usd` with `meta_usd_mcap`, `holder_top1_share` with `holder_rank1_share`, the whole `cluster_*`
family, the whole `golden_*` family — so a correction assuming independence over ~693 tests (77
columns x ~9 decile thresholds) would be both wrong-shaped and brutally conservative.

**Registered control: a permutation test on the maximum.**

1. Run the full search on the training window: for every eligible column, every decile threshold,
   both directions, record the best subset mean subject to the size floor in section 4.
2. Shuffle the OUTCOME labels within the training window, holding the feature matrix fixed. Re-run
   the identical search. Record the best subset mean.
3. Repeat B >= 2000 times. The null distribution is of the MAXIMUM, so it absorbs both the column
   count and the arbitrary correlation between columns without assuming either.
4. **The observed best must exceed the 95th percentile of that null.** Report the observed value's
   percentile in the null regardless of outcome.

This is the only control that answers "best of many correlated searches" honestly. A per-comparison
p-value on the winner is the error this document exists to prevent.

## 4. The size floor, registered before the search

**A subset must contain at least 5% of its arm's trades AND at least 30 trades to count.** Without
this the search returns a 12-row pocket every time, and a positive mean on 12 rows is noise wearing
a rule's clothing.

The floor is part of the search, not a filter afterwards: subsets below it are never candidates, so
the permutation null is computed under the same constraint.

## 5. Train and forward, split by TIME

**Never cross-validated in place.** Signals are serially correlated within a coin and within a
market regime, so a random split leaks the regime across folds.

- **Train**: 2026-08-21 to 2026-08-27.
- **Holdout**: 2026-08-28 to 2026-08-29, untouched until the rule is frozen.
- **Prospective**: data accruing after this registration, which is the decisive read. The holdout is
  ~3 days of a market that may not repeat; the prospective window is the only one that was not
  available to be peeked at.

A rule must survive BOTH to be called real. Surviving the holdout alone is a hypothesis.

## 6. Scope limits, registered so they are not quietly exceeded

- **Single-feature rules only in v1.** Two-way interactions raise the effective search to ~237,000
  and need their own budget and their own permutation null. If v1 finds nothing, a two-way search is
  a SEPARATE registration, not a continuation.
- **No threshold refinement after seeing a result.** Deciles are the grid; a rule that works only at
  a hand-tuned cut is a rule fitted to the sample.
- **The three spent features cannot be re-reported as discoveries.**

## 7. Kill and publish

**PUBLISH THE NEGATIVE** if the observed max does not exceed the permutation null's 95th percentile.
The finding is then: *no single signal-time feature in the 77-column candidate set separates a
tradeable positive subset in any arm.* That stops the tuning loop, and it is the expected result.

**A negative must be BOUNDED, not merely reported.** State the smallest effect the search could have
found: given each arm's n and outcome spread, report the minimum subset mean that would have cleared
the permutation bar at the size floor. "We could have detected any subset of >=5% with mean >= +X%
and found none" is a finding; "we looked and saw nothing" is not.

**ADOPT** requires all of: the observed max clears the permutation bar on train; the same rule,
unchanged and un-refit, is positive on the holdout; and it is positive on the prospective window at
its own size floor. Anything less is a candidate, not a lane.

**A rule that clears train and fails either forward window is a NEGATIVE result and is reported as
one** — not re-fit, not re-thresholded, and not moved to another arm to try again.

## 8. What this cannot answer

- Whether a feature the scanner does not currently record would separate. The candidate set is what
  the table holds, and absence from it is not evidence of irrelevance.
- Whether a rule that fails at trade level would work at portfolio level through position sizing.
  That is a different estimand and needs its own design.
- Anything about the funded lane. This is scanner paper only, and the two populations have already
  been shown to diverge.

## 9. AMENDMENT, 2026-08-29 — feasibility cuts, and a disclosure

### The first run is SPENT and its winners are known to me

I ran the search on the 60-column set before the mechanics constraints arrived. **That run is spent
and is not the deliverable.** Its train winners are known to this desk — `vol300 >= 96140` on golden,
`cold_buyers >= 7` on conviction, `funder_top_share >= 0.132` on conviction_scalp_v1.

**The re-run is therefore not a fresh look, and is not presented as one.** Two things bound the
damage: the permutation null is mechanical, so prior knowledge cannot bias its arithmetic; and every
cut below was driven by a mechanics pass on FEASIBILITY that arrived independently and knew nothing
of the results. `vol300`, the golden train winner, **survives into the strictest tier** — the cuts
were not shaped to preserve or to exclude it.

### A SMALLER CANDIDATE SET LOWERS THE BAR

A permutation null on the maximum over fewer masks has a lower 95th percentile. **Restricting the
candidate set makes a positive result EASIER to reach, not harder.** So the earlier negative does
not carry forward by implication and the study must be re-run — a point worth stating because the
intuition runs the other way.

### Two exclusion reasons, and they are NOT the same

A reader will assume one bucket means one reason. It does not:

- **LEAKAGE (24 columns)** — `track_*`, `exit*_out_atomic`, `candidate_*`, `golden_entry_*`,
  `confirmed_*`. Using them is cheating: they encode the outcome.
- **INFEASIBILITY (33 columns)** — `bundle_max`, `bot_uniform_share`, `fresh_wallet_share`,
  `buyers120`, `buyer_*`, `crew_score`, `sniper_held`, `conviction`, `quality_score`, `notify_tier`,
  `goplus_risks_n`, `golden_elite`, and the whole `meta_*`, `tweet_*` and `funder_*` blocks. Using
  them is IMPOSSIBLE: the golden lane decides 1.6s from t0 and these are written after it commits.
  **A feature that only exists post-hoc cannot become a gate however well it scores.**
- **DEAD (6 columns)** — all six `cluster_*`, verified at **0 non-null of 17,951 rows**. Dropped so
  they cannot dilute the permutation null with columns that cannot vary.

`funder_*` deserves its own line: it is **6% populated (1,050 of 17,951)**, not the ~25% reported,
and it produced one of the spent run's winners on n=31 drawn from that 6%. That is precisely the
finding the infeasibility cut exists to kill.

### Two tiers, because only one can move money

    107 columns
     -24 leakage      -9 identifiers      -6 dead
     ----
      68 before feasibility
     -33 infeasible at the golden decision
     ----
      35  TIER "gateable at PAGE"
     -15 too late for the QUOTE (holder_*, rugcheck_*, dev_*)
     ----
      20  TIER "gateable at QUOTE"

**The entry quote is taken inside the insert, BEFORE the holder snapshot runs.** So `holder_*` and
`rugcheck_*` exist in time to change what a human is PAGED about but not in time to change what is
BOUGHT. Both tiers are searched and both reported, and **a page-tier result must never be presented
as a trading gate.** The quote tier is the only actionable set.

### Scope: the golden arm alone in v1

Lanes decide at different times with different information — golden at 1.6s, candidate at 18.4s,
confirmed at 390s. Pooling them pools a 1.6-second decision with a 390-second one.

**This also closes a gap in the original registration that the spent run exposed.** I searched six
arms with a permutation null WITHIN each and no correction ACROSS them. One arm reached its own
97.8th percentile, which across six arms is a family-wise p near 0.12 and clears nothing. The
original text said arms are never pooled and never said how the family is controlled. Scoping v1 to
golden removes the problem instead of papering it.

### Accrual, so a date can be quoted rather than a promise

Closed paper trades run **481/day** across arms on complete days; **golden is 163/day**. A
prospective window matching the train window's power (n=969) takes **6 days**.

## 10. RESULT, 2026-08-29 — negative on the registered question

**Verdict: no signal-time feature carves a subset with POSITIVE expectancy that survives forward.**
The population is unconditionally negative and the best available gate does not rescue it. That is
the outcome section 0 named as most likely, and it is reported as the deliverable rather than as a
failure to find.

### Search 1 — SPENT, reported so the searches can be counted

Before the feasibility constraints arrived, a search ran over 60 columns across 6 arms. Its winners:
`vol300 >= 96140` on golden (+8.16%, n=97), `cold_buyers >= 7` on conviction (+30.98%, n=36),
`funder_top_share >= 0.132` on conviction_scalp_v1 (+12.95%, n=31).

**The conviction_scalp_v1 winner is the worked example of what the infeasibility cut exists to
kill**: `funder_*` is 6% populated, so that subset was drawn from a 6% slice by a column the lane
cannot consult at decision time. It scored well and could never have been a gate.

Five of six arms did not clear their own permutation null. `conviction` reached its own 97.8th
percentile — which across six arms is a family-wise p near 0.12 and clears nothing. **This data has
now been searched twice and a reader must count both.**

### Search 2 — the registered re-run, golden arm, EXPLORATORY

Conditioned on a prior spent search, so it promotes nothing on its own.

| tier | cols | masks | observed best | null p95 | percentile | verdict |
|---|---|---|---|---|---|---|
| QUOTE (actionable) | 18 | 237 | **+8.16%** via `vol300 >= 96140` (n=97) | +6.49% | 96.9th | clears |
| PAGE (alerting only) | 31 | 369 | +8.16%, same rule | +7.28% | 96.4th | clears |

The page tier adds 13 columns and finds nothing better than the quote tier's winner, so
**`holder_*` and `rugcheck_*` bought no separation** — worth knowing, because they are the fields a
human would reach for first.

### The holdout kills it, and the way it dies is the finding

Rule frozen from train, graded on 08-28..29 (golden, n=339, arm mean -12.23%):

    subset n=43  (meets the >=30 floor)
    subset mean  -4.13%
    train claim  +8.16%

**The relative improvement REPLICATES: +8 points better than its arm on train, +8 points better on
the holdout. The positive expectancy DOES NOT.** `vol300 >= 96140` reliably identifies a less-bad
slice of a losing population. It is not a gate; it is a ranking signal for a lane that should not be
trading.

Per section 7 this is a negative result and is reported as one — not re-fit, not re-thresholded, not
moved to another arm.

**Independently reproduced.** A second desk cut the holdout window differently and wrote its own
query: subset n=50 at **-4.52%** against arm n=383 at **-13.20%** — an 8.7-point relative gap
against this desk's 8.1, same sign, same level, no shared code. **The result rests on two
independent computations**, which for a negative matters more than for a positive: a null is the
easy thing to produce by accident.

### Findability: does holder concentration or a rugcheck score predict? NO.

Recorded here under the words someone would actually search. The page tier added
`holder_top1_share`, `holder_top5_share`, `holder_rank1_share`, `holder_top5_ex_rank1_share`,
`holder_top20_n`, `holders_total`, `dev_hold_share`, `dev_prior_mints`, `rugcheck_score`,
`rugcheck_risks`, `rugcheck_lp_locked`, `rugcheck_insiders`, `rugcheck_insider_nets`,
`rugcheck_insider_max_net` and `rugcheck_total_holders` — **13 usable columns of holder
concentration and rug screening, and not one of them beat a volume threshold.**

**So "should we gate on holder concentration" is answered: not on this evidence.** The same applies
to rugcheck scoring as a signal-quality gate. Both remain legitimate as SAFETY screens, which is a
different question this study did not ask.

### The bounded negative — the deliverable

    QUOTE tier: any subset of >=48 golden trades with mean >= +6.49% would have been detected.
    PAGE  tier: any subset of >=48 golden trades with mean >= +7.28% would have been detected.
    None was found that survived forward.

That is what was ruled out. It is not "we looked and saw nothing."

### What would still promote something

**Only the prospective window.** Neither search could touch data collected after this registration.
Golden closes **163 trades/day**, so a window matching the train window's power (n=969) is **6
days**. Nothing becomes a gate before then, and the relative-improvement result above is a
hypothesis for that window, not a finding.
