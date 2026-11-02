# Robinhood Entry Speed Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Preserve non-overlapping file ownership and red-first behavioral verification.

**Goal:** Remove proven RH discovery work from warm entry execution, preserve every funded check, and measure original signal receipt to canonically verified successful execution observation.

**Architecture:** Existing bounded RH preparation warms the current V4 discovery cache, including tape-observed shared-route candidates. Existing confirmation reads emit invocation-local timestamps through the bounded off-path publisher; canonical booking validates the witness. Cold routes and mutable funded gates retain their current behavior.

**Tech Stack:** Python, existing threaded RH lane/research pacer, JSON-RPC, SQLite timing/ledger stores, pytest with two workers.

**Spec:** `docs/superpowers/specs/2026-10-11-rh-entry-speed-design.md`.

## Global Constraints

- No new entry RPC, lock, retry or waiting for prewarming; no increased provider quota/pacing or funded research host.
- Exact approvals, authority, sizes, route permissions, signed floors and all mutable safety gates stay intact.
- Retain 600/30/3-second existing discovery lifetime rules, candidate cap32 and existing top-K.
- One speculative discovery job at a time; provider/stop/deadline errors cannot publish authority.
- No additional receipt polling; 250 ms confirmation cadence remains, observation is an upper bound.
- Unknown original clocks/eligibility/coverage never prove the speed goal.
- Tests use `-n 2`; only one full suite under `/home/bigpapi/.cache/codex-pytest-full.lock`.
- Normal merged-SHA pinned deployment, reconciliation/rollback and `--defer-backup`; no funded test order.

## Review Focus

- Graduated Pons sources must retain full venue discovery; do not force the source curve.
- Failed/partial/one-direction or stale derivation must not look like completed preparation.
- Shared-only mints must receive discovery work without native PASS admission or starvation of native/live work.
- Clock/receipt transport must preserve first timestamps across retries and exclude approval/exit receipts.
- Chain-specific publisher health/losses and full refused/unknown denominator must prevent false speed success.

## Task 1: Discovery preparation and proof reuse

**Ownership:** Performance worker owns V4 discovery/preparation and RH candidate/background wiring. The root owns integration edits to shared RH lane transport after this worker finishes.

**Files:** `sniper/execution/evm_v4_quote.py`, `sniper/execution/evm_v4_launchpad.py` only if its proof boundary needs a helper, `sniper/services/memecoin_rh_fast_decide.py` candidate generation, `sniper/services/memecoin_rh_fast_lane.py` preparation worker, `sniper/settings/memecoin.py`, and new `tests/test_rh_presignal_discovery.py` plus existing launchpad/RH safety tests.

**Interface:** Reuse `_quote_pools_for_mint(chain, mint, rpc_url=..., http=...)` and existing discovery cache publication. The candidate datagram adds a bounded `discovery_mints` list, distinct from native safety entries. New `MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED` defaults off; startup reads it once. Preparation status reports attempted/completed/unavailable/deadline counts, queue size and original completion age and a cheap in-memory causal readiness helper bound to proof completion/TTL/descriptor/chain/mint and same cached list at original ReceiptClock; generation fences job publication only. Late completion is cold; missing clock is unknown. The optional background adapter covers all discovery HTTP; local cancellation propagates and final publication rechecks stop/deadline/generation. Default entry behavior stays unchanged. Caller names may follow an existing seam discovered during implementation; document the final names in the audit.

- [x] Write a real discovery/quote regression with injected HTTP and registered launchpad struct/probe responses. Cold initial quote must show info+probe-buy+probe-sell, then fresh actual-clip buy/reverse. Prewarm the same pipeline and require only the fresh clip calls at consumption. Assert exact winning pool and complete pool-union/ranking, not mock invocation alone.
- [x] Run `python3 -m pytest -n 2 tests/test_rh_presignal_discovery.py tests/test_memecoin_v4_launchpad_discovery.py -q`; verify the new warm/candidate behavior fails on baseline.
- [x] Add bounded discovery candidate collection and one paced off-path preparation job. Keep existing cache TTL/proof checks. Consumer never joins/locks/waits; no permanent mutable derivation cache or entry single-flight.
- [x] Test wrong chain/mint/hash, pending and buy-only probes, malformed/partial/failed responses, expiry, changed descriptor, oversized list, generation/stop/deadline, concurrency and native priority, bounded one-discovery-after-two-native fairness, sustained native overload, and urgent/live cancellation. Confirm cold behavior and shared live safety remain.
- [x] Wire only proven existing immutable fact/slot seams if they add no synchronous read/wait and preserve exact amount/identity; otherwise record their unaddressed cost rather than speculate.
- [x] Run focused discovery/RH safety/round-trip/executor regressions and paired call-count/local p50/p90 benchmark. Commit one coherent performance change through the root after review.

