# BSC 56 fast lane: design record (V2 native, V3 and Flap shared delivery)

## 2026-10-11 follow-up: fair turns and earlier verified storage hints

Cancelled preparations have no verdict, but have consumed a worker turn. Same-priority due
candidates use their existing last-attempt timestamp for ordering; priority, TTL, refresh and
provider backoff remain unchanged. Missing or failed evidence never becomes a verdict.

Job-bound BSC preparation retains the cheap standard/Solady slot probes, then may ask for the
exact getter's access-list keys before the long tail. Every hinted balance and allowance key
still requires the existing override/read-back proof, and the original code, quote and sell
simulation checks remain. Provider, malformed, cancellation and deadline failures remain unknown;
only an exact unsupported-method reply may fall through. Hints are bounded and tried once.
Default shared and inline probe order, request pacing, job expiry and cache freshness do not change.
The [runtime audit](../../audits/2026-10-11-bsc-entry-speed/runtime-preparation-repair.md) records
the cold-path reproduction and the separate rollout evidence; readiness alone is insufficient.

## 2026-10-11 runtime repair: complete preparation across snapshots

The first preparation release cancelled the whole job whenever an essential snapshot refreshed.
The [runtime repair](../../audits/2026-10-11-bsc-entry-speed/runtime-preparation-repair.md) instead
suspends that same immutable native job outside account locks, bounded by the existing safety TTL
and at most 60 seconds. Every physical read still requires the original nine-second snapshot
admission and 1.1-second pacing. Live/inline pressure, provider failure, changed route/generation,
closed jobs and expiry still cancel; original evidence timestamps never reset on resume.
Valid RPC batches require exact reply IDs and per-row results or explicit contract reverts.
Local refusal uses `RequestNotSent`, and a late completion cannot publish expired evidence.
This amends essential-refresh cancellation only; shared mutable safety and all entry checks remain.

## 2026-10-11 amendment: preparation before source buys

The approved [BSC entry-speed contract](2026-10-11-bsc-sub200-entry-design.md) adds default-OFF
warming from authenticated, factory-verified candidate facts through the existing bounded safety
worker. It retains the discovery subscription gate, current verdict TTLs, funding snapshots,
exact final sizing and signing checks. In the first rollout, warm reads waited for the existing pace
outside the shared request lock and cancelled when live work or essential refresh started. Interrupted or incomplete
checks stay unknown. Candidates grant no buy authority; the followed wallet's payment proof still
comes from the watcher. Original same-boot ingress stamps grade readiness; inline replacement
cannot inherit an older warm verdict. Shared mutable quote and round-trip checks remain live.

Beta, 2026-10-02. Operator order via Alpha: "BSC needs a fast lane, live as fast as possible".
This records the decisions. The code is the detail:
- `sniper/services/memecoin_bsc_fast_lane.py`: steps 1-3, each documented in its section header;
- `sniper/services/memecoin_bsc_v2_live.py`: the hop tape.

## 2026-10-11 amendment: unsigned native-float handoff

An exact native `bsc_fast_lane` attempt ending `aborted` / `float_short`, with its matching
failed `bsc_fast_float_short` reservation and no signing, submission, broadcast or cleanup
identity, no longer suppresses ordinary on-chain execution. The existing indexed duplicate
lookup carries that terminal state to the watcher, which ends its two-second hint hold
immediately. An exact unfinished native attempt holds the original detection
until it resolves or the source becomes stale. This pending state never authorizes funding.
Another matching feed/native or persisted ordinary decision takes precedence; explicit queued
retries retain their existing policy. Missing, malformed,
conflicting, signed or in-flight evidence does not qualify; pre-reservation misses retain
the existing timeout. The ordinary hook retains original source time and all funded gates.

This changes the historical all-attempt duplicate suppression for this one funds failure.
It does not retry a signed transaction, loosen a price cap, change native/WBNB allocation,
or replay historical detections. Shared fallback may quote and unwrap WBNB through its
existing executor; no provider call, wait, retry or lock is added to eligible native orders.
Proof: `docs/audits/2026-10-11-bsc-kedaengee-466e12da.md`.

## 2026-10-11 amendment: shared native failed-receipt settlement

