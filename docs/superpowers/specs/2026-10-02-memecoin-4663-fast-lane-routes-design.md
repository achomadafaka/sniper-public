# 4663 fast lane: more pool types (spec delta to 2026-10-01-memecoin-4663-fast-lane-design.md)

## Amendment 2026-10-19: unsupported bridge output and cached safety prefixes

With `MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY=1`, the native builder cannot bind an
independent output floor for `v4_bridge`. M1 returns `bridge_output_state_unbound`
before bridge state checks, fresh reads or source re-asking. The existing feed hold
releases to ordinary receipt-backed execution. Nonmovement bridge behavior and
receipt-backed shared routes retain their existing paths; no bridge signing guard is removed.

The shared executor's source-pool safety sell probe may reuse a valid cached
cash-to-middle-to-counter prefix when the existing prefix gate is enabled. It uses
one existing cache snapshot, validates its currencies, keys and fees, appends the
exact source coin pool and quotes the entire reversed path at the full atomic
quantity. Partial or different-pool answers refuse; misses retain ordinary fallback.
No discovery call, wait, second cache-lock acquisition, fee or admission change is added.
Evidence and limits: `docs/audits/2026-10-19-robinhood-entry-latency.md`.

## Amendment 2026-10-10: complete receipt splits

Complete, positive V4 split buys may reach the shared funded executor as `split_shared`, gated by existing `v4_shared` permission. The receipt must bind all unique positive coin-output pools to wallet-net acquisition within the existing 90% bound, with every V4 key known. There is no single pool price or reserve-derived native cap for a split. Unknown keys, repeated pool legs, sells/intermediate coin legs and incomplete economics retain refusal. Exact receipt economics take precedence over permitted estimates.

Verified Relay initiation survives fill-proof persistence and the RH event handoff. Receipt-backed deposit proofs retain the watcher's configured chain authority and require receipt hash, output and origin identity; receipt-less option C retains its separate gate. Shared 4663 routes can overlap early quotes/safety with market reads. Native RH routes never launch a speculative shared quote ladder. Evidence: `docs/audits/2026-10-10-evm-fill-incidents.md`.

**2026-10-10 admission repair:** The lane's final receipt guard now enforces this amendment:
explicit initiation already accepted by canonical validation survives the old blanket Relay
refusal, and complete `split_shared` evidence does not require a fictitious single pool or price.
Beneficiary-only Relay envelopes still stop before an attempt can suppress independently verified
delivery. Proof flags, native cap economics and all funded gates remain unchanged. Runnable
regressions: `docs/audits/2026-10-10-evm-fast-lane-rh-admission.md`.

**Author:** Beta · **Order:** Alpha, operator GO 2026-10-02 12:0xZ: "the 4663 fast lane routes more
pool types … V4 pools quoted in WETH or USDC/stable, V3 pools, and unknown pools via a learn step …
exact-state or estimate pricing under the same caps; keep the inline safety path; measure p50/p90.
Spec delta first." **Status:** revision 2 (Gamma's review of 73bb39d11: R2 M1 mid-rate conversion, R1 M2 staleness,
LOW1 learn thread). Build R1, R2, R3 in that order (Alpha, 2026-10-02).

> **Not the main blocker.** `float_short` refused 18 of 31 decisions: the 4663 wallet held 0.0028 ETH
> against a 0.018 ETH pilot clip. Funding it is the operator's call. This build adds routes; it does
> not fix that.

## 0. Answer first

The data reorders the order. The fast lane's biggest blocker is not routing:

- **Decisions since 10-01 22:57Z** (M1 shadow, `rh_fast_decisions`, n=31): `float_short` 18,
  `no_native_v4_pool` 8, `would_send` 3, `no_pool` 2. Routing refused 10 of 31; the native float
  refused 18. The float size is the operator's call (C2 in the entry-speed spec).
