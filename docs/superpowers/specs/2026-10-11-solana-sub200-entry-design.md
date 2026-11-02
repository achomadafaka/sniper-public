# Solana entries within 200 ms of seeing the signal

**Status:** approved architectural design, 2026-10-11. The operator approved the isolated TPU read-provider exception. Implementation and production activation remain pending.

**Operator intent:** resolve Frank's cold route preparation and initial TPU `stale_slot`, then make actual successful entries occur within 200 ms of the original signal receipt. A fast submission acknowledgment does not satisfy this goal.

2026-10-13 bounded amendment: supported DFlow early candidates may supply a
bounded account-discovery batch to the existing chain-validated pool cache, and
preparation hints wake its idle owner while retaining pacing and original ages.
See [the preparation and fee-allocation contract](../../audits/2026-10-13-solana-pool-preparation-fee-split.md).
This does not prove earlier complete delivery or same-slot acquisition.

## 1. Evidence and limits

Frank's Dacity entry, pending `5165`, position `73356`, source slot `455637876`, landed in `455637878`. The retained trace gives approximately 181 ms receipt-to-enqueue, including 45 ms learning an unknown PumpSwap pool and 135 ms preparing an uncached USDC-to-TTWO funding leg. The final queue took approximately 0.13 ms; decision/build/sign took approximately 9 ms and first submission acknowledgment approximately 12 ms. Recognizing confirmation took approximately 464 ms and receipt/accounting approximately 398 ms. These are one entry's stages, not measured p50/p90 distributions. The retained historical subcall and initial TPU guard snapshots are absent. The current probes and producer/consumer review below resolve the dependencies and select the repair; they cannot invent those missing snapshots.

Removing both cold misses would leave approximately 21 ms to the first acknowledgment in this trace. That arithmetic does not predict landing time: neither acknowledgment nor confirmation observation is execution time. The exact millisecond landing time cannot be reconstructed from the currently retained slots.

The bounded current-route [probe](../../audits/2026-10-11-frank-entry-speed/probe-readme.md), on separate paper quota, measured a cold funding leg at 109.523 ms: quote-mint read 23.846, quote GET 40.381, swap-instructions POST 21.666 and ALT batch 23.390 ms. A pooled client with cached ALT measured 73.768 ms: mint 23.472, GET 23.713 and POST 26.408 ms. Local parsing was below 0.1 ms. The separate pool read took 40.984 ms and identified a Pump-migrated pool. This current analog used one Meteora DLMM hop and one ALT; Frank's original leg used two hops. It identifies present dependencies and a pooling/cache opportunity, not the missing historical 135 ms split or a latency distribution.

The bounded post-release audit found 64 Solana attempt rows and only one filled row, Frank's. That sample cannot support live fill p50/p90. Retained attempt rows are also not the full received-signal universe.

