# Solana slot lane: earliest funded execution after quality gates

## Amendment, 2026-10-20: any execution slot after quality gates

The [current operator decision](2026-10-18-first-buy-slot-window-design.md) removes source-slot
deadlines and signed Clock.slot assertions from new automatic copies. Entries may land in any
slot after existing quality, source-proof, first-buy, freshness, price, safety and risk gates.
Fixed-offset same-byte resends have no dispatch-slot cutoff. Blockhash validity, provider health,
packet limits and minimum output remain. Existing signed guards still reconcile; there is no
replay or re-signing of old attempts. This supersedes the execution-window amendments below.

## Historical amendment, 2026-10-19: signed execution window restored

The [superseding operator decision](2026-10-18-first-buy-slot-window-design.md)
restores inclusive signed Clock.slot bounds at the original source slot S and S+1
for every automatic funded Solana copy. It supersedes the historical no-slot-guard
amendment below. Missing source slots refuse; manual orders, exits and already-sent
settlement retain their separate contracts. An exact failed receipt records
`slot_window_expired`, signed bounds, landing slot and paid fee using the existing
receipt read. Late packets still roll back and can spend fees; no window is widened.

## Amendment, 2026-10-10: V3 source fee-recipient conversion

Pump V3 source trades retain fees on the curve and may emit the System Program placeholder as
`TradeEvent.fee_recipient`. That placeholder cannot be used as the payable recipient in our
legacy SOL buy or quote-token V2 buy: the executable System Program account stays readonly and
the instruction fails `fee_recipient / ConstraintMut` before filling.

The curve builder's existing off-path Global read caches standard and reserved Mayhem recipients
as one immutable instance snapshot. Validate the Global owner, discriminator, initialized state
and required bytes before publishing it. Replace only a placeholder recipient in a copied event,
using the event's mode. Preserve explicit recipients, original source evidence/prices and every
signed output floor. Native SOL, prefetched funding legs, quote-token builds and on-demand packet
estimation resolve the same recipient. Missing or zero mode-specific recipients refuse unsigned
preparation locally; failed refreshes retain the valid held state. Both low-level account builders
reject the System Program placeholder.

No entry-path RPC, retry or lock is added. Provider failures, protocol rotations between refresh
and execution, and slot landing speed remain separate limits. Deployment uses the merged pinned
release, reconciliation and flat-book checks; rollback requires no data/config migration.

Primary protocol references: [Pump buys and fee modes](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/BUY.md),
[Pump Global layout](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json).

## Amendment, 2026-10-09: readiness and timing completion

Processed Shyft full metadata may feed the existing USDC-only `RoutedPairer` for unsigned venue
preparation. One bounded 64-item metadata queue decodes off the socket loop; current roster and
shared `(signature, owner)` dedup apply before emission. It supplies no signer/payment proof.
The watcher-owned CONFIRMED streams remain the proof source; no second subscription is added.
Token identity/decimals, lookup-address counts and instruction payloads must be coherent. Unsupported
metadata keeps HTTP fallback. Confirmed proof suppresses HTTP only after the observation writer
acknowledges an exact durable row; queue acceptance and failed commits remain unknown.

A bounded 64-entry proof-first cache matches signature, owner, user, mint and source slot. It cannot
extend the original processed receipt's 500 ms admission clock. Receipt availability is stamped once;
requeueing never changes it. All routed venue conversions preserve their observed feed label.

Queue heartbeat counts lane handle calls, including retries and refusals before attempt creation.
Settlement/control messages are excluded. The latest 512 queue delays supply p50/p90. `queue_expired`
means fresh at enqueue and expired at handler entry; `already_old` means expired before enqueue.
Expiry inside the handler and missing/backward intervals are unknown. Receipt, enqueue, proof-ready
and dispatch intervals use monotonic clocks. Persisted arrivals use corrected UTC reconstructed from
that receipt age; raw host epochs remain only for compatible local valuation-cache age measurements.

