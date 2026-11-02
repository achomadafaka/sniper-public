# Solana funded exit grid (TP x SL x trail): pre-registration

Date: 2026-10-16. Owner: Lambda. Order: the operator's pick, via Alpha (2026-10-16). Reviewer:
Gamma, before any outcome is read. Status: amendment 2 (Gamma's four blockers and the hardening
asked for on 58cc87c2d; amendment 1 answered the review of df1b3be8f; both are listed at the end);
sealed on Gamma's PASS at 973c12f12 (2026-10-16; this commit adds only the HANDOFF citations and
one known limit, as Gamma asked). Read-only study: no policy, rail, paper or funded state changes. Whatever it
finds goes to paper first (2026-10-01 rule), never straight to funded.

Script: `scripts/research/solana_exit_grid_study.py`. `--cohort` prints identity, coverage and the
exclusion counts only. `--read --out <file>` is the one read, run once, after the PASS; it refuses an
existing output file. Formula pin: `tests/test_solana_exit_grid_study.py`, on synthetic series and a
synthetic ledger only. Every threshold below is a constant at the top of the script.

## Question

On the Solana funded positions we actually held, would another take-profit, stop-loss or trailing
stop have returned more than the policy that ran? The answer is reported per wallet and pooled,
priced at the exact whole-bag exit quote at the moment each rule would have fired. "No arm beats
the control" is a valid answer.

## Prior work, and why its replay is not reused

`sniper/analysis/memecoin_exit_rail_replay.py` (2026-08-26/27) ranked rail variants on the chart
mark series. Its own status line voids those rankings: the chart series does not contain the
realized route-space outcome. It named the route-space series this study uses as the substrate a
rebuild needs, and it fixed the convention kept here: the trigger anchors on our fill, as the live
runner does. Its model, `sniper/analysis/memecoin_rails_model.py`, is not reused: its trail acts
only on the remainder after a partial TP (a whole-bag trail cannot be expressed), it has no
catastrophe floor, and it has none of the live loss-rail mechanics below. The replay here is one
short scan, pinned by the test file.

## Why it can be measured now

`memecoin_route_series` has kept the funded exit path's exact whole-bag sell quotes since
2026-08-26 13:08Z. Rows in the `exit_decision` seam come about once a second per open position on
the read set: the per-position median gap is 1.13 s at the median and 1.85 s at p90. Each row
carries the quote time, the expected output and the route plan. These rows are the price series for
every arm. No chart price and no mid is used anywhere.

## Cohort (frozen)

Solana funded positions (`execution_mode='real'`, `network_id='solana'`) opened in
[2026-08-26T13:08:00Z, 2026-10-16T06:00:00Z). The upper bound is fixed now, so nothing depends on
when the PASS lands. Positions still open at the read are excluded and counted.

**Rules era** (Gamma B2). Only positions opened from 2026-09-05T00:00Z are read. Before that the
live stop mechanics are not known well enough to replay: the elapsed-time stop confirm window was in
use (the last `breach_pending` audit row is 2026-09-03; its value is not recorded), a TP confirm
window was in use at least on 2026-08-30 (`tp_confirm_pending`), and the instant bypass (fda39f3a8,
09-03) and its grace (557c421e4, 09-04) were being added. Earlier positions are counted as
`pre_rules_era` and never read.

**Read set** (Gamma B3): included positions whose as-run contract equals the control (TP 25, stop
35, trail 0, catastrophe 80, max hold 14400 s). On them the simulated control is the rule that ran.
Included positions on another contract are read apart, under the same rules, and decide nothing.

Counts from `--cohort` on 2026-10-16 (identity and coverage only, no exit value):

| step | positions |
|---|---|
| opened in the window | 1223 |
| `pre_rules_era` (opened before 2026-09-05) | -377 |
| other policy (`manual-unmanaged-v1` x38, `exit-trial-v4-v1` x1) | -39 |
| partial exit before the close | -3 |
| coverage `thin` (below 80%, no gap over 60 s) | -114 |
| first gap over 60 s: `quote_miss` | -22 |
| first gap over 60 s: `outage` | -2 |
| included on another contract (TP 10 x16, 35 x14, 24 x6, 30 x5, 20 x4, 15 x2, 17 x2): read apart | -49 |
| **read set** | **617** |

In the read set, 347 positions carry `stop_debounce_sweeps` 1 and 270 carry 2. Eight wallets have at
least 20 positions in it (21 to 60 each); their read shares run from 58% to 92%. No read position
lacks a `source_user_id`.

**Usable quote row**: seam `exit_decision`; `expected_usdc > 0`; quote age
(`observed_at - quoted_at`) at most 5 s. Any other row is treated as absent.

