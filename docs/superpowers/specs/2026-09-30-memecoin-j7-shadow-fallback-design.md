# J7 shadow with a J7 fallback — design

Status: SPEC ONLY. Money path (entry admission), operator-gated, default off. No code in this change.
Owner: Lambda. Order: Alpha 2026-09-30, after the D2 24 h read.

## Problem

With the J7 buy-shadow on (`MEMECOIN_J7_SHADOW_EVM_CHAINS` for EVM, `MEMECOIN_J7_SOLANA_MODE=shadow`
for Solana), a J7 buy by a wallet the on-chain watcher covers only records its decision
(`delivery_lane='j7_shadow'`, `memecoin_real_runner.j7_shadow_event`). The watcher lane owns the
entry. When the watcher's socket silently stops delivering, nothing acts until D2
(`J7CrossCheck`) notices, forces a reconnect and backfills.

The 4663 D2 24 h read (09-29 06:37Z to 09-30 06:37Z):
- 3 of 21 watched J7 buys were not socket-delivered.
- D2 recovered each at chain +11.6 s, +23.6 s and +22.2 s, while J7 had them within 2.4 s.

The shadow stays reverted on 4663 for that reason.

Measured: watcher detection time minus J7 receipt time, same (chain, tx), 09-13 to 09-30:

| chain | n | p50 | p90 | p95 | p99 | watcher first | within 1 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| 4663 | 97 | −0.31 s | −0.19 s | −0.08 s | +21.1 s | 94.8% | 95.9% |
| 56 | 31 | −1.21 s | −0.68 s | −0.48 s | +3.2 s | 96.8% | 96.8% |
| 8453 | 8 | −0.43 s | −0.40 s | −0.16 s | −0.16 s | 100% | 100% |
| solana | 821 | +0.13 s | +0.30 s | +0.42 s | +0.87 s | 32.5% | 99.0% |

The sample is the J7 buys the watcher detected at some point. A J7 buy the watcher never detected is
not in it, and the fallback covers exactly those.

## Proposal

A covered J7 buy is still recorded as a shadow decision at once. The runner then waits up to
`N` seconds after the J7 receipt for the watcher's detection of the same transaction.
- If the detection arrives, the watcher lane owns it, exactly as today.
- If it does not, the J7 event runs through the live hook with `delivery_lane='j7_fallback'`.

`N = 1.0 s` on every chain (knob `MEMECOIN_J7_SHADOW_FALLBACK_S`). That covers 95.9-100% of the
detections the watcher makes at all. A buy the socket missed is then decided at J7 + 1 s, instead of
chain + 12-24 s or never.

The wait is event-driven, never a sleep in the decision thread. The shadow decision schedules one
check on the existing retry machinery (`db_memecoin.enqueue_entry_retry` into
`memecoin_entry_retry_queue`, the queue fresh-pool and on-chain retries already use).
The check looks the transaction up in `onchain_wallet_detections` with the range seek from
7efe6914b, and in the on-chain and slot attempts. No RPC is added.

Knobs:
- `MEMECOIN_J7_SHADOW_FALLBACK_CHAINS`, default empty: no fallback, today's behaviour.
- `MEMECOIN_J7_SHADOW_FALLBACK_S`, default 1.0.
- The fallback applies only to chains that are also shadowed.

## Exactly once, across both lanes

The existing guards already make a double open impossible:
- A reservation is unique per coin while reserved (`idx_mc_pending_coin_uniq_v2`, `UNIQUE(network,
  mint) WHERE status='reserved'`).
- An open position is unique per (source, network, mint, strategy, mode) (`idx_mc_pos_open_uniq_v3`).

On top of those:
- **Watcher first, fallback second.** The fallback check skips when an `onchain:` or `slotlane:`
  attempt (or a detection) of the same transaction exists. It records `status='skipped',
  reason='watcher_delivered'`.
