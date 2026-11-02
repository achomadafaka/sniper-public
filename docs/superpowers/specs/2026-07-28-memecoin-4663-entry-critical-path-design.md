# Memecoin 4663 entry critical-path remediation (2026-07-28)

Status: **approved for implementation and production activation on chain 4663**.

## 1. Goal

Reduce funded Robinhood Chain entry time before swap broadcast without weakening admission,
sizing, token safety, exact approvals, transaction identity, confirmation, or accounting.

## 2. Measured baseline

The shared `auto_free` transport is already the live EVM backend. Approval and swap transactions
are signed once and offered concurrently to custody, public RPC, and the Robinhood sequencer. The
remaining latency is before swap submission:

- 4663 total p50: about 10.6 seconds;
- token safety: about 2.4 seconds, already parallel with balance/quote preparation;
- exact approval receipt wait: about 2.8 seconds;
- post-approval requote: about 0.15 seconds;
- swap confirmation: about 2.3 seconds after broadcast.

Production submission telemetry shows the sequencer accepting the exact signed EVM bytes in about
0.22 seconds. The fast transport is not the fallback. The receipt-first approval policy prevents
the swap from reaching it sooner.

## 3. Decisions

1. Enable the existing nonce-ordered approval pipeline only for configured chains. Production
   activates chain `4663`; Base `8453` retains receipt-first approval.
2. Keep the legacy global flag as the master switch. A chain allowlist is mandatory when the master
   switch is enabled; an empty or malformed allowlist fails startup instead of enabling every EVM
   chain accidentally.
3. The approval must receive a positive, identity-matching provider acceptance before the swap is
   signed at `approval_nonce + 1`. Ambiguous or rejected approval delivery aborts before swap send.
4. Record approval provider attempts against the owning entry reservation. Approval delivery must
   be visible as `entry_approval`, not an unaudited process-global send.
5. Expose `approval_pipeline` on each EVM runtime context so health/startup evidence proves the
   effective chain policy.
6. Preserve live ETH/USD pricing. No price, balance, quote, or token-safety cache is added to the
   funded path.
7. Poll 4663 receipts at 250 ms for the first two seconds, then resume the existing two-second
   cadence. Preserve the roughly 120-second unknown-confirmation window and bounded RPC load.

## 4. Invariants

- Exact-amount approval remains exact; no max allowance.
- Token safety still completes and passes before approval signing or broadcast.
- Initial exact quote and post-peak confirmation remain authoritative.
- Approval and swap use distinct ordered nonces and byte-identical multi-provider fanout.
- Swap identity is persisted before its broadcast under the existing reservation contract.
- Confirmation and whole-wallet balance reconciliation remain authoritative for fill accounting.
- Ambiguous delivery never becomes a fabricated rejection or an automatic retry.
- V6 remains paper-only and funded admission policy is unchanged.

## 5. Expected effect and proof

The swap can be submitted after a positive approval acceptance instead of after approval mining and
requote. Based on the latest successful 4663 trace, this removes roughly 2.7 seconds from the
pre-broadcast path. This is an estimate until organic funded attempts produce post-deploy traces.

Deployment proof requires:

- pinned release and runner manifest match the merged main SHA;
- authenticated health reports `auto_free` and `approval_pipeline=true` for 4663 only;
- kill-switch state is preserved;
- provider telemetry for the next 4663 entry includes `entry_approval` and `entry` attempts;
- latency claims remain pending until measured organic attempts exist.
