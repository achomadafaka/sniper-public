# Solana chase-cap cost study: pre-registration

Date: 2026-10-07. Owner: Lambda. Approval: operator yes via Alpha (task fe513bdd). Reviewer:
Gamma (task ca5a85c7), before any outcome is read. Status: sealed on Gamma's PASS. Read-only.

Revision 2 (same day, before any outcome read): Gamma's CHANGES REQUIRED on 8259242e8 and Alpha's
ruling for option (a).
- The knob also gates pre-sign quote refusals, so each buy is now valued over both gates (section
  "One knob, two gates").
- The robustness checks are added: median, and the mean with the best buy dropped.
- The bag-ratio column is added.

Script: `scripts/research/chase_cap_cost_study.py` (`--cohort` prints identity and coverage
only; `--read` is the one read). Formula pin: `tests/test_chase_cap_cost_study.py`.

## Question

What would a looser chase cap cost or earn? The operator asked: how many missed buys would fill if
the cap were 5, 10 or 20 points looser, and what would those fills have returned 60 s and 5 min
later, after network fees?

One knob moves two gates, so both are in scope:
- the slot lane's signed price floor;
- the pre-sign exact-quote gate (`solana_swap_executor.py:1336`), which reads the same cap through
  `solana_wallet_chase_cap_pct`.

## Cohort (frozen, n = 29)

Solana source buys from 2026-10-04T00:00Z to 2026-10-07T12:38Z where:

- the slot lane sent the buy and never filled it;
- the source buy has no policy refusal (same rule as the bottleneck read in finding 41ce21e8);
- the last slot-lane attempt failed on chain with a cap-type reason:
  - `BuySlippageBelowMinTokensOut` (pump curve 6042)
  - `BuySlippageBelowMinBaseAmountOut` (PumpSwap 6040)
  - `ExceededSlippage` (Raydium)
  - `slippage tolerance exceeded (Jupiter 0x1771)`

Fate: **late_fallback** when another fast lane later filled the same source buy, else
**missed**. The result is 23 missed and 6 late fallbacks.

**Difference from the 33 in the order.** The order's 33 counted every on-chain failure. 4 of them
are not cap failures, so a looser cap cannot fill them, and they are excluded:
`BondingCurveComplete` (3 rows) and `NotEnoughTokensToBuy` (1 row).

The full list is in the appendix. The cohort does not grow. A later row does not join this read.

## One knob, two gates (revision 2)

Pre-sign refusals in the same window take two forms:
- `exact quote is +X% above source price (cap Y%)`;
- `coin already ran +X% past their entry (cap Y%)`.

`j7_shadow` decisions are excluded because they spend nothing. The remaining refusals fall into
two groups, and both are frozen.

1. **Fallback legs of cohort buys.**
   - 25 quote refusals on 23 of the 29 cohort buys. In every case the slot send failed on chain,
     then the on-chain fallback was refused at the quote gate.
   - 10 of those buys have a refusal within +20 points: 9 missed and 1 late fallback (appendix B).
   - These are the same buys as the cohort, so they are not a second cohort. Counting them
     separately would count one buy twice.
2. **Independent pre-sign refusals.** These are buys outside the cohort that never filled.
   - There are 8, and all are over +20 points (+30 to +95; appendix C).
   - They cannot gain at any tested `t`, so they are listed, not valued.

Gamma's count of 13 rows within reach consists of 12 fallback-leg attempts from group 1 and 1
`j7_shadow` refusal.

**The outcome per buy at `(variant, t)`**, in time order:
1. **The slot leg fills** when `need_pp <= t`.
2. **Else the earliest fallback refusal fills** when its exact `gap_pct` is within the lifted cap.
   - The exact `gap_pct` and `cap_pct` come from that attempt's last `chase_quote_confirmation`
     substage, not from the rounded reason string.
   - Lifted cap, variant A: `min(cap + t, 35)`. Variant B: `cap + t`.
3. **Else the buy stays missed.**

A slot leg censored as `landing_unknown` cannot gain by the slot leg; its fallback leg still can.

