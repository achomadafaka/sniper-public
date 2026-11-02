# Chase reference = the source's POST-trade price on EVM (4663, 56, 8453), both lanes

## 2026-10-19 discovery-only amendment

On 4663, a successful receipt with positive owner acquisition and exactly one known
V4 coin leg may retain `entry_hint_pool` when its output fails price binding.
Every official-manager Swap must have complete ABI shape and every V4 key must be
known. Unknown, split, malformed, reverted and unacquired cases remain unhinted.
The original `leg_does_not_bind` reason stays; no ratio, price, venue or fill proof
is granted. Only entry discovery opts into this field, with one nonblocking peek.
Safety exit hints ignore it. The local key must still hash to that pool and contain
the mint; fresh full-amount quotes, round-trip budgets and ordinary parallel fallback
remain authoritative. Evidence: `docs/audits/2026-10-19-robinhood-entry-latency.md`.

2026-10-06 routing amendment: the shared `source_post_trade` wrapper queues bounded Infinity
CL key discovery on BSC source receipts. It never waits for that work. `_decode` remains the
pure price decoder; Infinity price references remain unsupported and keep the existing average
fill fallback. Key discovery changes route coverage only, not chase references or caps. See
`docs/audits/2026-10-06-bsc-hodl-infinity-route.md`.

Status: operator order via Alpha, 2026-09-28 ~20:45Z ("take the post-trade chase reference live on
EVM for both lanes", no 4-5 day wait). Author: Lambda. Alpha approves and activates. The knob is per
chain and defaults off. Extends `docs/superpowers/specs/2026-09-28-memecoin-chase-post-trade-reference-design.md`
(Solana), whose clamp and fail-strict rules carry over unchanged.

## What the EVM gates compare today (live values, .env loaded, 20:4xZ)

Every EVM gate below compares a price with the source's AVERAGE fill (`pending.source_price` in the
executor, `ev.their_price` in the runner):

| gate | where | measure | cap |
|---|---|---|---|
| pre-quote chase | runner `chase_ok` (~:2891) | display / reference | `MEMECOIN_REAL_CHASE_MAX_PCT` 30 (fresh follow 30) |
| quote chase | `evm_swap_executor._price_gates` -> `_chase_gap` | display / source ("market"), else quote / source | `quote_chase_cap`: 30; nano 35 (on 4663 only when display and source agree within 10%, else 0 = off); 4663 over-nano ceiling 30 |
| quote over source | `_quote_over_source_refusal` | quote / source | 4663 only, 30 (`MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663`) |
| post-approval requote | executor ~:884-906 | quote / source, then quote over source | as above |
| execution premium | `execution_premium_decision` | quote / max(display, source) | 1.5x. NOT changed: it prices our route, not the coin's move |
| 4663 nano prices-match | runner ~:2984 | display / source <= 10% | NOT changed: a data-consistency check that picks the cap |

Last 7 days of EVM refusals by a source-referenced chase gate: 12 "our quote is +31..+49% above
source price (cap 30%)" (4663, J7) and 3 "exact quote is +33/+42/+51% above source price" (56, J7,
the post-approval requote). The on-chain EVM lane had none (76 EVM detections exist, all since
2026-09-28 06:43Z).

## Change