BSC inherits the Robinhood lane's settlement tail. A canonical failed receipt now clears only
its exact live signed binding and releases the reservation in one database update. Conflicting
or malformed evidence remains unresolved; default bounded retry resets remain unchanged. The
failed receipt's known landing block is retained before optional replay, independently of missing
or invalid timestamp evidence. The exact `bsc_fast_reverted` resolution projects as a transaction
failure in the dashboard and Telegram. No entry calls, waits, locks, retries or price-policy changes
are introduced. Proof: `docs/audits/2026-10-11-rh-anr-0060e015.md`.

## 2026-10-10 amendment: split recovery and shared signing reuse

The native V2 leg recovery accepts the shared decoder's exact `split_route` reason as well as
`leg_does_not_bind`. The existing unique canonical PCS V2 leg, owner-net acquisition, tax,
successful receipt, Swap/Sync, source initiation and signed price-floor proofs still decide.
This does not price an unsupported V3 funding bridge or an unknown curve from V2 reserves.

When the BSC LIVE context already owns a fresh complete custody snapshot, ordinary quote-backed
entries may use its pending nonce through the same executor nonce floor. The callback is installed
locally at startup and copies the existing snapshot; it adds no polling or provider call. The
serialized signer validates chain/custody identity, nonce zero versus unknown, host-time freshness,
execution/broadcast generation and outstanding intents, then rechecks after the required gas read.
A malformed, stale, future, faulted, in-flight or floor-conflicting snapshot keeps the ordinary
nonce read. Accepted approval next-nonces and unwrap pre-transactions keep their existing signer.
Unsigned signing failures release only their owned floor claim; signed or ambiguous sends retain
the existing durable identity and recovery. Transaction value, calldata, gas policy, route booking,
admission, exact quotes/reverse sell, premium and preflight remain authoritative.

Warm Flap pre-entry reverse/safety quotes may try a previously verified allowance override before
the usually unfunded real sell. The immutable slot proof is keyed by mint, custody and spender;
amounts, curve state and exact simulation outputs are rebuilt for each call. Failed overrides or
fee-model refusals allow one real fallback. Cold discovery, injected callbacks and actual exits
retain real-first behavior. No estimated payout or mutable state cache is introduced.

`evm.entry_signing_snapshot` reports `used`, `fallback` or `fallback_recheck`. Quote-backed mixed,
curve and relay entries remain distinguishable from reserve-priced native V2. Rollback uses the
prior pinned release; no schema, provider, cadence or funded-policy activation is required.
Proof and measured limits: `docs/audits/2026-10-10-bsc-fast-route-coverage.md`.

## 2026-10-10 amendment: Flap shared receipt delivery and price reuse

The operator's new request extends LIVE receipt delivery to a Flap buy positively bound to the
known Portal's `TokenBought` event, exact successful mined transaction/receipt identity, the
direct signing wallet's positive native payment, and the actual acquired token flow. The
watcher uses its transaction and receipt already in memory; this adds no provider read. Unknown,
foreign, partial, ambiguous or unsupported evidence retains ordinary delivery. Shadow remains
V2-only. This changes the historical Flap delivery deferral below; a native receipt-priced Flap
builder remains deferred.

`flap_shared` uses the same funded hook and custody contexts as V3 shared delivery. Original
source arrival, `onchain:56:tx:mint` identity, source initiation, average-fill price, active/completed
delivery claims and ordinary retry policy remain authoritative. No V2 reserves, curve post ratio
or source-payment value is synthesized from the Portal event's quote/price fields. Exact live
quotes, mutable curve-state reads, sell simulation, premium/age/sizing gates, nonce allocation,
write-ahead identity and ambiguous-send recovery remain unchanged. The existing BSC LIVE mode
controls the handoff; no new authority setting is introduced.

Flap pre-entry safety also peeks the same executor's existing one-second BNB/USD memo instead
of fetching the same price again. Reuse requires canonical WBNB/BNB quote identity and a finite
positive price with age 0–1000 ms. Missing, stale, future-aged, malformed or failed peeks retain
the existing bounded live fetch. A pending price Future is never joined. `curve_price_ms` records
the actual price boundary. All fresh state/simulation reads remain. Rollback is the previous
pinned release; there is no schema, policy or configuration migration.

