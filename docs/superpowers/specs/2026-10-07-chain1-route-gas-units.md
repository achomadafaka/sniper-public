# Chain 1: entry-only multihop gate and per-route gas units (2026-10-07)

Operator yes (via Alpha, 2026-10-07 ~18:5xZ). Branch `beta/multihop-gate-route-gas`.

## 1. The v4_multihop round-trip gate is an entry gate

`evm_real_seams._quote_v4_multihop` applied the round-trip budget to every quote. A sell quote
carries no `round_trip_pct`, and `round_trip_within_budget(None)` is False on every chain, so
v4_multihop never answered a SELL in the ladder. The chain-1 safety sell probe refused every
multihop-only coin (0x24c2c6f7, five attempts, 18:17-18:18Z). On 4663/56/8453 the kyber rung
answers those sells, which is why nothing noticed there. The gate is now `is_buy`, and a buy over
budget still refuses.

Real exits of a multihop-ENTERED position were never affected: they replay the stored route
through `dex_v4_multihop_exit_quote`. That path is fork-proven on chain 1
(`docs/superpowers/evidence/eth/multihop_24c2_fork_2026-10-07.jsonl`).

## 2. Gas units per route class

The class comes from the quote's own `route` (`memecoin_evm_risk.route_gas_class`), never a pool
lookup:

| class | rule | knob `MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1_<CLASS>` | value | basis |
|---|---|---|---|---|
| V2V3 | protocol uniswap_v2 / uniswap_v3 / pancake_v3 | optional (flat fallback) | 382,335 | 09-30 run4 v2 p90, n=4 (v3 n=1: 320,038) |
| V4 | uniswap_v4 without `hooked` | optional (flat fallback) | 414,240 | 09-30 v4 p90, n=3 |
| V4_HOOKED | uniswap_v4 with `hooked: True` | REQUIRED | 638,162 | 09-30 v4_hooked p90, n=11 |
| V4_MULTIHOP | uniswap_v4_multihop, no leg hooked | REQUIRED | 488,436 | max of n=2 hookless coins (a912 488,436; bb0c 444,609), 10-07 |
| V4_MULTIHOP_HOOKED | uniswap_v4_multihop, any leg hooked | REQUIRED | 1,051,328 | one coin (0x24c2, hook 0xcba49142), two runs (1,051,328; 1,048,308) |

- A multihop's legs come from the route's `legs_path`, else its `bridge_key`/`coin_key`. Without
  them the class is unknown, never assumed hookless.
- Any other protocol (kyber, relay, lifi, curves) is unknown and refuses on chain 1.
- Required classes refuse while their knob is unset; V2V3 and V4 fall back to the flat
  `MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1`.
- The executor hands the guard the chosen quote's route. A winner whose class changes after the
  guard (post-approval requote, stale-winner refresh, min-out retry) is re-priced before the swap is
  signed (trace `evm.gas_guard` revision `class_change`). A refusal there costs at most the approval
  already sent.
- Code defaults stay unset. Alpha writes the knobs in `.env` at deploy.

## Known under-prices

- V4_HOOKED at its p90 leaves the 09-30 tail: ELA round-tripped at 904,983 (1.42x).
- V4_MULTIHOP rests on n=2 coins, V4_MULTIHOP_HOOKED on one coin. Fork units are one block's state.
  Verify from live receipts (entry and exit gasUsed) and re-measure as chain-1 multihop fills arrive.

## Expectation for the operator

Of the 6 chain-1 J7 coins tried for a multihop route on the fork (0x24c2 plus 5 USDC-bridged ones):

- 1 routed in budget (0x24c2, -4.1%);
- 2 routed over the entry round-trip budget (-13.5%, -25.3%) and are refused correctly;
- 3 had no route.

The gate fix will admit few chain-1 multihop coins.