Generic DAMM hints use bounded exact `(pool, mint)` permanent refusals and transient cooldowns.
Complete wrong-program/discriminator/mint evidence is refused once; timeout, absent, malformed or
partial data stays unread. Source-bound learning bypasses generic refusals and retains its mappings.
HTTP warming uses one fixed daemon per provider, one outstanding probe each, without queued work or
catch-up bursts. The existing configured keepalive budget remains conservative. Rollback is the
preceding pinned release; in-memory refusal and timing state resets on restart.

## Amendment, 2026-10-09: refresh isolation and receipt settlement

The operator approved separating dispatch from confirmation. The wallet lock now covers admission,
building, signing and durable dispatch. Two fixed workers confirm and book already-sent identities
on their own SQLite connections; capacity is checked before reservation. Unknown outcomes keep the
signed reservation and never authorize another send. Ordinary Solana entries and full/partial exits
use signature-bound receipt amounts before this overlap is enabled; wallet balance deltas cannot
attribute a concurrent transaction to the wrong fill.

Balance read-start timestamps bind cash sizing. Settled reservations remain conservative cash
debits for an older snapshot, evaluated inside the existing reservation transaction. A bounded
256-record immutable history serves ordinary entry threads too; a read older than discarded
history fails closed. Fresh snapshots stop subtracting completed debits. Global book exposure is
unchanged. Rollback uses the preceding pinned release after reconciliation/reservation checks.

Balances, blockhash and SOL/USD each have one independent refresh worker. Slow venue/Jupiter
extras retain one owner and its own read-only SQLite connection. Partial/failed balance pairs keep
their old values and read-start stamps. HTTP idle expiry covers the warming pause and configured
serial-call timeout budget; a server may still close a socket.

The original admission clock, source proof, signed price floors, sizing policy and submission
identity remain authoritative. Paired feed metrics measure same-process, same-signature processed
arrivals only; confirmed proof is a separate stream. No same-slot inclusion or organic fill-speed
improvement follows from tests. USDC legs can still refuse a changed clip until their existing
off-path refresh completes. A Frankfurt host comparison needs a separate host and calibrated clocks.

Operator order, 2026-09-26 ~20:16Z: the only goal is a real funded Solana fill that lands at the
source buy's slot S or at S+1.

## Why the current path cannot do it

- 36 exact J7 copy fills: none landed by S+1. Best S+3, median S+7 (Codex).
- Decision to submission: 1,380 ms median, at least 535 ms, over 141 recent Solana attempts
  (Codex). Slots are about 400 ms.
- The J7 feed itself arrives about 1.34 s after the block at p50 (docs/audits/2026-09-24-execution-speed-budget.md),
  which is already S+3.
- Codex's read-only processed watcher (scripts/research/solana_processed_tx_probe.py) decodes
  pump.fun TradeEvents from free `processed` logsSubscribe frames. In a four-buy same-socket sample,
  two arrived while the head was still S and two at S+1.

So the signal can be early enough, and the execution path is what is too slow.

## Shape

A new funded entry lane, `slot_lane`, on its own thread inside the runner process, behind
`MEMECOIN_SOLANA_SLOT_LANE_ENABLED` (code default 0).

1. Signal: one WebSocket, `logsSubscribe` at `processed`, `mentions` one armed roster owner
   address each (owners from data/solana_roster_owners.jsonl, filtered to wallets armed on
   solana). The lane decodes pump.fun `TradeEvent` from Program data (decoder already in
   sniper/research/memecoin_opportunity/pumpfun.py). It acts only on `is_buy` events whose `user` is
   the watched owner. S is the notification's context slot.
2. Admission, in memory, no network:
   - wallet armed on solana (roster cache, refreshed every 10 s);
   - kill switch absent;
   - bonding curve not complete; pump.fun curve venue only (v1);
   - mcap from the event's post-trade virtual reserves x pinned supply, checked against
     MEMECOIN_REAL_MAX_MCAP_USD and the band;
   - source buy in USD (SOL amount x cached SOL/USD) checked against the source-buy floor for the band;
   - one position per coin, concurrency, exposure and bankroll, all through `reserve_entry`, fed
     balances cached off the path (refreshed about every 2 s) and a classification synthesized from
     the event.