**Coverage**: the share of the position's lifetime (open to close) lying within 5 s after a usable
row. Gaps are counted at both ends (open to first row, last row to close). A position needs coverage
of at least 80% and no gap over 60 s. The exclusion depends on the recording only, not on any arm.

**Exclusion by cause**. Each excluded position is labelled by its first gap over 60 s:
- `stale`: the position's own rows exist inside the gap but are older than 5 s.
- `quote_miss`: the runner was recording (another route-series row of any seam or position, or a
  chart mark for this mint, inside the gap) but this position got no usable quote. This is the
  route-dead case: a dead route, a token failure or a provider fault for this mint. The history
  cannot separate those three; the route-series gap table being built now will, prospectively.
- `outage`: nothing was recorded at all.
- `thin`: coverage below 80% with no gap over 60 s. Thin positions are very short holds (median life
  11.7 s against 95.7 s in the read set).

**Partial exits**: a position with a de-risk fill or more than one exit fill is excluded (the
whole-bag quote stops being the bag). `--read` reports their count and the distribution of their
realized return (exit proceeds over entry notional, minus 1).

**Per-wallet read share**: per wallet, read positions over all of that wallet's positions in the
window, flagged below 50%: such a wallet's result describes a selected subset.

## Control

The live policy as it ran on the read set: `follow-30-50-v2`, TP 25%, stop 35%, no trail, no
de-risk, catastrophe 80%, 4 h maximum hold, with each position's own stop debounce and the live
loss-rail mechanics below. The read reports how often the control fires on the series and how often
it inherits.

## Arms (decided now; the grid does not change after the seal)

- TP in {10, 15, 25, 35, 50, off}
- SL in {20, 35, 50, off}
- trail in {off, 10, 20}

That is 72 arms, including the control (TP 25 / SL 35 / trail off). Every arm keeps the contract's
4 h maximum hold, its 80% catastrophe floor and the position's own loss-rail mechanics: "SL off"
means no stop above the catastrophe floor.

## Rule semantics, priced on the usable quote series