- **Fallback first, watcher second.** `memecoin_onchain_lane.already_attempted_from_another_lane`
  already treats any non-shadow J7 attempt of the same transaction as the feed's, and returns
  `duplicate_of_feed`. `j7_fallback` is not `j7_shadow`, so it counts as the feed's attempt with no
  change.
- **Both at the same instant.** The second reservation hits the coin UNIQUE index and is refused.
  The loser's attempt records the refusal. At most one reservation, and so at most one fill.

## Rule 5

1. **Money path: YES.** A fallback opens funded positions. Default off; per-chain activation by
   the operator after a shadow read. The fallback computes its decision without submitting, and
   records `j7_fallback_would_enter` for N days first.
2. **Speed on the fill path.** A fallback entry is J7 + N + the ordinary hook, so N is paid only on
   the ~4% of covered buys the watcher misses. Nothing is added to the watcher lane or to uncovered
   J7 buys. The check itself is an indexed lookup; its p50 and p90 must be measured in the runner
   before activation.
3. **Concurrency.** The check runs on the retry machinery's thread, with the ordinary hook locks. No
   new module state. The entry-knob applier's `entry_decision()` lock covers it, as it covers the
   first decision.
4. **Absent vs unknown.**
   - No detection at N: the fallback acts.
   - An unreadable lookup: the fallback acts too, because the J7 event is live evidence and today's
     shadow-off behaviour is to act on it.
   - The J7 event's age is checked again at fallback time with the ordinary follow age gate.
5. **Dependency failures.**
   - Watcher down: `solana_watcher_healthy()` / `_j7_evm_covered` already make the buy uncovered,
     so J7 acts at once. The fallback is for a watcher that looks healthy but is not delivering.
   - Retry queue full: act at once (the shadow-off behaviour), logged.
6. **Units.**
   - N is seconds on the host monotonic clock between the J7 receipt and the check. The J7
     receipt is the corrected-clock `received_at`; do not mix it with detection host stamps for the
     wait.
   - Identity: `(network_id, tx)`. EVM tx lowercase; Solana signature exact.
7. **Sample.** N comes from covered wallets' matched pairs over 09-13..09-30. The relayed-owner
   wallets (sunnydog06, 0xdetweiler) sit mostly in the "never detected" tail. For them the fallback
   fires every time, which is right.
8. **Rollback.** Empty `MEMECOIN_J7_SHADOW_FALLBACK_CHAINS`. A fallback already queued runs its
   check and records it, with no entry.

## D2 when J7 has nothing to check against

The 8453 socket was dark 613 s (03:28:41-03:38:55Z on 09-30) with 0 J7 events. D2 had no reference,
so it had no verdict. The existing silent-socket detector ("socket silent Ns with no message at all")
caught that window, because heads stopped too.

What stays invisible today is a socket whose heads flow while its transfer filters deliver nothing,
at a time when no watched wallet trades on J7. That is the 09-29 05:30Z failure shape on a quiet
chain.

Proposal (measure first):
- A canary filter per EVM chain on a busy token's Transfer logs (WETH, or the chain's USDC),
  subscribed like the transfer filter.
- If the canary is silent for longer than its measured p99 gap while heads flow, force one
  reconnect, at most once per D2's `min_gap_s`.

Step 1 is to measure each chain's canary log-gap distribution over 24 h on the research host. If
the p99 gap is short (seconds), the canary is a better liveness reference than J7 on quiet chains.
If it is long, it isn't, and the chain keeps J7 plus the silent-socket detector.

## Checks the build leaves behind

- Covered buy, detection within N: a shadow decision only; no fallback attempt.
- Covered buy, no detection by N: one `j7_fallback` attempt; a later detection of the same
  transaction gives `duplicate_of_feed`.
- A detection and the fallback in the same instant: exactly one reservation (the UNIQUE index); the
  other attempt is refused.
- An unreadable lookup: the fallback acts.
- Knob empty: behaviour identical to today (a guard test on the shadow path).
- A guard test that the fallback adds no RPC call and no sleep to the decision thread.
