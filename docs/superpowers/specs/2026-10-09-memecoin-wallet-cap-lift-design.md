# Per-wallet Solana cap lift (150%), frankdegods and ResellCalendar

Status: SPEC + BUILD, for Gamma. Author: Beta, 2026-10-09.
- **Order:** the operator, via Alpha: "put the cap-lift LIVE directly, no paper, for Frank and
  ResellCalendar, Solana only".
- **The fast exit is dropped.** The operator decided on no timer: there is no 30 s fast exit and no new
  exit policy. Their positions keep the normal follow-30-50-v2 rails (TP 25, stop 35, catastrophe floor,
  max hold 14400).
- **Default:** inert. The lift acts only through a per-wallet list, which Alpha sets in `.env` after
  Gamma's PASS.
- **Rollback:** empty the list, drop the `solana:<uid>=150` entries, then restart.

**Evidence: spec 2026-10-09 S-landing, section 6, M1 late-uncapped arm.** Uncapped fills at our real
landing were:
- up on Frank: +$52 at +30 s and +$24 at +60 s, 3 of 5 sends;
- up on ResellCalendar: +$179 and +$160, 7 of 8 sends;
- small n, and pump_amm reads are biased upward (vault reserves omit the virtual quote).

That replay exited at +30/+60 s. **These positions exit on follow-30-50-v2 instead, so the M1 numbers
do not predict this policy's PnL.**

**The wallets** (`source_user_id`):
- frankdegods: `aefe2ddd-c580-5245-a2f5-e4ed62f7ef10`;
- ResellCalendar: `ff03cead-db17-5f20-b8aa-b2cae7e15d6a`.

## 1. The change

Today: `MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET` entries are capped at
`MEMECOIN_REAL_MAX_COPY_PREMIUM_CEILING_PCT` = 75, a code constant. An entry over it is dropped loudly,
and the wallet falls to the global cap.

Change (`sniper/settings/memecoin.py`, `parse_copy_premium_by_wallet`):
- **New list `MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS`** (source_user_ids; default empty).
- **For a `solana:<uid>` entry whose uid is on that list,** the ceiling is
  `MEMECOIN_REAL_MAX_COPY_PREMIUM_LIFTED_CEILING_PCT` = 150, a code constant.
- **Every other entry keeps the 75 ceiling.** That covers an unlisted wallet, a listed wallet's every-chain
  entry and any other chain.
- **An entry over its ceiling CLAMPS to it, loudly; it is no longer dropped** (Alpha). So a wrong edit
  order can never cut a cap to the global: Frank's `solana:` 150 written before the list lands at 75,
  not at the global 35.
  - A malformed, non-finite or non-positive entry is still dropped.
  - This applies to every wallet's over-ceiling entry, not only these two. Today an entry over 75 is
    dropped; it now holds at 75.
- **One log line per listed wallet per process,** on its first slot build:
  `[memecoin] cap lift first build wallet=… cap_used=… band_used=… min_out=… min_out_bound_x_source_avg=…`.
  This lets Gamma's first grade confirm what the lane signed.

Alpha then sets `solana:<uid>=150` for the two wallets, and both list entries in the same edit.

What one entry of 150 moves (the existing rule; nothing else changes):
- **The hard cap over the source's average fill** (`copy_premium_cap`) becomes 150.
- **The post-trade chase band** (`wallet_band_cap_pct`, inside `solana_wallet_chase_cap_pct`) is lifted
  to 150.
  - For Frank, today it is 50: his exec-premium ratio is 1.5, and his copy-premium entry is 50.
  - For ResellCalendar it is 30, the roster default.
- **The same two numbers apply on every Solana funded lane:**
  - the slot-lane builders: pump curve, PumpSwap, CPMM, LaunchLab, DAMM v2 and DBC;
  - the on-chain/J7 exact-quote chase gate (`solana_swap_executor`);
  - the Solana dip retry.
- **Unchanged:** the impact cap, the round-trip cap, sizing, every other admission gate and the exit
  rails.
- `MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET`, the ratio knob for Frank's 1.5, is not touched. The 150 copy
  entry lifts the chase band by itself.

**What 150 does to the signed min-out (slot lane, `pump_curve_buy.capped_min_out`).**
- `min_out = max(output_floor, price_floor)`.
- **The output floor** is `expected × (1 − cap/100)`. At any cap ≥ 100 it is ≤ 0, so it no longer binds.
- **The price floor** is the larger token count of:
  - our clip at source post-trade price × (1 + chase/100);
  - our clip at source average × (1 + hard/100).
