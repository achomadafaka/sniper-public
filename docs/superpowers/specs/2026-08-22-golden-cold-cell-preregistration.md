# Golden + no-cold-wallet cell — forward pre-registration (2026-08-22)

Status: **PRE-REGISTERED, NOT VALIDATED.** Label and grade only. No page, no gate, no
funded authority. This document is sealed before any forward row is graded.

## The claim under test

A signalled coin whose at-signal buyer tail contains at least one GOLDEN wallet and ZERO
COLD wallets reaches a $40k peak far more often than a golden coin that carries a cold
wallet.

Rosters (both defined on day-1 outcomes only, tested blind on day-2):

- GOLDEN: `>=3` signalled coins, `>=2` reaching a $40k peak, hit rate `>= 40%`.
- COLD: `>=3` signalled coins, `<= 5%` reaching a $40k peak.

COLD is a different question from the existing RUG roster. RUG asks "is this wallet always
in dead coins" (`>=6` coins, `>=95%` peak below 1.25x). COLD asks "does this wallet fail to
appear in coins that run". The distinction is load-bearing: RUG screens out only 14 of 102
golden coins and moves the rate from 35% to 38%, Fisher `p = 0.37` — no effect.

## What was observed (blind day-2, day-1 rosters)

| cell | n | reached $40k | reached $100k |
|---|---:|---:|---:|
| no golden wallet | 993 | 5% | 2% |
| golden, any | 102 | 35% | 20% |
| golden + a cold wallet present | 89 | 28% | 12% |
| golden + ZERO cold wallets | 13 | 85% | 69% |

Fisher exact, cell against golden-with-cold: `p = 0.0001`. Against every non-golden coin:
`p = 2.5e-12`. The result is stable across the cold threshold: `<=0%` through `<=20%` and
minimum-coin cuts of 3 and 4 give 64-88% on $40k and 36-71% on $100k.

Supporting mechanism: of 845 wallets active on `>=3` coins on both days, the day-1 hit rate
correlates 0.458 with the day-2 hit rate. Wallet behaviour persists across days.

## The confound this forward test must resolve

Two placebos were run against the cell. It survives one and does not cleanly survive the
other.

1. Tail size — PASSED. Within tail-size terciles the cell holds at 80% / 80% / 100% against
   27% / 30% / 26%. The correlation between cold-wallet count and tail size is 0.184.
2. Tracked-wallet count — NOT SEPARATED. Selecting the 13% of golden coins with the fewest
   day-1-tracked wallets of any kind reaches 68% / 42%, against the cell's 85% / 69%. The
   cell's median tracked-wallet count is 4; a golden coin with a cold wallet has 11.

So roughly half of the effect may be a FRESH tail rather than a CLEAN one. At `n = 13` the
two cannot be separated. **The forward test exists to separate them.** Every graded row must
record `cold_buyers` and the count of tail wallets carrying any day-1 record, so the cell can
be compared against a tracked-count-matched control.

## Grading rule (fixed now, not after the data arrives)

- Primary metric: share of cell coins reaching a $40k peak.
- Control: golden coins with `cold_buyers > 0`, and a tracked-count-matched golden subset.
- Seal at `n >= 30` cell observations. At the observed rate of about 4 qualifying mints per
  hour that is under one day; do not read the result before `n = 30`.
- CONFIRMED only if the cell beats BOTH controls at `p < 0.01` and exceeds 60% on $40k.
- KILLED if the cell fails to beat the tracked-count-matched control at `p < 0.05`.
- Rows opened before the roster's `defined_at` are contaminated and are excluded.
- The roster must not be refreshed mid-cohort. A refresh starts a new cohort.

## What this does NOT establish

Entry selection is not monetization. Every golden paper arm on record is negative; 50 of 55
runners later fell at least 60% from peak; and 97.7% of even the highest-conviction coins
end dead. A cell where 85% of coins touch $40k is not a cell where 85% of exits pay. The
exit question is separate, unsolved, and is where every measured loss currently lives.

Related: `2026-08-22-golden-lane-design.md`.
