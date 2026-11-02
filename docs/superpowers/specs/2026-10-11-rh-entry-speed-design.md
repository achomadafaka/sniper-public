# Robinhood entry preparation and execution evidence

The operator requested design, implementation, pinned rollout and verification end to end on 2026-10-11. Authority, sizing, safety, exact approvals and signed-price limits stay unchanged. The request authorizes execution of this design without another approval round.

## Incident and scope

RRR attempt `a67abf05aaf34988bf62916a2f915f2c`, pending 5174, fill 147975, position 73451 used `pons_shared`. Receipt-bound initiation and wallet-net source economics were available. It did not miss the native safety cache. The source Pons curve was graduated; the winning entry was hooked V4 pool `0xee8f223ef4b6033011202bbdb0e220ccba72c7157477eec1ed0acb0ba53f6552`.

Detection-to-submission-reply was 1,540.251 ms; detection-to-confirmation-observation was 1,857.337 ms. The 1,780.440 ms attempt starts at decision, 108.987 ms after detection. Submission follows the sender returning; it does not locate physical dispatch. No original monotonic clock or processed witness exists. Source block 85969586 to landing 85969603 is exactly +17 blocks, without a precise execution timestamp.

The V4 entry priced its actual clip only after a 135.377 ms registered launchpad information read and 107.624/26.357 ms two-direction key probes: **269.358 ms discovery/proof**. Actual-clip buy/reverse then cost 22.914/38.290 ms. Safety's 623.661 ms overlaps the 449.409 ms entry quote, leaving 167.131 ms additional wait. Signing cost 3.080 ms; exact approval cost 250.738 ms. Detailed safety storage-probe requests were not recorded.

Process logs identify incident release `eb7186fd049ec63f57f8302aa3ce9969cae85bca`; this is chronology evidence, not an immutable SHA on the attempt. V4 discovery/quote and early-quote modules are unchanged on current `bdc670679f0924bb8ac439d298c821bb47748571`.

## Selected design

### Prepare the existing discovery pipeline

Warm registered launchpad discovery through the existing V4 discovery pipeline before a signal. Use the same confirmed pool union, ranking, fee screens and existing cache lifetime: 600 seconds for a sane ETH pool, 30 seconds for provisional lists, at most 3 seconds when a derived pool is pending. Do not make the launchpad's current active pool or graduation permanent. Do not prefer the source curve: RRR demonstrates why that is wrong.

The runner's existing RH background preparation owns the work. Candidate messages may additionally name a bounded set of active tape mints, without enrolling them into native PASS handling. Cap candidates at 32 and retain the existing top-K selection limit. Use the configured research rotation and its current account-wide pacer, host identity and failure classification; no larger quota, faster pacing or funded endpoint is granted. One preparation job at a time, bounded completion deadline, no network call under shared account/state locks. Live pressure and stop/generation changes suspend or cancel speculative work. Native preparation receives priority with bounded fairness: allow one shared discovery job after two completed native checks. Newly signaled urgent native work and live entry pressure cancel speculative work. Ordinary due native refreshes remain queued; this reallocates existing background capacity and can reduce native cache coverage, which must be reported without weakening TTL/checks. Strict native-first cannot complete shared warming under sustained overload: eight approximately eight-read native checks at 2-second pacing require about 128 seconds against a 60-second TTL. The background HTTP adapter must cover every discovery request, including metadata and launchpad probes. Local stop/deadline/generation cancellation propagates through caught provider failures and is checked again immediately before cache publication; a durable fallback is not permission to publish a cancelled job. Default entry behavior retains its existing provider handling.

Entry consumers retain the existing in-memory discovery lookup; they never join a preparation Future, acquire a new network lock, wait for warming or retry it. A cold miss runs the original discovery. Concurrent cold misses may still duplicate work; do not introduce an entry single-flight wait to suppress them. Publish only through existing complete discovery/proof boundaries. Missing, malformed, wrong-identity, one-direction and unavailable evidence cannot become a confirmed key or safety PASS.

Do not transfer a native safety verdict to shared execution. Fresh actual-clip buy/reverse quotes, round-trip budget, code/sell safety, exact sell simulation, curve gates, buy preflight, post-peak confirmation, funds, exact approval and all signed minimum/copy-price bounds remain live and authoritative. Verified immutable storage/fact reuse may use existing proof seams only; it must not add a synchronous DB read or relax proof/TTL/amount identity. Any improvement outside these boundaries requires a revised evidence-backed design.

### Measure using receipts already read

Capture an immutable `ReceiptClock` before RH sequencer-frame decode. Preserve it through authenticated detection, pending-source reasks, decision records, lane payload, `LaneEvent`, native/shared hook and retries. Extend the existing EVM watcher clock creation from 56 to 4663; J7 clocks already travel through the generic trace. Missing, evicted, cross-boot or malformed clocks remain unknown; never reset T0 at decision or execution.