**How a fallback fill is valued:**
- Entry is our exact refused quote (`quote_price`, USD per token, at the clip).
- The anchor is that attempt's `completed_at`.
- The sell series is every ok exact-route Jupiter sell quote captured on this buy's origins
  (`slotlane:<sig>` or `onchain:<sig>`, any gate). The nearest observation within ±15 s or ±30 s
  is used.
- Fee: the cohort's median slot round-trip fee in percent. There is no failed transaction on this
  leg to read a fee from.

**Caveat.** The quote path also runs `watched_gate` against the source price with the hard cap.
That gate is not recorded on these refusals. If it would bind after loosening, the fallback gains
are an upper bound.

**Caveat (Gamma, PASS on revision 2).** On a fallback leg, every gate after the chase-quote gate
never ran: the impact cap, the round-trip budget, `post_peak_confirmed`, safety, the reservation
and capacity. Fallback gains are therefore an upper bound, as with `watched_gate`.

**Scope note (Gamma).** `QUOTE_REFUSAL` matches only `exact quote is ...` lines, so
`coin already ran +X% past their entry (cap Y%)` refusals are never legs. In this window none falls
on a cohort buy within +20 points; the only reachable one was `j7_shadow`. The results do not move.
The optional fallback bag ratio is not reported, because the fallback attempt's ticket is not
stored.

## Instruments (local data only; no DexScreener anywhere)

1. **Landing.** Pump curve 6042 and PumpSwap 6040 failures log the program's own `Left` (tokens
   the spend would have bought when it executed) and `Right` (the signed minimum). The trace
   records them in substage `slot_pump_slippage` (`solana_slot_executor.pump_slippage_failure`).
   - Check: `Right` must equal the `min_tokens_atomic` we signed (substage `slot_price_limit`).
   - Rows without the pair are censored as `landing_unknown`: the Raydium and Jupiter failures.
   - The landing price is measured exactly from the failed transaction itself. It is not modelled
     from other fills.
   - **Deviation from Gamma's checklist item "landing premium from our measured S+1 fills per
     lane".** S+1 fills describe other buys. For the 3 Raydium rows and the 1 Jupiter row a model
     would be a guess, so those rows are censored instead.
2. **Floor at a looser cap.** The signed minimum is the largest of three floors. The haircut and
   the chase floor both take the same cap (`pump_curve_buy.py:1170`, `pumpswap_buy.py:833`).
   - Output haircut: `expected x (1 - cap/100)`.
   - Chase floor: the USDC ticket at the source's post-trade price x `(1 + cap/100)`.
   - Hard floor: the ticket at the source's average price x `(1 + hard/100)`. It applies only
     where the row carries the average.
   - The study recomputes all three with the live formula (pinned by the test). At `t = 0` the
     result must reproduce the recorded `min_tokens_atomic`, or the row is censored as
     `floor_unreproduced`.
   - Variant **A**: the chase cap goes up by `t`, clamped to the hard cap, as
     `solana_wallet_chase_cap_pct` does today. For a cap-30 wallet, A stops at +5.
   - Variant **B**: both the chase cap and the hard cap go up by `t`.
   - `need_pp` is the smallest `t`, to 0.01 pp, at which `Left` meets the recomputed minimum.
   - Assumption: the same landing. A looser floor changes only the minimum, not the transaction's
     slot or its place in that slot.
3. **Price after the failure.** `memecoin_refusal_captures`, filtered to gate `slippage_fail`,
   provider `jupiter`, `quote_price_basis = 'exact_route'`, quality `ok`.
   - The first observation is a Jupiter buy quote at the clip. It fixes a bag.
   - Every later observation is an exact-route sell quote of that bag (`expected_usdc / tokens`,
     the same convention funded uses for exits).
4. **Fees.** 2 x the failed transaction's own `fee_lamports`, one for entry and one for exit. SOL
   is priced at the leg's own rate, `cost_usdc / spend_lamports`. The auto_free route sends no tip.

## Rules then and now

| What | In the window | Today |
|---|---|---|
| Hard floor (35 points over the source average) | 4 of 29 rows carry the average. It became required on 10-06 (SOL-1). | Every row |
| frankdegods' cap | 50 | 35, clamped (ca171fec5, trap 5ece041f) |

