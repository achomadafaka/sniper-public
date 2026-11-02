# Memecoin EVM measured entry ladder (chain 4663) — design

Date: 2026-09-06. Owner: Alpha (sniper-fc). Builder: sniper-f0. Status: operator approved.

## Problem

The entry ladder takes the FIRST hit in a fixed priority list
`["v3","v2","pons_v2","v4","v4_multihop","relay"]`, so a rung that quotes nothing about cost can
beat a rung that has already measured one.

Measured on 4663, 2026-08-23..09-06: 123 funded fills had an exact exit quote inside 15 s of fill.
Twelve were already at or below -30% on that FIRST quote, -$571 in total. Ten of the twelve came
from rungs that measure nothing:

| rung | cases | loss |
|---|---|---|
| v4 single-hop | 4 (MEOW, FATCOIN, DEBTCOIN, BUDDY) | -$267 |
| v3 | 2 (SUIT, 🅿️) | -$93 |
| relay | 4 | -$45 |

The measured multihop rung filled 38 with one such case (X, 2026-09-04, before its budget refusal
shipped).

BUDDY on 2026-09-06 is the shape: the v4 rung hit through decoy pool `0x5bb3337725` (fee 52760, no
hook) and `v4_multihop` ALSO hit, through the real hook-`0xe5e702` pool. Priority alone chose v4.
First exact exit mark 4 s after fill: -61%.

## Design

1. **Entry only** (`is_buy`). After a hit on v3, v2 or v4, quote the reverse of the whole planned
   bag and set `route["round_trip_pct"]` and `route["round_trip_measured_by"]`. One extra quoter
   call per hit. Exits are never measured and never refused: the alternative to an expensive exit
   is a bag we cannot sell.
2. **Same helper, same budget** as the multihop rung —
   `round_trip_within_budget(pct, _entry_tp_pct(chain))`; over budget returns
   `(None, None, "round_trip_over_budget:<pct>")`. No new budget knob.
3. **v4 reverses through the SAME pool.** `_v4_same_pool_reverse` reads the hit's `pool_id`, loads
   its key and quotes `_quote_single` in the opposite direction. Re-running discovery would price
   the exit through the BEST pool rather than the one we would be holding a bag in — which is
   exactly how BUDDY's decoy would still have looked fine.
4. **Unmeasured is not free.** A hit whose reverse fails carries `round_trip_unmeasured` and is
   never selected while any measured hit exists; alone, it is refused `round_trip_unmeasured:<rung>`
   and the entry window requotes. **2026-10-06 safety amendment:** Relay also requires its own
   whole-bag reverse quote within the existing cost budget. Missing/failed reverse quotes and
   insufficient deadline budget refuse the buy, including priority mode, winner refreshes and
   preflight alternates. The former unmeasured Relay exception is superseded: RIZOWEEN position
   71056 was admitted despite V4/Kyber measurements of -99.99%/-92.55%.
   Keep the existing reverse timeout and minimum time budget; add no requests, retries or waits.
   Exits remain eligible regardless of round-trip cost. This proof cannot guarantee later liquidity.
5. **Selection** waits for every rung (they already run in parallel under their own timeouts) and
   takes the highest `round_trip_pct`; priority is only the tie-break. `pons_v2` and `v4_multihop`
   already measure and are unchanged.
6. **Trace.** Every rung record carries `round_trip_pct` or the unmeasured marker, so an attempt
   explains which rung won and why.
7. **Switch.** `MEMECOIN_EVM_ENTRY_MEASURED_LADDER`; `0` restores fixed-priority selection.
   Relay buys are refused when measurement is disabled, without adding a provider call.

## Tests

`tests/test_memecoin_evm_measured_ladder.py`, real objects, fake quote functions, no network:
an over-budget v4 hit is refused; v4 at -8% loses to multihop at -3% with both round trips in the
trace; a v4 whose reverse raises is not selected while a measured hit exists and is refused alone;
an exit is never measured or refused; the switch off reproduces first-hit-by-priority; a
Relay-only unmeasured hits are refused without extending the deadline; healthy Relay reverse
quotes still pass; refreshes and preflight alternates cannot bypass the refusal.

## Rollout

Ship with the switch off. Before anyone calls the extra call free, report `quote_ms` p50/p95 from
attempt traces before and after over at least 20 attempts. The first refusal naming
`round_trip_over_budget:` from v3, v2 or v4 is the first evidence the lane does what this is for.