- **At 150, the signed floor is the tokens our clip buys at 2.5 × the source's average fill.** The average
  is below the post-trade price for a buy, so it is the binding term. Today:
  - Frank: 1.5 × average;
  - ResellCalendar: 1.35 × average and 1.3 × post-trade.
- **The cost.** A searcher who sees our transaction can push the price up to that bound before it lands,
  and we fill there. The bound is what keeps this from being a free sandwich. Alpha chose 150 because 5
  of Frank's 7 max-price misses landed under +150%.
- **A floor of zero or less still refuses before signing** (`clip buys no tokens`).
- DAMM v2 and DBC sign the price floor alone (`source_price_floor_atomic`), which is equally bounded at
  2.5×.

## 2. The check (Alpha item 4)

`tests/test_memecoin_wallet_cap_lift.py::test_an_unlisted_wallet_never_gets_a_per_wallet_cap_above_75`:
- an unlisted wallet's `solana:` entry of 150 clamps to 75;
- a listed wallet's every-chain entry of 150 clamps to 75;
- a listed wallet's `solana:` entry of 151 clamps to 150;
- a listed wallet's `solana:` entry of 150 is accepted;
- with no list, a `solana:` 150 clamps to 75, never to the global;
- the live parsed table is held to the same rule.

It is shown to fail on a mutant that grants the lifted ceiling without the list check, and on one that
drops instead of clamping.

## 3. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** The entry price bound for two wallets on Solana, and the clamp for every
   over-ceiling entry.
2. **Fill path speed.** Nothing is added. The cap is a dict lookup that already runs. The log is a set
   lookup per slot build, and one line per listed wallet per process.
3. **Concurrency.**
   - The lists are parsed once at import.
   - The log set is add-only and touched by the lane thread.
   - Nothing is captured and restored.
4. **Absent, zero, unknown.**
   - An empty list is off.
   - A malformed entry is dropped and the wallet keeps the global.
   - An over-ceiling entry clamps to its ceiling.
   - The log's bound is None when a price-limit field is missing; the line still prints.
5. **Dependency failures.** None added.
6. **Units.** Caps are PERCENT; `MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET` is a RATIO and is untouched.
7. **Sample.** Gamma grades per wallet, never pooled, on the LIFT BAND of section 4. It is NOT
   `copy_cap_override.override_applied`, for two reasons:
   - that flag measures against the global caps (35 / 30), not the wallet's prior caps;
   - it exists only on pump builders.
8. **Rollback.**
   - Empty the list and drop the entries, then restart. Open positions are unaffected: the cap is an
     entry gate.
   - Half-applied:
     - the list without its 150 entries changes nothing;
     - the 150 entries without the list clamp to 75. That is the old ceiling: never a cut, though above
       Frank's 50 today.

## 4. Grading and stop rule (Gamma, PROPOSED; Alpha and the operator confirm the numbers before the list goes live)

**The lift band, per wallet.** These are the funded Solana entries whose own fill price is above the
wallet's PRIOR cap, on either term. They are computed from the position's entry fill against the source
average and post-trade price, on every venue.
- **frankdegods:** fill > source average × 1.50, or > source post-trade × 1.50.
- **ResellCalendar:** fill > source average × 1.35, or > source post-trade × 1.30.

Entries at or under the prior cap would have been admitted anyway, so they are not lift evidence.

**The stop rule.** It mirrors 663c9882 (397397). It is per wallet, never pooled, on the lift band, and
measured as REALIZED return under follow-30-50-v2 (the exit that ships), with the 60 s mark as a
secondary read. Stop the lift for a wallet when any of these holds:
- at n = 10, the one-sided 95% upper bound of mean realized return is below 0;
- at n = 20, mean realized return is below 0;
- the band's realized total is at or below −$150, at any time.

A stop means removing that wallet from `MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS`, then a restart. Its entry
then clamps to 75. To return to its prior cap, set the entry back as well (Frank: 50).

**Known before activation (Gamma).** M1's late-uncapped arm exited at +30/+60 s. Gamma's Frank grade,
from the would-be fill at our real landing:

| mark | median | sends up |
|---|---|---|
| +60 s | +16% | 10 of 13 |
| +5 min | −15% | 6 of 14 |
| +30 min | −11% | 6 of 14 |

With no timer, the result rests on TP 25 firing early. Otherwise the stop at 35 and the 4 h hold follow
a decaying path. The stop rule is the guard.

**For the operator: the clamp applies to every wallet.** An over-ceiling entry now clamps to its
ceiling instead of being dropped to the global. So an intended 8 typed as 80 gives 75, where before it
was dropped to the global 35. Alpha chose this.
