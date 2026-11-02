# Execution-premium threshold — pre-registration

Date: 2026-09-02
Author: Gamma (measurement desk)
Status: PRE-REGISTERED. Written and committed BEFORE the outcome data was read.
Requested by: Alpha, with three conditions, all adopted below.

## Why this is pre-registered

`MEMECOIN_REAL_MAX_EXEC_PREMIUM=1.95` came from a cliff observed in-sample, where n=18
decided the top cell. Half our exits run on rails, so outcome data is partly produced by
the rails themselves. If the outcome data selects the threshold after I look at it, the
rail is fitted to the rails. The cut is therefore fixed here, in advance.

## The question

At what quote-to-reference premium does the realized outcome of a funded position
deteriorate, measured against BOTH references?

    reference A  display   = classification_json.facts.display_price   (what the gate uses)
    reference B  executed  = memecoin_entry_attempts.source_price      (the trader's fill)

## A STRUCTURAL LIMIT, STATED BEFORE THE RESULT

Reference A is TRUNCATED AT THE THRESHOLD BY CONSTRUCTION. A position exists only where
the gate permitted entry, so on the display reference almost no position can carry a
premium above 1.95. The exception is a fail-open, which happens when `display_price` is
ABSENT — and those rows then have no display price to measure retrospectively.

So reference A cannot locate a cliff above 1.95. It can only describe the region below it.
Reference B is not truncated, because no gate has ever used it.

I expect this to be the finding rather than a nuisance. It is recorded here so that it
cannot be presented later as a discovery.

## Population

1. `memecoin_positions` where `execution_mode='real'`, `closed_at` is not null,
   `realized_pnl` is not null, and `entry_notional > 0`.
2. Joined to `memecoin_entry_attempts` on `attempt_id`.
3. Both `quote_price > 0` and the reference price `> 0`.
4. No date restriction. The window is whatever the funded book holds; I report its span
   and n rather than choosing it.

Open positions are EXCLUDED. That censors the sample toward resolved outcomes.

## Metric

`roi = realized_pnl / entry_notional * 100`.

MEDIAN roi, never the mean. Memecoin PnL is fat-tailed and a mean is decided by one trade.
Win rate and n are reported beside every median.

## The fixed grid

Premium cuts evaluated, and no others:

    1.15   1.30   1.50   1.75   1.95   2.25   2.50

Bands reported: <1.15, 1.15-1.50, 1.50-2.00, 2.00-3.00, >=3.00. The first three match the
2026-08-23 study exactly so the two are comparable; the top is split because that study
had n=18 in one cell.

At each cut: median roi below, median roi above, win rate each side, n each side, and a
6000-sample bootstrap 95% CI on (median roi below − median roi above).

Seven cuts are seven looks. I report all seven, always, and treat any single separating
cut as weak unless neighbouring cuts agree.

## Minimum n

- A band with n < 15 is reported but never used to justify a threshold.
- A candidate cut requires n >= 30 ABOVE the cut. Below that, "not decidable" is the
  result.

## The decision rule, fixed in advance

**Recommend a change to 1.95 only if ALL of these hold:**

1. A cut on reference B (executed) has a bootstrap 95% CI excluding zero, in the direction
   of worse outcomes above the cut.
2. n above that cut is at least 30.
3. At least one neighbouring cut on the grid points the same way.
4. Reference A agrees within one band, OR reference A is truncated at that cut and
   therefore cannot speak — in which case the disagreement is not evidence against.

**Leave 1.95 alone if ANY of these hold:**

1. No cut separates on reference B.
2. The separating cut has n < 30 above it.
3. The two references disagree by more than one band while both are able to speak.
4. Only one isolated cut separates and its neighbours do not.

**A number produced here is a recommendation to the operator, never a deploy.** Alpha has
stated they will not change a funded premium gate on an inferred threshold, and I agree.

## What I will not do

- I will not add a cut to the grid after seeing the data.
- I will not drop a band because it is inconvenient.
- I will not report a mean roi anywhere.
- I will not convert a point estimate into a recommendation when the interval spans zero.

## Known confound, stated in advance

Exit rails produce part of the outcome. A position entered at a high premium may exit worse
because the premium is real, or because a rail fires differently on it. This design cannot
separate those. Any recommendation carries that limit explicitly.
