# BSC okt entry repair implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development and test-driven-development for each repair. Preserve the already approved BSC speed and safety contract.

**Goal:** Remove duplicated attempt-local safety and avoidable price-lookup overhead, and report actual receipt-to-submission and confirmation observation accurately.

**Architecture:** Reuse the existing exact early safety after the reservation selects the approved clip. Keep the ordinary fresh check for unavailable, refused, expired or mismatched evidence. An owned off-path worker prepares the existing public Binance REST price authority, with the original one-second freshness limit. Separate prepared and selected snapshots keep a selected sizing price stable within its original lifetime. The worker pauses 500 ms after each completed read (at most 120 public CEX reads/minute, approximately 82/minute at the measured 230 ms RTT), backs off failures, and adds no funded RPC reads.

**Tech stack:** Python, existing threaded executor and pooled HTTP transport, pytest with two workers, read-only SQLite incident evidence.

**Spec:** `docs/superpowers/specs/2026-10-11-bsc-sub200-entry-design.md`, especially the preserved shared-route live safety contract.

## Evidence and constraints

Pinned incident release: `47d4fefc4bd2df24a96e84ee54d78d2d07f498e8`.
Attempt: `4374042f509a436ebda2433194735bb1`; position: `73798`.

| Boundary/work | Recorded duration |
|---|---:|
| Original receipt → decision | 10.693 ms |
| Early sizing, fresh BNB/USD prerequisite | 276.200 ms |
| DexScreener pair selection, overlapped | 227.628 ms |
| Selected PCS V2 two-hop quote | 276.452 ms |
| Fresh token safety, overlapped with quote | 309.108 ms |
| Storage slot discovery inside safety | 177.9 ms |
| Original receipt → sender call | 659.3 ms |
| Original receipt → submission acknowledgment | 701.759 ms |
| Original receipt → confirmation observation | 1157.028 ms |

These are one incident's measurements, not p50/p90. The execution observer has no successful first-receipt witness for this transaction; its state is expired, validation pending. Confirmation observation does not prove the first fill was observed then.

The source block's integer timestamp is `17:40:26 UTC`; the original receipt is `17:40:27.680738 UTC`. Their coarse interval is 1.680738 s. The unverified J7 source timestamp is `17:40:27.584 UTC`; it is a different clock. Preserve both facts without calling either an exact execution stage.

Native refused the exact source buy at `17:40:26.838781 UTC`, about 842 ms before J7's receipt. Its post-state market cap was $262,855; shared admission used the source-average $239,370.839. Both thresholds were $250,000. This performance repair does not raise the cap, change its basis or alter admission authority. The native/feed refusal-consistency gap remains a separate finding.

## Edge cases and review focus

- Money path: preserve exact reservation size, safety proof, provider pacing, signing identity and durable submission claims.
- Speed: add no synchronous provider call, lock or retry. Preserve speculative work's original deadline; do not reset it at reservation.
- Threads: freeze the clip captured by each safety worker. Do not mutate module functions, environment, DNS or shared SQLite handles.
- Unknown: missing price/evidence stays unavailable; zero is not a valid USD mark. Missing clocks remain unknown.
- Dependencies: timeout, rejection, rate limit, malformed or partial answer must retain the existing refusal/fallback path.
- Identity/units: bind chain, mint, exact USD clip and attempt. Keep milliseconds separate from coarse block seconds and slot distances.
- Sample: include cold, refused, expired and successful attempts when claiming performance; no selected-warm-only fill headline.
- Rollback: normal immutable release validation and rollback. No database migration, backup, funded replay, policy activation or forced position close.
- Not handled: first-token metadata/slot discovery, network inclusion delay, upstream J7 delay, or a guaranteed universal ≤200 ms fill.

## Task 1: Match BSC early safety to the reserved clip

**Files:** `sniper/services/memecoin_evm_leg.py`; focused BSC early-safety tests.

- [x] Write a deterministic regression using a real `EarlyEvmQuote`: initial configured clip differs from the final reservation, while early safety matches the final clip. Assert one safety check and reuse of its exact PASS. Verify RED before editing production.
- [x] Cover final mismatch, failed or expired evidence, and reservation/worker scheduling on either side of final clip assignment.
- [x] Freeze each worker's clip. Defer BSC early-candidate selection until the approved clip is known; retain immediate ordinary parallel work when no candidate exists and the original leg deadline in both cases.
- [x] Run focused leg/early-quote/safety tests with `python3 -m pytest -n 2`; inspect the complete output and diff.

