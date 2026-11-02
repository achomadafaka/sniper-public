# Robinhood speed parity and shared subsecond prices — design and current evidence

Date: 2026-09-27. Scope: Robinhood chain **4663**, plus the shared price-delivery repair needed by Solana. This is a scoped engineering design and read-only verification, not implementation or funded activation.

**Verdict:** Robinhood needs restored verified on-chain detection, event-driven handoff, fresh prepared routes and independent live-mark delivery. Its direct sequencer sender and durable execution machinery already exist. **Solana has not achieved end-to-end subsecond price delivery:** the current release still drops a changed mark at +400 ms and publishes ordinary prices downstream of quote/exit work.

Implementation plan: [file-by-file tasks](/home/bigpapi/Desktop/Sniper/docs/superpowers/plans/2026-09-27-robinhood-speed-and-shared-subsecond-prices.md).

## 1. Verified boundary

Checkout and both pinned release symlinks resolved to `d34edd6f27b30ed6369a4ac0d531ecd6b02963c4` at the code/runtime capture. At the final check checkout had advanced to `581ed4fb`; both pinned releases remained `d34edd6f`. This design is bounded to the captured application code and does not certify later concurrent edits. The funded runner process executed the captured absolute release. This supersedes the preceding audit's `5f83faf7` runtime snapshot. The newer commits change mirror coverage, rail wake anchors and lane heartbeat reporting; they do not remove the price-tape floor or the ordinary publication dependency.

At 09:12–09:13 UTC:

- No open real positions; no reserved entries. Thus no organic held-position UI latency sample exists for this run.
- Solana heartbeat: live slot lane, 39 owners/subscriptions, 44 armed profiles, connected. A connection and subscription count do not prove every event was delivered.
- Robinhood: **60 armed profiles, zero verified armed addresses** returned by the actual watcher eligibility registry.
- The watchdog deliberately excludes 4663 at `scripts/memecoin_watchdog.sh:422`. The retirement comment records the operator decision. No 4663 wallet-watcher process was running. The old unqualified `onchain_evm_watch.heartbeat` was dated September 16; it is not present health evidence.
- Existing market-volume collection is separate from wallet detection. A running volume worker does not restore wallet-follow coverage.

Evidence: [runtime snapshot](/home/bigpapi/Desktop/Sniper/output/research/robinhood-speed-plan-20260927/runtime.json), [address coverage](/home/bigpapi/Desktop/Sniper/output/research/robinhood-speed-plan-20260927/coverage.json).

## 2. What “same speed as Solana” means

Use comparable boundaries, not chain-specific marketing labels:

1. Source transaction first observed by a trustworthy local reference.
2. Its actual owner, bought token, size and route validated.
3. Entry accepted by existing authority/risk gates.
4. Signed transaction identity persisted, then first provider dispatch.
5. Identity-matching acknowledgement.
6. Successful execution receipt/inclusion observed.
7. Fill accounted and position available to exits.

Report each interval, including cold-route, approval-needed, failed, refused and unknown cases. Solana processed observation and Robinhood executed-log observation occur at different points in their respective lifecycles; do not call both simply “source time.” Whole-second EVM block timestamps cannot prove subsecond delay.

The preceding bounded Solana sample had five successful slot entries at **622 ms median / 817 ms p90 local attempt total**, with dispatch around 21–35 ms after local attempt start. That is useful calibration, not a guaranteed SLO or proof of current-release performance.

A bounded sample of the latest 500 Robinhood attempts since September 25 contains 22 fills, at **5,068 ms median / 7,107 ms p90 local attempt total**. This is not the complete time-window population and is not matched against the Solana cohort. The latest sampled Robinhood fill, September 26 18:01:41 UTC, took **5,618 ms**. Stored stages include market data 945 ms, initial quote 2,869 ms, token safety 1,957 ms, approval 304 ms, broadcast 214 ms, confirmation observation 436 ms and balance delta 29 ms. Some stages overlap; **do not sum them**. Historic labels such as signing can include preparation; instrument the actual boundaries before interpreting them as cryptographic cost.

Practical parity target: warm supported Robinhood entries should reach successful accounted execution in the same **subsecond local-total class**, with a target p90 below 1,000 ms and p50 at or below 650 ms. Dispatch readiness and network inclusion get separate budgets. Cold discovery, new approvals and fallback routes remain separately reported; the system cannot claim universal parity while these cohorts are slow or censored. Do not weaken gates to manufacture the metric.

## 3. Current chain capabilities

