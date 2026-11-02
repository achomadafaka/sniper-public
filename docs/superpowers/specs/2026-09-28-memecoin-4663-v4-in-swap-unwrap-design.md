# 4663 V4 buy: unwrap WETH inside the swap transaction (design + rule 5)

Status: SPEC + read-only PROOF, nothing built. Author: Lambda, 2026-09-28. Operator direction via
Alpha (11:31Z): "remove the conversion and just keep our equity in the right value". This supersedes
option 3 (native counted as spendable, branch `lambda/native-spendable`, shelved).

## What the measurement says

Funded 4663 fills since 2026-09-21, n=51 (ledger, `evm.quote_initial` winning rung and the
`evm.pre_transaction_pipeline` / `evm.unwrap_facts` substages, read-only):

| winning rung | unwrap pre-tx | no unwrap |
|---|---:|---:|
| v4 (single hop) | 18 | 3 |
| v4_multihop | 2 | 12 |
| launchpad_curve | 3 | 0 |
| kyber / relay / v2 | 0 | 13 |

- The V4 single-hop unwrap costs p50 1203 ms / p90 1825 ms (n=18). It is a receipt wait: a nonce+1 swap cannot
  spend ETH from an unmined unwrap (`pipeline_native_pre_transaction`, attempt 7204b30c).
- Of the V4 unwrap fills whose executed pool matches a quoted key: 5 native hooked
  (hook 0xe5e702…), 2 WETH-currency hooked (hook 0x4e3468…, today unwrap THEN re-wrap inside the
  router), 0 plain native. 11 could not be matched.
- WETH-in routes (kyber, relay) already pay a pipelined exact approval instead: entry
  `evm.approval` outcome `broadcast` n=29, p50 295 ms / p90 453 ms.

## Design

One transaction, value 0, no pre-transaction, behind `MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS`
(default empty = today byte for byte):

| pool | today | new |
|---|---|---|
| native, hooked | pre-tx `WETH.withdraw`; UR `[V4_SWAP(path, settle from router)]`, value=amount | UR `[PERMIT2_PERMIT(WETH), PERMIT2_TRANSFER_FROM(WETH, router, amount), UNWRAP_WETH(router, amount), V4_SWAP(same input)]`, value 0 |
| WETH, hooked | pre-tx `WETH.withdraw`; UR `[WRAP_ETH(router), V4_SWAP(path, settle from router)]`, value=amount | UR `[PERMIT2_PERMIT(WETH), PERMIT2_TRANSFER_FROM(WETH, router, amount), V4_SWAP(same input)]`, value 0; no unwrap and no wrap |
| native, plain | pre-tx + UR `[V4_SWAP(single)]`, value=amount | unchanged in v1 (no live pool to prove it on; see Proof) |
| WETH, plain | permit-funded already | unchanged |
| v4_multihop native | pre-tx + value | unchanged in v1 (unproven) |

- The `V4_SWAP` input is byte-identical to today's. SETTLE with `payerIsUser=false` pays from the
  router's own balance: native for a native pool (V4 `_settle` sends `value` from the router), WETH
  for a WETH pool.
- The quote declares `issues.allowance = {"spender": permit2}`. The executor's existing exact
  WETH->Permit2 approval then runs. On 4663 it is nonce-pipelined (balance-neutral, so admission
  is safe), the same flow kyber and relay use.
