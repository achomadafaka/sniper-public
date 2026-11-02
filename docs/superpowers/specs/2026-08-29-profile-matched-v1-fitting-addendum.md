# `profile_matched_v1` — the fitting rule, registered before anything is fitted

> 2026-10-04 amendment: legacy sampled-path paper results are diagnostic only under
> `2026-10-04-memecoin-wallet-execution-research-design.md`. They cannot publish a
> supported strategy, promote a candidate, fit active exit rails, or select new
> funded copy-arm rails. Cached ready receipts are also blocked. Existing frozen
> funded contracts and explicit operator overlays remain authoritative. Complete
> causal one-second execution paths and an independently verified future-study
> receipt are required; that publication receipt is not implemented yet.


Addendum to `2026-08-28-wallet-promotion-prereg.md`. Written 2026-08-29, before any profile is
fitted. Everything here is measured on the funded ledger under section 4's censoring and is
reproducible from the definitions below.

The binding constraints from the parent document are unchanged: prior events only, frozen at the
cohort cutoff, chain-specific, section-4 censoring applies to the FITTING data too, default-policy
fallback where thin, no hold-derived timecap in v1, per-wallet identity by `source_user_id`.

## A0. What this arm can and cannot deliver — register this before the study, not after

> **CORRECTION 2026-08-29.** The EV figures first published here (+0.096 at 25%, +0.032 at 50%,
> -0.000 at 150%) were **wrong and are withdrawn**. They came from a mispaired computation: peak
> values were assembled grouped (all observed, then all censored) while outcomes stayed in row
> order, so 60% of rows were scored against another row's outcome. Correctly paired, the same
> formula gives -0.062 at 25%, -0.033 at 50%, -0.023 at 150%. The builder caught it by failing to
> reproduce the curve and refusing to pick the variant that fit. See A2 for what replaced the rule.
>
> **The variance components below are unaffected** — they involve no pairing.

One measurement bounds the whole exercise.

**Wallets barely differ.** Variance components on the log route-peak multiple, 29 wallets with at
least 3 observations, 250 observations total:

| component | variance | sd |
|---|---|---|
| within-wallet | 0.0456 | 0.214 |
| between-wallet | **0.0088** | **0.094** |

Between-wallet sd is 0.094 in logs — about **9.4%** in peak multiple — against a within-wallet sd
2.3x larger. So the entire room for per-wallet differentiation is roughly **+23% to +27%** around a
global optimum of +25%.

**Therefore `profile_matched_v1` is expected to be a small effect, and the parent study's kill
criterion of -3% per dollar is wide relative to what fitting can move.** This is registered as a
prediction, not discovered afterwards. If the arm shows a large effect, that is evidence something
other than the fitted knobs is driving it, and it should be investigated before it is believed.

## A1. Peak space: EXECUTABLE, with the missing rows CENSORED not dropped

Registered: fit from `executable_peak_price`, the route-space peak. Never from `peak_price`.

The chart peak is complete and wrong: it sits more than 5% above the executable peak on 25% of
positions, so a TP fitted there targets a price the book never offered. The executable peak is right
and biased: it is absent on 37% of funded closes, and those rows are the losers — mean realised PnL
**-9.14** against **+3.82** for rows that have it. Both available estimators bias the target upward,
for opposite reasons.

**The fix is not to choose between them. It is to stop dropping the missing rows.** A position
without a recorded executable peak still has a realised exit, and its peak was AT LEAST that. Every
such row enters the fit as a **left-censored observation with lower bound `max(realised_return, 0)`**.

This is not cosmetic:

| estimator | cohort median peak |
|---|---|
| observed rows only (drops 37%) | **+25.6%** |
| censored-aware (uses all rows) | **+13.7%** |

**Dropping the censored rows overstates the median peak by 11.9 points.** Any fit on observed-only
data sets targets roughly twice as far away as the evidence supports.

The artifact records `peak_space="executable"` and the observed/censored split, because a later
reader cannot tell which was used from the parameters alone.

## A2. The TP rule — a shrunk OFFSET from the funded level