Proof: `docs/audits/2026-10-10-bsc-flap-fast-handoff.md` and
`docs/audits/2026-10-10-bsc-flap-safety-price.md`. Offline checks establish binding and duplicate
read removal. Organic post-deploy inclusion improvement and same-block landing remain unproved.

## 2026-10-09 amendment: cold safety overlaps inputs

After DopamineFeenFr1's native attempt spent 922 ms on inline safety and reverted on minimum
output, BSC reuses the Robinhood lane's single safety worker beside cold facts/market inputs.
Source proof, pilot limits, snapshot checks and the absolute source-buy band still precede
scheduling. The worker only reads: the funded hook still admits and reserves before the lane
consumes a verdict, caches a pass, or signs. Busy workers use the existing sequential check;
failed, expired and abandoned results never grant authority. All reads share the original deadline.

This changes the 2026-10-06 scheduling rule below: a coin later refused by market-cap or another
admission gate can finish an unused safety read. One worker bounds speculation; no new read type,
retry, queue or provider is added. Passing candidates perform the same reads. Warm hits are unchanged.
`bsc_lane_live.parallel_safety` records `used`, `busy` or `abandoned`; for `used`, `safety_ms` measures
the remaining wait after reservation, and display labels it "safety wait". Total decide-to-send,
not that remaining wait, is the latency comparison. Signed minimums and all funded gates remain intact.
An additive lane-file column permits rollback; the funded ledger and policy are unchanged.

Organic inclusion improvement remains unmeasured. A minimum-output revert can still occur when
prices move before inclusion. See `docs/audits/2026-10-09-bsc-dopamine-cold-safety.md` for the incident
identity, regression proof and bounded offline timing sample.

## 2026-10-03 amendment: V3 delivery and shared funded execution

The V2-only execution limit below changed after SHIELD position 69063 landed +4 blocks.
LIVE mode now sends a separate `route_kind=v3_shared` datagram for a V3 coin leg bound by the
existing source decoder to the actual owner, transaction and block. It carries the pool and
post/average ratio; it never invents V2 reserves for concentrated liquidity. Watcher PID/UID,
positive finite values and atomic/unit shapes are validated before dispatch.

V3 uses the ordinary funded hook, original custody executor, token safety, MCAP resolution and
retry queue. It does not use the experimental V2 pilot clip, native V2 builder or V2 safety verdict.
Warm market stats use the lane cache's existing TTL; cold or absent stats use the ordinary read.
Publishing stats alone grants no safety verdict. Read failures remain absent, not zero.

For BSC ordinary entries with existing early gate reads enabled, an attempt-local exact quote
starts beside the already-required market GET. The same measured ladder and round-trip budget
run. Reuse requires the same custody, chain, mint, USD clip, slippage and atomic sell input; a
nonempty quote must itself report the exact sellAmount. Existing executable preflight freshness,
winner refresh, chase, approval, write-ahead identity and recovery checks remain authoritative.
No price request is added: preparation needs the executor's already-warm price memo. Changed
clips, unavailable memo, stale results or invalid identities use the ordinary path.

A matching unfinished quote uses its original ladder deadline. A completed or deadline miss
preserves its typed rung reason and does not start a second full ladder. Background completion
cannot change a consumed timeout envelope. Results belong to one attempt and are used once.

The ordinary watcher and V3 lane keep the same `onchain:56:tx:mint` origin. Active and completed
delivery claims are scoped by that origin plus source_user_id under the existing decision lock.
Completion publication and claim release are atomic; the completion cache is capped at 4096
entries with a 120-second monotonic TTL. A duplicate writes no terminal outcome and makes no
provider read. Explicit retries bypass completed markers but still honor active owners; a busy
owner leaves the durable retry/dip row eligible. An exception releases without marking completion.
The cache is a performance aid; reservations and signed identity remain the money authority.

Rollback: previous pinned release; `MEMECOIN_ENTRY_EARLY_GATE_READS_EVM=0` disables quote overlap
with its existing gate-read behavior. V3 delivery follows the existing BSC LIVE-mode setting.
No schema or funded admission policy changes. Shadow's native V2 builder remains V2-only.

This removes avoidable serial/duplicate work, not all market/quote waits. Cold required data,
exact pricing, safety, approval and inclusion still take time. Receipt-based detection cannot
guarantee the source block. Organic post-deploy block-gap percentiles remain unmeasured.
Proof and runnable checks: `docs/audits/2026-10-03-bsc-v3-shared-fast-lane.md`.