1. **Source side: one pure decoder**, `sniper/services/memecoin_evm_post_trade.py`
   `source_post_trade(chain, receipt, mint, owner_delta_atomic=None)` returns
   `{"ratio", "pool_kind", "pool", "venue"}` or `{"reason"}`. Among the receipt's venue events it
   takes the ONE whose coin output is `mint`. Zero or two or more such events (a split route, a
   batched bot tx) give no ratio. When the owner's coin delta is known, the event's out must satisfy
   delta <= out <= delta / 0.9 (transfer-tax slack, as on Solana). Ratio = the pool's post-trade
   coin price / the swap's own average coin price, both in the SAME pool's raw units, so token
   order and decimals cancel and no USD conversion happens:
   - **V4** (PoolManager `Swap`, pool = topic1 PoolId): post = sqrtPriceX96 after the swap; the
     coin side and the sign convention are read from the event and checked against the PoolManager's
     coin Transfer, on real frames before any code relies on them.
   - **V3** (`Swap` from the pool, pool = the emitter): post = sqrtPriceX96; out = the negative amount
     (pool perspective).
   - **V2** (`Swap` + the same pair's `Sync`, pool = the emitter): post = Sync reserves; average =
     amountIn / amountOut from the Swap (includes the 0.3% fee, so the ratio reads slightly low:
     stricter).
   - Curves (Pons V2, flap, launchpad) and anything else: no ratio (`venue_unsupported`).
   A ratio outside [0.98, 50], a zero sqrtPrice or reserve, or any decode error gives no ratio.
2. **Our side: the pool our quote will trade**, `route_pool(chain, route)`:
   - `uniswap_v4` -> `route.pool_id`; `uniswap_v4_multihop` -> the coin leg's `coin_pool_id`;
   - `uniswap_v2` -> the pair address by CREATE2 from the route's router's factory and init-code
     hash, from a table verified against real source receipts (a router not in the table -> None);
   - V3, curves, `relay_same_chain`, `kyberswap`, LI.FI -> None (no single pool known before the
     fill; `memecoin_executed_pool.py` says the same about aggregator routes).
3. **Binding (the answer to "which pool").** The post-trade reference applies only when OUR pool
   equals the SOURCE's pool. Otherwise the gate keeps the average fill, with the reason in the note:
   `pool_mismatch <ours> vs <source>`, `route_pool_unknown <protocol>`, `no_ratio <reason>`,
   `receipt_pending`. A price from a pool we will not trade is never used.
   - Executor gates: our pool = `route_pool` of the quote being judged (the initial quote and the
     post-approval requote are bound separately).
   - Runner pre-quote chase: our pool = the display pair `st["pair_address"]` (a V4 PoolId or a pair
     address), because that is the price the leg compares.
4. **Reference** = source average (USD, as today) x ratio, clamped at
   `MEMECOIN_CHASE_POST_TRADE_MAX_RATIO` (2.0, as Solana) x average, basis `post_trade` /
   `post_trade_clamped` / `avg_fill`. The trace's `chase_quote_confirmation` substage records
   `reference_price`, `reference_basis`, `reference_note` (pool binding result and venue) beside
   the unchanged `source_price`; refusal strings name the basis. Drift columns, sizing floors,
   studies and the premium keep the average.
5. **On-chain lane.** The EVM watcher (`push_detection`) already holds the receipt; it adds
   `evm_post` (ratio, pool_kind, pool, venue, or reason) to `source_valuation_json`. It is a NEW
   key, not `source_post_price`: `memecoin_onchain_lane.chase_reference` is chain-agnostic today, so
   writing `source_post_price` for EVM rows would go live on every chain at once under the global
   `MEMECOIN_REAL_CHASE_REFERENCE=post_trade`. `OnchainEv` carries `evm_post`; its retry payload
   carries it too.
6. **J7 lane (`follow_activity`, origin `swap:raybot:<tx>:<trader>:<mint>`).** One
   `eth_getTransactionReceipt` for the source tx, submitted when the EVM prereads start
   (`_start_entry_prereads(chain=...)`, i.e. after the roster gates, so the 977 weekly
   `not_approved` events never read), on a 2-worker pool, off the fill path. Each gate PEEKS: done
   -> bind; not done -> `receipt_pending`, average fill. Nothing ever waits on it.
7. **Knob** `MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS` (csv of canonical chain ids, default empty).
   It applies only while `MEMECOIN_REAL_CHASE_REFERENCE=post_trade` (live since 15:01Z).
   Activation: `MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS=4663,56,8453` + runner and watcher restart.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: YES**, EVM entry admission on both lanes. Only the chase references change, only on
   enabled chains, only when the pools bind; the caps stay 30 (35 nano); premium, safety, sizing and
   every other gate are unchanged. Expected effect: more admissions of large source buys into thin
   pools, bounded by the 2.0x clamp.
2. **Fill speed.** On-chain lane: no new I/O; the decode is pure on a receipt the watcher already
   fetched (sub-ms; measured in the build on real receipts). J7 lane: one receipt read that nothing
   waits on. Measured on the hosts the watcher uses for the same read (`onchain_wallet_detections`
   `source_valuation_json.reads`, all EVM rows to 20:22Z today):
   - 4663 (tx-read host): n=53, p50 48 ms, p90 70 ms, max 164;
   - 56: n=10, p50 136 ms, p90 157 ms;
   - 8453: n=6, p50 180 ms, p90 251 ms.
   Time from decision start to the gates that need it (EVM attempts with a quote, 7 days):
   - executor chase gate: 4663 p10 1,470 ms (n=87); 56 p10 941 ms (n=27); 8453 p10 2,574 ms (n=2);
   - pre-quote leg, near `probe_classified`: 4663 p10 186 ms (n=14); 56 188 ms (n=5); 8453 173 ms (n=1).
   So the executor gates see the ratio almost always; the pre-quote leg sees it on 4663 and 56 and
   often not on 8453, where it keeps the average (stricter). Added latency on the fill path: 0 by
   construction (a peek, never a join). The live p50/p90 of the J7 read is recorded in the trace
   (`evm_post_receipt_ms`) and re-read after 20 attempts per chain.
3. **Concurrency and global state.** The J7 pool is a module-level executor created once under a
   lock; futures carry nothing shared. The J7 reader has a process-local host cooldown behind a lock
   (a 403 or 429 cools it 60 s; the gate then reads `no_ratio http_403`). No module attribute, env
   var or shared connection is mutated; no capture-then-restore anywhere.
4. **Absent vs zero vs unknown.** No ratio, a pending read, an unknown route pool, a mismatch: all
   are the average fill, with the reason recorded. A ratio of exactly 1.0 is a valid post price.
   Zero, negative or non-finite inputs are never a reference.
5. **Dependency failures.** J7 read: a timeout (a 1 s socket timeout, which does not bound DNS; nothing waits on it; no
   retry), 403/429, null receipt, JSON-RPC `error` (read as an error, never as a null), or a
   reverted tx -> no ratio. The watcher path adds no dependency. A wrong-but-plausible answer (a
   different party's swap in the same tx) is what the one-event rule and the owner-delta bound
   are for.
6. **Units and identity.** The ratio never mixes pools or currencies; the USD average is the one the
   gates use today. Stated looseness: the average includes router, Relay and V4-hook fees and any
   upstream leg (USDG -> WETH -> coin), so the reference is overstated by that fraction; unmeasured,
   bounded by the clamp (same statement as the Solana routed-leg amendment). PoolIds are 32 bytes,
   pairs 20 bytes; they never compare equal. Chain ids are canonicalized before the knob check.
7. **The sample.** The first read is forward: EVM attempts by `reference_basis` and by binding
   reason. Coverage ceiling from the last 14 days of funded EVM fills (n=141): quote route with a
   bindable pool on 4663 65 of 99 (`uniswap_v4` 42 + `v4_multihop` 23), 56 32 of 37 (`uniswap_v2`),
   8453 1 of 2. How often the SOURCE traded the same pool is unmeasured until the first reads.
8. **Rollback.** Remove chains from `MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS` (or empty it) and
   restart the runner. Half-applied: a new watcher with an old runner writes an ignored key; an old
   watcher with a new runner has no `evm_post`, so the on-chain lane keeps the average; the J7 path
   is runner-only.

Not handled: V3 routes on our side, curve venues (Pons, flap, launchpad), aggregator routes (Relay,
Kyber, LI.FI), split source routes. Each keeps the average fill with its reason recorded.

## Stop line

Gamma's `chase_source_split.py forward` rule applies per chain: first 10 closed EVM fills that
passed on `post_trade` while the average-fill gap was over its cap sum <= -$150 with >= 7 losers,
or at >= 30 such closes the mean-return 95% CI (mint bootstrap) sits below 0 -> remove the chain
from the knob.

## Check left behind

`tests/test_memecoin_evm_post_trade.py`, on REAL receipts (4663 V4 buys through Relay and direct,
56 V2 buys; fetched from the research hosts, at most one call per 10 s):
- the decoder's ratio on each real receipt equals the value hand-computed from its raw logs;
- a split receipt and a curve receipt give no ratio;
- the CREATE2 pair of a real 56 route equals the real source pair's address;
- the executor, fed a real 4663 source, refuses at quote-over-source on the average and admits on
  `post_trade` when the route pool is the source pool, and refuses when it is not (`pool_mismatch`);
- the knob off, or a chain not listed, leaves every gate on the average.
Each proven red with its guard removed.

## Amendment (Beta's review of b0a1fb60, fixes by Lambda, 2026-09-28)

- **Knob off is byte-identical (F1).** An on-chain-lane EVM row carries `evm_post` whatever the knob
  says. A reference that is off for its chain resolves to `("avg_fill", None)`, and then no refusal
  suffix, no `reference_*` substage field and no `chase_requote_reference` /
  `chase_pre_quote_reference` substage is written. Tested against the no-reference run.
- **No funded host for the J7 read (F2).** The J7 lane reads a receipt only from
  `MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain>`. A chain without one gets no read and no reference
  object: its J7 gates keep the average fill. This replaces the runner-read-host fallback in change
  item 6 and Alpha's condition 2. Today only 4663 has such a host (robinhood-rpc.publicnode.com, the
  watcher's own fast host; a 403 there cools the runner's reader for 60 s, and the watcher's own
  reader keeps its own cooldown, F3). The on-chain lane still gets `evm_post` from the watcher on
  every chain.
- **The deadline (F4).** The J7 read has a 1 s SOCKET timeout, which does not bound DNS. Nothing
  waits on it: every gate peeks, so a slow read costs only its reference (`receipt_pending`).
  Dependency failures item 5 is corrected accordingly.
- **Canonical chains (F5).** The knob's entries go through `canonical_chain_id` (`robin` is 4663);
  an unknown or non-EVM name is ignored with a warning at the first read.
- **The clamp can no longer be disabled (F6).** `chase_post_trade_max_ratio()` returns 2.0 for a
  `MEMECOIN_CHASE_POST_TRADE_MAX_RATIO` below 1, non-finite or unreadable, on EVM and on the Solana
  on-chain lane. Before, a value below 1 turned the clamp off and a ratio of 40 was admitted.