`0.8 x median peak` was replaced once, by EV-argmax. **EV-argmax is now withdrawn too, and not only
because of the arithmetic error in A0. It is not identifiable from this data.** Three reasons, any
one sufficient:

1. **The outcomes were produced by the rails being varied.** 50% of fitting rows exited on a rail
   (137 take-profit, 96 stop or derisk). A take-profit exit's realised outcome is approximately +25%
   BY CONSTRUCTION, so using it as "what happens if we do not reach t" feeds the current policy into
   the evaluation of a counterfactual one.
2. **The peak distribution carries the policy's own footprint.** The observed peak histogram has a
   spike at [+25%, +30%) holding 61 of 297 observations against roughly 20 in each neighbouring bin.
   Fitting a target to a distribution whose mode is the current target re-derives the current
   target.
3. **The uncontaminated instrument does not cover the population.** `memecoin_route_series`, which
   holds the actual price path, covers 67 of 468 fitting rows — 14%.

**Registered rule, replacing it.** Do not fit the LEVEL, which is contaminated. Fit only the
RELATIVE deviation between wallets, which is what the variance components in A0 actually measured:

    tp_i = 25% x exp( w(n_i) x ( mean log-peak of wallet i - mean log-peak of cohort ) )

clamped to **[+15%, +60%]**. The +25% anchor is the funded contract's own level, taken as given
rather than re-derived, which is precisely what removes the circularity in reason 2.

Clamp bounds are unchanged and still derived: **+15%** is the first 5% step clear of the 6.1-point
round-trip cost floor; **+60%** is the observed cohort p90 of +59.6%.

Measured on the 29 wallets with at least 3 observations, this rule yields TPs from **+22.9% to
+29.0%, median +24.7%, with 100% inside the clamp window.** Under the withdrawn EV-argmax rule only
33% landed inside it and two thirds were decided by a clamp bound rather than by evidence — which
A3 forbids, and which is how the error surfaced.

**On what a censored non-reacher should contribute:** the question dissolves under this rule and
does not need a convention. Censored rows enter the wallet's log-peak mean as left-censored
observations exactly as A1 registers, and no EV term exists for them to contribute to. Choosing a
censoring convention to make an unidentifiable formula behave would have been the mirage this
document exists to prevent.

The clamp bounds are not round numbers. **+15%** is the round-trip cost floor: cost is 6.1 points at
15 seconds on the qualifying cohort, so a target below about +12% is inside our own spread, and +15%
is the first 5% step clear of it. **+60%** is the observed p90 of the cohort peak distribution
(+59.6%): a target above the 90th percentile of what the route has ever reached is not a target, it
is a decision never to take profit.

## A3. Shrinkage, not a threshold with clamps

The proposal used a thin-history threshold near 10 measurable closes with clamps so that "a 5-close
wallet cannot freeze an absurd profile". **Replaced by shrinkage**, which achieves the same
protection without an arbitrary cutoff and uses every wallet's data.

Each wallet's log-peak mean is shrunk toward the cohort mean with the James-Stein weight implied by
the measured variance components:

    w(n) = n / (n + s2w/s2b),    s2w/s2b = 5.2

| n | own data | cohort |
|---|---|---|
| 3 | 37% | 63% |
| 5 | 49% | 51% |
| 10 | 66% | 34% |
| 20 | 79% | 21% |
| 50 | 91% | 9% |

A 5-close wallet is automatically half cohort, so it cannot freeze an absurd profile — not because a
clamp forbids it but because its own evidence is weighted at what it is worth. The clamps in A2
remain as a hard backstop, never as the primary control.

**`s2w/s2b = 5.2` is re-estimated at each fit and recorded in every artifact.** It is a property of
the cohort at that moment, not a constant.

## A4. Derisk and trail

Registered: **derisk fraction and trail distance are NOT fitted per-wallet in v1.** They inherit the
funded contract's values.

The proposal was to fit them from the p75/median spread as a tail-runner test. The spread is not
estimable at these sample sizes: a p75 needs far more than the 10 observations most wallets have,
and only **6 of 21** roster wallets carry 10 or more executable-peak observations, **2** carry 20,
and **none** carry 30. Fitting a quantile ratio on 10 points would fit noise and freeze it into a
per-wallet policy for the length of the study.

