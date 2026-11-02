# BSC inventory recovery, source initiation and usable funding

## Amendment, 2026-10-09: durable proof acknowledgement

Confirmed Yellowstone metadata must bind static signer keys, loaded-address lookup counts, coherent
pre/post token identity and decimals, quote decimals, and supported token instruction payloads.
A missing token-balance side requires exact initialization/closure evidence and zero corresponding
account lamports; an idempotent ATA create cannot establish a zero prior balance.
An unsupported token extension keeps the existing HTTP fallback. The observation writer acknowledges
proof only after committing an exact source-bound row; a duplicate must contain that same proof.
Queued, rejected, timed-out or dropped writer jobs cannot suppress HTTP receipt fetching. Callback
failure never retries a committed write. Processed metadata may prepare the existing USDC routed
candidate without granting proof authority or resetting its original monotonic receipt deadline.

## Amendment, 2026-10-11: refused direct multi-buy frames

A direct multi-buy refusal no longer consumes shared native/routed feed ownership. Processed
whole-wallet balance pairs may prepare a routed target, but the refused frame requires confirmed
whole-transaction signer, payment and acquisition proof; one embedded venue leg cannot grant it.
Verified PumpSwap `buy_v2` pool/buyback transfers may bind route payment only when the instruction,
event, actor, accounts, token programs, decimals and exact transfers agree. Platform fees stay outside
the signed whole-route average. Gross source-size policy still counts the confirmed wallet payment.
V2 post-pool quote reserves include retained creator/protocol fees and exclude external buyback.
Unsupported cashback, unknown layouts and contradictory payment data remain blocked.

Proof-first receipts may resume the original cached candidate only inside its original 500 ms
window. Every exact-origin reserved, signed or confirmed native owner blocks resume, even if a newer
trace reports missing proof. Ownership reads and cached-proof consumption run on background receipt
or routed threads. Successful native filling gains no provider call, database read, lock or retry.
This fixes route preparation and handoff coverage; it does not establish a sub-200 ms live fill.

## Amendment, 2026-10-09: confirmed Shyft routed proof

A filtered CONFIRMED Yellowstone stream may supply the complete successful Solana transaction
metadata to the existing signer/payment proof decoder. It uses the configured Shyft token and
regions, with bounded decoding, persistent subscriptions and no replay. Only explicit verified
proof acceptance suppresses a later HTTP receipt read. Unsupported instructions, partial metadata,
unknown identities and stream failures preserve the Helius HTTP path. PROCESSED balance pairs
still cannot grant routed initiation authority. Receipt resumes retain the original 500 ms clock.

Operator order, 2026-10-04: write off positions 69487 and 69489, restore usable BSC funding,
and block the verified failure shape without banning every small/new coin. The operator
authorized implementation and live activation. This supplements the Relay acquisition decoder
and BSC fast-lane contracts; acquisition and initiation are separate facts.

2026-10-06 operator amendment: require the same initiation safety feature on all chains.
Robinhood incident SICAT #70895 was another wallet's Base-funded Relay delivery to the followed
wallet. Destination acquisition alone remains insufficient.

## Contract

1. Use the existing atomic write-off helper. Book $0 final proceeds against the actual entry
   cost and existing de-risk proceeds. Refuse any in-flight close/de-risk identity or ambiguous
   exit intent. Preserve an audit, the real loss and the token balance. Do not invent a sale.
2. Automatic on-chain copies on every chain require identity-bound proof that the tracked wallet initiated
   and paid for that acquisition. Direct proof requires the tracked wallet as transaction sender;
   linked token payment alone does not establish initiation. Preserve the proof through receipt decode, stored valuation, datagram,
   event and retry reconstruction. A Relay delivery proves acquisition by the beneficiary but
   does not prove that beneficiary initiated the purchase. Unknown initiation remains available
   for observation and cannot authorize automatic entry. Existing independently reported feed
   events retain their current policy; manual entries retain operator authority.
3. Enforce the source check in the shared funded hook before provider work or a reservation.
   It must be pure and bounded: no extra query, RPC, thread, lock, wait, or retry. Proofs must
   bind chain, transaction, token, beneficiary, source identity and receipt block. Missing,
   malformed or mismatched evidence refuses. An operator risk-cap override does not change
   source identity. EVM proof is chain-independent. Solana requires the owner among the successful receipt's
   signers and a linked positive quote payment/token acquisition. Known Pump/PumpSwap processed
   buy events may prove their own signing user without another receipt read. Processed routed
   balance pairs cannot prove a signer: refuse them before reservation and leave the unique
   detection signature free for the confirmed watcher. A slot refusal for unknown initiation
   allows one ordinary confirmed attempt, which must pass the shared guard again.
