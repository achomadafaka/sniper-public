# BSC entry preparation and honest sub-200 ms measurement

**Status:** operator-approved architectural design, 2026-10-11. Implementation and gated activation authorized by the operator request to implement and verify actual signal-to-fill speed. Scope: chain 56. The approved Solana design is a separate change. [Review result](../../audits/2026-10-11-bsc-entry-speed/design-review.md).

**Goal:** remove preparation waits from BSC entries and measure successful execution from the original signal receipt. Target **≤40 ms dispatch for fully prepared native entries**. Measure the requested **≤200 ms successful execution observation** honestly; do not substitute an acknowledgment or promise universal 200 ms inclusion.

## 1. Evidence and the chain limit

The [frozen local baseline](../../audits/2026-10-11-bsc-entry-speed/baseline.md) contains 131 attempts from 2026-10-10 18:00 UTC through 2026-10-11 15:00:07 UTC: 126 refused, three unsigned funding shortages and two shared-route fills. It includes unarmed wallets and duplicate source transactions across origin prefixes. It is not the policy-eligible received-signal universe.

| Exact example | Preparation or observation boundary | Measured time |
| --- | --- | ---: |
| Native revert `be9677a1…` | Safety | 322.291 ms |
| Same native attempt | Lane start → physical send call / send call → ACK | 595.463 / 12.271 ms |
| Dante, position 73334, shared route | Sizing / quote / safety | 252.763 / 227.959 / 335.950 ms |
| Same Dante attempt | Recorded J7 observation → ACK / ACK → successful receipt observation | 931.294 / 803.430 ms |
| 吉祥羊, position 73295, shared route | Sizing / quote / safety | 349.840 / 296.240 / 569.075 ms |
| Same 吉祥羊 attempt | Recorded J7 observation → ACK / ACK → successful receipt observation | 1,084.212 / 487.266 ms |
| Latest two retained native fills | Source-to-copy block gap | B+2, B+2 |
| 吉祥羊, exact source and copy transactions | Source-to-copy block gap | B+5 |

Quote and safety overlap. These rows do not identify additive savings. Actual swap signing in the two shared fills took 0.017–0.025 ms; signing is not their main bottleneck. Their first live preflights detected minimum-output failure. Removing preflight or lowering signed floors would change safety, not repair execution.

The earlier audit's 12 successful shared fills had recorded observation-to-ACK p50/p90 **1,355.4/2,197.7 ms**. That selected historical cohort differs from the new window. Neither cohort has immutable per-attempt release identity, so neither proves current-release performance. Existing J7 normalization can replace its producer receipt with a later drain-time stamp; these historical observation boundaries do not prove latency from the first ingress.