3. Build: one transaction containing
   - compute budget and priority fee;
   - Lighthouse `AssertSysvarClock` slot >= S and slot <= S+1 (program
     L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95, no accounts, 62 bytes);
   - a Jupiter USDC->SOL leg, PREFETCHED from /swap-instructions for the current clip and refreshed
     about every 2 s off the path;
   - the pump.fun buy, spending that leg's minimum output.
   The blockhash is cached and refreshed off the path. Nothing on this path makes a network read.
4. Sign in process. Write the signature ahead through `set_pending_sig`. Send the same bytes to the
   TPU sidecar and the custody RPC.
5. Confirm and book through the existing executor tail: the fill comes from the transaction's own
   receipt balances (`receipt_entry_balance_delta`), then `finalize_pending_entry`. The position is
   an ordinary funded position, so the existing exits manage it.

The J7 lane is unchanged. The same buy reaches J7 1-3 s later, and the (network, mint) reservation
unique index dedups it. If the slot lane's transaction fails the guard, the reservation is released
and J7 may still fill it late. Whether the guard also applies to J7 is an operator ruling (below).

## Edge cases (CLAUDE.md rule 5)

1. Money path: yes. Entry, sizing and signing. Operator rulings are needed before arming (below).
2. Speed: the lane adds nothing to the J7 path. Its own budget is signal receipt to bytes sent,
   target under 50 ms, measured p50/p90 before arming.
3. Concurrency: its own thread with its own ledger connection. Reservation goes through
   `reserve_entry`'s BEGIN IMMEDIATE, so capacity is serialized with the main loop. No module
   globals are mutated. The prefetch caches are single-writer and read by snapshot.
4. Absent vs zero: a missing SOL price, blockhash, prefetched leg or balance older than its bound
   refuses (`slot_lane_stale_<input>`). It never falls back to a network read on the path.
5. Dependencies: a WS drop means no signal, the J7 lane covers it. A guard failure is a failed tx
   (fee only, the buy rolls back). A lost send is ambiguous: keep the reservation, same as today.
   Lighthouse is upgradeable; its upgrade authority is recorded, and the guard is re-simulated at
   startup.
6. Units: lamports vs SOL, token atomic units (pump.fun 6 decimals, read from the event, never
   assumed), USDC 6 decimals, slots vs seconds.
7. Sample: the lane only sees armed wallets' pump.fun curve buys through their OWNER address. A
   wallet trading through a shared executor is invisible (finding-roster-wallets-trade-through-a-shared-executor-2026-09-22).
   Its fill rate describes that subset, not the roster.
8. Rollback: knob to 0 plus a restart. Half-applied: the lane is off unless every input is present.

Checks left behind: a pytest per builder (bytes and account order), the guard proven by
simulateTransaction at S, S+1 and S+2 on mainnet, and a lane test proving a stale input refuses
without a network call.

## Operator rulings needed

1. Gates: the lane keeps only the in-memory set above. It drops DexScreener market data, the h1/m5
   volume floors, the chase cap, the round-trip cap and the token-safety RPC reads. Approve, or
   name what must stay.
2. Guard on the J7 lane too? With it, J7 would almost never fill (median S+7).
3. Deploy with MARIO 65480 open (Long Hold, automatic exits off), or close it first.
4. SOL for fees: the wallet holds 0.0734 SOL. Every guard failure costs a fee plus the priority fee.
5. Clip for the first fills: the normal clip, or a fixed pilot size.

### Amendment, 2026-10-19: cached Solana volume floor

Ruling 1 above changes only for the distinct, default-OFF
`MEMECOIN_SOLANA_VOLUME_GATE_ENABLED` gate. Native and ordinary automatic Solana entries
require verified organic source-pool volume over a complete 300-second window to reach 2% of
market cap, only while producer coverage is at most five seconds old. Missing, incomplete,
stale, unpriced or unsupported coverage passes unknown. Own trades and the exact source
transaction are excluded. Entry reads immutable memory only; no provider, DB, lock or wait is
added. The old provider h1/m5 floors remain absent from this lane. Full contract and rollback:
`docs/superpowers/specs/2026-10-19-solana-volume-cache-design.md`.