Use the first successful receipt already returned by the current RH confirmation path. Shared `ReceiptState` receives additive invocation-local observation timestamp/boot fields, stamped at the status-one read. An entry-only confirmation wrapper publishes the exact entry observation; approval and exit reads are excluded. Native execution stamps its receipt immediately before generation updates/booking, then publishes after releasing the wallet lock. Preserve the first successful timestamp across canonical retries.

Reuse the bounded publisher ring/worker with explicit chain identity and separate RH/BSC health, losses and namespaces. RH needs no additional head or receipt reader. Persist observation metadata off-path. Later canonical booking must agree on chain, exact transaction, success, block number/hash and actual real entry fill before an observation becomes a verified witness. No ACK, sender handoff, quote or paper fill qualifies.

Retain all authenticated received signals before route/pilot/safety/lock refusals, including those never creating an attempt. Use existing RH feed/decision records for those earlier outcomes and retain queue/drop uncertainty. Classify preparation readiness at original ingress: completed proof monotonic time must precede the immutable ReceiptClock and retain TTL, descriptor, chain/mint and the same published discovery list at ingress. Job generation must remain admitted through final publication; a later wallet operation does not expire immutable discovery proof. This label covers discovery only and proves no current nonce, funds or native safety readiness. A completion after ingress is cold for that signal; missing original clock is unknown. Stamp this causal native/shared label before dispatch. Keep `universe_complete=false` until producer/drop and eligibility coverage is demonstrated. Reports separate native, shared, warm/cold, authority/refusal/failure/unknown cohorts and expose every censor reason. They identify this witness as **confirmation-observation**, an upper bound on execution, not processed inclusion time.

## Acceptance and verification

1. Red-first full-path regression reproduces the three preparation reads in the RRR V4 winner. After off-path preparation, entry omits those reads while still making fresh actual-clip buy and reverse calls and retaining pool competition, sell simulation/preflight and exact approval ordering.
2. Expiry, descriptor/chain/mint changes, wrong key/hash, pending/uninitialized, buy-only, provider failure, partial response, job stop/deadline and malformed candidate tests retain unavailable/cold behavior. No native PASS enters the shared safety path.
3. Original-clock and receipt regressions cover reasks, repeated/late receipts, queue loss, cross-boot clocks, wrong chain/hash/block/status, booking mismatch and exclusion of approvals/exits. Existing confirmation request counts and cadence do not increase.
4. Measure local consumer overhead p50/p90 and paired complete quote/preparation call counts. A bounded read-only live preparation probe may verify current positive proof reuse, with no signing or funded replay; all requests use existing limits.
5. Independent review, focused tests, project validation with two workers/shared full-suite lock, and normal exact-release deployment gates pass. No database backup or validation override. Preserve concurrent changes and both active BSC flags.
6. Verify pinned runner/web and child ownership, effective RH flags, cache completion/freshness, source receipt/observer health, policy/funds/book and rollback. Compare organic RH post-cutover traces with the seven-day diagnostic baseline (17 organic filled entries with paired clocks, 2,000.198/3,205.804 ms p50/p90; success-conditioned and mixed-route/release, not a failure-inclusive SLA), clearly separated by route/release and clock type.
7. Actual signal-to-success p50/p90 require verified organic witnesses. Zero samples mean unknown. Improvement in discovery calls proves a preparation improvement, not a funded fill-speed improvement. Do not infer actual inclusion time from whole-second headers or +blocks.

## Edge cases and exclusions

Money path: yes. No new entry I/O, lock, retry or waiting for preparation. Measure added memory work; do not estimate it. Background ownership is bounded and thread-local HTTP/deadline state stays invocation-local; no module/env/resolver capture-restore or shared connection.

Absent, zero and unknown remain distinct for prices, amounts, supply, fee, nonce, receipt status, clocks and counters. Handle timeout, rejection, 429/ban, partial/wrong responses and late completion without cached PASS. Identity includes chain 4663, mint, registered descriptor, pool hash/currencies/hooks, actual atomic amount, taker/spender, original boot and signing generation. Store exact quantities without SQLite integer overflow. Readiness and successful checks are not fills; denominators retain failures and unknowns.

Rollback disables RH preparation/measurement flags and restores the previous complete pinned release. Durable signed/submitted claims and unknown confirmation retain existing reconciliation; do not release or replay them. Half-applied additive measurement fields remain optional/unknown, never funded permission.

Not handled: historical missing clocks; first-ever cold mints; a permanently cached mutable active-pool mapping; unsupported/disabled routes; wider permissions or max approvals; speculative native-funding conversion without known spare-float/gas and unchanged preflight; paid/colocated infrastructure; simultaneous provider loss; arbitrary cross-signal safety PASS; universal sub-200 fills. Both current receipt paths poll at 250 ms after a null response. A >200 ms confirmation observation does not prove execution itself exceeded 200 ms. RRR's 251 ms approval alone precludes proving that target on its unchanged required-approval path.

## Organic verification correction, 2026-10-12

