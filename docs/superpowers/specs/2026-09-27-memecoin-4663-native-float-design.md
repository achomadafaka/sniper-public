# 4663 native-ETH float: skip the WETH unwrap on native-routed entries (design + rule 5)

## Amendment, 2026-10-19: BSC probe starts beside the early quote

For an enabled first BSC shared entry with an early quote and a valid reservation,
start the existing exact native/gas/book probe before joining that quote. The first
float decision consumes that prepared instance once. A subsequent quote, approval or
signing discards unused preparation; later probes retain their existing fresh-read path.
Other chains, LiFi, disabled float policy and entries without an early quote keep their
existing timing.

The probe remains nonblocking and subject to the existing one-second expiry, gas memo
and full-book reserve. Pending, failed, missing, zero, stale or insufficient evidence
keeps the unwrap. No gate-balance float is converted into atomic funding evidence, and
no new synchronous read, wait, retry or policy change is added. Earlier start does not
prove a live latency gain. Evidence and runnable check:
[bound incident audit](../../audits/2026-10-19-bsc-native-float-preparation.md).

## Amendment 2026-10-13: BSC maintenance readiness

The operator's `c2492ce4` incident repair extends the BSC maintenance surface.
The prior BSC overlay kept low-water + float and dropped the base gas target's
headroom. It now adds the unchanged configured float above the full base target.
An originally valid enabled refill mark retains at least base low + float;
zero or originally invalid marks remain disabled. Original liquidity caps and
all wallet/claim/nonce/receipt safeguards remain intact. This can retain the
existing gas headroom in native instead of WBNB; total bankroll does not increase.
No entry call, lock, wait or retry is added. Larger clips, gas spikes and unavailable
maintenance can still fall back. See [bound incident and runnable checks](../../audits/2026-10-13-bsc-c2492ce4-float.md).

Status: executor skip BUILT (1b95ff7c, MEMECOIN_EVM_NATIVE_FLOAT_CHAINS, default empty). Design point 1
is SUPERSEDED by Option B below (2026-09-28, 9b0e8ced, inert until set). Operator order 2026-09-27
(via Alpha): "fill on the same slot or reliably fast, any chain". Author: Lambda.

## What the measurement says

Funded 4663 fills since 2026-09-13, first attempt per origin (n=112; total p50 5.35 s / p90 9.35 s):

- 84 of 112 fills (every `uniswap_v4`, `uniswap_v4_multihop`, `pons_v2` and `launchpad_curve` route)
  pay `evm.pre_transaction_pipeline` p50 668 ms / p90 1.32 s before the swap can be sent.
- That pre-transaction is `WETH.withdraw(amount)` (selector 0x2e1a7d4d). The V4, pons and launchpad
  builders attach it to EVERY WETH-quoted buy (evm_v4_quote.py:1584, 1635; the hooked-pool branch
  too), whatever native balance the wallet holds, because the bankroll is WETH and these pools are
  paid in native. The executor must see the unwrap CONFIRMED before the swap can spend its ETH
  (evm_real_seams.py `pipeline_native_pre_transaction`: a pending unwrap's ETH is not spendable by a
  nonce+1 swap, attempt 7204b30c), which is why it costs a full receipt wait.
- The other 28 fills (relay, kyber) pay a pipelined exact approval instead (p50 296 ms).
- Keeping MORE WETH pre-wrapped does not help. WETH is the cause.

Sizing facts: clip p50 0.0126 ETH, p90 0.0354, max 0.0558 (ETH/USD 2,688). 4663 fills are sparse:
at most 2 within 60 s, 3 within 15 min; median gap 52 min.

## Design

1. Float, maintained by the existing gas top-up (no new code). `memecoin_evm_gas_topup` already unwraps
   WETH to native when native < MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE (now 0.002) up to _TARGET_NATIVE
   (now 0.003), and its reclaim pass wraps native above the target back to WETH. It runs every 15 s
   and holds the same wallet lock (`evm-<chain>-<addr>`) as funded entries and exits. A float of two
   max clips plus gas is TARGET ~0.12, LOW ~0.07 (operator .env).
2. Executor, 4663 only, behind MEMECOIN_EVM_NATIVE_FLOAT_CHAINS (default empty):
   after the quote returns and under the wallet lock, read the native balance concurrently with the
   existing `evm.balance_pre` reads. If the quote's ONLY pre-transaction is an unwrap of `amount` to
   the chain WETH, and native >= amount + gas_reserve, drop that pre-transaction. The swap
   transaction is byte-identical: same route, same calldata, same minBuyAmount, same `value`.
   `gas_reserve` = swap gas limit x gas price x 3 + MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE, so an exit
   still has gas after the entry.
