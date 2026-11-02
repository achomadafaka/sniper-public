# Research that can change a decision

Status: discovery feasibility implemented; revised validation design proposed. No live policy, collector or cohort switch. The active v4 contract remains immutable. Its scheduled read is not replaced by this companion audit.

## The job

Determine whether a decision-time observation adds enough information to justify a later execution experiment. The present +30-second exact-quantity quotes can study short-horizon quoted outcomes. They cannot establish net executed profit or validate an exit strategy.

There are three distinct jobs, in order:

1. **Measurement:** can we observe the relevant coins comparably, including failures?
2. **Signal:** does one feature add information beyond simpler explanations on new coins?
3. **Paper strategy:** does a fully specified entry and exit rule behave as intended with realistic timing and quotes?

We are at the first job. Multiple named paper books do not advance us to the third.

## First decision: repair selective quote coverage

**Question:** are coins with fewer observed buyers systematically losing their +30-second measurements, and which failure mechanism explains that?

**Why it matters:** conditioning on successfully quoted exits can manufacture apparent differences between winners and losers. More coins will not fix that selection.

Fresh discovery snapshot (`decision-audit.json`, 2026-09-28; never old-paper proof): the provisional buyer split has 67 missing outcomes among 132 lower-buyer coins, versus 11 among 142 higher-buyer coins. Missing feature values add further uncertainty. Worst-case allocation gives a realized-sample win-rate gap of about -49 to +21 percentage points. These are identification bounds, not confidence intervals; they do not address day dependence or confounding.

**Measurement repair prepared:** keep bounded HTTP status and uppercase provider error code in existing request payloads. No free-text error body, URL, credentials, extra call or retry. Existing reason categories and quote acceptance remain unchanged. Old rejection details cannot be reconstructed.

**What each answer changes:**

| Finding in fresh diagnostics | Next action |
|---|---|
| Invalid request or identity | Repair that request; new producer version before fresh evidence. |
| Explicit no route | Report route availability separately. Do not label the coin a price loser. Define any later route-conditioned study explicitly. |
| Rate limit / provider failure / deadline | Repair capacity or scheduling; do not infer a token property. |
| Unclassified response | Keep unknown and improve diagnostics. Do not guess from a generic rejection. |

Proceed to signal validation only if the missingness bounds, observable overlap and attainable sample precision can address the stated practical difference. No arbitrary capture percentage is substituted for that calculation.

## First signal candidate: buyer participation beyond momentum

**Thesis:** among otherwise comparable observations, broader distinct-address participation contains information about the next 30-second quoted return beyond current momentum and age. The possible mechanism is less dependence on repeated buying by a small set of addresses. Addresses are not resolved people; this is an observational hypothesis, not a manipulation claim.

**Decision it informs:** whether buyer participation deserves a separate research alert component. It does not authorize a funded gate, sizing change, or removing a safety input.

**Alternatives to rule out:** older coins naturally accumulate buyers; faster tape truncates the latest-100-trade snapshot; momentum and trade count already explain the difference; only liquid coins retain measured outcomes.

**Comparison:** higher/lower buyer participation within common source, age, market-cap and momentum contexts, additionally accounting for recorded snapshot duration, truncation and buy-trade count. Cutoffs and the final matching model must be frozen before a future validation period. The diagnostic's median split and coarse coverage buckets are exploratory, not silently substituted into v4.

**Current feasibility:** both buyer groups exist. Adding tape-duration/truncation/trade-count buckets leaves 219 of 286 mature acquired-quantity observations in contexts containing both groups at the snapshot. This is a coverage count, not an effective sample size or proof of balance. Missing outcomes still prevent a useful conclusion. Buyer snapshots span roughly 6–120 seconds, with a median near 39 seconds; raw counts alone are not a fixed-window demand measure.

**Supporting result:** a registered difference survives outcome-missingness bounds, context adjustment, sampling uncertainty and independent days in an untouched forward set.

**Evidence against useful discrimination:** the complete uncertainty range falls inside the registered practical-equivalence band on the specified bounded outcome. This rules out useful discrimination only for that feature definition, universe and horizon. Win-rate equivalence cannot rule out a mean-return or tail-payoff difference.

**Inconclusive result:** missingness, overlap or precision still permit a meaningful effect either way. Name the exact missing measurement; do not call the feature useless or automatically extend the experiment with old data.

Raw mean return has no finite upper bound for unobserved outcomes. Do not conceal this by using complete cases, assigning missing outcomes zero, assuming missing-at-random, or silently capping returns. A bounded alternative objective would require an explicit new contract.

## Disposition of existing lanes

| Existing name | Actual research purpose | Current disposition | Re-entry requirement |
|---|---|---|---|
| Scanner | Shared discovery observations and collection feasibility | Retain as exploratory; not a paper strategy verdict | Passing measurement and precision assessment for one frozen question |
| Golden | Incremental value of a frozen roster label beyond basic coin state | Discovery label only; rare positive observations | Enough positive observations for the stated uncertainty target; no old roster performance as proof |
| Conviction | Added value of an aggregate score beyond its simpler components | Discovery label only; currently little score>=10 support | Two supported comparison groups and a reproducible simple baseline |
| Confirmed | Whether later wallet confirmation adds information at the time it becomes known | Secondary discovery only | A new confirmation-time decision, fresh quantities, same-time eligible controls; never back-label the initial entry |
| Quiet-before-buy | Difference after independently observed quiet versus active prehistory | Blocked by measurement | Independent pre-buy coverage in both groups; held-position marks are not a representative market tape |
| Clone/refusal paper | Whether a particular refusal rule helps under the same policy and source population | Blocked by invalid reporting epoch / provenance | Stable per-row producer and policy identity, same-clock comparisons, explicit lag and coverage |
| Scalp/hold exits | Comparison of complete exit policies | Not validated by this capture | Paths and quotes sufficient to evaluate both policies; sparse endpoints cannot reconstruct stops |
| Radar / V6 | Archived | Excluded | No reactivation proposed |

These are analytical dispositions. No running service is silently stopped or relabeled, and no funded authority changes.

## Delivered tools and boundaries

`research_decision_audit.py` uses the active cohort's pinned reader. It reads discovery only, obeys the existing window/expiry checks, and emits sample-level adversarial win-rate bounds with missing inputs retained. It separates failed entries, incomplete follow-up windows and secondary confirmations. It reports group counts, common-context coverage, buyer-tail coverage and quiet-history gaps.

The old formal report is deliberately unchanged: the new diagnostic does not weaken its guards or overwrite its registered six-feature test. A future experiment must register an answerable objective before activation.

The HTTP diagnostics patch changes a hashed producer file. It is prepared and tested, not active. Activation requires a new research-only cohort/release; preserve v4 and its records, use unchanged quote budgets/quantities/horizons, and never restart the funded runner. Rollback restores the prior research manifest and release; no DB deletion or backup is proposed.

Validation: exact synthetic bounds including adversarial missing feature assignments, zero versus unknown, failed-entry separation, disjoint observation-window controls, safe HTTP diagnostics and existing scanner/collector regressions. No live provider calls are needed to run the audit.