## Task 2: Original clocks and receipt witnesses

**Ownership:** Measurement worker owns source clocks, publisher/receipt validation/report, shared confirmation metadata and runner wiring. Do not edit `memecoin_rh_fast_lane.py`, `memecoin_rh_fast_decide.py` or `sniper/settings/memecoin.py` concurrently with Task1; provide tested additive integration patches for the root after performance work.

**Files:** `sniper/services/memecoin_rh_feed_shadow.py`, `scripts/research/onchain_evm_wallet_watch.py`, `sniper/services/memecoin_bsc_execution_observation.py` (reuse engine without unrelated rename), receipt types and `sniper/execution/evm_real_seams.py`, `sniper/services/memecoin_real_runner.py`, `scripts/research/bsc_entry_speed_report.py` shared cohort seams plus a thin `scripts/research/rh_entry_speed_report.py`, focused clock/receipt/report regressions.

**Interface:** Existing `ReceiptClock` travels under `receipt_clock`; no reset at reask. `ReceiptState` gets optional first-success monotonic/boot metadata. Publisher observation/booked events include explicit `chain_id`; RH confirmation-observation is distinct from BSC independent observation. New `MEMECOIN_RH_EXECUTION_OBSERVER_ENABLED` defaults off. Publisher readiness is surfaced in current-generation health. RH executor-context callback readiness remains an open reporting requirement; no final-cohort funded dispatch validates it live.

- [x] Write red regressions for frame-before-decode clock, reask preservation, watcher4663 clock, shared/native exact receipt observation and full refused/unknown RH denominator.
- [x] Add optional local receipt timestamps at existing status-one reads and publish only from entry confirmation. Exclude approval/exit callbacks and preserve first success through retries.
- [x] Add explicit per-chain publisher namespace/health/validation. RH uses no head/receipt reader. Successful canonical transaction/block hash and actual real funded fill must agree before witness validation.
- [x] Test wrong identity/status/chain, moved booking, malformed/absent/cross-boot clocks, queue overwrite/drop, duplicate/later receipts, journal failures and unknown denominators. Assert confirmation RPC count/cadence unchanged.
- [x] Return integration patches for native lane payload/event/receipt and candidate source clock propagation. Root applies them after Task1, runs their already-red tests and benchmarks added memory append/clock work p50/p90.
- [x] Implement the read-only RH report. Separate observed UTC diagnostic intervals, original monotonic signal-to-confirmation witnesses and ACK/handoff/dispatch unknowns; do not reconstruct historical clocks.
- [x] Run focused observer/evaluator/receipt-booking/clock tests, then commit the measurement change through the root after review.

## Task 3: Integration, rollout and actual evidence

**Ownership:** Root integrates/reviews all changes and owns deployment; workers never run a full suite or change production.

- [x] Self-review spec and plan for unsupported assumptions, placeholders and accidental safety/price/authority changes; inspect every worker diff and independent review findings.
- [x] Persist exact RRR baseline, seven-day denominator and mixed-release limits in `docs/audits/2026-10-11-rh-entry-speed/`.
- [x] Run one full project pytest with `flock /home/bigpapi/.cache/codex-pytest-full.lock python3 -m pytest -n 2 ...`, and required schema/connection/index checks. Name any baseline failures. Keep funded services untouched during tests.
- [x] Complete all eleven Knowledge Sync questions in audit-only mode, update current RH docs, create/attach/merge normal PRs and fetch the merged full SHA. Initial product PR436 merged normally.
- [x] Capture exact running release/generation, authenticated health, effective flags/policy, known local funds and pending book. CAS only the two RH flags, preserving peers/BSC. Deploy `scripts/deploy_memecoin_runtime.sh <merged-sha> --restart --defer-backup`; no override or backup. Restore owned flags/runtime on failure through existing rollback. Initial exact release ea475213 passed 17,134 tests / 21 skips and activated normally.
- [ ] Verify exact new runner/web/child source and ownership, fresh RH preparation/observer health, preserved policy/funds and no unresolved reservation/reconciliation errors. Verify actual positive discovery preparation and unchanged exact mutable gate calls.
- [x] Run pinned `rh_entry_speed_report.py` read-only against the ledger/timing/RH source stores. Record cutoff, all observed eligible/refused/failure/unknown counts, clock/receipt coverage, preparation coverage, actual witness p50/p90 and route-specific legacy diagnostic comparison. No funded retry/replay.
- [x] If organic witness count is zero, record speed as unknown and retain active measurement. If witnesses exist, report the sample and censor limits; do not call a single fast fill a universal or causal cohort improvement. The one initial organic RH witness was 2,071.514214 ms confirmation observation; no improvement verdict.