## Owners

- Lambda: slot guard encoder and mainnet simulation proof.
- Beta: direct pump.fun buy builder, the prefetched Jupiter leg, size and build timing, simulation proof.
- Gamma: the addressable set (pump.fun curve share of armed-wallet Solana buys) and the processed-signal slot budget.
- Alpha: the lane thread, admission, submission and booking wiring, deploy.

## Amendments, 2026-09-26 evening (what the lane does now)

The sections above are the original design. Where they disagree with this section, this section
was current at that date. The 2026-10-19 signed-window amendment above supersedes item 1.
Each item names its commit.

1. **Soft deadline, no slot guard** (5d83ef79, d4ff0d5b; operator policy: aim for the earliest
   fill, allow a later landing within the price and risk limits). The Lighthouse [S, S+1] guard,
   the head-past refusals and the signal-age re-checks after the decision are gone. The only
   freshness gate is the signal's age at decision time (`MAX_SIGNAL_AGE_S` = 0.5 s), before any
   write. Head slot, its age and the signal age are still recorded after the build, before signing
   and at dispatch (substages `slot_post_build`, `slot_pre_sign`, `slot_pre_send`), record-only.
   Compute-unit limit 400,000; the guard's 1,016 CU and 62 bytes are gone.
2. **Unit-price cap** (e04ee58a). Both venues sign `max(output floor, price floor)`: the price
   floor is the fewest base-token units the whole USDC ticket may buy at the source's post-trade
   price x (1 + `MEMECOIN_REAL_CHASE_MAX_PCT`/100), rounded up, in the base mint's own decimals
   (PumpSwap: learned `base_decimals`, unknown refuses). Own impact beyond the cap refuses before
   signing. Substage `slot_price_limit`.
3. **6042 diagnosis** (ebd209aa). A pump.fun BuySlippageBelowMinTokensOut failure records the
   tokens available and the minimum signed (`slot_pump_slippage`), and the view and Telegram say
   "Price moved beyond limit ...; no purchase".
4. **Regime-scaled clip** (c88bc6eb). The leg is prefetched for the full clip, so a classification
   with `risk_scale` < 1 is refused before reserving (`slot_lane_scaled_clip`); the confirmed
   watcher treats that as provably unsent and gives the signature one ordinary attempt, which
   quotes the scaled clip itself. Amount-keyed legs were not built.
5. **Entry route** (6c5f7e96). `quote_route_target` on a lane fill is the leg's ammKeys, then the
   coin-facing key (bonding-curve PDA, or the PumpSwap pool), the J7 shape, so vol_death can match
   exit routes. Rows before 6c5f7e96 store the label `slot_lane`.
6. **Token program** (5d34d224) comes from the owner event's own pump invocation; two programs
   there are unknown and refuse.
7. **Startup order** (ecabbc99). The signal socket starts first; a refused socket leaves no worker
   running and no connection open.
8. **Instrumentation** (8698bd92, b419891e). `SlotBuy.head_at_recv`, substages `attempt_row` (the
   shell-row INSERT window, with thread CPU beside wall) and `slot_receipt`, and `recv-S=` /
   `land-S=` on the log line.

9. **Vol_death route** (1710f7b1): an exit route must name one pool (`ammKey` or Raydium `poolId`); a split or keyless route holds as `route_unknown`.

Not landed from Codex's parked branch (wip/foreign-2026-09-26-codex-slot-remediation): the pump
tape pre-sign reserve veto, amount-keyed legs, and the lane stop/cancel machinery.

## 2026-10-09 amendment: Shyft native detection