**Trigger** (the live convention): `r_t = unit_price_t / entry_price - 1`
(`alpha_b_real_exit_reason`, `memecoin_real_runner.py`), where `unit_price_t = expected_usdc_t /
open_qty` (`_executable_unit_price`). For a whole bag `open_qty` is the entry quantity and the entry
fill's notional is `entry_price x qty` (true on all 1223 entry fills), so `r_t = expected_usdc_t /
entry_notional - 1`. Fees do not enter the trigger, as they do not live.

- **TP x**: the first usable quote with `r_t >= x%`. No TP confirm window (none after 2026-08-30).
- **Trail z**: arms once the peak `expected_usdc` since open reaches `+z%` over the entry notional.
  It then exits at the first usable quote with `expected_usdc_t <= peak x (1 - z%)`.
- **Max hold**: the first usable quote at or after open + 14400 s.
- **Loss rails, as live** (the funded sweep in `memecoin_real_runner.py`). A quote is a stop read
  when `r_t <= -SL%`, else a catastrophe read when `r_t <= -80%`.
  - Instant bypass: a loss read with `r_t <= -50%` sells on that quote, except a stop read within
    20 s of the fill (`opened_recorded_at`) that is not past the catastrophe floor
    (`MEMECOIN_REAL_STOP_INSTANT_PCT` 50, `MEMECOIN_REAL_STOP_INSTANT_GRACE_S` 20).
  - Otherwise the read needs the position's frozen `stop_debounce_sweeps` N counted hits, and any
    quote that is not a loss read resets the count. On a position opened from 2026-09-27T04:38:38Z
    (the runner restart on release 068dd3e7, which shipped `stop_hit_counts` in 5bc3ad3f2:
    `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md:8708`, "Started 04:38:38Z") hit k+1
    counts only once the breach is k x 0.75 s old; before it every loss read counted.
  - A loss read still pending takes no other exit on that quote (the live sweep moves on).
  - No-loss window: on a position opened before 2026-09-15T16:58Z, a stop or catastrophe read within
    30 s of the fill is no exit at all (`MEMECOIN_REAL_NO_LOSS_SELL_S` 30, from the 2026-09-04
    evening deploy of 9d866c348 until the operator's switch-off at 16:58Z on 09-15, recorded in
    `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md:522`). Evidence: the runner log shows the window deferring position 58219 at "of 30s" on
    09-15; no loss close came within 30 s of its fill from 09-04 17:10Z until 09-18, and such closes
    recur from 09-18 (7.9 s, position 61028).
- Exit value = that quote's `expected_usdc`. One quote meeting several rules exits once; the label
  follows the live order (stop, catastrophe, TP, trail, max hold).

The instant and grace values are the code defaults and the `.env` values now; HANDOFF lists
`MEMECOIN_REAL_STOP_INSTANT_PCT=50` with `MEMECOIN_REAL_STOP_CONFIRM_SECONDS=0` and
`MEMECOIN_REAL_TP_CONFIRM_SECONDS=0` from early September, and no confirm-window audit row exists
after 2026-09-03. Their `.env` history is not otherwise recorded; the read assumes them constant from
2026-09-05.

**Rules this replay does not simulate**: unquotable exit 900 s, volume death, manual or operator
exits, mirror-on-sell, and the dust-route guard on the instant bypass. If the position actually
closed for one of these reasons before an arm fired, the arm inherits.

## The censoring that cannot be avoided

The series stops when the position actually closed. An arm that has not fired by then would still
have been holding, and its outcome after that point is unknown. Primary rule (Gamma B1, R2): the arm
**inherits the quote the as-run contract's replay fires on** (the live decision quote), or the last
usable quote at or before the close when that replay does not fire. Every value in an
arm-minus-control difference is on the quote basis, and an arm that inherits the control's own exit
differs from it by exactly zero, even where quotes taken while the position sold follow it. The quote-against-fill gap is reported in the validity check as the
execution haircut and never enters an arm.

For every arm the read reports the share of positions inherited. An arm with more than 50%
inherited (in the set being analysed) is **not identifiable**: it is mostly the control by
construction, and it takes no part in the Holm family. Looser TP or SL arms are expected to land
there. The study cannot say whether they pay. That is a stated limit, not a result.

## Outcome

**Arm-minus-control difference** (the primary quantity), per position:
`(exit_value(arm) - exit_value(control)) / entry_notional`. It uses no fee, so a missing receipt
can never change it.

**Absolute PnL** per position and arm:
`pnl_pct = (exit_value - exit_native_usd) / (entry_notional + entry_native_usd) - 1`.
- `entry_native_usd` and `exit_native_usd`: the entry and exit transactions' `meta.fee` plus every
  lamport each moved into a Helius Sender or Nozomi tip account (`solana_sender_ab.TIP_ACCOUNTS`,
  `NOZOMI_TIP_ACCOUNTS`), read from the receipts. Tips are SOL transfers outside `meta.fee`.
- Receipts are read on the research Helius key through `funded_exit_truth.rpc` (shared pacer; stops
  on a third rate 429, a quota 429 or any other HTTP error), never on a funded lane, and only after
  the validity check passes. `memecoin_fills` cannot supply this: its fee columns are set on 86 of
  1223 entry fills and on no exit fill.
- SOL/USD: the median, in the fill's UTC hour, of the wSOL-to-USDC legs inside the cohort's own
  whole-bag quotes (nearest hour within 6 h otherwise).
- Rent is not counted: the token-account rent paid at entry comes back when the exit closes the
  account.
- A missing receipt or price leaves that position's absolute PnL unknown (counted), never zero.

## Analysis

**Validity check first**, on every included position (read set and other contracts): each
position's as-run contract (its own TP, stop, trail, catastrophe, maximum hold and stop debounce,
with the loss-rail mechanics above) is replayed on its series and compared with what actually
happened, for positions that closed on a simulated rail (`real_tp`, `b_stop`, `catastrophe`,
`b_timecap`, `real_trail`).
- Reported per rail and overall: the relative error `|replay value - realized proceeds| / realized
  proceeds` (median, p10, p90), its signed median (the execution haircut), the absolute exit-time
  difference, the share of matching exit reasons, the count where the replay never fired, and the
  count where the replay fired before a non-rail close.
- If the overall median relative error exceeds 5%, the read stops there, writes the validity block
  only and prices no arm.
- If the stop rail's own median relative error exceeds 5%, the SL dimension is not read: only arms
  with SL 35 are analysed, and the output says so.

**Primary, per wallet** (the 2026-10-02 operator rule: judge each wallet alone):
- Covers wallets with at least 20 positions in the read set.
- For each identifiable arm, the mean paired difference with a 95% percentile CI from a bootstrap
  over positions (10,000 resamples, numpy `default_rng(20261016)`, drawn in a fixed order).
- One-sided p-value for "arm beats control": `(k + 1) / 10,001`, with `k` the resamples whose mean
  is **at or below zero** (Gamma B4). A tie (every difference zero) gives p = 1.
- Holm step-down at 0.025 across that wallet's identifiable arms: the one-sided equivalent of a
  95% lower bound above zero.
- An arm **beats the control for a wallet** iff Holm rejects for it and its mean difference is
  positive. Report n and the arm.

**Pooled, secondary**: the same over the read set, cluster bootstrap by wallet, Holm across arms.

**Read apart, no decision**: the included positions on another contract, pooled, under the same
rules (their simulated 25/35 control is not the rule that ran).

**Sensitivity reads** (no decision is taken on them):
- Fired-only: per wallet and pooled, each arm over the positions where it fired before the actual
  close (no inheritance).
- Dead route kept: the read set plus every control-contract `quote_miss` and `stale` position. Each
  added position's series is cut at the start of its first gap over 60 s, keeping the quote at the
  cut. An arm that fires on a usable quote up to the cut prices there; otherwise it inherits the
  realized proceeds, because no quote exists at that close. This read is therefore mixed-basis by
  necessity. Pooled and per wallet, with the same Holm rule.
- Thin kept: the read set plus every control-contract `thin` position, priced on its sparse series
  with the primary inheritance rule. Pooled and per wallet, with the same Holm rule.
- `outage` positions stay out of every read.

Reported for every arm: n, the mean and median absolute `pnl_pct`, the paired difference with CI
and p, the inherited share and identifiability.

## No peeking

- This document, the arm grid, the thresholds (rules era 2026-09-05, 80% coverage, 60 s gap, 5 s
  quote age, 20 positions, 50% inheritance, 50% read share, 5% validity overall and per stop rail,
  0.025 one-sided) and the script are fixed at Gamma's PASS.
- `--cohort` may run before the seal; it reads no exit value.
- `--read` runs once, on the frozen cohort. A second run with changed rules is a new study with its
  own pre-registration.

## What a result authorizes

Nothing on funded. An arm that beats the control for a wallet becomes a candidate for a paper arm
on that wallet (2026-10-01 rule). The paper result decides any later funded change, with the
operator's yes.

## Known limits

- Inheritance makes looser rules non-identifiable (above).
- The read set starts 2026-09-05 and leaves out short holds (`thin`); the read reports what left it,
  by cause, and the thin-kept sensitivity prices the short holds.
- The quote cadence (~1 s) bounds trigger precision. A spike between quotes is invisible to every
  arm alike.
- Whole-bag quotes measure an all-at-once exit. A real exit can fill differently; that gap is the
  validity check's error and signed haircut.
- Fees count only landed transactions: failed or reverted attempts are not charged. Every arm is
  charged the actual exit transaction's fee and SOL/USD hour, whenever it would have sold.
- The loss-rail knob values are assumed constant from 2026-09-05 (above).
- Arms that have not fired at the as-run decision keep scanning the 1-2 s of quotes recorded while
  that exit was in flight.
- The cohort is our own fills: copy-trade entries that passed our gates. It describes our book,
  not the market.

## Amendment 2 (2026-10-16), before any outcome read

Gamma's blockers on 58cc87c2d:
- B1. An arm that does not fire inherits the last usable quote at or before the close, not the
  realized proceeds; the paired difference is on one basis. Validity adds the signed haircut.
- B2. Each position's frozen stop debounce, the instant bypass at -50% with its 20 s grace, the
  0.75 s hit spacing and the 30 s no-loss window (09-05 to 09-15T16:58Z, found while checking the
  knob history) are simulated in every arm and in validity; validity is reported per rail; the SL
  dimension is read only if the stop rail's replay passes. Positions before 2026-09-05, when confirm
  windows of unrecorded length were in use, are not read.
- B3. The read set is the included positions whose as-run contract equals the control; the others
  are read apart; the control's fired share is reported.
- B4. The p-value counts resamples at or below zero, as the code always did.
- R1. The 0.75 s hit spacing applies only to positions opened from its deploy
  (2026-09-27T04:38:38Z); earlier ones count every loss read.
- R2. An unfired arm inherits the as-run replay's fire quote when there is one.

Gamma's hardening: a synthetic-ledger test of `load()` (causes, quote age, partial exits, the
dead-route cut), and pure tests for trail arming, wallet clustering and the contract's maximum hold.
Added: the thin-kept sensitivity, and two known limits (landed-only fees, one exit fee per arm).

## Amendment 1 (2026-10-16), before any outcome read

Gamma's conditions on df1b3be8f:
1. Exclusions by cause (`stale` / `quote_miss` / `outage` / `thin`) and the dead-route-kept
   sensitivity read.
2. Partial exits: count and realized-return distribution in `--read`.
3. The validity check replays each position's as-run contract; the 25/35 control stays for the
   arm comparisons.
4. Absolute PnL carries entry and exit tips, read from the receipts.
5. Per-wallet included share, flagged below 50%.

Lambda corrections, accepted by Gamma:
6. Trigger on the live convention (`unit_price / entry_price`, equal to `expected_usdc /
   entry_notional` for a whole bag), not on a fee-inclusive cost. Fees move to the absolute PnL
   only; the arm-minus-control difference uses the entry notional and no fee.
7. Every arm keeps the contract's 80% catastrophe floor, as live.
8. Fees come from receipts, not `memecoin_fills`. The cohort's upper bound is fixed at
   2026-10-16T06:00Z.
