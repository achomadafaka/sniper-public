# Coin-type fast-pump cohort — pre-registration

**Status:** measurement only. No funded authority, no roster change, no exit-policy change.
**Version:** v4, superseding v3, v2 and v1 of the same date. **The v3 cohort definition was
invalid**: it counted prior buyers by `created_at`, the trader's own buy time, so an event created
early but received late counted before we had it — and the count ignored `network_id`, letting the
same mint on another chain contribute buyers. v4 counts distinct wallets RECEIVED before this
signal, on this chain. The cell falls from 10 mints to **6**. No v3 episode was ever captured, so
no forward evidence needs removing.
**The v2 freeze claim was FALSE.** One coin-filter episode was captured at 10:21:31 EDT, before
the v2 worker restart at 10:25:01 EDT, and no row carried a version. That episode is labelled
`pilot-pre-v2` and is EXCLUDED from every verdict. `spec_version` is now stamped on every
scheduled episode, so a cohort can never again be ambiguous about which freeze it ran under.
The v3 forward window begins with the first episode stamped `coin-type-fast-pump-v3`.
**Frozen:** 2026-08-11. Everything below is fixed BEFORE the forward data exists.

## v2 corrections (all found by adversarial review, all reproduced)

1. **The retrospective lift was inflated by clustering.** v1 reported 3.4x on "512 episodes".
   Those were `(wallet, mint)` rows: one coin bought by six wallets counted six times, with the
   same price path each time. Clustered by MINT the cell holds **36-170 distinct coins** and the
   lift falls to **1.27x-1.90x** depending on the mark-freshness bound. Two independent reruns
   disagree within that range, so the effect is not stable to reasonable analysis choices. The
   retrospective case for this cohort is WEAK, not strong.
2. **`real_decision_at` is required only for roster wallets.** The feed is global (6,446 wallets,
   221 on the roster) and the column is set exclusively for roster wallets, so requiring it
   everywhere restricted a coin cohort to a 221-wallet sample. Waived for off-roster wallets,
   which the funded path can never touch.
3. **The clock was wrong.** 590/week counted sub-$1M signals BEFORE the 4-8 buyer filter. After
   the full filter it is 125 signals and **47 distinct mints per week**, so 30 distinct coins take
   about **4.5 days**, not one.
4. **Entry is not immediate.** Discovery polls, so the entry quote lands 0-`DISCOVER_EVERY_S`
   after the signal (now 3s, was 30s). The first captured episode measured source->quote at
   13.3s. Every result carries `delay_source_to_quote_ms`; no analysis may treat the captured
   entry as the reachable price at signal time.

<!-- schema:external=copy_quote_schedule -->
<!-- schema:external=copy_quote_result -->

## Why this exists

Wallet-level screens ran out of power. The best account cell had 8–22 coins, and after Holm
correction across 98 wallet-chains, **zero** wallets cleared any bar. Moving the question from
*who bought* to *what they bought* raises n by two orders of magnitude.

## The finding this tests

Measured from the 60s mark so the buyer's own price impact is excluded. **The unit of observation
is the MINT.** A `(wallet, mint)` unit counts one coin once per wallet that bought it, with an
identical price path each time — that inflation produced v1's headline and is the same clustering
error found earlier in the wallet screens.

Frozen `as_of = 2026-08-11T14:00:00Z`, signals and marks both bounded by it, solana, first buys,
earliest qualifying signal per mint. Reproduce with `scripts/coin_type_lift_artifact.py`.

The claim has now been computed five ways and produced five answers:

| revision | unit | consensus | marks | cell | lift | p |
|---|---|---|---|---|---|---|
| v1 published | signal | +60s (leaky) | unfiltered | "512" | **3.40×** | — |
| v1, sub-$1M base | signal | +60s | unfiltered | 190 | 1.64× | — |
| v2 published | mint | +60s (leaky) | as_of only | 36 | 1.90× | 0.052 binom |
| independent rerun | mint | +60s | looser | 170 | 1.27× | — |
| v3 | mint | strict, but by trade time | fresh | 10 | 2.71× | 0.037 hyper |
| **v4 authoritative** | **mint** | **receipt time, chain-scoped** | **fresh** | **6** | **3.38×** | **0.040 hyper** |