Operator authorized live Shyft integration. Authenticated AMS/FRA Yellowstone
processed transaction logs now join Helius in the same signal event loop. Native
Pump.fun/PumpSwap decoders and existing funded rules remain authoritative; first
valid source signature/owner wins without an entry lock or provider read. Full
successful metadata, signed watched owner, valid identity and a fresh local head
are mandatory on the Shyft input. Routed balance-pair entries still require their
confirmed signer/payment proof; RabbitStream shred-only input is not funded.
Configuration, evidence and rollback: `docs/audits/2026-10-09-shyft-live-feed.md`.

## Amendment, 2026-10-18: slippage and fee reserve

The existing processed PoolTape also supplies an optional pre-sign impossibility check. A
complete frozen bank at or after the source slot, on the observed ancestry and within current
coverage, may veto a Pump curve or identity-bound PumpSwap buy when even its fee-free gross output
upper bound falls below the already-built signed minimum. This is not an executable quote.
Missing, stale, seeded, unsupported or concurrently changed state keeps the existing path.
The check adds no RPC, simulation, subscription, wait or writer lock to entry admission.
Source-price ceilings, the soft slot deadline, sizing, bids and eligible fallback stay unchanged.

Reservation publishes the current-message SOL allowance from its existing serialized balance,
pending debits and full-book exit reserve. Before signing, the executor compares the rewritten
priority fee, signature base fee, validated tips, native principal and existing conservative ATA
rent bound with that allowance. An unsigned refusal releases only its own unsigned reservation;
an already-signed unknown identity remains owned. Legacy direct executor seams without the new
allowance retain compatibility; the production slot lane always passes it.

These checks cannot prevent price movement after the cache read or submission. A landed revert
still pays fees. No wider slippage, lower priority bid, provider preflight or retry loop is added.

### Mode coverage correction, 2026-10-18

The cached check also covers ordinary Mayhem, cashback and holder-reward curves. It reads the
current frozen account, rather than reconstructing reserves from a source trade. These modes
redirect nonnegative fees; omitting those fees still gives an output upper bound for our own
legacy `buy_exact_sol_in`. The builder identifies that instruction in its immutable guard
metadata. A different or absent instruction contract remains unknown. A source using V3 does
not change our own instruction into V3: our legacy buy remains capped by real token inventory.
The privileged Mayhem agent's fee-free source events are not ordinary-trader pricing evidence.

Known older curve layouts are accepted at complete field boundaries; missing trailing mode
flags use their documented defaults. Truncated required fields, invalid boolean bytes,
and incomplete, seeded or fork-ambiguous snapshots remain unknown. PumpSwap virtual quote
reserves decode as the complete signed `i128`; the guard uses the effective quote reserve and
rejects malformed or out-of-domain evidence. No new call, lock, subscription or wait is added.

A valid completed curve with zero real token inventory has a zero inventory bound for our
legacy or V2 instruction. A completed curve claiming nonzero inventory remains unknown.
Quote-token PumpSwap checks bind the exact mint and token program on both cached vaults. Their
builder must also certify that no funding-prefix instruction names the target pool or either
vault, including read-only account metas because compiled writability is global. Missing or
overlapping prefix proof remains unknown. Native WSOL retains its legacy Token requirement;
all source actor, bank, anchor and concurrent-state checks remain authoritative.

