# Runner Cell A500 Forward Preregistration

**Date:** 2026-08-22. **Status:** SEALED before any forward data.
**Origin:** Codex replay study (n=2104 signals, 2026-08-21..22) plus our quoted cross-check
(n=1639 quoted+tracked rows, 7 in-cell fires, 4 of 7 reached >= $40k).

## Question

Does the discovered quality cell hold its winner rate on forward data that the cell
definition never saw?

This study opens no position, signs nothing, and grants no funded authority. The cell is a
paper/observation pattern only.

## Frozen cell definition (primary)

A runner signal is IN CELL when all four conditions hold at signal time:

1. The fired profile includes A.
2. `mom300 >= 500` (percent, from pre-trigger candles).
3. Exact quote-implied entry mcap below $40,000: `1e17 / entry_out_atomic < 40000`.
4. Exact chase in [10%, 15%]: chase = `(1e17 / entry_out_atomic) / mcap_usd - 1`,
   the exact price against the tape price. A signal without a completed entry quote is
   not in the cell and is counted as `no_quote`.

No condition may change during the collection window. The discovery estimates for the
primary outcome (reached >= $40k) were 10/15 = 66.7%; the broader winner rate was 80%
(n=15, chance ~1.1% after a 346-cell search correction). Our cross-check estimate was
4/7 = 57%. All are in-sample.

**Amendment 2026-08-22 (pre-data, zero forward cell rows existed):** an audit found two
defects in the first seal. (a) Condition 3 used the tape mcap; the discovery used the
exact quote-implied mcap `1e17 / entry_out_atomic`. Corrected above; the 7 historical
primary rows are unchanged, the secondary 25-60% chase group changes from 5/6 to 4/5.
(b) The CELL tag rode the tier notify line, and most cell fires tier as mute — the alert
now sends tier-independently. The count restarts: the first countable signal is the
first row after this amendment's commit. The primary comparison anchor is 66.7%, not 80%.

## Collection

The scanner records every fired signal and its quote legs already. Cell membership is
computed from recorded fields at read time (`cell_a500()` implements the same rule at
signal time). An in-cell fire sends a tier-independent observation-only alert, because
most cell fires tier as mute and the tier lines would suppress them. The tag and alert
never gate tier, paper, or notify of other signals.

**Channel note 2026-08-22 (presentation only, count unaffected):** the alert-feed
redesign moved the in-cell line from an immediate message to the periodic research
digest. Membership, recording, and the read are unchanged.

## Read gates

Read after 20 in-cell forward fires or 21 days, whichever comes first. First countable
signal id: the first signal after this file's commit time. Report:

- Primary: share of in-cell signals whose tracked peak mcap reaches >= $40,000, with a
  binomial 95% interval. Compare against the two in-sample estimates.
- Primary support: exact-quote net return at +30 s for in-cell signals (sealed legs).
- `ESTABLISHED-DESCRIPTIVE` if the lower interval bound is above the 8% base exploitable
  rate. `NOT SUPPORTED` if the interval contains the base rate. No middle verdict.

## Pre-declared secondary reads (descriptive only)

1. The same population WITHOUT condition 4 (any chase). Our cross-check showed high-move
   rates of 46% at chase 0-5% (n=39) and 83% at chase 25-60% (n=6) — the chase term may
   be search noise. This read grades whether condition 4 earns its place. It cannot
   modify the primary cell during the window.
2. Tier distribution of in-cell fires (all 7 historical fires were muted by the removed
   chase kill; forward fires should now reach tiers).
3. Peak-class check: share of in-cell winners that later fall >= 60% from peak
   (discovery: 34 of 35). This grades exit design, not entry.

## Shadow cell ACTION-M100 (sealed 2026-08-22, replay-only)

This shadows the EIGHT-condition action path (prefilter plus survival), not the
four-condition A500 cell above. The frozen terms, enumerated so the seal does not
depend on any mutable function:

1. Exact quote-implied entry mcap `1e17 / entry_out_atomic < 100000`
   (the ONLY change from the action rule, which uses $40,000).
2. `mom300 >= 500`.
3. `vol60 >= 8000`.
4. `buyers120 >= 40`.
5. `holder_top5_share < 0.90`.
6. `bundle_max >= 10`.
7. `off_high` in [-50, -5].
8. Survival: `exit15_out_atomic >= 95000000` (95% of the $100 entry value).

A signal missing any input value fails the term. One fire per mint. The forward
cutoff is signal id >= 2892. Historical replay (1,656 finalized signals, both audits
agree): 9 fires, 9 of 9 reached $40k, 6 of 9 reached $100k, zero false fires — double
the runner recall of the action cell. This shadow exists ONLY as a replay rule over
recorded columns: it sends no alert, opens no paper, and changes no runtime behavior.
Miss-reason context (per-condition counts over the 48 missed runners, overlapping):
exact mcap 35, bundle 29, +15 s survival 27, dip range 17, volume 16, buyers 14,
momentum 9, top-five 9. Read the shadow at the same gates as the primary cell, side by
side with the action cell's forward record. Promotion of the $100k ceiling into the
action alert requires that forward read, not this replay.

## Amendment rule

Zero amendments to the primary cell during the window. A defect in the chase formula or
units is fixable only with a dated note here and a restart of the count.