Robinhood's official documentation says ordering is first-come-first-served at the sequencer; paying more does not jump ahead. The existing sender already includes its direct sequencer endpoint. Optimize trustworthy observation, preparation and arrival time. Gas must still cover execution/base-fee requirements. [Official chain overview](https://docs.robinhood.com/chain/)

The official endpoints include JSON-RPC, a sequencer feed and direct sequencer submission. Public endpoints are rate-limited and not recommended as production infrastructure. Do not assume a free endpoint's short benchmark proves continuous capacity. [Official connection documentation](https://docs.robinhood.com/chain/connecting/)

The documentation describes subsecond **soft confirmation**, followed by Ethereum posting and finality later. A transaction hash acknowledgement alone is not an execution receipt. Preserve reorganization and reconciliation handling. [Official finality documentation](https://docs.robinhood.com/chain/transaction-finality/)

A bounded public probe in this run found:

| Surface | Result | Limit |
|---|---|---|
| Official HTTP RPC | Correct chain 4663; four warm latest-block reads 112.5–120.9 ms | First request 1,188 ms; tiny sample. |
| PublicNode HTTP RPC | Correct chain 4663; four warm reads 21.6–45.8 ms | First request 1,094 ms; tiny sample, no quota/head-coverage certification. |
| Official sequencer feed | 12 JSON frames received; version/messages envelope, sequenceNumber, blockHash, signatureV2, blockMetadata | No signature verification, replay recovery, transaction execution or receipt reconstruction was performed. |

The two RPCs were sampled concurrently but their requests completed at different times. Do not subtract reported block heights as a precise head-lag estimate. These reads sent no transactions and used no funded credentials. [Raw probe](/home/bigpapi/Desktop/Sniper/output/research/robinhood-speed-plan-20260927/public-probe.json)

**Infrastructure decision:** start with a measured provider WS/receipt path and the existing same-byte sequencer sender. Compare actual log/receipt arrival on the same events. If it cannot satisfy the source-delivery budget, use a supported Nitro full node subscribed to the official feed; do not implement a partial EVM interpreter or treat the raw feed as `eth_subscribe` logs. The official node guide calls for 8+ CPU cores, 64 GB RAM (128 recommended), multi-TB local NVMe, and Ethereum execution plus beacon endpoints. Run it separately from the funded worker unless resource isolation is demonstrated. No node purchase or deployment is part of this task. [Official node guide](https://docs.robinhood.com/chain/run-a-full-node/)

Chainlink's “Data Streams” page describes an oracle product. It does not establish price coverage for arbitrary memecoins or our actual full-size sell routes. Use verified pool/curve state for these marks. [Official Data Streams description](https://docs.robinhood.com/chain/data-streams/)

## 4. Bottlenecks and existing machinery to reuse

| Path | Current evidence | Required change |
|---|---|---|
| Verified owners | `axiom_address_verify.watcher_address_owners`, zero eligible armed 4663 addresses | Resolve exact beneficiary/payer identities; preserve existing proof standard and reject routers/fee receivers. |
| Watcher launch | 4663 excluded in watchdog | Restore only after identity, source coverage and shadow acceptance; do not merely delete retirement guard now. |
| WS callback | `onchain_evm_wallet_watch.push_detection` fetches transaction and receipt; `on_transfer` also obtains quote-asset USD; `run_push_stream` invokes it inline | Socket callback queues bounded work; enrichment occurs outside receive loop. |
| Roster changes | Head branch calls on_head then continues before armed-set refresh | Refresh on monotonic timer/head cadence, even with no wallet Transfer events. A successful empty roster revokes subscriptions. |
| Detection handoff | `memecoin_runner_loop._onchain_lane_worker` sleeps default one second after each sweep | Wake durable consumer immediately; retain poll/cursor recovery. Process must not need a second signing engine. |
| Volume origin | Writer emits `4663:hash:mint`; lane adds `onchain:`; Robinhood volume parser expects bare hash | Shared chain/mint-validated parser, producer-to-consumer test. |
| Market preparation | Synchronous market lookup around 945 ms in example | Prepare immutable metadata and continuously refreshed market facts; decision uses valid current evidence, preserving all gates. |
| Route selection | Existing whole-ladder deadline 8 s; configured post-hit grace 1.5 s | Reuse proven route keys, quote actual amount on current state, measure alternatives; no first-response-wins shortcut. |
| Safety | Example 1,957 ms; parallel-safety setting already enabled | Measure components, overlap independent reads, reuse immutable facts only; dynamic sellability/route proof stays current. |
| Approval | Existing chain-scoped exact approval pipeline enabled for 4663 | Retain positive-acceptance requirement and nonce order; do not use unlimited approval as a speed fix. |
| Submit | `free_endpoint_registry`, `submission_backends`, `evm_fast_submit` already provide sequencer/same-byte fanout | Preserve identity-before-broadcast and four-state outcomes; warm connections and measure actual provider arrival. |
| Receipts | 250 ms initial polling, then slower fallback | Measure observed receipt delay separately; optionally use a head wake on the existing bounded receipt reader. No extra watcher per order. |
| Wallet lock | `memecoin_evm_leg` serializes entry/exit/de-risk including receipt wait | Measure wait. Do not remove lock while whole-wallet delta accounting depends on serialization. |
| Prices | EVM volume writer has useful decoders but inline backfill/metadata/prune; old hot engine is incomplete and defaults off | Bounded held-route state receiver, independent of broad capture and historical replay. |
| UI | SSE 250 ms; prices primarily come from stored marks | Add shared latest-mark snapshot, separate from historical recording and exact-exit decisions. |

**Store-first v4_multihop against the post-hit grace (2026-09-28, operator trade-off; measured on the 105 4663 ladders since 09-06 with a measured multihop hit, multihop moved to 0.6 s, grace 1.5 s):**
- No floor: the ladder ends p50 1.1 s / p90 3.2 s sooner, but 12 of 105 ladders pick a worse route (-27 pp in total), because relay or kyber lands 2.1-3.6 s after ladder start, after the grace has closed.
- Grace floor 3.6 s after LADDER start when the first hit is a store route: 0 of 105 are worse, and the ladder still ends p50 0.6 s / p90 3.1 s sooner, only where multihop was the slowest rung. A floor of 2.5 s after DECISION start does nothing, because the ladder starts p50 0.82 s after decision start.
- `MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST` stays 0 until the operator chooses. Batched pair-liquidity reads (`extsload(bytes32[])`, with next-slice measurement and hook-priced middle legs) ship either way. On 40 recorded 4663 coins, cold, the new provider path was checked against the old one: 0 routes lost, none worse by more than 0.09 pp, 2 routes gained.

**Post-hit grace on 8453, 56 and 5042 (2026-09-28, read-only replay for the operator's grace decision; `scripts/research/evm_ladder_grace_read.py`):**

Tonight's Base fill hit v3 at 76 ms, then waited out the full 1.5 s grace. This replay asks what a shorter grace G would have cost on every measured buy ladder in the ledger.

How the data is recorded:
- The rung records come from `venue_quote_with_v2_fallback` (`rung_record`). They ride on the `evm.quote_initial` and `evm.quote_miss` substages, which `MemecoinEntryTrace.finish` writes into `memecoin_entry_attempts.trace_json`.
- A rung's `duration_ms` is `perf_counter` time from that rung's own start. All rungs are submitted together, so this is its answer time from ladder start.

How the replay works:
- It applies the runner's rule. The first measured hit opens G. A curve hit ends the wait at once. The ladder takes the best measured round trip among the rungs that answered by then.
- It compares the result with today's 1.5 s grace. The runner started 2026-09-28T01:10:04Z, after the last `.env` write.
- "Worse" means a lower round trip than today's pick. "Saved" means today's ladder end minus the replay's ladder end.
- Some rungs were cut by the recorded ladder's own grace or deadline, so their answers are unknown. If a replay would have waited past such a cut, the ladder is counted as censored. None were censored at G ≤ 1.5 s.
- The recorded grace end sits −3.3 to +0.6 ms from the rung clock, so the replay treats limits within 25 ms of each other as the same.

| 8453 (15 ladders, 5 coins, 2 fills) G | worse than today | sum pp | worse > 0.5 pp | saved p50 / p90 |
|---|---|---|---|---|
| 0.25 s | 2 | 2.51 | 1 | 1.25 s / 1.25 s |
| 0.5 s | 2 | 2.51 | 1 | 1.0 s / 1.0 s |
| 1.0 s | 0 | 0 | 0 | 0.5 s / 0.5 s |
| 1.5 s (today) | 0 | 0 | 0 | 0 / 0 |

| 56 (50 ladders, 41 coins, 40 fills) G | worse than today | sum pp | worse > 0.5 pp | saved p50 / p90 |
|---|---|---|---|---|
| 0.25 s | 0 | 0 | 0 | 0 / 0.72 s |
| 0.5 s | 0 | 0 | 0 | 0 / 0.47 s |
| 1.0 s | 0 | 0 | 0 | 0 / 0 |
| 1.5 s (today) | 0 | 0 | 0 | 0 / 0 |

| 5042 (14 ladders, 10 coins, 7 fills) G | worse than today | sum pp | worse > 0.5 pp | saved p50 / p90 |
|---|---|---|---|---|
| 0.25 s | 0 | 0 | 0 | 0.02 s / 0.61 s |
| 0.5 s | 0 | 0 | 0 | 0 / 0.36 s |
| 1.0 s | 0 | 0 | 0 | 0 / 0 |
| 1.5 s (today) | 0 | 0 | 0 | 0 / 0 |

- **8453.** On 14 of the 15 ladders, v4_multihop was still unanswered when the grace ended. So every Base ladder waits the full 1.5 s for it, and any G saves 1.5 s − G on every ladder.
  - The rung that answers inside the grace and can beat the first hit is kyber. It answered 775, 928 and 970 ms after the first hit (n=3; the 970 ms answer had no route). A fourth ladder's kyber was cut by the grace itself.
  - At G = 0.5 s, kyber is lost once: ace7da22 would have taken v3 at −4.55% instead of kyber at −2.03% (2.51 pp). That attempt ended `route_failed`. The other worse row is 0.002 pp.
  - G = 1.0 s keeps all three kyber answers with 30 ms to spare. That margin is too thin to call safe on 3 samples.
- **56 and 5042.** No ladder picks a worse route at any G. Most ladders finish before the grace does: the first measured hit is v2 or v4 at p50 629 ms on 56 and 287 ms on 5042. So the median ladder saves nothing, and only the tail (p90) saves 0.4-0.7 s.
- **4663 (same replay, cross-check).** 306 ladders; 2 are left out because their only measured hit came after the 8 s deadline. G = 1.0 s is worse on 10 of them (15.4 pp, 7 by more than 0.5 pp), and G = 0.5 s on 17 (22.0 pp). Today's 1.5 s already loses 11 of 272 comparable ladders (50.9 pp) against waiting for every recorded answer, which is consistent with the store-first finding above.
- **Sample.** Base is 5 coins. Nine of its 15 ladders are one coin retried within 50 s (2026-09-25 16:20Z), and only 2 of the 15 filled. Any per-chain grace should be re-read at 30 or more Base ladders.

References are mapped to exact paths in the implementation plan. Retain direct/curve/multihop exit support; do not demote existing safe fallback routes to meet a target.

## 5. Solana subsecond verdict and shared repair

**Current result: not achieved end to end.** The prior passive sample received confirmed account updates in 371 ms median / 395 ms p90 relative to that node's first slot notification. That is neither source-chain creation time nor browser paint time.

The current release still follows account cache → normal refresh/exact previews → exit work → recorded marks → UI. `MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S` defaults to one second; no override was found. The actual mark writer again dropped a changed +400 ms mark in an in-memory fixture. The UI's 250 ms SSE poll cannot recover that missing observation. [Recheck evidence](/home/bigpapi/Desktop/Sniper/output/research/robinhood-speed-plan-20260927/solana-price-recheck.json)

Use one small shared latest-mark store in the funded runner. Existing Solana cache and new Robinhood held-state adapter publish to it. A separate bounded publisher writes a coalesced atomic snapshot; web reads that snapshot through the existing ticker. Reuse `sniper.util.atomic.atomic_write_json` and `state_path`; do not add Redis, Kafka, a new database or a second web service. Writes occur off stream, signing and exit threads; measure fsync cost and drop/coalesce obsolete pending snapshots rather than queue them indefinitely.

Historical mark tape keeps its existing recording purpose. Exact quote, funded executable peak and confirmation remain separate. A display mark can wake a candidate evaluation through existing rails, but does not establish a full-balance sell price.

One runner owns the shared snapshot. Robinhood's bounded market receiver runs as its background worker, so multiple processes do not race to replace the same file. The standalone wallet watcher remains a durable detection producer. On writer restart, change generation and publish unhealthy until fresh state is acquired; web must not treat an old snapshot as current.

For USD prices, all contributing legs must be healthy and current: token/quote state and quote/USD conversion. Keep both observation times. If the conversion is older than the strict freshness budget, label the USD price degraded; never reset its age just because the meme pool traded. Quiet pools need healthy subscription coverage and an unchanged-value label, not invented trades or repeated fresh source timestamps.

## 6. Hard acceptance criteria

These are requirements to implement and measure, not achieved claims.

| Metric | Acceptance |
|---|---|
| Validated received pool update → in-memory market mark | p90 ≤100 ms; no metadata, provider or DB wait on callback. |
| Trustworthy first local observation → browser-rendered new market price | p90 ≤750 ms and p99 <1,000 ms in healthy covered intervals; publish every breach, gap and unsupported route separately. |
| Healthy event coverage | At least 99% of independently observed supported held-route changes represented; missing events count as misses, never disappear from latency denominator. |
| Healthy display refresh | Newest valid state appears in <1 s; quiet unchanged state remains visibly checked. Broken/reordered/gapped state becomes visibly degraded in <1 s after local detection. |
| Full-position executable-price observation | Separately require <1 s observation age on ≥99% healthy held-position samples under maximum book if claiming subsecond executable prices; include quote start/end and state identity. Dynamic/state-unavailable cases fail this claim. |
| Robinhood warm source observation → dispatch | Target p90 ≤500 ms, separately reporting enrichment, queue/lock wait, preparation, approval and signing. |
| Robinhood warm valid entry → accounted execution | Target p50 ≤650 ms, p90 <1,000 ms; network/approval/cold cohorts reported separately. |
| Solana regression | No new synchronous entry I/O. Entry dispatch p50/p90 must not worsen materially under matched workload. |
| Exit reaction | Received rail-crossing mark → quote start p90 ≤100 ms; quote-to-decision separately measured. No optional audit work can stall the next decision. |
| Identity/recovery | Zero duplicate logical orders under crash/retry/ambiguous receipt fixtures; exact durable claim and pre-broadcast binding remain authoritative. |

Percentiles of stages do not add to the percentile of the whole path. Use complete causal traces for end-to-end verdicts. Network outages cannot be guaranteed subsecond; they must be visible and included in availability reporting. No stage may report successful subsecond service by excluding slow but otherwise eligible work.

Executable-price capacity is a separate gate. With N held positions and cadence Δ seconds, baseline read load is approximately `N / Δ × (balance_reads + quote_reads + route_proof_reads)` plus conversion, recovery and entry traffic. Measure actual per-venue counts. For three positions at 0.5 s and only one balance plus one quote read, the floor is already 12 reads/s; multihop/venue search increases it. A local cheap market mark does not satisfy this budget. Reserve capacity for actual exits before increasing observational quote rate.

## 7. Non-negotiable boundaries and edge cases

- Chain 4663 only for Robinhood execution changes; Solana shares publication and verification, not EVM nonce logic.
- Preserve current authority, amount, source-size/age, price ceiling, risk, loss, safety and exit policies. No arming, state repair, trades, deployment or purchased infrastructure in this planning task.
- No unlimited allowances, speculative fresh nonce after an ambiguous send, or removal of the wallet accounting lock.
- Reject wrong chain, wrong mint, wrong pool, wrong quote side and ambiguous source identity. V4 identity includes manager and PoolId; a 32-byte PoolId is not an address.
- Atomic quantities remain integers internally and decimal strings across JSON where needed; never JavaScript numbers above 2^53.
- Decode multi-hop, native/wrapped assets, token decimals, hooks/tax and graduation explicitly. Unknown never becomes zero or a healthy empty route.
- Gap, removed log, block-hash mismatch, reconnect, out-of-order event, missed subscription acknowledgement or stale conversion invalidates affected live state until reconstructed.
- Queue overflow is a typed coverage gap with cursor recovery. Metadata failures cannot stop receiving other events. Authority revocation is rechecked before admission.
- Source-time precision remains honest. Use process-local monotonic clocks for durations; use calibrated cross-process UTC or shared reference correlation with explicit uncertainty.
- Preserve 90-second post-close observation contract. Bound its population and report overflow; it must not take execution capacity from held positions.
- Keep signed unknown entry/exit ownership through restart and rollback. Backfilled signals cannot be replayed as fresh trades.

The plan does not promise protection from all copier dumps. A source may sell after other buyers, and an executable price may move before inclusion. Measure realized source-to-fill drift and post-fill liquidity alongside speed; do not raise slippage merely to improve successful-fill latency.

## 8. Verification performed for this design

Schema validation found no violations and 80 unknown-table warnings; the tracked diff whitespace check passed. **156 focused tests passed in 5.76 s** across price freshness, curve marks, rail wakes, EVM boundaries/seams, on-chain decoding and verified addresses. The price writer witness still reproduces the unmet subsecond requirement. Public probes were read-only and bounded. No funded transaction, live state mutation, service restart, authenticated browser render benchmark or new-release fill benchmark was performed.

The earlier preparatory Robinhood memo was source-only; this design corrects its largest operational omission: the direct wallet watcher is retired and currently has no eligible verified addresses. Any execution schedule must budget identity/coverage restoration first.