Coverage of the cohort's forward buy volume if a per-wallet fit required N observations: N=5 covers
72%, N=10 covers 38%, N=20 covers 13%, N=30 covers 0%. Under shrinkage the TP still degrades
gracefully at low n; a quantile ratio does not, which is why it waits for v2.

## A5. The stop, and what the artifact carries

The two constraints "the stop is not fitted in v1" and "the arm resolves policy only from the
artifact" cannot both hold as stated. Registered resolution:

1. The arm resolves the **stop live from the funded resolver at decision time**, never from the
   artifact. Freezing an inherited stop reintroduces exactly the staleness `funded_clone` exists to
   prevent.
2. The builder records `stop_at_fit` in the artifact **for audit only**. It is never read as policy.
3. A divergence between `stop_at_fit` and the live-resolved stop is LOGGED as an observation on the
   position, and does not block. A drifting stop is a fact about the study that its readers need,
   and suppressing it would hide a change in the comparison.

So the artifact is authoritative for the FITTED knobs, and the funded resolver is authoritative for
the inherited ones. That boundary is the registered contract.

## A6. `fit_window_end` is GLOBAL

One value for every artifact: the cohort cutoff. Per-wallet windows would fit each wallet on a
different amount of market, so two artifacts would not be comparable and a difference between them
could be market rather than wallet. Thin wallets stay thin, and A3's shrinkage is what handles them.

The guard the parent document requires — the arm may read nothing newer than `fit_window_end` — is
then a single comparison against a single value.

## A7. Thin, and the fallback

There is **no thin threshold**, because shrinkage removes the need for one. The one hard case is
`n = 0`: a wallet with no prior measurable closes on that chain gets the fallback.

**The fallback is `funded_clone`'s resolved policy for that origin, NOT the band default.** Under the
live all-wallets regime the band default is unreachable on funded, so falling back to it would put
the arm on rails no funded position runs, and the comparison would measure a configuration
difference rather than profile matching. This is the same error the parent document's section 2
identified in the legacy paper policy.

## A8. Chain

One artifact per `(source_user_id, chain)`. A wallet active on two chains gets two artifacts.

A thin chain falls back **independently**, and shrinks toward the cohort mean **on that chain
only**. No borrowing across chains, at any n. Chain-specific is a binding constraint from the parent
document and pooling across chains is the pooling that doctrine forbids.

## A9. The dataset hash covers the INPUT ROWS

`dataset_sha256` is computed over the exact prior-event rows used for the fit: the sorted list of
position ids with, for each, the fields the fit consumed (`entry_price`, `executable_peak_price` or
its censoring bound, `realized_pnl`, `entry_notional`, `chain`). Never the source table state.

A hash over table state changes whenever any unrelated row lands, so a rebuild would look
non-deterministic and a determinism test would be testing the clock rather than the builder. Hashing
the inputs makes the artifact reproducible from its own declared inputs, which is the property the
test needs.

## A10. Artifact schema

Per `(uid, chain)`: `source_user_id`, `chain`, `fit_window_end`, `peak_space="executable"`,
`n_observed`, `n_censored`, `shrinkage_ratio` (s2w/s2b at fit time), `shrinkage_weight`,
`tp_pct`, `derisk_fraction` and `trail_pct` (both inherited, marked `inherited=true`),
`stop_at_fit` (audit only), `fallback` (bool, with reason), `script_sha256`, `dataset_sha256`.

The builder is a committed deterministic script. Re-running it with the same `fit_window_end` and
the same input rows must reproduce every artifact byte for byte, and a test must assert that.

## A11. What would invalidate this rule

1. If `executable_peak_price` coverage rises materially above 63%, the censoring correction weakens
   and A1's 11.9-point gap must be re-measured.
2. If `s2b` falls to zero at re-fit, wallets are indistinguishable on this statistic and the arm has
   nothing to fit — publish that and stop, rather than fitting noise.
3. If the EV-optimal cohort TP moves away from +25%, A2's clamp bounds must be re-derived, since
   +60% is the current p90 and not a constant.

## A12. Ruling on the censored non-reacher, and the amendment it forces

