# Volume-death swap witness for chain 1 and Arc (5042), with E2 where it can serve

Date: 2026-10-08. Author: Lambda. Order: Alpha's queue (the vol_death plan's step 3): a chain-1/5042
swap witness, on-chain tape or a 56-style poller, which also feeds E2 there; rule 5 and an RPC budget
per chain (chain-1 Infura has 429 history) before building. Status: BUILT as amended in §7
(Alpha approved option A, 2026-10-08).

## 1. Today

- vol_death has a witness on 4663 (its WebSocket rail), 8453 (the pool_ticks tape,
  `MEMECOIN_VOLDEATH_TAPE_CHAINS=8453`) and 56 (the archive poller, `MEMECOIN_VOLDEATH_POLL_CHAINS=56`,
  `memecoin_bsc_volume_poll.py`, spec 2026-10-01-memecoin-bsc-swap-feed-design.md). Chain 1 and 5042
  have none: every funded position there is `blind_frozen` for life.
- Volume: chain 1 had 1 funded position since 09-08 (BUNKER, 10-08) and 364 entry attempts since
  10-01. 5042 had 6 funded positions since 09-08 (the last on 09-26) and 2 attempts since 10-01.
- E2 on chain 1 records a would-refuse only (`MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS=56,8453,1`). On
  5042 it decides, and with DexScreener out it admits unknown (decision (a), C, 2026-10-08).

## 2. Measured (one bounded read, 2026-10-08)

Chain 1, last 10 blocks, no address filter, V2/V3/V4 Swap topics: 444 logs, 44.4 per block (v4 187,
v3 161, v2 96), about 3.7 per second, 320,000 per day. Arc: not measured; every Arc URL we hold is a
funded host (see §4), so the read waits for the dedicated endpoint.

## 3. Design

### Chain 1: the 56 poller, generalized, bound pools only (vol_death; E2 not fed)

A chain-wide tape at 320k swaps a day would share `data/memecoin_onchain_volume.db` with the 4663 and
8453 writers, whose history is "database is locked" restarts; the 56 spec rejected the same shape at
BSC volume. So chain 1 reuses the 56 poller, made per chain (`EVM_POLL_STATE["1"]`, its own state):

- Bindings: every known pool of each open funded chain-1 mint: the route's pool, the executed pool
  from our own entry receipt, Uniswap V2 / Sushi V2 `getPair` and Uniswap V3 `getPool` (fee tiers 100,
  500, 3000, 10000) against WETH, USDC and USDT in one Multicall3 eth_call, and the V4 pool ids of the
  mint from our Initialize index (`memecoin_evm_v4_pool_keys`). 73 of 83 roster coins trade on V4,
  so V4 is not optional.
- Per tick, every 12 s (one block; measure at build): one `eth_blockNumber`, one `eth_getLogs` over
  `[last + 1, head]` with `address = [bound V2/V3 pools] + [CANARY]` and the V2/V3 Swap topics, and one
  with `address = PoolManager`, `topics = [V4 Swap, [bound pool ids]]`. The canary is one always-busy
  pool (Uniswap V3 WETH/USDC 0.05%), read from the factory at start-up, never typed in.
- Coverage, disconnect, `MAX_RANGE`, the threshold confirm, re-discovery at the threshold and the
  audited blind reasons are the 56 poller's, unchanged.
- E2 is not fed: a bound-pool poller sees only coins we hold, and E2 asks about coins we are about to
  buy. On chain 1 E2 records only, so nothing is lost.

### Arc (5042): measure first, then the cheaper of the two

Arc blocks are 0.5 s. Two candidates, picked by the measured chain-wide swap rate on the dedicated
endpoint:

- If Arc's DEX flow is small (under ~2 swap logs per second; measure over one hour): add 5042 to the
  volume writer (`MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS`) as a chain-wide tape. It then feeds vol_death
  (`MEMECOIN_VOLDEATH_TAPE_CHAINS`) AND E2 (`gate_evidence`), the only way E2 gets an Arc number.
- Otherwise: the generalized poller as on chain 1 (every 1.5 s, 3 blocks), vol_death only; E2 stays
  unknown and admits.

## 4. Hosts and the RPC budget

Both chains need an operator-provisioned read endpoint that is not a funded host; the poller's
start-up guard (`funded_overlap`) refuses a host the funded runner uses.

