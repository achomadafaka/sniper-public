# Robinhood 4663 fast lane: a funded fill one or two blocks after the source buy (design)

**Author:** Beta. **Order:** the operator, via Alpha, 2026-10-01 ~19:00Z: "Fix Robinhood speed so we
land same slot; don't stop until achieved." Alpha assigned Beta end to end (feed, decode, decide,
send); Lambda stays on the Sender A/B. Gamma measures blocks-behind at each milestone.
**Status:** PROPOSED, spec only. Phase A (spec 2026-10-01-memecoin-evm-entry-speed-design.md,
merged 34c7974cb) keeps improving today's path, which stays the fallback.

**2026-10-04 source-pricing amendment:** Current funded consumers require a complete receipt-backed
wallet-net fill bound to chain, UID, wallet, transaction, block and mint before using a fast-lane
source price. Calldata payment permission and one pool Swap output do not establish those economics.
An off-path read-only cache loads the existing receipt decoder's proof; missing or ambiguous proof
falls back immediately to the ordinary path. Full-receipt source pool state replaces an unrelated
pool's modeled source trade. Shared routes keep the verified wallet average and abstain on the
gross-pool post ratio until its units are bound to wallet-net payment. Native caps use the exact
receipt WETH atoms, never detection-time USD divided by a later ETH/USD value; native post USD
references use the actual receipt pool price and current ETH/USD. Native transaction value without
refund proof is an upper bound and cannot supply an exact fast-lane average. Unexplained separate
quote outflows retain the observation without complete payment proof. No additional provider reads
are introduced. The original latency targets remain targets; this change needs organic proof of
coverage and latency before a speed claim. See the receipt-proof addendum and dated repair audit.

**2026-10-10 research-response amendment:** The background research HTTP seam distinguishes
external bodyless REST price GETs from JSON-RPC requests. A healthy price object no longer counts
as malformed RPC evidence. HTTP/transport failures remain provider failures, including a timeout
swallowed by an optional price reader; malformed/partial RPC responses still fail closed. No
research quota, pacing, retry, slot rotation, funded safety gate or entry request changes. This
allows actual route refusals to be cached without manufacturing a `ResearchPaused` episode.
Proof: `docs/audits/2026-10-10-rh-research-response-classification.md`.

**2026-10-11 failed-receipt amendment:** A canonical native status-zero receipt clears its exact
live signed binding and sets the reservation's terminal failure reason in one database update.
There is no intermediate unsigned reservation that a recovery actor can rebind. A mismatched
identity, moved status or malformed proof stays unresolved. Default reset-for-retry semantics
remain unchanged. Proven failed receipts retain their landing block before optional revert replay;
an unavailable timestamp keeps time unknown. The shared operator projection recognizes the exact
`rh_fast_reverted` and `bsc_fast_reverted` resolutions as transaction failures. No price limit,
funded admission rule or entry-path request changes. Proof: `docs/audits/2026-10-11-rh-anr-0060e015.md`.

**2026-10-14 native submission amendment:** Native 4663 entries use the chain context's existing
funded same-bytes submission transport when configured, after the native signed identity is
durably bound. Custody and eligible official endpoints receive identical bytes concurrently;
only an identity-matching acceptance wins. Malformed signed chain/hash, binding conflicts,
timeouts, unavailable providers and wrong returned identities retain an unresolved owner.
Provider audit queues after transport return, including late outcomes under their original owner.
The native recovery backend tag remains compatible. Explicit injected senders and no-transport
rollback retain their existing path; BSC is unchanged. The submission-only sequencer is warmed
on the actual pooled sending client without granting identity/health authority from a refused
read. No additional entry RPC, approval, signing, fee, safety TTL or recovery deadline change.
This replaces the sequencer-only dependency below; earlier organic inclusion remains unproved.

## 0. Answer first

**Same block is not reachable from any public signal. The floor is the next block, N+1.**
- The public sequencer feed publishes a block only after the sequencer has SEALED it (§1.1). By the
  time any outside process sees the source's buy, its block is closed.
- Robinhood (Arbitrum Nitro) has no public mempool. Submission goes first come, first served to the
  sequencer, so nothing outside the sequencer sees the transaction earlier.

**Targets (agreed with Alpha):**

