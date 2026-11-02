# EVM entry minimum output that knows the coin's buy tax — design

Date: 2026-09-30. Author: Beta. Order: the operator, through Alpha. Review: Gamma.
Status: **BUILT, pool-bound, default off** (§9). Gamma: spec CLEAN. Alpha's ruling: build it, bound to the pool the source's receipt proves taxed.

## 1. The defect

On BSC (56), Base (8453) and Arc (5042) the first entry attempt signs a minimum output of the
quote less `MEMECOIN_ENTRY_SLIPPAGE_PCT` (3%, `evm_real_seams.py:3346`). Robinhood (4663) signs 8%.

- The V2 rung quotes with `getAmountsOut`, which cannot see a transfer tax. The buy is
  `swapExactTokensForTokensSupportingFeeOnTransferTokens`, which checks what the wallet actually
  RECEIVED. So a coin's buy tax comes straight out of the 3%.
- BINF (attempt f26d403f, 18:17:50Z) carries a 3.0% buy tax: its source's receipt sends 626,252
  of the pair's 20,875,075 tokens to the token contract. The first simulation met a price about
  2.5% worse than the quote (estimated from the pair's reserves one block apart): about 5.5%
  needed, 3% allowed. The retry at 8% came 1.8 s later
  (median retry cost), after a block that moved the pool +32%, and failed too.

## 2. Measured (§1 of the order)

14 days, 2026-09-16 to 09-30, every attempt on 56, 8453 and 5042 whose first entry simulation
failed on minimum output:

| attempt | chain | coin | source | lane | buy tax on the source's receipt | retry | outcome |
|---|---|---|---|---|---|---|---|
| f26d403f | 56 | BINF 0x9862…7777 | badabeepp | on-chain | **3.0%** (to the token contract) | failed: +32% block | refused |
| ccd32b2a | 56 | 0x65b7…7777 | 0xsneakky | on-chain | none visible (the source bought on the flap curve) | failed | refused |
| bbe19b06 | 56 | 0xac83…7777 | ResellCalendar | J7 | **1.0%** (to the token contract) | not reached: the re-quote hit the 1.50x chart-mid premium | refused |
| 6adc2c86 | 56 | 0x7c9c…7777 | manual | manual | none (our own fill; Kyber kept 1.1% as a fee) | passed | filled |

- 8453 and 5042: none.
- Of the 3 lost, the retry lost to the move once (0x65b7: beyond 8% as well), and to the
  premium gate once. BINF is the only one a tax-aware first attempt would have filled: 3% tax +
  3% base = 6% against the 5.5% it needed. The other two stay lost under any tax rule.
- BINF was bought later from another source (position 67604, 20:39Z, closed by hand +$9.80).
- Denominator: 50 BSC attempts reached an entry simulation in the window: 45 passed first time,
  4 failed on minimum output, 1 on another simulation reason.

**Plain reading: one fill in 14 days.** The build is small (§4); whether it is worth it is the
ruling asked for (§8).

## 3. Can the tax be read with no new call? (§2 of the order)

| source | in hand at the decision? | measured coverage (14 d) |
|---|---|---|
| the on-chain lane's own decode of the source receipt (`evm_post`, in `source_valuation_json`) | yes: the watcher decodes before it emits | 56: 9 of 14 attempts carried a bound pool leg (7 post-trade, 2 our pool unknown); 4 decoded no leg; 1 had no decode |
| the J7 lane's receipt read (`_start_evm_post_trade_read`) | only raybot origins, only with `MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain>` set; peeked, never waited | 56, 8453, 5042: **0 of 53**. The host is set only for 4663; on 4663 it resolved 5 of 66 J7 attempts in time |
| `evm_token_safety` | it simulates a SELL; nothing measures a buy tax | none |
| the entry simulation itself | an `eth_call` of the router returns no output for this function | none without a new contract call |

So the only source with no new call is the on-chain lane's decode, and only for that lane.

**What the decode already computes.** `memecoin_evm_post_trade._decode` sums every coin Transfer
by sender (`paid`) and finds the one pool leg that bought the coin (`out`). The tax on a
constant-product pair shows as the pair's coin sent to anyone but the swap's next hop: for BINF,
626,252 to the token contract beside 20,248,823 to the router. Proposed:
`buy_tax = 1 - max(single Transfer from the pool) / paid[pool]`, added to the returned dict for a
V2 leg. One division on the watcher's thread; nothing on the entry path.

Limits of that measure, stated:
- A reflection or rebasing tax that moves no Transfer is invisible (reads 0: today's behaviour).
- Taxes above 10% already fail the decode's own binding check (`OUT_SLACK = 0.9`), so they read
  as unknown. The cap below matters only if that changes.
- A coin the source bought on the flap curve and we buy on a pair (0x65b7) reads 0.
- V3 and V4 pools: a tax on those legs is not decoded here (a first cut is V2 only, which is
  where BSC's taxed coins trade).

## 4. Proposed rule (§3 of the order)

On 56, 8453 and 5042, for the first attempt of a V2 route whose event carries a decoded buy tax:

```
min_out = quote x (1 - base slippage - min(buy_tax, cap))     base = 3%, cap = 10%
```

- Tax unknown (J7, no decode, not V2): today's 3% then 8% ladder, unchanged.
- The retry keeps its 8% (its own knob) and adds the same tax.
- Settings: the cap (default 10%, the decode's own ceiling) and a per-chain switch, empty by default.
- The tax is the SOURCE's, read from its own receipt; ours is the same token-level rate on a
  constant-product pair.

## 5. Latency (Rule 5.2)

- Entry path: **0 added calls**. The decision reads one float from the event's decoded payload,
  which it already parses for the chase reference: a dict lookup and two multiplications. The build
  measures the quote step p50/p90 on the production context with and without it.
- Watcher thread: one division per decoded receipt.

## 6. Sandwich exposure against a flat 8%

The tax part of the tolerance is taken by the token, not by a searcher: a sandwich can only take
the base.

| rule | room a sandwich can take on a $92.85 clip |
|---|---|
| today, first attempt | 3% = $2.79 |
| flat 8% | 8% = $7.43 on every entry, taxed or not |
| tax-aware | 3% = $2.79; the tax share is paid to the token either way |

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It widens the signed minimum on taxed entries by the measured tax.
2. **Fill path:** no call added (§5).
3. **Concurrency:** the payload is immutable per event; no shared state.
4. **Absent, zero, unknown:** no decode means unknown means today's ladder. A decoded 0 means
   no transfer tax was seen, and gives today's 3%. They are told apart by the key's presence.
5. **Dependencies:** none new.
6. **Units:** a fraction of the pool's output, the same token units the minimum is in.
7. **Sample:** 4 first-attempt failures in 14 days on these chains; the rule touches the
   on-chain lane only, 14 attempts in that window.
8. **Rollback:** the per-chain switch.

## 8. Rulings asked

1. Build it for about one rescued fill a fortnight, on the on-chain lane only?
2. Or set `MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56` so J7 raybot events also carry a decode (an
   operator `.env` change; a non-funded read host; peeked, never waited), then measure how often it
   is in hand before building?
3. Or neither: the refusals are rare, and 2 of the 3 lost would stay lost under any tax rule.

## 9. As built (2026-09-30)

Alpha's rulings: tax-aware, not a flat 8%; bound to the POOL, not to "any V2 route"; a source
transaction with more than one swap reads as tax unknown. The pool match is available at decision
time, so the pool-bound rule was built, not the V2-only proxy.

- **Measure** (`memecoin_evm_post_trade._decode`): a V2 leg gains `buy_tax` only when the source
  transaction holds exactly one V2 swap through that pair and every coin Transfer out of the pair
  other than the largest goes to the token contract. Then `buy_tax = (sent - largest) / sent`.
  Any other shape (two swaps, a fee to a wallet, no transfers) leaves the key absent: unknown.
- **Apply** (`execute_entry_evm`, before each entry preflight): only when the chain is in
  `MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS` (default empty), the event's decode is already in hand
  (`chase_reference.payload()`, never waited on), `buy_tax > 0`, and `route_pool(chain, route)` of
  THIS quote equals the decoded pool. The signed minimum drops by
  `buyAmount x min(buy_tax, MEMECOIN_EVM_BUY_TAX_CAP_PCT)`; the base slippage (3% first, 8% on the
  min-out retry) is unchanged. Only the amountOutMin calldata word changes
  (`evm_v2_exit_quote.with_lower_min_out`); a quote the V2 fee-on-transfer builder did not build
  is left alone. Every applied, pool-mismatched or non-V2 decision is trace substage
  `evm.buy_tax_min_out` with the before and after minimum.
- **Latency, measured** (5,000 runs, this host): switch off p50 0.4 us, p90 0.4 us; applied
  p50 15.9 us, p90 16.3 us (two keccak pair derivations). No call, lock or retry is added.
- **Activation:** `MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS=56` in `.env` (operator). Rollback: remove it.
- **Not handled:** J7 events on 56/8453/5042 (no decode in hand, §3); V3 and V4 legs; reflection
  taxes that move no Transfer; a source that bought on a different pool than ours (pool_mismatch).
- **Check left behind:** `tests/test_memecoin_evm_entry_executor.py` tax tests run the production
  V2 builder and assert the signed bytes; 12 of 12 mutants killed (pool binding, cap, switch, zero
  tax, both call sites, tax base, one-swap rule, fee receiver, tax denominator, patched word,
  not-lower).
