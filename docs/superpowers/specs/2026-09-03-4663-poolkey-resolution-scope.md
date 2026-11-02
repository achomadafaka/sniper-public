# 4663 V4 PoolKey resolution — scope

Status: scoping input for Alpha. Not a plan. No code written.
Author: sniper-9a. Date: 2026-09-03.

## Recommendation

Do not build the 345-page creation-event sweep. Build three layers in this order. Layer 1 is
most of the win and costs no new network traffic at all.

The sweep is not wrong, it is redundant. The index it would fill already exists, already runs,
and already holds 8 of the 9 coins that were refused.

## Evidence

### The refusals

12 rows carry `quote_provider_poolkey_read_failed`, over 6 hourly buckets from 2026-09-02T15 to
2026-09-03T19. 9 distinct mints. Checked each against `pool_meta` in `data/memecoin_ticks.db`:

| mint | already in `pool_meta`? |
|---|---|
| 0x2fd9800f, 0xe4950829, 0x3eb06ff0, 0x7de44792, 0x3394da3c, 0x255dbb4b, 0xede9e16f, 0x6c9d6f06 | YES (3-42 pool rows each) |
| 0xa57f332d | no |

**8 of 9 were already indexed when we refused them.** The one miss, 0xa57f332d, carries
`source_token_age_s = 3454668` (40.0 days) -- older than the forward index, and far outside the
backward scan's 6 x 100k-block window.

### What the tickstream already does

`memecoin_tick_stream.evm_tick_pass` decodes V4 `Initialize` on the SAME `eth_getLogs` it already
runs for ticks, at zero extra request cost, and writes `pool_meta`. Live count: **49,397 V4 rows
for 4663**, being written now (cursor at block 53810136, current to ~5s).

### The actual gap

`decode_v4_initialize` ([memecoin_evm_v4_events.py:115](../../sniper/services/memecoin_evm_v4_events.py:115))
returns `pool`, `token0`, `token1`, `resolved_at` -- and **never reads `log["data"]`**.

The event is `Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)`: 3 indexed
(id, currency0, currency1), and the data words are `fee`, `tickSpacing`, `hooks`, `sqrtPriceX96`,
`tick`. The three fields the V4 quoter needs are **in bytes we already receive and already parse
past, on all 49,397 rows.**

And `sniper/execution/evm_v4_quote.py` contains no reference to `pool_meta` -- the funded quote
path cannot see the index at all.

So this is a wiring gap plus three discarded words. Not a missing indexer.

### Derivation is a fast path, not a fix

Brute-forced `keccak256(abi.encode(PoolKey))` over fee x tickSpacing x {0x0, 0xe5e7...}:

- 1,500 random 4663 V4 pools -> **125 matched (8.3%)** across 7 shapes; 1,375 matched nothing.
- The 14 pools we actually traded -> **4 matched (29%)**, all `(0, 200, 0xe5e7...)`.

The hook is an arbitrary address, so the space is unbounded and derivation can never be the
general resolver. It is still worth having for one reason given in Layer 3.

## The three layers

### Layer 1 — read the index we already fill. Covers 8/9. Zero new requests.

1. Decode `fee`, `tickSpacing`, `hooks` from `log["data"]` in `decode_v4_initialize`.
2. Add the three columns to `pool_meta`; keep the existing INSERT OR IGNORE immutability.
3. Have the funded pool-key path consult `pool_meta` before any `eth_getLogs`.

Backfilling the three columns for the existing 49,397 rows is NOT required to ship -- new rows
carry them from the next block onward, and Layer 2 covers any row that predates the change.

Note `pool_meta` lives in `data/memecoin_ticks.db`, a different file from the main ledger. The
read must be a read-only handle and must not put a funded-path dependency on a research DB's
availability: a missing or locked ticks DB degrades to Layer 2, never to a refusal.

### Layer 2 — one targeted query, not 345 pages. Covers the 40-day case.

`currency0` and `currency1` are INDEXED topics (verified on chain 2026-08-30, confirmed by
`memecoin_evm_v4_events.py:23`). So a first-touch coin of any age resolves with ONE
`eth_getLogs` filtered on `address = PoolManager` and `topics[2] = token` (then `topics[3]`),
`fromBlock: 0x0`, over all history.

This is the existing `backfill_v4_pool_meta` idea generalized from by-pool-id to by-currency. Its
docstring already states the principle: bounded by the size of our universe, not by chain history.

That is 1 request where the sweep is 345, and it is on-demand rather than continuous.

### Layer 3 — derive the launchpad shape. The only thing that helps sniping.

Try `(fee 0, tickSpacing 200, hook 0xe5e7...)` first, with zero RPC, and confirm by quoting.

This matters for one reason no index can fix: **the 4663 log index does not surface a fresh
pool's Initialize for 38-61s** (measured 2026-09-03, n=61 launches; 3 appeared at ~2s, the rest at
38.1-61.0s, both endpoints agreeing to the second). An indexer reads that same log index, so it
inherits exactly the same blindness. A wallet sniping at +3s cannot be followed by ANY
log-derived design, sweep included.

Layer 3 is a trust-model change -- it replaces the Initialize read that currently cross-validates
against a foreign PoolManager with a key bound to ours by construction. It needs its own review
and should ship last, behind its own flag.

## The constraint the proposal understates

"Off the funded path and not on the custody endpoint" is necessary but not sufficient.

Measured capability on 4663: Alchemy returns HTTP 400 on `eth_getLogs` at ANY span (free-tier
10-block cap), publicnode returns 403 on archive ranges. **Only
`rpc.mainnet.chain.robinhood.com` serves an archive `eth_getLogs`** -- and that endpoint is
already doing two funded-adjacent jobs:

- it is `MEMECOIN_EVM_ARCHIVE_RPC_URLS_4663`, the archive fallback the funded `_pool_key` uses;
- it is the only endpoint serving the real-time tickstream, which just recovered from 393
  consecutive `getLogs` failures and is finally current.

It is also the slowest and stalest member (ewma 393ms, lag 5.3 blocks vs 0.38 on the others).

A 345-page sweep at 773ms is ~4.4 minutes of continuous load on the single endpoint that both of
those depend on. "Not the custody endpoint" does not buy isolation here, because the scarce
resource is not custody -- it is the one endpoint with archive capability.

This exact failure mode has now happened twice: a 2s research retry loop burned ~7k funded RPC
quota in 4h (2026-08-21), and a research watcher silently fell back to the funded endpoint and sat
at 100% 429 (2026-09-03). A third instance is worth designing out rather than rediscovering.

Layers 1 and 2 avoid it structurally: Layer 1 issues nothing, Layer 2 issues one request per
unknown coin.

## What I would want before any of it ships

- The refusal population is n=12 over ~28h, 9 distinct mints, largest single row $100.20. Worth
  fixing, but it does not justify a new always-on process. Layer 1 is a few hours of work.
- Layer 1 needs one regression test: a pool present in `pool_meta` must be quotable with the
  network read stubbed to raise. That is the whole contract.
- Do not let Layer 1 silently widen: `pool_meta` rows are written by a research lane, so the
  funded path must treat them as a HINT that a quote still has to confirm, never as authority.