| where the lane runs | target | what decides it |
|---|---|---|
| this host | **N+2** at a ≤50 ms decide budget; **N+1 stretch** at ≤10-14 ms | ~53 ms feed one way + decide + ~53 ms send one way, against a ~120 ms block |
| a decider colocated in AWS us-east-2 | **N+1** | the two network legs drop to ~1-2 ms each (§5) |
| today | median **N+23**; 0 of 20 fills at ≤ N+2 (Gamma's scoreboard, current regime since 2026-09-28T12:30Z, n=20) | the 7-day figure, N+44, mixes the old regime |

**The payoff (Gamma's read).** Of 416 buyers ahead of us, 401 were in LATER blocks than the source,
not in the source's block. Landing at N+1 or N+2 puts us ahead of nearly all of them.

**Lane outline.** A new in-runner thread, `rh_fast_lane`, default off:
1. It consumes the sequencer feed directly.
2. It decodes armed wallets' Relay buys from calldata in ~0.2 ms (§1.4). It accepts a buy only from
   a known Relay solver sender, with a live permit (§2.1, Gamma HIGH).
3. It decides in memory against precomputed per-mint state: route, coin gates, a completed
   token-safety verdict, decimals.
4. It sets an on-chain minimum output from the price caps.
5. It signs a Universal Router buy paid with native ETH (`msg.value`), so it needs no approval and
   no Permit2. It writes the identity ahead, then sends the bytes straight to the sequencer on a
   warm connection.

Anything missing, stale or unknown falls back to today's path at once. Safety is never skipped.

## 1. Evidence (all read-only, 2026-10-01)

### 1.1 The feed is per sealed block

A 60 s sample of `wss://feed.mainnet.chain.robinhood.com` (block header timestamp 1790881787,
2026-10-01T19:09:47Z) showed:
- 591 messages with contiguous `sequenceNumber` (77614300 → 77614890, 0 gaps), one message per
  frame;
- every message carried `blockHash` and `signatureV2`, i.e. a sealed L2 block;
- message kind 3 (an L2 batch), p50 11 / max 53 transactions per block.

### 1.2 Block cadence

Inter-arrival p10 / p50 / p90 was 12 / 120 / 124 ms, ~9.9 blocks per second. This matches the
research QuickNode note (10 blocks/s, whole-second timestamps) and Gamma's 44 blocks ≈ 5 s.

### 1.3 Network

- `sequencer.mainnet.chain.robinhood.com` resolves to AWS us-east-2 (3.136.74.196, 3.141.111.43,
  3.142.9.34). TCP connect from this host is min 106.3 / p50 107.6 ms, about 53 ms one way.
- The feed and the public RPC are behind Cloudflare (edge RTT 8-10 ms). Their origin is presumably
  beside the sequencer, so a feed message reaches us about 50-60 ms after sealing. **This is an
  estimate.** Milestone M0 measures it (§6).
- Today's sends report sequencer acceptance at ~200 ms (`evm.broadcast` p50 203 ms). That is a full
  round trip plus connection reuse or setup; it is not one way.

### 1.4 Who the sources are and what their transactions look like

- **All of them are Relay fills.** All 194 armed-wallet 4663 buys detected on chain since 09-14 (11
  wallets; 0xdetweiler 29, DegenCapitalLLC 25, probablyinsider 18, …) were fetched from the research
  QuickNode at 0.5 s spacing. Every one has `to = 0xccc88a9d…` (`RELAY_APPROVAL_PROXY`), selector
  `0x0a2b8f36`, value 0, and `tx.from` = the Relay solver, never the wallet.
- **The calldata alone identifies the buy.** The Permit2 envelope (`PERMIT2_TYPES` in
  `memecoin_evm_onchain_watch`) carries:
  - `refund` = `nft` = the wallet;
  - the permitted payment token and amount (USDG `0x5fc5360d…` in the sample, 994,518,588 atomic);
  - three calls: approve, swap through the Relay/Kyber adapter `0x0da5899d…` (`998b5942`, 10.6 kB of
    route data), and cleanup through `RELAY_ROUTER` (`9bb43718`), whose token list holds the bought
    mint.
- **The solver set.** The 194 fills came from 31 distinct solver addresses. The set is stable:
  every one of the 97 later fills used a solver already seen among the 97 earlier ones. Gamma's 47
  read1 sources (36 J7, 11 on-chain) are also all Relay.
- **Decode cost.** `eth_abi` decodes the envelope in 0.137 ms and RLP-decodes a typed transaction in
  0.024 ms. Recovering a sender costs 4.6 ms in our pure-Python ECC. The lane recovers it only for
  candidates that already passed the cheap filters (§2.1).
- **Consequence for "mirror the source's route."** The source's route is Relay → Kyber calldata →
  pools. Copying it means running Relay's solver path. The fast lane instead uses the mint's
  precomputed best DIRECT pool (§2.3). The source's executed pool, decoded later from its receipt as
  today, is recorded for grading.

### 1.5 The coins are already trading before the signal

Of 473 4663 decisions since 2026-09-29T22:30Z with tape flow features (`entry_features.flow_300s`),
471 had at least one swap on the coin in the 5 minutes before the decision (p50 107 swaps, p10 9).
A pre-signal verdict is therefore possible on almost every coin. The tape is wide, though: ~440-510
distinct mints trade per 10 minutes (~182k swaps per hour), so the precompute set must be narrowed
(§2.2).

### 1.6 Where today's 44 blocks go

- watcher detect latency p50 0.72 s / p90 1.08 s from block time (±0.5 s: whole-second timestamps);
- decide → broadcast accepted p50 1.85 s (Phase A spec);
- the rest is J7-lane lateness (seconds) and inclusion.

The fast lane removes the first two for eligible signals.

## 2. Shape

### 2.1 F1: feed consumer and decoder (fast path)

- **Process and connection.** One WebSocket to the feed, held by a thread inside the funded runner.
  The decision needs the wallet key, the nonce floor and the ledger, and a cross-process hop would
  cost time.
- **Filter per block.**
  1. RLP-decode each transaction's `to` and the first 4 bytes of its input.
  2. Keep only `to == RELAY_APPROVAL_PROXY` with selector `0x0a2b8f36`.
  3. ABI-decode those (strict canonical decode, as `relayer_purchase_from_receipt` does today).
  4. Require `refund == nft ==` an armed 4663 wallet in the in-memory roster.
- **Authenticity (Gamma HIGH).** The feed carries sequenced calldata with no receipts. A sealed
  block includes reverted transactions, and `refund`/`nft` are plain calldata, so anyone can send a
  Relay-shaped transaction naming an armed wallet and their own mint. It reverts at Permit2 but still
  appears in the feed. So a candidate is a signal only if ALL of these hold:
  - **(a)** The sender, recovered from the signed bytes (4.6 ms, candidates only), is in the
    known-solver allowlist. The list is learned only from Relay fills the watcher has already proved
    from receipts (`read_relayer_purchase`). An unknown sender falls back to today's path, which
    proves it from the receipt and then teaches the allowlist.
  - **(b)** The permit deadline is at or after the block's header timestamp.
  - **(c)** It is not a duplicate: dedupe on (wallet, Permit2 nonce) and on tx hash.
  - **(d) (Gamma MEDIUM)** The Permit2 payer is a known Relay payer.
    - All 194 proven fills have ONE payer, `0xf70da97812cb96acdf810712aa562db8dfa3dbef`: Relay pays on
      4663 from its own funds after the user's deposit elsewhere.
    - The payer is never the wallet. Today's receipt decoder already rejects payer == wallet
      (`relayer_purchase_from_receipt`), so "payer equals the armed wallet" would refuse every genuine
      fill.
    - The payer check stops a non-Relay forgery that reuses the envelope shape.
- **The residual risk, a "gift" (stated, not solved).** A real Relay user can name an armed wallet
  as `refund`/`nft`. A solver then executes it with Relay's own payer, so on chain it is
  indistinguishable from the wallet's own buy.
  - The bait: someone buys their own coin "for" a famous wallet, and copiers buy behind it.
  - **Today's on-chain lane carries the same exposure.** `read_relayer_purchase` proves a Relay
    delivery to the wallet, not that the wallet asked for it. J7 (Fomo's own record of its user's
    trade) is the only authenticated source.
  - **M0 measures it.** For every feed signal, M0 looks for a J7 event for the same (wallet, mint)
    within 60 s. The share with no J7 match is an upper bound on gifts plus other-initiator fills.
  - **Operator ruling at M2.** Either accept that measured rate, or require J7 corroboration (seconds
    late, which defeats the lane). The Relay request's origin user (Relay API) can be checked offline
    in the M0 grader. It is not usable on the fast path.
- **A genuine fill can still revert** (the source's own slippage), and we would then trade on a buy
  that never happened. Nothing before the receipt can rule it out. M0 records the share of feed
  signals whose source receipt later failed, and that cost is charged in M2's grade.
- **Buy, not sell.** The permitted payment token must be a quote asset of the chain (USDG, WETH as
  configured), and the cleanup token must not be one. Anything else is not a buy and is ignored.
  - A Relay SELL spends the coin and receives the quote asset, so it fails this rule, never the
    other way round.
- **Contract code.** The Relay contracts' code hash uses the existing cached verdict
  (`warm_relay_code`, `MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S`). An expired or unknown verdict sends the
  event to today's path.
- **Source size in USD.** It comes from the payment amount (USDG at 6 decimals, or WETH ×
  ETH/USD from the warm price). The source's received quantity and average fill are NOT known
  without the receipt (§2.4).

### 2.2 F2: precomputed per-mint state (off the fast path)

**The worker.** A background worker keeps a candidate set and, for each candidate, a snapshot
written whole and read by reference:
- the best direct route: the pool and its identity (a V4 PoolKey from `memecoin_evm_v4_pool_keys`,
  or a V3/V2 pool address);
- the coin-gate inputs: mcap from tape price × cached supply, volume from the tape, coin age, sell
  share;
- token decimals;
- a token-safety verdict and its completion time;
- the pool's latest state from the tape: `sqrtPriceX96`, liquidity and tick from the last V3/V4
  Swap event, with its block number.

**The candidate set.** A mint enters when it trades on the tape and any of these holds:
- an armed wallet bought it in the last 60 minutes (any lane);
- it is in the activity top-K by swaps over 5 minutes AND passes the coin gates computable without
  the signal (mcap band and ceiling, volume floors, coin-age floor).

It leaves after 10 minutes without a swap. V2 pools are out of v1: the 4663 tape records
`v3_swap` and `pons_v2_swap` only (24 h kinds, 2026-10-01), with no V2 reserves (Gamma LOW 3). K, the TTLs and the refresh cadence are knobs; shadow M1
measures them against load.

**Token safety.** It runs today's `token_safety_fn`, unchanged, with `usd` = the clip.
- It runs on entry into the set, and again every `MEMECOIN_RH_FAST_SAFETY_TTL_S` while the mint is
  in the set (start at 60 s).
- A verdict older than the TTL counts as absent.

This deliberately departs from the 07-28 invariant "no token-safety cache on the funded path" (the
operator's Phase C order). It is bounded by the TTL, and the on-chain minimum output (§2.4) limits
what a stale verdict can cost.

**Load.** The worker's reads use the same hosts as today's safety and ladder. Its call rate is
counted through the funded RPC call counter (d83c8b63e). A 429 rise pauses the worker, which empties
the fast lane: every signal then goes to today's path.

### 2.3 F3: route and template (fast path, in memory)

- **One router, paid in native ETH.** The Universal Router on 4663 (`0x8876789976decbfcbbbe364623c63652db8c0904`),
  `execute(commands, inputs, deadline)` with `msg.value` = the clip in wei:
  - V4 native-ETH pool: `V4_SWAP` with SETTLE from `msg.value`;
  - V3 WETH pool: `WRAP_ETH` then `V3_SWAP_EXACT_IN` with `payerIsUser = false`.
  - V2 pools are not in v1 (§2.2).
- **No approval and no Permit2 on this lane.** That takes C5 (a standing allowance) off the
  operator's list for the fast lane (Alpha 2026-10-01).
- **Verify the encodings by `eth_call` on 4663 before any build.** 4663's V4 `ExactInputParams` has
  five fields, and a struct written for one chain broke on another (trap db0e84dc). The V3/V2
  commands on 4663's router are unverified.
- Pons pools, launchpad curves and flap curves are not on this lane (v1). They go to today's path.
- **The template** holds calldata with placeholders for amount, minimum output and deadline, plus
  the chain id and gas limit. It is rebuilt off the path when the route changes.

### 2.4 F4: price and minimum output (fast path)

No quote is fetched. The minimum output enforces the price gates on chain.
- **Pre-trade state.** The candidate snapshot's pool state comes from the latest tape swap at a
  block before the source's block B. When the tape already holds the source's own swap in that pool
  (the tape sees a tx ~18 ms before the feed, §2.8), its post-swap state is used instead: exact.
- **Staleness is the tape's lag, not the pool's quiet (M1 correction, 2026-10-01).** On 14,573
  native V4 buys on the tape, the pool's previous swap was p50 20 / p90 182 blocks earlier; a
  pool-age bound of 8 would have refused ~75% of buys whose state was current. The bound is on the
  tape's newest block (any pool): more than `MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS` (8) behind B
  counts as absent. Liquidity adds and removes on a quiet pool are not modelled; M1's cap ratio
  measures what that costs.
- **Estimated post-trade price.** Apply the source's swap to the pre-trade state with single-range
  concentrated-liquidity math. The source's input is taken from BELOW: the payment amount minus the
  maximum Relay and Kyber fees. A smaller input means a smaller estimated impact and a lower cap.
- **Price cap.** max_price = min(estimated post-trade price × (1 + chase cap), source price ×
  `MEMECOIN_REAL_MAX_EXEC_PREMIUM` (1.5 in `.env`; code default 0 = off)). The source price is the source's average implied by the
  same math.
- **Minimum output** = floor(clip_in_quote_atomic / max_price), in token atomic units with the
  snapshot's decimals.
- **The error is two-sided (Gamma MEDIUM).** Today's reference is the TRUE post-trade price × (1 +
  chase cap), read from the receipt. Our estimate can differ either way:
  - **Too strict, the safe direction.** A swap in block B before the source, or a tick crossing into
    deeper liquidity, can leave the true post-trade price above the estimate. Our cap is then below
    today's, so we revert (gas only; 4663 gas is about 0.03 gwei) or fill cheaper.
  - **Too loose.** If the pool fell (sells) between the snapshot block and B, the pre-state is above
    the true pre-trade price, and our cap can sit above today's.
  - **Bounds on the loose side:** the state-age limit (8 blocks), the input taken from below, and
    the absolute cap of source price × `MEMECOIN_REAL_MAX_EXEC_PREMIUM`.
  - **Measured, not assumed.** M0 records, per signal, our cap price against today's cap price from
    the source receipt. M2 goes live only if that ratio's p90 is ≤ 1.00 (no looser than today at the
    90th percentile), else the state source must improve first.
- **Record for grading.** The actual post-trade price, from the source's receipt decoded later as
  today, is recorded beside the estimate.

### 2.5 F5: admission, sizing and reservation (fast path)

- **In-memory gates.** Wallet armed on 4663 (roster cache refreshed every 10 s), kill switch, the
  source-buy floor and band (§2.1 size), the coin gates from the snapshot, one position per coin,
  concurrency, exposure and bankroll.
- **Reservation.** Everything goes through `reserve_entry` (BEGIN IMMEDIATE) on the lane's own
  ledger connection, so capacity stays serialized with the main loop.
- **Clip.** The tier clip (`MEMECOIN_EVM_TIER_CLIP_USD_4663`, today 100,100,170) converted with the
  warm ETH/USD price.
- **Float.** If the wallet's native ETH float, minus the gas reserve, is below the clip, the lane
  falls back to today's path at once and never waits.

**Float sizing (Alpha asked).** At least the largest tier clip plus gas, refilled off the path by the
gas pass (C2 knob, `MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE`). Today that is $170 plus gas.
Entries are serialized by the wallet lock, so one clip in flight is the most at any instant.
- A refill cycle is up to 300 s (`MEMECOIN_EVM_GAS_TOPUP_PERIOD_S`), so back-to-back entries inside
  one cycle need about 2 clips; the second otherwise falls back.
- **Recommendation:** float = 2 × the largest tier clip + the gas reserve. One clip is enough to
  start; the shadow counts float-short fallbacks.

**Amendment 2026-10-01 (Gamma's BLOCK on d6e567683; Alpha's order).** The in-memory gates above
are NOT re-implemented in the lane. After its in-memory checks the lane builds a `LaneEvent` (the
on-chain lane's `OnchainEv` shape: the watcher's paid-size and paid/received price basis, the
source's post-trade facts on its pool, the mcap at the tape's post-trade price carried as the
event's own) and runs the runner's own funded hook, `make_real_hook(...).attempt`, over a lane
context: the snapshot's balances, the safety cache's verdict and the lane's executor. Every gate a
4663 entry passes today therefore runs exactly as it does there: roster and per-chain arming, the
per-wallet mcap ceiling and floor, staleness, the source-buy floor and band, the chase gates, the
volume family, sell share, coin age, the band; then the leg's kill switch, arming, quiet hours,
admission, reservation and safety. Only then is the lane's executor called (the hook's own 4663
executor is never reached; a test makes it raise).
- **Gate market data.** The hook's market-data read is today's `dexscreener_stats`, prefetched by the
  safety thread for each candidate after a passing verdict, TTL 60 s. Absent or stale means
  fallback before any funded row. It is gate data only: the lane's min-out, cap, booking and grading
  never read it (pinned by a test). Every lane request is counted; a 429 stops the lane's reads for
  the life of the process.
- **Staleness cost.** In runner_shadow, after each hook verdict one fresh `dexscreener_stats` read on
  a background thread re-judges the same signal; `rh_fast_lane_stats_checks` records whether the
  verdict would differ. Gamma reads that rate before live.
- **runner_shadow** runs the same hook on a read-only ledger connection inside a rolled-back
  transaction, stopped before the leg as a J7 shadow decision is: no funded row is written.
- **No revival.** A lane refusal never arms a revival watch (`no_revival`): today's lane for the
  same buy owns the one re-ask.
- **Refusal pages.** The lane's refusals do not page (today's lane pages the same buy); fills and
  failed sends still do.
- **Measured cost (2026-10-01).** The hook's gate stage on the production ledger, read-only, J7
  stop: p50 11.9 ms, p90 14.1 ms over 40 runs, no network attempt; the first call in a process was
  376 ms (cold). Harness decide-to-send (tmp ledger, 60 sends): p50 5.00 → 5.95 ms, p90 5.08 →
  6.15 ms.
- **Warm (Alpha 2026-10-01).** The lane thread runs one shadow verdict at start, off any signal
  (`warm_hook`: an armed wallet's synthetic buy of a coin no ledger row names, read-only, rolled
  back). Measured on the production ledger: the warm 308 ms, then the first real verdict 18.2 ms,
  p50 14.2 / p90 16.1 ms over 40.

### 2.6 F6: sign, write ahead, send (fast path)

- **Nonce.** From the executor's nonce floor (the same allocator the approval pipeline and the
  pre-approval share), taken under the same signing lock. If the lock is held by another entry or
  exit, the lane falls back and never waits.
