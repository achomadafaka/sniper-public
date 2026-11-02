# Funded EVM type-2 (EIP-1559) signing — spec delta

Date: 2026-09-30. Builder: Lambda. Order: operator "build the type 2", relayed by Alpha. Reviewer:
Gamma. Status: Gamma CLEAN on 8f0466e75 (2 LOWs folded in).

## 1. Why

Finding 3 of the chain-1 fork rehearsal (`docs/audits/2026-09-30-eth-step7-fork-rehearsal.md`,
`beta/eth-mainnet` 8147497a8): the executor signs every funded EVM transaction as a legacy
(type 0) transaction at 1.5x the node's `eth_gasPrice`. On an EIP-1559 chain a legacy
`gasPrice` is both the fee cap and the tip, so the whole signed price is paid. The fork's receipts
show `effectiveGasPrice` equal to the signed price. A type-2 transaction with the same cap pays
`baseFee + maxPriorityFeePerGas` and keeps the rest.

## 2. What changes

1. **New knob `MEMECOIN_EVM_TYPE2_CHAINS`**: comma-separated chain ids. Default empty. It is read
   when called, like `MEMECOIN_EVM_PREAPPROVE_CHAINS`. Malformed means empty.
2. **Tip**: `MEMECOIN_EVM_TIP_GWEI_<chain>`. This is the same env name that `beta/eth-mainnet`
   reads in `evm_tip_gwei_for`. On that branch the tip is added on top of the buffered legacy
   price. Here it becomes `maxPriorityFeePerGas`. One knob, one meaning: what we pay the block
   producer.
3. **A chain signs type 2 only when it is listed AND its tip is above 0.** A listed chain with no
   tip signs legacy exactly as today, and `build_real_evm_executor` logs an ERROR naming the chain.
   Flashbots Protect drops a transaction with a priority fee of 0 (its quick-start docs:
   "does not accept transactions with a priority fee of 0"). Keeping legacy bytes keeps exits
   signable on the proven envelope; a refusal would strand bags.
4. **One place converts: `sign_tx`.** It is already the single signing point for every funded EVM
   signer: `_approve`, `_sign` (entries, exits, the exit gate's unwrap and reduced-fee branch,
   the pre-transaction rewrap), LI.FI (`_lifi_sign_complete`, `lifi_local.sign_fn`), and the gas
   top-up, which uses `lifi_local`. Callers keep building a dict with `gasPrice` = the price they
   compute today. For a type-2 chain (`tx_dict["chainId"]`), `sign_tx` replaces `gasPrice` with:
   - `maxFeePerGas` = that same `gasPrice`, so every fee cap is unchanged: `max(venue quote, 1.5x
     node price)`, or the exit gate's reduced price;
   - `maxPriorityFeePerGas` = `min(tip, maxFeePerGas)`. A node refuses a tip above the cap, and the
     exit gate's reduced-fee branch can push the cap under the tip;
   - `type` = 2. `chainId`, `nonce`, `gas`, `to`, `value` and `data` are unchanged, and the
     access list is empty.
5. **No new network read.** Both fees come from data already fetched: the `eth_gasPrice` read the
   executor makes today, and the knob. No read is added on the fill path. The only added work is
   CPU: two env reads and a dict rebuild inside `sign_tx` (§5.2).

## 3. Every consumer of the signed bytes