The first measured organic RH entry, RUNNER position 73544, used the ordinary watcher/shared
multihop path after two native bridge-state refusals. Its earliest authenticated sequencer
receipt preceded the watcher receipt by 24.340232ms. Every dispatch/reply/confirmation interval
must use that same earliest valid original same-boot clock. Retain raw monotonic endpoints until
after source deduplication; missing endpoints or a conflicting boot make the affected latency
unknown. A malformed clock cannot crash the read-only report. Existing BSC sparse metrics retain
their historical contract. No measurement repair changes a funded gate or sends a request.

The initial rollout recorded a discovery job occupying the worker for at least 240 seconds before
its 60-second admission deadline rejected publication. The exact historical stall is unresolved;
one reproducible bypass is the research rotation's inner dRPC shared-account pacer, which waits
without the job's admission callback. For speculative discovery only, propagate invocation-owned
admission through rotation, chain-identity reads and quota retries. Use the existing nonblocking
account grant; paused, reserved, contended or unreadable account state skips the job. Recheck
deadline, generation and live/stop pressure outside account/health locks before physical reads.
Local account failures are RequestNotSent, never provider penalties. Ordinary native/research
readers keep their existing pacing and error handling; actual transport errors still count.

This closes account waiting, not a hard wall-time bound on DNS, an already-running HTTP response
or local ledger I/O. A cache hit without a fresh job-owned publication is not a new causal proof;
do not force a refresh or claim preparation completed to obtain a positive counter.

RUNNER's shared multihop route uses separate metadata/path discovery. Warming registered
single-hop V4 discovery does not prepare those inputs. Its measured 979.656 ms quote (including 754.199 ms
in five metadata calls), 197.741 ms market read and stale native bridge state remain outside the
proved single-hop preparation saving. The generic watcher trace has no causal preparation label,
so classify it as unknown. Actual RH fill-speed improvement remains unproved; do not compare its
different route and earliest monotonic clock directly with RRR's historical UTC diagnostic.

## Multihop metadata correction, 2026-10-12

The operator requested implementation of the 754.199 ms metadata fix. RUNNER made five serial
GeckoTerminal/DexScreener reads through urllib despite already owning a pooled funded read client.
Its direct bridge search and full-provider fallback repeated the same successful DexScreener read.

Robinhood buys alone may use the already-owned client for these metadata GETs. Preserve the
existing provider guards, browser user agents, status/error and Retry-After handling, physical-request
allowance and discovery deadline. Redirect hops consume that allowance; no retry is added.
Injected transports, other chains, research callers and exits retain their current seams.
Owned metadata uses the pooled client's ordinary `Accept: */*` representation, with strict JSON
decoding and unchanged source/pool validation. GT's responses vary by Accept; warmed same-client
controls retain the full ordered depth/pool result. An expired CDN response still requires an
upstream read and can remain slow. This header choice grants no local depth TTL or cached PASS.

Reuse successful identical DexScreener ranked results within the same decision only, including
valid empty results. Provider failures, deadline cuts and cancelled reads are not reusable.
Chain/token/pair/provider identity and decision ownership bind reuse. No cross-signal depth cache,
store-first activation, new quota, entry wait or route ordering change is authorized by this fix.
Keep provider candidate union/depth/fees, fresh fixed-block actual-clip buy/reverse quotes and every
funded gate. Rollback restores the previous pinned code; the existing dashboard hold stays intact.

Red-first tests must prove transport ownership reaches the executor's quote wrapper, retained
provider failures/cooldowns, duplicate elision, concurrent isolation and unchanged route selection.
Measure p50/p90 locally and socket/request counts. Controlled transport results do not establish
live provider response time or signal-to-fill speed. Organic successful receipt witnesses remain
necessary; no funded replay is permitted for verification.

## Pre-release mixed-route correction, 2026-10-11

A real TapeState regression with authenticated Pons receipt proof and a known native V4 pool reproduced deliberate `pons_shared` execution with zero discovery candidates. Native-route presence cannot exclude discovery candidacy: the bounded list now keeps the existing touched-first/busiest order across every active tape mint, capped by unchanged top-K/32. It adds no tape scan/route lookup, state, lock, provider budget or entry work. Native authority and safety candidates are unchanged. Pure-native candidates can occupy this bounded preparation budget; cold/crowded/evicted candidates retain existing checks and are reported as coverage limits. No per-source route tracker or unlimited warming is added.

### 2026-10-15 amendment: approved-amount safety evidence

The optional shared safety wrapper is a bounded exception to the earlier blanket shared-route prohibition: only fresh passing non-curve v4_shared/split_shared verdicts may answer nominal USD questions from one quarter through the verdict's own clip, with only usd/deadline/timings keywords. Exact inputs and unsupported route kinds retain funded checks. Each leg worker binds its amount and keeps private result/timing storage. Only evidence for the final reservation-approved amount may authorize mutation. A changed amount runs the existing wrapper again in a bounded worker under the original deadline; no remaining budget refuses without another callback. Completion time, rather than a later gate arrival, proves the result met that deadline. Matching final-amount early safety remains reusable. No activation, sizing, quote floor or override policy changes.