- PERMIT2_PERMIT reuses `_permit2_input` (the sell path's signer). Its Permit2 nonce read starts
  on a worker thread at the top of `dex_v4_quote`, beside the quoter calls, so it adds no serial
  read.
- Eligibility, all required, else today's shape: chain in the knob;
  `cfg.memecoin_evm_approval_pipeline(chain)` is true (a receipt-first approval costs ~2.4 s,
  worse than the unwrap); a signer; a permit built. A failed permit falls back to today's shape
  and says so on the trace (`evm.v4_in_swap_unwrap` outcome `fallback_<why>`).
- `route.in_swap_unwrap = true` on the quote, so readers can tell the shapes apart.

Expected saving on a V4 hooked buy: the 1203 / 1825 ms receipt wait, minus the approval send
(295 / 453 ms), about 0.9 s at p50 and 1.4 s at p90. It is not measured until a canary runs.

Pons and launchpad (3 of 23 unwrap fills, all launchpad_curve in the window; pons 0) pay native
`value` straight to the curve (`evm_launchpad_curve.py:222`) or to the GMGN router
(`evm_pons_v2_quote.py:290`). The Universal Router cannot call either contract, so an in-tx unwrap
there needs a helper contract that pulls WETH, unwraps and forwards the call. That is a deploy and
an operator decision. Until then they keep the pre-tx, or a native float sized to them.

## Proof (read-only, 2026-09-28 12:04-12:05Z)

`scripts/research/v4_in_swap_unwrap_proof.py`. eth_call with state overrides on
robinhood-rpc.publicnode.com from a throwaway, never-funded address (key in memory only). The
overrides give it WETH, a WETH->Permit2 allowance (the approval the executor would send) and gas
native. 0.001 WETH per call, 14 calls at a 3 s spacing, RTT 38-89 ms, no 403.

```
native_hooked (0xcabb54…, hook 0xe5e702…)   weth_hooked (0x082235…, hook 0x4e3468…)
  legacy       ok      as expected            legacy       ok      as expected
  in_swap      ok      as expected            in_swap      ok      as expected
  no_unwrap    revert  as expected            min_out_2x   revert  V4TooLittleReceived (0x8b063d73)
  min_out_2x   revert  V4TooLittleReceived    no_approval  revert  TRANSFER_FROM_FAILED
  no_approval  revert  TRANSFER_FROM_FAILED
native_plain: both candidate pools revert at the QUOTER (0x6190b2b0; one wraps
PriceLimitAlreadyExceeded at MIN_SQRT_PRICE+1, i.e. drained). No verdict for the plain shape.
```

- The controls fail for the reason each one removes. `min_out_2x` reverts with
  V4TooLittleReceived, so the swap ran. `no_approval` reverts in the Permit2 pull, so the WETH
  pull is real. `no_unwrap` reverts at the native settle, so the router pays the pool from the
  unwrapped ETH.
- The router's `WETH9` is the chain WETH: UNWRAP_WETH found the WETH that PERMIT2_TRANSFER_FROM
  delivered.

## Rule 5

1. **Money path.** Yes: this builds the funded entry transaction. Every item below applies.
   Nothing is built until Alpha (and the operator, through Alpha) says go.
2. **Speed.**
   - Removes a receipt wait: p50 1203 / p90 1825 ms on V4.
   - Adds a pipelined approval send: p50 295 / p90 453 ms, measured on 29 entry approvals.
   - Adds a permit sign: p50 4.8 ms / p90 4.9 ms, measured locally (n=30).
   - Adds a Permit2 nonce read. It runs concurrently with the quoter, so its serial cost should be
     zero. Its latency on the custody pool is UNMEASURED; the canary records it as a sub-mark.
   - Nothing new runs off the fill path.
3. **Concurrency.**
   - Permit2 nonces are sequential per (owner, WETH, router), so two WETH permits signed on the
     same nonce: the second reverts.
   - Entries and exits on one chain hold the per-(wallet, chain) lock across quote, send and
     confirm (`serialize_executor`, `memecoin_evm_leg.py:318`). Exits permit the coin, not WETH.
   - The only permit taken outside the lock would be a reused capacity quote. That happened 0 of
     103 times on 4663 since 09-21 (`reused_capacity_quote`). NOT guarded in v1 (build note): the
     probe cache is a closure with no observable a test can pin, and the failure it would prevent
     is a reverted swap (gas and one lost entry), not a loss of funds. Revisit if reuse is ever
     non-zero.
   - One new global (build correction): `_PERMIT_NONCE_POOL`, a 4-worker ThreadPoolExecutor in
     `evm_v4_quote.py`. It is thread-safe by construction and holds no state between reads. The
     builder is otherwise pure apart from the knob read.
4. **Absent vs zero vs unknown.**
   - No signer, a failed nonce read, or a failed signature: today's shape, stated on the trace.
     Never a missing quote.
   - A WETH balance below `sellAmount` makes the transferFrom revert and burns gas. Today's
     `evm.unwrap_facts` guard refuses an unwrap above the WETH just read. The same comparison
     (sell amount vs `quote_raw_pre`) applies to `in_swap_unwrap` quotes.
5. **Failure modes.**
   - The approval is accepted, then dropped: the swap reverts in the Permit2 pull. WETH does not
     move and only gas is lost. This is the existing allowance-revert path.
   - The swap reverts on min-out: permit, pull and unwrap all roll back.
     - No orphaned native is left to re-wrap (today's `finish_with_cleanup` exists for exactly
       that orphan). Strictly safer.
     - The WETH->Permit2 approval stays at `amount`, and the retry's approval overwrites it.
     - The Permit2 nonce is unchanged, so the retry signs the same nonce, which is correct.
   - Preflight: `preflight_weth_entry_quote` already overrides the WETH allowance for
     `issues.allowance.spender` (Permit2 here), so it simulates before the approval lands. A test
     pins this.
6. **Units.**
   - `amount` is WETH wei (18 dec); `native_scale` is 1 on 4663.
   - The Permit amount is bounded to uint160 (checked in `_permit2_input`).
   - UNWRAP_WETH's minimum is `amount`.
   - The fill accounting is unchanged: one WETH balance delta (`quantity_source` `balance_delta`).
   - The native float path never triggers, because the quote carries no pre-transaction.
7. **Sample.**
   - The canary population is 4663 V4 single-hop hooked entries with the knob on, first attempt
     per origin.
   - Compare against V4 unwrap fills since 09-21 on:
     - `evm.pre_transaction_pipeline` absent;
     - `total_ms` p50 and p90;
     - approval `broadcast` p50;
     - receipt `gasUsed` against today's two receipts.
   - n >= 20 before claiming a live improvement. 4663 fills are sparse: median gap 52 min.
8. **Rollback.** Empty the knob and restart the runner, and today's bytes come back. The knob is
   read per quote, and a quote is signed inside the same lock hold, so a process never mixes the
   two shapes within one attempt.

## Not handled

- Plain native V4 pools and V4 multihop keep the pre-tx until a live pool proves them.
- Pons and launchpad keep the pre-tx (they need a helper contract).
- A standing WETH->Permit2 allowance would also remove the 295 ms approval. The project rule is
  exact approvals, so that is an operator decision and not part of v1.

## Check left behind

`scripts/research/v4_in_swap_unwrap_proof.py --selftest` pins what each control removes, and the
live run exits 1 on any mismatch. The build adds pytest cases on the calldata command bytes for
each branch, on value 0 with no pre-transaction, and on the fallback shape. Each gets a mutant.
