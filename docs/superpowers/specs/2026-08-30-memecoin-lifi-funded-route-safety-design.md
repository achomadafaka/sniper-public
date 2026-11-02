# LI.FI funded fallback route safety for chain 4663

Date: 2026-08-30
Status: approved specification; implementation not approved
Scope: same-chain LI.FI entry and exit fallback on Robinhood Chain 4663

Related specification: `2026-08-30-memecoin-lifi-funded-quota-reserve-design.md`

<!-- schema:external=memecoin_lifi_authority_events -->

## 1. Decision

LI.FI is a fallback for an exact direct-route miss. It is not another direct route rung.

The system can use LI.FI only on chain 4663. The system must disable bridges.

The system must preserve every current safety, chase, exposure, duplicate, loss, and exit rule.

The system must validate all LI.FI data as untrusted input. A LI.FI simulation grants no authority.

The local executor owns transaction construction, identity, submission, settlement, and recovery.

This specification does not approve implementation, deployment, a funded canary, or activation.

## 2. Non-goals

This design does not add another chain. It does not add bridge use or Composer use.

This design does not lower a price, impact, chase, round-trip, gas, or post-peak limit.

This design does not promise a fill for every signal. An unsafe token or missing safe route must still fail.

This design does not use LI.FI status data as transaction or settlement proof.

## 3. Direct route precedence

The runner must call the existing `classify_quote_miss()` result before it can call LI.FI.

LI.FI can run only when all configured direct rungs return the exact `no_route` class.

These results block LI.FI:

- `provider_*`;
- timeout;
- HTTP 429;
- an unattributed result;
- an incomplete direct rung result.

A valid direct quote ends route selection. It must cause zero LI.FI client calls.

A later safety, chase, approval, or execution refusal cannot promote the attempt to LI.FI.

Direct provider faults cannot promote LI.FI. The trace must keep direct and LI.FI failures separate.

Each position preview and exit must try the direct ladder first. A valid direct exit can close a LI.FI-dependent position.

## 4. LI.FI request sequence

A successful entry path uses exactly four LI.FI quote requests:

1. Request the initial buy quote.
2. Request the initial sell check.
3. Confirm the exact approval receipt.
4. Request the refreshed buy quote.
5. Request the final sell check.

Step 3 makes no LI.FI request. An early failure ends the attempt with fewer requests.

The quota guard must reserve four requests before the attempt starts.

The final sell input must equal the refreshed buy quote's exact integer atomic `toAmountMin`.

The initial sell input must equal the initial buy quote's exact integer atomic `toAmountMin`.

The refreshed buy must keep the approved spender and exact input. A spender change must abort the attempt.

The system must not start a second approval cycle for that attempt.

The final sell check proves current exit availability for the guaranteed minimum. It cannot prove future liquidity.

LI.FI defines `toAmountMin` as the guaranteed minimum output after slippage.