The existing [S-landing design](2026-10-09-memecoin-slot-lane-s-landing-design.md#1-the-problem-measured) explains why receipt head equal to source slot is node-relative and cannot establish the leader's slot phase. Do not convert slot differences to milliseconds with a fixed slot duration. The earlier spec's historical sender/fee observations do not prove the current Nozomi arm's effect.

## 2. Existing seams to reuse

| Responsibility | Current seam | Required use |
| --- | --- | --- |
| Original signal time | `memecoin_slot_signal.run_signal`, socket receipt before parsing; Shyft `run_feed` receipt before decode | Retain original `recv_monotonic`, exact source identity and feed; never reset on retry, proof resume or cold preparation. |
| Signal/queue/dispatch timing | `memecoin_slot_lane.SlotLane.handle`, `_boundary`, `_traced` | Extend existing in-memory trace fields, not synchronous measurement writes. |
| Pool warming | `PumpSwapPoolCache.offer_stream`, `warm_persisted`, `learn`; `start_slot_lane.on_migrated` | Learn verified migration/pool facts off the filling path; publish only complete pool identity. |
| Exotic quote legs | `raydium_launchlab_buy.WarmExoticLegs`, `OnDemandLegWorker`; `memecoin_slot_lane.refresh_extra` | Seed known quote/clip candidates before use, reuse existing refresh/freshness and cold fallback. |
| Native proof | `memecoin_source_initiation.solana_program_initiation`; confirmed receipt resume | Preserve signer/payment/acquisition authority and original 500 ms candidate deadline. |
| Signing/submission | `solana_slot_executor.execute_prebuilt_entry`; shared submission transport/provider seams | Preserve signed copy-price floors, write-ahead identity and identical-byte sends. |
| Deferred accounting | `DeferredSlotEntry`, `SlotConfirmations`, `rpc_confirm`, exact receipt parser | Keep confirmation and booking authoritative; measurement never finalizes money state. |
| Measurement storage | separate `data/memecoin_fill_timing.db`, resolver and read-only views | Store execution observations off-path; preserve unknowns and avoid funded-ledger write contention. |

Current references: `memecoin_slot_signal.py:1063`, `memecoin_shyft_signal.py:184`, `memecoin_slot_lane.py:889`, `:1271`, `:1312`, `:1850`, `:3582`; `pumpswap_buy.py:511`; `raydium_launchlab_buy.py:725`, `:778`; `solana_slot_executor.py:149`, `:179`, `:307`; `solana_real_seams.py:2096`, `:2463`; `memecoin_fill_timing.py:130`. Line numbers describe the reviewed checkout and may move.

## 3. Selected approach and rejected shortcuts

Use existing bounded pool/leg preparation, repair the exact TPU freshness defect, and observe processed execution independently. This removes known preparation waits while retaining money invariants.

- Shortening confirmation polling makes the result visible sooner; it does not make the transaction land sooner. It can be assessed separately after the execution witness exists.
- Raising fees, adding paid feeds, changing custody inventory or relaxing price/proof gates changes policy or cost. This design does not authorize those changes. If the prepared path still misses 200 ms, report the remaining leader/detection/network evidence before proposing them.
- A universal 200 ms guarantee is unsupported: a first unseen route, required late proof, unavailable providers and leader timing can exceed the budget. These remain visible failures or unknowns against the operator's goal; they are not removed from the evidence.

## 4. Remove preparation from known first encounters

### 4.1 Pool identity before a source buy

Reuse migration delivery and the pool cache's existing background owner. Measure migration notification-to-offer, queue, read and publish separately. Record whether a buy's pool was already known when its original signal arrived. A socket subscription acknowledgment is not delivery or complete pool coverage.

The current CreatePoolEvent `canonical` predicate includes quote mint equal to wSOL (`pumpswap_buy.py:250`), which can exclude a migrated exotic-quote pool from early warming. Separate verified Pump migration identity from the wSOL-only canonical route restriction, then admit only fully validated supported quote pools to preparation. This concrete predicate does not prove that Dacity's TTWO CreatePoolEvent existed on, or reached, the migration listener; retain that historical gap.

After a successfully validated pool learn, publish a bounded preparation candidate naming network, venue, pool, base mint, quote mint, token programs and builder. Retain immutable validated quote-mint safety facts with their original monotonic read timestamp in that same complete pool snapshot: PumpSwap currently drops this evidence, and `_live_quote` otherwise performs another RPC before checking the warm leg. Reuse the existing stamped-snapshot path in `_live_quote`, retain its 300 s recheck, and never refresh a timestamp without a new read. The first completely prepared buy must make zero provider calls through pool/quote validation, leg lookup, build and signing. Do not seed another worker's `_quote_seen` by mutating shared state. Invalid ownership, incomplete data, unknown decimals, unsupported extensions and provider failures must not publish a usable candidate. Learning may use a hint; funded eligibility still comes from the source buy and current builder validation.

Do not add pool reads, retries or waits to `SlotLane.handle`. If migration delivery is absent or late, the existing bounded miss learner keeps its original deadline and handoff behavior. A pool first exposed by the source buy remains a cold case.

### 4.2 Quote preparation before use

Current `WarmExoticLegs` is after-use only: at most two entries, a 60 s window, refresh at most every 1.5 s, and a service margin inside the existing 3 s leg-age bound. A migration/pool candidate may seed the same bounded warmer before first use; it must not start an unbounded quote sweep over all mints.

Prepare only configured clips derived from current roster/wallet caps and existing supported clip tiers. The funding leg key remains exact in venue, quote mint and USDC atomic clip, and its evidence binds to the custody owner, token programs, route policy, response identity, lookup tables and fetched time. No arbitrary larger cached clip may increase the approved stake. A final scaled clip without an exact supported warmed leg follows existing bounded cold preparation.

Current warm `note` is seeded after an admitted live source buy to bound provider load. Extending it to every migration would remove that resource bound. Proposed pre-seeding therefore requires a verified migrated eligible quote pool plus at least one current armed exact clip candidate, and retains the existing two-entry/in-flight budgets. No scan of all persisted pools or broad quote enumeration is introduced.

Coalesce pending preparation by exact key into at most two pending candidates. Candidate replacement cannot trigger an immediate fetch: fetch and refresh share the existing 1.5 s cadence and two-leg ceiling, at most four Jupiter HTTP calls per 1.5 s for the exotic warmer, including initial preparation. These calls use the existing funded leg seam and its bounded failure handling; they are additional to existing SOL legs, exits and other funded quotes. Activation requires measured remaining provider capacity and essential balance/blockhash freshness; the two-entry limit alone is not a request-rate limit. Queue overflow or no capacity stays a visible cold miss. Refresh scheduling must return without network work on the essential snapshot refresher. Move the existing warmer's fetch work to its own single bounded background worker with the two coalesced pending keys; publish complete results through the existing warm cache. Preserve both `OnDemandLegWorker` live in-flight slots: speculative work never occupies them, even when its HTTP call cannot be preempted. Use an isolated client/connection for this background owner and retain the explicit shared-key request budget; thread separation alone does not create provider capacity. A saturated warmer must leave live cold admission capacity and essential snapshot-refresh cadence unchanged.

Candidate replacement is deterministic: active recent-use entries first, then the newest completely verified pool candidate. Distinct exact clips compete for the same two slots: warming a $180 clip does not serve a $175 clip, and more than two useful identities leaves explicit miss coverage. No HotQuoteShadow snapping or sizing change is introduced.

Track initial candidate time, last actual use and scheduled refresh separately. Refreshing does not update last use or keep an unused candidate immortal. Unused candidates expire after the existing warm window; used entries follow the current use-based lifetime. Leg freshness always uses the actual fetch timestamp. Publish candidates through the existing background ownership; do not mutate shared env/module attributes or add a lock to the entry thread.

A 401/429, timeout, rejected route, empty/partial response, stale identity or failed refresh retains typed failure and the previous leg's original timestamp. Stop warming according to existing backoff. Do not let preparation occupy essential balance, blockhash or source-feed refresh ownership. Report calls, queue time, hit/miss reasons, refresh failures and age so a speed improvement cannot conceal provider starvation.

The historical 135 ms subcall breakdown was not retained and cannot now be recovered. The current analog identifies the mint, quote, instruction and ALT dependencies; instrument those existing boundaries in future cold preparation, with timestamps collected in memory and publication off-path. Existing quote and swap-instruction dependencies remain sequential where one consumes the other's result. Pool and leg reads may overlap only once the verified quote identity is already known; never guess the quote mint to start a funded route.

### Preparation retention repair, 2026-10-19

Retained preparation snapshots now coalesce by venue, quote mint, quote token program and quote decimals, keeping the newest verified pool snapshot per identity and at most two identities. Previously, two pools sharing one quote could displace another quote before a clip change. Inventory changes reseed exact current clips through existing validation. Warmer capacity, pacing, freshness, live pool checks and execution deadlines remain unchanged. The regression proves retention; it does not prove timely landing or the cause of incident `50a23938`.

## 5. Repair TPU source alignment and missing guard evidence

The sidecar's public `slotsUpdatesSubscribe` feeds Agave's private leader estimate, while the entry lane passes a Helius processed head. The guard refuses a clock frozen for 1000 ms or an estimate more than two slots behind that head. Both conditions currently return the same `stale_slot` without a snapshot. At Frank's first resend the estimate was two slots behind, with warm QUIC connections; 17 of 19 earlier rebuild triggers had a moving clock less than 1000 ms old. Clock disagreement is a verified recurring defect; Frank's exact initial frozen-versus-behind subtype is unavailable.

### 5.1 Capture the exact decision without blocking

Capture `estimated_slot`, `minimum_slot`, `since_advance_ms`, `clock_frozen` and `behind_head` once at the existing guard, from the same client snapshot used for send selection. Return optional numeric/boolean metadata alongside the unchanged `not_sent/stale_slot` result. Use existing `SubmissionAttempt.provider_timing` and the asynchronous submission audit owner to persist the sanitized snapshot; the existing audit logger needs an explicit TPU snapshot branch because it currently emits only HTTP post timings. Do not add a synchronous log, DB write, RPC, lock, retry or new provider attempt. A lost response remains ambiguous. Old consumers tolerate absent optional fields; producer and consumer deploy together.

Cold route boundaries likewise use the existing thread-local timing sink in each on-demand worker, retaining original signal identity and total; quote-safety read, GET, POST, ALT read and parse times publish on the existing deferred trace/audit path. Restore only thread-local sink state, never process globals.

Relevant changes: `rust/solana-tpu-sidecar/src/delivery.rs:839`, `src/main.rs:58`; `sniper/execution/solana_tpu_sidecar.py:123,184`; `submission_transport.py:53`; `submission_wiring.py:155`; `pump_curve_buy.py:681`; `raydium_launchlab_buy.py:1023`. Keep `record_submission_attempts`' money/audit identity unchanged; no funded schema migration is needed for optional trace metadata.

### 5.2 Selected source repair

Prefer the existing `MEMECOIN_TPU_RPC_URL` selector with an isolated mainnet Helius research/read endpoint, on the same provider/region as the native source but on a separate credential. Rust already derives WSS from this endpoint. This updates the very estimator used for leader sockets and preconnection; changing the two-slot guard alone would leave wrong leader selection intact. No Agave fork, custom leader cache or new entry-path I/O is required. Relevant seams: `memecoin_real_runner.py:1154`, `settings/memecoin.py:4033`, Rust `main.rs:204`.

The 20 s paired subscription [probe](../../audits/2026-10-11-frank-entry-speed/slot-clock-results.json) ACKed both subscriptions on both surfaces. Public FirstShred delivery was 104 ms median, 412 ms p90 later over matched slots. After transforming `Completed(slot+1)` correctly, matched SDK-input delivery offsets were 36 ms median, 258 ms p90. The separate [pinned SDK replay](../../audits/2026-10-11-frank-entry-speed/slot-clock-sdk-replay.json) applies the real last-12/median/48-slot outlier rule and censors startup history: across 18.387 s of common coverage, the public estimate fell three-to-four slots behind the watch head for one 296.423 ms episode; the watch-derived estimate never fell behind it. Neither replay models live sidecar rebuilds or cached schedule publication, and neither reconstructs Frank's missing snapshot. Validate the actual pinned SDK in an isolated **no-send** sidecar control before selecting or activating the endpoint. Source alignment is evidence-backed; eliminating all guard refusals remains an acceptance condition, not an assumed result.

Keep the 1000 ms frozen-clock guard, two-slot limit, cached schedule bounds, mainnet identity, private estimator outlier handling and identical-byte sends. Native `HEAD` remains a reject-only hint. Do not promote it into leader authority: its current notification parser does not bind the slot frame to the acknowledged subscription ID or a connection generation. Unknown, stale, regressing, future or epoch-mismatched inputs never fabricate a current leader window.

Implementation-plan review found that current sidecar health checks leader availability and clock freshness but performs no genesis/mainnet check. The no-send control must make one bounded `getGenesisHash` read on the selected isolated endpoint before starting its child and require the existing mainnet constant. This proves endpoint-reported cluster identity; Agave still trusts that source for epoch/schedule data. Health alone must not be described as independent schedule verification.

This selector changes the current **public-only/keyless read-provider policy**. The operator approved this isolated read-provider exception on 2026-10-11; capacity and no-send validation remain required before activation. QUIC sends, current fees and custody remain unchanged. Never use custody or native-watch credentials for the sidecar. Confirm isolated HTTP bootstrap/schedule-refresh and WS capacity, typed ACKs/errors, secret redaction, startup mainnet/epoch/schedule identity and the absence of pressure on funded quote capacity. If a separate read quota cannot support both HTTP and WS, the smaller alternative is one `MEMECOIN_TPU_WS_URL` setting passed to the resolver's existing `ws_url` parameter while HTTP stays public; do not build this extra option unless capacity makes it necessary.

The exact rejected Frank snapshot cannot be replayed. Tests instead reconstruct the reviewed frozen and advancing-but-behind guard states and demonstrate the old diagnostic ambiguity; the repair preserves rejection and exposes the right reason. A source-control test replays aligned versus lagged input streams to prove leader selection follows the selected source. An isolated no-send control and organic initial TPU participation validate the source change; configuration alone is insufficient. Include disconnects, wrong ACK/source, stale/regressing/future slots, schedule/epoch rollover and missing leaders. Any timeout or crash after a write remains ambiguous, never permission to re-sign or release a reservation.

## 6. Witness actual execution without waiting for it

### 6.1 Definition

Let `T0` be the original signal's local monotonic socket receipt, before parsing. Let `Tp` be the first local socket receipt of an execution observation for the exact submitted signature, with successful processed status and a positive slot. Later require the confirmed successful exact receipt, matching landing slot, custody wallet, purchased mint and validated payment/acquisition.

Then `Tp - T0` is a conservative upper bound on the observed successful processed execution: that execution preceded its observation. Report the result precisely as **successful processed observation within 200 ms, later confirmed with the same signature and slot**. This is the best proposed observable execution criterion, not a reconstructed canonical wall-clock timestamp. Logs supply no fork identity; the same slot number alone cannot establish the canonical execution instant. A bound above 200 ms does not prove execution exceeded 200 ms because provider delivery may be late. A rollback/error, slot mismatch, failed transaction, unreadable receipt or missing observer invalidates or leaves the witness unknown.

Solana's official [commitment documentation](https://solana.com/docs/rpc#configuring-state-commitment) says processed state can roll back; confirmed state requires supermajority stake votes. This witness supplies measurement, never confirmation authority.

### 6.2 Observer ownership

Before trades arrive, maintain one bounded observation subscription for the custody wallet's transactions, independent of admission. Prefer an already provisioned processed transaction stream with a separate measurement-only filter; otherwise use one pre-established processed `logsSubscribe` mentioning the custody pubkey. The official [logs subscription reference](https://solana.com/docs/rpc/websocket/logssubscribe) defines a single-address mention filter and supplies signature, error and context slot. Neither a mint-only subscription opened after booking nor a post-send subscription that races execution proves complete first observation.

Stamp on socket receipt before parsing. Append the minimal immutable observation to a bounded queue; the observer's own recorder joins exact pending/attempt identities and writes the separate timing DB. The entry thread neither opens a socket nor waits for subscription acknowledgment, writes, locks, reads or observer readiness. Observer absence does not refuse or delay a trade.

Preserve original `recv_monotonic` in the existing trace publication and associate it with host boot/clock identity captured at startup. Same-host observations use that same monotonic domain. Restart or cross-host matching without a valid shared domain is unknown; cross-host comparisons require calibrated clock bounds, using the existing feed-host benchmark's bracketing approach. Never substitute upstream `createdAt`, raw wall clock or decision start for `T0`.

Bind records by exact network/signature/source wallet/mint and attempt/pending identity. Retain first observation; duplicates never move its timestamp. Bound orphan retention and queue capacity, record overflow/reconnect/subscription gaps, and preserve first-seen evidence across a delayed DB join. A measurement filter must not add the custody wallet to the armed trader roster or produce an entry event.

The official [signature subscription reference](https://solana.com/docs/rpc/websocket/signaturesubscribe) distinguishes status at the requested commitment from `receivedSignature`. The latter is node receipt, not execution, and cannot satisfy this goal. A future signature-specific observer must remain asynchronous and report its subscription race; it is not the preferred complete witness.

Confirmation, exact receipt accounting, unknown reservations and recovery remain at the existing seams. Do not shorten booking truth by treating a processed notification as an owned position.

The current ledger's `fill.qty` is floating-point and does not independently retain both atomic payment/acquisition deltas or the actual confirmed receipt slot. Extend the existing deferred accounting trace with these facts from its already-cached successful receipt; no additional RPC, booking gate or commit. Validate signed-payload/payer identity locally from the retained pending payload. Historical rows without this evidence remain unknown; never reconstruct atoms from `qty` or credit-adjusted `notional`.

## 7. Budgets and organic acceptance

These are proposed engineering budgets, not measured distributions or guarantees:

| Metric | Proposed p50 | Proposed p90 | Scope |
| --- | --- | --- | --- |
| Original signal to dispatch | 20 ms | 40 ms | Proof-ready, completely warm candidate. |
| Original signal to first provider acknowledgment | 35 ms | 60 ms | Same scope; acknowledgment is transport evidence only. |
| Successful processed observation, later confirmed same signature/slot | 120 ms | 200 ms | Observable execution criterion; canonical millisecond execution time remains unavailable. |

The first two leave most of the 200 ms budget for delivery and execution. They do not replace the third. Warm readiness, proof readiness and leader-relative arrival are strata, not new clocks.

Freeze the observed event universe and release/config identity before evaluating: unique exact source signature, source wallet and mint for the current armed roster and supported venues. The universe is received signals, not every buy on the network. Retain every outcome: independent policy refusal, missing proof, cold miss, expired preparation, submitted failure, unknown submission, confirmed fill, witness gap and verified sub-200 fill. Do not define the denominator from successful fills or the warmer's hits.

Publish both (a) observed-execution p50/p90 for completely witnessed confirmed fills with their sample counts and (b) sub-200 successful processed observations, later confirmed same signature/slot, divided by all policy-eligible received events, with unknown eligibility and observer gaps separately visible. Also publish warm/cold and proof-ready/proof-late counts, feed, venue, quote asset, candidate age, landing slot delta and attempt fee. Refused entries do not become latency zeros; unresolved ones do not disappear.

For the all-admissible p90 target, keep cold, proof-late, failed and unknown events in the denominator: a missing successful sub-200 witness is an unmet observation, not a fast success. At least 90% of that universe must have a complete successful sub-200 witness to demonstrate the proposed p90 criterion. Report both that conservative fraction and the censored complete-witness distribution; never use cache hits alone to pass the goal. Unknown eligibility prevents a verdict for the full candidate universe until its coverage is stated and resolved.

The organic acceptance gate requires:

1. No money invariant or gate change; focused regression coverage includes zero provider reads on the first completely prepared buy, saturated speculative warming preserving both live cold slots and essential refresh cadence, exact key/expiry/generation rejection and all edge cases below and the synthetic old-defect regressions demonstrably fail before the repair; Frank's missing historical snapshot is not claimed as captured replay.
2. Added receiver/trace publication has measured p50/p90 in microseconds. Prepared-entry dispatch shows no regression beyond measurement uncertainty; no synchronous measurement dependency was introduced.
3. At least 100 unique organic submitted entries in the declared cohort, at least 30 complete successful witnesses per reported warm/cold stratum, and at least 95% observation coverage for confirmed fills. If nature supplies fewer cold cases, cold performance remains unproved; no fabricated funded transaction fills the sample.
4. Warm dispatch/acknowledgment meet their budgets, the complete execution-observation distribution has p90 at most 200 ms, and at least 90% of all admissible received events, including cold/proof-late/unknown outcomes, have successful sub-200 witnesses later confirmed same signature/slot. Report the all-event success fraction and every unknown even when the warm stratum passes. A pass for warm entries alone is not a pass for all entries.
5. Compare exact organic before/after cohorts by the same source/venue/quote/readiness strata and clock definition. Historical rows without processed witnesses cannot establish the old execution distribution; use their measured dispatch stages and slot gaps only.

A 200 ms universal service guarantee remains unproved even after this empirical gate. The system must expose any observed miss immediately and its named stage, rather than claim that a fast acknowledgment achieved the operator's goal.

## 8. Money-path edge cases and exclusions

| Case | Required behavior |
| --- | --- |
| Transfer without a signed linked payment; wrong source wallet/mint/signature; opaque provider ID | No new authority. Existing initiation validation remains decisive. |
| Missing, zero, malformed or nonfinite balances, prices, outputs or timestamps | Unknown stays distinct from zero; invalid values never seed a usable warm leg or execution witness. |
| Pool learned from an unsolicited or wrong-program hint; partial metadata; unsupported mint extension | No warmed funded route until complete identity and existing token checks succeed. |
| Final clip changes; wallet cap changes; token program/route/lookup-table identity changes | Exact warm-key/contract validation; mismatch uses existing cold/refusal behavior, never oversizes or weakens the floor. |
| Quote expires between publication, dequeue and signing | Existing builder freshness and signed copy-price floor remain authoritative; never refresh timestamps without new evidence. |
| Simultaneous native/J7/watcher deliveries; late proof; resized leg; duplicate completion | Preserve current exact ownership, one signed attempt, CAS and original receipt deadline. This design adds no J7 backup authority. |
| Provider timeout/rejection/429/ban, empty/partial/wrong result; warm worker overload | Typed failure, existing backoff and bounded cold path; no new synchronous retry or essential-refresh starvation. |
| Module/env/shared-connection mutation, lock contention, warmer replaced while in flight | Single existing background owner, immutable publication, generation-bound results; stale work cannot overwrite a newer identity. |
| Signed/submitted ambiguity, sidecar crash, provider accepts then times out | Preserve signature/reservation and existing recovery; never re-sign or fabricate safe release. |
| Processed fork, failed transaction, slot mismatch, unreadable confirmed receipt | No successful execution bound until exact confirmed evidence agrees; booking stays unchanged. |
| Observer disconnect, silent subscription, queue overflow, duplicate or reordered frame, first observation before DB join | Preserve first evidence and typed gap; keep trades unaffected and denominators honest. |
| Host reboot, clock-domain mismatch, backward interval, uncalibrated remote clock | Latency unknown; no reconstructed millisecond claim. |
| Cold/unknown pool or quote, required receipt proof arriving late, congestion or adverse leader phase | Continue existing policy and record the miss; this design cannot guarantee 200 ms for these cases. |

No changes to entry sizing, source authority, safety thresholds, slippage/copy caps, funded admission, fees, exits or custody balances are authorized by this artifact. The isolated TPU read-provider exception above is approved.

## 9. Rollback and partial rollout

Land passive measurement/guard diagnostics first, then independent commits for preparation and the evidence-backed TPU source repair. Retain an organic baseline before activating speed changes where practical; otherwise report absolute post-change execution observations and historical dispatch comparisons, with no comparative landing-gain claim. Never manufacture a funded baseline. Each preserves the original cold path and tolerates absent observation fields. Activate only the merged pinned release after the normal exact identity/reservation/reconciliation checks; no backup is added unless the operator requests it.

If preparation is disabled or rolled back, old cold preparation continues with original deadlines. In-flight warm work is generation-bound and cannot publish into a replaced cache. A partial rollout with pool hints but no quote preparation remains cold; one with preparation but no observer has no 200 ms execution verdict.

If the observer fails or rolls back, stop its separate owner, retain append-only measurement evidence and expose the gap. Existing confirmation/booking/recovery continue. A schema mismatch cannot prevent an entry; the observer fails visibly off-path. A TPU rollback restores its old request protocol/producer and consumer together while ordinary transport remains available; never roll back by releasing signed reservations or sending different bytes.

No historical entry is retried. Implementation and organic proof are separate deliverables; this approved design establishes neither a live speed gain nor a guaranteed fill time.
