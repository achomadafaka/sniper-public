# Chase reference = the source's POST-trade price (Solana on-chain lane)

## 2026-10-20 execution-slot amendment

The [current entry policy](2026-10-18-first-buy-slot-window-design.md) permits any execution
slot after the existing quality gates. Source-slot and S+2 deadline clauses below are historical
and superseded. First-buy selection, original receipt-age bounds, source proof, movement policy,
signed minimum output and full-only mirrors remain active.

## 2026-10-19 operator amendment: intervening market movement only

This amendment supersedes the source-relative admission and signed-price-floor rules below
**only while `MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY=1`**. The default is `0` for a reviewed
pinned cutover/rollback. The operator explicitly chose to exclude both trades' impact, proceed
when movement cannot be verified, and add no entry-time network call, lock, wait or retry.

The measured quantity is `100 × (current marginal pool price / source post-buy marginal pool
price − 1)`, on the same mint, pool and quote currency. Raw `(quote_atomic, base_atomic)`
reserves cancel units; exact rational comparison admits the boundary. A source move from $1 to
$2 followed by a pre-copy pool price of $2.70 is +35%. Our average entry, route fees, hypothetical
post-copy pool price and signed output minimum are not inputs. Source averages and drift
columns retain their historical meaning. Existing numerical band/wallet/hard caps remain; the
lower enabled cap applies to measured movement. The average-based post-reference clamp is not
used as a marginal baseline.

The existing coherent PoolTape guard measures Pump curves and compatible PumpSwap from the same
account snapshot it already validates: identity, source ordering/ancestry, stream coverage,
fresh head, generation/revision and account-vector revalidation remain mandatory. Missing,
stale, malformed, unsupported or mismatched evidence is explicitly `unknown`, proceeds, and
never falls back to either average fill or a provider impact-percentage inversion. Ordinary
Solana, EVM/LI.FI and native paths without this complete two-anchor proof record unknown.
This is bounded coverage, not proof of a universal 35% landing-price ceiling.

Source-relative display checks and signed average-price floors stand down in this mode.
Independent impact, token safety, quote-relative output slippage, roundtrip, post-peak, roster,
sizing, authority and entry-window rules remain. Native quoted Solana builders retain the
unlifted base output haircut: wallet movement overrides do not turn it into a >=100% haircut.
Native DAMM/DBC lack an independent output quote and return unsigned `output_quote_unavailable`;
the existing confirmed mechanical backup retains the original first-buy/S+2 deadline. It may
miss that deadline. No zero minimum or new delayed retry is allowed. Native EVM uses its existing
local size-aware output model and a positive independent slippage floor. Its V4 liquidity must
bind to the validated final source receipt alongside pool, key, mint, quote, orientation and sqrt.
The existing source-post proof carries optional canonical uint128 `liquidity_atomic`; legacy
proofs remain valid for their old uses but cannot supply the new output model. Missing model
evidence, unknown fees/hooks and an unbound bridge first leg are mechanical build misses.
Single-range EVM estimates do not prove tick crossing or hook state.

Edge cases: unknown is not zero movement; negative movement passes; exact cap equality passes;
wallet overrides change movement only; fees/own size cannot consume the movement allowance;
source-price telemetry cannot grant authority. A movement-witness failure cannot erase a
separately proven signed-minimum impossibility veto. Per-process settings are never mutated on
a live entry thread. Disabling this one mode and restoring the prior pinned release restores
legacy policy; no ledger rewrite is required. Same-slot/+2 sends and full-only source mirrors
are unchanged. Local cache checks cannot guarantee bank landing or subsequent price movement.

Offline warmed exact-source-cache benchmark, 3,000 iterations per venue: curve guard p50/p90
87.19/89.50 → 116.74/119.81 µs; PumpSwap 69.87/72.28 → 123.97/127.31 µs. Added p90 was
30.31/55.03 µs (under 0.06 ms). Final receipt-bound native EVM output floor p50/p90 72.54/76.69 µs. These fixture
measurements prove local bounded arithmetic only, not live dispatch or landing latency.

## 2026-10-06 receipt-valuation amendment: forwarded WSOL residue

PLT receipt `2ebfcf66` paid $500 USDC and received 16,675,750.779965 tokens.
A new owner WSOL account forwarded its remaining 0.041064597 SOL to an external
fee account through `CloseAccount`. `_route_facts` now includes that proven
outflow instead of reporting `route_quote_inflow` and dropping initiation proof.
The account must be created and initialized as owner WSOL in the same receipt,
absent before and closed afterward, with exactly one owner-authorized close to
an external destination. Existing accounts, retained/unknown inflows, wrong
mint/authority and ambiguous token targets retain refusal. No RPC is added.
The watcher builds the ordinary signed-wallet payment proof; balance fallback
never supplies authority. The processed slot lane still requires its own proof.