4. Keep the BSC native float but cap it against the current liquid native + WBNB bankroll.
   Protect the ordinary gas target first; allocate at most half remaining liquid funds to
   the additional native float, up to the configured float target. Preserve WBNB cash for
   reservation sizing and wrapped-token routes. Preserve refill hysteresis. This runs only in
   gas maintenance using balances already required by that pass. Recompute under the existing
   custody lock, with current WBNB as well as native balances, before deciding an amount.
5. A preliminary BSC lane check may require only the minimum eligible clip. The shared hook
   chooses the approved size; the executor rechecks that exact amount under its existing
   custody lock before signing. The $50 pilot ceiling remains a ceiling rather than a minimum.

## Edge cases and boundaries

- Money: immutable classification, approved sizing, reservation identity and signing remain
  authoritative. Write-offs retain the actual loss in global financial/risk totals. No wallet
  grading exclusion, risk reset, or rearm is implied by closing inventory.
- Speed: source checking and preliminary balance checking add only local arithmetic. Measure
  p50/p90 separately from organic fill latency. Funding balance reads belong to the existing
  maintenance worker, never the entry path.
- Concurrency: no global config mutation, shared observer connection, or temporary monkeypatch.
  Maintenance checks reservations before and under custody locking. Signed identity is journaled
  before broadcast. Unknown confirmation holds that identity and prevents a second transfer.
- Unknown: absent initiation is not direct spend; absent balance is not zero. Unknown/malformed
  balances abstain from rebalance. Zero cash is known cash starvation; total funds under the gas
  reserve cannot support a trade, and gas protection takes precedence.
- Dependency failures: transport, rejection, rate limit, timeout, partial and wrong responses do
  not authorize a new source or a transfer. Existing balance-agreement, chain identity and
  confirmation guards remain active. A rejected source signal is not retried as a provider miss.
- Units: BSC is chain 56, native and WBNB use 18-decimal atomic integers. Source proof equality
  uses canonical addresses/hashes; USD sizing stays separate from atomic rebalancing.
- Sample: regressions demonstrate eligibility and refusal, not a market edge. A local benchmark
  measures this predicate, not same-block landing. Real fill latency needs organic post-activation
  observations.
- Rollback: restore the preceding pinned release after the reservation/reconciliation checks.
  Ledger write-offs and maintenance transaction journals remain real history. A rollback cannot
  erase a loss, undo a confirmed wrap, or release an unresolved signed transaction.

Not handled: recovery of withdrawn liquidity, first-time rug actors, a future LP pull after a
genuine purchase, full token permission/immutability verification, or an externally submitted smart-account authorization. The existing asynchronous Relay verifier
may bind a destination on any numeric EVM chain to a signed origin deposit and a prior verified
wallet identity; API user/beneficiary labels alone cannot authorize it. Solana Relay deliveries
without owner signer proof remain unknown. Genuine unproved deliveries may miss the on-chain fast opportunity until the
existing authoritative feed supplies its own buy event. Market cap, youth, sparse trading,
shared Relay infrastructure and clone bytecode alone are not blacklist evidence.

## Verification

### 2026-10-06 fast-only and signed copy-price amendment

CPMM candidates carry net acquired tokens: `SwapEvent.output_amount - output_transfer_fee`, matching the wallet's processed balance delta and confirmed receipt exactly. Comparing the receipt to gross output rejects valid Token-2022 buys such as Rari. Native decisions, including pool misses before an attempt row exists, use the existing asynchronous timing-note drain; routing and pair-expiry hints cannot replace a concrete lane result.

