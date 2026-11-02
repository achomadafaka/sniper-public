# Base 8453 fast lane (spec, revision 3)

**Author:** Beta. **Order:** Alpha 2026-10-02: "the Base (8453) fast-lane spec, modelled on the BSC
lane, default runner_shadow, spec first". **Status:** revision 3. Gamma passed rev 2 (7ce13fa13) for
step 0 with one MED and four LOWs, all folded in here:
- MED: the decide reads ETH/USD with a cache-only `NativePrice.peek()`, never `get()`, and runs on a
  named thread with no network call (§1.1, §4.2, §4.3);
- LOW 1: the landing measure also reads B at the end of block S, and end-of-S+1 is a bound (§2);
  **withdrawn 2026-10-08 (review EVM05): end of S+1 is not a bound in either direction; see §2;**
- LOW 2: buys are counted per wallet. The 11 J7-only wallets made 1 of 67 Base buys, so D2 is not the
  volume lever (§0, §2, §6);
- LOW 3: a minimum of 30 would-send decisions before the gate is read (§2);
- LOW 4: the landing reads move out of the watcher into a research job, so no service uses the
  research host; a 429 is a censored row (§2, §4.5).
Earlier: rev 2 answered Gamma's review of a57bea374 (hop A in step 0, a landing measure, keys from
calldata, per-wallet reporting, Flashblocks, expected volume). No code yet.

## 0. Answer first

Volume is the binding constraint, not speed. Build a sign-less step 0 now and decide on the
runner lane from what it measures.

- **Base fills are rare.** 3 funded Base fills in all history: 64178 V2, 66253 V3, 67763 Kyber.
  In the last 30 days there were 1,119 Base attempts: 778 `not_approved`, 213 over the mcap
  ceiling, 25 quote aborts on provider read failures (transport or unavailable).
- **The roster changed today.** The operator armed 25 wallets on 8453 (2026-10-02). 14 of them have
  a proven Base address (`evm_watched_owners`), so the 8453 watcher sees their buys. The other 11
  reach us only through J7, which is not a lane signal source (operator order 10-02).
- **Their recent Base buys, per wallet (rev 3, Gamma LOW 2).** Ledger buy attempts on 8453,
  2026-09-18..10-02 (15 days):
  - 138 distinct origins, or 67 distinct buys once deduplicated to (wallet, mint, 10 minutes). That
    is about 4.5 buys a day.
  - The 14 watched wallets made 66 of the 67 buys and 137 of the 138 origins. The 11 J7-only wallets
    made 1 (fmpumpguy); 10 of them made none.
  - Four wallets carry most of it (origins): Roveryaps 34, SerAvocado 23, frankdegods 15,
    randomuser123 12. Then 0xsneakky 11, 0xdetweiler 11, badabeepp 6, DegenCapitalLLC 6,
    chrollo0o0 5, LingardSol 4, figaro 4, onmycheck 4, ResellCalendar 1, bluntz_capital 1.
  - Rev 2's count of 121 was the receipts read (tx level). Before today almost all came through J7;
    the watcher only subscribes armed wallets, and the arming happened today.
- **Where today's time goes** (47 Base attempts that reached a quote since 09-18):
  - quote p50 1.7 s, p90 3.8 s;
  - safety p50 0.6 s;
  - market data p50 0.25 s.
  - Base blocks are 2 s, so the quote alone costs about 1-2 blocks.
  - The watcher detects a buy in 0.17 s p50 and 0.30 s p90 (18 detections).
- **The venue mix suits the lane we have.** Receipts of the 121 buys, read on
  `base.gateway.tenderly.co`:
  - V4 is in 97: 51 V4 only, 42 V3+V4;
  - Aerodrome or V2 is in 23.
  - Of the 57 distinct V4 pools, 23 have a stored PoolKey: 13 USDC-quoted, 4 WETH, 2 native,
    4 other; 9 hooked.
  - A USDC transfer is in all 121.
  - So the 4663 lane's V4 routes cover the shapes: native (`v4_native`), WETH (`v4_weth`), and
    USDC as the bridge X (`v4_bridge`). They were merged today in 12e3b650f.