Status: operator order 2026-09-28 ~14:40Z ("the way we measure is trash ... instead of us just
measuring what we can actually control"; "take it over yourself"). Author: Alpha. Activation is a
separate operator yes (knob default off).

## Problem

The Solana exact-quote chase gate (`solana_swap_executor.py` ~:1242) compares our quote with
`pending.source_price`. On the on-chain lane that is the source's AVERAGE fill
(`source_size_usd / token_delta`, `scripts/research/onchain_wallet_watch.py:612`). A large source buy
moves a thin pool by itself, so its own price impact is charged to us as "chase". 3310 (attempt
5a4a32af, DegenCapitalLLC, 2 x $2k): average fill 7.74e-5, our quote 1.39e-4, "+80%, cap 30".
The slot lane already measures against the post-trade price (`memecoin_slot_lane.curve_price_usd`,
`amm_market_facts`), so the same buy is judged on two yardsticks depending on the lane that saw it
first (finding-the-chase-reference-differs-by-lane-2026-09-28).

## Change

1. `value_source_buy` adds `source_impact_ratio` = post-trade pool price / the source's average pool
   price, both from the SAME receipt in pool units (unit-free), and `source_post_price` =
   `source_price` x ratio (USD):
   - pump curve: post = `virtual_sol_reserves / virtual_token_reserves` from the TradeEvent (post-trade),
     average = net lamports / token atomic amount;
   - PumpSwap: ratio = B_pre / (B_pre - base_out) (constant product; the base side holds no virtual
     reserve, so the pool's virtual quote, absent from the event, is not needed). A vault-only quote
     price read 0.78 on the real untaxxable receipt and is NOT used.
   A ratio below 0.98 or above 50, or any unreadable input, gives no post price.
2. `OnchainEv` carries `chase_reference_price` = `source_post_price` when
   `MEMECOIN_REAL_CHASE_REFERENCE=post_trade` and the row has one; otherwise None.
3. The executor's exact-quote chase uses `chase_reference_price` when given, else `source_price`
   (today). It records the basis in the `chase_quote_confirmation` substage. `source_price` itself,
   drift columns and every study keep the average fill.
4. The runner's pre-quote chase (`chase_ok` + `effective_chase_cap`) uses the same reference, and
   applies NO source-impact allowance when the reference is post-trade (removing the impact twice
   is the 09-28 chase_source_split error).
5. Knob `MEMECOIN_REAL_CHASE_REFERENCE` = `avg_fill` (default, today) | `post_trade`.
6. Risk bound (Lambda review F1): the reference is clamped at `MEMECOIN_CHASE_POST_TRADE_MAX_RATIO`
   (default 2.0) x the average fill, basis `post_trade_clamped`; the premium over the average fill
   is then at most 1.3 x 2.0 = 2.6x at cap 30. The oversize-packet retry passes the reference on
   (F2). Readers of `chase_naive_premium_pct` split bases by `chase_impact_skip_reason =
   'post_trade_reference'` (F3).

## Edge cases (CLAUDE.md rule 5)

1. Money path: YES, Solana entry admission on the on-chain lane. It only changes which reference the
   chase cap compares against; the cap stays 30 (35 nano); every other gate is unchanged.
2. Fill speed: no new call. The ratio is computed in the watcher from the receipt it already
   fetched and decoded; the runner adds one attribute read. No lock, no retry.
3. Concurrency: none. Pure functions; the reference travels on the event object and one executor
   kwarg. No module or process state.
4. Absent vs zero vs unknown: no post price (route/multi-leg valuations, non-SOL quote, unreadable
   reserves, ratio out of range) = the average-fill reference, with the reason recorded. A zero or
   negative price is never a reference.
5. Dependency failure: a malformed event already makes the valuation unknown (existing path). The
   PumpSwap post price omits the virtual quote reserve, so it understates the impact (stricter).
6. Units: the ratio is taken in pool units from one receipt and applied to the USD average price;
   pool-unit prices are never compared with USD (log sd ~3 across pools).
7. Sample: after activation, attempts carry `chase_reference_basis`; compare fills and refusal
   captures by basis. Expect more admissions on large source buys into thin pools.
8. Rollback: unset the knob (or `avg_fill`) and restart the runner. Half-applied: a new watcher with
   an old runner adds an ignored field; an old watcher with a new runner has no post price, so the
   reference falls back to the average fill. Both are safe.

J7 (no receipt) and the EVM chains are out of scope: J7 keeps the average fill, and J7 Solana is in
shadow for covered wallets. The slot lane already uses post-trade prices.

## Check left behind

A pytest on the real 3310-shaped numbers: the average reference refuses and the post-trade reference
admits; the pre-quote leg does not stack the impact allowance on a post-trade reference; the ratio
on the two real confirmed receipts in `tests/fixtures/memecoin/confirmed_source_buys.json` is >= 1.

## Amendment: routed USDC buys (Beta, 2026-09-28T17:57Z; defect found by Alpha)

### Problem

Since activation (15:01Z) every on-chain chase check on a routed USDC buy used `avg_fill`. The
routed pairer's `RoutedHandoff.write` (`memecoin_slot_lane.py`) inserts the detection FIRST with
basis `slot_lane_routed` and no `source_post_price`, and the watcher's later row is dropped by
INSERT OR IGNORE. The watcher's own route valuation (`_value_route`, basis `route_owner_quote_out`)
also carries no post price. From 14:50Z to 17:40Z, 16 of 34 Solana detections had no post price:
11 `slot_lane_routed` and 5 `route_owner_quote_out`.

### Change

1. One pure function, `routed_leg_ratio(events, mint, delta)`, in `memecoin_source_valuation`. It
   takes the venue events decoded from the buy's own logs and returns (ratio, venue) for the ONE
   event that bought the coin's whole delta, or (None, reason). Every venue uses the
   constant-product BASE-side ratio B_pre / (B_pre - out). It is unit-free, so it does not depend
   on the USDC average:
   - pump curve: B_pre = virtual_token_reserves (post) + token_amount; event mint must be the coin.
   - PumpSwap: B_pre = pool_base_token_reserves (pre-trade), as `_impact_ratio` already does.
   - LaunchLab: B_pre = virtual_base - real_base_before, and out = real_base_after -
     real_base_before. The curve must be constant-product: the event must satisfy `curve_out`
     exactly. It did on 59 of 59 receipts, and on all 6 here, where the base-side ratio equals the
     quote-side ratio exactly. Any other curve type fails the identity and gives no ratio.
   - Raydium CPMM: B_pre = output_vault_before, and output_mint must be the coin.
   - Meteora DLMM, Orca, Raydium CLMM and Manifest are not constant-product: no ratio.
2. The binding is by amount, because a LaunchLab or PumpSwap event names its pool and not its coin.
   The event's base out must satisfy delta <= out <= delta / 0.9, where delta is the owner's coin
   rise (RoutedBuy.token_delta_atomic; the route's `amount`). The slack covers a Token-2022
   transfer fee of up to 10%: the 6 LaunchLab coins here carried exactly 1% or 3%. A split route
   has out < delta on every leg and fails. Zero, or two or more, qualifying events give no ratio.
3. `RoutedHandoff.write` and `_value_route` then record `source_impact_ratio`,
   `source_post_price` = `source_price` (the USDC or route average per received token) x ratio,
   and `impact_leg`, or `impact_unknown` with the reason. The same [0.98, 50] filter and the
   existing MEMECOIN_CHASE_POST_TRADE_MAX_RATIO clamp apply. No new knob:
   MEMECOIN_REAL_CHASE_REFERENCE already gates use.
4. `_value_route` decodes the events from the receipt's `logMessages`, with the same decoders the
   routed pairer uses (`routed_pump_buys`, `trade_events`, `swap_events`).
5. The stated looseness is recorded where the chase is judged (Alpha, on approval). When the reference
   came from a routed leg, `OnchainEv.chase_reference_note` = `routed_leg:<venue>; average includes
   upstream leg and router fees`. The executor records it as `reference_note` in the
   `chase_quote_confirmation` and `chase_quote_skipped` substages. The entry retry payload carries
   it, and so does the oversize retry. Gamma's forward read splits on it.

### What it gives, on the 16 real receipts (14:50-17:40Z)

| coin leg | rows | ratio |
|---|---:|---|
| LaunchLab, one leg | 6 | 1.0021-1.0481 (5 routed handoff, 1 route_owner_quote_out) |
| PumpSwap, one leg | 2 | 1.0056, 1.0109 (both route_owner_quote_out) |
| split across two venues | 6 | none (DLMM+CPMM x2, PumpSwap+DLMM x2, Orca+CPMM, DLMM x2) |
| Manifest | 1 | none |
| Raydium CLMM | 1 | none |

Coverage is 8 of 16. It would have changed NO chase verdict here. The two covered rows that
reached the chase gate (7MYpDa, 9to7pM; ratios 1.048 and 1.048) filled anyway. 5HpB4g (+56%,
refused) is an Orca+CPMM split route and keeps `avg_fill`. Even binding its dominant CPMM leg
(ratio 1.153) leaves +35%, over the 30% cap. The fix makes the reference consistent across
lanes; it does not add admissions in this sample.

### Edge cases (CLAUDE.md rule 5)

1. Money path: YES, the Solana entry chase reference on the on-chain lane. It changes only the
   reference, only under `post_trade`, and it is bounded by the existing clamp (2.0 x average).
2. Fill speed. The routed handoff adds pure arithmetic on events the pairer already decoded.
   Measured on the implemented `routed_post_facts`: p50 1.5 us, p90 2.0 us per buy (16 real
   receipts x 200 runs; the prototype measured 0.9/1.1 us). It adds no import: `routed_leg_ratio`
   lives in `memecoin_slot_signal`, and importing `memecoin_source_valuation` from the lane would
   have cost 141 ms on the first routed buy. The watcher's route fallback adds decoding of the
   receipt's logs: p50 228 us, p90 294 us. That runs only on route fallbacks (5 in ~3 h), on the
   watcher thread, before its detection insert. No I/O, lock or retry is added.
3. Concurrency: pure functions over immutable decoded events. No module, env or connection state.
4. Absent vs zero vs unknown: no qualifying leg, an ambiguous leg, a non-CP curve, or a ratio
   outside [0.98, 50] means no `source_post_price` and `avg_fill`, as today. The reason is recorded
   in `impact_unknown`. A ratio of exactly 1.0 (no impact) is a valid post price equal to the
   average.
5. Dependency failure: a decode exception is caught, and the valuation stays as today (no post
   price). The routed pairer's own decoders already never raise.
6. Units and identity: out and delta are the same token's atomic units. The ratio is unit-free.
   `source_price` stays the average per RECEIVED token, as the gate uses it today. Stated error:
   the USDC average includes the upstream legs' fees and impact (USDC to SOL) and router fees, so
   the post price is overstated by that fraction. That makes the gate looser by that fraction. It
   is unmeasured, small on deep SOL/USDC legs, and bounded by the clamp.
7. Sample: the 16 are every Solana detection without a post price from 14:50Z to 17:40Z, not a
   selection.
8. Rollback: MEMECOIN_REAL_CHASE_REFERENCE=avg_fill ignores the field. A code revert removes it.
   Half-applied (new watcher, old runner) adds an ignored JSON field.

Not handled: split routes (6 of 16), and concentrated-liquidity or orderbook venues.

Review fixes (Lambda, 2026-09-28, after 43839716):
- 3: in `_value_route`, where the receipt is in hand, a leg binds only when a token account NOT owned
  by the source lost exactly `out` of the coin: the vault that paid the leg. Otherwise the reason is
  `leg_unverified`. The routed handoff has no balances and keeps the amount binding. A split route
  binds nowhere without a transfer fee. On a fee coin, the major leg binds only when the minor leg is
  at most f/(1-f) of it; 3ZAqTxur's CPMM leg (98.66%) would bind at a fee of 1.4% or more.
- 6: `post_trade_clamped` never reached the executor substage, which re-derives the basis from the
  price alone, nor the retry rebuild. `reference_note` now carries it ("post_trade_clamped at 2.0x
  the average fill"), after a routed leg's note when there is one.

### Check left behind

`tests/test_memecoin_routed_leg_ratio.py`, on a fixture of the real receipts' `logMessages`:
- the 8 covered rows bind to their venue with the ratios above;
- the split, DLMM, CLMM and Manifest rows give no ratio;
- the transfer-fee slack both admits and bounds (a 1% and a 3% LaunchLab coin bind; an event
  whose out is below delta, or above delta / 0.9, does not);
- `RoutedHandoff.write` stores `source_post_price` on a real routed LaunchLab buy, and the
  on-chain lane's `chase_reference` reads it as `post_trade`;
- `_value_route` stores it on a real `route_owner_quote_out` receipt.
- a routed-leg reference reaches the executor substage as `reference_note`, the event-path
  reference carries none, and the note survives the entry retry payload.
