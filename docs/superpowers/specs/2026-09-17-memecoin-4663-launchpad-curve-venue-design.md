# Memecoin 4663 launchpad-curve venue (buy and sell before graduation) — design

Date: 2026-09-17. Owner: Alpha (sniper-fc). Status: native-counter is wired end to end into the
real entry/exit ladder and merged (deployed with the flag off), phase-4 dry-proven with
`eth_simulateV1` on the transactions the RUNNER itself builds and signs on a live curve, and
checked and fixed against `54ffd4ec`'s bound-identity contract (the clip check uses the executor's
actual quote amount when it is known, not a second independent estimate). 1133
`test_memecoin_evm*.py`/`test_memecoin_v4*.py` tests green. USDG and stock-counter pre-legs remain
phase 2. Do not arm `MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` without review.

## Problem

Chain 4663 has a launchpad at `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e`. Before a coin
graduates to its Uniswap V4 pool, the coin trades only on a per-coin bonding-curve contract. The
runner has no rung for this curve. It can only buy or sell a launchpad coin after graduation,
which means it misses the coin's full pre-graduation price range.

## Response to the first review (2026-09-17)

Alpha's review raised four blockers before phase 3. This revision answers each with new evidence,
gathered with `eth_call`, `eth_getLogs`, and `eth_simulateV1` (all read-only; nothing was
broadcast). The short answer to each:

1. **Atomic entry.** The curve exposes a direct, payable, single-transaction `buy` function.
   Confirmed live on 3 real transactions and one controlled simulation. The two-step entry in the
   first draft is removed. See section 3.
2. **The sell evidence was wrong, and the review was right to flag it.** The router-mediated
   transactions in the first draft were not the curve's real sell path. The curve has its own
   direct, atomic `sell` function, found by brute-force `eth_call` probing exactly as the review
   asked, confirmed on a real transaction and a controlled simulation. See section 4.
3. **Sellability is confirmed, not assumed.** A chained `eth_simulateV1` buy-then-sell, same
   block, real curve, real coin, succeeded with no revert. No cooldown was found on the direct
   path. See section 4 and section 7.
4. **Fees.** A real, chained `eth_simulateV1` round trip on $50 of a native-counter coin, same
   block, cost $0.995 (1.99%). See section 9.

### 1. Atomic entry — resolved

The curve's `buy` function is `buy(uint256 amountIn, uint256 minAmountOut, address recipient)`,
selector `0x59a87bc1`. It is payable: for a native-counter coin, the caller sends `amountIn` as
`msg.value` in the same call that credits the coin. For an ERC-20-counter coin, the same call
pulls `amountIn` via a standard `transferFrom` from the caller, which needs a prior `approve` —
the same pattern every other rung in this codebase already uses, not a new one. Both forms are
one transaction. There is no prior-transfer step and no window where a payment sits in the curve
unclaimed. This is confirmed on 3 real chain transactions and one controlled `eth_simulateV1` call
(section 3). The first draft's "pay first, then call" reading was a misread of one relay-routed
example; the curve's own direct interface does not work that way.

Because a fully atomic direct path exists, question 1(b) (an atomic router path) and question
1(c) (an EIP-7702 batch) do not need an answer — there is nothing to batch. For completeness: this
codebase's own execution layer (`sniper/execution/evm_wallet.py`, `evm_v4_multihop_tx.py`) has no
`authorizationList`/EIP-7702 signing support today. The EIP-7702-delegated wallets referenced in
this project's memory are wallets the runner *watches* (other traders' relayed wallets on this
chain), not our own custody wallet. Building EIP-7702 support for our own signer would be new,
unscoped work; it is moot for this design.

### 2 and 3. The sell evidence — corrected, and sellability confirmed

The review's suspicion was correct. Investigating it found the real mechanism:

The curve also has a direct `sell` function: `sell(uint256 amountIn, uint256 minAmountOut, address
recipient)`, selector `0xd04c6983`. Found by brute-force `eth_call`, per the review's instruction:
every one of the curve's ~50 still-unmatched selectors (from the phase-1 byte scan) was called
inside an `eth_simulateV1` bundle, on a real curve, after a real simulated buy funded a coin
balance and a real `approve` gave the curve an allowance. `0xd04c6983` was the only one that pulled
coin from the caller and returned a nonzero, coherent result. Its keccak hash matches the literal
signature `sell(uint256,uint256,address)` — this is not a guessed name, the hash matches exactly.

In the same transaction: `sell` pulls `amountIn` coin from the caller via `transferFrom` (a real
ERC-20 `Transfer` log, caller to curve) and pays `recipient` in the counter currency in the same
call (native currency for a native-counter coin, so no `Transfer` log for that leg — confirmed by
the function's own return value, which is the payout amount, and by the curve's own event). One
transaction, no separate step, no theft window — the same atomicity property as `buy`.

Confirmed live on a real transaction found by scanning chain history for this function's own
event (`0x8113d738…f59df`, 6,640 occurrences across the same 50,000-block window used elsewhere in
this document — far more common than the earlier, wrongly-identified "sell" count). Decoded
transaction `0xdda5a0c9…86ade`: `tx.to` is the curve itself, selector `0xd04c6983`, `word0` =
amount sold (matches the event), `word1` = the caller's own minimum-out, `word2` = the caller's
own address (matches `tx.from`).

**The router path in the first draft (`0xe33e9e479df8802cb0866d5d05258bec4cf62948`, selector
`0xf85f8e41`) is a separate mechanism, not confirmed atomic, and not used by this design.** It is
not the curve's own sell entry — it is some other flow (an intent fill, a bridge, or an aggregator;
which one is not established here) that happens to also move the coin into the curve address and
also emit a curve trade event. This document does not rely on it and does not recommend building
against it. It is left unexplained rather than mis-explained.

