# J7 native-failure recovery — 2026-10-18

User authorization: make both saved J7 accounts useful when the native lane fails, preserve source proof and safety, deploy and verify the live bot. This amends the exact-hint implementation plan of 2026-10-11.

## Contract

- Amendment 2026-10-22, SHAV position #74109: a confirmed native failure with a named minimum-out/slippage reason is a final price refusal (`decided`), not mechanical `landed_fail`. Ordinary fallback and J7 backup must not re-buy the same source transaction at a fresh price. This intentionally skips a later recovered price; unnamed chain errors retain existing handling. No provider call, wait, or settings change is added.
- Exact Wallets/Pump transaction hints may pass Shyft's early hint gate only with an independently confirmed wallet signer/payment/acquisition receipt and a configured native mechanical failure. Strict exact hints use the receipt's original block time for age and cohort checks, never a later detection timestamp. Recheck native ownership and policy on retries. Preserve unrelated confirmed-onchain authority.
- Current Fomo relays carry `swap:<uuid>`. The UUID cannot identify or authorize a transaction. Persisted J7 buys from either socket may only wake a bounded scan of the current verified, armed wallet registry. Every discovered signature is independently decoded from its own confirmed receipt; never construct a UUID-to-transaction match from wallet, mint or time proximity.
- Scan discoveries retain canonical transaction identity and conservative block-time freshness. Newly discovered rows require the same strict native mechanical release as exact J7 hints. Existing rows are not upgraded or replayed. Preserve the J7 pre-arm backlog check after canonicalization. Ordinary approval, source proof, source age/price, mcap, reservation, sizing, quote, slippage and kill-switch checks remain authoritative.
- A carried `lane_down` label alone cannot prove a restart blackout for a history scan. The new strict path refuses it without an independently established native release; the existing confirmed lane's blackout behavior stays unchanged.
- Keep scans off the entry/stream paths, sequential and capped. Reuse the watcher's funded read client, rate-limit handling, decoder and writer. No new provider, stream, credential, table or trading setting. No historical funded retries or forced test trade.

## Execution and acceptance

1. RED/GREEN tests for Shyft admission, exact Wallets/Pump identity, native mechanical/policy/ownership states, retry provenance, unsigned refusal deduplication, independent scan proof, freshness, bounds and withdrawal.
2. Independent review, focused regressions with `-n 2`, schema validation, and a cached-path timing comparison. Record coverage limits.
3. Knowledge Sync report and operating-document amendment. Supabase memory writes remain deferred because the session permits memory updates only on direct request.
4. Integrate against current main, guarded flat-book pinned deployment with backup deferred, then verify runner/web/watcher release and effective flags, both saved sessions, current chain authority and unchanged safety settings.
5. Verify the deployed code with isolated read-only historical replay and executor sentinels. Observe organic events when available. Report replay, worker readiness, J7-triggered recovery and funded fills separately; no artificial production signals.

Rollback: previous pinned release. Keep the existing verified-backup flag as the feature rollback; no wallet approvals or risk settings change.
