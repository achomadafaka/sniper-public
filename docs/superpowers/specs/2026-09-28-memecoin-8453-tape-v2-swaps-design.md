# 8453 volume tape: V2-family swaps, the header crash, and what an own h1 needs

Status: rule-5 spec before code. Operator order via Alpha (2026-09-28 ~21:20Z): "add V2 Swap and V4
PoolManager Swap decoding to the 8453 volume tape so an h1 is a local read"; the first DexScreener
E2 replacement (docs/audits/2026-09-28-dexscreener-removal-inventory.md). Input: Gamma e1c8313e
(`scripts/research/own_h1_feasibility.py`). Author: Lambda.

## What the tape already has (measured 2026-09-28 ~21:27Z)

- **V4 is already decoded on 8453.** `memecoin_volume_writer.run` subscribes the 8453 PoolManager
  (`V4_POOL_MANAGERS["8453"]`) exactly as on 4663. Last hour of `pool_ticks` for 8453: 14,851 rows
  with a 32-byte pool id (V4) and 54,360 with a 20-byte id (V3); `onchain_pool_protocols` holds 53,596
  8453 `v4` rows and 17,572 `v3`. "8453 decodes V3 only" is the `kind` label: `decode_v4_log` stamps V4
  swaps `v3_swap` (the naming trap documented at `memecoin_evm_v4_events.py:103`); the pool-id length
  is the discriminator.
- **V2 is decoded but never subscribed.** `memecoin_tick_stream.decode_evm_log` already turns a
  Uniswap-V2 `Swap` (topic `0xd78ad95f...`) into `kind='v2_swap'` with in-minus-out amounts (the
  house sign convention, positive = into the pool). The volume writer subscribes to no V2 topic, and
  the Solidly/Aerodrome `Swap(address,address,uint256,uint256,uint256,uint256)` has no decoder.
- **Rates on the tape's own host** (base-rpc.publicnode.com, one 60 s topic-only subscription each,
  21:3xZ): Uniswap-V2-style `Swap` 0.6 frames/s over 25 pools, 0.5 KB/s; Aerodrome/Solidly `Swap`
  0.7 frames/s over 17 pools, 0.7 KB/s; V3 `Swap` 11.8 frames/s over 130 pools, 11.6 KB/s. Adding both
  V2 families costs about 1.3 frames/s.
- **Coins:** 9 distinct 8453 attempt mints in the last 24 h; 7 have a V3/V4 pool on the tape, 2 have
  none (V2, Aerodrome or a curve: unverified).

## The header crash (a defect found here)

`VolumeTape.head` reads `head['number']`; a newHeads frame without it raises `KeyError`, which the loop
treats as a disconnect: the socket is closed and the window reset. `logs/memecoin_onchain_volume.8453.log`
holds 54 `volume stream disconnected: KeyError: 'number'` lines, `logs/memecoin_onchain_volume.log`
(4663) 77. In the last 24 h the 8453 tape reset 222 times: `init` 60 (worker starts, mostly deploys),
`disconnect:TimeoutError` 55, `disconnect:KeyError` 50, the rest head gaps.

## Why an own h1 is not a local read yet

An h1 over the tape is complete only when one socket has covered 3,600 s: the reader counts blocks
stamped after the socket's first header, and V3 (and V2) have no backfill on publicnode (it refuses an
address-less `eth_getLogs`). Over the last 24 h on 8453 the unbroken segments were p50 59 s, p90
1,020 s, max 16,680 s, and **only 18.7% of the time had an unbroken window of 3,600 s or more**. A tape
h1 today is therefore a lower bound over a shorter window on about four fifths of reads, and the E2
floor that consumes it would refuse live coins. The KeyError fix removes about a quarter of the
resets; worker restarts at deploys remain.

## Change (this build)

