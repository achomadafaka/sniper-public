# 4663: approve the token for Permit2 at entry, so the exit does not wait for it

Beta, 2026-09-30T01:19Z. **PROPOSED: money path. Operator decision required before any build.**
Requested by Alpha (overnight 2026-09-29/30) after the 4663 exit-cost read
(`docs/audits/2026-09-30-4663-exit-route-cost.md`).

## Why

On 4663 a sell cannot land before its token is approved for the exit route, and today that
approval runs *inside* the exit.

Closed 4663 exits, 2026-09-13 to 2026-09-29T22:00Z, from `memecoin_exit_execution_attempts` and
`memecoin_positions.exit_timing_json`:

| exits entered on a V4 route | n | pre-broadcast p50 / p90 (ms) | exit total p50 / p90 (ms) | approve stage p50 / p90 (ms) |
|---|---|---|---|---|
| approval inside the exit | 70 | 849 / 1,372 | 1,030 / 2,173 | 310 / 419 |
| no approval needed | 16 | 540 / 1,000 | 694 / 1,439 | none |

The approval costs about **300 ms at p50** before the sell can broadcast, on 81% of V4 exits. That is
the window in which a take-profit's price falls away: in the exit-cost read, all 13 worst exits ran
the approval inside the exit.

Exit approvals are already pipelined (`evm_real_seams._approve`: the swap signs at approval nonce+1
without waiting for the receipt). So the saving is the approve stage itself (nonce read, gas read,
sign, send), not a receipt wait.

**The spender is known at entry.** Every V4 exit on 4663 approves the canonical **Permit2** contract
(`evm_v4_quote`: `issues.allowance.spender = V4["4663"]["permit2"]`). The Universal Router then pulls
through a Permit2 signature carried inside the swap, not through a second transaction. Decoding the
signed exit payloads (`exit_intent_json.signed_payload_b64`): **88 of 88** positions entered on a V4
route exited through the Universal Router `0x8876789976decbfcbbbe364623c63652db8c0904`. The 38
non-V4 entries (Kyber, Relay, Pons, launchpad) mostly exited through other spenders, and stay out of
scope.

## Design

1. **Trigger.** Right after a 4663 entry is booked (after the atomic booking commit in
   `sniper/execution/evm_swap_executor.py`, next to the post-commit executed-pool read), start a
   daemon thread when all of these hold:
   - `network_id` is in `MEMECOIN_EVM_PREAPPROVE_CHAINS` (new, default empty, so OFF);
   - the entry route's `protocol` starts with `uniswap_v4`;
   - the position has no close claim.

   The entry never joins the thread. Its return value, trace and timings are byte-identical with the
   switch on or off.