## v1 (built and merged; LIVE verified 2026-10-03)

### 2026-10-06 amendment: cold admission before safety

Cold V2 signals now reuse Robinhood's bounded, attempt-local facts/market overlap, then run the
shared funded hook's admission gates before inline token safety. The hook reserves the coin
before checking safety; failure releases that unsigned claim through the existing path. An
over-ceiling coin therefore cannot spend the inline safety budget. Warm verdicts are unchanged.
The existing deadline covers inputs and safety together; a busy market worker returns immediately,
and late reads cannot publish a verdict. Snapshot/generation revalidation still runs before signing.
No new provider request, synchronous lock or retry was added. Missing inputs still fall back.
Offline scheduling with fixed 40/30/60 ms facts/market/safety reads, 12 paired samples: old
p50/p90 130.5/130.6 ms; new 100.6/100.7 ms. These are scheduling measurements, not live fills.
Rollback is the previous pinned release; no schema or sizing-policy change.

### 2026-10-04 amendment: verified bad-token admission and sell evidence

Operator authorized protection after two Frank-labelled BSC buys became unquotable. Both exact
Uniswap V4 pools had liquidity withdrawn by the same address 13/140 seconds after our buys;
this is not proof of a token honeypot or source-wallet ownership of the liquidity.
`MEMECOIN_REAL_BLOCKED_TOKENS` rejects these chain-specific mints in the shared reservation gate,
including the V2 lane's calls, even with a risk-cap override. It does not impede exits.
The BSC safety factory now requires the router-sell simulation to succeed rather than admitting
an unknown revert on an isolated token pull. BSC also refuses an older confirmed-exit override of
the failed current check. Other chains retain that exception; explicit manual overrides remain
separate. Passing safety traces preserve the positive admission reason.
There is no added network call, retry, lock or DB read. This does not predict future liquidity
removal from a new token; pool-controller screening would require separately collected evidence.
Proof: `docs/audits/2026-10-04-bsc-stuck-69487-69489.md`.

- **Decide in the 56 watcher** from the source receipt's own Sync on the coin's PancakeSwap V2 pair.
  The pair is accepted only by CREATE2 (factory plus quote). A non-WBNB quote is a second hop,
  WBNB -> quote. Its state comes from the receipt, else from the bsc_v2_live tape at lag 0, else the
  signal falls back.
- **Min out** = max(cap rule, exact expected output x (1 - MEMECOIN_ENTRY_SLIPPAGE_PCT/100)).
  - Cap = min(post x (1 + chase), source avg x premium).
  - The hop is priced at fee-inclusive spot under the cap.
- **Runner lane:** the funded hook's gates, then the 4663 lane's executor:
  - a native-BNB PCS V2 fee-on-transfer buy;
  - one parallel send to Puissant, the chain owner's endpoints and BlockRazor;
  - booking from the receipt, with a block-pinned delta fallback;
  - recovery from calldata.
- **Live mcap** = chain totalSupply x exact post price.
- **The lane's own host:** safety, facts and snapshot read only `MEMECOIN_BSC_FAST_LANE_RPC_URL`,
  refused at start when its registrable domain is used by any other role. A cache miss is checked
  inline on that host.

## v1.1 split routes (scoped 2026-10-02; see the commit for the build)

**Measured on 79 armed-source 56 detections, 2026-09-24 to 10-02.** 11 fell back as
`leg_does_not_bind`:
- 5 distinct coins; 6 of the 11 are one coin.
- All 11 are Relay-solver SPLIT routes. The solver buys the coin from our CREATE2 PCS V2 pair AND
  from other venues in the same transaction: a `0x28e2ea09...` contract emitting `0x40e9cecb`
  swaps, and unrecognised senders. So the owner's net coin is 1.0003x to 2.12x the pair's payout.
