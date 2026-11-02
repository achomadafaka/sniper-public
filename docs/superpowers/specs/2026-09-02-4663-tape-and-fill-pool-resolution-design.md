# 4663 tape restoration and fill-anchored pool resolution

Status: PROPOSAL. Needs Protocol C review and an operator decision. Nothing here is implemented.
Date: 2026-09-02. Author: Gamma.

## The problem, in the order the parts actually fail

1. The 4663 tick tape stopped at 2026-09-01T08:58 and is now many hours stale.
   `evm_tickstream_rpc_for_chain` returns `""` for 4663 when the tickstream RPC equals the custody
   RPC, because the tape must not compete with funded execution on one endpoint. Both were set to
   the same host, so the lane disabled itself and logged one SKIP line per pass.
2. Every `pool_meta` consumer caps tape age — `MEMECOIN_TAPE_PRICE_MAX_AGE_S` 90s,
   `hot_state_from_feats` `stale_s` 180s — so all of them are fail-closed on age. Funded exits fall
   back to DexScreener.
3. Separately, mint-keyed pool resolution refuses whenever a mint has more than one usable pool
   (`len(rows) == 1`). Measured over the 61 funded 4663 positions opened since 2026-08-30:
   10 resolve, 35 ambiguous, 15 have no row, 1 has no usable row.

These compound. Restoring the tape without fixing (3) feeds a watch set that can only name the pool
for 10 of 61 positions.

## Measurements taken 2026-09-02 (publicnode, chain 4663)

| probe | result |
|---|---|
| chain block rate | 9.92 blocks/sec (200 blocks / 20.2s) |
| address-UNFILTERED getLogs, span 1 | OK, 12 logs, 63ms |
| address-UNFILTERED getLogs, span >= 10 | refused: "Please specify an address in your request" |
| address-FILTERED, 1 pool, span 400 | OK, 547 logs, 3061ms |
| address-FILTERED, 1 pool, span 2000 | OK, 3102 logs, 1351ms |
| address-FILTERED, 1 pool, span 10000 | refused: "logs matched by query exceeds limit of 10000" |
| address-FILTERED, 8 pools, span 400 | OK, 783 logs, 3334ms |

Two conclusions the earlier reading missed. The refusal is about the ADDRESS FILTER, not archive
depth — a prior report of a ~34-block limit does not reproduce. And the ceiling on a filtered query
is RESULT SIZE (10,000 logs), not block span.

So the full broad tape needs ~10 requests/sec on publicnode (one block per request) against a
measured 16.5 req/s sustained floor with no SLA and no catch-up headroom. Not viable. A
watch-set tape is comfortably viable: one request covers 400 blocks (~40 seconds of chain).

## Fill-anchored pool resolution

`memecoin_fills` has no pool column, but it has `tx_sig`, and coverage is complete: 144 of 144
funded 4663 `kind='entry'` fills carry one. The swap receipt names the pool by construction.

Probed on 6 recent funded entries, 6 of 6 resolved:
- V2/V3: the emitting contract address of the Swap log is the pool.
- V4: the log is emitted by the PoolManager, and `topics[1]` is the 32-byte pool id.
- Routed entries produce several Swap logs (observed 3 swaps / 2-3 distinct pools). Select the hop
  whose currencies include our mint; do not assume a single hop.

This replaces a one-to-many guess with a record. Depth or recency ranking would silently pick a
pool we never traded, which is worse than refusing. A receipt is immutable, so the lookup is
cacheable for ever and costs one RPC per position once.

## Proposed order, and why it cannot be reordered

1. **Fill-anchored resolution.** Store the resolved pool per position. Fail closed when the receipt
   yields no swap log involving the mint.
2. **Watch-set tape for 4663**, address-filtered on publicnode over the pools from step 1, for open
   funded positions. Cheap: a handful of requests per poll.
3. Only then do the `pool_meta` consumers become live, and the existing route tie-break and V4
   backfill start to matter.

Doing 2 before 1 builds a watch set that covers 10 of 61 positions. Doing 3 first is what we already
did, and it produced no live change.

## Defect found while surveying the precedent

`watched_base_pools` (`memecoin_tick_stream.py:286`) builds the 8453 watch set from
`f.quote_route_target`. That column is the ROUTER/settler, documented at
`entry_attribution.py:110`, and `memecoin_exit_materializer` states the router "has no ticks by
construction". So the 8453 watch set watches an address that emits no pool events.

It is latent, not live: 8453 has zero funded fills with a route target and zero rows in
`pool_ticks`, so the lane has never produced anything. Fix it with the same fill-anchored
resolution rather than reproducing the pattern for 4663.

## What this does NOT do

It restores marks for positions we HOLD. It does not restore the broad research tape that feeds
feature windows, and it should not be described as doing so. A sampled or watch-set tape gives
partial pool coverage, and partial coverage interacts badly with the `len(rows) == 1` rule — which
is why step 1 comes first.

## Open decisions for the operator

1. Whether a dedicated archive endpoint is worth paying for. That is the only route to the broad
   tape; everything above serves pricing only.
2. Whether `MEMECOIN_EXIT_STALE_S` (now a real knob, previously inert) should stay at 180s. It was
   never chosen for a ~10 blocks/sec chain; it is the value the code fell back to.