**Each correction shrank the sample faster than it sharpened the estimate. The authoritative cell
is SIX COINS with three hits.** A statistic recomputed six ways across defensible analysis choices,
whose correct form rests on six observations of a fat-tailed outcome, is not evidence of anything.

**The coin-type effect is NOT ESTIMABLE from this tape.** It is neither established nor refuted.
That is the honest state, and it is the reason to capture rather than to trade.

Both inputs are known before the buy. `prior_buyers` counts distinct wallets STRICTLY BEFORE the
signal; counting later arrivals would read the coin's own outcome, which is the leak that
retracted `creator_prior` (+1,453 bps → +392, t=0.70).

## Why it is NOT yet an edge

Under its own best take-profit and stop pair the cohort **loses**:

* best cell TP +50% / stop −10%: **−0.97%/coin**, 90% CI [−2.68, +0.74], P(>0) = 18%
* every one of 20 TP×stop cells is negative
* time split, rule fixed: first half **+1.23%**, second half **−3.16%**
* buy and hold 5 minutes: −1.50%/coin

The retrospective verdict is therefore NEGATIVE. Two things it rests on are exactly what this
capture replaces:

1. **a modelled 450 bps round-trip cost** — never measured for these specific coins at $50; and
2. **four-point paths** (300/900/3600/21600s) — so first-touch ordering between a take-profit and
   a stop is guessed, and that ordering is the entire result.

## Frozen rule

```
COHORT     first buy, solana. real_decision_at IS NOT NULL required for ROSTER wallets only
           (the column is never set off-roster; requiring it everywhere sampled 221 of 6,446)
FILTER     market cap at signal < $1,000,000
           AND 4 <= distinct wallets whose buy of this mint ON THIS CHAIN we had RECEIVED
               strictly before this signal <= 8   (receipt time, never trade time)
ENTRY      executable $50 quote, taken after the funded hook decides for roster wallets;
           immediately for off-roster wallets, which the funded path never touches.
           NOT immediate in either case - discovery polls; the true delay rides on every row.
EXITS      exact entry quantity, quoted at 5,10,...,90s then 120s and 300s
PRIMARY    FAST_MFE90 = max executable exit value within 90s, vs the entry quote
SECONDARY  time to peak; drawdown from peak; first-touch outcome of TP+50/stop-10
COST       measured from the quotes themselves. Do NOT subtract a further 450 bps —
           executable quotes already carry route fees and price impact. Add only gas,
           priority fee and submission cost, which the quotes do not include.
```

## Verdict rule, fixed now

* **Minimum evidence:** 30 distinct coins with a successful entry quote and ≥ 80% of their exit
  legs recorded.
* **PASS:** mean net-of-cost outcome of the frozen TP/stop pair is positive with a 90% coin-block
  bootstrap interval excluding zero, AND the first and second halves of the forward window agree
  in sign.
* **FAIL:** anything else, including a positive point estimate whose interval spans zero.
* No threshold, filter bound, horizon or exit rule may be changed during the forward window. A
  change ends the run and starts a new one.
* **A PASS does not grant funded authority.** It qualifies the rule for a paper arm.

## Expected clock

**47 distinct qualifying mints per week** (125 signals collapsing to 47 coins), so 30 distinct
coins take about **4.5 days**. Quote volume stays far under the 3,000/day ceiling.

## What would falsify the finding itself

If measured executable entry and exit costs on these coins exceed the modelled 450 bps, the
cohort's negative retrospective verdict is confirmed and strengthened, and the coin-type
direction should be closed rather than re-tuned.