| consumer | today | with type 2 |
|---|---|---|
| write-ahead identity | `signed.hash` from eth_account; `evm_tx_identity` = keccak(raw) | eth_account hashes the typed envelope, keccak(0x02 ‖ rlp). Both agree; tested. |
| byte-identical re-broadcast / re-offer (`memecoin_exit_rebroadcast`, the executor's receipt-wait re-offer, the shared transport) | sends stored bytes | unchanged: the bytes are opaque there |
| `decode_signed_tx_cost` | legacy only; returns None for a typed envelope | also decodes 0x02: `rlp([chainId, nonce, tip, maxFee, gas, to, value, data, accessList, v, r, s])`. `gas_price` = maxFee, `worst_case_wei` = gas × maxFee + value, and it adds `max_priority_fee`. Types 0x01, 0x03 and 0x04 stay None (fail closed). |
| nonce-floor rewind (`evm_real_seams`, preapprove release: `rec["nonce"]` from the decoder) | legacy nonce; None for typed, so the rewind is skipped | nonce decoded from type 2 |
| dropped-close cause label (`memecoin_real_runner`, `never_admitted_gas`) | decoder | decoder, bound = maxFee |
| trace facts (`signed_gas_facts`) | price + limit | also `max_priority_fee_wei` |
| receipt recovery, Ink recovery (`recover_confirmed_ink_entry`, `recover_confirmed_native_pre_transaction`) | receipt and `eth_getTransactionByHash` JSON (hash, from, block, value, input) | unchanged: no byte decode; `effectiveGasPrice` is in every receipt type |
| chain-1 deadline decode (`beta/eth-mainnet`) | already handles 0x02 | unchanged |

## 4. Gates

- **Exit sign gate** (`_exit_sign_gate`): `need = gas × gp + value`, and `gp` becomes
  `maxFeePerGas`, so it is the same number and the admission bound a node applies to type 2. The
  reduced-fee branch sets `maxFee = reduced` and the tip is capped by §2.4.
- **Entry float** (`evm_native_float.float_covers`), **LI.FI affordability**, **top-up plans**:
  they price `gas_price_fn` = the buffered node price, which is what the signed cap is built from.
  Unchanged.
- **Gas guard** (`evm_gas_guard_refusal`, on `beta/eth-mainnet` only). Alpha's order: the guard
  prices the EXPECTED effective cost, `baseFee + tip`. This build adds `_expected_gas_price()`
  inside `build_real_evm_executor`. On a type-2 chain it returns the raw (unbuffered) `eth_gasPrice`
  from the same memoised read, plus the tip. On a legacy chain it returns the signed price
  (legacy pays all of it). It is an upper bound: the node's `eth_gasPrice` already holds the node's
  own suggested tip. On rebase Beta replaces `gas_price_wei=_node_gas_price()` in the guard with
  `_expected_gas_price()`. Admission reserves still use the cap, never the expectation.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** Every funded EVM signature on a listed chain. With the default (empty)
   list, the signed bytes are byte-identical to today's: a golden test pins legacy raw bytes for a
   fixed key, nonce and fee.
2. **Fill-path speed.** No new RPC, lock or retry. The CPU cost is measured at build with the real
   `sign_tx`, legacy against type 2, p50 and p90 over 2,000 signs. A prototype with eth_account
   0.13.7 measured 3.5 to 4.0 ms per sign for both envelopes, a difference inside the run-to-run
   noise (0.06 to 0.4 ms).
3. **Concurrency.** No module state is written. The knob is read from `os.environ` at sign time.
   `_gas_memo` gains a `raw` field, written under its existing lock in the same critical section.
4. **Absent, zero, unknown.**
   - Chain not listed: legacy.
   - Listed with tip unset, 0, malformed or not finite: legacy plus an ERROR at build.
   - Tip above the cap: capped.
   - Unreadable gas price: `None`, as today (no signature).
   - A decoder input of an unknown type: None, so its caller fails closed as today.
5. **Dependency failures.**
   - A node that does not support type 2 refuses the bytes: `eth_send_raw` returns its existing
     rejection or ambiguity result. That happens only on a listed chain, and chain 1 supports it.
   - Flashbots Protect: its docs name the 0-tip refusal but do not name the envelope types it
     takes. Type-2 acceptance there is UNVERIFIED until the first chain-1 send; the fork proof
     cannot reach the relay.
   - eth_account 0.13.7 signs typed dicts. A signing exception returns None, as today.
   - **Same-nonce replacement (Gamma LOW 1).** A node replaces a pending type-2 transaction only
     when BOTH its maxFee and its tip rise by at least 10%. With a fixed tip, every same-nonce
     re-sign is refused as "replacement transaction underpriced". No path replaces a nonce on
     purpose today. One can collide: after an ambiguous preapproval rewinds the nonce floor, the
     next signature may reuse a nonce whose transaction is still in a mempool. Legacy can win
     that at a fresher 1.5x price; type 2 never does, so the pending one lands first. Any future
     speed-up or replacement path must raise the tip and the cap by 10% or more.
6. **Units and identity.**
   - The tip is in gwei; `int(round(gwei × 1e9))` wei.
   - `chainId` must be present, and all four signers set it. The knob lookup keys on
     `str(int(chainId))`, so `1`, `"1"` and `"0x1"` all match `1` (Gamma LOW 2).
   - The priority is capped in wei.
   - The decoder reads the rlp item widths: long data lists (0xf8+) and empty value.
7. **The sample.** The fork proof (§6) runs on anvil's fee market. It proves the mechanism: type
   2 lands, pays base fee plus tip, and costs less than legacy at the same cap. It does NOT
   measure live chain-1 savings, which depend on the live base fee and the tip we choose.
8. **Rollback.**
   - Remove the chain from `MEMECOIN_EVM_TYPE2_CHAINS` and restart. Its next signature is legacy.
   - Type-2 intents already stored keep working: the decoder stays and the re-broadcast is opaque.
   - Reverting the CODE while a type-2 intent is pending brings back the old decoder. The rewind
     is skipped and the label reads `evicted_unmined`; that is the state before this change, never
     a double send. Revert the code only with no pending type-2 intent.

**Not handled:**
- Operator scripts (bridges, withdrawals, funding) sign legacy on their own paths.
- The tip is fixed per chain: no `eth_feeHistory`, no dynamic tip.
- Listing an L2 changes our ordering priority there. A legacy transaction's tip on an L2 is
  `gasPrice − baseFee`, which is large at 1.5x; type 2 pays only the knob. Listing a chain is the
  operator's decision; the build targets chain 1.

## 6. Tests and proof

- Golden: empty list gives legacy bytes identical to today's, for each of the four signing paths.
- Type 2 on a listed chain with a tip, from each signing path: envelope 0x02, `maxFee` = the
  caller's price, priority = `min(tip, maxFee)`.
- Listed without a tip gives legacy plus the ERROR. A reduced exit cap below the tip gives
  priority = cap.
- Decoder round trip: legacy and type 2, value 0, data > 55 bytes, and bound = gas × maxFee +
  value. Types 0x01 and 0x04 decode to None.
- Identity: `sign_tx` hash = `evm_tx_identity(raw)` for type 2.
- Nonce-floor rewind and the `never_admitted_gas` label work on type-2 bytes.
- `_expected_gas_price`: raw + tip on a type-2 chain, the signed price on a legacy one.
- **Fork proof** (opt-in, anvil fork, throwaway key, the same traps as the step-7 harness): the
  production `sign_tx` signs one `WETH.deposit` as legacy and one as type 2, at the same cap and
  the chain-1 tip.
  - Both land.
  - Type 2 has `effectiveGasPrice = baseFee + tip < cap`, and legacy has `effectiveGasPrice =
    cap`.
  - Cost is `gasUsed × effectiveGasPrice`: type 2 < legacy.
  - The receipt hash equals the signed identity.
  - The landed bytes decode to the gate's bound.
- The repo guards (`guards.sh`) and every test file that touches the changed symbols.

## 7. Coordination

`beta/eth-mainnet` (unmerged, waiting on the operator's chain-1 decision) adds `1` to
`MEMECOIN_EVM_TYPE2_CHAINS` there only, never on main before the chain-1 go.
- Beta's chain-1 private transport is untouched: this changes only the envelope.
- On rebase Beta:
  - wires the gas guard to `_expected_gas_price()`;
  - keeps `evm_tip_gwei_for` as the one reader of `MEMECOIN_EVM_TIP_GWEI_<chain>`, and points this
    build's tip helper at it.

## 8. Results (build, 2026-09-30)

- **Latency (rule 5.2), measured with the real `sign_tx`.**
  - The added knob lookup (`_type2_tip_wei`) costs p50 1.2 µs on an unlisted chain and 2.3 µs on
    a listed one, over 20,000 calls.
  - Legacy signing before and after this change: main p50/p90 3.53/3.67, 3.69/4.83, 3.64/3.81 ms;
    this branch 3.37/3.49, 3.55/3.68, 3.75/3.85 ms. Three runs of 2,000 signs each; the difference
    is inside the noise.
  - Type 2 against legacy on this branch: p50 +0.02 to +0.06 ms, p90 +0.04 to +0.11 ms, on a
    3.3 to 3.5 ms sign.
- **Fork proof** (`tests/test_memecoin_evm_type2_fork.py`, upstream `mainnet.gateway.tenderly.co`,
  fork block 26,093,073). The production executor `_sign` signed `WETH.deposit` at the same cap,
  after a warm-up deposit:

  | | effective gas price | gas used | cost (wei) |
  |---|---|---|---|
  | legacy | 2.518124521 gwei (= its cap) | 27,938 | 70,351,362,867,698 |
  | type 2 | 0.149552077 gwei (base 0.099552077 + tip 0.05) | 27,938 | 4,178,185,927,226 |

  Both landed with status 1. Each receipt hash equals the signed identity. The type-2 bytes decode
  to the gate's bound, 150,000 × cap + value. The cap is anvil's own price, which climbs as legacy
  transactions overpay, so the ratio is not a live chain-1 figure.
- **Checks that can fail.**
  - `tests/test_memecoin_evm_type2_signing.py` kills 18 of 18 mutants
    (`~/.cache/lambda/prior/mut_type2.py`). Three of those survived the first draft and were closed
    by the tip-without-list, malformed-list and 11-item tests.
  - The fork proof kills "never type 2" and "tip = cap".
