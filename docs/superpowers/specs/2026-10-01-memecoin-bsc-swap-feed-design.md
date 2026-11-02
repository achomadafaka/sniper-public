# BSC (56) swap feed for volume-death (design)

**Author:** Beta · **Order:** the operator, 2026-10-01 (brain 32538846): vol_death on BSC "once a BSC swap feed
exists on the operator's coming archive RPC". Relayed by Alpha as queue item 5: spec now, build only after the
operator adds `MEMECOIN_EVM_ARCHIVE_RPC_URLS_56`. **Status:** PROPOSED, spec only. Not provisioned at
2026-10-01 ~10:00Z.

## 1. Why and what

Every funded 56 position reads `stream_healthy()`, which only sees Solana marks, so vol_death on 56 is a
bare `blind_frozen` for life with no audit row. There have been 58 funded 56 positions since 2026-09-01:
28 real_tp, 20 b_stop, 8 manual, 1 written off, 1 timecap. The rail has never measured one.

The design feeds the 4663 rail's own state object (`RobinhoodVolumeState`) from a polling thread on the
operator's archive endpoint. It watches only the pools of open 56 positions. Its silence decision and
eth_getLogs confirm are the 4663 rail's, unchanged.

**Why not a chain-wide tape** like 8453's: a topic-only Swap subscription on BSC carries the whole
chain's DEX flow, far above 8453. That tape would share `data/memecoin_onchain_volume.db` with the
4663 and 8453 writers, whose history is "database is locked" restarts under load (2026-09-22). Watching
the few bound pools costs about 1.3 requests per second and nothing on that file.

**Why polling, not a WebSocket:** a subscription can stay open and stop delivering (4663, 63ba30af; 8453
V4, 2026-09-29), and only a heartbeat or a cross-check notices. A poll either returns a range or fails.
Coverage is then contiguous block ranges by construction, and a failed poll is a measured gap.

## 2. Host

