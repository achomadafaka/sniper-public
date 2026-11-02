# Memecoin 60 s time-cap shadow: a measurement-only forward test

Beta, 2026-09-29T22:44Z. Requested by Alpha for the operator (overnight order on 2026-09-29). This
spec changes **nothing** on the money path: no runner code, no config, no new quote. It fixes the
question, the data, the read and the decision rule *before* the cohort exists.

## Question

For every funded Solana and 4663 position, would closing at the first exact whole-balance sell
quote at open + 60 s have returned more than what the position actually returned?

## The data already exists: zero new quotes, zero runtime change

The funded exit rail already quotes the exact whole-balance sell on every sweep. It already
persists that quote in `memecoin_route_series`, seam `exit_decision`. The writer is
`sniper/services/memecoin_route_series.py`: it is buffered, runs outside the decision loop, and
flushes during maintenance. This spec reads those rows and adds nothing to the exit path.

Measured on funded fills from 09-13 to 09-29 (scripts in this commit):

| | Solana | 4663 |
|---|---|---|
| positions (manual entries excluded) | 393 | 126 |
| open at fill + 60 s with no quote in the next 10 s | 6 | 2 |
| first quote after +60 s: lag p50 / p90 | 0.8 s / 1.6 s | 0.8 s / 2.4 s |
| quotes per minute of hold, p50 | 35.4 | 28.2 |
| realised proceeds vs the last quote before close (%, p10 / p50 / p90) | -8.2 / 0.0 / +4.0 | -7.0 / 0.0 / +2.4 |

The quote the shadow uses is the rail's own read of that moment. Real exits land at a median 0.0%
from that same kind of quote.

## What the shadow computes

Implemented in `scripts/research/timecap_shadow_read.py`. It is read-only on the ledger through
`get_readonly_connection`, reads rows by position id through the indexes, and makes no network call.

**T counts from `opened_recorded_at`, the fill**, which is when a live cap would start its clock.
`opened_at` is the decision, on average 2.1 s earlier on Solana and 5.3 s earlier on 4663 (Gamma
post-review, 2026-09-30).

Each position gets one of five statuses:

| status | when | delta |
|---|---|---|
| `closed_before_cap` | the rail closed at or before fill + 60 s | 0: a true zero, the cap changes nothing |
| `measured` | the first `exit_decision` quote in [fill+60 s, fill+70 s] exists | quote `expected_usdc` minus actual proceeds (`entry_notional + realized_pnl`) |
| `unmeasured` | open at fill + 60 s with no quote in that window | absent: counted, never summed |
| `partial_before_cap` | any exit fill (`derisk`, `partial_exit`, `mirror_exit`, ...) before fill + 60 s | absent: the whole-balance quote is not the whole book |
| `no_fill_stamp` | no `opened_recorded_at` | absent: no clock to count from (0 of 519 positions in the retrospective) |

The exit-cost-adjusted delta charges every `measured` shadow exit the cohort's own mean
quote-to-fill gap. That gap is realised proceeds over the last `exit_decision` quote within 10 s
before the real close, computed per chain over positions **closed in a single sell**. A position
closed in several sells has no single gap.

Secondary caps (30, 120 and 300 s) are reported and are descriptive only.

## Retrospective result (prior, not the test)

Window: opened 2026-09-13 to 2026-09-29T22:00Z. Chronological split at 2026-09-22T21:34Z.

| chain | positions | measured | actual PnL | shadow delta at 60 s | exit cost (mean) | delta after exit cost | H1 after cost | H2 after cost |
|---|---|---|---|---|---|---|---|---|
| Solana | 393 | 245 | +$1,601.43 | -$654.85 (-1.09% of notional) | -0.88% | -$973.32 | -$618.87 | -$378.87 |
| 4663 | 126 | 84 | -$196.78 | +$221.43 (+4.20%) | -1.32% | +$176.43 | +$139.27 | +$34.73 |

Exit cost is realised proceeds over the last quote, minus 1; negative means real fills landed below
the quote.

*Updated 2026-09-29T23:07Z (Gamma post-review).* The first version counted T from the decision and
took the exit cost from every close, including multi-sell ones. It read Solana -$238.92 and 4663
+$178.40 after cost. The multi-sell closes had turned Solana's mean exit cost positive (+0.91%). The
direction is unchanged.

