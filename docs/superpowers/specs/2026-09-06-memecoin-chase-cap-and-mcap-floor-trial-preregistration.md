# Memecoin funded entry: chase cap 50% and source-buy mcap floor 1% — trial preregistration

Date: 2026-09-06. Owner: Alpha (sniper-fc). Operator approval: "You see what the data says, set it
up" (2026-09-06). Status: sealed before activation. Read date: 2026-09-20 (14 days) or 100 funded
closes after activation, whichever comes first.

## Evidence that motivated the change

Funded closes from 2026-08-20 to 2026-09-06 (669 rows with `realized_pnl`).

### Chase: our fill price above the trigger buy price (`fill_gap_pct`)

| bucket | fills | losers | total $ |
|---|---|---|---|
| under 50% | 456 | 40% | +1,158 |
| 50% to 100% | 43 | 60% | −148 |
| over 100% | 35 | 46% | −375 |

The over-50% bucket (78 fills, −$523, median −$2.0) loses in both halves of the period
(late August −$310, September −$213) and on both chains (Solana −$277, 4663 −$246). The 25 to 50%
bucket is positive (+$255, median +$6.3). 135 rows carry no `fill_gap_pct` and are not counted.

### Conviction: the source buy as a share of market cap at the buy

| bucket | fills | losers | median $ | total $ |
|---|---|---|---|---|
| under 100 bps | 93 | 55% | −1.2 | −451 |
| 100 to 500 bps | 98 | 34% | +11.1 | +825 |

Both chains agree on both rows. Coverage: `entry_mcap` is present on 144 of 162 September rows
and 103 of 507 August rows, so this sample is mostly the last two weeks. The paper `funded_clone`
arm (3,355 modeled fills) is flat at 100 to 200 bps and positive at 200 to 500 bps (+$8.1 average,
78 fills). The floor is therefore a trial, not a proven cut.

## The change (`.env`, runner restart on the same release 05ec1505)

| knob | before | after |
|---|---|---|
| `MEMECOIN_REAL_CHASE_MAX_PCT` | 0 (off) | 50 |
| `MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT` | 0 (off) | 50 |
| `MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT` | 0 (off) | 50 |
| `MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO` | 0.0025 (25 bps) | 0.01 (100 bps) |

`MEMECOIN_REAL_MIN_SOURCE_BUY_USD` (249) and the unknown-mcap fallback are unchanged.

## Pre-registered read

At the read date, on funded attempts observed after activation:

1. Chase refusals (`skipped_chase`): count, and their 1-hour follow-through from the feed price
   where a later event exists. Expected: the refused set has a lower average outcome than the
   admitted set. If the refused set shows a positive median follow-through above +25% on 20 or
   more rows, the cap moved the wrong way and is reverted.
2. Mcap-floor refusals (`source_buy_floor` between 25 and 100 bps): count and follow-through, same
   rule.
3. Admitted set: loser rate and average dollars per fill against the pre-activation baseline
   (40% losers, +$1.7 per fill on the under-50% chase bucket; 34% losers, +$8.4 per fill on the
   100 to 500 bps conviction bucket).
4. Fill count per day against the two weeks before activation. The trial expects fewer fills; a
   drop of more than half is reported as a cost, not hidden.

No other entry knob changes during the trial. The measured entry ladder (05ec1505) activated the
same day and is read separately by its own rollout line.

## Rollback

Set the four knobs back to the "before" column and restart the runner on the pinned release.

## What the chase knob measures (recorded before activation)

The evidence bucket above is `fill_gap_pct` = our quote / the trigger buy price. On the EVM leg
the cap compares a different quantity since 2026-09-02: the coin's move since the trader's fill
(display / source), with our route markup judged separately by the premium gate
(`MEMECOIN_REAL_MAX_EXEC_PREMIUM` 2.0x, unchanged). On the Solana leg the cap compares our quote
to the source price directly. On the 96 fills since 2026-09-02 that carry both numbers, the
over-50% rows lose under either quantity: 12 fills −$177 by the EVM quantity, 22 fills −$236 by
`fill_gap_pct`. The read uses the refusal rows the gate actually writes.

## Amendment 1 (2026-09-06, same day, operator "Go ahead"): bundle read

Two more entry changes activate inside the trial window, so the 2026-09-20 read is a verdict on
the BUNDLE, not on any single knob:

- `MEMECOIN_REAL_MAX_EXEC_PREMIUM` 2.0 -> 1.5 (`.env`, runner restart on 05ec1505). Evidence
  (sniper-f0, 861 funded closes, 30 days, ratio = quote / display with the trader price as floor,
  the reference `execution_premium_decision` uses): under 1.15x n=379 +$468; 1.15-1.5x n=149
  +$558; 1.5-2.0x n=36 −$211; 2.0x and above n=8 −$343, zero winners. The cut at 1.5 refuses 44
  fills worth −$554 and touches neither profitable band. In-sample; the 2.0x cell is n=8.
- Premium gate fallback to the trader's price when the chart price is absent (code, builder
  sniper-f0, separate deploy). 289 of the 861 closes had no chart price and passed the premium gate
  unjudged (−$294 net); 155 of them carry a trader price, and at 1.5x the fallback refuses 43 of
  those worth −$349. The EVM measured ladder and the Solana round-trip cap already judge these
  entries on cost since 2026-09-06; the fallback closes the price-reference hole.
- The measured entry ladder (05ec1505) also activated 2026-09-06.

Read additions: count of `execution_premium` refusals by reference basis (chart vs source) and
their 1-hour feed follow-through, same rule as item 1 of the pre-registered read.

## Amendment 2 (2026-09-06, operator): source-buy mcap floor 1.0% -> 0.8%

The morning's cut at 100 bps came from coarse bands (under 100 against 100-500). Re-measured in
fine bands on funded closes since 2026-08-20:

| band | fills | losers | total $ | avg $ | median $ |
|---|---|---|---|---|---|
| 60-80 bps | 24 | 58% | −284 | −11.8 | −12.4 |
| **80-100 bps** | **14** | **43%** | **−24** | **−1.7** | **+2.6** |
| 100-150 bps | 44 | 27% | +374 | +8.5 | +11.3 |

The losing slice is 60-80, not 80-100. The band this change re-admits is roughly break-even on
funded (slightly negative sum, positive median) and mildly positive on the paper `funded_clone`
arm (149 fills, +$279, +$1.87 average). The floor moves to 0.8% and MUST NOT go lower: 0.6% would
pull in the 60-80 band, the worst row in the table on both datasets.

Caveats recorded rather than hidden. The funded sample at 80-100 is 14 fills, enough to say "not
clearly negative" and not enough to say "positive". `entry_mcap` is missing on 480 of 681 funded
rows, so this is mostly the last two weeks. And paper disagrees with funded at 100-150 (paper
−$518, funded +$374), so paper is weak confirmation here, not independent proof.

Volume effect: in the five hours the 1.0% floor was live it refused 7 distinct coins in the
0.8-1.0 band. Of those with a later feed print, two moved about −2% and one moved +63%.

READ IMPACT: this is the third entry knob to move inside the trial window, after the premium cap
and its source fallback. The 2026-09-20 read now describes a bundle of five changes and cannot
attribute an outcome to any single one. That is the accepted cost of shipping them the same week;
it is stated so the read is not later mistaken for a per-knob verdict.