- **Where our 50 funded 4663 fills of the last 7 days executed** (the last hop's pool,
  `evm.executed_pool` joined to the tape's `pool_meta`):

  | pool type | fills |
  |---|---|
  | V4 coin/native ETH | 19 |
  | V4 coin/another token: the launchpad base `0xaa07a0e9…` ×6, five other tokens ×1-2 | 23 |
  | V4 coin/WETH | 3 |
  | V4 coin/USDG | 1 |
  | V3 coin/USDG | 1 |
  | V2 | 1 |
  | unknown | 2 |

  The 23 "another token" fills are two-hop buys (native → bridge token → coin), the same
  `v4_multihop` route that today's path quotes and builds.
- **4 of the 10 routing refusals had a native V4 pool in `pool_meta`** that the lane's in-memory
  tape never loaded: it knows only pools that swapped since the process started.

So the build order is:
1. **R1, learn:** load a mint's known pools.
2. **R2, two-hop V4 through a bridge token:** covers the 23 bridged fills plus USDG.
3. **R3, WETH-quoted V4.**
4. **V3 waits.** One fill in 7 days; the earlier 134-fill sample had none. It is re-measured 7 days
   after R1-R3 ship. Pushback on that part of the order: building a V3 template and its
   price math now spends a review cycle on about 2% of fills.

## 1. Routes

### R1. Learn a mint's pools (unknown pools)

- **When:** the decide thread finds no route, or a mint enters the candidate set (an armed wallet
  bought it in the last hour, or it is in the activity top-K). The mint then goes on a learn queue.
- **Its own thread, `rh-fast-learn` (Gamma LOW1),** never the tape thread. The decide path needs
  the tape thread current for `tape_post`. The learn thread:
  - reads every pool of the mint from `pool_meta`;
  - resolves its PoolKey through its own ledger key reader;
  - reads each pool's newest `pool_ticks` row (one indexed read per pool);
  - applies the result to `TapeState` under its existing lock, as `ingest` does.
- **A learned state can be stale (Gamma M2).** A quiet pool's newest row can be hours old. A
  learned pool routes only with `tape_post` (the source's own swap in that pool is on the tape) or a
  state no older than v1's tape lag limit (`MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS`, 8). Otherwise it
  is no route: `learned_stale`.
- **Local reads only, no RPC.** A pool with no recorded swap has no state and stays unroutable.
- **First-signal limit:** a coin's first signal still falls back to today's path when the coin was
  not a candidate. The record then names it `learned_after` instead of `no_pool`.

### R2. Two-hop V4 through a bridge token X

- **The route:** native ETH → X in pool A (native/X), then X → coin in pool B (X/coin).
- **X:** any currency with a native V4 pool. In the fills that is USDG, the launchpad base
  `0xaa07a0e9…`, and five others.
- **Pool choice:**
  - A is the busiest native/X pool with tape state;
  - B is the coin's busiest X pool.
  - Both come from `TapeState`, the same rule as v1's single hop.
- **The bytes:** `evm_v4_multihop_tx._encode_exact_input`, the encoder today's path already sends
  live. It is ONE `V4_SWAP` with `SWAP_EXACT_IN` over `PathKey[]` and ONE end-to-end
  `amountOutMinimum`, paid by `msg.value` (`currencyIn` = native): no approval, no Permit2. The
  path form also serves hooked pools.
  - Proven by `eth_call` on 4663 at a pinned block before any shadow, as v1 was
    (`rh_fast_template_proof.py` gains a two-hop case).
- **Price and minimum output, the same caps:**
  - **Hop A at its MID rate (Gamma M1):** X_mid = clip × A's pre-fee, pre-impact price at the
    snapshot. NOT a swap estimate: min_out = X / cap_X, so a LOW X would LOOSEN the native price
    cap, and a conservative swap estimate (fees taken off) would loosen it on every bridged buy. The
    real fill X_act is at most X_mid, so hop A can only TIGHTEN the cap (a revert, the safe side).
    What remains is B-state error plus the tape-lag staleness of A's mid, both bounded as in v1.
  - **Hop B, exact or estimated:** as v1. If the tape holds the source's own swap in B
    (`tape_post`), its post-trade state is exact. Otherwise the source's swap is applied to B's
    pre-state: the source's input converted to X at A's price, net of the maximum Relay and Kyber
    fees, taken from below.
  - **Cap**, in X per coin atomic: the tighter of B's post-trade price × (1 + chase cap) and the
    source's average price × `MEMECOIN_REAL_MAX_EXEC_PREMIUM`.
  - **Minimum output:** min_out = floor(X_mid / cap).
- **The error stays two-sided, as v1 §2.4:**
  - Hop A fills X_act ≤ X_mid (fee and impact), so the final output is lower than at mid, and
    min_out can revert. That is the safe direction.
  - If B fell between the snapshot and block B, the cap can be loose. It is bounded by the same tape
    lag limit and the absolute premium cap.
  - **The gate is END-TO-END (Gamma M1).** It compares the native price per coin implied by min_out
    (clip / min_out) with a reference built from the source's own receipt. R2 goes live only when
    that cap ratio's p90 is ≤ 1.00, not on B's X-denominated cap alone.
  - **Hop A's error is measured as an UPPER bound.** M1 is sign-less, so X against an actual fill
    comes only from today's-path bridged fills (about 23 in 7 days), quoted seconds after the
    decision, while A can move. M1 records X_mid against the receipt's X_act for those, labelled
    an upper bound.
- **Gas:** `_multihop_gas(2)` for the float check.

### R3. WETH-quoted V4 pools

- **The route:** UR `[WRAP_ETH(router, clip), V4_SWAP(currencyIn = WETH, payer = the router)]`, the
  WRAP_ETH command today's multihop module already encodes. Pricing is v1's single hop with WETH
  in place of native.
- Proven by `eth_call` first.
- Three fills in 7 days. It shares R2's code for picking pools, so it adds little.

### Not now: V3, V2, Pons, curves

V3 is re-measured 7 days after R1-R3. V2, Pons and curves stay on today's path (v1 §2.3).

## 2. What does not change

- **Inline safety path:**
  - the safety verdict is cached on the COIN (`memecoin_rh_fast_safety`), whatever the route;
  - the inline safety host and its fail-closed refusal stay as they are;
  - every coin gate, the floors, the armed check and the kill switch stay.
- **Caps and float:** the chase cap and the execution premium apply to every route. The float check
  uses the route's gas.
- **Rollout per route kind:**
  - M1 shadow (sign-less) records `route_kind` (`v4_native`, `v4_bridge`, `v4_weth`) on every
    decision;
  - then `runner_shadow`;
  - then live, behind `MEMECOIN_RH_FAST_ROUTES` (default `v4_native`, today's behaviour), one kind at
    a time on the operator's word.

## 3. Measurement

- **Decide timing:** M1 and the runner shadow record `decide_ms` per route kind, as p50/p90 per
  kind. R2 adds one more pool lookup and one more swap evaluation: microseconds, measured.
- **Learn:** learn-queue latency from the signal to the pools being loaded, and the hit rate on
  each coin's NEXT signal.
- **Scoreboard:** the fills Gamma's scoreboard counts, by route kind.

## 4. Edge cases (CLAUDE.md rule 5)

1. **Money path:** yes. R2 and R3 change the bytes of a funded buy. Each kind is gated by an
   `eth_call` proof, the M1 cap ratio and the runner shadow before live, behind its own knob value.
2. **Speed:**
   - learn runs on its own thread, never the tape or the decide thread;
   - R2 adds one in-memory pool lookup and one swap evaluation per decision, measured p50/p90 per
     kind;
   - no new call on the send path.
3. **Concurrency:** learn writes `TapeState` under its existing lock, like `ingest`. No new shared
   state.
4. **Absent / unknown:**
   - a pool with no recorded state is no route, never a zero price;
   - a missing X decimals or PoolKey is a named fallback (`bridge_unknown`).
5. **Failure modes:** a hop A that moves gives a lower final output, and the min_out reverts on
   chain. A ledger key read that fails counts toward the key reader's existing pause.
6. **Units:** the cap is in X per coin atomic units; X's decimals come from the tape's
   `volume_token_decimals`; USDG has 6 decimals; native and WETH have 18.
7. **The sample:** the fills table is our own funded fills. It describes what our wallets buy, not
   the market. Hence the order R1 > R2 > R3 > V3.
8. **Rollback:** `MEMECOIN_RH_FAST_ROUTES=v4_native` (today's behaviour), applied without a release.

## 5. Not handled

- V3, V2, Pons and launchpad curves (today's path).
- A coin's first signal when it was never a candidate.
- Liquidity changes on quiet pools (as v1).
- Paths longer than two hops.

## 6. The lane part: R2/R3 in the runner (revision 3, Gamma's ruling 2026-10-02 12:19Z)

Gamma approved this design ("route-shaped registry, reverse-path quoteExactInput by eth_call on the
SAME safety host, sell tx via the existing builder, inline-on-miss shares it") under four
conditions: C1 host pinning, C2 hop attribution, C3 safety_ms per route kind, and C4 old registry
entries read as v4_native.

### 6.1 The datagram

- `route_kind`. A datagram without one is `v4_native`.
- `post_wei_num` / `post_wei_den` on every kind: the coin's post-trade price in wei per coin atomic,
  as an exact fraction. It is what LaneEvent uses for the market cap and the chase ratio:
  - native: 2^192 / sqrt²;
  - weth: `coin_price(post, in_is_c0)`;
  - bridge: `coin_price(post, x_is_c0)` × 2^192 / a_sqrt².
  A datagram without it is an older one: LaneEvent keeps the v1 formula from `post_sqrt`.
- `v4_weth` adds `in_is_c0`.
- `v4_bridge` adds `bridge` (X), `a_pool`, `a_key`, `a_sqrt` (A's sqrtPriceX96 at the snapshot,
  so the lane prices hop A at its mid for its own clip) and `x_is_c0`.

### 2026-10-06 amendment: verified non-native source fills

A receipt-proven non-native V4 source now returns `v4_shared` before the WETH/bridge model.
The shared funded executor keeps the wallet's exact net acquisition and owns live quotes, safety,
admission, signing and settlement. The historical estimated-source shadow builders below remain
unchanged. Native V4 still uses its independently receipt-bound fast builder. This fixes signals
such as WIN (source transaction `0xb07617316bc3a35a7d294b6f403e7292d072f46b756d362a0d3008c95abebf1e`)
that previously reached the disabled `v4_bridge` route. A local replay preserves the exact source
amount while selecting `v4_shared`; it performs no network read or transaction submission.
Deployment enables the existing `v3_shared` and `v4_shared` kinds beside `v4_native,pons_shared`.
It does not enable the native WETH/bridge builders or remove any shared funded gate.
Rollback restores the previous pinned release and the prior route list.

### 6.2 The build, one function for both modes

`lane_buy_tx(payload, clip_wei, deadline, taker) -> (tx, min_out)` is used by `_precheck` and
`LiveLane._buy_tx`. cap = cap_num / cap_den.

| kind | min_out | tx |
|---|---|---|
| `v4_native` | clip × den // num | `native_buy_tx` (today) |
| `v4_weth` | clip × den // num (the cap is WETH-atomic per coin = wei per coin) | `weth_buy_tx(key, in_is_c0, …)` |
| `v4_bridge` | x_mid × den // num, with x_mid = clip × a_sqrt² >> 192 at the LANE's clip | `native_multihop_buy_tx` |

Every kind pays native as the tx value. Booking is unchanged:
- `lane_receipt_amounts` takes the coin from the PoolManager to us and the tx value as paid;
- WETH and X move only between the router and the PoolManager.

### 6.3 The live gate

- `MEMECOIN_RH_FAST_ROUTES` is a comma list, default `v4_native`.
- `LiveLane` refuses any other kind first, in memory: `route_off:<kind>`.
- `runner_shadow` judges every kind.
- The detector sends every kind (`LANE_ROUTE_KINDS` is removed), so the shadow measures R2/R3
  before any is live.

### 6.4 A route-shaped registry

- `registry[mint]` = `{kind, key, pool, bridge, a_key}`, where `key` is the pool the coin is sold
  through (B).
- C4: an older `(key, pool)` tuple, or a candidate entry without `kind`, reads as `v4_native`
  unchanged.
- `make_candidates` also lists WETH-quoted and bridged coins with their route shape.

### 6.5 Safety by route kind (C1, C2)

- `v4_native`: unchanged.
- `v4_weth`:
  - one quoteExactInputSingle (`_quote_single`) on the lane host for coin → WETH on the coin/WETH pool;
  - the sell tx from today's `dex_v4_quote` with that quote as its override (the WETH branch takes
    to the sender);
  - today's simulation and classification.
- `v4_bridge`, two steps:
  - **Classification on coin → X only.** The same quote, override and simulation, through pool B
    with X as the quote currency. The tx takes X to the router and the wrap of an empty balance is
    a no-op. The simulation therefore proves the coin sells into X, and only that classifies the
    coin.
  - **Routability on the full path.** After a pass, one quoteExactInput coin → X → native on the
    same host. A failed or zero answer raises `BridgeLegFailed`:
    - in the cache, it is no verdict (today's 10 s retry), never the coin's;
    - inline, the fallback is `bridge_leg_failed`.
- **Buy probes** (only when a one-token sell quote fails) quote the forward path, with no tx.
- **C1 host pinning:**
  - every read goes through the lane's own seam on its own host;
  - the path quote calls `_rpc` on that host directly, never `evm_v4_multihop._quote_path` or
    `_freshest_read_member`;
  - `_quote_single` resolves through `_freshest_read_member`, which returns the URL unchanged for
    any URL outside the funded pools, and `safety_host_refusal` keeps the lane hosts out of them.
  - Test: with a funded pool member configured, the seam sees only the lane host.
- The signer: the leg's `v4_direct_quote_fn` gains `quote_override`, so the lane builds the sell tx
  with the leg's own signer for the permit, as today.

### 6.6 Measurement (C3)

- Every verdict, cache or inline, records `(at, mint, route_kind, path, wall_ms, outcome)` in
  `rh_fast_lane_safety_times`, flushed once a minute by the main loop as the drop counts are.
- Before any kind goes live: wall_ms p50/p90 per kind against the 2 s inline deadline, on the cache
  host while the inline host is dead.

### 6.7 Edge cases (CLAUDE.md rule 5)

1. **Money path:** yes, the bytes of a funded buy. Each kind is behind `MEMECOIN_RH_FAST_ROUTES`
   (default today's behaviour), after the eth_call proof (21/21), the M1 cap ratio and the runner
   shadow.
2. **Speed:**
   - no new call on the send path;
   - a bridged verdict costs one more quoter call (the path), in the cache or inline;
   - the build adds one multiply and a shift.
3. **Concurrency:**
   - the dispatch thread writes the registry by single dict assignments, which the cache worker
     reads;
   - the timing deque is append/popleft only;
   - no new lock.
4. **Absent / zero:**
   - no `a_sqrt` or `a_key` gives `build_refused`;
   - no post price gives `no_source_trade`;
   - a zero path quote is `BridgeLegFailed`, never a price.
5. **Failure modes:**
   - a bridge-leg failure gives no verdict, and the 10 s backoff bounds the retries on the paced
     host;
   - a transfer quirk of X itself fails the coin → X simulation and classifies the coin. It is
     named, not handled.
6. **Units:**
   - the cap is in X per coin atomic for bridge and in wei per coin atomic otherwise;
   - post_wei is in wei per coin atomic;
   - all exact fractions, so X's decimals never enter.
7. **The sample:** the shadow's numbers describe our own decisions on the coins our wallets buy.
8. **Rollback:** `MEMECOIN_RH_FAST_ROUTES=v4_native`, with no release.

**Not handled:**
- A lane position keeps no entry route (`evm_route_json` NULL), so a bridged lane fill exits
  through rediscovery, as today's native lane fills do.
- X's own transfer quirks are not separated from the coin.
- Paths longer than two hops.


## Amendment 2026-10-09: Alpha handoff completion

Shared V4 multihop key lookups now pass known currency pairs into existing family derivation after the exact key cache/store misses. The complete key hash must equal the requested pool id. Cold, wrong-family or wrong-pair cases keep existing log fallback; dynamic bridge fee keys remain excluded. No discovery RPC is added to entry.

Evidence and limits: `docs/audits/2026-10-09-codex-alpha-handoff-closeout.md`.