## Task 4: Verification-discovered boundary repairs, 2026-10-12

- [x] Reproduce inner shared-account waiting and post-grant cancellation through the real rotation/discovery pipeline. Propagate speculative admission only; reuse nonblocking grants and preserve ordinary readers/provider penalties.
- [x] Reproduce duplicate source-clock rebasing and malformed/cross-boot repeated same-ID events through the real read-only cohort reader; every RH latency must use the final original anchor or remain unknown.
- [x] Repair the audit verifier for the actual pinned standalone RH source owner, interpreter and owner-specific relative state/ledger paths; wrong code/state/chain/executor must fail.
- [x] Independent review, focused two-worker checks and normal exact-release deployment gates pass; normal PR merge/deploy includes the active BSC fallback repair. Live PR438 merge `527ea1cfa` passed 2,452 mandatory pinned checks; the predecessor's ordinary-call compatibility refusal was fixed without fixture or gate changes.
- [x] Persist final runtime/report cutoff, positive preparation evidence or its absence, and actual route-specific successful observations. Do not force a funded replay, relax a guard, increase provider capacity or infer a warm label from native PASS/cache presence. Through 09:42:58 UTC: two `not_approved` refusals, zero fills/witnesses, zero admitted preparation completions.

Live acceptance remains incomplete: current positive discovery proof, actual organic fill-speed
improvement and complete pre/post configuration equivalence are unproved. The whole-configuration
digest changed; no old full configuration was retained to identify the field. Current pinned
ownership, publishers, flags, roster, local balances and reconciliation are verified separately.
Multihop metadata/path discovery retains fresh provider calls; neither a single-hop cache nor
a remembered winner provides the same ordered candidate union or an approved metadata-age rule.

Closure cutoff 10:33:47 UTC: three received RH origins/four delivery attempts, all refused,
zero fills/witnesses and zero admitted preparation completions. A dashboard entry hold was
tripped at 10:26 UTC and retained. Runner/web stay pinned; exit workers are healthy/alive.
Actual speed acceptance remains open, and the hold bounds future organic measurement.

## Pre-release mixed-route correction, 2026-10-11

A real TapeState regression with authenticated Pons receipt proof and a known native V4 pool reproduced deliberate `pons_shared` execution with zero discovery candidates. Native-route presence cannot exclude discovery candidacy: the bounded list now keeps the existing touched-first/busiest order across every active tape mint, capped by unchanged top-K/32. It adds no tape scan/route lookup, state, lock, provider budget or entry work. Native authority and safety candidates are unchanged. Pure-native candidates can occupy this bounded preparation budget; cold/crowded/evicted candidates retain existing checks and are reported as coverage limits. No per-source route tracker or unlimited warming is added.

## Multihop metadata follow-up, 2026-10-12

- [x] Reproduce the five serial metadata reads and exact repeated DS pair identity.
- [x] Reuse the owned pooled client for RH buys; preserve status, Retry-After, strict JSON errors,
  redirect accounting, request/deadline reserves, custom seams, other chains and exits.
- [x] Reuse successful DS results only within one decision; preserve complete candidate union/depth
  and quote actual clips forward/reverse at each decision's fresh fixed block.
- [x] Independent review and red-first focused tests; controlled p50/p90/socket/send calibration.
- [x] Paced metadata-only live controls: cold 596.247 ms, warm 111.451 ms; no fill-speed claim.
- [x] PR441 merge, pinned cutover and independent held-runtime verification at `e15c81ab1`;
  peer provider gate 1,834 tests and same-SHA normal restart 240 core tests are recorded separately.
- [x] Complete the original 907-file connected/core/changed scope: immutable `e15c81ab1`
  recorded one baseline fixture leak; test-only repair `ddfac3323c` passes 20,007 tests/21 skips
  with identical production bytes. Cancelled and same-SHA restart runs remain separate evidence.
- [ ] Organic successful receipt cohort proves signal-to-fill improvement. The dashboard hold
  was cleared externally at 15:53 UTC; the closing held-window check remains failed evidence.
  Cold upstream CDN expiry remains outside the proved saving.

Evidence: `docs/audits/2026-10-11-rh-entry-speed/multihop-metadata.md`.