- **Keys come from the source's own calldata.**
  - All 121 source txs go to the Relay router `0xccc88a9d…` (selector `0x0a2b8f36`), never straight
    to the Universal Router.
  - The V4 PoolKey still sits in that calldata as consecutive words. In a 30-tx sample, 51 of 61 V4
    pool ids were recovered exactly, with keccak(abi.encode(key)) equal to the Swap event's pool id.
    23 of the 30 txs were recovered in full.

## 1. Design, modelled on the BSC lane

### 1.1 Decide in the 8453 watcher, from the source receipt

This is BSC step 1's shape. The watcher (`onchain_evm_wallet_watch.py --chain 8453`, running)
holds the source buy's receipt after today's hand-off (commit and wake). The receipt's own
PoolManager `Swap` on the coin's V4 pool B carries the exact post-trade `sqrtPriceX96` and
liquidity at zero lag. No tape is fresher.

**The thread (rev 3, Gamma MED).**
- The decide runs on the push socket's loop thread, as BSC's `_signal_bsc_lane` does today.
  `memecoin_onchain_push.run_push_stream` calls the transfer handler synchronously on that thread.
  The decide is called at the end of `_on_transfer`, after `_commit_detections` and the wake.
- While it runs, the next transfer delivery and the next head on this socket wait behind it. So:
  - the decide is CPU and in-memory only: no network call, no ledger write;
  - its one local read is the key store, on a read-only handle with `busy_timeout` 250 ms (the
    roster handle's bound), and only when the calldata found no key;
  - the step 0 decision record goes to a queue drained by one writer thread into the watcher's own
    SQLite file, never written on the socket thread;
  - decide_ms p50/p90/max is recorded, and the step 0 report states it beside the watcher's
    detection latency.

- **PoolKey (Gamma MED-3)**, first that answers:
  1. **From the source tx's calldata.** The watcher already reads the transaction in its hand-off
    (`eth_getTransactionByHash`), so this costs no read.
     - Candidates come from struct-shaped windows of the input: a PoolKey is 5 consecutive words; a
       PathKey is 4 words plus one other currency from the input.
     - A candidate is accepted only when keccak(abi.encode(key)) equals B's pool id from the Swap
       event. A match is exact; anything else is refused, never guessed.
  2. **The ledger's `memecoin_evm_v4_pool_keys`**, through the read-only ledger key reader.
     Together with the tape this knows 23 of the 57 pools.
  3. **Else `key_unknown`.** The existing off-path warm is queued.
  - Step 0 records which source answered, so key-known share is measured both ways.
- **Route kind:**
  - B's other currency is native: `v4_native`.
  - It is WETH (`0x4200…0006`): `v4_weth`.
  - It is USDC: `v4_bridge` with X = USDC.
  - Anything else is `bridge_unsupported` in v1.
- **Hop A for the bridge** (native → USDC; Gamma MED-1):
  - **Step 0 uses no new read and no lane host.** A's mid is the watcher's existing ETH/USD mark
    (`NativePrice`: Binance ETH; Base's quote asset is WETH), with USDC taken at $1.
    - **Read with a new cache-only `NativePrice.peek()` (rev 3, Gamma MED).** It returns the cached
      value and its age under the existing lock, or (None, None), and NEVER fetches.
      `NativePrice.get()` cannot be used: past its `max_age_s` (10 s) it fetches Binance
      synchronously on the caller's thread (median 252 ms) and returns age 0.0. Called from the
      decide, `bridge_state_stale` could never fire and decide_ms would absorb a network call.
    - The mark is used only when its peeked age is at most 30 s, else `bridge_state_stale`. The warm
      thread refreshes it every 5 s, so a stale peek means that thread's fetches are failing.
    - The detection path's own `get()` is today's code and is untouched. Its source (cache or sync)
      is recorded beside the decision, so a sync fetch paid by the detection is counted, never
      hidden in decide_ms.
    - It is recorded as `a_source=eth_usd_mark`, with the peeked age.
    - Its error against the real pool is measured off-path (§2).
  - **The decide makes no network call**, in step 0 and in every later step. It runs on the push
    socket's loop thread (below), where a network wait would hold back the next delivery.
  - **From `runner_shadow` on:** A is one fixed deep, hookless native/USDC V4 pool, read by a 1 s
    background slot0 poll on the lane host (D1), in memory, with a 2-block staleness bound. The
    decide reads it the same cache-only way: value and stamp, never a read of its own.
  - The bridge's min_out uses A's mid, as on 4663. A mark above the pool's mid only tightens it.
    A mark below loosens it by the measured error, which is why live uses the pool.
- **Price cap and min-out:** the 4663 rules unchanged (rev 3 §6.2):
  - cap = min(post × (1 + chase), source average × premium);
  - the bridge prices hop A at its mid for the lane's own clip;
  - min_out = x_mid × den // num, or clip × den // num.
- **Multi-pool receipts:** 42 receipts carry V3+V4.
  - The coin's hop is the V4 Swap whose pool has the coin as a currency. Its exact post-state
    stands alone.
  - The source's average for the cap is that hop's own in/out (the BSC v1.1 split-leg rule), never
    the whole transaction.
  - A receipt with two V4 swaps on coin pools is `leg_ambiguous`.

### 1.2 The runner's lane

- The datagram has the 4663 rev 3 shape: route_kind, key, a_key, a_sqrt, post_wei,
  cap_num/cap_den, src_in/src_out, paid_usd, snapshot and eth_usd.
- The receiver is the shared `LiveLane` with Base pieces: chain 8453, the custody address, Base's
  funded-submission sender (`auto_free` endpoints), and booking from the receipt.
  `lane_receipt_amounts` holds for every kind, as on 4663: the coin comes from the PoolManager and
  the native value is paid.
- The safety cache and inline-on-miss carry over: route-shaped registry, classification on
  coin → X, full-path routability, the C1 host pin, and `BridgeLegFailed` never cached.

### 1.3 Rollout

- **M1 (step 0):** sign-less decide in the watcher, every decision recorded. Nothing is sent.
- **`runner_shadow`:** the runner judges every kind through the funded hook in a rolled-back
  transaction, as on 4663.
- **Live:** behind `MEMECOIN_BASE_FAST_LANE=live` and `MEMECOIN_BASE_FAST_ROUTES` (default off),
  one kind at a time on the operator's word.

## 2. Step 0, and the gate to build past it

Step 0 is the watcher's decide and its record, `base_fast_decisions`, in the watcher's own SQLite
file (written by the writer thread of §1.1). Per decision:
- reason, route kind and decide_ms;
- the key source (calldata, store or unknown);
- the cap and min_out;
- for a bridge, `a_source`, the peeked mark's age, and the detection read's source (cache or sync);
- the source block S, the pool id of B and B's post-state from the receipt, so the landing job
  needs nothing from the watcher's memory.

Nothing is sent. Run it with the 14 watched wallets until the minimum sample below is reached.

**The landing measure (Gamma MED-2; rev 3 LOW 1 and LOW 4).** The decide prices on the source
receipt's exact post-state, so our own clip is under the cap almost by construction. What decides a
Base lane is whether the price still holds when we would land, 1-2 blocks later, behind other
copiers.

- **Where it runs (LOW 4).** A separate research job, `scripts/research/base_fast_landing.py`, not
  a timer in the watcher. It reads `base_fast_decisions` read-only and works through would-send rows
  it has not graded yet. So the watcher service never touches the research host, which stays a host
  no service uses (rule 573d8817), and the watcher gains no timer thread.
- **Reads:** B's slot0 by PoolManager extsload with the multi-hop module's StateLibrary layout, on
  the 8453 research host (`base.gateway.tenderly.co`, rule 0724bc08), at three block tags:
  - **end of block S** (LOW 1). The receipt's post-state is the state right after the source's own
    swap. Copiers later in block S move B before S+1 begins. Without this read, "moved in S+1" and
    "moved later in S" read the same;
  - **end of S+1** and **end of S+2.**
- **For a bridge:** also A's slot0 at block S, which measures the ETH/USD mark's error.
- **Cost:** 3-4 reads per decision, none on the decide path, none in a service.
- **Recorded:**
  - whether our min_out would still clear on B's state at the end of S, S+1 and S+2;
  - the number of other swaps in B in those blocks, from the tape where it covers B (30 of the 57
    pools).
- **End of S+1 is a proxy, not our fill and not a bound.** *(Corrected 2026-10-08, engineering
  review EVM05. This line used to say a clear at the end of S+1 is a lower bound on clearing and a
  fail an upper bound on failing; that claim is withdrawn.)* The end-of-S+1 state includes swaps
  placed after our would-be position, and a later swap can move the price either way: a sell after
  our slot makes the end-of-block state cheaper than the state we would have met, a buy makes it
  dearer. Exact counterexample in the script's own one-range model (L = 1e20, clip 1e12, fee 0,
  min_out 5e11): buys raise sqrt to 2Q before our slot, so our buy fails (249,999,998,750 out); a
  later sell restores Q, so the end of S+1 clears (999,999,990,000 out). The reverse order gives an
  end-of-block fail over an insertion-time clear. The statistic is **end-of-block modeled min-out
  survival**, which can err in both directions, and the report names it so. Rows keep their
  states and `detail_json.s1_estimand`.
- **A failed read is censored, never a clear (LOW 4).** A 429, a transport error, or a refusal of
  historical state for the block gives a landing row with `censored=<why>` and no clear/fail value.
  Censored rows are counted and excluded from the 60% denominator, never treated as clears.

**Gate to build the runner lane** (named before running):
- at least 3 would-send decisions a day on armed wallets;
- AND at least 60% of the graded ones still clear min_out at S+1;
  *(2026-10-08, EVM05: this threshold was set on the withdrawn lower-bound reading. It is left
  as written, since the gate was named before running, but a pass no longer counts as conservative
  evidence of insertion-time clearing. Re-setting the gate on the right estimand is an operator
  decision, open. Until the operator re-sets it, a gate read reports the end-of-block figure with
  no build verdict.)*
- AND the bridge mark's error p90 ≤ 0.3%.

The M1 cap-ratio alone would be close to tautological here (Gamma).

**The minimum sample (LOW 3).** The gate is read at **30 graded would-send decisions or more**,
never earlier. At ~2.5 a day, 7 days gives about 17, and 60% on n≈17 is ±24 points at 95%. If 7
days have not reached 30, the run is extended, not read early. The volume leg (≥ 3 a day) is read
over the same whole run.

**Expected volume (Gamma LOW c; rev 3 LOW 2, per wallet).**
- The watched 14 made 66 of the 67 distinct Base buys of 09-18..10-02 (§0), about 4.4 a day. The 11
  J7-only wallets add almost nothing, so arming those addresses (D2) does not move this number.
- × a V4 share of 0.8 × calldata-or-store keys of about 0.84 gives about 3.0 decisions a day before
  the gates. The mcap ceiling, the floors and the other gates cut it further.
- So the ≥ 3/day leg is borderline at best and probably FAILS after the gates. Four wallets carry 61%
  of the origins, so one of them pausing moves the rate a lot. Step 0 still answers what the Base
  lane would do and costs nothing to run.

**Reporting (Gamma LOW a):** every gate input is reported per wallet as well as pooled. The volume
count can stay pooled.

## 3. What does not change

- The funded Base path: quote, safety, gates and executor.
- The 8453 watcher's detection hand-off. The decide runs after commit and wake, as BSC's does, and
  a decide failure never touches the detection.
- J7 signals never feed the lane (operator order 10-02).

## 4. Edge cases (CLAUDE.md rule 5)

1. **Money path.**
   - Step 0: no.
   - The runner lane: yes. It is gated by `runner_shadow`, the knob (default off) and an eth_call
     proof of each Base template before any shadow. The 4663 templates are proven on 4663 only;
     Base's V4 multi-hop struct is the 4-field one (`_EXACT_INPUT_FOUR_FIELD_CHAINS`).
2. **Speed.**
   - The decide runs on the push socket's loop thread after the hand-off (§1.1), so it delays the
     next delivery on that socket, never the current detection. It is CPU and in-memory only:
     receipt, transaction, A's peeked mid, and the key store only when the calldata has no key.
   - It makes no network call (rev 3): `NativePrice.peek()` never fetches; `get()` is not called
     from the decide.
   - Its decide_ms p50/p90/max is recorded and reported beside the detection latency. A p90 above
     5 ms is a finding to report before any later step.
   - The calldata key scan stops at the first key whose hash is B's pool id; its time is inside
     decide_ms.
   - The record is queued to a writer thread, not written on the socket thread.
   - The hand-off is untouched.
   - The landing reads run in a separate research process (§2), never in the watcher.
   - From runner_shadow on, A's poll is a background thread.
3. **Concurrency.**
   - In step 0, A's mid comes from `NativePrice.peek()`: one acquire of the existing lock, no fetch,
     no write. The warm thread stays the only writer of the cache.
   - The decision queue has one producer (the socket thread) and one consumer (the writer thread).
     A full queue drops the record and counts the drop; it never blocks the socket thread.
   - From runner_shadow on, A's state is one value written by its poll thread, read with its stamp.
   - The key reader is read-only.
   - No shared mutable module state.
4. **Absent, zero, unknown.**
   - A missing key is `key_unknown`, never a guessed key.
   - A stale A is `bridge_state_stale`.
   - A receipt with no V4 coin swap is `no_v4_leg`, which covers the 23 Aerodrome/V2 receipts.
   - Liquidity 0 in the Swap event is `no_liquidity`.
5. **Failure modes.**
   - Step 0: the warm thread's Binance fetches failing for over 30 s put every bridge decision at
     `bridge_state_stale`; the count is recorded. (From runner_shadow on: A's poll failing for over
     2 blocks does the same.)
   - The key store being unreadable, or busy past 250 ms, gives `key_unknown`.
   - A receipt missing from the watcher gives no decide.
   - A decide that raises is logged and dropped; the detection is already committed (as BSC's).
   - **The landing job shares the research host's quota (rev 3, LOW 4).** Its reads and every other
     research read on `base.gateway.tenderly.co` draw on one rate limit. A 429 or failed read gives
     a censored landing row (§2), never a clear. The job paces itself and stops on repeated 429s.
     Censored rows are reported by cause, and a censored share above 20% makes the gate unreadable
     until re-graded.
6. **Units.**
   - USDC has 6 decimals and native/WETH 18. All prices are exact fractions in atomic units.
   - post_wei is wei per coin atomic.
   - The 4-field multi-hop struct applies on 8453.
7. **The sample.** The 121 receipts and the 67 buys are our armed wallets' Base buys, so the venue
   mix and the rate describe them, not Base. The window predates today's arming, so step 0 measures
   the new roster. Four wallets carry 61% of the origins, so every gate input is also reported per
   wallet (§2).
8. **Rollback.**
   - Step 0: `MEMECOIN_BASE_FAST_LANE=off` (the default) and the watcher skips the hook.
   - Later steps: the knob back to off, no release.

**Not handled in v1:**
- Aerodrome and V2 (23 of 121);
- V3-only buys (1);
- non-USDC bridges (4 pools);
- hooked native/USDC pools as A;
- J7-only wallets (11 of 25, 1 of the 67 buys);
- keys absent from both the calldata and the store;
- **Flashblocks (Gamma LOW b):** Base pre-confirms about every 200 ms. Detection and ON_TIME are
  measured in 2 s blocks here. If the watcher's endpoint streams flashblocks, a flashblock index
  would be the finer clock. Not measured in v1.

## 5. Measurement

- Step 0: decisions per day by reason and kind, key-known share, decide_ms p50/p90/max, the
  detection read's sync share, the cap ratio against the receipt, and the landing clears at the end
  of S, S+1 and S+2 with censored rows by cause.
- Live: fill timing's 8453 ON_TIME = 1 block, read from the fill-timing ledger as today.

## 6. Operator decisions

- **D1. A Base lane host** for the safety cache, the inline check and A's slot0 poll. It is needed
  from runner_shadow on, not in step 0. It must not be the custody RPC or a funded pool member.
  `base.gateway.tenderly.co` is the research host and is not used for the lane.
- **D2. Address proofs** for the 11 Base-armed wallets with no proven 8453 address. Without them
  their buys never reach the watcher. **Not a volume lever (rev 3, LOW 2):** these 11 made 1 of the
  67 Base buys of 09-18..10-02, so D2 does not go ahead of step 0 and step 0 does not wait for it.
- **D3. The Base float.** The lane pays native ETH as the transaction value. The funded Base wallet
  needs a native float beyond gas, as on 4663; the float check refuses otherwise.

### 2026-10-14 amendment: Base warm market transport

Base startup now passes the existing lane_dex_base(None) transport used by RH/BSC, honoring MEMECOIN_DEX_WARM_HTTP. Market-data budgets, cache age checks, admission and quote rules remain unchanged. This repairs transport wiring; no organic Base latency or fill-rate improvement is established by the offline regression.
