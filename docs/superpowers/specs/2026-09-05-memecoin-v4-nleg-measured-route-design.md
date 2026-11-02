# Memecoin V4 N-leg measured-cost route (chain 4663) — design

Date: 2026-09-05. Owner: Alpha (sniper-fc). Status: approved for build by the operator on 2026-09-05.

## Problem

On Robinhood chain a launchpad coin has one real pool (hook `0xe5e702…`, fee 0) and several
ETH-paired decoy pools (fee traps at 75-99%, or $4-$216 seedings). The real pool is often paired
with USDG or a tokenized stock (LLY, GOOGL, NVDA, GME, RBLX, HIMS, SPCX).

The funded entry can route two legs: `cash -> bridge -> coin`. The bridge leg must be a
fixed-fee pool. A stock-paired coin whose stock has no fixed-fee ETH pool, or whose ETH pool is
thin, is refused as `no_route` or is admitted at a poor round trip.

Measured on 2026-09-05 (7 days, chain 4663): 99 mints were refused for route reasons. 43 had no
sane ETH/WETH pool at all. 26 of those 35 with a fee-0 pool sit on hook `0xe5e702…`. The existing
two-hop finds a route for 8 of 8 sampled at round trips from -7.3% to -18.3%. PH: two legs through
LLY/ETH ($28k, 5% fee) cost -12.7%; three legs through ETH -> USDG -> LLY -> PH cost -3.4%.

## Outcome

The route is an ordered list of legs. A leg is admitted by its measured end-to-end cost, quoted
forward and back on every decision, not by a fee rule. Three-leg routes through a configured
second bridge (default USDG) are searched next to the two-leg route, and the best measured round
trip wins. The caller's round-trip budget stays the only cost gate.

## Scope

In scope:

1. Route model: `route["legs_path"]`.
2. Search: two-leg and three-leg candidates, best measured round trip.
3. Builders: entry and exit encode N PathKeys.
4. Exit: reversed legs from the stored route, partial-sale fallback unchanged.
5. Gas per leg.
6. Refusal reasons for every new branch.
7. Flag `MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS`, default 2.

Out of scope:

- Hook `0x0310cfeb…` pools (sell quotes revert with `UnexpectedRevertBytes`; 2 coins per week).
- Mixed V3 + V4 paths.
- Any change to the ETH -> first-bridge leg rule (fixed fee only).

## Route model

`route` keeps `bridge`, `bridge_key`, `coin_key` for two-leg routes and for stored positions.
It gains `legs_path`: a list of `{"currency": <address>, "key": <PoolKey>}` ordered from cash to
coin. The last leg's currency is the coin. `bridge` = `legs_path[0].currency`, `coin_key` =
`legs_path[-1].key`. The existing integer `route.legs` stays the leg COUNT (it is asserted by
`tests/test_memecoin_v4_multihop_tx.py` and `tests/test_memecoin_v4_multihop.py`); there is no
`legs_n`. `multihop_quote` keeps its own `"legs": 4` (quote calls) unchanged.

A reader that finds no `legs_path` builds it from the pair. This keeps every stored two-leg
position readable.

## Search

Inputs: `chain`, `cash` (native), `mint`, `amount_atomic`, `clip_usd`, `min_depth_clips`.

1. Coin pools: `_pools_with_depth(chain, mint)` (unchanged).
2. For each counter-currency S of a fee-sane coin pool (existing `_candidate_bridges`):
   - Candidate A (two legs): `_select_bridge(cash, S)` fixed-fee; `_select_coin_leg(S, mint)`.
   - Candidate B (three legs), only when `MAX_LEGS >= 3`: for each M in
     `MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_<chain>` (default: USDG on 4663):
     leg 1 `_select_bridge(cash, M)` fixed-fee; leg 2 the deepest pool joining M and S with a
     sane fee, hook-priced allowed; leg 3 `_select_coin_leg(S, mint)`.
3. Quote every candidate forward (`_quote_path`) and back (reversed legs, amount = forward out).
   A candidate with any leg that does not quote is dropped with a named reason.
4. Choose the candidate with the highest `back / amount_in`. Return its route with
   `round_trip_pct`, `legs_path`, `coin_depth_usd`, `bridge_depth_usd`, and `legs` = the count.

The caller (`evm_real_seams._quote_v4_multihop`) applies `round_trip_within_budget` as today.

Why a hook-priced middle leg is safe here: the rule that excludes it exists because its cost was
unbounded and unmeasured. In this search the cost is measured on every quote, in both directions,
and re-measured at exit. The exit already falls back to a partial sale when the full bag does not
quote. The rule stays on the ETH -> M leg, which carries all volume.

## Builders

`_legs_from_route(route, cash, mint)` returns `route["legs_path"]` as `(currency, fee, ts, hooks)`
tuples when present, else the two-leg tuple list. It fee-screens every leg with `_fee_is_sane`.
A hook-priced leg (fee == `DYNAMIC_FEE_FLAG`) passes the screen (it already does).

`build_multihop_entry` and `build_multihop_exit` do not change shape. `_encode_exact_input`
already takes N PathKeys.

Gas: `_multihop_gas(legs_n)` = `MEMECOIN_EVM_V4_MULTIHOP_GAS_BASE` + `legs_n *
MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG`. Defaults reproduce today's two-leg value for `legs_n == 2`.
The plan's canary reads `eth_estimateGas` on three-leg builds and sets the per-leg default from
the measured p95 plus 20%.

## Exit

`dex_v4_multihop_exit_quote(route=stored)`: when `stored["legs_path"]` exists, the sell path is the
reversed legs. The full-bag quote uses `_quote_path` on that path; the partial-sale trial loop
uses the same path. When `legs_path` is absent, the current two-leg code runs unchanged.

## Refusal reasons

`_note(...)` names each new branch: `second_bridge_pool_missing:<M>`, `middle_leg_missing:<S>`,
`path_forward_no_quote:<n>`, `path_reverse_no_quote:<n>`, `no_candidate_within_legs:<max>`.
The ladder writes the reason on the `v4_multihop` rung (shipped 2026-09-05, commit `ced1c989`).

## Settings

- `MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS` (int, default 2). The deploy sets 3 on 4663.
- `MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_4663` (comma list, default `0x5fc5360d…` USDG).
- `MEMECOIN_EVM_V4_MULTIHOP_GAS_BASE`, `MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG`.

All thresholds live in `sniper/settings/memecoin.py`.

## Testing

Unit (new file `tests/test_memecoin_v4_multihop_nleg.py`):

1. A three-leg route round-trips through `_legs_from_route` into three PathKeys in order.
2. The exit builder reverses a stored three-leg route.
3. A stored two-leg route without `legs_path` still builds (compatibility).
4. Search returns the three-leg candidate when its measured round trip beats the two-leg one.
5. Search never returns a three-leg candidate when `MAX_LEGS == 2`.
6. A hook-priced middle leg is admitted only through the measured path; a hook-priced first
   bridge leg is still refused.
7. Every new refusal branch sets a reason.
8. Gas is per leg and reproduces today's value for two legs.

Live canary before funded use (read-only, publicnode, no GeckoTerminal): quote 20 stock-paired
refused coins from the last 7 days; report the round-trip distribution for two-leg versus
three-leg and `eth_estimateGas` for three-leg builds.

## Rollout

1. Merge with `MAX_LEGS` default 2 (inert).
2. Canary report to the operator.
3. Deploy with `MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS=3` in `.env`. Report the first three-leg fill
   and its exit.