- **Fees.** `maxFeePerGas` = the cached base fee × 2, refreshed every ~1 s off the path by one read.
  `maxPriorityFee` = 0 (FCFS: paying more does not jump ahead, per Robinhood's docs, 09-27 spec §3).
- **Signing.** In process: 3.1 ms with today's `eth_keys` native backend. `coincurve` would be about
  0.2 ms, but installing it changes eth_keys' backend for every signer in the process. That is an
  operator decision, measured first; v1 does not need it.
- **Write-ahead.** The signed identity goes through the existing write-ahead contract before any
  network I/O. A missing identity, timeout, transport loss or crash after send is
  `submitted_unknown`, as today.
- **Send.** POST `eth_sendRawTransaction` to the sequencer endpoint (`4663:sequencer` in
  `free_endpoint_registry`) on a dedicated, kept-alive HTTPS connection: TLS done in advance and a
  keep-alive ping every ~20 s. Then offer the same bytes to the other `auto_free` providers. The
  lane does not wait for acceptance before returning the thread.

### 2.7 F7: after the send (existing paths)

**Amendment 2026-10-01 (Gamma's MEDIUM; Alpha reversed the earlier ruling).** The lane holds the
per-(wallet, 4663) execution lock through the receipt, at most 3 s (4663 receipts p50 211 ms, p99
2.8 s), so a lane buy never lands inside another executor's balance-delta window. Past the cap it
releases the lock, alerts, and books outside it. The cap is `MEMECOIN_RH_FAST_LANE_LOCK_RECEIPT_CAP_S`
(default 3; LiveLane takes it only from build_lane, and a test pins the production value). The lane's booking is its own receipt booking
(`recover_confirmed_lane_entry`, M2 step 3a), with the block-pinned balance-delta fallback.

Confirmation, receipt and booking go through the existing executor tail. The spend is paid in
native ETH (`msg.value`), so it uses the existing native-float accounting, not a WETH delta:
- spend = the swap's own `value` × the quote price, `quantity_source=native_value` (evm_swap_executor
  `float_used` branch);
- test `tests/test_memecoin_evm_native_float.py::test_a_covered_float_skips_the_unwrap_and_books_the_swap_value`;
- gas is paid in native separately and is not part of the spend, as today;
- the token quantity comes from the balance delta (Gamma LOW 4). The position is an ordinary funded position, managed by the existing exits (and the
4663 pre-approval). The same source buy reaches today's watcher and J7 later. The (network, mint)
reservation and `already_attempted_from_another_lane` dedupe it.

### 2.8 Trigger choice, decided by M0: the feed or executed logs (Gamma's read, 2026-10-01)

**Gamma's measurement (29 min, n=64,621 txs).**
- Our 4663 swap tape's first row for a tx arrives p50 18.6 ms BEFORE the feed's arrival of the same
  tx: p10 −73 ms, p90 +42 ms, with the clocks within the shared offset file's tens of ms.
- `blockNumber == sequenceNumber` (offset 0).
- Block gap p50 119.7 / p90 127 ms.

So executed logs (`eth_subscribe` logs on the tape's hosts) see the source about as early as the
feed, and they bring three things the feed cannot:
- **Execution truth.** Logs exist only for executed transactions. Reverted or forged sources cannot
  trigger, so §2.1 (a)-(d) become a second layer, not the only one.
- **The exact post-trade state.** The source's own V3/V4 Swap log in the same transaction carries the
  post-swap `sqrtPriceX96`, liquidity and tick. That replaces the §2.4 estimate with today's
  receipt-grade reference, and the two-sided error disappears.
- **The exact received quantity**, from the Transfer to the wallet.

**What logs do NOT fix: the gift (§2.1).** A Transfer to the wallet proves delivery, not intent.

**The comparison M0 makes, with no new code.** Per armed fill, the feed arrival
(`rh_feed_signals.arrival_utc`) against the watcher's socket arrival
(`onchain_wallet_detections.detected_at`, the Transfer-log push). Both are stamped with the corrected
clock, so differences under ~50 ms across the two processes are noise.

**The decision.**
- If logs are no later than the feed at p50, the lane triggers on an in-runner Transfer-log
  subscription for the armed addresses, plus the source tx's Swap log, read from a Swap
  subscription filtered to the candidate pools (V3 pool addresses; V4 by PoolManager + PoolId
  topic).
- Otherwise it keeps the feed decoder, with §2.1 and §2.4 as written.

### 2.9 Where the lane runs: a GIL budget (Alpha, after Gamma's M0 MEDIUM)

**The constraint.** The funded runner is one Python process. The Solana slot lane decides in ~6 ms
at p50 inside it. Anything else that holds the GIL there can delay that decision. Rule 5.2 says
nothing may slow filling.

**The measurement (M0, live feed, 60 s, in a test process).**
- Parsing and filtering every block costs 0.16 ms p50 / 1.35 ms p99 / 3.6 ms max of CPU per frame,
  at ~10 frames a second: 0.29% of a core.
- A concurrent 5 ms ticker's worst lateness went from 0.42 ms to 2.5 ms.

So the per-block work alone would put millisecond stalls on the runner, ten times a second. M0
therefore runs in its own process (built: `scripts/memecoin_rh_feed_shadow_loop.py` under a
release-pinned watchdog).

**The live shape: split the per-block work from the per-signal work.**
- **The detector process** does all per-block work:
  - the feed (or the log subscriptions of §2.8), the frame parse and the `to`/selector filter;
  - the envelope decode, the sender recovery (4.6 ms) and the solver/payer/deadline/dedupe checks.
  - It holds no key and writes nothing funded.
- **The handoff.** For an accepted signal (a few an hour), the detector sends one small datagram to
  the runner: tx hash, wallet, mint, payment, permit nonce, sequence number and arrival stamp. It
  goes on a local abstract `AF_UNIX` socket, the same mechanism as the on-chain lane's wake
  (`memecoin_onchain_wake`; a local datagram is well under a millisecond).
- **The runner's fast-lane thread** sleeps on that socket. It wakes only for a signal and runs F2-F6:
  snapshot read, gates, reservation, template, sign, write-ahead, send.
  - Its GIL use is per signal, about 10 ms of CPU (signing 3.1 ms, reservation and write-ahead
    ~3 ms each, the rest under 1 ms). It is zero between signals.
- **The precompute worker (F2)** also stays out of the runner. It is read-heavy (tape, safety RPC)
  and writes its snapshots to a file the runner reads by reference: atomic write-and-rename, versioned
  by mtime.
  - A snapshot older than its bound is absent (§4).
  - Token safety still runs today's `token_safety_fn`, in that worker's process. The verdict is
    data, read with its completion time.

**Budget, pre-registered for M2: overlap, not day-over-day (Gamma LOW).** The lane costs ~10 ms of
runner GIL a few times an hour, so its effect on a day's p90 is about zero, while ordinary day-to-day
drift (coins, RPC weather) exceeds 1 ms. A day rule would trip on noise or hide a real hit.
- The fast-lane thread stamps its busy window: signal receipt to send return.
- Every Solana slot-lane decision (`slot_send_call`) and every EVM decision in the same period whose
  window overlaps a busy window is flagged.
- The read compares decide-to-send p50/p90 of overlapped against non-overlapped decisions over the
  same period.
- If overlapped decisions are slower by more than the larger of 1 ms or the non-overlapped
  p90's bootstrap 95% interval half-width, the lane is switched off and moved.
- **Minimum sample (Gamma note).** The rule concludes only once at least 10 overlapped decisions
  exist. Below that, the read reports "exposure too rare to measure" with the count, never a pass.
- The detector's own CPU is reported per heartbeat. It is not the runner's.
- The handoff latency (detector send → runner receive) is measured per signal and added to the §3
  budget.

## 3. Budget (decide = feed frame received → bytes written to the sequencer socket)

| step | measured / estimated | basis |
|---|---|---|
| frame parse + per-tx `to` filter, in the detector process (§2.9) | 0.16 ms p50, 1.35 ms p99, 3.6 ms max | M0, live feed |
| detector → runner datagram (§2.9) | < 1 ms (est.) | local `AF_UNIX`; M2 measures |
| Relay envelope ABI decode (per matching tx) | 0.137 ms | measured |
| sender recovery for a candidate (armed refund, Relay shape) | 4.6 ms | measured (pure-Python ECC) |
| in-memory gates, snapshot read, min-out math | < 0.5 ms (est.) | pure Python, no I/O |
| `reserve_entry` (BEGIN IMMEDIATE, ledger) | p50 3.4 ms on 4663 today; tail unknown | `reservation_ms` (measured explicitly) |
| template fill + sign | ~3.5 ms | 3.1 ms sign measured |
| write-ahead identity | ~3 ms (est., same DB) | to measure |
| socket write | < 0.5 ms | |
| **total decide** | **~16 ms p50 (estimate)** | M2 measures p50/p90 |
| feed one way + send one way | ~106-120 ms | §1.3 |
| **arrival at the sequencer after source block seals** | ~122-136 ms | vs the ~120 ms next-block window: N+2 likely from here, N+1 sometimes |

- **Two tail risks.** The ledger write lock (the `attempt_row` probe exists because waits happen),
  and the GIL. The runner is heavily threaded, and a 5 ms GIL switch interval can stretch the decide
  thread. Both are measured in M2; neither is assumed away.
- **The 50 ms budget is comfortably met.** The N+1 stretch from this host depends on the feed's real
  one-way delay (M0).

## 4. Safety is never skipped: the fallback list

The lane hands the signal to today's path (the existing on-chain lane / executor, unchanged) when
ANY of these holds:
- no completed safety verdict younger than the TTL;
- no route snapshot, or the route is pons/curve/unknown;
- pool state older than its bound;
- the Relay code verdict has expired;
- the payment token is not a known quote asset;
- the float is short, or the signing lock is busy;
- a missing ETH/USD price or base fee;
- no market stats younger than the TTL (2026-10-01: the funded hook's gates read them);
- the datagram lacks the source trade (paid size, post-trade state);
- a decode is not canonical, or anything raises.

Once the in-memory checks pass, every refusal is the funded hook's own (`hook:<status>:<why>` in the
lane's record), the same refusal today's lane makes for the same buy.

Each fallback writes one trace substage `rh_fast.fallback` with its reason, so coverage is
measurable.

## 5. N+1 from here is a coin flip: the colocated options (operator infrastructure decision)

Sending only the forwarding to us-east-2 gains nothing. The path is sequencer → feed → decider →
transaction → sequencer, and a forwarder that only relays our signed bytes leaves the decider here,
so both 53 ms legs remain. The options that do give N+1:

| option | how | keys on the remote box | cost | caveats |
|---|---|---|---|---|
| (a) colocated decider | the fast lane (F1-F6) runs on a small us-east-2 instance with its own fast-lane hot wallet, funded with ~2 clips + gas, swept back on a schedule | yes, a separate small wallet only | ~$15-40/month (a small Graviton instance) plus build time to run the lane outside the runner (its own reservation link to the ledger here) | reservation/accounting must cross the network: the remote decides, then records here, so a crash between them is a new `submitted_unknown` shape |
| (b) colocated trigger with pre-signed bytes | this host keeps a SET of pre-signed buys for the hot candidates (one per mint, all at the SAME nonce, min-out from the current price × cap) on a us-east-2 trigger. The trigger watches the feed and forwards the matching pre-signed bytes on an armed wallet's Relay buy | no keys: only pre-signed alternatives, one nonce, so at most one can ever land | same instance cost | caps are judged against the pre-source price, not post-source; re-signing on price moves costs CPU here (3.1 ms each); candidates limited to ~10-30 mints; a compromised trigger can only fire one of our capped buys |
| (c) forward-only relay | relays our signed bytes | no | same | **no gain**: listed only so it is not built by mistake |

Recommendation: decide after M2. If this host lands N+1 on more than half of fills, stay. If it
lands N+2, (b) is the cheapest N+1 that keeps keys home; (a) is the most general.

## 6. Milestones (Gamma reads blocks-behind at each one)

**M0. Feed shadow (read-only, no trading).** The lane decodes armed Relay buys from the feed and
records, per event:
- feed arrival time and block;
- the sender and payer verdicts, and the J7 corroboration join (§2.1);
- the watcher's later detection time for the same tx;
- would-be admission (with the fallback reason);
- the would-be decide time.

It also measures the feed's one-way delay: our own 4663 sends' send time against their block's feed
arrival, minimised over sends.

Knob `MEMECOIN_RH_FAST_LANE=shadow`.

**M1. Decide shadow (built 2026-10-01, branch beta/rh-fast-m1; Alpha's scope).** Sign-less and
outside the runner: three threads in the M0 shadow process behind `MEMECOIN_RH_FAST_DECIDE_SHADOW`
(default off). No key, no reservation, no send, no funded host.
- A tape tail (100 ms, rowid scan) keeps each pool's last 16 swaps and 300 s of activity in memory:
  the F2 snapshot without a file.
- A 10 s poll of our 4663 wallet's native balance and gas price on the research QuickNode only, plus
  ETH/USD and stable decimals from the tape (the C2 float check, `float_covers`).
  **2026-10-09 amendment:** the background wallet snapshot uses the research rotation, including
  its chain-verified dRPC fallback. Native balance, WETH balance and pending nonce share one
  three-item JSON-RPC batch; gas uses a second batch. The healthy 10 s cadence now counts elapsed
  poll work instead of sleeping 10 s after it. Failed polls retain their full cooldown, including
  rate-limit backoff. Snapshot freshness still starts before the reads; the 30 s age limit and
  execution-generation checks are unchanged. See `docs/audits/2026-10-09-rh-papa-snapshot-stale.md`.
- A decide thread runs F3-F5 on each M0 signal from memory: busiest native-ETH V4 pool of the mint,
  state (§2.4), source input net of `MEMECOIN_RH_FAST_SOURCE_FEE_PCT` (1%), the single-range
  estimate, the cap, the min-out and the unsigned UR buy (`native_buy_tx`, eth_call-proven at block
  77690971). Every unknown is a named fallback.
- Token safety (Alpha): no safety call in M1. The decision records the mint's activity rank, i.e.
  whether a top-K off-path safety cache would have held a verdict; the cache waits for M2 and the
  TTL ruling.
- Coin gates are not re-implemented in M1: the read joins today's own attempt on the same mint.
- v1 route scope from funded 4663 entries since 09-13 (n=134): V4 hooked 57, plain 3, multihop 33,
  relay 16, kyber 11, pons 5, curve 4, direct V3 0. V3 and multihop are fallbacks in v1.

Read: `scripts/research/rh_fast_decide_read.py --since <deploy>`: addressable share, decide and
handoff ms, est/true and cap ratio, fee haircut, activity ranks, today's attempt, research load.
Stop at any 429.

**M2. Live, pilot.** `MEMECOIN_RH_FAST_LANE=live`, after Gamma's grade of M0/M1 and the operator's
yes:
- clip: the normal tier clip, or a pilot size (operator);
- read: blocks-behind (fill block − source block) per fill, decide p50/p90, fallback reasons, and
  min-out reverts.

**Coverage is reported beside speed (Gamma LOW 2).** That means the fast-lane share of all 4663
funded entries, and the scoreboard over ALL 4663 entries, not only fast-lane fills, so a lane that
fires on a few entries cannot pass on speed alone.

**Pre-registered success, all of these at n ≥ 20 fast-lane fills:**
- p50 ≤ N+2 on fast-lane fills;
- the all-entries median below the current baseline of 23 blocks;
- min-out revert ≤ 20%;
- the cap-ratio p90 ≤ 1.00 (§2.4);
- the source-revert cost (§2.1) charged in the PnL grade;
- no accounting anomaly.

**M3. N+1 decision.** Choose (a) or (b) from M2's distribution.

## 7. CLAUDE.md rule 5 map (the lane as a whole)

1. **Money path:** yes. Entry, sizing, signing. Operator rulings: the pre-signal safety TTL (a
   departure from the 07-28 invariant 6), float size, pilot clip, and the §5 option.
   - Forged or reverted source transactions are the money risk unique to this lane. The answer is
     the §2.1 sender, permit and dedupe checks, plus the measured revert share.
2. **Speed on the fill path:** the lane adds nothing to today's path; it is a separate thread.
   - Its own budget is measured at p50/p90 in M0 (decode and would-be decide) and M2 (live).
   - The F2 worker shares RPC hosts with today's entries and exits. Its load is capped and stopped
     on 429s (M1).
3. **Concurrency:**
   - own thread and own ledger connection; reservation through `reserve_entry`;
   - nonce from the shared floor under the shared signing lock, never a second allocator;
   - snapshots single-writer, read by reference;
   - no module globals, env vars or process-wide resolvers mutated.
   - `coincurve` is NOT installed in v1, because it would switch the process-wide eth_keys backend.
4. **Absent vs zero vs unknown:**
   - a missing verdict, state, price, base fee or float is a fallback, never a zero;
   - a decoded payment of 0 is not a buy;
   - token decimals come from the snapshot, never assumed;
   - an expired verdict is absent.
5. **Dependency failures:**
   - a feed drop means no fast signals; today's lanes cover them;
   - sequencer rejection or timeout is handled by the existing four-state submission
     (`submitted_unknown` on ambiguity);
   - F2 read failures empty the snapshot (fallback);
   - a min-out revert costs gas only, and the reservation is released by the existing mined-reverted
     path.
6. **Units and identity:**
   - wei vs ETH; USDG 6 decimals vs WETH 18;
   - token atomic units, kept as integers end to end;
   - (network_id, mint) identity; V4 PoolKey including hooks and PoolManager (trap 807bcf99);
   - block numbers as integers;
   - the feed's `sequenceNumber` is not a block number until mapped (M0 records the offset).
7. **Sample:**
   - the lane sees only Relay fills to armed wallets' verified addresses: 194 of 194 detected 4663
     buys since 09-14 were Relay, but wallets the watcher cannot verify are invisible;
   - blocks-behind is measured on fast-lane fills only and reported beside today's path, never
     pooled.
8. **Rollback:** `MEMECOIN_RH_FAST_LANE` empty plus a restart. Half-applied (shadow), nothing signs.

Checks left behind, each proven red on a mutant:
- the decoder test: buy vs sell on a Relay fixture;
- the authenticity tests: a Relay-shaped tx from a non-solver sender naming an armed wallet is never
  a signal (red on a mutant without the sender check); an expired permit and a repeated (wallet,
  nonce) are not signals;
- a fallback test per reason in §4: no network call on the fast path;
- a template `eth_call` proof on 4663 at a pinned block, V4 native and V3 WRAP_ETH;
- a min-out test that the source input is taken net of the maximum fees, and that the cap never
  exceeds source price × the execution premium;
- the M0 instrument recording our cap against today's receipt-based cap per signal.

## 8. Open items for Alpha / the operator

1. The pre-signal safety TTL (60 s proposed). Operator: it departs from the 07-28 invariant.
1b. The gift risk (§2.1): accept M0's measured no-J7 rate, or require J7 corroboration. Today's
    on-chain lane already carries this exposure.
2. Float size: 2 × the largest tier clip + gas recommended, 1 clip is enough to start (§2.5).
3. Pilot clip for M2.
4. After M2: §5 option (a), (b), or stay.
5. `coincurve`: not needed for v1. Revisit only if M2's decide p90 shows signing matters.

## 2026-10-03 correction: unsent refusals and retry scheduling

The initial EVM quote-price refusal precedes approval, pre-transactions, signing and sending.
It returns an explicit `UnchangedWalletResult` so serialization does not invalidate the parallel
fast lane's wallet snapshot. A refusal status alone proves nothing: every ordinary result,
exception, post-approval refusal, mined-swap retry and raw send keeps generation invalidation.
The snapshot's age and generation margin checks are unchanged.

Ordinary re-quotes run on independent per-chain workers, each owning its connection and a
nonblocking pass lock shared with the main-loop fallback. They retain the configured due time,
backoff, expiry, attempt limit and funded gates. No fresh-entry provider read or lock is added.
See `docs/audits/2026-10-03-rhooks-entry-delay.md` for the source transaction and regression proof.
