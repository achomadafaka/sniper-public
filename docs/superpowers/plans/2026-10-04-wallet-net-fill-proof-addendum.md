# Receipt-backed wallet net fill proof

Scope authorized by the operator through the root agent on 2026-10-04. Producer ownership is
`memecoin_evm_onchain_watch.py`, the observation watcher's `_record`, a pure source-fill validator,
and focused tests. Root owns the Robinhood cache reader and funded consumers. No deployment.

## Edge cases before implementation

1. Money path: consumers can price a funded entry from this proof. Preserve existing reservation,
   routing, signing, and submission logic. The proof describes a receipt-backed wallet fill; it
   does not grant trader initiation or funded authority.
2. Speed: use only the receipt and decoder arithmetic already available. No new RPC, retry,
   synchronous lock, or receipt scan. Measure added pure proof construction/validation p50/p90.
   Root's cache reads belong to its separate read-only worker, outside the decision path.
3. Concurrency: all new state is function-local. Do not mutate module attributes, environment,
   provider clients, or shared handles. `_record` retains its existing caller-owned connection.
4. Absent, zero, unknown: missing identity/atoms/decimals/USD, zero quantities, malformed values,
   and non-finite values all produce no proof. Preserve the observation and ordinary fallback.
5. Dependencies: provider timeout, rejection, rate limit, ban, incomplete receipt, or wrong
   receipt remain handled by the existing decoder. No proof from unsupported decoder evidence.
   Malformed stored JSON is a cache-reader concern; the validator returns unknown for bad objects.
6. Units/identity: atoms are positive canonical decimal TEXT; decimals are integer 0..36; paid
   USD is the decoder's finite positive payment valuation, with explicit exact/upper-bound basis.
   Bind canonical numeric chain,
   transaction, block number/hash, wallet, source UID, and mint. Reject mismatched initiation UID.
   Pool gross output is not wallet net output. Tax/hook and split fills use complete receipt net.
   Direct simultaneous native/quote payment cannot fit this one-payment proof. Its previous
   max(native, quote) USD observation understated actual mixed spend. The authorized repair
   returns no direct buy/price; ordinary sources remain available, without authenticated mixed
   payment pricing. Combining currencies into a new proof format is outside this change.
   Direct Arc ERC20 payment previously used 18 decimals against its 6-decimal quote. The separately
   authorized correction supplies existing chain quote metadata without new reads; native EVM
   transaction value retains 18 decimals. This changes Arc observation pricing to its actual units.
7. Sample: proof coverage denominator is decoded observations with complete matched receipt
   evidence, not swaps, trader activity, executable edge, or profitability. Fixture checks prove
   wire behavior only; organic source coverage and latency need a separate live observation.
8. Rollback/half-applied state: old rows and old producers lack `source_fill`; consumers must
   retain ordinary fallback. A producer-only deployment changes JSON provenance, not execution.

Not handled: unverified receipts, unsupported currencies/routes, trader intent, or receipt
reorganizations after recording. Preserve the already-decoded complete source-pool post state,
including final same-pool sqrt price and buy-leg gross atoms. Root must bind post-price/market cap
to that source pool and avoid pricing wallet net output from a partial swap tape.

## Wire/API

`validate_source_fill(proof, sig) -> dict | None` is pure. `proof_version` is
`wallet-net-fill-v1`; `receipt_proof_version` names a recognized receipt decoder proof.
Normalized keys are `network_id`, `tx_hash`, `block_number`, `block_hash`, `wallet`,
`source_user_id`, `mint`, `payment_token`, `payment_atomic`, `acquired_atomic`,
`payment_decimals`, `token_decimals`, `paid_usd`, `payment_basis`, and the two proof versions.
Optional `source_post` preserves typed fallback reasons or a unique receipt-derived pool/venue,
ratio, `mint_is_c1`, `pool_input_atomic`, `pool_output_atomic`, and final `sqrt_price_x96` for
V3/V4. V2 keeps exact `reserve0_atomic` and `reserve1_atomic` and uses the ordinary route.
Missing post is permitted for measurement; native fast consumption requires unique complete post.
Unique post also requires `receipt_routes_complete=true`. Any unresolved V4 PoolKey retains the
ordinary decoder's existing ratio but normalizes stored source post to `reason=v4_key_unknown`.

Expected signal keys are `wallet`, `source_user_id`, `mint`, `tx_hash`, `seq` (or
`block_number`), and `chain` (or `network_id`). Absent chain is accepted only as the RH
caller's implicit canonical `4663`; explicit aliases or other inconsistent bindings abstain.
Optional signal block hash must match. `_record` requires complete Transfer log identity.

Store the validated proof under existing `onchain_wallet_detections.source_valuation_json`'s
`source_fill` field. Schema confirmed with `scripts/brain_index.py schema onchain_wallet_detections`.

## TDD/checks

Write failing behavioral tests first: 200 pool output/180 net, split net 300, exact large TEXT,
wrong chain/UID/transaction/block/hash/mint/wallet, partial evidence, malformed atoms/decimals/
USD, and legacy proof-less observation. Then smallest producer/validator change. Run focused
decoder, source-initiation, observation write-scope, and source-fill suites. Keep one runnable
negative proof check. Root coordinates broader funded reservation/native execution checks.

## Independent-review follow-up: complete payment basis

Money-path edge found: quote100 to a linked mint counterparty plus quote10 to an unrelated fee
recipient previously produced a purportedly complete payment100 proof. Retain the ordinary
observation, but omit complete fill proof whenever any negative quote counterparty flow is
unexplained; preserve `source_fill_reason=unexplained_quote_outflow` in valuation JSON. A quote
refund from the same linked party is provable in Transfer logs and reduces exact wallet outflow.

Native transaction value is gross, with an unobserved possible native refund. Record it only as
`payment_basis=transaction_value_upper_bound`; acquired token net remains receipt-backed. A gross
native upper bound can loosen an average entry cap. Strict funded fast consumers must reject this
payment basis unless no-refund semantics are independently proved; this producer does not prove
such semantics. Exact ERC20 direct payment is `wallet_quote_outflow`; existing verified Relay
receipt payment is `relay_receipt_outflow`. Old direct proof lacking a basis is unknown; the Relay
decoder and economics remain unchanged.

No added RPC, receipt scan, retry, lock, global mutation, signing or submission. Amounts remain
positive TEXT; absence of basis is not zero or exact. Existing side-party outflow arithmetic runs
on local decoded counters. Rollback to an older consumer must not turn this bounded native
measurement into an exact funded cap. TDD includes unlinked quote fees, offsetting unrelated quote
inflow, linked refunds, native upper-bound labeling and malformed/missing basis. Benchmark the
updated local arithmetic again. Not handled: native internal refunds or new no-refund route proof.