1. **V2-family subscriptions**, only on chains in `MEMECOIN_ONCHAIN_V2_SWAP_CHAINS` (csv of canonical
   ids, default empty): two topic-only subscriptions on the tape socket, Uniswap-V2 `Swap` and
   Solidly/Aerodrome `Swap`. Both carry the same four data words (amount0In, amount1In, amount0Out,
   amount1Out). `decode_evm_log` gains the Solidly topic with the existing V2 decode. `VolumeTape.log_v2`
   mirrors `log_v3` (incomplete log resets, a removed log deletes its row and resets, one commit per
   log).
2. **Pool resolution:** the existing resolver thread (`resolve_v3_pools`) also takes 20-byte pools
   whose ticks are `v2_swap` (token0()/token1()/decimals, the same eth_calls V3 uses) and records
   protocol `v2` or `aerodrome_v2` by the topic that first delivered the pool.
3. **Reader:** `COVERED_PROTOCOLS` gains `v2` and `aerodrome_v2`; `_SUBSCRIPTION_OF` maps both to the
   `v3` delivery lag (the same topic-only publicnode subscription class; see edge case 5).
4. **A subscription error no longer kills the socket.** Today any `error` frame raises
   "provider rejected subscription". An error answering one of the NEW subscription ids is logged and
   that subscription is skipped; errors on the existing ids keep today's behaviour.
5. **The header crash:** a newHeads frame without `number` or `timestamp` is logged (its keys only, at
   most once a minute) and skipped. A real missing block still opens a hole and resets through the
   existing reorder/gap logic.

Not in this build: any gate change. The m5 gate on 8453 already reads the tape through
`max(provider, tape)`; the h1 floor (E2) keeps DexScreener until coverage is solved (next section).

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes, indirectly.** The 8453 m5 gate reads this tape (`MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS`
   includes 8453) as a lower bound through `max()`. V2 swaps can only RAISE that bound, so a coin whose
   volume sits on V2 pools can pass an m5 floor it failed before. Knob default off; Alpha activates.
2. **Fill speed.** The writer is its own process. The entry path reads the store (`read_for_gate`);
   V2 adds about 1.3 rows/s to the 19.2/s the 8453 store took over the last hour (69,211 swap rows), so
   the read's row count grows about 7%. The read's p50/p90 is measured before and after in the build (a synthetic store at both
   densities) and reported; no new I/O on the entry path.
3. **Concurrency.** The new subscriptions ride the existing socket and loop thread; the resolver
   thread is existing; no module global is added.
4. **Absent vs zero vs unknown.** An unresolved V2 pair's ticks are kept and priced once resolved (as
   V3); until then the coin reports the pool as unpriced. A V2 subscription that never confirmed is
   an uncovered protocol, not zero volume.
5. **Dependencies.** publicnode may refuse or drop the new subscriptions (handled per id, item 4). A
   V2 subscription that stalls while V3 flows is invisible to the `v3` lag it is mapped to, so V2
   volume under-counts: a lower bound, the strict side. No V2 backfill exists (address-less getLogs
   is refused), the same limitation V3 has.
6. **Units and identity.** Pool ids are 20-byte pair addresses; the sign convention is the house one
   (in minus out); quote-side USD uses the existing quote and hop prices. A fee-on-transfer token
   changes what the trader receives, not the pool-level amounts the tape sums.
7. **The sample.** 9 coins in 24 h is small; the forward read is per 8453 attempt: the tape m5 split
   by protocol (v4, v3, v2, aerodrome_v2) and the tape/provider ratio before and after.