Source: [LI.FI quote API](https://docs.li.fi/api-reference/get-a-quote-for-a-token-transfer), read on 2026-08-30.

## 5. Request controls

Each request must set all these controls:

- chain 4663 as the source and destination chain;
- bridges disabled;
- an explicit, non-empty `allowExchanges` list;
- no `all` or `default` exchange selection;
- zero integrator fee;
- LI.FI simulation enabled;
- the current contract-specific `maxPriceImpact` limit.

The response must contain exactly two `includedSteps` values in this order:

1. one `feeCollection` step with type `protocol`;
2. one release-approved step with type `swap`.

The response must contain no other step.

The fee step must use the input token as its input and output token.

The fee step input must equal the exact request input.

The decoder must calculate `fee_atomic = input_atomic * 25 // 10_000` for each request.

The fee step output must equal `input_atomic - fee_atomic`.

The swap step input must equal the fee step output.

The LI.FI service fee rate must equal `0.0025`. The integrator fee must equal zero.

The top-level tool and each step tool must exist in the release pin-set manifest.

The system must reject a different service fee or a non-zero integrator fee.

The system must record all provider fees and gas estimates.

`maxPriceImpact` is an additional refusal. It cannot override a local refusal.

The `/v1/tools` result is information only. It cannot change funded authority.

## 6. Release pin-set manifest

Each content-addressed release must contain one chain-4663 LI.FI manifest.

The system must use canonical serialization before it calculates the manifest hash.

The canonical manifest hash is the pin-set hash.

The manifest must contain these release-pinned values:

- each accepted top-level tool;
- each accepted step tool;
- each transaction target;
- each spender;
- each calldata selector;
- each nested `callTo` and `approveTo` target;
- each release-pinned ABI definition;
- each target runtime bytecode hash;
- each Diamond selector facet address;
- each facet runtime bytecode hash.

The manifest must pin the exact two-step sequence and the `0.0025` service fee rate.

The manifest must pin the fee target, spender, selector, ABI, and runtime bytecode hash.

The system must reject a missing, empty, incomplete, or mismatched manifest.

Each new quote must use the current release manifest. A historical route grants no authority.

LI.FI uses an EIP-2535 Diamond. The Diamond delegates selector calls to facets.

Sources: [LI.FI contract addresses](https://docs.li.fi/introduction/lifi-architecture/smart-contract-addresses) and [LI.FI contract architecture](https://docs.li.fi/introduction/lifi-architecture/smart-contract-overview), read on 2026-08-30.

## 7. Local semantic decoder

The system must decode the outer selector and each nested call locally.

The decoder must use only the ABI definitions from the release manifest.

The decoder must reject these conditions:

- malformed offsets;
- unused bytes;
- trailing bytes;
- an unknown selector;
- a selector without a complete local decoder;
- an unpinned address, spender, selector, facet, or code hash;
- an unexpected chain, wallet, token, recipient, amount, or native value;
- a path that does not start with the input token;
- a path that does not end with the output token;
- a bridge;
- a Composer VM call;
- a protocol action other than the required `feeCollection` step;
- an external recipient;
- unauthorized native value.

The decoder must validate chain 4663, the custody wallet, both tokens, the recipient, and the exact input.

The decoder must validate the minimum output and the complete token path.

The decoder must pin each nested `callTo`, `approveTo`, selector, and token path.

The decoder must validate the exact fee arithmetic and atomic continuity from the request to the swap step.

The system must decode the refreshed post-approval quote again.

The LI.FI beta calldata parser is information only. It grants no funded authority.

## 8. Economic acceptance

The system must use exact integer atomic arithmetic. It must not use a float or a USD value for the round-trip cap.

The guaranteed entry quantity equals the refreshed buy `toAmountMin`.

The final sell input must equal that guaranteed entry quantity.

The guaranteed proceeds equal the final sell `toAmountMin`.

The system must apply the current contract-specific round-trip cap without adjustment.

Each economic gate must use the full request input and the guaranteed output after the service fee.

The round-trip check must include the buy service fee and the sell service fee.

The system must rerun each quote-based local gate against the refreshed buy quote.

The rerun must include these gates:

- impact;
- chase;
- execution premium;
- post-peak;
- gas sufficiency.

A local refusal always overrides a valid LI.FI quote.

This test proves only the current worst-case quote round trip. It cannot guarantee future exit liquidity.

## 9. Approval authority

The LI.FI path must not use the current generic approval pipeline.

The LI.FI path must reject Permit2, EIP-2612, all typed signatures, and Permit2Proxy.

The system must not sign EIP-712 data or any other typed approval message.

The system must reject `typedData`, `executionType=message`, and `skipApproval=true` flows.

The system must reject any target or nested call to Permit2 or Permit2Proxy.

The decoder must reject each permit selector, `callDiamondWithPermit2`, and `callDiamondWithEIP2612Signature`.

Only a classic ERC-20 `approve(spender, exact_amount)` transaction is permitted.

The system must approve only the exact atomic input amount for the validated spender.

The system must persist the approval transaction identity before network I/O.

The approval record must bind these values:

- lifecycle identity;
- signed transaction hash;
- nonce;
- signed-payload hash;
- token;
- custody wallet;
- spender;
- exact atomic amount.

The system must wait for the approval receipt before the refreshed buy and final sell checks.

An ambiguous approval must stay unresolved. The system must not revoke it immediately.

Recovery must reconcile its hash, nonce, receipt, and allowance.

A failed pre-sign check or a confirmed reverted swap requires `approve(spender, 0)`.

A submitted or unknown swap must keep the allowance state unresolved. The system must not revoke it.

After a successful swap, the system must read the exact token, custody, and spender allowance.

If the allowance is non-zero, the system must start the durable revoke path.

The system must record the fill and open the position before allowance cleanup.

A cleanup fault must never hide a real position.

The system can release a failed-entry reservation only after chain evidence confirms zero allowance.

An unresolved LI.FI allowance blocks new LI.FI entries for that chain and custody wallet.

Direct entries remain available. Allowance cleanup uses local EVM authority and needs no LI.FI API access.

Each later revoke attempt must use a new immutable sequence record. It must not overwrite a prior identity.

LI.FI supports Permit2 and EIP-2612 approval flows through typed signatures and Permit2Proxy. This funded path rejects those modes.

Source: [LI.FI Permit and Permit2 approval flow](https://docs.li.fi/introduction/user-flows-and-examples/permit2-approval-flow), read on 2026-08-30.

## 10. Quote freshness and retries

The LI.FI client must disable HTTP, SDK, and transport retries for each request.

The system must bind each response to its full request, corrected receive time, and quota headers.

The attempt trace must store the request hash and response hash.

The system must check both refreshed quote ages immediately before it signs the swap.

A stale refreshed buy or final sell quote must start allowance cleanup.

The system must not request a fifth entry quote.

The funded LI.FI path must not share positive or negative results across ticks.

This rule replaces a funded 60-second negative cache.

A pre-approval provider fault can permit a new attempt on the next tick.

A post-approval provider fault must start allowance cleanup.

An exit can advance a slippage rung only after a typed slippage or minimum-output failure.

An exit must not advance after `no_route`, timeout, HTTP 429, or a provider fault.

The attempt trace must store the stage, rung, and provider failure type.

## 11. Transaction construction and code proof

LI.FI can supply only the validated `to`, `data`, and `value` fields.

The local executor owns the sender, chain, nonce, gas, fee, and all other transaction fields.

Each LI.FI response must have its own on-chain code and facet proof.

This rule applies to the initial buy, initial sell, refreshed buy, and final sell responses.

Immediately after each response, the system must select one canonical proof block.

The proof must bind the request hash, response hash, corrected receive time, block number, and block hash.

At that block, the system must check each target, spender, selector, Diamond mapping, facet, and runtime code hash.

The check must include each nested executable target from the decoded response.

A proof for one response cannot prove another response. The system must not reuse a proof for an identical route.

A pre-approval proof failure must refuse the attempt. A post-approval proof failure must start allowance cleanup.

The final buy transaction requires an additional code proof and exact simulation at one canonical execution block.

The system must use this sequence after the final sell response proof:

1. Estimate gas and construct the complete unsigned transaction.
2. Select one canonical block.
3. Read the block number and block hash.
4. Recheck the Diamond, selector mapping, facet, spender, and executable targets at that block.
5. Simulate the exact final transaction at that block.
6. Sign locally without more provider I/O.
7. Persist the block proof, selector mapping, and all code hashes.
8. Broadcast the exact signed bytes through the existing EVM path.
9. Audit the same identities at the receipt block.

The system must decode and validate the final transaction before simulation.

A successful simulation cannot override a semantic mismatch.

A mismatch before the signature must start allowance cleanup.

A receipt-block mismatch must disable all LI.FI quote and swap actions.

Direct exits and local allowance cleanup must remain active. LI.FI reserves must remain allocated.

A confirmed transaction remains valid settlement evidence. A later mismatch cannot erase a real fill.

Before the signature, the system must read the current input-token allowance for every pinned LI.FI spender.

The selected classic spender allowance must equal the exact transaction input.

Each other pinned LI.FI spender allowance must equal zero.

The input-token allowance to Permit2 and Permit2Proxy must equal zero.

An unexpected non-zero allowance must block the signature and start allowance cleanup.

The upgrade loss is amount-bounded only when all these allowance checks pass at the execution block.

Under that condition, the exact allowance bounds the accepted upgrade loss to the transaction input plus gas.

This risk is amount-bounded. It is not time-bounded because an EVM transaction can remain pending.

## 12. Immutable submission and settlement

The system must use the existing immutable submission binding.

The system must persist the signed hash, nonce, and signed-payload identity before network I/O.

The system must broadcast the exact signed bytes through the existing EVM path.

An ambiguous send must stay `submitted_unknown`.

The system must never rebuild, rebroadcast, release, or replace an ambiguous transaction.

Settlement requires a successful canonical receipt for the exact signed hash.

An entry requires exact WETH input evidence and exact-mint output evidence.

The entry WETH evidence must equal the refreshed buy quote's exact atomic `fromAmount`.

Each entry WETH recipient must match the locally decoded execution semantics.

The entry receipt must contain no other WETH outflow from the custody wallet.

The entry mint evidence must sum only exact-mint `Transfer` logs to the custody wallet.

The acquired mint quantity must be at least the refreshed buy quote's `toAmountMin`.

An exit requires exact-mint input evidence and WETH proceeds evidence.

The exit mint evidence must equal the executed exit quote's exact atomic `fromAmount`.

Each exit mint recipient must match the locally decoded execution semantics.

The exit WETH evidence must sum only WETH `Transfer` logs to the custody wallet.

The exact WETH proceeds must be at least the executed exit quote's `toAmountMin`.

The receipt must contain no unexpected custody token movement.

The system must use the balance delta when the balance read is current.

For an entry, current WETH and mint balance deltas must agree with the validated receipt transfers.

For an exit, current mint and WETH balance deltas must agree with the validated receipt transfers.

If a balance read lags, the system must use the validated receipt transfer quantity.

A current balance and receipt disagreement must keep settlement unresolved.

Incomplete settlement evidence must keep the reservation, claim, position slot, and LI.FI reserve.

The system must commit fill state, position creation, and lifecycle position linkage in one transaction.

The system must commit terminal close, position finalization, and LI.FI slot release in one transaction.

LI.FI quote and status fields are never settlement evidence.

## 13. Finalized replacement proof

Elapsed time, mempool absence, null lookup, timeout, and a pending nonce are not drop evidence.

The system can prove a drop only at a canonical finalized block.

If an RPC does not support finalized state, the transaction must stay `submitted_unknown`.

The custody RPC and the release-pinned proof RPC must agree.

A provider disagreement must keep the transaction `submitted_unknown`.

The proof must contain all these facts:

1. The finalized block number and block hash.
2. No canonical receipt for the exact signed hash.
3. A finalized custody nonce above the signed nonce.
4. A finalized same-nonce transaction with a different hash.
5. The replacement transaction's canonical receipt.
6. No exact-mint custody transfer or other swap settlement effect.
7. A complete balance and allowance reconciliation.

The risk ends only after a terminal receipt or this complete replacement proof.

After the proof, the system must read the allowance.

A non-zero allowance requires a confirmed revoke before reservation release.

A zero allowance needs no revoke.

## 14. LI.FI-dependent position rule

A successful LI.FI entry must set `lifi_dependent` during the position insert.

The value is immutable. The system must never update it later.

The LI.FI dependency remains until the terminal close commits.

A later direct route does not release the LI.FI slot or reserve.

A partial exit does not release the slot.

A signed, submitted, or unknown close does not release the slot.

The system can release the slot only after the confirmed terminal close and position finalization commit.

Each fresh LI.FI exit quote must use the current release authority. A historical path grants no authority.

## 15. Durable lifecycle storage

The system must use one append-only `memecoin_lifi_authority_events` table.

One immutable lifecycle identity must exist for each reservation.

Each lifecycle event must have a unique, increasing sequence number.

Each state transition must insert a new event. The system must not overwrite a prior event.

The event must bind the expected prior state. A failed compare-and-set must cause recovery to reread the lifecycle.

The lifecycle must link `position_id` once, in the same transaction that inserts the position.

All atomic amounts must use canonical decimal `TEXT`.

These values must be write-once:

- transaction hashes;
- nonces;
- signed-payload hashes;
- transaction identities;
- block numbers and hashes;
- code and manifest proofs;
- request, response, calldata, and decoded-semantics hashes.

Each revoke attempt must use a new sequence event.

The implementation must provide no delete path. The audit record stays permanent.

The system must not use `memecoin_submission_attempts` as the authority record.

The system must not use the pending row as authority after position creation.

The system must not add a mutable slot counter.

Slot use must use a distinct union of these owners:

- each unresolved pre-position lifecycle;
- each linked open LI.FI-dependent position.

A linked open position and its lifecycle must count once.

An invalid lifecycle must remain unresolved and consume one conservative slot.

## 16. Quota contract

The quota formulas and source limits live in the related quota reserve specification.

The current binding values are:

```text
entry_request_cost = 4
canary_request_cost = 7
one_position_exit_reserve = 7,206
one_position_admission = 7,210
two_position_exit_reserve = 14,412
two_position_admission = 14,416
```

Each admission check must subtract all in-flight requests.

Entry disable must not consume reserved exit quota.

The authenticated default quota permits one LI.FI-dependent position.

A second position requires at least 14,416 available requests when no request is in flight.

Public mode permits no automated LI.FI entry or exit.

## 17. Startup and recovery

LI.FI must stay default off.

LI.FI recovery must not delay direct-route startup or the runner heartbeat.

Startup must reconstruct each lifecycle and validate its event sequence.

The runner can start direct routes before LI.FI recovery completes.

The runner must recover all nonterminal lifecycles before it admits a new LI.FI entry.

An unresolved approval must block new LI.FI entries.

An unresolved swap must keep its reservation, slot, allowance state, and finality state.

An invalid lifecycle must disable LI.FI and consume its conservative slot.

Startup must verify these values before automated LI.FI use:

- chain identity;
- release SHA;
- manifest hash;
- custody wallet;
- finalized support from the custody RPC;
- finalized support from the proof RPC;
- authenticated quota evidence.

A quota, pin, proof, or RPC fault must block new LI.FI entries.

Existing LI.FI exits can run only when all transaction checks pass.

Direct entries, direct exits, the runner heartbeat, and local allowance cleanup must remain active.

## 18. Canary and activation

Public access permits one explicit manual `$5` canary only.

The canary must bind these values:

- canonical manifest hash;
- custody wallet;
- chain 4663.

The canary must complete entry, settlement, position creation, exit, and terminal finalization.

The canary must prove zero allowance for each token and spender pair.

A canary from an older release can authorize activation when the manifest, wallet, and chain match.

A stale or mismatched manifest, wallet, or chain cannot authorize activation.

The canary is the only live end-to-end proof. A test or fixture cannot replace it.

Canary success does not enable automation.

An operator must separately activate the exact pinned release.

Authenticated quota evidence must exist before automated activation.

This specification does not authorize the canary or automated activation.

## 19. Disable and alert rules

The state snapshot must show all active disable reasons.

The system must emit one alert for each new disable reason.

The system must not repeat the same alert on each tick.

The system must emit one recovery alert after the reason clears.

A receipt-block code mismatch must disable all LI.FI quote and swap actions.

An authentication fault or HTTP 429 must disable new LI.FI entries immediately.

The system must not use public access as an automated fallback.

Direct route actions and local allowance cleanup must remain active.

## 20. Operator state and trace

The runner must persist one atomic LI.FI state snapshot.

The snapshot must contain a corrected timestamp and the runner heartbeat.

The web process must read this snapshot. It must not infer state from the web environment.

The UI must mark stale state. It must never present stale data as current.

The UI must show these non-secret values:

- release SHA;
- manifest hash;
- custody wallet identity;
- chain identity;
- public, authenticated, or disabled mode;
- effective quota and reset;
- in-flight requests;
- exit reserve;
- slots used and slot limit;
- each active disable reason;
- unresolved approvals, swaps, revokes, and finality proofs.

The attempt trace must store these hashes:

- request;
- response;
- calldata;
- decoded semantics.

The trace must also store receive times, quota headers, tools, fees, gas, gates, stages, and rungs.

The trace must never store API keys, raw calldata, or signed transaction bytes.

## 21. Required focused tests

The implementation must use a red-green focused test cycle.

The quota tests must test both sides of these boundaries:

- `6 / 7` for the canary;
- `7,209 / 7,210` for one-position admission;
- `14,415 / 14,416` for two-position admission.

Each boundary test must also run with in-flight requests.

The route tests must prove that a direct success causes zero LI.FI client calls.

The direct-route tests must prove that a LI.FI fault does not delay startup, heartbeat, entry, or exit.

The decoder tests must reject each malformed or unauthorized semantic form in Section 7.

The decoder tests must reject a wrong step order, a third step, a wrong fee, and broken atomic continuity.

The decoder tests must calculate the fee from each exact input. They must not use a sample amount constant.

The authority tests must reject each address, selector, facet, ABI, code, and manifest mismatch.

The approval tests must reject Permit2, EIP-2612, typed signatures, Permit2Proxy, and an unexpected allowance.

The code-proof tests must require a distinct proof for each of the four quote responses.

The code-proof tests must reject a reused proof and a mismatch in each response stage.

The economic tests must use exact atomic values at each cap boundary and include both service fees.

The state tests must inject a crash before and after each durable write and network call.

The recovery tests must cover approval, swap, settlement, revoke, receipt audit, and finalized replacement proof.

The recovery tests must prove that unresolved state retains the slot and allowance authority.

The settlement tests must prove receipt transfer recovery after a delayed balance read.

The entry settlement tests must prove exact WETH outflow and exact-mint custody inflow.

The exit settlement tests must prove exact-mint outflow and WETH custody proceeds.

The settlement tests must reject an unexpected custody movement and a current balance disagreement.

The finalized proof tests must reject each incomplete proof and each RPC disagreement.

The status tests must prove stale state, all disable reasons, alert deduplication, and one recovery alert.

The schema validator and focused funded-path tests must pass.

## 22. Implementation and activation boundary

This document approves the design only.

The design does not approve an implementation plan, code, migration, API key, deployment, restart, canary, or funded activation.

An implementation plan requires separate approval.

A funded canary requires separate approval after the implementation and review complete.

Automated activation requires another explicit operator action for the exact release SHA and manifest hash.
