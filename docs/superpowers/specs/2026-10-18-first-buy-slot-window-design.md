# First source buy and Solana entry quality

## Superseding operator decision, 2026-10-20: any execution slot after quality gates

Automatic funded Solana copies may execute in any slot once the existing signal-quality,
source-initiation, first-buy, freshness, price, safety, sizing and risk checks pass. Source slot
remains immutable identity and attribution evidence. It no longer supplies an execution deadline.
The Laura attempt `d08852e951a345159abb0bfc3ed6cc4d` passed entry checks, then its signed
Lighthouse upper assertion rejected landing at S+2 and spent 0.010005 SOL without buying tokens.

Keep `MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW=1`: canonical first-source claims, cohort identity,
known zero traded-account pre-balance and duplicate protection remain active. Source sells,
failed attempts and restarts do not reopen a first opportunity. Exact-ID later-buy permissions
retain their selection meaning; historical `max_slot_delta` values no longer limit execution.

Native and ordinary automatic producers pass no slot bounds to signing or transport, retaining
the existing cached-head callback and 500ms head-freshness quality check. Builders
measure their real packet size without hypothetical Lighthouse guards. Signing accepts absent
bounds; explicit malformed or partial legacy bounds still refuse. Local delivery, RPC and TPU
use their existing uncapped request path, including the RPC provider's configured rebroadcast
budget instead of the deadline-specific zero-retry override. Fixed-offset resends retain the same signed identity,
at most two sends, outcome stop and bounded busy-provider grace, without a dispatch-slot cutoff.
Blockhash validity, provider freshness, real 1232-byte packet limits and signed output minima
remain authoritative. No new client RPC, lock, wait, fee setting or retry loop is introduced.

Heartbeat reports no maximum source-slot delta or guaranteed landing deadline, with the 500ms
head-freshness bound still visible.
Already-signed transactions retain their original guards; historical receipt decoding and
settlement remain intact. Unknown outcomes stay reserved. This change cannot cancel those
transactions or ensure that a later landing satisfies its signed price limit.

Deploy the exact merged release with focused validation, reservation/reconciliation gates,
flat-book cutover and `--defer-backup`. Rollback restores the preceding pinned release; no schema
or configuration migration is required. Never re-sign or replay an old failed opportunity.

## Historical operator decision, 2026-10-19

Every automatic funded Solana copy may execute only in the original source slot S or S+1.
The native and ordinary signing seams append inclusive on-chain Clock.slot assertions using
the existing immutable Lighthouse deployment. Local head checks still avoid expired sends;
the signed assertions prevent a late packet from buying tokens. A rejected late transaction
can still pay transaction fees. Manual orders, exits and already-sent settlement remain separate.

This execution deadline applies even when first-buy selection is disabled. Missing source slots
fail closed; no wallet exception may extend beyond S+1. A delta-zero exception is stricter.
The guard preserves instruction positions, ALT account identities, and the prior priority-fee
budget while reserving 1,016 additional compute units where capacity permits. Oversized packets,
conflicting guards and unreadable compute budgets refuse before signing. At the protocol compute
limit, additional headroom is unavailable and execution may fail without a purchase.

J7 is an advisory backup, never a prerequisite for a native copy. Canonical opaque UUID hints
remain `hint_only`, not executable events or failed orders. Exact backup entries still require
independent receipt identity, signer/payment/acquisition proof, native ownership checks and the
same S/S+1 assertions. Invalid or unavailable verification evidence remains visible.

The crowded-fee daily allowance defaults to zero (unlimited); positive values remain a rollback.
Per-send limits remain. Failed accounting is unknown, not zero; a finite allowance cannot enable
raised bids until accounting is known. Restart and midnight accounting use the same decision day.

The older send-only S+2 and wider-exception rules below are superseded by this amendment.

### Guarded packet fitting repair, 2026-10-19

Slot-lane packet fitting includes the actual signed slot guards before the existing tip-overflow
fallback. PumpSwap may reuse the same wallet's already verified, held curve lookup table when
the resulting wire packet is strictly smaller. Table identities must agree; funding-leg tables
and instructions remain unchanged. No lookup-table creation, entry-path provider read or new
retry is added. The signing seam still applies the authoritative original bounds and fee cap.
Missing tables and packets that remain oversized refuse unsigned; no clock or slot is extended.