- `source_post_trade` (shared with the funded path's chase reference) correctly refuses to bind
  one leg to the whole buy.

**The lane does not need the whole buy:** it buys on the V2 pair, and that pair's own Swap, Sync
and Transfers in the receipt give its exact state, its leg's average and its tax. The extension
is lane-local:
- when `evm_post` says `leg_does_not_bind`, the lane re-derives the coin's single V2 leg from the
  receipt;
- it accepts the leg when the owner received at least 0.9x of that pair's payout, so the pair is
  part of the buy;
- it then runs v1 unchanged.

The shared decoder and the funded path are untouched.

**Coverage:**
- 4 of the 11 (3 coins) are quoted in configured hop quotes (`0x9b8e987e`, `0x205812cd`).
- The other 7 (2 coins) are quoted in `0x2f8a339b...` and `0x21caef8a...`. No WBNB pair of these
  appears in any receipt, and their existence is unverified. They stay `hop_state_stale` until
  those quotes are measured and added to `MEMECOIN_BSC_FAST_LANE_HOP_QUOTES` (config, not code).

## v2 flap curve: DEFERRED (Alpha 2026-10-02)

**Decision.** Deferred until the v1 pilot has run on real money:
- About 8% of 56 detections are curve-stage buys.
- Every sampled one is quoted in a non-native ERC-20.
- The workable option costs an inline read per signal, plus new safety and recovery paths.

**Sizing (read-only; 25 receipts on 1rpc.io/bnb at 1.2 s, no 429).**

| class (79 detections since 09-24) | n |
|---|---|
| flap-launched mints (`...7777`) | 73 |
| on PCS V2 at detection (graduated; v1 scope) | 48 |
| `leg_does_not_bind` (graduated split routes; v1.1) | 11 |
| non-V2, of which 20 receipts were read | 20 |
| curve-stage Portal buys in those 20 | 4 |

The 4 curve buys were quoted in `0x02fca66c` (x2), `0x7ddef360` and `0x4902c5eb`. None was
native-quote.

The 58 funded "no route ... flap_curve" aborts all date 2026-09-20 to 09-23, while
`MEMECOIN_EVM_FLAP_CURVE_ENABLED` was off. Funded 56 has filled 2 through the Portal since.

**Receipt math.** Decoded on `0x0bdb3e3f46c1751de238eeb540085e5a2e8ca6cad46f39d841d377145d7dd19d`:
- 2026-09-30T15:51:45Z, coin `0x65b7e0fbbb741234b1a17e7e6f4c742ae8147777`.
- Quote `0x02fca66c1d1afb4e2a7884261eb00f63598a7436`.
- Paid through the Relay router `0xccc88a9d...`.

The Portal (`0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0`) emits:

| topic0 | event | this buy |
|---|---|---|
| `a800a203...` | `TokenBought(uint256 ts, address token, address buyer, uint256 amount, uint256 quoteIn, uint256 fee, uint256 postPrice)` | amount 22,683,831.52 coins; quoteIn 3.220843880568898; fee 0.032208 (1%); postPrice word `0x226fbe662c` |
| `115c78ad...` | `FlapTokenCirculatingSupplyChanged(address token, uint256 supply)` | circulating 746.0M coins after |
| `4c35e20d...` | `FlapTokenProgressChanged(address token, uint256 progress)` | 0.795 x 1e18 |
| `b4aa5d6b...` | (token indexed, uint256): equals the 1% platform fee | 0.032208 |

- The fee is exact from the quote's transfers out of the Portal: 0.0322 to `0x8a08d98c` and 0.0322
  to `0xc3ea52d4`, so 2% total on this coin (1% platform + creator).
- The coin transfers carry no tax on this buy. `c4f02cd4` is a transfer-hook event (from, to,
  amount).
- Curve post-state, with the per-mint constants h, r, k and dexSupplyThresh read once by
  `getTokenV6` (`evm_flap_curve.curve_state`):
  - x = 1e27 - circulating_post;
  - y = k x 1e18 / (x + h) - r.
- A buy's curve leg: out = x - (k x 1e18 / (y + net_in + r) - h), with net_in = quote_in x (1 - fee).

**First test when this is picked up:** the formula with this coin's getTokenV6 constants must
reproduce the TokenBought amount and postPrice of the fixture above, before any decide code. The
postPrice scale and the quote token's decimals are unconfirmed.

**The open leg.** Every sampled curve buy is non-native-quote. The Portal converts native to quote
internally (`nativeToQuoteSwapEnabled`), and that conversion is not in the source's receipt: the
source paid quote it had bought with USDC over Relay. Options, ranked when deferred:
- **(b) Recommended.** Decide on the receipt:
  - a USD cap from postPrice and quote_usd = paid_usd / quote_in;
  - graduation refused at progress >= 0.98, or when our out would reach dexSupplyThresh.
  Then run one inline exact Portal `swapExactInput` simulate on the lane host
  (`evm_flap_curve.simulate_swap_exact_input`), with min_out = max(cap rule, exact x 0.97).
- (a) Identify the Portal's internal native-to-quote pool. If it is a PCS V2 WBNB/quote pair, reuse
  the v1 hop.
- (c) Native-quote only: 0 of 4 sampled.

**What v2 also needs:**
- The lane's safety cache must quote flap exits (`wrap_token_safety_fn` /
  `dex_flap_curve_exit_quote` on the lane host). Today it quotes V2 exits only, so every curve coin
  would fail safety.
