# BSC entries: a standing WBNB allowance instead of a per-entry approve (design, 2026-10-07)

Operator YES via Alpha ("just improve the bsc buy"). Author Beta. Branch `beta/bsc-standing-allowance`.

## Today

A chain-56 entry pays WBNB. Every router quote flags `issues.allowance` with its spender and reads
nothing. `approve_entry` then sends `approve(spender, sell_atomic)` through `_approve`, pipelined:
the swap signs at the approval's nonce + 1. The `evm.approval` stage on 56 since 10-01 is n=37,
all `broadcast`, at p50 186 ms, p90 505 ms and max 589 ms. HUSH (2c0ab7cf8714) is the max.

These spenders were actually pulled through on 56 fills since 09-25 (n=62; spender = the swap's
`to` = the quote's `issues.allowance.spender` for these rungs):

| spender | address | fills | rung |
|---|---|---|---|
| PancakeSwap V2 Router | 0x10ed43c718714eb63d5aa57b78b54704e256024e | 33 | v2 |
| PancakeSwap Smart Router | 0x13f4ea83d0bd40e75c8222255bc855a974568dd4 | 13 | mixed_v3_v2 |
| PancakeSwap V3 SwapRouter | 0x1b81d678ffb9c0263b24a97847620c99d213eb14 | 8 | v3, v3_bridge |
| KyberSwap router | 0x6131b5fae19ea4f9d964eac0408e4408b66337b5 | 4 | kyber |
| flap portal | 0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0 | 2 | flap_curve (native value, no approve) |

On 56, Permit2 and the Relay approval proxy were pulled through 0 times. The standing set is the
first four, pinned in code. Any other spender keeps today's approve.

## Design

Knobs: `MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS` (default empty; 56 at deploy) and
`MEMECOIN_EVM_STANDING_ALLOWANCE_CAP_USD` (500).

- **The cache.** For each (chain, wallet, WBNB, spender) the cache holds V, our own count of the
  allowance (None = unknown), under one module-level lock. It moves only when we approve or spend.
  It is never read from chain on the entry path, and never read at all: a start-up top-up sets it.
- **The entry** (`approve_entry`). The pinned spender and WBNB, with V known and V >= amount:
  V -= amount under the lock and return `{"standing": True}`. The executor treats that like a
  pipelined approval (no post-approval requote) and traces `evm.approval outcome=standing`.
  Otherwise today's exact approve runs. That approve OVERWRITES the allowance with the exact
  amount, so V := 0 and any in-flight top-up is marked tainted.
- **Top-up thread** (daemon, one per executor). It wakes at start-up (V unknown), when V falls
  under 2 x the last clip, and on invalidation, with at most one top-up in flight. It sends
  `approve(spender, cap)` through the same `_approve`, so it shares the nonce floor every signer
  uses. It takes this wallet's execution lock (`evm_exec_lock_key`) non-blocking and skips while
  any executor holds it (entries, exits and the lanes hold it from quote to confirm). It holds
  the lock only through sign and send, never the receipt. cap = floor(500 / WBNB USD x 10^18), with WBNB USD the last entry's `sell_token_usd`. With
  no price there is no top-up.
  - Confirmed: V := cap - P, where P is the sum of reservations outstanding when the top-up took
    its nonce plus every reservation since. Those swaps sign later, at higher nonces, so they mine
    after the approve. Subtracting an earlier one twice only errs low.
  - Tainted: V := 0, and another top-up follows.
  - Rejected or failed: V unchanged. It keeps decrementing and entries fall back.
  - Ambiguous: the pre-approval rule (`_preapprove_position`). Rewind the nonce only when no later
    allocation exists; otherwise re-offer the identical bytes until they land. V does not change
    until the top-up confirms.
- **A standing entry that does not fill** (aborted after the reservation, reverted, or unknown):
  V := None, so entries fall back until the next top-up confirms. A reservation is never returned.
- **Every rule errs low.** An under-count only falls back to today's exact approve.
- **Audit.** One structured log line per top-up: chain, spender, amount_atomic, nonce, tx_hash,
  send_status, receipt_status. `memecoin_evm_preapprovals.position_id` is NOT NULL, so that table
  does not fit. A table can come later if the log is not enough.

## Edge map

1. **Money path: yes.** This is the entry. A cache that over-counts makes the swap revert on
   `transferFrom`: the existing mined-reverted path releases the reservation, at the cost of the
   gas and a missed entry. Exposure: a pinned router could pull up to $500 of WBNB, against one
   clip today. MAX is never used.
2. **Fill-path speed.** The approve's sign and send leave the entry (p50 186 / p90 505 ms). The
   entry gains one dict lookup under a lock. The swap's pending-nonce read moves from the approve to
   the sign, so the RPC count is the same. Residual: an entry signed just after a top-up waits
   behind it at nonce + 1, which is balance-neutral, like today's pipelined approve. p50 and p90 are
   measured after deploy, not estimated here.
3. **Concurrency.** One cache lock is shared by every executor in the process. Parallel entries
   reserve under it and never spend the same allowance twice. The top-up shares the executor's nonce
   floor. A signer in another process on the same wallet is invisible: its spend surfaces as a
   revert, then V := None.
4. **Absent vs zero.** V None means unknown and falls back; V 0 is known empty. No price means no
   top-up.
5. **Dependency failures.** A top-up that times out, is rejected or is ambiguous follows the rules
   above. RPC down means no top-up, and entries fall back.
6. **Units.** WBNB has 18 decimals, and the clip is the entry's own `sell_atomic`.
7. **Sample.** Measured after deploy: the share of 56 entries with `outcome=standing`, the approval
   stage's ms, and any allowance revert (Gamma).
8. **Rollback.** Knob off restores today's path at once; the allowance stays on chain, unused.
   Revoke is `scripts/memecoin_evm_revoke_standing_allowance.py`: approve(spender, 0) for each
   pinned spender, dry-run by default, `--confirm` run by the operator.

The checks left behind: a reservation larger than V falls back; two parallel entries cannot both
spend the last clip; an exact approve while a top-up is in flight leaves V at 0; an entry that does
not fill makes V unknown.

## Amendment — durable standing approvals and manual holdings, 2026-10-07

The original sign/send lock did not protect another signer after an unknown approval result or a
runner restart. Standing top-ups now check unresolved manual holding approvals/swaps under the
existing wallet lock through a short-lived read-only ledger connection. An unreadable ledger skips
signing and reaches the existing daemon error log; its private connection uses no busy wait.

Before broadcasting, a standing top-up writes its actual signed bytes through `intent_send` in
`standing_intent_lock_dir()` (`<execution lock directory>/standing_allowance`). This record is
required even when `MEMECOIN_EXEC_INTENT_STAMP` is off. Any outstanding or unreadable record for
the chain blocks another standing top-up, including another spender after restart. A record-write
failure prevents broadcast. No new database table or process-local recovery state is introduced.

The record survives rejection, send/receipt exceptions and unknown confirmation. A confirmed or
failed receipt clears it through `stamp_send`; otherwise the existing background sweep resolves it
only on its chain-proof rules, including a consumed nonce. Ordinary send completion does not clear
this separate record. The separate namespace does not become an ordinary fast-lane intent fence.

The wallet lock is still released before receipt polling. Entry nonce-floor and exact-approval
behavior are unchanged, and no entry-path network call is added. Manual holdings use this durable
standing namespace as a nonce fence before signing. Cache updates retain the original confirmed,
tainted, failed and ambiguous rules above.