**Sellability is proven, not assumed.** A chained `eth_simulateV1` call — one real curve, one real
coin, a state override giving a throwaway account native balance, `buy` then `approve` then `sell`,
all in the same simulated block — completed with all three calls returning success. No cooldown,
no time-gate, and no revert appeared on the direct path. See section 7 for the exact numbers.

### 4. Fees — resolved with a real number

A real, chained `eth_simulateV1` round trip (buy $50 of native-counter coin, immediately sell back
the exact coin amount received, same block, same curve) cost $0.995, 1.99% of notional, from curve
fees alone. See section 9 for the full working and the USDG/META cases.

## Evidence — phase 1

All evidence below comes from live `eth_call` reads and real transaction receipts on chain 4663,
read through `MEMECOIN_EVM_RPC_URL_4663`. No claim in this section is a guess from memory. Where a
value is read but its purpose is not confirmed, this document says so.

### 1. The launchpad's per-coin info call

`eth_call(launchpad, 0x3cf28b5a + pad32(coin))` returns 15 words. Confirmed by direct calls on
several coins, including a coin still on its curve and a coin already graduated:

| Word | Meaning | Confirmed by |
|---|---|---|
| 0 | the coin address | matches the query argument on every call |
| 1 | the coin's curve contract address | matches the curve address used to fetch every trade event in this document |
| 2 | an address, role not confirmed | present on every call; not yet exercised |
| 3 | an address, role not confirmed | appears again inside a hook event fired at graduation (word 3 of the hook's own event data); may be a locker or fee-recipient reference, not confirmed |
| 4 | the counter currency (the token the coin trades against); `0x0…0` means native | confirmed against `getReserves()`-style curve reads and against real trade transfers |
| 5 | a large number, role not confirmed | present on every call; not yet exercised |
| 6 | 0 on every coin checked | — |
| 7 | 200 (tick spacing) | matches the tick spacing read directly from the graduation transaction's pool-initialize event |
| 8 | 100 (fee, basis points) | matches the curve's own `feeBps()` live read (see below) exactly |
| 9 | 0 on every coin checked | — |
| 10 | the coin's state: 0 = still on its curve, 2 = graduated | confirmed by reading a coin before and after its own graduation transaction |
| 14 | 1 on every coin checked | — |

The hook address comes from a separate call, `eth_call(launchpad, 0x6651812c)`, with no argument.
It returned `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` on every coin checked. This is the same
hook the existing V4 launchpad-derived-pool-key design already targets
(`docs/superpowers/specs/2026-09-05-memecoin-v4-launchpad-derived-discovery-design.md`). Word 6
(fee) and word 7 (tick spacing) from this call match that design's own registry entry for this
launchpad (`0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:3cf28b5a:0:4:6:7:6651812c`). This rung does
not need a new pool-key derivation; it must reuse the existing one after graduation.

### 2. The curve contract's own functions

A byte scan of the curve's bytecode (10,229 bytes; contract `0x18528e6f021a867d7cfaea5d7873d2c89b539352`,
one working example) found 76 candidate function selectors. Matching each against the keccak hash
of about 106 candidate signatures (standard ERC-20 names, common bonding-curve names, common view
names) confirmed 9 names. Three of the confirmed ones were also read live and returned a value
consistent with other evidence in this document:

| Selector | Name | Live read (example curve) | Cross-check |
|---|---|---|---|
| `0x18160ddd` | `totalSupply()` | not read live | — |
| `0x70a08231` | `balanceOf(address)` | curve's own coin balance after graduation = 0 | see graduation section |
| `0x095ea7b3` | `approve(address,uint256)` | not read live | — |
| `0x0902f1ac` | `getReserves()` | not read live | Uniswap-V2-style signature; the curve looks like a constant-product pool against the counter currency |
| `0xff6d8d05` | `graduate(address)` | not read live | name match only |
| `0x8b0bc501` | `graduationThreshold()` | 4,200,000,000,000,000,000,000 (raw units) | one curve, one read; not cross-checked against a second curve |
| `0x24a9d853` | `feeBps()` | 100 | matches word 8 of the launchpad info call, and matches the fee fraction measured in every trade event in this document |
| `0xc45a0155` | `factory()` | `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e` | matches the launchpad address |
| `0xfc0c546a` | `token()` | the coin address | used throughout this investigation to map a curve to its coin |
| `0x59a87bc1` | `buy(uint256,uint256,address)` | see section 3 | keccak match, confirmed live |
| `0xd04c6983` | `sell(uint256,uint256,address)` | see section 4 | keccak match, confirmed live |

`debug_traceTransaction` is not available on this RPC endpoint (returns `-32601`). `eth_simulateV1`
**is** available and is the primary tool for sections 3, 4, and 9 below: it runs a sequence of
calls with a chosen `msg.sender`, an optional native-balance override, and no broadcast, and
reports each call's status, return data, and logs. Nothing in this section was broadcast to the
real chain.

### 3. The buy function — confirmed, atomic, payable

`buy(uint256 amountIn, uint256 minAmountOut, address recipient)`, selector `0x59a87bc1`. Found by
first classifying every real transaction behind the curve's trade events by its `tx.to`: of 121
sampled transactions across 59 curves, 34 called the curve directly (`tx.to == curve`), and every
one of those 34 was on a native-counter coin (`info` word 4 = `0x0…0`).

Decoded 3 of those calls directly (no byte-offset guessing needed — this is the transaction's own
top-level input):