Requested in three ordered parts. Answered in that order, and the derivation was fixed before any
candidate's effect on the numbers was computed.

### (1) What a censored position contributes to EV(t), derived from the censoring model

For position `i` let `P_i` be the true route peak and `Y_i` the realised outcome. Under a candidate
target `t` the position contributes `t` when `P_i >= t` and `Y_i` otherwise. For an OBSERVED row the
indicator is known. For a CENSORED row we know only `P_i >= b_i`, where `b_i = max(Y_i, 0)`.

The contribution is therefore an EXPECTATION over what the bound leaves open:

    b_i >= t   ->  contributes t                        (the bound alone PROVES reach)
    b_i <  t   ->  contributes  q*t + (1-q)*Y_i,  q = P(P >= t | P >= b_i)

**Both proposed shortcuts are wrong, and each is wrong in a way the data refutes.**

*Charging the full realised loss* asserts `q = 0` for every censored row — that "peak unseen" means
"peak low". That is A1's missing-not-at-random error moved one level down, exactly as the builder
suspected, and it is falsified directly: **15 of 171 censored rows (9%) have a bound that already
proves they reached +25%**, and 29% of censored rows are profitable.

*Charging zero* assigns the row no outcome at all. That is not a censoring model; it deletes a real
loss from the average, and it biases upward without bound as `t` rises.

**The censoring is informative but not deterministic**, which is why neither extreme survives.
Missingness varies by exit path — `real_tp` is 10% censored against `mirror` at 76%, `manual` 49%,
`b_stop` 47% — and censored rows run a median realised -17.3% against +19.3% for observed. They are
worse, and they are not uniformly dead.

### (2) Recomputing A0 with the ruled estimator — the registered numbers do NOT reproduce

| t | charge loss | charge zero | **RULED** |
|---|---|---|---|
| +15% | -0.0709 | +0.0241 | **+0.0298** |
| +25% | -0.0620 | +0.0304 | **+0.0334** |
| +50% | -0.0331 | +0.0532 | **+0.0031** |
| +150% | -0.0232 | +0.0539 | **-0.0090** |

The ruled estimator's argmax is **+25%**, and the curve **falls away above it** — so A0's
QUALITATIVE claim survives under a properly derived estimator. **Its NUMBERS do not: +0.0334 against
a registered +0.0964, a factor of three.** The registered figures are amended, not defended. That
the argmax coincides is not evidence the original computation was sound; a mispaired average can
land on the right maximum by accident, and this one did.

Note also that "charge zero" peaks at +100%, which explains why that variant matched at +25% and
diverged at +150% — the builder's diagnosis was right.

### (3) Which variant A0 actually used

A0 used the literal A2 formula — charge the realised outcome to every non-reacher — computed with a
**mispaired zip**: peaks were assembled grouped (all observed, then all censored) while outcomes
stayed in row order, so 60% of rows were scored against another row's outcome. It was not any of the
three columns above. It was none of them, and the number came from the pairing rather than the
estimator.

### (4) The ruling does not restore EV-argmax as the fitting rule

Even with the censoring correctly handled, EV-argmax stays unidentifiable for the three reasons in
A2, none of which concern censoring: half the outcomes were produced by the rails being varied, the
peak histogram spikes at the policy's own trigger, and the clean instrument covers 14%. **The ruled
estimator also inherits a bias of its own** — `q` is estimated from the observed peak distribution,
which is winner-enriched, so every censored row is credited with more reach probability than it has,
and the EV column above is optimistic by an unquantified amount.

**A2's shrunk-offset rule stands and is unaffected by this entire ruling, because it never uses
outcomes.** It reads only the peak distribution, and censored rows enter it as left-censored
observations under A1.

### (5) Shrinkage ratio

The builder's re-estimate of **3.874** at this cutoff is correct behaviour under A3, not a
discrepancy: the addendum's 5.18 was measured on 29 wallets with at least 3 observations, the
builder's on 112 wallet-chains at the cohort cutoff. Use the builder's, record it in every artifact,
and note that it moves the n=5 weight from 49% to 56%. The offset rule absorbs this without change.
