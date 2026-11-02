# Golden roster refresh — pre-registration

Written 2026-08-28, before any refresh runs. The roster is `data/runner_golden_wallets.json`,
sealed 2026-08-22T19:13:30Z with 290 golden, 126 rug and 3,794 cold wallets, built by
`scripts/research/refresh_golden_wallets.py`.

Parts 1 to 3 are registered here. Part 4, the scalp-relevant label, is HELD — see section 6.

Every figure below is measured on `data/runner_signals.db` and is reproducible from the definitions
in section 1.

## 0. Two findings that change the task before it starts

**The roster has NOT gone stale, and the premise for refreshing it is not supported.** The task was
motivated by a six-day-old one-shot seal in a population where wallets rotate weekly. Graded on
forward coins only — signals after the seal, 4x more data than built the roster — the sealed roster
still works:

| population (coins with buyer data) | reached $40k |
|---|---|
| a golden wallet present | 516/993 = **52.0%** |
| no golden wallet | 400/3927 = **10.2%** |

That is a **5.10x lift six days after the seal**, and it survives a tail-size control. Bucketed by
number of tracked buyers, the lift is 3.38x, 6.95x, 4.22x and 2.86x — present in every bucket, so it
is not an artifact of golden wallets appearing in coins with larger tails.

**Refresh is therefore about ADDING wallets, not about replacing decayed ones.** Section 4 shows
there is currently nothing to demote.

**A measurement note that matters for every rate below.** Only 4,920 of 9,887 forward tracked coins
carry any buyer row at all. A coin with no buyer data cannot show a golden wallet, so including
those coins deflates the comparison. Every rate in this document is computed on buyer-covered coins
only. Against the diluted population the same lift reads 4.45x.

## 1. Definitions, frozen

- **Roster epoch**: a roster's `defined_at`. All grading for that roster uses signals STRICTLY AFTER
  its epoch. A wallet is never graded on the data that admitted it.
- **Outcome**: `mcap_usd * track_peak_x >= 40000`, the sealed rule's chart peak. This is a CHART
  quantity, not an executable one. Section 6 holds the change.
- **Buyer-covered coin**: a forward coin with a tracked peak and at least one `runner_signal_buyers`
  row.
- **Forward base rate**: the share of buyer-covered forward coins reaching $40k. Currently 0.186.
- **Candidate**: any address with at least 3 buyer-covered forward coins.

## 2. The standing refresh rule

1. Run weekly, on a fixed UTC weekday, and never on demand while a decision is pending.
2. Compute the forward base rate on the epoch's forward window.
3. Admit and demote per sections 3 and 4, both under multiplicity control.
4. Write a NEW roster with a new epoch and archive the old one immutably, as the builder already
   does.
5. **No hand-edits, at any hour, under any pressure.** The frozen-rule discipline is what caught
   this week's selection mirages. A roster edited by hand cannot be reproduced from its rule and is
   therefore not evidence.

## 3. Admission — and why the sealed rule cannot simply be re-run

**The sealed rule does not survive a larger candidate pool.** Applying it unchanged to forward data
admits **3,111 wallets**, of which **~1,807 are expected by chance alone — 58%**:

| forward coins | wallets | P(pass by chance) | expected false | actually passed |
|---|---|---|---|---|
| 3 | 5074 | 0.091 | 462 | 974 |
| 4 | 2920 | 0.160 | 467 | 693 |
| 5 | 1800 | 0.235 | 422 | 480 |
| 6+ | 6274 | — | 456 | 964 |

The rule asks for at least 2 wins in at least 3 coins at a 40% hit rate. At the forward base rate a
wallet clears that by chance 9.1% of the time on 3 coins and 23.5% on 5. With 16,068 candidates the
rule is a coin-flip filter. It worked at seal time because the pool was far smaller; it does not
scale, and re-running it weekly would fill the roster with noise.

**Registered admission rule: Benjamini-Hochberg FDR at q = 0.05**, testing each candidate for
OVER-performance against the forward base rate with an exact binomial tail.