3. Accounting: when the unwrap was dropped, `spent` = tx `value` x the same quote_asset_usd snapshot,
   written with quantity_source 'native_value'. Today the WETH balance delta would read 0 and fall to
   the receipt path (`receipt_spent_fn`, evm_swap_executor.py:1150), which already books a native-in
   entry from `value`; the design makes that explicit instead of relying on the fallback.
4. Exits unchanged: V4 sells wrap proceeds to WETH, so the float refills only through the top-up.

## Rule 5

1. Money path: YES, 4663 entry execution (which asset pays). Price is unchanged: the signed swap is
   byte-identical; only the unwrap before it is skipped.
2. Speed: removes one signed transaction and its receipt wait (p50 668 ms / p90 1.32 s) on 75% of
   4663 fills. Adds one native balance read, concurrent with the existing balance_pre read (p50 50 ms),
   so ~0 added wall. The top-up's own unwraps happen off the critical path, between entries.
3. Concurrency: the top-up, the reclaim, entries and exits all take `evm-<chain>-<addr>`; the native
   read and the drop decision happen under that lock, so the float cannot move mid-entry. No module
   globals.
4. Absent / zero / unknown: an unreadable native balance, a zero, or a read below amount + reserve all
   KEEP the unwrap (today's path). A phantom-high read (the top-up saw 13 on 2026-09-05; it now needs
   3 agreeing reads) would make the node refuse the swap for insufficient funds: not mined, nothing
   spent. The design falls back INSIDE the same entry: on that refusal, re-attach the unwrap and send
   once more (attempt 2). Lost time, not money. Not yet verified: which refusal class the 4663 nodes'
   message maps to in the submission transport; the build must pin it with a real node message.
5. Failure modes: send refused (insufficient funds): in-entry fallback as above; swap reverted
   (unchanged path); the
   top-up blocked (MIN_WETH_RESERVE, daily cap) leaves the float short, so entries fall back to the
   unwrap per entry; nothing fails closed that works today.
6. Units: wei throughout; USD via the quote's own quote_asset_usd.
7. Sample: 84 native-routed fills of 112; after the change, compare evm.pre_transaction_pipeline
   presence and total_ms on organic 4663 fills.
8. Rollback: empty MEMECOIN_EVM_NATIVE_FLOAT_CHAINS (every entry unwraps again). Setting the top-up
   TARGET back to 0.003 makes the reclaim pass wrap the float back into WETH by itself. Half-applied
   state (flag on, float not yet funded) is today's behaviour per entry.

## Option B: a refill mark separate from LOW (2026-09-28, supersedes design point 1)

Design point 1 does not hold. The float check (`evm_native_float.float_covers`) keeps
GAS_TOPUP_LOW as the native it must leave behind, and the gas pass refills only below that SAME LOW
while its reclaim wraps everything above TARGET back to WETH. LOW therefore does two jobs:
- raising LOW to ~0.07 as proposed makes every float use need clip + 0.07 ETH, i.e. about two clips;
- keeping LOW at 0.002 means native is refilled only after gas burn takes it under 0.002. After one
  or two float uses native sits between LOW and one clip, nothing refills it, and the entries unwrap
  as before with up to one clip (about $170) idle in native.

The knob. MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE (default 0 = off). On a chain in
MEMECOIN_EVM_NATIVE_FLOAT_CHAINS the gas pass (scripts/memecoin_runner_loop.run_evm_gas_topup_pass)
uses it as its low-water mark and refills native up to TARGET below it; float_covers still leaves LOW
behind. It is CLAMPED: outside LOW < mark < TARGET it is ignored and the pass keeps LOW. At or above
TARGET the planner would refuse every top-up (`target_not_above_low_water`) and starve exits of gas.

Numbers (ETH $2,650.82 and 4663 gasPrice 0.0202 gwei at 2026-09-28T07:01Z; clips
MEMECOIN_EVM_TIER_CLIP_USD_4663=100,100,170):
- max clip $170 = 0.0641 ETH, 0.0713 at ETH -10%; 3-swap gas reserve ~0.00014 ETH;
- one max-clip float use needs ~0.0662 ETH native (0.0735 at ETH -10%);
- .env: MEMECOIN_EVM_NATIVE_FLOAT_CHAINS=4663, MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE=0.145,
  MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE=0.0735 (LOW stays 0.002);
- cycle with $170 clips: 0.145 -> 0.081 (still covers one) -> 0.017 -> refilled to 0.145 within one
  15 s pass; with $100 clips: 0.145 -> 0.107 -> 0.070 -> refilled. After a refill one max clip is
  always covered. Up to 0.145 ETH (~$384) sits in native, outside the WETH balance the clip and
  bankroll are sized from.

