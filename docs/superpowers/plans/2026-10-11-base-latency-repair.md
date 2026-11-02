# Base latency repair

Approved scope: deploy the four repairs identified from BASEDOG #73395. Existing safety, sizing, signed floors, retries and duplicate protection remain authoritative.

1. Enable Base background V4 token preapproval with the shared nonce floor, retaining receipt-first Base swaps. Include Base maintenance nonce allocation in that same floor; LI.FI remains chain-4663 only. Test stale node counts, ambiguous/rejected approvals, concurrency and non-Base controls.
2. For Base entries that actually unwrap WETH, refresh an aged winning quote after native visibility, then rerun price/copy/simulation gates before signing. Never rerun the whole ladder. Unknown/mismatched refresh retains existing fallback policy. Native float eliminates this boundary when sufficient; failed reads retain unwrap. Activate existing balance-bound Base float maintenance with 0.025 ETH low and 0.030 target, retaining 0.005 WETH and 0.10 ETH daily cap. This splits existing liquid funds; it adds no funding or clip.
3. Separate Base observation RPC from execution RPC. Reproduce public endpoint 429, verify replacement chain ID and real Swap-log request. Preserve existing bounded catch-up/deep-gap recording and backoff. No recovery calls on entry/exit threads.
4. Label detection-to-submit accurately. Separate waited approval from quote timing. Measure receipt-proceeds/settlement stage so residual time is visible.

Edge cases: stale pending nonce; concurrent entry/exit/preapproval/maintenance; ambiguous send retains identity; unknown balances keep unwrap; refresh cannot change spender or input; expired wallet override uses fresh timestamp; refresh above copy ceiling refuses before swap; confirmed unwrap cleanup follows existing policy; RPC failures never advance cursor; old replay cannot masquerade as live ticks. Does not guarantee fill/exit timing or reconstruct missing executable peaks.

Rollback: prior pinned release plus restore exact pre-deploy env; no DB rollback or backup. Pending reservations and reconcile/frozen contract gates remain required. No funded replay.

Validation: real executor regressions RED then GREEN; affected suites with two workers; final independent review; pinned deployment gate; runtime process/manifest/heartbeat/effective flags, Base tape frontier and maintenance/preapproval health. Organic gains require organic traces.

Ruling: the held-Base tape also watched the router instead of the actual V4 pool. Use receipt executed_pool first, route pool next, legacy route target last; split mixed V2/V3 and V4 scans, with exact V4 pool IDs in topics[1]. Both requests must succeed before inserting or advancing cursor. Historical direct probe: correct Base manager returned 71 BASEDOG swaps versus no router swaps. This adds at most one observation-worker request; no money-thread I/O.

Review correction: maintenance reads the pending nonce without mutation within its existing timeout, then claims the shared Base floor synchronously after both nonce/gas reads succeed. Signing/journal failures and explicit rejection CAS-release only the latest claim; ambiguity and a later allocation preserve it. Both callbacks are absent on other chains. Tests cover real executor, production context, short/long passes, unwrap/reclaim, late reads and non-Base controls.