| gate | admits | expected false |
|---|---|---|
| sealed rule, re-run | 3111 | ~1807 (58%) |
| **BH-FDR q = 0.05** | **794** | **<= 40 (5%)** |
| BH-FDR q = 0.10 | 1442 | <= 144 |

BH imposes no coin minimum and needs none: a wallet at 3 of 3 has an exact p of 0.0064 and is
genuinely unlikely, while the sealed rule's 2 of 3 has p = 0.091 and is not. The gate requires
EVIDENCE rather than a fixed threshold, which is the property that makes it safe to re-run weekly.

**The candidate count does not reproduce, and that is the finding.** The originating note recorded
~143 candidates. Re-derived under the frozen rule on forward data I get **2,980 new candidates**
under the sealed rule and **794 admissions** under BH q=0.05. Neither is 143. The difference is the
multiplicity effect above, not an error in either count, and it is the reason this section exists.

## 4. Demotion — the machinery, and the fact that it has nothing to do

Each roster wallet with at least 5 forward coins is tested for UNDER-performance against the forward
base rate, exact binomial, one-sided. Multiplicity is controlled with Benjamini-Hochberg at q = 0.10
for a screening decision, with Holm at 0.05 reported beside it.

**Current result, 158 wallets tested:**

| test | demotions |
|---|---|
| uncorrected p < 0.05 | **0** (chance alone predicts ~7.9) |
| BH-FDR q = 0.10 | 0 |
| Holm FWER 0.05 | 0 |

Not one wallet under-performs, and the smallest raw p-value is 0.157. Forward hit rates run p10 13%,
median 60%, p90 100% against a base of 19%. The multiplicity control is registered because it will
be needed later; today it is not the binding constraint, because there is nothing to correct.

62 of 290 roster wallets never reappeared at all. **Absence is not under-performance and must not
demote.** A wallet with no forward coins has no evidence either way and stays until it has some.

## 5. What this rule cannot do

- It cannot detect a wallet that has stopped trading. See the 62 above.
- It grades on a CHART peak, so a wallet that reaches $40k on an unpriceable book scores the same as
  one that reaches it on a tradeable one. That is the gap section 6 exists to close.
- The forward base rate moves with market regime. A roster admitted in a hot week and graded in a
  cold one will look worse than it is. The rule compares each epoch against ITS OWN forward window,
  which controls this, and cross-epoch comparisons are forbidden without restating both base rates.

## 6. Part 4 is RESOLVED: the scalp-relevant label is DROPPED as specced

**Resolved 2026-08-29.** The dense-window probe answered the question this part was waiting on, and
the answer removes its subject. At 1-second sampling the median decisive step is 14.6 points against
15.6 at 3 seconds — 10% of the recovery a continuous move predicts — so the price GAPS and a stop
cannot be enforced at any interval this system can run. Scalp is structurally dead, not merely
unprofitable. See `2026-08-28-scanner-scalp-viability-prereg.md` section 12.

**The label as specced is dropped.** It was defined as relabelling on route-reachable,
priceable-book runs *for scalp*, and scalp is now outside what we trade. A label whose subject has
been removed should not be redefined into the nearest surviving thing; that is how a specification
outlives its reason and quietly measures something nobody asked for.

**What survives, and why it is not a redefinition of this label.** The underlying objection stands:
the roster admits on `mcap_usd * track_peak_x >= 40000`, a CHART peak, and a wallet that reaches
$40k on an unpriceable book scores identically to one that reaches it on a tradeable one. That gap
is real and section 5 still names it.

But closing it requires an executable outcome defined against **the lane that actually consumes the
roster**, and this document cannot name that lane. The scanner pages on golden presence; what
happens to a page after that is a consumer decision I have no measurement of. **An executable label
must be specced against a named consumer with a named outcome, and neither exists in writing today.**
Registering one now would repeat the error this part was held to avoid.