## Task 2: Remove avoidable price transport overhead

**Files:** `sniper/execution/evm_real_seams.py`, production context construction in `sniper/services/memecoin_evm_leg.py`, trace metadata in `sniper/services/memecoin_real_runner.py`, and focused price tests.

- [x] Verify current price HTTP already uses an owned pooled client. Six cold and six warm Binance requests all returned HTTP 200 and valid BNBUSDT prices: cold p50/p90 254.885/276.817 ms; warm 230.684/232.368 ms (nearest-rank p90). Transport pooling alone cannot remove the prerequisite.
- [x] Reject the WebSocket alternative on measured whole-sample coverage. Over 35 seconds, 153/320 warmed reads had evidence within one second (47.8%); the snapshot stream also had gaps up to three seconds. Do not ship the prototype.
- [x] Add RED tests for prepared price promotion, selected-price stability, original timestamp preservation, expiry/failure, single-flight, a blocked worker with a fresh nonblocking peek, and owned shutdown. Injected contexts must not start network work.
- [x] Reuse the existing native-price helper, memo lock, Future single-flight and submission-backend worker lifecycle. Start one bounded refresh worker only from normal production BSC construction; pause 500 ms after completion and back off failures up to 30 seconds.
- [x] Background reads refuse the FMP host in their instance-local transport, so outages cannot create new FMP quota usage. Live reads keep the existing REST→FMP fallback. Background reads update prepared evidence without replacing an unexpired selected sizing snapshot. Live/peek consumers promote only fresh prepared evidence and retain its original monotonic completion timestamp. A miss keeps the existing live lookup fallback and error behavior.
- [x] Record prepared/live source and original age in existing sizing traces. Run focused price/native-quote/transport tests and a bounded read-only preparation probe, including startup, misses and errors. Do not equate lookup gain with live fill gain.
- [x] Optional preparation thread startup failure must preserve funded runner construction, exits and recovery. Two per-instance `RuntimeError`/`OSError` regressions were RED; the guard stops the owned worker, records the error class and preserves ordinary live pricing. Independent review cleared the guard; 194 related focused tests passed.

## Task 3: Separate coarse source time from execution stages

**Files:** `sniper/services/memecoin_fill_timing.py`; focused fill-timing tests.

- [x] Reproduce the okt coarse 1.680738 s interval and exact receipt-stage boundaries in RED reporting tests. Cover absent/cross-boot clock evidence and unaffected Solana slot reporting.
- [x] Retain coarse EVM source-block-to-receipt evidence but exclude it from exact largest-stage ranking. Label receipt→dispatch, submission acknowledgment and confirmation observation using existing same-boot trace fields. Never fabricate a first-fill witness.
- [x] Run focused reporting/view/Telegram/resolver tests and inspect saved-stage compatibility.

## Release and live verification

- [x] Review the complete diff independently. One host-locked full run completed before the operator prohibited further full suites: 36,860 passed, ten failed; nine are independently reproduced baselines and one expected clock label was corrected. Final focused regressions pass. Use the explicit focused manifest plus mandatory funded-path, schema and integrity checks for the pinned release.
- [x] Commit logical repairs, create/attach the PR and merge normally. PR #443 merged as `7d63fcd962deeb96cfa6fee51a156c9e5301c196`; normal no-backup pinned deploy passed 888 focused tests and preserved reservation, reconciliation, drain and rollback gates.
- [x] Verify actual runner/dashboard paths and effective configuration, observer readiness and ordinary safety. Final release `773915c05111c1c2aa1f0e5ac6a1aa92fa0ce413` passed 964 exact-pin focused tests. Authenticated status and nine watcher paths match; policy/authority/foreign-work/original-incident hashes retained. Actual worker heartbeat reports alive and ready with no error. The invalid Python 3.13 OS-name probe was replaced with nonblocking producer status.
- [x] Publish actual signal-receipt→dispatch/ACK/confirmed-observation and first-fill witness status separately. Final cutover cohort after rowid 81022 has zero BSC attempts at 20:51:01 UTC. Live gain and universal ≤200 ms fills remain unproved; no funded retry.