When `MEMECOIN_SOLANA_FAST_LANE_ONLY=1`, automatic Solana entries use the native lane. The confirmed watcher may resume a cached processed native candidate only when signature, wallet, source ID, slot, mint, acquired amount and paid amount match exactly; the built venue leg must be a positive part of that acquired amount (a split route's leg is smaller; amendment 2026-10-06 late, KKK F4g7BYDm). Publish the candidate before terminal commit/wake; carry the original receipt clock and reject past 500 ms. The queued candidate owns the signature until its native result is durable. No extra receipt read, quote, retry or entry lock is added. An unsigned refusal, pool miss, expiry or ambiguous submission cannot authorize ordinary fallback. A signed native pending row with confirmed `chain_error` failure permits one ordinary attempt through all existing gates.

`MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT` is an independent ceiling above the source average paid price. It cannot be lifted by post-trade spot references, stale chart marks or wallet premium overrides. Solana builds strengthen the signed output floor; Pump curve/AMM floors use fee-inclusive source average alongside their existing post-trade checks. EVM rejects a loose or missing guaranteed minimum before approval, after any tax adjustment, and after refreshed quotes. LI.FI checks its decoded guaranteed output. Automatic events without a valid source average refuse before provider work. Manual entry authority and all exit rules remain unchanged. Both flags default off for compatibility; the operator-authorized funded rollout enables fast-only and a 35% ceiling unless a tighter ceiling is selected.

2026-10-14 clarification: a valid final Pump event can be an intermediate SOL leg
of a USDC-funded purchase. For confirmed receipts, value that purchase using
instruction-bound USDC swap input, excluding external fees/unrelated transfers;
do not multiply the intermediate SOL leg by an independent USD mark. Retain
verified final-event provenance and require complete payment identity for parsed
and compiled receipts. Mixed or ambiguous payment remains unknown. Program-only
processed native valuation and existing admission/size policy remain unchanged.
The Pump native price-limit trace names post-price chase and average-copy caps
separately; both values come from the actual build inputs. See the
[RICK audit](../../audits/2026-10-14-rick-source-price-measurement.md).

2026-10-09 OP amendment: the operator selected a 30% maximum for Robinhood. `MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT` defaults to 30 and bounds the shared hard-cap result on chain 4663, including wallet overrides, EVM/LI.FI signed minimums, receipt fast-lane builds and dip retries. A stricter existing cap remains stricter; global zero remains off, and zero on the new setting disables only its chain bound. Invalid negative or non-finite values fail startup. Other chains retain their existing limits. Override telemetry compares against the actual bounded chain baseline. Rollback is the preceding pinned release; existing positions and exit rails are unchanged.

This does not guarantee a profitable fill, prevent a later rug, recover withdrawn liquidity or turn unknown signing into retry authority. Tight minimums may skip or revert rather than chase. Roll back code and these environment settings together; durable signed entry and exit claims remain authoritative.

Relay provenance alone never disqualifies a signal. Source initiation and our execution route's signed floor remain separate checks. For same-chain Relay entry routes ending in the pinned, non-proxy `AggregatorGuard.IceCreamSwap`, bind the terminal guard's output token and recipient to our requested token and wallet, strengthen its uint128 minimum to at least Relay's declared minimum, and preserve all other calls and the request-ID trailer. That guard checks the recipient's actual token balance increase, including transfer fees, before returning. Verify its runtime hash at executor startup; failed or unavailable verification grants no binding authority and adds no entry read, retry or lock. Work on a fresh quote copy. The ceiling accepts only the local proof bound to the exact executable calldata, target, zero native value, sell amount and minimum; refreshed or altered bytes require a new proof. Unknown terminal shapes, malformed bounds and mismatched identities remain unproved execution routes, never honeypot labels. Exits keep their original Relay transactions. RelayRouter `cleanupErc20s(0)` alone still provides no minimum guarantee ([primary router source](https://github.com/relayprotocol/relay-periphery/blob/main/src/RelayRouter.sol)); the supported terminal guard's verified source/runtime and real TENET quote are recorded in `tests/fixtures/memecoin/relay_tenet_signed_minimum_20261006.json` ([primary guard source](https://sourcify.dev/server/v2/contract/4663/0x603206d6105217dd972e4ab30676a220ca393346?fields=all)).

Replay the two acquisition shapes; prove they cannot authorize automatic entry on every supported chain. Test genuine
direct purchases, small-market-cap eligibility, malformed and mismatched proofs, retry/datagram
preservation, early refusal with provider seams forbidden, and exact under-lock size validation.
Reproduce the native-float cash starvation with the observed balances, then test sufficient,
insufficient, unknown and racing balances plus journaled wrap/unwrap confirmation. Watch the new
regressions fail before implementation. Verify the exact pinned release, chain balances, journal,
closed/Open APIs, reservations and admission state after activation.

## Amendment — verified J7 Solana backup (operator approval 2026-10-11)

`MEMECOIN_J7_SOLANA_VERIFIED_BACKUP=1` admits a narrow exception to fast-only: an exact J7 wallet buy hint may use the existing ordinary entry path after the confirmed watcher has persisted matching direct-wallet signer/payment/acquisition proof. Bind transaction signature, source user, wallet and mint; opaque event IDs, absent receipts, processed program proofs and unproved transfers cannot qualify. J7's external buy label is never proof.

The canonical on-chain event retains its receipt clock and average-fill reference. Existing source-age, roster, source-size, price premium, signed minimum, safety, route and exposure gates apply unchanged. Filled, unresolved, unreadable or policy-decided native attempts block backup; the existing per-coin reservation CAS arbitrates races. A native receipt resume takes priority and retains its original 500 ms deadline. Publish confirmed proof-first arrivals to that same bounded resume cache even before a processed candidate exists. No provider read or wait is added to native filling.

Only terminal unsigned J7 fast-only/proof-unknown refusals with no reservation or submission evidence stop owning the source decision, so a later confirmed detection can proceed. Retry preserves the canonical proof and backup marker, rechecks native ownership, and requires the flag still enabled. Rollback: disable the flag and restart the pinned runner, or restore the prior release and environment. No historical replay or funded retry is authorized.