`need_pp` is measured against the floor actually signed then. `t` is added to each row's own
recorded `cap_pct`. For the 25 rows without the average, today's hard floor could only raise the
minimum. Fills gained on those rows are therefore an upper bound for today's policy, and the
result is labelled that way.

## Per-row output

- `cap_pct`
- `need_pp_A` and `need_pp_B`
- landing premium over the post-trade price, and over the average where the row has it
- `ret_60` and `ret_300`
- `bag_ratio`: `Left` tokens / the capture bag at the 300 s observation. Rows outside [0.8, 1.25]
  are flagged. The ratio is reported only and decides nothing.

**How `ret_h` is computed:**

1. Anchor at the failed attempt's `completed_at`.
2. Take the sell observation (`refused_at + age_s`) nearest `anchor + h`.
3. Accept it only within ±15 s at 60 s and ±30 s at 300 s; otherwise censor that horizon. No
   imputation.
4. `ret_h = (Left tokens x sell unit price - fees) / ticket - 1`, in percent.

## The table

For each variant and each `t` in +5, +10 and +20, over missed buys, report three tables:
- slot-leg gains;
- fallback-leg gains;
- the union.

Each table shows fills gained and, for `ret_60` and `ret_300`, n, mean, median, sum and the mean
without the best buy. Per wallet, report the union for variant B.

**Late fallbacks (6).** These were filled later, so the cap's cost is a later, pricier fill. For
each, report the fallback lane, the percent paid over the modelled slot landing, and the
position's realized percent return.

**Cap-passing slot fills (33 in the window).** These are descriptive only, never part of the
decision, because the valuation differs:

- the book's realized percent return under the live exit policy. 16 of 33 closed before 60 s and
  29 of 33 before 300 s, so a hold-to-300 s value does not exist for them;
- hold-to-60 s from `memecoin_route_series` (`unit_price` against `entry_price`, nearest within
  ±15 s), where it covers. It covers 19 of 33.

## Decision rule (pooled, variant B, over the union of missed buys)

Recommend raising both caps by the largest `t` that meets all of the following:

1. At least 8 missed buys are gained at `t`, by either leg, with both horizons determined.
2. Their mean `ret_300` is at least 0.
3. Their mean `ret_60` is at least 0.
4. Their median `ret_300` is at least 0.
5. Their mean `ret_300` with the single best buy removed is at least 0.
6. The marginal band (buys whose first gain, by either leg, falls in the band ending at `t`) has a
   mean `ret_300` of at least 0, when that band has 3 or more buys.

If no `t` meets all six, the verdict is NOT SHOWN, and the recommendation is to keep the caps.

