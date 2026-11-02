# Memecoin tick-stream data plane (v0 ingest) — design

**Date:** 2026-07-23 · **Status:** v0 approved by operator direction ("think like a quant" / per-position reader); this doc marks what is physics, what is engineering, and what the nulls already forbid.

## Why

Polled ~56s sweeps are the wrong data model for momentum reading. The exit signals that lead —
order-flow imbalance, trade-arrival acceleration, unique-buyer velocity, buy-size distribution,
LP-depth changes — are computable only from the swap tape. Every day without ingest is tape lost
forever; the feature and model work can wait, the data cannot.

## Walls (fixed, not engineering)

1. **Block time caps exit precision.** Exit = quote→sign→broadcast→confirm, seconds, chain-bound
   (4663 ≈ 2.3s blocks; Solana ≈ 0.4s slots but confirm ~1-2s). The strategy ceiling is "detect
   distribution early, exit with margin" — never "sell the top."
2. **Measured nulls stand.** Lifecycle *prediction* is null (3 methodologies); 0/46 traders beat
   luck; 4/10 stopped funded positions never had a profitable exit at any rail. The tape enables
   *reactive* state detection; it does not license an exit oracle. Any model built on this data
   passes the same pre-registered-read discipline as everything else.
3. **The tape is adversarial.** Wash/bot flow means raw features lie. Feature jobs must carry a
   wash filter (self-trades, circular makers, dust storms) before any model consumes them.

## Probed feasibility (2026-07-23, this host, existing credentials)

| venue | mechanism | probe result |
|---|---|---|
| 4663 | per-block `eth_getLogs`, V2+V3 Swap topics, no address filter | 76 swaps / 28 pools in 20 blocks (~46s) — WHOLE-CHAIN tape is small enough to ingest in full |
| 8453 | same | 86 swaps / 47 pools in 5 blocks (~10s) — busy; ingest bounded (watched pools / retention) |
| solana | `logsSubscribe` wss (Helius key already in .env) | subscribe ack + live event < 5s |

Per-block `eth_getLogs` **is** the complete tape on EVM — block time is the tick granularity
anyway; subscribe-vs-poll changes latency by <1 block there. On Solana subscription is the only
correct transport.

## v0 scope (ingest only — no features, no decisions, off the money path)

- **Store:** separate SQLite file `data/memecoin_ticks.db` (module `sniper/persistence/db_ticks.py`)
  — keeps the firehose out of `sniper_tracker.db`'s WAL and backups.
  Table `pool_ticks(chain_id, pool, kind, block_no, tx_sig, log_index, maker, amount0, amount1,
  observed_at)`, `UNIQUE(chain_id, tx_sig, log_index, kind)`; amounts stored as TEXT (int256-safe),
  sign convention: positive = into the pool (V2 rows normalized `in − out`). `v2_sync` rows carry
  reserves in amount0/1 → LP-pull detection.
- **EVM lane:** one `eth_getLogs` per chain per ~3s tick, `fromBlock=last+1`: 4663 unfiltered
  (whole tape); 8453 filtered to watched pools (open-position pools + config extras). Cursor
  persisted; restart-safe; fail-closed on RPC errors (skip tick, never invent).
- **Solana lane:** asyncio `logsSubscribe` `mentions=[mint]` per open Solana real position —
  arrival events only (slot, signature); amount decoding is deferred enrichment. Coverage is
  partial by construction (mentions match account keys); documented, degrades gracefully to an
  arrival-rate signal.
- **Runner wiring:** background thread (blind-capture worker pattern), `MEMECOIN_TICKSTREAM_ENABLED`
  default OFF in code, ON in `.env`. Observation only: no reads by any exit/entry path.
- **Retention:** rows for pools/mints never associated with a position or candidate pruned after
  `MEMECOIN_TICKSTREAM_RETENTION_D` (default 7d); position-linked tape kept indefinitely.

## What v0 unlocks (later phases, each with its own pre-registered read)

1. Feature jobs (offline first): OFI per window, arrival rate + acceleration, unique-maker
   velocity, size distribution, depth deltas — with the wash filter.
2. Distribution-state detector (logistic on flow features is enough to start) — validated
   matched-pair against the trail/stall arms before any funded wiring.
3. The momentum-remainder policy (RIDING/STALLING state machine) reading real features instead of
   no-new-high mark counts.

## Non-goals of v0

No exit decisions, no model, no wash filter yet, no Geyser/gRPC dependency, no new paid provider.
The RPC-provider decision (still open, operator) buys rate headroom and the 5s exit fastlane — it
is complementary, not prerequisite.

## v0 limitation (2026-07-23, found on first live feature run)

`observed_at` is INGEST time, not event time. Steady-state (cursor at tip) that is block time
±3s — fine for 300s windows. During catch-up slices (~400 blocks stamped in one tick) windowed
features are garbage; the first live run showed 2,927 "active pools in 10min" from exactly this.
Any detector study MUST exclude catch-up windows (identifiable by cursor jumps > expected
blocks-per-tick). Proper fix when features graduate: block-number windows with a block→time map.


## Amendment 2026-10-11: Base latency repair

Base watch identity now prefers the receipt executed_pool, then route pool_id/pool, then legacy quote_route_target. V4 pool IDs are filtered under the configured PoolManager address with IDs in topics[1]; V2/V3 contract pools use address filters. Mixed scans perform at most two observation-worker reads, and both must succeed before inserts or cursor advancement. Existing catch-up/gap limits remain. Base observation uses a dedicated publicnode endpoint rather than the execution endpoint that returned429/-32011. This repair does not establish an earlier executable exit peak.

Contract: `docs/superpowers/plans/2026-10-11-base-latency-repair.md`.