### TPU forwarding deadline repair, 2026-10-19

The private TPU request carries the original automatic entry's `entry_max_slot`. Python checks
the same fresh cached-head policy before connecting and before each socket write. Expiry after
any partial frame remains ambiguous; a no-write refusal is `not_sent`. Exits and manual entries
retain their uncapped request shape. Invalid or zero caps cannot disable the bound.

The pinned Rust sidecar refuses before QUIC when its cached head exceeds that cap and reduces
lookahead to the estimated eligible leader groups, preserving Agave's four-slot leader stepping.
A head changing during selection refuses. No provider call, retry, fee change or shared lock is
added. Agave privately clamps its schedule start, so this is estimated lookahead, not proof of
absolute target-slot membership. Already-running QUIC reconnects and early RPC packets may still
land late and pay fees; signed Clock assertions remain authoritative. Ship matching Python and
Rust artifacts through the pinned deploy gates; rollback uses the preceding matching release.

### RPC rebroadcast deadline repair, 2026-10-19

The shared typed Solana RPC provider sends `maxRetries=0` for entries carrying an
`entry_max_slot`. Custody RPC's configured retries otherwise allow node rebroadcasts after
the local deadline. This override is request-local: uncapped entries, exits and withdrawals
retain their configured retries, including concurrent sends. The original cap, fresh-head
guards, bounded local resends, signed bytes, preflight settings and fee policy remain intact.
No new provider call, wait or lock is added. This cannot recall an initial packet, guarantee
S/S+1 landing or remove fees from late signed-guard failures. Rollback uses the preceding
pinned release. Legacy direct RPC seams are outside this shared-provider repair.

## Historical operator decision, 2026-10-18

Operator decision, 2026-10-18: prefer the source slot; permit sends through source slot +2,
then miss the trade. Do not enter from a later source buy after missing the first opportunity.
Wallet-specific exceptions may widen the deadline and admit later source buys, but require
explicit qualification. The current evidence qualifies no wallet.

The policy applies to automatic funded Solana copies. Manual orders, EVM entries, exits,
already-broadcast confirmation, and reconciliation retain their existing authority.

- Persist the first canonical source transaction per wallet, mint, and armed cohort before
  selection refusals. Same-transaction observations share that claim across feeds and restarts.
  Sells, failures, quote misses, and process restarts do not reopen it. Re-arming starts a new
  cohort. Existing recorded on-chain buys and canonical native attempts in that cohort seed
  the claim conservatively. Unknown historical slots cannot acquire a fresh deadline.
- A default first entry also needs a known zero pre-balance on the source's traded accounts.
  Positive pre-holdings identify an add-on; missing evidence holds. Transaction-touched balances
  are not a complete owner-wide inventory proof. Synthetic Rabbit tape zero is not evidence.
- Freeze each candidate's original source slot and maximum slot. Check a fresh local processed
  head before admission, signing, identity binding, and every provider send, including queued
  workers and identical-byte resends. Never rebase the deadline to dispatch or retry time.
  A per-signature durable slot anchor also covers explicitly qualified later buys. Conflicting
  slot observations hold rather than renew the budget.
- Disable delayed entry/dip/revival retry scheduling for this policy. Any already queued item
  must re-enter the same first-transaction and slot guards. A qualified later buy has its own
  canonical source transaction and bounded deadline; a retry never acquires a new one.
- Missing, invalid, future-dated, or >500ms-old head evidence holds. The local head is an
  observation proxy, not proof of the bank's active slot. Packets sent within the observed
  window can land later; accepted fills must still be booked and reconciled.
- Exceptions use exact source user IDs, never handles or wildcards. There is no automatic
  promotion from PnL, wallet labels, or a few late fills. Keep the live exception map empty until
  independent delayed/later-buy evidence supports a specific bounded exception.

Deploy from an isolated branch through the pinned runtime script with validation,
reservation/reconciliation gates and `--defer-backup`. The operator explicitly chose to keep
the existing policy trading until this release is ready. Do not restore or clear the old pause.
Refresh J7's roster with the cutover so lebomb appears in runtime and persisted caches at 23.