**This corrects the money map** (`docs/audits/2026-09-30-entry-side-money-map.md`, section 5b). That
section put a 60 s cap at +$370 / +$309, replayed on chart-space position marks. Mids leave out the
price impact of selling our own whole balance. On the exact quotes, the Solana cap loses in both
halves, and only 4663 gains.

Secondary caps at the measured exit cost:

| cap | Solana | 4663 |
|---|---|---|
| 30 s | -$1,153.43 | +$173.25 |
| 120 s | -$537.02 | +$122.46 |
| 300 s | -$266.20 | +$67.72 |

## Pre-registered forward read

- **Cohort.** Funded Solana and 4663 positions opened at or after 2026-09-30T00:00:00Z: closed,
  `execution_mode='real'`, `signal_source != 'manual'`. Operator-closed positions stay in, because a
  time cap would pre-empt the operator's close too.
- **When.** Once each chain has at least 50 cohort positions. Until then, a check may count
  positions and the unmeasured share, and nothing else: no deltas are read early. After the
  2026-09-29 roster cut, 4663 has 5 armed wallets, so its read may take weeks. Report the fill rate
  at the first check.
- **Command:** `python3 scripts/research/timecap_shadow_read.py --since 2026-09-30T00:00:00+00:00`.
- **Void** on a chain if `unmeasured_share > 0.10`. That means the rail was blind or the route-series
  writer dropped rows, so the shadow would be a sample of the moments we could see.
- **Verdict per chain:**
  - **cap pays**: `shadow_delta_usd > 0`, `shadow_delta_after_exit_cost_usd > 0` and
    `measured_better_share >= 0.5`.
  - **cap costs**: both deltas `< 0`.
  - **inconclusive**: anything else.
- **Consequence.** The verdict goes to the operator. It never changes config. A funded time cap
  needs its own spec, with its own edge-case map: exit latency, the approval inside 4663 exits, and
  interaction with TP/stop/vol_death.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. A read-only script. Nothing runs inside the runner, and no config changes.
2. **Speed on the fill path:** none. The quotes are already taken, and their writer buffers outside
   the decision loop. This spec adds no call, lock or retry anywhere.
3. **Concurrency and global state:** the ledger handle is `get_readonly_connection` (autocommit,
   `query_only`). The script mutates no module state that a runner thread reads.
4. **Absent vs zero vs unknown:**
   - `closed_before_cap` is a real 0.
   - `unmeasured` and `partial_before_cap` are None: counted, not summed.
   - A chain with no close quote has no exit-cost adjustment (None), never 0.
5. **Failure modes of dependencies:**
   - A dropped series buffer or a stopped writer shows up as `unmeasured` and trips the void rule.
   - A wrong quote is bounded by the quote-to-fill spread (p10 -8%). It is charged through the exit
     cost, and both raw and adjusted deltas are reported.
6. **Units and identity:**
   - `expected_usdc` is USD on both chains (it matches realised USD proceeds at median 0.0%).
   - Timestamps are ISO text compared as strings. Every writer uses `+00:00`; a `Z` suffix would
     misorder.
   - Positions are keyed by `memecoin_positions.id`; `network_id` is 'solana' or '4663'.
7. **The sample:**
   - These are our fills from our wallets, so the result describes our copy book, not the market.
   - The forward cohort follows the 09-29 roster cut, so it differs from the retrospective cohort.
   - About 36% of Solana and 32% of 4663 positions close within 60 s of the fill and cannot move
     the result.
8. **Rollback:** nothing is deployed. Deleting the script is the whole rollback.

## Check left behind

`tests/test_timecap_shadow_read.py`, 9 tests:
- a closed-first position is a true zero;
- the cap is priced at the first `exit_decision` quote after it, never at another seam's or an
  earlier one;
- a late quote is unmeasured, not zero;
- any exit fill before the cap (derisk, mirror_exit) removes the position;
- T counts from the fill, not the decision;
- a position without a fill stamp is absent, not zero;
- a multi-sell close gives no exit-cost sample;
- the exit-cost adjustment uses the cohort's own gap.

Mutants, each caught: any seam, a 60 s lag, unmeasured as 0, partials ignored, no exit cost,
closed-first not zero (first version); derisk-only partials, a decision anchor, multi-sell closes
kept (Gamma post-review).
