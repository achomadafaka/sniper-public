# Impact-aware chase cap — how its admissions get read

Registered 2026-08-29, before the first graded row. The rule: when the chase gate would refuse, one
exact quote at the SOURCE'S size estimates their own price impact and extends the cap by it, clamped.
Attempt rows carry naive premium, estimated source impact, effective cap, and decider.

This document registers only the READ. It does not evaluate the rule.

## 0. Formula correction before the first graded row

The first implementation treated `priceImpactPct` as the final pool-price move. It divided that
value by two. The provider contract defines it as the lost input-value fraction.

Formula v2 converts the loss fraction `i` to the paid-price premium: `i / (1 - i)`. The runner uses
this estimate only for Solana. Other chains keep the base chase cap and make no Jupiter request.

The production ledger had zero formula-v1 rows when this correction started. Thus, the correction
does not mix two formulas in the registered cohort. The formula identifier remains in each row.

Provider contract: [Jupiter Metis quote guide](https://developers.jup.ag/docs/guides/how-to-build-a-custom-swap-with-metis#step-1-get-a-quote).

## 1. The prior that threatens this rule, stated before any result

The premium predicts the outcome, monotonically, on 443 funded closes:

| our entry / their price | n | mean return | median |
|---|---|---|---|
| < 1.0x (we paid BELOW them) | 73 | +1.8% | **+22.4%** |
| 1.0 - 1.25x | 278 | +0.2% | +0.7% |
| 1.25 - 2.0x | 80 | -6.8% | -3.3% |
| >= 2.0x | 12 | **-35.1%** | **-63.4%** |

**The rule admits entries at a higher naive premium than the old gate allowed, and this gradient says
those lose.** The rule's defence is that part of the premium is the source's own footprint rather
than the coin running away — a real distinction, and a HYPOTHESIS that this table does not support
or refute.

**So the primary read is not "did adjusted admissions make money". It is "did adjusted admissions
ESCAPE this gradient".** Grade each newly admitted entry against the band its NAIVE premium falls
in, using the table above as a pre-specified benchmark fixed before the rule shipped. Beating the
band is the claim; matching it means the decomposition bought nothing.

## 2. The cohort — and why the obvious control is the wrong one

Newly admitted entries are a **new cohort**: the old gate refused them. They must never be pooled
with pre-change entries, which were admitted under a different rule.

**The refused-and-stayed-refused population is a confounded control.** Those coins are still refused
because their premium exceeds even the EXTENDED cap, so they sit at systematically higher premium by
construction. Comparing admitted against them compares two different premium levels and attributes
the difference to the adjustment.

Two comparisons that are not confounded:

1. **PRIMARY — the pre-specified gradient of section 1.** Each admission is scored against its own
   naive-premium band. No control population needed, and the benchmark cannot drift because it was
   fixed here.
2. **SECONDARY — retrospective recomputation.** For historical `chase_*` refusals in the capture
   lane, recompute what the extended cap WOULD have been (`their_size_usd` is populated on 467 of
   469 funded closes, so the model's input exists) and grade the would-have-been-admitted subset on
   the forward price path the capture lane already records. That is a matched pre-change control for
   the same admission region, which the still-refused population is not.

## 3. `est_impact` is a model output, and a model of the WRONG MOMENT

It must never be read as recorded slippage. Two separate reasons, and the second is the larger:

1. It is constant-product route math, not an observation of the source's fill. Nothing in it was
   measured.
2. **It is computed on the CURRENT book, and the source traded on an EARLIER one.** The premium
   exists precisely because time passed and the book changed, so estimating their impact against
   today's liquidity misestimates what they actually paid. **The sign of that error is unknown** and
   must not be assumed conservative.

The column name and the doc must both say model. If a later reader needs the source's true impact,
it is not in this data and no column here is a substitute.

## 4. Stratification

Reads are stratified by `decider` — never pooled across gates. An admission the impact adjustment
decided is a different object from one another gate let through, and the whole question is what the
adjustment did.

## 5. Kill

Stop and revert the extension if, at n >= 40 adjusted admissions, their mean return does not beat
the section 1 band benchmark for their naive premium. Matching the benchmark is a failure, not a
neutral result: it means the decomposition identified nothing and the cap was simply loosened.

## 6. What this cannot answer

The 08-23 decomposition held that on nano coins the premium partly reads OUR route cost rather than
market movement. That component is still not separated here — this rule decomposes a THIRD component
(the source's own impact), and the read above cannot tell the two apart. A result that beats the
benchmark shows the extension helped; it does not show WHICH component the extension actually
removed.

`entry_mcap` coverage is currently too thin to re-verify the size-band claim on funded closes: only
the >$100k band carries n >= 5. The nano-coin premise behind this rule rests on the 08-23 study, not
on anything reproducible from the funded ledger today.