**Per wallet.** Apply the same rule with at least 5 buys. Per-wallet verdicts come first (the
operator's order: judge each wallet alone).

A wallet-scoped lift above `MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT` is clamped today (trap 5ece041f).
A wallet verdict is therefore a recommendation that needs a per-wallet hard cap in code. Nothing
is built from this read.

## Expected usable sample (coverage only; no price was read)

| What | Rows |
|---|---|
| Missed rows with a landing pair that matches our minimum and reproduces the floor | 20 of 23 |
| Of those, determined at 60 s | 18 |
| Of those, determined at 300 s | 15 |
| Late fallbacks with a landing pair | 5 of 6 |

By wallet, missed rows:

| Wallet | Missed rows |
|---|---|
| ResellCalendar | 10 (2 are Raydium, so censored) |
| DegenCapitalLLC | 9 (1 is Raydium) |
| frankdegods | 2 |
| fmpumpguy | 1 |
| Pasterniq | 1 |

**Erratum (2026-10-08, engineering review SOL04).** This table first said ResellCalendar 11, a
total of 24. Appendix A holds 10 ResellCalendar missed rows, 23 in all, matching the cohort headline
(29 rows: 23 missed, 6 late fallbacks). Only this summary count changed. The cohort, its
parameters, the read and its output, the horizon censoring and the verdict are unchanged, and the
study was not re-run. `tests/test_chase_cap_prereg_counts.py` reconciles this table with Appendix A.

**Revision 2 coverage.** 10 cohort buys have a fallback refusal within +20 points; 9 of them are
missed. A buy reached by both legs is counted once, as a slot fill.

**The one read.** Run `--read` once after Gamma's PASS. Commit its output as the results doc. No
re-run with changed parameters. Any deviation found after the read is stated, not fixed silently.

## What this does not measure

- **Exit policy.** Returns are hold-to-h. TP, stop and trail rails are not simulated.
- **Independent pre-sign refusals beyond +20 points.** The 8 in the window are all over +20 and are
  listed, not valued.
- **Bag size.** The capture's bag is bought seconds later through Jupiter. Its sell quote prices
  a bag of similar size, not exactly `Left` tokens, and the impact difference is not modelled.
- **The 3 Raydium `ExceededSlippage` rows.** They have no capture. `SLIPPAGE_FAIL_REASONS` lacks
  that string, which is an instrument gap reported to Alpha separately.
- **The sample describes our own sends.** The denominator is our slot-lane failures, not the
  market.

## Edge cases

| Case | How it is handled |
|---|---|
| Money path | None. Read-only `mode=ro` connections, no knob or code change. |
| Speed and concurrency | Not applicable. One offline read. |
| Absent versus zero | A censored horizon is reported as censored, never as 0. A row without the source average is not given a hard floor. |
| Units | Token atomic units use the base decimals. USDC has 6 decimals; lamports are 1e9 per SOL. `t` is in points over the cap; returns are in percent. |
| Rollback | Not applicable. |

## Appendix A: the frozen cohort

| At (UTC) | Signature | Wallet | Reason | Fate |
|---|---|---|---|---|
| 10-04 06:38 | uZvDkWNpLVqw | frankdegods | BuySlippageBelowMinBaseAmountOut | missed |
| 10-04 06:42 | 3QehYQd7jbiH | frankdegods | BuySlippageBelowMinBaseAmountOut | missed |
| 10-04 07:34 | V7D7ujALmXwL | DegenCapitalLLC | BuySlippageBelowMinTokensOut | late_fallback |
| 10-04 08:29 | 5Wt7PjMwKPE5 | DegenCapitalLLC | BuySlippageBelowMinTokensOut | missed |
| 10-04 18:03 | 3HEoGvYwxAgB | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | missed |
| 10-04 19:58 | 4xpn4zx1RYig | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | late_fallback |
| 10-04 20:17 | 5jHY1D82Zc84 | fmpumpguy | BuySlippageBelowMinBaseAmountOut | missed |
| 10-04 20:22 | 5d3VeyCBV8FX | ResellCalendar | ExceededSlippage | missed |
| 10-05 00:41 | 2mhamZKAW2T1 | ResellCalendar | ExceededSlippage | missed |
| 10-05 06:57 | 4wmNDRZbyGnm | ResellCalendar | BuySlippageBelowMinTokensOut | missed |
| 10-05 06:58 | dXthjcWTBvCA | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-05 07:51 | 3xWE3xacmesE | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | late_fallback |
| 10-05 08:44 | 25NVVnXPBLWF | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | late_fallback |
| 10-05 14:20 | 4BJZJKrm9yG2 | Pasterniq | BuySlippageBelowMinTokensOut | missed |
| 10-05 17:02 | 2RrVR2KjVZUW | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | missed |
| 10-05 17:27 | 2Q467SEwtAdL | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | missed |
| 10-05 17:32 | 2hmKR88aWEGF | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | late_fallback |
| 10-05 20:09 | 61gtnYwi84rG | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-05 21:36 | 4snDRASBRm79 | frankdegods | slippage tolerance exceeded (Jupiter 0x1771) | late_fallback |
| 10-06 06:22 | 3J7UXYfZWxN8 | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-06 06:24 | 4Sq1aZA4tbRG | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-06 09:14 | 3e8SvFZa77QR | DegenCapitalLLC | BuySlippageBelowMinTokensOut | missed |
| 10-06 12:00 | 647MF77krmNK | DegenCapitalLLC | ExceededSlippage | missed |
| 10-06 14:14 | 3hyBhw2e3v3C | DegenCapitalLLC | BuySlippageBelowMinTokensOut | missed |
| 10-06 19:07 | 5Eqzq7tKiXH8 | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-07 01:00 | 2XoWgDN9eptP | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-07 01:46 | FM7LeMbc9wtH | ResellCalendar | BuySlippageBelowMinBaseAmountOut | missed |
| 10-07 06:27 | 4Vni4fSXPpaS | DegenCapitalLLC | BuySlippageBelowMinTokensOut | missed |
| 10-07 07:31 | 4E1f85HhyYQ8 | DegenCapitalLLC | BuySlippageBelowMinBaseAmountOut | missed |

## Appendix B: fallback quote refusals on cohort buys (exact gap minus cap, points)

| Signature | Wallet | Fate | Refusals | Any within +20 |
|---|---|---|---|---|
| uZvDkWNpLVqw | frankdegods | missed | +76.8 | no |
| 3QehYQd7jbiH | frankdegods | missed | +148.2 | no |
| 5Wt7PjMwKPE5 | DegenCapitalLLC | missed | +2.0 | yes |
| 3HEoGvYwxAgB | DegenCapitalLLC | missed | +18.9 | yes |
| 5jHY1D82Zc84 | fmpumpguy | missed | +9.1 | yes |
| 5d3VeyCBV8FX | ResellCalendar | missed | +37.2 | no |
| 2mhamZKAW2T1 | ResellCalendar | missed | +78.7 | no |
| 4wmNDRZbyGnm | ResellCalendar | missed | +120.0 | no |
| dXthjcWTBvCA | ResellCalendar | missed | +25.4 | no |
| 3xWE3xacmesE | DegenCapitalLLC | late_fallback | +34.7 | no |
| 25NVVnXPBLWF | DegenCapitalLLC | late_fallback | +6.7 | yes |
| 4BJZJKrm9yG2 | Pasterniq | missed | +20.8 | no |
| 2Q467SEwtAdL | DegenCapitalLLC | missed | +10.8 | yes |
| 61gtnYwi84rG | ResellCalendar | missed | +40.4 | no |
| 3J7UXYfZWxN8 | ResellCalendar | missed | +49.0 | no |
| 4Sq1aZA4tbRG | ResellCalendar | missed | +8.8 | yes |
| 3e8SvFZa77QR | DegenCapitalLLC | missed | +0.9 | yes |
| 647MF77krmNK | DegenCapitalLLC | missed | +13.4, +0.0, +3.5 | yes |
| 3hyBhw2e3v3C | DegenCapitalLLC | missed | +29.0 | no |
| 5Eqzq7tKiXH8 | ResellCalendar | missed | +131.8 | no |
| 2XoWgDN9eptP | ResellCalendar | missed | +6.0 | yes |
| FM7LeMbc9wtH | ResellCalendar | missed | +75.8 | no |
| 4E1f85HhyYQ8 | DegenCapitalLLC | missed | +17.7 | yes |

## Appendix C: independent pre-sign refusals (points over the cap, from the reason string)

| Signature | Wallet | Lane | Points |
|---|---|---|---|
| 4GhYqG6G53mA | ResellCalendar | onchain_fallback_after_slot_lane | +95 |
| 4UHJwgsiyxJX | ResellCalendar | on-chain (null lane) | +34 |
| 5yLdd3i2S71h | DegenCapitalLLC | on-chain (null lane) | +33 |
| 1wGGVQFoZ5PQ | ResellCalendar | onchain_fallback_after_slot_lane | +32 |
| 5vK3Fdi8ij47 | DegenCapitalLLC | onchain_fallback_after_slot_lane | +44 |
| LjihGPJBN4tt | DegenCapitalLLC | onchain_fallback_after_slot_lane | +30 |
| 4NYezY9ySCmb | Pasterniq | on-chain (null lane) | +50 |
| 5Q4bFdNgMLvN | DegenCapitalLLC | onchain_fallback_after_slot_lane | +63 |