Protocol references: [holder rewards and existing cashback](https://github.com/pump-fun/pump-public-docs/blob/main/docs/HOLDER_REWARDS_README.md),
[legacy versus V3 migration](https://github.com/pump-fun/pump-public-docs/blob/main/docs/SYNTHETIC_MIGRATION.md),
and [signed virtual quote reserves](https://github.com/pump-fun/pump-public-docs/blob/main/docs/NEGATIVE_VIRTUAL_QUOTE_RESERVES.md).

### Quote-token V2 witness correction, 2026-10-18

The quote-token curve builder publishes the same witness for its own `buy_exact_quote_in_v2`:
the exact signed quote-atom spend and token minimum, both token programs, mint, derived curve
PDA, quote mint, source signature and slot. The source event and cached curve must name that
same quote mint; a missing raw quote field cannot borrow a legacy SOL default. The existing
bank, ancestry, startup, source-version and concurrent-state checks select the state unchanged.

The curve's second reserve is `virtual_quote_reserves` in quote atoms. Its fee-free bound is
`min(virtual_token_reserves * spend / (virtual_quote_reserves + spend), real_token_reserves)`,
rounded down. Protocol/creator fees and Token2022 transfer fees cannot increase this result.
Pump V2 remains inventory-capped; our own V3 or an unknown instruction remains unsupported.
Signed floors, source/copy ceilings, funding, bids and fallback policy are unchanged. Missing
proof still preserves the existing path; no provider call, lock, wait or new account is added.

Protocol references: [Pump account layout](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json),
[V2 quote mint and PDA](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/BUY.md),
[V2 versus synthetic migration](https://github.com/pump-fun/pump-public-docs/blob/main/docs/SYNTHETIC_MIGRATION.md),
and [Token2022 withheld transfer fees](https://www.solana-program.com/docs/token-2022/extensions#transfer-fees).

### Open-slot state correction, 2026-10-18

The same processed account stream retains transaction signature, bank ID and startup provenance.
An open bank in the exact source slot can supply the check before its child is created. Every
required account must have the same latest transaction signature and bank ID: one curve account,
or the PumpSwap pool plus both vaults. The exact source transaction's complete group must remain
in the bounded rings in that bank, with no candidate account version older than its source write.
A different candidate transaction must advance every required account's write version.

Agave stores every writable account of a successful transaction, including unchanged data, and
notifies Geyser with the causing transaction. Requiring the pool callback therefore avoids mixing
older virtual quote reserves with newer vault balances. Write versions order updates to each
account; they are not transaction indexes. Missing callbacks never imply unchanged state.

A newer partial group blocks using an older complete group for a veto. Missing provenance,
startup/RPC-seeded accounts, an evicted source group, dead or conflicting banks, conflicting
duplicate writes and concurrent changes remain unknown. Later open slots do not join across
unproved bank ancestry. Existing frozen reads retain their ancestry and coverage checks.
Provenance and guard counters expose actual provider capability; optional protobuf fields alone
do not establish that the provider populates them.

This is an already-observed state check, not a prediction of the execution bank. Updates still
in flight, newly watched vaults and movement after the check remain uncovered. It adds no provider
request, subscription, reader lock or wait; the signed spend, minimum output and bid stay intact.

Protocol references: Agave 3.0.0 [writable account collection](https://github.com/anza-xyz/agave/blob/b6c96e84b10396b92912d4574dae7d03f606da26/runtime/src/account_saver.rs#L104-L129)
and [account versions and transaction-bound notifications](https://github.com/anza-xyz/agave/blob/b6c96e84b10396b92912d4574dae7d03f606da26/accounts-db/src/accounts_db.rs#L6250-L6290).

Bank-ID ambiguity is scoped to the optional gas check. IDs are unique within one validator
process, not across validators or restarts; field presence alone cannot prove a shared origin.
Such ambiguity keeps both open and frozen gas checks unknown. It does not add an RPC fallback
to the older preparation reader, whose coverage, ancestry, seed and late-parent rules remain
authoritative. Dead slots, contradictory parent lineage and conflicting raw owner/lamport/data
bytes at one key/slot/write-version still invalidate that reader. Fixed conflict counters and
eight bounded ID-only samples diagnose
actual provider behavior without a new subscription, request or account scan.

Reference: Agave [process-local BankId contract](https://github.com/anza-xyz/agave/blob/f48350bb93698d0e107030924dd7ce2979acb14b/accounts-db/src/bank_id.rs#L3-L5).

The optional `MEMECOIN_SHYFT_TAPE_CONNECT_IP` pins only the reserve-tape channel to a provider-approved literal IPv4 address. It requires the canonical HTTPS Amsterdam URL, retains its TLS hostname, authority and token, and leaves every other feed on its existing route. Empty restores DNS. The route is frozen for each tape owner; reconnects still invalidate all proofs. Invalid pins fail the existing Shyft startup validation. No signing-path work is added.

An address pin does not establish validator origin by itself. Activation requires a provider contract for one process's account/lifecycle updates and disconnect before any process change, plus observed transport identity and current-generation tape progress after cutover. Shyft confirmed a temporary route on 2026-10-18 and warned it may be deprecated. Preserve that warning, capacity gates and a staged empty-pin rollback; never silently fall back to shared DNS or normalize conflicting bank IDs.

## Amendment, 2026-10-18: cash preparation after Jupiter throttling

Native cash-leg preparation yields for the same ten seconds as exotic warming after a funded
Jupiter HTTP 429. Its background-only scope checks before every physical request, including host
failover, and before each remaining clip size. A throttled quote therefore cannot trigger another
background host request, instruction build or sibling builder refresh during that interval.
Retained legs keep their original timestamps and expire under the existing age bound.

Funded entry workers and exits remain outside this scope: their host failover, Raydium rescue,
typed outcomes and fresh exact-quote requirements are unchanged. This adds no retry, wait,
provider capacity, stale-leg admission, fee change or relaxed source/output check.

## Amendment, 2026-10-18: first source buy and S+2 observed-head limit

When `MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW=1`, automatic funded Solana entries replace the
legacy soft sending deadline with a durable first-source opportunity and an original source-slot
+2 cap. Native and confirmed feeds share the claim; selection misses, source sells and restarts
cannot reopen it. Every signing/send boundary requires fresh observed-head evidence. This does
not guarantee the active bank or landing slot; already-sent transactions still reconcile and book.
Exact-ID exceptions start empty. Manual entries, EVM entries and exits remain unchanged.
The full contract is `docs/superpowers/specs/2026-10-18-first-buy-slot-window-design.md`.


## 2026-10-19 movement-only copy cap

`MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY=1` supersedes the source-relative price floors in this
spec. The existing coherent PoolTape guard compares source-post to current-pre marginal spot;
unknown proceeds by explicit operator choice. Size-aware positive output slippage is independent.
See the 2026-10-19 amendment in `2026-09-28-memecoin-chase-post-trade-reference-design.md` for
coverage, missing-output backup, no-new-I/O constraints and rollback.

## Amendment, 2026-10-21: creator-independent launch buys

Position 74106's source created and bought AmJ7MkUDgQUsjakuos9nqvBx1z8TaedKiHTyTyt2pump.
Before our S+1 execution, its creator changed to the fee-sharing configuration. The authentic
source TradeEvent therefore named an obsolete creator; our legacy buy failed `ConstraintSeeds`
on `creator_vault`. Do not infer fee-sharing authority or cashback eligibility from zero fees.

For a SOL-quoted direct source buy, a unique prior Pump CreateEvent in the same successful
transaction may prove cashback disabled. Bind that event to the mint, canonical curve and token
program. Only explicit boolean false selects `buy_exact_quote_in_v3`; cashback true and missing,
malformed or conflicting evidence retain the legacy instruction. Exotic quotes are unchanged.

The v3 layout has 17 accounts and no creator vault. Native SOL funding, the exact spend, output
floor, source/copy caps, base ATA creation and durable signed identity remain unchanged.
`partial_fill` is false. Its unmeasured CU class uses the existing default, not legacy samples.
V3 can continue into synthetic migration: where the fee-free bound reaches real inventory,
total token supply is a conservative upper bound. The independent market-movement cap still runs.
This does not add a synchronous call, lock or retry, or predict the landing slot.

Check: `python3 -m pytest -n 2 tests/test_pump_curve_creator_migration.py tests/test_memecoin_slot_signal.py tests/test_slot_lane_slippage_guard.py -q`.
The fixture preserves the exact source and failed creator-vault evidence. Unsigned mainnet
simulation on this mint reproduced legacy error 2006 while v3 succeeded (55,822 CU).
The alternating 650-sample local decode/build check measured p50/p90 0.263/0.274 ms before and
0.294/0.305 ms after; this is CPU overhead, not signal-to-fill evidence. Rollback restores the
legacy build without a schema or state migration. No activation is implied by these checks.

Protocol reference: [Pump v3 trade contract](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/TRADE_V3.md).