| Example | `amountIn` (word 0) | matches `msg.value`? | `minAmountOut` (word 1) | `recipient` (word 2) | matches `tx.from`? |
|---|---|---|---|---|---|
| `0x133ca80e…5cab76` | 1,722,062,479,840,703 | yes, exactly | 970,000,000,000,000,000,000,000 | `0x9e2c20aa…c38807` | yes |
| `0x3c305340…6d350d` | 201,033,081,548,721,150 | yes, exactly | 83,753,974,482,387,697,585,974,855 | `0x63dc0558…846560` | yes |
| `0xe15464e4…ba21c6` | 60,000,000 | `value` = 0; pulled instead via a real ERC-20 `Transfer` (caller to curve) of the same 60,000,000 | 12,741,197,004,658,186,181,389,609 | `0xb79716d9…75181a` | yes |

The third example is an ERC-20-counter coin (a stablecoin, not native) — `amountIn` is pulled via
`transferFrom` instead of `msg.value`, confirming the buy function is atomic for both counter-currency
shapes.

`minAmountOut` is a real slippage floor, not a placeholder: in all 3 examples the coin actually
received (read from the curve's own trade event, word 1) is 3%–18% above the claimed minimum,
never below it.

Cross-checked with an `eth_simulateV1` call on a fourth, fresh curve: `buy` with a chosen
`amountIn` as `msg.value`, `minAmountOut = 0`, `recipient` = a throwaway account, succeeded and
returned the exact coin amount credited as its return value.

The first draft of this document decoded a *different*, relay-routed buy (a call embedded inside a
router's own event data, `amountIn = 0`) and concluded the curve was paid before being called. That
reading does not apply to the direct interface above, which is what this design uses: `amountIn = 0`
in the relay example most likely tells the curve "use whatever you were already sent by an earlier
step of that specific route," a convenience for a multi-hop router, not the curve's normal calling
convention. This design calls `buy` directly, in one transaction we build, with a real `amountIn`.

### 4. The sell function — confirmed, atomic, symmetric with buy

`sell(uint256 amountIn, uint256 minAmountOut, address recipient)`, selector `0xd04c6983`. The
first draft of this document treated a different, router-routed transaction pattern as "the sell
function." That was wrong, and section 2 of Alpha's review was right to question it. The real
mechanism was found by following the review's own instruction: brute-force every still-unmatched
selector from the phase-1 byte scan with a read-only `eth_call`/`eth_simulateV1`, after a real
simulated `buy` gave a throwaway account a real coin balance and a real `approve` gave the curve an
allowance over it.

Of ~50 candidate selectors tried this way, only `0xd04c6983` pulled coin from the caller and
produced a coherent, nonzero result. Its keccak hash equals `sell(uint256,uint256,address)`
exactly — a hash match, not a guessed name.

Confirmed on a real transaction, found by searching chain history for this function's own trade
event rather than for the wrongly-identified one: transaction `0xdda5a0c9…86ade`, `tx.to` is the
curve itself (`0x65b96125…73c2e5`), selector `0xd04c6983`, `word0` (amount sold) matches the
event's own word 0, `word2` (recipient) matches `tx.from` exactly.

In the same call: `sell` pulls `amountIn` coin from the caller via a real ERC-20 `Transfer` (caller
to curve — confirmed in the receipt logs of both the real transaction and the simulation), then
pays `recipient` the counter currency in the same transaction. For a native-counter coin this is a
plain native transfer (no `Transfer` log for that leg by construction — confirmed instead by the
function's own return value, which is the payout amount, and by the curve's own event, below).
This is one transaction: the coin leaves the seller and the payout arrives in the same call, the
same atomicity property `buy` has, and the same reason a two-step design is not needed here either.

The router-mediated pattern in the first draft (`0xe33e9e479df8802cb0866d5d05258bec4cf62948`,
selector `0xf85f8e41`) is left as an open, unexplained, separate mechanism — see the response to
the review above. This design does not use it.

### 5. The trade events

Buy and sell fire two *different* events on the curve, confirmed by cause and effect in the
`eth_simulateV1` tests above (calling `buy` fired one signature every time; calling `sell` fired
the other every time — this is controlled evidence, not inference from chain history):

- Buy: `0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455`.
- Sell: `0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df`.

Both carry 2 indexed topics and 4 data words in the same shape: topic 2 is the trader's own
address (confirmed against `tx.from` on every real example in this document); word 0 is the amount
the curve received (buy) or the amount sold (sell); word 1 is the amount the curve paid out (coin,
on a buy; counter currency, on a sell); word 2 is exactly 1.00% of word 1 in every example checked,
matching the curve's own live `feeBps()` read (100 basis points) — this is the confirmed platform
fee, taken on the *paid-out* side of the trade on both a buy and a sell. Word 3 sometimes equals
word 2 (another apparent 1%) and sometimes reads 0; a referral or affiliate fee that is not always
active is a plausible explanation, not a confirmed one. Topic 1 is an address whose exact role is
not confirmed — it equals the trader's own address on some examples and a different address (seen
on the router-mediated transactions this design does not use) on others.

### 6 and 7. Sell frequency and sell restriction — corrected

The first draft searched only the buy event (`0xec36bf57`) across the last 50,000 blocks (507
events) and mis-read a handful of them as sells by inspecting Transfer direction, finding 3 and
concluding sells were rare (about 1.3%). That was the wrong event to search. Searching the real
sell event (`0x8113d738…`, section 5) across the identical 50,000-block window returns 6,640
occurrences — far more than the (mis-measured) buy count, not fewer. Selling on this launchpad is
common, not rare; the first draft's "sell frequency" and "sell restriction, open question" claims
are both retracted.

No cooldown exists on the direct path. Proven, not inferred: a single chained `eth_simulateV1`
call — one real curve, one real coin, a throwaway account funded only by a state override, `buy`
then `approve` then `sell`, all three calls in the same simulated block — completed with every call
returning success (`status = 0x1`), no revert, no delay. The exact numbers are in section 9. Phase
4's live dry proof should still be the final word (a simulation is not a broadcast), but nothing in
read-only evidence points to a time-gate on this path.

### 8. Graduation (state 0 to 2)

Real example: coin `0x7a77fff7fef331b4369b79c02baa180d63a317a6`, curve
`0x18528e6f021a867d7cfaea5d7873d2c89b539352`, graduation transaction
`0xff2058ce459a3e141b4450bd99e768360f34f0185449fb1d706a04300d4a39e9`.

Confirmed by live reads taken after this transaction:
- The launchpad's info call now returns word 10 = 2 for this coin.
- The curve's own coin balance is 0.
- The curve's own native balance is 0.
- The curve's bytecode is unchanged (still 10,229 bytes) — the contract still exists, it is only
  drained.

Confirmed by reading the transaction's own logs:
- The launchpad moves the coin and the counter currency out of the curve.
- A Uniswap V4 pool is initialized on a pool-manager contract
  (`0x8366a39cc670b4001a1121b8f6a443a643e40951`), with tick spacing 200 and hook
  `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` — the same hook the existing V4
  launchpad-derived-pool-key design already targets.
- Coin and counter-currency approvals route through the standard Permit2 contract
  (`0x000000000022D473030F116dDEE9F6B43aC78BA3`).
- An LP position is minted, through a position-manager-style contract
  (`0x58daec3116aae6d93017baaea7749052e8a04fa7`), to a fixed recipient address
  (`0x267444d099b10fb5ed7c3cc7b7c767adca574952`). This address received coin and counter-currency
  transfers earlier in the same transaction and looks like a treasury or locker used for every
  graduated coin, not something specific to this one coin. This is not confirmed against a second
  graduation.
- No transfer in this transaction touches any individual holder's wallet. A holder's own coin
  balance is unaffected by graduation.

Consequence for this rung: because the curve's own reserves are 0 immediately after graduation, a
curve-sell attempted after that point cannot pay out. The exit design (below) must re-check word
10 immediately before every sell, not rely on a value read earlier.

### 9. Fee round trip at $50

A live chain read (`sniper/execution/evm_wallet.py:native_usd_price`) put chain 4663's native unit
at $2,439.39 at the time of this test. $50 is 20,496,927,510,566,168 wei.

Chained `eth_simulateV1`, one real native-counter curve
(`0xd1255489d894452dd234a001f0fbcab6d4037040`), one throwaway account, same simulated block:

1. `buy(20496927510566168, 0, recipient)` with `msg.value` set to the same amount: succeeded,
   credited 11,795,799,327,068,400,332,257,214 coin units.
2. `approve(curve, max)` on the coin: succeeded.
3. `sell(11795799327068400332257214, 0, recipient)`: succeeded, paid out 20,089,038,653,105,901
   wei native.

$50.0000 spent, $49.0050 recovered, cost $0.9950 — **1.99% round trip**, with zero price movement
(both legs in the same simulated block) and no other market activity. This isolates the curve fee:
2 legs at ~1.00% each (the confirmed platform fee from section 5), compounding down slightly from
a flat 2%. This matches feeBps() = 100 exactly and needed no assumption about the unconfirmed
word-3 fee, because it did not fire in this test (no referral code used).

For a worst-case budget, assume word 3 fires on both legs (another 1% each way): up to 4% round
trip from curve fees alone on a native or USDG counter coin, before price impact and before this
codebase's own gas cost. This is the number this rung's round-trip budget should use until word 3
is explained.

For a stock-counter coin (for example the token this document calls META): this design pays the
curve's own fee above on both legs, **plus** the existing cash↔counter-currency rung's own swap
fee, twice — once acquiring the counter currency before the curve buy, once converting the sell
payout back to cash. That second cost is not re-measured here: it is the existing multihop rung's
already-confirmed fee, inherited unchanged, not something this design adds. Do not invent a number
for it; read it from the existing rung's own numbers before sizing a stock-counter position.

## Design — phase 2

### New rung: `launchpad_curve`, chain 4663 only

Gated behind `MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` (boolean, default off). The rung applies only
when the launchpad's info call for the mint returns word 10 = 0 (still on its curve). When word
10 is not 0, this rung does not apply; the existing V4 launchpad-derived-pool-key rung already
covers the graduated case and must not be duplicated here.

### Entry

1. Read the launchpad's info call for the mint. Confirm word 10 = 0. Read the counter currency
   from word 4.
2. If the counter currency is one the runner can already reach directly from cash (native, or the
   chain's USDG, through the rungs already in the ladder), acquire it there.
3. If the counter currency is a different token (for example the token this document calls META),
   first swap cash into that token through the existing V4 or V3 rung already used for other coins
   on this chain. This rung does not implement a new cash-to-counter-currency path; it reuses the
   existing one.
4. Call the curve's `buy(amountIn, minAmountOut, recipient)` (selector `0x59a87bc1`, section 3)
   directly, in one transaction built by our own code: `msg.value = amountIn` for a native counter
   currency, or a prior `approve` plus `amountIn` pulled via `transferFrom` for an ERC-20 counter
   currency — the same approve-then-call shape every other rung in this codebase already uses. Set
   `recipient` to our own custody wallet and `minAmountOut` from a fresh quote with a real
   slippage margin, never 0 in a funded transaction (0 was used only in this document's read-only
   simulations, to observe the true output).
5. No two-step version is needed. `buy` is atomic (section 3): the payment and the coin credit
   happen in the same transaction, so there is no state where our funds have moved but the coin
   has not, and no window where anyone else could claim a payment we sent ahead of the call.
6. Token safety, live pricing, post-peak confirmation, and balance-delta accounting apply
   unchanged, the same as every other rung.

### Exit

1. Before every sell quote or sell attempt, read the launchpad's info call for the mint again.
   Do not reuse a value read earlier in the same position's lifetime.
2. If word 10 = 0, call the curve's `sell(amountIn, minAmountOut, recipient)` directly (selector
   `0xd04c6983`, section 4), in one transaction: a prior `approve` of the curve over our coin
   balance, then `sell` with a real `minAmountOut` from a fresh quote. This is atomic in the same
   sense as the entry — the coin leaves us and the payout arrives in the same call.
3. If word 10 is not 0 (graduated), sell on the derived Uniswap V4 pool using the existing
   `derived_pool_keys()` function
   (`docs/superpowers/specs/2026-09-05-memecoin-v4-launchpad-derived-discovery-design.md`)
   unchanged. This rung must not re-implement that derivation.
4. Extend `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` (`sniper/execution/evm_token_safety.py`) to
   include chain 4663 for this rung. Add to the existing setting; do not write a second gate.
5. The existing round-trip budget, post-peak confirmation, and quote-over-source checks apply
   unchanged. Size the round-trip budget from section 9 (up to 4% from curve fees alone, plus the
   existing swap rung's own fee twice for a stock counter currency, plus price impact and gas).

### Fee accounting

The curve keeps 1.00% of the traded counter-currency amount on the paid-out side of every trade,
each direction, confirmed in section 5 and measured empirically at $50 notional in section 9. Size
and PnL math for this rung must include this fee the same way the existing rungs already account
for on-chain swap fees, budgeting up to 4% round trip until the unconfirmed second fee word
(section 5) is explained.

### What phase 3 must still confirm before building

- Word 3 of the trade events (section 5): sometimes a second ~1% fee, sometimes 0. Not explained.
  Budget for the worse case (section 9) until it is.
- Topic 1 of the trade events (section 5): its exact role (initiator vs. something else) is not
  confirmed across enough examples.
- The router-mediated mechanism from the first draft (`0xe33e9e47…`, selector `0xf85f8e41`): left
  unexplained. This design does not depend on it, so it does not block phase 3, but do not reuse
  its word offsets for anything — they were never the curve's own interface.
- The fixed recipient seen at graduation (section 8): read once, not cross-checked against a
  second graduation. Do not assume it is the same address for every coin without checking again.
- Everything in this document is read-only evidence and simulation, not a broadcast transaction.
  Phase 4's live dry proof on a real, funded, curve-phase coin is still required before this rung
  is armed, per the original task's phasing.

## Phase 3 approval conditions (Alpha, 2026-09-17)

Alpha approved phase 3 on the second review, with 7 conditions. The build must satisfy all 7.

1. **No zero slippage floor.** `minAmountOut` is never 0 on a real buy or a real sell — it comes
   from the exact quote (section "Exact quote for rails" below) minus the rung's own slippage
   margin, the same way every other rung derives it. A test pins that a code path building
   `minAmountOut = 0` cannot exist.
2. **Exact quote for rails.** The exit preview, the post-peak confirmation, and the clip-size exit
   check all need a whole-balance sell price for a curve-phase position. Implement a curve quote
   from `getReserves()` plus `feeBps()` (a closed-form constant-product formula). Verify that
   formula against a real `eth_simulateV1` sell at 3 different sizes on a live curve, and require
   agreement within 0.1%. If the formula and the simulation disagree, the simulation wins — never
   ship a guessed formula. The 4% worst-case fee from section 9 is for round-trip *budgeting*
   only; it must never be used inside the quote itself, which uses the real `feeBps()` value.
3. **Counter-token scope for v1.** Native-counter curves buy and sell in one atomic call, per the
   design above. A USDG or a stock-counter curve (for example META) needs a leg into the counter
   token first. Build that leg as a pipelined pre-transaction, in the same pattern as this chain's
   existing WETH-unwrap pre-transaction: persist the reservation's identity before any signing or
   broadcast. If the leg fills and the curve buy then fails, the wallet holds the counter token,
   not native currency and not the coin — name that reservation state explicitly, and the cleanup
   path sells the counter token back through the existing rung (the same re-wrap-style cleanup
   pattern already used elsewhere). If shipping all three counter-token shapes makes v1 too large,
   ship native and USDG first; report a stock counter currency (META-style) as phase 2, not as
   part of this build.
4. **Approvals.** An exact-amount `approve` of the coin (for a sell) or of an ERC-20 counter
   currency (for a buy) to the curve, through the existing approval path this codebase already
   uses elsewhere. No unlimited approval.
5. **Graduation mid-hold.** The exit re-reads launchpad word 10 before every quote and before
   every sell, never from a cached value. A test proves the switch: a position opened while on the
   curve exits through the existing derived-V4-key path once word 10 becomes 2, and a curve
   `sell` is never built once word 10 is not 0.
6. **Marks.** A curve-phase position needs its own mark source (a `getReserves()`-derived mid
   price) so the rails and the position card are not blind while the coin is pre-graduation. State
   plainly which tape this mark writes to and what quality it carries.
7. **Flag and clip-check scope.** `MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED`, default off. The
   Arc-only clip-size exit check (`MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS`) extends to cover this
   rung on chain 4663 — scoped to `launchpad_curve` positions specifically, not to every 4663
   rung, since the existing V3/V4 4663 rungs must stay byte-identical to their current behavior.

Then phase 4: dry-prove the *real* transactions this code builds (not hand-encoded calldata) with
`eth_simulateV1` — buy, approve, sell, and the graduated-exit variant — for one native-counter
curve coin and one USDG-counter curve coin. TDD, `set -o pipefail`, targeted tests plus
`test_memecoin_evm*.py` and `test_memecoin_v4*.py`. Report the SHA; do not merge or deploy.

## Phase 3 and phase 4 status (this commit)

### Built and tested (TDD; `tests/test_memecoin_evm_launchpad_curve.py`, 29 tests, all green)

`sniper/execution/evm_launchpad_curve.py`:

- `curve_state`, `curve_reserves`: read the launchpad's info call and the curve's `getReserves()`
  through the same shared read layer (`evm_read_rpc.eth_call_raw`) every other rung uses.
- `cpamm_out`: the closed-form constant-product-with-fee quote formula. Condition 2's 0.1% bar is
  met with real numbers, not asserted: 3 buy sizes agreed with a real `eth_simulateV1` buy to the
  last few atomic units (effectively exact); 3 sell sizes agreed within 0.002%–0.057%. The golden
  numbers from those real chain reads are pinned as test cases, not re-derived at test time.
  Reserve-update rule confirmed the same way: the fee comes out of `amountIn` before it changes
  the reserves (read `getReserves()` before and after a real simulated buy, in the same bundle).
- `encode_buy`, `encode_sell`, `encode_approve`: byte-exact calldata for the confirmed selectors.
  `encode_approve` is exact-amount only (condition 4) — a test pins that it never encodes
  `2**256-1`. `_min_out` (condition 1) is used everywhere a transaction is built; a test pins that
  the encoded `minAmountOut` word is never 0 for a real quote.
- `dex_launchpad_curve_quote`, `dex_launchpad_curve_exit_quote`: rung-shaped quote functions
  (same call signature as `dex_v3_quote`/`dex_v4_quote`, so they can sit in the existing ladder in
  `evm_real_seams.py` once wired in — see "not yet done" below). Native counter currency builds a
  `msg.value` transaction; an ERC-20 counter currency builds an `issues.allowance` transaction, the
  same shape `dex_v3_quote` already returns.
- `whole_balance_exit_quote_or_refuse`: this rung's own clip-size safety check (condition 2's
  rails requirement and condition 7's scoping requirement) — an exact whole-balance quote from the
  curve or an outright refusal, implemented entirely inside this module so it cannot touch
  `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` or change V3/V4 behavior on any chain.
- `exit_quote`: the graduation switch (condition 5). Re-reads launchpad state on every call; a
  test proves a position quoted on the curve at state 0 and quoted again at state 2 switches to
  the derived-V4-key path with no curve `sell` ever built. `derived_pool_keys()` is called
  unchanged, not re-implemented.
- `mark_price_entry`: one `memecoin_position_marks`-shaped price entry (condition 6) — a
  `getReserves()` mid price, `source="launchpad_curve"`, `quality="live"`, writing to the same
  table `write_marks()` already writes every other rung's marks to
  (`sniper/services/memecoin_exits.py`). None past graduation.

Settings: `MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` (default off), `sniper/settings/memecoin.py`.

Regression: `test_memecoin_evm*.py` (796 tests) and `test_memecoin_v4*.py` (303 tests) both green,
unaffected by this addition.

### Phase 4: dry proof with eth_simulateV1 of the real built transactions

All three required cases, using this code's own functions end to end (not hand-encoded calldata),
nothing broadcast:

1. **Native-counter curve** (`0x5e9ca04c…afd14f`, curve `0xd1255489…037040`): `curve_state()` read
   live; `dex_launchpad_curve_quote()` built the buy transaction; chained
   `buy → encode_approve → dex_launchpad_curve_exit_quote()`'s sell transaction, all through
   `eth_simulateV1` — every call `status = 0x1`, sell payout above its own `minBuyAmount`.
2. **USDG-counter curve** (`0x7231d030…62d5c5`, curve `0xf56ff876…c0bdb3`): same chain, this time
   with a state override funding the throwaway account's USDG balance (storage slot 1, found by
   probing `balanceOf` against candidate slots — USDG's own layout, not guessed from memory).
   `encode_approve` on USDG before the buy, per condition 4. Every call `status = 0x1`.
3. **Graduated-exit variant**: `exit_quote()` called on a real, already-graduated coin
   (`0x7a77fff7…3a317a6`, the same coin used in section 8's graduation evidence).
   `curve_state()` read `state = 2` live; the dispatcher refused the curve and called the real
   `derived_pool_keys()`, which returned the confirmed pool id
   `0xe2474040…1a9ba06` — the exact same pool id the graduation transaction itself initialized in
   section 8. No curve `sell` was built.

## Integration into the live entry/exit path (second commit)

Per Alpha's second review, native-counter is now wired end to end into the real runner path, not
only the rung's own standalone module. USDG and stock-counter pre-legs remain phase 2, unchanged
from the section above.

### The ladder

`evm_real_seams.py`, inside `venue_quote_with_v2_fallback`'s `combined` closure: a
`launchpad_curve` block sits right after `v4_multihop`, gated by
`MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` and `chain == "4663"` only — the mint-specific decision
(on this launchpad, still on its curve, a payable counter) lives entirely inside
`dex_launchpad_curve_quote`/`_exit_quote` and fails closed to `None`, so this block is the rung's
only gate; there is no other rung to race against for a coin that has not graduated. `"launchpad_curve"`
was added to `ordered_names` (first position — placement is cosmetic here, since this rung and
every other rung are mutually exclusive by construction: only one can ever answer for a given
mint). The direction (buy vs. sell) is read from the same `is_buy` flag the rest of the ladder
already computes. 5 targeted ladder tests
(`tests/test_memecoin_evm_launchpad_curve_ladder.py`) cover: flag off → clean skip; flag on → a
real hit each direction, including the `pre_transactions` WETH-unwrap; a graduated coin falls
through to the next rung untouched; a mint this launchpad has never heard of falls through
untouched. `tests/test_memecoin_evm_quote_miss_attribution.py`'s 5 exact-rung-list assertions were
updated to include the new (always-present, off-by-default) rung entry — a mechanical change, not
a behavior change; all 1121 `test_memecoin_evm*.py`/`test_memecoin_v4*.py` tests pass.

### The WETH ↔ native equivalence (a correction found while wiring this in)

The ladder always calls an entry rung with `sell_token` = this chain's bankroll token
(`CHAIN_QUOTE["4663"]["token"]`, the WETH ERC-20) — never the literal native address, because the
wallet holds WETH, not native. The first version of `dex_launchpad_curve_quote` required a literal
native match and would have missed every real call. Fixed to accept WETH as interchangeable with a
native counter (`_counter_matches`, matching `dex_v4_quote`'s own `is_buy = str(sell_token).lower()
== weth` pattern) and to unwrap it first via a `pre_transactions` entry — the SAME generic
mechanism `dex_v4_quote`'s native buy path already uses (`execute_entry_evm` runs
`quote.get("pre_transactions")` ahead of the main swap for every rung, not just this one).

On the sell side, the curve pays out raw native directly (no router to wrap it back to WETH
first), and this codebase's own accounting already has a fallback for exactly that:
`_receipt_quote_usd` (`evm_real_seams.py`) tries an ERC-20 Transfer of the quote token first, and
when the chain's quote `kind` is `"eth"` and that finds nothing, falls back to
`native_received_in_receipt` — a gas-corrected before/after native balance read at the sell's own
block. A curve sell (the caller sends no value, receives native back) is exactly that shape, so
realized proceeds are measured correctly with no new accounting code. Read, not assumed — verified
against `native_received_in_receipt`'s actual implementation before relying on it.

### The clip-size check, wired as a wrap, not a shared-gate touch

`evm_launchpad_curve.wrap_token_safety_fn(chain, rpc_url, http=, taker=, fallback_fn=)` wraps the
per-chain `token_safety_fn` built in `memecoin_evm_leg.py` (one wrapping point, so both the
parallel and the sequential entry call sites inherit it for free — no second edit site to miss).
For a live curve-phase, native/WETH-counter mint with a known `usd` clip size, it derives the
expected entry amount from a real `dex_launchpad_curve_quote`, then checks the resulting coin
amount is priceable by `whole_balance_exit_quote_or_refuse` — the rung's own exact whole-balance
check, unchanged from the first commit. Every other mint (graduated, a different launchpad, a
non-native counter, no `usd` supplied) falls straight through to the standard dust-probe cascade,
untouched. 9 tests (`test_memecoin_evm_launchpad_curve.py`), all pass-through and refusal paths
covered.

### Bound identity (third commit, matching `54ffd4ec`)

Alpha merged this rung on top of `54ffd4ec` ("Bind ARC exit safety to each actual entry quote"),
which gave `token_safety_fn` a new `bought_amount_atomic` parameter: when the caller already knows
the exact amount its own entry quote will buy, the safety check must price sellability of THAT
amount, never a second, independently-derived estimate that could disagree with what actually
fills. `wrap_token_safety_fn` is checked and fixed against this directly:

- Its signature now names `bought_amount_atomic=None` explicitly (not only caught by `**kwargs`),
  so `_executor_supports_kwarg` — which `evm_real_entry`'s new startup gate uses to decide whether
  a chain's safety function is quote-bound-capable — inspects a real, named parameter.
- When `bought_amount_atomic` is supplied, the curve branch uses it directly and skips deriving
  its own estimate from `usd` entirely (a test pins that the `usd`-based price lookup is never
  even called in that case). When it is `0` or absent, the existing `usd`-based estimate is the
  fallback, unchanged from the second commit.
- `bought_amount_atomic` is always forwarded to `fallback_fn` unchanged (`None` when the caller
  did not supply it — identical to omitting it), so no other chain or rung's contract with the
  real, post-`54ffd4ec` `token_safety_fn` is altered.

Chain 4663 is not a member of `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` (pinned by its own test), so
`evm_real_entry`'s new `quote_bound_safety` startup refusal and its `quote_safety_fn` mechanism are
not wired to this chain today — the pre-existing parallel/sequential safety-worker path (unchanged
by `54ffd4ec` for any chain outside that set) still calls this rung's wrapped function with
`usd` only, exactly as before. The fix above is what makes the contract correct if 4663 is ever
added to that set later, not a change needed for anything armed today. 5 new module-level tests
(`test_memecoin_evm_launchpad_curve.py`) plus 3 tests running the wrapped function through the
real, current `evm_real_entry` (`test_memecoin_evm_launchpad_curve_bound_safety.py`) — a pinned
premise test (4663 is not in the quote-bound set), a full fill through the parallel path with the
curve branch deciding it, and a refusal that never reaches a wallet mutation. All 1133
`test_memecoin_evm*.py`/`test_memecoin_v4*.py` tests green.

### Marks

The fast lane (`memecoin_fastlane.py:refresh_open_positions`) already prices a curve-phase
position correctly once the ladder is wired in — it goes through `preview_exit_fn` →
`_exit_venue_quote` → the same ladder — but tags every mark `source="fastlane_rpc"` regardless of
venue, not `source="launchpad_curve"`. A new `_launchpad_curve_price_fallback`
(`memecoin_real_runner.py`, called from `real_exit_sweep` right before `_quarantine_spikes`) fills
a *gap* in the general (non-fast-lane) `price_map` for a 4663 curve-phase position with a direct
`mark_price_entry` read — `source="launchpad_curve"`, `quality="live"` — never overriding an
existing entry, default off, a per-mint read failure never raises into the sweep. 5 tests
(`test_memecoin_evm_launchpad_curve_marks_fallback.py`).

### Approvals

Both the entry (`issues.allowance` on the quote, handled generically by the existing
`execute_entry_evm` approval check) and the exit (the existing `_approve_token`/`_approve`
closure, called with `token=` the coin being sold) route through the codebase's one existing
approval path. `encode_approve` builds exact-amount calldata only — no unlimited approval,
pinned by a test.

### Integration test — proving the same contract as V4

`tests/test_memecoin_evm_launchpad_curve_integration.py`, mirroring
`test_memecoin_evm_seams.py:test_build_executor_execute_fn_fills`'s exact pattern (a fake
send/confirm `http` stub, `venue_quote_fn` injected directly, real `build_real_evm_executor`):

- A full entry through `execute_fn` fills, with `pos["entry_price"]` from the same balance-delta
  math V4 uses, `pos["evm_route_json"].protocol == "launchpad_curve"`, and
  `m.pending_row(pid)["nonce"]` proving the SAME write-ahead-before-broadcast contract V4's own
  test pins.
- A full exit through `exit_fn`, on a position opened while still on-curve, closes.
- **The graduation-mid-hold switch, through the real runner exit path**: a position opened while
  on-curve, `venue_quote_fn` later reporting `state = 2`, closes through a stand-in
  derived-V4-key quote with the curve `sell` never attempted — proving `exit_quote()`'s
  re-read-every-time design (no stored per-position venue blocks this; confirmed separately that
  `evm_route_json` is consulted only for the `uniswap_v4_multihop` stored-route optimization,
  every other protocol re-derives fresh) actually works through the live call chain, not only in
  the module's own unit tests.

### Phase 4: dry proof of the INTEGRATED path

Per Alpha's instruction, this proof uses the transactions the RUNNER seams build, not the module
functions called directly. `evm_real_seams.sign_tx` — the one signing choke point every entry and
exit transaction passes through — was wrapped to capture every transaction dict handed to it,
while the real `build_real_evm_executor`/`execute_fn`/`exit_fn` ran unmodified, with real live
`curve_state`/`curve_reserves` reads (no stubs) on curve `0xd1255489…037040` (coin
`0x5e9ca04c…afd14f`, native counter). Nothing was broadcast.

Captured and replayed via `eth_simulateV1` against the live chain: the runner's own WETH-unwrap
pre-transaction (byte-identical shape to `dex_v4_quote`'s own, not independently re-verified
here), then the runner's own `buy` call (`status = 0x1`, credited real coin), an `approve` at the
exact coin amount the buy credited, and the runner's own `sell` call (`status = 0x1`, paid out
real native — recomputed `minAmountOut` for the real amount using the same verified `cpamm_out`
formula, since the test harness's fake "held balance" `eth_call` stub had sized the exit's own
captured amount off an unrealistic number; the runner's calldata *shape* — selector, word
layout — was used exactly as captured, only the amount/minOut words were resized to something
real before replay). All three steps: `status = 0x1`, no reverts.

### Signal-count data (2026-09-17, chain 4663, `memecoin_entry_attempts`, last 7 days)

**Corrected.** The first count (below, retracted) counted every signal for a coin on this
launchpad, graduated or not — the wrong question, since a graduated coin needs the *existing* V4
rung, not this one. Re-counted by the launchpad's OWN CURRENT word 10 (0 = still on its curve).

Sampled 220 of 512 distinct mints (most-recent-first): 89 do not belong to this launchpad at all;
of the 131 that do, only 14 (10.7%) are still on their curve RIGHT NOW — 117 (89.3%) have already
graduated. Of the 14 still-curve-phase mints: 11 native-counter, 1 USDG-counter, 2 stock-counter.
As a share of every sampled 4663 signal (220): curve-phase-native 5.0%, curve-phase-USDG 0.5%,
curve-phase-stock 0.9%, already-graduated (any counter) 53.2%, not this launchpad 40.5%.

This is a snapshot of CURRENT state, not state at signal time: a coin seen 6 days ago and
graduated 5 days ago reads as "graduated" here even though this rung would genuinely have applied
to it on the day of that signal. A true curve-phase-at-signal-time count needs a historical
per-block read this session did not do. Read this number as "how much of the current backlog of
recent signals this rung can act on today", not as "how often a signal is curve-phase at the
moment it fires" — the true per-signal rate is almost certainly higher than 5.0%, since most
graduations happen after, not before, the first signal.

<details><summary>Retracted first count (counted all-this-launchpad, not curve-phase)</summary>

512 distinct mints. Sampled the first 180 (most-recent-first): 70 do not belong to this launchpad
at all (a different launchpad or a non-launchpad token); of the 110 that do, 71 are native-counter
(64.5%), 8 are USDG-counter (7.3%), 31 are a stock counter (28.2%). This counted graduated and
curve-phase coins together and overstated what this rung alone covers — see the corrected count
above.

</details>

### What is still not built

- **The USDG / stock-counter pre-leg (condition 3, second half).** Unchanged from the first
  commit: native is complete end to end; a non-native counter's cash-acquisition leg (with named
  reservation states and a cleanup sell-back) is phase 2.
- Word 3 of the trade events, topic 1's exact role, and the router-mediated mechanism from the
  first draft remain open, as stated above — none of them block this rung.

Do not arm `MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` until this is reviewed. Do not merge or deploy
this commit without that review.

### 2026-10-15 amendment: complete price-read recording

Injected curve price adapters receive the current check's recording/deadline HTTP transport and pass it to the live price fallback. Cold or stale Binance/FMP GETs retain provider host, HTTP method and outcome in curve_reads with either one-read flag value. Fresh memo peeks remain nonwaiting and read-free; bounded deadlines, unknown-price refusal, and legacy chain-only callback compatibility remain unchanged. This repairs diagnostic coverage and establishes no live fill-speed improvement.