| Chain | Funded hosts today | Usable for the witness | Budget |
|---|---|---|---|
| 1 | Infura (funded `MEMECOIN_EVM_RPC_URL_1`, 429 history), mevblocker, publicnode (funded pool) | none: `MEMECOIN_EVM_ARCHIVE_RPC_URLS_1` is empty | 3 calls per 12 s = 0.25 req/s, 21,600/day; plus one multicall per new position and a re-discovery per fire attempt; confirm 1 call per threshold |
| 5042 | arc-rpc.publicnode, rpc.mainnet.arc.io (both in the funded pool and in the archive list) | none until a dedicated one exists | tape: one WS subscription; poller: 3 calls per 1.5 s = 2 req/s, 173k/day |

Chain-1 Infura is never used: it is the funded write/read lane with 429 history, and a witness that
starves it would cost exits. The operator names the endpoint; the build reads its rate limit and
sizes the tick to stay under half of it.

## 5. Rule 5

1. **Money path:** yes, an exit rail (vol_death fires an exit). Default off per chain: nothing
   changes until the operator lists the chain in `MEMECOIN_VOLDEATH_POLL_CHAINS` or
   `..._TAPE_CHAINS` and names the endpoint.
2. **Fill speed:** nothing on the entry path; a daemon thread. The decision reads memory; the threshold
   confirm is one bounded call (`CONFIRM_TIMEOUT_S`=2) on the exit sweep, which on chain 1 is the
   Ethereum worker: measure its p50/p90 at build.
3. **Concurrency:** one writer thread per chain, its own state instance, bindings replaced whole; the
   chain-1 state is never 56's.
4. **Absent/zero/unknown:** a successful empty getLogs is silence; a failure is blind (audited); a pool
   bound after the last poll is uncovered; no canary logs at the threshold is `poll_unvouched`.
5. **Dependency failures:** timeout, 429 or a ban disconnects (blind, audited), never silence. A host
   that serves the canary and drops our pools is the stated residual; the exact-quote veto still
   applies.
6. **Units and identity:** chain ids "1" and "5042"; V2/V3 pools are addresses, V4 pools are 32-byte
   ids under the PoolManager address; block times measured at build (12 s, 0.5 s are assumptions).
7. **Sample:** chain 1 ran 1 funded position in 30 days and 5042 none in October, so this buys
   coverage for rare positions; every blind minute stays audited so the denominator includes them.
8. **Rollback:** remove the chain from the switch and restart; no schema; the poller writes nothing
   to the ledger.

## 6. Operator decisions

(a) A dedicated non-funded read endpoint for chain 1 (any provider but Infura's funded key), and one
for Arc. Without them the guard refuses and nothing runs.
(b) Whether the value is worth it now: 1 chain-1 position in 30 days and no Arc position in October.
Recommendation: build the per-chain generalization of the poller now (small, tested against 56's
suite), and turn it on per chain when the endpoint exists.

## 7. As built (2026-10-08)

- Hosts: the keyed dRPC research account turned out SATURATED (its shared pacer queued ~7 slots;
  acquire 4.9-6.0 s against a 54 ms call), so both chains use KEYLESS public dRPC hosts no other role
  uses, each on its own SharedPacer: chain 1 `https://eth.drpc.org` ("voldeath-poll-1", 2 s gap),
  Arc `https://arc.drpc.org` ("voldeath-poll-5042", 1 s gap). A 429 pauses that host only; the rail
  reads blind (audited). Overrides: MEMECOIN_VOLDEATH_POLL_URL_<chain>.
- Chain 1 (93eb73537, 9197d94ba): bound pools every 24 s; V4 by pool id from the PoolKey cache on the
  PoolManager; activity and canary stamped at the block's estimated time; every block recorded as a
  head so the threshold confirm starts at the block after the last swap; the confirm reads to
  `latest`, unpaced, 2 s timeout, and after a failure answers None (state_reader_dark) for 15 s.
  Health and canary windows 60 s. Silence resolution: fires at 30-44 s of true quiet with exit_s 30.
- Arc (47acefd76): the same poller on a 6 s tick; the canary is the PoolManager's chain-wide V4 flow
  (no V2 factory known; most flow is V4: 10 swaps in 12 blocks measured). Windows 15 s. vol_death
  only; E2 on Arc stays unknown and admits (decision (a) C). The Arc tape (E2 feed) waits for volume.
- Residuals: V4 pools outside the PoolKey cache are not bound (our executed pool always is on chain 1;
  on Arc the executed V4 pool is not decoded); the confirm start rides on estimated head stamps.