BNB Chain reports approximately **450 ms blocks** and **650 ms normal deterministic finality**. A confirmed source receipt arrives after its block is sealed; our copy can first enter a subsequent block. Block phase, source-provider delay and delivery can make 200 ms impossible for a particular signal even with zero local work. Integer block timestamps cannot establish millisecond inclusion. [Official settlement explanation](https://www.bnbchain.org/en/blog/what-sub-second-finality-means-for-bsc-settlement), [Parlia implementation](https://github.com/bnb-chain/bsc/blob/master/consensus/parlia/parlia.go).

Therefore the BSC goals are fast dispatch, improved next-block inclusion and an explicitly measured sub-200 success fraction. Universal sub-200 inclusion is not an acceptance claim. No pending-mempool authority, new fee policy, paid feed or builder subscription is introduced.

## 2. Reuse current repairs and seams

The checkout and pinned runner/web resolved to `21b18572e5f7dbedf2375458c0d352a7c29d2eef` at the baseline freeze. The examples above predate that release. Main advanced during planning to `eb7186fd049ec63f57f8302aa3ce9969cae85bca` with Base latency fixes; this is not evidence of a BSC runtime change. Already implemented: bounded off-WebSocket canonical receipt decoding, split-route payment proof, strict-ID batched V2 quotes, attempt-local Flap state reuse, early safety deadline, native snapshot reuse for shared signing, early funding precheck and exact unsigned-shortage handoff. Do not reimplement these or claim their unmeasured benefit.

| Responsibility | Existing seam | Design use |
| --- | --- | --- |
| Original on-chain receipt and proof decoding | `scripts/research/onchain_evm_wallet_watch.py`, canonical admission and `_read_pair` | Preserve original ingress through the bounded decoder; record queue/read/null-retry/relay work independently. |
| Verified pre-buy PCS discovery | `memecoin_bsc_v2_live.created_hop_targets`, existing PairCreated subscription | Add a bounded candidate-only handoff; do not broaden native buy authentication. |
| Native candidate safety/facts/stats | `memecoin_bsc_fast_lane.build_live`, inherited `memecoin_rh_fast_lane.SafetyCache` | Seed verified candidates before a buy; reuse existing verdict, supply and market-data freshness. |
| Native source reserve facts and hop tape | `make_watcher_signal`, `_decide_v2_leg`, `tape_hop_state` | Preserve exact source block/pool/payment identity and existing tape freshness. |
| Wallet balance/gas/nonce snapshot | BSC `Snapshot.poll_once`, `get_native_signing_snapshot_fn` | Warm off-path; retain age, generation and local nonce-floor checks. |
| Shared early sizing | `memecoin_real_runner._early_cached_balances`, chain-56 cached sizing | Use fresh existing hints; final clip/admission and spendable funds remain authoritative. |
| Shared exact quote preparation | `evm_real_seams.prepare_entry_quote_fn`, executor `early_quote.take` | Reuse the full ladder envelope, exact inputs and one-shot consumption; never use the global capacity-probe hint for speculative warming. |
| Route facts | V2 pair cache, Flap override layout, Infinity manager/hash-bound keys | Warm identity and metadata without reclassifying mutable state as immutable. |
| Dispatch and booking | `BscSender.send`, `execute_reserved`, `_receipt`, `_settle`, shared executor | Keep write-ahead identity, same-byte sends, flock/nonce generation and canonical receipt accounting. |
| Timing storage | Separate `data/memecoin_fill_timing.db`, trace/resolver/views | Add off-path observations, without synchronous funded-ledger diagnostic writes. |

References describe this checkout and may move: watcher `:497`, `:1461`, `:2581`, `:3069`; BSC lane `:414`, `:550`, `:1025`, `:1207`, `:1398`, `:1492`, `:1716`, `:1895`, `:1993`; RH safety/receipt `:599`, `:2147`, `:2393`; runner `:2224`, `:8483`; EVM seams `:3011`, `:3179`, `:3299`, `:4032`, `:4399`, `:4658`, `:4970`.

## 3. Correct the original clock before grading speed

Thirteen retained native attempts have negative observation-to-decision deltas, from −1.479 to −37.410 ms. The watcher emits raw UTC; the funded hook uses corrected UTC; the trace calls both `observed_real_utc`. This is a clock-domain defect, not evidence of execution before receipt.

At the first callback, before parsing or queueing, capture one immutable receipt stamp: raw UTC, corrected UTC from one cached local correction, `monotonic_ns`, host boot identity, producer identity and sequence. Publish these through the source payload, authenticated native IPC and shared hook. Existing source timestamp/age authority remains separate. Receipt stamping must not fetch a correction, read a file per signal or perform a DB write; boot identity is loaded once at startup.

Use monotonic differences only within the same host boot. Retain original T0 through duplicate deliveries, proof completion, retries and handoffs. Record the earliest locally observed *exact* source transaction/wallet/mint identity; opaque J7 IDs retain their own receipt and an explicit source-identity gap. Never merge by nearby time or wallet/mint alone. A producer restart must not reset a received event's T0; a missing or foreign-boot anchor makes the interval unknown.

J7 already captures monotonic receipt and corrected UTC in `sniper/feeds/j7_feed.py:J7WalletsFeed.observe`. Carry that stamp through `memecoin_runner` drain and `fomo_feed.normalize_feed_item`/`CopyEvent`; do not create a later drain-time T0. Retain its existing independent-feed authority. Missing exact source transaction identity affects state reuse/block attribution, not a new J7 admission gate.

Stamp physical dispatch immediately before the existing provider call, separately from ACK and receipt observation. Preserve queue admission, decoder start/end, lane dequeue, facts/safety availability, final admission, nonce/generation check, durable binding and send boundaries. A single quote timestamp must not conflate forward quote, reverse simulation, selection and dispatch age.

Historical negative deltas remain labeled mixed-clock evidence. Do not clamp them, subtract today's correction or rewrite old rows. Generic J7 ingress must supply the same anchor before decoding if available; otherwise its monotonic receipt interval remains unknown.

## 4. Prepare native candidates before the signal

### 4.1 Candidate input and exact facts

The BSC lane currently calls `note_candidates` inside `handle`, after the buy. Wire pre-buy publication from `memecoin_bsc_v2_live.created_hop_targets`: it validates PCS factory/PairCreated shape, token order and CREATE2 pair, and currently publishes WBNB hop read targets. Its factory subscription is active only when `discovery_on=bool(hops)`; no active subscription means no claimed first-buy coverage. Extend those validated facts into mint/quote/pool candidates; do not add a broad discovery subscription or entry-path lookup. Existing local recent supported-mint facts can seed repeat buys. Candidates can prompt reads; they cannot arm a wallet, assert source payment or reserve funds. Paper/Radar ownership does not become funded authority.

Use authenticated existing IPC with a separate candidate message shape. Native buy messages keep watcher-only PID/UID authentication. Candidate messages additionally accept the current tape worker, validated against `$STATE_ROOT/data/memecoin_bsc_v2_live.pid` and the pinned worker manifest; load/update that identity off-path, never read its pidfile on a buy. Only canonical chain-56 mint/quote/pool identities enter it. The receiver performs an in-memory publication; discovery, DB reads, pool lookup, market data and safety run outside the entry handler. Deduplicate before work and keep at most **32 active candidates**, one speculative job in flight and a ten-minute candidate lifetime. Rank armed-wallet recent touches first, then least recently checked; record evictions and unknown first encounters.

For each candidate, prepare existing token facts, finite fresh supply, gate market data, quote identity and native-V2 safety. Reuse the current native safety policy and TTL; do not extend a verdict by reading it or reuse it as a generic shared-route PASS. The current verdict is at the pilot clip; exact final funding/output and all existing executor checks remain decisive. Quote identity changes invalidate preparation rather than mutating a shared dictionary underneath a running check.

Publish complete immutable records with original observation time and generation. Entry consumption uses the existing cache/ownership checks, never a new network read, wait, retry or contended lock. A cold or expired candidate retains today's bounded inline/fallback/refusal behavior and its original receipt clock.

### 4.2 Funding, gas and price preparation

Reuse existing native BNB/WBNB/nonce/gas snapshots and one-second BNB/USD/gas memoization. Also verify the watcher's existing five-second `NativePrice.start` worker is started, and measure its ten-second cache hit/age. Its expired `get()` fallback currently performs a synchronous read inside the bounded decoder worker; retain and label that cold behavior in this design rather than silently using stale prices or adding a new refusal. Keep worker-owned HTTP clients, freshness limits and generation invalidation. Missing balances are unavailable, not zero; zero is valid. A snapshot is a hint, not a claim on funds or a durable nonce allocation. The final approved clip must fit the exact funding leg plus gas, fees, reservations and configured reserve.

Do not join a speculative price Future on the entry path or lengthen price TTL. Preserve the early unsigned-shortage handoff: a signed/submitted/unknown attempt never becomes a safe fallback. Refilling floats, changing inventory or reserve thresholds is outside this design.

### 4.3 Resource bounds

Read-only configured values at inspection: snapshot period **9 s**, safety HTTP minimum gap **1.1 s**, refill-below **0.07 BNB**, builder-all enabled. Default safety TTL is 60 s; default snapshot max age is 30 s. These are configured values, not an independent process-effective audit.

Use the existing safety worker and paced host first. Candidate work may use only spare capacity after essential snapshot, keepalive and live reads. One client/thread is not provider-account isolation. Keep the 1.1 s minimum request spacing and existing rate-limit/backoff behavior; adding a second worker must not multiply that budget. Native warm coverage may consequently be low. Report ready/queued/expired counts and per-job request cost; do not silently accelerate polling to achieve a hit rate.

For shared speculative preparation, use one separately owned read client and a shared nonblocking budget token. No token means skip, never wait. Stop speculative admission during live read pressure, rate-limit/backoff or essential refresh lateness. The proposal authorizes no higher account request rate. Any later cadence/provider-capacity change needs a separate measured proposal.

## 5. Shared routes: prepare facts, preserve live state

The latest slow fills used shared routing. A native-only fix does not establish their improvement.

Warm verified V2 connectors/pair identities, token decimals and code-bound metadata; precompute Flap storage-override layout keyed by mint/taker/spender/code identity and Infinity's manager-verified six-field pool key/hash. Mutable supply, reserves, V3 ticks/liquidity, Flap fees/graduation, hooks, gas and prices keep their existing age/state checks. A configuration/custody/code/route change discards the record.

Keep the existing exact quote preparation parallel with attempt-local safety. Its envelope already binds custody, chain, mint, USD clip, slippage and atomic input and runs the full venue ladder, including required reverse/simulation work. Match the *final approved clip* exactly; do not round it to a warmed tier, substitute a larger output, skip mixed-route whole-balance checks or steal another attempt's capacity-probe hint. Existing attempt-local `EarlyEvmQuote.take` can wait until its original ladder deadline; a speculative completed-cache adapter must instead return a ready copied record or an immediate miss. Never pass an unfinished cross-signal Future into that blocking seam.

Cross-signal reuse requires additional provenance. If adding a bounded per-key prepared-quote slot, retain the original forward-response and completion times, every selected route/pool, effective-policy and signer generation, and a coherent read-block number/hash. All mutable reads and venue simulations must support that same block. Maximum age remains the stricter existing guards: three-second prepared-envelope lifetime and **750 ms preflight freshness** where applicable. Age alone does not prove usable post-source state.

Before use, require independently observed canonical route state at or after the exact source block, with matching hashes for the read evidence; no RPC may be added to entry merely to validate a speculative hit. A before-source state, unpinned RPC/aggregator result, missing canonicality, mixed blocks, resized clip, code/config change or unavailable source block is a cache miss. Do not stamp it fresh at consumption. At most two exact clip keys per candidate; unbound preparation may warm connections/immutable facts but is never an executable quote hit.

Initially keep shared live safety and transaction preflight. Attempt-local `EarlySafety` cannot become a cross-signal PASS: it lacks route, quantity, policy and block binding. This design does **not** authorize a reusable shared safety verdict or new local V3/Flap/Infinity signing engine. Consequently it makes no ≤40 ms dispatch promise for those routes. Report their remaining live-state waits. If the safe prepared-quote contract yields no useful hits at the current budget, leave executable cross-signal warming disabled and ship only useful facts/sizing preparation.

## 6. Source freshness, delivery and successful execution witness

BSC has no TPU estimator. Its corresponding uncertainties are source-provider delivery, proof reads, current-header age, relay checks and transaction propagation. Trace each against original ingress and a locally observed header number/hash/time. Measure candidate-ready coverage and decode queue delay, including the observed 53–153 ms arrival-to-decode examples. Retain null-read retries as proof latency, not missing entries.

Compare existing identity-checked source/read/send endpoints with bounded off-path, separate-capacity diagnostics. Check same canonical block/hash, response/notification arrival and source-to-dispatch block gap; do not compare integer block timestamps as exact latency. A selected source endpoint is not automatically a better send endpoint. Keep existing endpoint selectors and identical signed bytes. There is no BSC equivalent of the Solana TPU public-read exception in this proposal. Endpoint/cadence changes require measured benefit and capacity evidence before activation.

The native authoritative receipt loop sleeps 250 ms after a null result. Shorter observation latency does not improve inclusion. The existing `scripts/research/fill_timing_resolver.py` runs once per minute on completed attempts; it cannot witness first successful execution within 200 ms. Retain it as the later canonical validator.

Add a measurement-only exact-hash observer owned by the existing BSC tape process, with its own bounded read thread/client, using that process's existing `newHeads` stream. Default **`MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED=0`**; enabling it is a separate measurement rollout, not a new trading service. After durable signed binding, sender tracing publishes hash/attempt/original receipt metadata to an in-memory bounded ring without a new mutex/wait; a background publisher sends read-only IPC admission to the tape worker. Authenticate this measurement-only admission to the pinned runner PID/UID, with identity refresh off-path. Missing/overwritten publication is counted as a witness gap. Neither IPC delivery nor observer startup is awaited by signing/submission.

Record a successful receipt (`status=1`, exact hash/block) and later match it to canonical confirmation and booked transaction. Observation is an upper bound on inclusion, not a mining timestamp. Failed/forked/mismatched receipts and observer gaps are not successes. Observer results never release reservations, allocate nonces or book fills. Starting/stopping the observer is generation-bound; stopping or rolling it back does not stop the source tape or funded runner.

Limit observation to two outstanding hashes, ten seconds each and a global two receipt reads per observed new head; coalesce provider calls and honor shared read-budget/backoff. Its ingress ring has at most 32 records; overflow/refused/expired observations are counted off-path by sequence/admission state. Head callbacks only publish to the read worker, never perform observer RPC inline on the tape's receive loop. Measure publisher, IPC, observer queue delay and provider RTT. No new 50 ms polling storm or wait in the sender. If the tape/head stream is unavailable or header-driven observation cannot witness sub-200 execution, record the gap instead of inventing an execution time. Keep authoritative polling/settlement unchanged in this first design.

The native datagram listener currently executes `handle` inline. Its wallet flock spans send and the first bounded receipt wait, up to three seconds. This serializes subsequent native handling. Record ingress-to-dequeue and actual lock-acquisition separately from lock-held time. Do **not** move its flock FD to another thread, unlock before receipt, or queue native signing behind an expired two-second cross-lane handoff hint. Shared nonce snapshots and `bind_entry_submission` are not an async wallet session. Native receiver/settlement concurrency requires a separate ownership design; burst/busy entries remain explicit misses here.

## 7. Acceptance and rollout

| Metric | Proposed target | Definition |
| --- | --- | --- |
| Fully prepared native original receipt → physical dispatch | p50 ≤20 ms; p90 ≤40 ms | Includes source proof/queue time after T0; proof-ready-to-send is supplementary only. |
| Same stratum receipt → ACK | p50 ≤35 ms; p90 ≤60 ms | Transport evidence, never a fill. |
| Fresh canonical source → copy inclusion | Improve B+1 fraction and block-gap distribution | Exact source/copy block identities; no fixed block-to-ms conversion. |
| Original receipt → successful receipt observation ≤200 ms | Publish successful fraction; operator goal remains open until measured | Later same-hash/block canonical confirmation; all eligible outcomes retained. |

The retained 53–153 ms decode intervals already exceed the proposed 40 ms dispatch budget before route preparation. Native warming alone cannot pass it. Source/proof endpoint diagnosis must establish that remaining proof latency can fit the budget; if it cannot, report the target unmet instead of starting a new clock after proof.

Predeclare the universe for the currently armed chain-56 roster and supported venues: automatic native/on-chain events deduplicate by exact source transaction/wallet/mint; independent J7 events use their stable upstream event ID/wallet/mint when exact source identity is unavailable. Opaque events retain a source-identity/overlap-unknown stratum without a new eligibility gate. Unknown cross-feed overlap prevents a deduplicated full-universe verdict. This is the observed signal universe, not every on-chain buy. Record receipt even if no attempt is created, using bounded asynchronous timing publication and an explicit loss counter. Candidate warming must not define eligibility or the denominator.

Publish warm/cold, native/shared, venue, quote asset, source feed, proof-ready/proof-late, wallet-busy and clock-valid strata. Keep independent policy refusals, unknown eligibility, funding shortages, quote failures, reverts, ambiguous submissions, observer gaps and confirmed fills separate. Missing successful sub-200 witnesses count as unmet observations in the eligible denominator. Unknown eligibility prevents a full-universe verdict. Publish successful witnessed-fill p50/p90 with n alongside that conservative fraction; a fast-fill-only percentile cannot pass the all-event goal.

Required gates:

1. Failing behavioral regressions reproduce mixed clocks, candidate-after-buy preparation, stale/wrong-state quote reuse, exact-key/generation rejection and overload starvation before the repair. No money-gate, signed-floor, nonce/claim, lock-lifetime or final-sizing change.
2. Instrumentation publication overhead has measured p50/p90; no new synchronous I/O, retry, wait or lock on filling. Saturated warmer/observer preserves live capacity and essential refresh cadence.
3. At least 100 unique organic eligible signals in the declared cohort, at least 30 complete confirmed witnesses in each stratum for which percentiles are claimed, and at least 95% witness coverage among confirmed fills. Smaller strata remain unproved; no funded test fills are manufactured.
4. The native prepared dispatch targets pass in their declared stratum. Shared-path latency, busy misses and cold/proof-late outcomes remain visible even if native warm passes. Report the actual sub-200 fraction and B+1 improvement; do not label the overall 200 ms goal achieved unless at least 90% of all eligible signals have a successful sub-200 witness later confirmed, including cold/busy/unknown outcomes. Even such an empirical pass is not a universal guarantee.
5. Freeze release SHA, effective config, roster, provider indices, fees and observation contract. Compare matched organic strata; historical mixed-clock ACK traces cannot provide an old exact-execution distribution.

Roll out clock/stage measurement first, native preparation second, useful shared preparation third, then consider an endpoint change only if diagnosed. Each activation is its own release/cohort. Use the pinned runtime and reservation/reconciliation/frozen-contract gates; deploy with `--defer-backup`. Verify runner and web release/PID, authenticated status, effective knobs, quote balances, reservations, recovery and heartbeat separately. No organic evidence means benefit remains unproved.

Rollback speculative preparation/observer by disabling their admission and invalidating published generations, then restore the prior pinned release/config. Do not discard signed/unknown claims or alter open-position policy. A half-started worker cannot leave usable cache records; unchanged authoritative receipt recovery must still handle in-flight transactions.

## 8. Money-path edge cases and explicit exclusions

| Edge case | Required behavior |
| --- | --- |
| Unsolicited transfer, wrong signer/payment/wallet/mint/chain, opaque J7 classification | Automatic on-chain/native copies retain exact initiation/payment/acquisition proof; independent J7 retains its current policy. Opaque source identity prevents bound cross-signal quote reuse/B+N attribution; candidate warmth grants no entry authority. |
| Missing vs zero vs invalid balances, price, supply, nonce, timestamps | Typed unknown; zero remains valid where allowed; nonfinite/negative/malformed values never publish executable evidence. |
| wei/BNB/USD confusion, decimals or near-identical mint/pool identity | Exact atomic units and canonical chain/custody/route identity; no float atomic quantities. |
| Source reorg, removed log, mixed read blocks, dynamic tax, Flap graduation, Infinity key/hook change | Discard unmatched preparation; retain current live proof/safety/quote behavior and canonical recovery. |
| Final clip, config, custody, price or nonce generation changes during warming | Validate final exact key and existing signing generation; stale completion cannot overwrite current evidence. |
| Timeout, rejection, 429, ban, null, partial or wrong response | Typed failure and current backoff; no swallowed error becomes empty safe state; no new entry-path retry. |
| Shared connection/module/env mutation; worker overload; essential refresh or live capacity pressure | Worker-owned clients and connections, immutable publication, shared rate budget and nonblocking skip; no capture/restore globals. |
| Simultaneous native/J7/shared signals; unsigned shortage; late native queue | Existing origin/claim/fallback authority stays intact. No new queue/signing concurrency or reset of the two-second hint/original clock. |
| Signed/submitted unknown; nonce floor above snapshot; process restart | Preserve exact durable identity/reservation, fail closed on stale generation and use existing receipt recovery. |
| UTC correction changes or cross-boot timestamps; timing queue full | Monotonic boot-bound intervals or explicit unknown, original T0 retained, loss counted; historical clocks unchanged. |
| Successful observation later reverts/forks or never confirms; wrong booked hash | No verified sub-200 success and no accounting mutation from the observer. |
| Rollback during an in-flight preparation/send | Invalidate only speculative generation; preserve signed bytes/claims and authoritative recovery. |

**Not handled:** universal 200 ms inclusion/finality; unknown first-mint prewarming or discovery-disabled coverage; expired watcher-price cold reads; greater provider quota; new funding inventory; pending-source authority; new shared safety policy; new local V3/Flap/Infinity engines; native burst concurrency/async settlement; historical missing clock/provider snapshots. These limits must remain in the operator report.

Implementation: [BSC entry-speed plan](../plans/2026-10-11-bsc-sub200-entry.md). No production change or funded retry has been made for this design.

### 2026-10-14 amendment: unused hinted safety preparation

A mandatory code/route refusal returns without joining irrelevant hinted slot discovery. Speculative discovery remains bounded by the existing read deadline and a nonblocking in-flight gate; BSC warm-job and cached inline safety checkers share that gate, so refusal bursts cannot queue unbounded jobs. A simulation that needs its attempt's discovery still waits within its original deadline. Required code, route and simulation checks remain authoritative. MEMECOIN_BSC_FAST_PARALLEL_SAFETY=0 retains the sequential caller path and does not disable safety. This changes scheduling, not admission, and carries no organic latency claim.