2. **Amount: exact, by default.**
   - Read `balanceOf(wallet, token)` after the fill, not the quoted quantity, because fee-on-transfer
     tokens deliver less.
   - Read `allowance(token, Permit2)`. If it already covers the balance, send nothing (16 of 86 V4
     exits needed no approval today).
   - Otherwise call `approve(Permit2, balance)`.

   The trade-off against MAX (`2**256-1`):
   - Exact caps the allowance at what we hold, matching the existing doctrine ("an EXACT-AMOUNT
     approve", `evm_exit_executor.py` header).
   - MAX avoids a shortfall if the balance grows (airdrop or reflection tokens). It is the standard
     Uniswap Permit2 practice, and the exposure is limited to this token's balance, since Permit2
     moves nothing without our signed permit. It still widens what a Permit2 or signing bug could
     move.
   - A shortfall under exact costs nothing new: the exit re-approves, which is exactly today's path.
     So exact is the recommendation.
3. **Send.** Through the **same executor instance** the exits on that chain use, so it shares that
   instance's `_nonce_state` floor and lock. It calls the existing
   `_approve(Permit2, amount, token=mint)`. A second executor instance would carry a second nonce
   floor and could collide with an exit.
   - **Positive acceptance** (a tx hash or `already_known`): done. The thread then waits for the
     receipt, only to record the outcome and the gas.
   - **Proven rejection** (`False`): `_release_floored_nonce(nonce)`, record it, no retry.
   - **Ambiguous** (`None`): **clear the chain's nonce floor** (a new `_clear_floor()` helper), so the
     next signer reads the node's pending count instead of trusting nonce+1.
     - If the approval did reach a node, the pending count is N+1 and the exit queues correctly
       behind it.
     - If it did not, the exit takes N and the late approval, if it ever lands, is rejected as
       nonce-too-low.
     - Either way, no exit swap is ever signed over a gap the approval might not fill. That gap is
       the review P0 hazard recorded in `_approve`.
     - Rebroadcast only the identical raw bytes, at 2 s and 5 s, then stop and record `ambiguous`.
4. **Exit: no change.** `execute_exit_evm` already reads `allowance(token, spender)` at `latest` and
   approves only when it is short of `sell_atomic`. A failed read already means "approve".
   - A mined pre-approval gives `approve_n=0`, and the sell broadcasts about 300 ms sooner.
   - A pre-approval still pending at exit time is not visible at `latest`, so the exit approves as
     today (a duplicate approval: its gas is wasted, and it is safe).
   - An absent or failed pre-approval is exactly today's path.
5. **Record.** Each attempt gets one row in a proposed table
   <!-- schema:external=memecoin_evm_preapprovals -->`memecoin_evm_preapprovals`:
   `position_id, network_id, token, spender, amount_atomic, nonce, tx_hash, send_status (accepted |
   rejected | ambiguous | skipped_allowance | skipped_route | skipped_closing), receipt_status,
   gas_used, effective_gas_price_wei, created_at, resolved_at`. It is written by the thread on its own
   short-lived connection, never the entry's. This is also the first per-approval gas record on
   4663.

## Gas against latency

- **Count.** A pre-approval moves the approval, it does not add one:
  - 70 of 86 V4 exits approved anyway;
  - the other 16 already had allowance, and the allowance check skips them;
  - 0 of 88 V4 positions exited through a spender other than the Universal Router.

  Added approvals arise only for positions that never sell through the Universal Router (a write-off
  or a stuck bag: 1 write-off on 4663 in the window), and for duplicates when an exit lands before
  its pre-approval mines (1 of 123 exits came within 3 s of the fill; 4 within 10 s).
- **Cost of one approval on 4663: unmeasured.** EVM `realized_pnl` excludes gas, and there is no
  local fee table. Before the operator decides, read `gasUsed x effectiveGasPrice` for 20 recent
  4663 approvals, taken from our wallet's own transaction history. That needs a receipt read, which the current research rule for 4663 does not
  allow (`eth_call` only), so it needs Alpha's approval or the funded path's own record. Step 5
  records it going forward either way.
- **Latency.** About 309 ms at p50 off pre-broadcast on about 81% of V4 exits (849 to 540 ms at
  p50, 1,372 to 1,000 ms at p90).

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** A new funded transaction (the approval) and a change to nonce handling
   (`_clear_floor`). Every item below goes to the operator before any edit.
2. **Speed on the fill path.**
   - The entry does not wait: the thread starts after the booking commit and is never joined. The
     only added entry latency is the thread start. Measure it (p50/p90) with the switch on and
     off; do not assume it.
   - The thread's RPC reads (balance, allowance, nonce, gas price, send, receipt) share the custody
     RPC with a concurrent entry or exit on 4663. That is about 5 calls per entry. Measure the
     entry-stage and exit-stage p50/p90 for 24 h before and after.
3. **Concurrency and global state.**
   - The nonce floor is per executor instance, with a lock. The thread must use the chain's executor
     instance, never build its own.
   - `_clear_floor()` mutates that shared state under its lock, only on an ambiguous pre-approval.
   - No module attribute, environment variable or process-wide resolver is touched.
   - The thread writes to its own DB connection.
4. **Absent vs zero vs unknown.**
   - An allowance read that fails is unknown: skip the pre-approval, and the exit approves. It is
     never read as 0 and approved blindly, and never read as enough.
   - A balance read that fails means no pre-approval.
   - `receipt_status` None means unresolved, which is different from `failed`.
5. **Dependency failures.**
   - An RPC timeout or 429 on any read skips the pre-approval, with a recorded status.
   - Proven rejection releases the nonce. Ambiguity clears the floor (step 3).
   - A token that reverts `approve` (a pausable token, or a non-standard return) is recorded, and
     the exit re-approves or fails as today.
   - A Permit2 address mismatch against `V4["4663"]["permit2"]` sends nothing.
6. **Units and identity.**
   - The amount is token atoms from `balanceOf`.
   - The spender is the chain's Permit2 from `V4`, never a quote-supplied address.
   - Identity is `(network_id, mint)`; chain `4663` is compared as a string.
7. **The sample.**
   - Before/after compares our own exits. The 09-29 roster cut changes the wallet mix at the same
     time, so compare within V4 exits and by exit reason.
   - The existing "no approval needed" group (16 exits) is a natural control for the pre-broadcast
     time.
8. **Rollback.**
   - `MEMECOIN_EVM_PREAPPROVE_CHAINS=` (empty) stops new pre-approvals.
   - Allowances already granted stay on chain. They are harmless: exact amount, and the balance is 0
     after the exit.
   - Half-applied (a pre-approval sent, then the runner restarts): the exit reads the on-chain
     allowance and does the right thing either way.

## Measurement (pre-registered)

- **Cohort.** 4663 positions entered on a V4 route and closed after activation, against the same
  set in the 14 days before.
- **Primary.** Exit `pre_broadcast_ms` p50/p90 from `exit_timing_json`, and the share of exits with
  `approve_n = 0` (`stage_ms_json`).
  - **Pass:** p50 down by at least 200 ms and at least 80% of V4 exits with no approval inside,
    at n>=30 exits.
- **Guards**, each reported:
  - pre-approval send statuses, with **ambiguous at most 2%**;
  - duplicate approvals (exit approved while a pre-approval was pending);
  - nonce-too-low or replacement errors on exits;
  - entry-stage p50/p90 (must not move).
- **Secondary** (descriptive, not a pass condition): the TP decision-to-fill gap on 4663, against
  the -$104.98 baseline.
- **Gas.** The sum of `gas_used x effective_gas_price` per position, with the same quantity
  estimated for the 14 days before once receipts are read.

## Checks to write (red first, when built)

1. The entry returns without waiting: a pre-approval seam that blocks forever does not delay
   `execute_entry_evm`'s return.
2. The pre-approval and a following exit share one nonce floor: the exit signs at approval
   nonce+1.
3. An ambiguous pre-approval clears the floor: the next exit reads the node's pending nonce.
4. An allowance already enough, a non-V4 route or an existing close claim sends no approval.
5. An exit whose allowance covers `sell_atomic` sends no approval (today's behaviour, pinned).
6. A spender other than `V4["4663"]["permit2"]` is refused.


## 2026-10-09 Ethereum amendment — operator requested

The operator requested Ethereum approval prewarming after CLEDGER 72737 paid a
48.267-second approval wait during manual close. Its route was Uniswap V4.
Ethereum now opts into the existing post-booking Permit2 worker through
`MEMECOIN_EVM_PREAPPROVE_CHAINS=4663,1`. It signs exact held-balance approvals
with the chain's exit tip floor (Ethereum defaults to 1 gwei).

Ethereum remains excluded from `MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS`. Protect's
receipt-first constraint remains authoritative: ordinary entry and exit approvals
still require canonical confirmation before their dependent swap. The shared nonce
floor covers the background approval and ordinary signers without enabling swap
pipelining. Only background preapproval returns after positive send acceptance;
its own thread records the receipt afterward. Proven unsigned/read/sign failures
release only the latest unused nonce. Ambiguous sends preserve the existing
conditional rewind and same-bytes recovery rules.

The entry starts the worker after booking and never joins it. No additional
provider read, receipt wait or wallet lock is added to entry processing. Existing
unknown balance/allowance, zero balance, covered allowance, close-claimed position,
non-V4 route and noncanonical spender checks remain. A sell before the approval
mines, a balance increase, a changed spender or a failed worker retains ordinary
approval behavior. No instantaneous inclusion or organic latency saving is claimed.
Rollback removes chain 1 from preapprove configuration and restores the previous
pinned release; no funded contract or position is rewritten.


## Amendment 2026-10-11: Base latency repair

Base8453 joins background V4 preapproval under its existing per-chain flag. Base swaps remain receipt-first; approval pipelining remains restricted to configured chains4663/56. Base swaps, preapproval and gas maintenance share one nonce floor. Maintenance reads pending nonce without mutation within its read budget, then claims synchronously after nonce/gas validation. Sign/journal failure and explicit rejection release only the latest allocation by CAS; ambiguous sends and later allocations retain the binding. These callbacks are absent on other chains. LI.FI remains chain4663-only. See the Base latency repair plan and real executor/context/pass regressions.

Contract: `docs/superpowers/plans/2026-10-11-base-latency-repair.md`.