8. **Rollback.** Empty the knob and restart the 8453 worker. Half-applied: a new writer with an old
   reader stores `v2_swap` rows that the old reader treats as an uncovered protocol (today's result);
   an old writer with a new reader simply has no V2 rows.

## The own-h1 question (a decision, not this build)

Three ways to make a tape h1 complete often enough to replace DexScreener's:

- (a) **Keep coverage across restarts.** Persist the covered block ranges per chain in the store and,
  on reconnect, refill the gap: V4 by its address filter (exists), V3/V2 by per-pool address filters
  over the known pools of the coins in the book or the attempt queue. Cost: bounded eth_getLogs calls
  per reconnect on publicnode (its 500-block archive boundary applies; 8453 makes ~1,800 blocks an
  hour).
- (b) **Accept a lower bound.** The E2 floor reads the tape h1 as a lower bound with its covered
  seconds and scales the floor by coverage (floor x covered / 3,600). Strict, simple, and wrong in the
  direction of refusing.
- (c) **Keep DexScreener for E2 on 8453** and use the tape for m5 only.

Recommendation: ship this build (V2 + the crash fix), measure the reset cadence for a day without the
KeyError resets, then choose between (a) and (b) on that number.

## Check left behind

`tests/test_memecoin_volume_writer_v2.py`, on real frames captured from base-rpc.publicnode.com (one
Uniswap-V2 and one Aerodrome `Swap`, with their `Sync`), proven red first:
- both topics decode to `v2_swap` with in-minus-out amounts equal to the frames' words;
- `log_v2` stores the row, and a removed log deletes it and resets;
- with the knob empty, the subscription list and every stored row are today's;
- an `error` frame answering a V2 subscription id leaves the socket up; one answering a V3 id still
  raises;
- a newHeads frame without `number` is skipped without a disconnect, and a real missing block still
  resets through the gap logic;
- the reader counts a resolved V2 pool's swaps for its coin, and an unresolved one as unpriced.

## As built (Lambda, 2026-09-28)

- One protocol label, `v2`, for both families: the two topics carry the same data words and the
  reader treats them the same, so a second label would add a mapping and no behaviour.
- The reader counts `v2` as covered ONLY on a chain in `MEMECOIN_ONCHAIN_V2_SWAP_CHAINS`
  (`memecoin_onchain_volume.covered_protocols`). Elsewhere a `v2` pool stays uncovered, as today:
  counting it covered with no subscription would report its missing volume as a measured zero.
- The Solidly topic joins `memecoin_tick_stream.INGEST_TOPICS` (the topic-coverage guard requires
  every decoded topic to be requested). That scan is address-filtered to held pools, so a held
  Aerodrome pool's swaps are now fetched instead of reading quiet.
- The resolver asks a `v2_swap` pool token0 and token1 only: a V2 pair has no fee(), and the revert
  would have marked it unresolvable for good.
- Gate read today (live 8453 store, 9 coins x 5): p50 4.6 ms, p90 7.9 ms. The read is per coin over
  that coin's pools, so V2 rows of other pools do not enter it; the post-activation read is taken
  from live traces.

## Review fixes (Beta T1-T4, 2026-09-28)

- **T1:** the Solidly topic is NOT in `memecoin_tick_stream.INGEST_TOPICS`, knob or no knob: the
  held-pool scan (exits, V6) is byte-identical to today. It is decoded for the volume tape only
  (the topic-coverage guard carries it as `VOLUME_TAPE_ONLY`, with a proof that the tape subscribes
  it). Adding Aerodrome to the held-pool scan needs its own `Sync(uint256,uint256)` topic
  (0xcf2aa508...) and a reader test, as separate work.
- **T2:** `VolumeTape.head` returns False for a skipped frame and True for a parsed one; the loop
  refreshes the silence timer only on True, so a stream of header-less frames still trips
  "head stream stalled".
- **T3:** `v2` counts as covered only after THIS socket confirmed both V2 subscriptions
  (`volume_feed_optional`, cleared on every reset). Required before any tape h1 replaces the
  provider.
- **T4 (forward check, not built):** a topic-only subscription registers any contract that emits the
  Swap topic and answers token0()/token1(); for m5 that error runs in the permissive direction
  through max(). Before E2 uses a tape h1: list `v2` pools that never emitted a Sync, and allowlist
  pair factories.