- Booking with `source=Portal`.
- Recovery decoding `swapExactInput` calldata.
- A watcher decide branch on `TokenBought` for the mint.
- A sub-knob, default off.

**Graduation edges:**
- A receipt showing migration, or reserve 0: refuse.
- The curve completes before our transaction lands: the Portal routes a buy to DEX (verified on
  Taraxacum, status 4). min_out bounds the fill, so the worst case is a revert costing gas.
- The status enum is undocumented: use the supply threshold, as `evm_flap_curve.is_graduated` does.

## Amendment 2026-10-09: Alpha handoff completion

MEMECOIN_BSC_FAST_PARALLEL_SAFETY defaults ON. Setting 0/off/false/no restores sequential safety after inline facts; it never disables safety. Flap receipt-native booking requires exactly one valid positive 32-byte Withdrawal from the Portal in our own Portal call. Missing, malformed, zero or multiple matching withdrawals remain unknown. Curve-phase no-unwrap proceeds remain unpriced.

Evidence and limits: `docs/audits/2026-10-09-codex-alpha-handoff-closeout.md`.

## Amendment 2026-10-10: native priority and cold quote readiness

In live mode, the watcher offers the native lane before publishing/waking ordinary execution. A successfully delivered live hint persists `bsc_fast_lane_sent`; ordinary execution defers asynchronously for at most two seconds from original source arrival while existing attempt deduplication remains authoritative. Shadow signals retain their existing publication-first order; failed sends/shadow mode do not hold ordinary execution. Lost hints expire without resetting age or rewinding the cursor.

With existing early cached sizing enabled, a cold gate cache can use the existing BSC wallet snapshot for speculative quote sizing. Its age, spend generation and USD price must be valid. A cold price uses the existing warmer on the private worker, sharing the in-flight fetch with other consumers and preserving the one-second price window. Actual funded gates/accounting retain live balances and exact clip binding. Mixed V3/V2 routes remain shared executor routes. Evidence: `docs/audits/2026-10-10-evm-fill-incidents.md`.

### Amendment 2026-10-14: BSC early quote sizing

BSC now sizes the speculative quote from the same attempt's existing balance preread, replacing
the cached sizing above. Position #73853 quoted the cached $121.30 clip while the funded gate
approved $121.32; strict clip binding correctly discarded that quote. The background quote worker
joins the already-running read; it adds no balance RPC or wait to the entry thread. Other chains
retain cached sizing. A cold read can start the speculative quote later; final sizing, quote binding,
spend generation and minimum output checks remain unchanged.

## Amendment 2026-10-10: entry pipeline evidence

Canonical BSC receipt decoding is bounded and asynchronous; receipt-native eligibility still
uses original receive time and all initiation/tax/reserve/premium proof. V2 multi-path shared
quotes batch the exact existing reads with strict reply identities and typed unavailability;
explicit unsupported capability is remembered per endpoint. Single-path mixed legs remain scalar.
Flap shared safety reuses one fresh copied identity-bound curve state inside each check while
retaining exact simulations, conversion/graduation/creator-fee rules and rereads on the next check.
This does not create native V3/Infinity/converted-Flap reserve evidence. Current mixed split-route
support requires a unique eligible V2 coin leg; a V3 funding hop still uses the shared executor.
Evidence and native census: `docs/audits/2026-10-10-bsc-entry-pipeline.md`.