- `MEMECOIN_EVM_ARCHIVE_RPC_URLS_56`, the operator's dedicated endpoint.
  - `_evm_archive_rpc_pool` parses only `("4663", "5042")` today, so `"56"` must be added there.
  - The endpoint is never the funded lane (`MEMECOIN_EVM_RPC_URL_56`, `MEMECOIN_EVM_RPC_POOL_URLS_56`, custody), never defibit, and
    never a host rule 0724bc08 refuses (ninicoin, nodereal's research WS, drpc). A start-up check
    refuses an archive URL whose host matches any funded 56 URL, in the style of `funded_overlap`.
- No URL, or the switch off: no poller thread, and 56 stays exactly as today.

## 3. The poller (runner process, daemon thread, never on the fill path)

`EVM_POLL_STATE["56"] = RobinhoodVolumeState(health_window_s=5.0)` is its own instance, never the 4663
one. Every `MEMECOIN_VOLDEATH_POLL_S` (default 1.5 s, about 2 BSC blocks; *measure the block time at
build*), the loop does:

1. Rebuild the bindings from the open real 56 positions and `replace_bindings(...)` them. A position binds
   EVERY known pool of its mint, not only the one we traded. Silence on our pool while the coin trades
   on another is the 49013 "stranger's quiet" in reverse (Gamma LOW 1). The set is:
   - the classification's `liquidity_pair_address`;
   - the route's pool ids;
   - the executed pool from our own entry receipt (`evm_position_pools`). Its worker runs for 4663 only
     today; it runs for 56 on the archive endpoint after the entry booking, off the fill path;
   - the same worker's discovery, read once per position and stored: V2 `getPair(mint, quote)` on the
     PancakeSwap-V2 and Uniswap-V2 factories, and V3 `getPool(mint, quote, fee)` on the PancakeSwap-V3
     and Uniswap-V3 factories for each fee tier, with quote in {WBNB, USDT, BUSD, USDC}.
   A mint whose set cannot be resolved (a discovery call failed) is `blind_frozen:pool_uncovered`. A curve
   route (flap) binds nothing, as on 4663.
2. Make one `eth_blockNumber` call, then one `eth_getLogs` over `[last + 1, head]` with
   `address = [bound pools] + [CANARY]` and these topics:
   - Uniswap-V2 / PancakeSwap-V2 `Swap(address,uint256,uint256,uint256,uint256,address)`;
   - Uniswap-V3 `Swap(address,address,int256,int256,uint160,uint128,int24)`;
   - PancakeSwap-V3 `Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)`, which
     carries extra protocol-fee fields and so a different topic. `executed_pool_from_receipt` needs this
     topic too, or PancakeSwap-V3 entries resolve no pool.
3. On success:
   - `record_pool_activity(log.address)` for each log;
   - `record_coverage(pool)` for every bound pool the range covered;
   - `record_head(head)`;
   - `last = head`.
4. On any failure (HTTP error, 429, timeout, malformed answer, or a range over `MAX_RANGE` = 40 blocks
   after an outage): `disconnect()` (coverage and heads cleared, so the rail is blind) and
   `last = head - 1` on the next good tick. Coverage restarts from there. A gap is never back-filled into
   coverage.

Rate: about 2 calls per tick, so about 1.3 req/s, plus one eth_getLogs confirm per fire attempt at the
threshold (4663's `robinhood_activity_logs` shape, same address filter, archive URL).

## 4. Decision and audit

- In `_armed_exit_decision`, a chain in `MEMECOIN_VOLDEATH_POLL_CHAINS` (new; call-time, digits only,
  default empty) calls `EVM_POLL_STATE[chain].decision(...)` with the archive confirm function. This is
  4663's path with a different state and host.
- The 4663 state answers a bare `blind_frozen` / `swap_lane_uncovered`, which the runner does not audit.
  For a poll chain they are renamed `blind_frozen:poll_stale` / `blind_frozen:pool_uncovered`, and both
  join `POST_THRESHOLD_REASONS`, so blindness writes one audit row per minute. That is the operator's
  "blind_frozen WITH an audit row", as on 8453. `state_reader_dark:*`, `state_moving` and
  `measured_state_still` are already audited.
- The exact-quote veto and the wrapper's executable veto apply unchanged.
- A 56 position never takes the 8453 tape path. A chain listed in both switches uses the poll, and the
  build tests that precedence.

### 4.1 The canary (Gamma MEDIUM)

- One always-busy reference pool rides in every poll's address filter at no extra call: PancakeSwap-V2
  WBNB/USDT, read from the factory at start-up and never typed in.
- At the threshold the rail requires the canary's Swap logs through the silence window, with no gap
  over `CANARY_GAP_S`, a bound measured on the host at build. Otherwise it returns
  `blind_frozen:poll_unvouched` (audited). This is the idea behind the 8453 tape's `tape_unvouched`.
- The threshold confirm call also carries `[bound pools] + [CANARY]` and requires the canary to show logs.
- A host that answers `[]` for everything therefore reads blind, not silent: a pruned range, a silent
  filter cap, a wrong-chain mirror.
- What remains uncovered is a host that drops OUR pools' logs while it serves the canary's. That is far
  narrower, and the exact-quote veto still applies.

### 4.2 Re-discovery at the threshold (Gamma LOW, CLEAN note)

- Discovery runs once per position, so a pool created after our entry would never be bound, and
  trading that moves to it would read as silence on the old pools. Examples: a new V3 fee tier, or a
  migration.
- So on a fire attempt only, before the confirm, the same getPair/getPool calls run again, a handful of
  eth_calls.
- A NEW pool returns `blind_frozen:pool_set_changed` for that tick (audited). The pool is bound and the
  clock restarts.
- A failed re-discovery is `blind_frozen:pool_uncovered`.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path:** yes, an exit rail. Default empty, so nothing changes until the operator lists 56.
2. **Fill speed:** nothing on the entry path. The poller is its own thread. The decision reads memory
   under the state's lock, plus one confirm call only at the threshold, on the exit sweep. The build
   measures p50/p90 of the decision and of the confirm call.
3. **Concurrency:** one writer thread, sweep readers, and the state's existing lock. It is a per-chain
   instance, so 4663's stream and the 56 poller never share or clear each other's state. Bindings are
   replaced whole, never mutated in place.
4. **Absent vs zero vs unknown:**
   - a successful empty `eth_getLogs` is silence;
   - a failed one is blindness (disconnect);
   - a pool bound after the last poll is uncovered until a poll covers it;
   - no head ever is blind.
5. **Dependency failures:**
   - a timeout, 429 or ban means blind and audited, never silence;
   - a wrong empty answer is caught by the canary (§4.1) unless the host serves the canary and drops
     only our pools. That narrow case is stated, not solved: the exact-quote veto is the only check
     independent of the host, and a second permitted 56 host would close it;
   - reorgs: polling at `latest` is safe for silence, because a reorged-out swap can only shorten the
     measured silence. Catch-ups stay bounded by `MAX_RANGE` (Gamma LOW 2).
6. **Units and identity:**
   - block numbers vs seconds: activity is stamped with arrival host time, as on 4663;
   - BSC block time is measured at build, not assumed;
   - chain id is the string `"56"`; pool addresses are lowercased.
7. **The sample:** fires happen only on bound, covered pools. Every blind minute is audited, so the
   graded denominator includes blind holds. Flap-curve positions (2 of 58 since 09-01) are uncovered by
   design.
8. **Rollback:** remove 56 from `MEMECOIN_VOLDEATH_POLL_CHAINS` and restart the runner; the poller does
   not start. Half-applied (switch on, URL missing): no thread, and the rail reads
   `blind_frozen:poll_stale` (audited), never silence.

## 6. Build (after the URL exists)

1. Settings: add 56 to `_evm_archive_rpc_pool`; add `memecoin_voldeath_poll_chains()`; refuse an archive
   URL that shares a host with a funded 56 URL.
2. TDD:
   - the poller's coverage under success, failure and an over-range catch-up;
   - the PancakeSwap-V3 topic, both in the poll and in `executed_pool_from_receipt`;
   - the label renaming and audit membership (the `_post_threshold_labels` guard);
   - per-chain state isolation from 4663.
3. Measure on the provisioned host:
   - BSC block time;
   - `eth_getLogs` latency for the bound-pool filter;
   - decision and confirm p50/p90;
   - the canary's gap distribution, which sets `CANARY_GAP_S`;
   - the poller thread's CPU and wall cost inside the funded runner process (Gamma LOW 2).
4. Gamma reviews; Alpha merges and deploys; the operator lists 56.

## 7. Build (Beta, branch beta/bsc-swap-feed, 2026-10-01)

**Host** (`MEMECOIN_EVM_ARCHIVE_RPC_URLS_56`, NodeReal, not a funded 56 host; read-only, at most
2 req/s):
- chainId 0x38.
- Block time 0.450 s over 200 blocks.
- 10-minute soak at about 1.2 req/s (735 calls): no 429. `eth_getLogs` p50 131 ms, p90 149 ms,
  max 253 ms; `eth_blockNumber` p50 129 ms.
- A 40 s step at 3 req/s: 120 calls, no 429. The free tier's monthly quota is unknown. The poller is
  idle (no request) while no 56 position is bound.

**Canary.** PancakeSwap V2 WBNB/USDT `0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae`, read from the
factory.
- Over the soak, 1,787 swaps; 13 of 367 polls (3.5%) held none.
- The gap between polls with a swap: p50 1.63 s, p99 3.27 s, max 4.91 s.
- `CANARY_GAP_S = 10` (2x the max).
- The vouch counts from the silence start, because the first tick of coverage may hold no canary.

**Addresses checked on chain.** Each has code: Multicall3, WBNB, USDT, BUSD, USDC, and the PancakeSwap
V2/V3 and Uniswap V2/V3 factories.

**Changes from sections 3-4:**
- **Discovery quotes.** Discovery also uses every token the position's route names as a quote. 56
  routes hop through other base tokens (67623: WBNB -> 0x4902c5eb... -> coin). On the 6 newest 56
  positions, factory discovery against the four quotes alone found 0 pools for 3.
- **One call.** Discovery is one Multicall3 `aggregate3` eth_call. With the route tokens that is 10
  to 60 sub-calls per position, so the threshold re-discovery is one call too.
- **The executed pool** is read from our own entry receipt (`executed_pool_from_receipt`, last swap)
  into the poller's memory, not into `evm_position_pools`. That adds no writer on the tick database.
  A restart re-reads one receipt per open position.
  - On the 6 newest 56 positions, the executed pool was in the bound set on 6 of 6.
- **Resolution budget.** One position is resolved per tick. A position whose discovery failed binds
  nothing (`blind_frozen:pool_uncovered`) and is retried on the next tick.

**Cost.**
- Poller tick, against the host with 3 positions and 7 pools bound, 60 ticks: CPU p50 4.29 ms, p90
  4.81 ms; wall p50 250 ms, p90 259 ms; the first ticks resolve pools at 609-1022 ms.
- Decision, offline with 12 pools, 4,096 heads and 20,000 canary marks: below the threshold
  p50 23 µs, p90 43 µs; at the threshold with a stub confirm p50 1.2 ms (the canary scan).
- Confirm call over 133 blocks (60 s): p50 501 ms, p90 614 ms, on the exit sweep at the threshold
  only.

**Rollback.** Remove 56 from `MEMECOIN_VOLDEATH_POLL_CHAINS` and restart.

**Not handled.** A host that serves the canary but drops our pools' logs (section 5.5). A pool on a
DEX whose Swap event is none of the three topics.

### 7.1 Gamma's review of 7a93d73c9, folded in

- **MEDIUM: no discovery on the exit sweep.**
  - The poller re-reads each bound mint's pool set every `REDISCOVER_EVERY_S` (20 s), one mint per
    tick, on its own thread. It binds any new pool at once, which leaves that pool uncovered until the
    next poll, so the clock restarts.
  - At a fire attempt, the decision reads that set with no network call. A set older than
    `REDISCOVER_MAX_AGE_S` (60 s), or none, is `blind_frozen:pool_uncovered`. A set holding a pool not
    yet bound is `pool_set_changed`.
  - The one remaining sweep read is the confirm `eth_getLogs`, the same class as 4663's. It runs on its
    own caller with a 2 s hard timeout (`CONFIRM_TIMEOUT_S`); a timeout reads as
    `state_reader_dark:getlogs_failed` (held). It measured p50 501 ms and p90 614 ms over 60 s of
    blocks.
- **L1.** A partial Multicall3 answer (any failed sub-call) is None, never a smaller set. Now tested.
- **L2.** The funded host set is derived: every `MEMECOIN_*URL*_56` name except archive and research
  (custody RPC, pool, WS, on-chain watcher, capture read).
- **Note: the archive pool holds this URL whatever the switch says.** Adding "56" to
  `_evm_archive_rpc_pool` puts `MEMECOIN_EVM_ARCHIVE_RPC_URLS_56` (bsc-mainnet.nodereal.io) in the
  archive pool at deploy, even with the poll switch off.
  - No funded quote path reads it: `evm_v4_quote._archive_rpc_urls` is V4-only, and V4 has no 56.
  - `_funded_hosts` in exact capture and the exit-venue shadow now count nodereal as a funded 56 host.
    That is harmless today: capture reads ninicoin, and `MEMECOIN_RESEARCH_WS_URL_56` has no live user.
  - "Default off" means the poller does not run. It does not mean the archive URL is unused.