Recommendation: leave the roster grading on the chart peak, keep section 5's limitation stated, and
open a separate specification if and when a consumer with a measurable executable outcome is named.
No build order follows from this section.

## 6b. Original hold (superseded, preserved for the record)

The scalp-relevant label — relabelling on route-reachable, priceable-book runs instead of chart
peaks — is NOT registered here. It waits on the dense-window probe.

The scalp verdict is currently NO at a 3-second interval: realised breakeven 51.4% against an
observed 33.3%. If the 1-second arm shows the pools GAP rather than traverse, then "what we actually
trade" excludes scalp entirely and the label's meaning changes before it is written. Registering it
now would freeze a definition whose subject may not survive the week.

Parts 1 to 3 stand regardless of that outcome, because they concern which wallets predict a $40k
chart peak, which is a well-defined question whatever the execution verdict turns out to be.

## 7. Consumers that must exist

1. A refresh runner that applies sections 3 and 4 and writes the archive. The current builder
   applies neither multiplicity control and cannot be re-run weekly as written.
2. A demotion log, so that a wallet leaving the roster records WHY and under which epoch. Today a
   re-run silently rebuilds from scratch and a departed wallet leaves no trace.

Neither exists. They are named here so the rule cannot silently have no implementation.

## 8. Activation is a separate decision, and the shadow says be careful

Registered admissions land as a **separate v2 artifact beside the sealed v1. They never overwrite
it.** Every downstream consumer was calibrated on the 290-wallet roster, and `golden_buyers` is a
COUNT of roster members in a coin's tail, so growing the roster changes what that number means in
every gate that reads it — `runner_scanner.py:1770` and `:2368` both branch on `golden_buyers > 0`.

**A shadow comparison is required before any swap.** Measured over the 4,923 buyer-covered forward
coins:

| roster | coins firing `golden_buyers > 0` | share of pool | of those, reached $40k |
|---|---|---|---|
| v1 (sealed, 290) | 993 | 20.2% | **52.0%** |
| v2 (BH q=0.05, 794) | 1756 | 35.7% | **43.2%** |
| v1 union v2 | 1939 | 39.4% | 39.6% |

**v2 fires on 77% more coins and predicts worse.** That is the tradeoff in one line, and it is a
finding against this document's own admission proposal. Whether the extra coverage is worth 8.8
points of precision is a decision about what the lane does with a page, not a fact the data settles.

**Two properties make it worse than the table looks.**

First, **v2 is not a superset of v1** — the overlap is only 82 of 290. BH grades on forward evidence,
and 62 v1 wallets never reappeared while others lack the coins to reach significance. So swapping
v1 for v2 DROPS 208 wallets that the seal validated day-split, and it is not a widening, it is a
different roster.

Second, and more important, **the comparison is unfair in v2's favour**. v1 was admitted on pre-seal
data and graded here on forward data, which is out-of-sample. v2 was admitted on the forward window
and graded on that same window, which is in-sample and therefore optimistic. v2 scores 43.2%
in-sample against v1's 52.0% out-of-sample; the honest gap is wider than 8.8 points. **A v2 roster
must be graded on a window AFTER its own epoch before any swap is considered**, exactly as v1 was
here. That grading does not exist yet and cannot exist until a week has passed.

Per-coin counts shift as well, so any consumer thresholding on the COUNT rather than on presence
must be re-derived: non-zero median is 2 under both, but p90 goes 8 to 17 and the maximum goes 25 to
74.

**Recommendation: do not swap.** Build v2, shadow it, and let it earn a forward window of its own.
The swap is the operator's call with these numbers in front of them, and the numbers currently argue
for keeping v1 serving while v2 accumulates out-of-sample evidence.

**Operator-facing reporting rule:** always carry BOTH the 5.10x lift on buyer-covered coins and the
4.45x against the full tracked population. Restricting to buyer-covered coins conditions on
coverage, and coverage may correlate with both golden presence and the outcome. Quoting only the
conditioned number hides that; quoting both shows the lift survives the question either way.