The wrap script (scripts/memecoin_robinhood_wrap_excess_gas.py) stays consistent without a flag: on
4663 its default --reserve-native is the top-up TARGET and it refuses any reserve below it.

Lock cost (measured in tests/test_memecoin_evm_native_float_refill.py): the gas pass holds the wallet
lock only for its in-lock reads (2 s deadline each), the nonce, sign and send; the receipt is read
after release. An entry arriving inside that window waited 0.301-0.302 s behind a 0.30 s in-lock
read, i.e. the reads plus ~2 ms. The live in-lock read time is not measured.

Rollback: unset MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE (the pass returns to LOW), and for the
whole float also empty MEMECOIN_EVM_NATIVE_FLOAT_CHAINS and set TARGET back to 0.003; the reclaim pass
then wraps the float back into WETH by itself. The runner reads env at process start, so each change
needs a restart.

## Decisions for the operator

- Float size: up to 0.145 ETH (~$384 at $2,650) held native instead of WETH (Option B above). That
  capital leaves the WETH balance the 4663 clip is sized from, so while it sits in the float the
  sizing base is up to ~$384 lower. Phase 2 could
  count native above the gas reserve as spendable; not in this change.
- Base 8453 has the same V4 native pools (1 funded fill in 14 d); the flag is per chain.
- BSC gains nothing: its winning routes (V2, 38 of 43) spend WBNB with an approval, not an unwrap.

## Not handled

- A burst of more than two native-routed entries inside one top-up cycle (15 s plus an unwrap): the
  third unwraps as today. Measured maximum is 2 fills within 60 s.
- Relay and kyber routes (25% of fills) keep their approval.

## Amendment, 2026-10-02

The guarded excess-native CLI now also supports operator-authorized Base wrapping using the existing gas/concurrency reserve calculation. This does not enable a Base float or recurring rebalance. --state-root binds receipt, key and generation paths in the standalone CLI process. Robinhood sizing still uses its existing denominator; this change does not raise its float target.

## Amendment, 2026-10-09: BSC shared curve execution

The earlier statement that BSC gains nothing is superseded for native-paid curve routes.
Position 72612 used Flap Portal and spent 1,020.204 ms unwrapping WBNB and proving native
visibility despite the BSC lane maintaining a usable BNB float. Its native receipt lane
correctly declined `post:no_venue_leg`; the shared watcher/executor handled the curve.

Adding `56` to `MEMECOIN_EVM_NATIVE_FLOAT_CHAINS` permits the shared executor to use that
existing float. BSC consumes only completed, under-one-second-old native/book evidence and
a gas-price memo still inside its existing one-second lifetime. The balance worker warms
that memo and reads `required_entry_native` through its own short-lived read-only connection
bound to the executor's ledger file, with `busy_timeout=0`. Unknown ledger binding retains
the unwrap. Neither an optional balance read, gas RPC nor a book read is waited
on by BSC's entry path. Unknown, failed, pending or stale evidence retains the full unwrap.

The retained native covers the greater of the configured book floor, the actual open plus
pending book's reserve, and the existing three-swap gas reserve plus LOW. Manual bags and
aged holds remain in the gas denominator even when automatic slot counting excludes them.
Other chains retain their existing float wait and partial-native behavior. Sizing, calldata,
minimum output, safety, signing, accounting and ambiguous-send handling stay unchanged.

Activation follows a code-only pinned deployment, then changes the existing per-chain flag
and restarts the same verified release. This avoids starting an older rollback release with
the new BSC flag. Rollback removes `56` and restarts the pinned release. No float target,
refill policy or native Flap receipt-lane support is added. See
`docs/audits/2026-10-09-bsc-curve-native-float-speed.md` for bound incident evidence and tests.


## Amendment 2026-10-11: Base latency repair

Operator-authorized Base activation uses the existing balance-bound native float and gas maintenance: low0.025ETH, target0.030ETH, WETH reserve0.005ETH, daily unwrap cap0.10ETH. Existing liquid funds are reclassified; bankroll and clip limits do not increase. Unknown balances retain the existing unwrap path. A Base entry that actually confirms an unwrap refreshes an aged winning quote before swap signing, reruns existing price/copy/simulation checks and preserves the existing missing/mismatched-refresh fallback. No refresh is added to entries already covered by native float. Historical fill success and organic latency gains are unproved.

Contract: `docs/superpowers/plans/2026-10-11-base-latency-repair.md`.
