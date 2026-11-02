# ALT batching and EVM read overlap implementation plan

> **For agentic workers:** Use test-first implementation and independent review. Work only in this isolated branch.

**Goal:** Remove independent serial reads from cold Solana funding-leg preparation and eligible Base/4663 entry preparation.

**Architecture:** Batch known uncached lookup-table accounts through the existing read transport. Extend the existing BSC overlap patterns to Base/4663 entries without changing nonce ownership or gas freshness.

**Tech Stack:** Existing Python RPC seams, futures and pytest; no new dependencies.

**Spec:** The accepted recommendations in `logs/speed-audit-706f4245f/recommendations.md` in the parent task workspace, items 4 and 5. User authorized implementation after verification.

## Constraints and review focus

- Preserve safety, live route checks, exact approvals, clip and minimum-output checks, source proof, reservations and durable signed identities.
- Do not change admission, sizing, funded settings, provider quotas, wallet locks, exits, deployment or runtime state.
- Batch at most the RPC's supported bounded account count; retain key order, deduplication and cache hits. Null, short, malformed, wrong-owner or deactivated accounts must fail closed.
- Base/4663 balance overlap remains behind the existing chain opt-in and within the wallet lock; retries must obtain their required fresh balances.
- Gas read overlap is entry-only for the new chains. Recheck the current gas observation's one-second freshness after a slow nonce; retain failure and serial fallback behavior.
- Missing/zero/unknown and explicit rejection/transport ambiguity stay distinct. No network work under new shared locks.
- Pytest uses `-n 2`; task test commands share `/home/bigpapi/.cache/codex-fill-speed-focused.lock`. No live provider calls.
- No live p50/p90 or same-slot claim follows from fake provider timings.

## Task 1: Batch cold Solana lookup-table accounts

**Files:** `sniper/execution/pump_curve_buy.py`, `sniper/execution/raydium_launchlab_buy.py`, relevant funding-leg and warm ALT tests.

**Interface:** Preserve `prefetch_usdc_sol_leg(...)` and its returned leg/table shape. Update the warmer's read wrapper to validate every batched ALT account.

- [x] Add behavioral tests: two missing/one cached ALT, deduplicated fetch and original table order, second-use cache hit, empty/short/null/malformed response, foreign owner and deactivated ALT.
- [x] Run the new cases against baseline and record expected failures.
- [x] Use one bounded `getMultipleAccounts` for missing keys; parse validated accounts and preserve original output ordering. Avoid partial-cache publication after a failed batch.
- [x] Run nearest leg/warm/fixture tests and inspect every shared caller.

## Task 2: Overlap eligible Base/4663 entry reads

**Files:** `sniper/execution/evm_real_seams.py`, existing BSC early-balance/signing tests or narrow new parameterized cases.

**Interface:** Preserve executor seam signatures unless a defaulted entry-only flag is necessary. New-chain exits retain their current behavior.

- [x] Add event-controlled tests for first balance reads before the early-quote join on opted-in 8453/4663, plus opt-out, quote mismatch and fresh retry behavior.
- [x] Add event-controlled nonce/gas overlap tests for Base/4663 entry signing/approval; test failed reads, nonce handoff, thread fallback and one-second gas expiry after a slow nonce.
- [x] Confirm expected baseline failures, then reuse the existing independent-read implementation. Keep nonce claims on the caller and refresh expired gas through the existing memo owner.
- [x] Run nearest executor/approval/nonce tests and inspect entry, pre-transaction and exit callers.

## Task 3: Review and validate the combined patch

- [x] Run the combined focused suite once with two workers and inspect schema/source guards appropriate to changed references.
- [x] Review the whole diff independently; address actionable findings and rerun only affected checks.
- [x] Record deterministic request-count/ordering evidence and any bounded fake-latency measurements, clearly labeled offline.
- [x] Check branch diff/whitespace and leave implementation reviewable on its isolated branch. Do not merge, push or deploy as part of this implementation request.

## Shared-file check

Task 1 and Task 2 edit disjoint production/test files and share no new interface. Root owns this plan and final evidence; implementers must not commit or modify each other's work.

## Completion evidence

- Final combined gate: 1,104 passed in 46.48 s, two workers, 33 focused files.
- Independent review: approved after removing an unnecessary gas refresh on failed nonce.
- ALT helper with synthetic 5 ms RPC waits: 3 cold table reads become 1; p50 15.464 to 5.203 ms, p90 15.639 to 5.293 ms.
- Base/RH signing helpers with the same synthetic waits: p50 about 10.3 to 5.5 ms, unchanged two RPCs. No live latency or landing claim.
- Source/report/test evidence is retained in the parent task logs/implement-fill-speed directory.
- Kept on the isolated implementation branch; no merge, push, deployment, runtime action or funded configuration change.
