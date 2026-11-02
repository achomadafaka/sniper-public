# Design: multi-hop routing on chain 4663

Date: 2026-08-30. Author: Gamma. **SPEC ONLY — no code written.** Funded execution path, Protocol C.
Requested by Alpha after the 4663 no-route diagnosis. Alpha sequences implementation; do not hand
this to Beta directly (they are mid-build on the m5 turnover replacement).

## 1. The problem, with the measurement that defines it

Chain 4663 is an RWA chain. Its liquidity is predominantly quoted in **USDG and tokenized
equities**, not in ETH. We hold WETH, unwrap to native, and quote **single-hop native -> token
only**, so a coin whose honest pool is quoted in anything else reads as `no_route`.

Top pools on the chain by depth:

| pool | depth |
|---|---|
| USDG / WETH | $7,801,228 |
| NVDA / USDG | $5,924,297 |
| SPY / WETH | $706,618 |
| WETH / USDG | $219,702 |

Across 14 sampled coins with a sane pool, counter-currencies were **USDG 21, NATIVE_ETH 11**, plus
NVDA, SPY, NFLX, PONS. 10 of 14 (71%) had SOME sane native pool; 4 had none at all.

**The live case.** Every 4663 attempt since the 16:10:25Z deploy — 12 of them — is `no_route`, and
all 12 are **ONE coin** (`0x94d84dac…`, "JOE", from an armed wallet). Its pools:

| pool | fee | verdict |
|---|---|---|
| SPY-paired (V4) | 0 | sane, and unreachable — we hold no SPY |
| NATIVE_ETH (V4) | 150,000 = **15%** | correctly fee-rejected |
| NATIVE_ETH (V4) | 864,114 = **86.4%** | correctly fee-rejected |

The refusal was CORRECT. The honest pool exists and we cannot reach it.

## 2. Exit parity, proven per hop — the evidence Alpha required

Not a depth figure. A measured **four-leg round trip on the live coin**, both hops, both
directions, 2026-08-30:

```
LEG 1  WETH -> SPY   (V3, fee tiers scanned: 100/500/3000/10000; only 500 quotes)
       0.00500000 WETH  ->  0.016145 SPY
LEG 2  SPY  -> JOE   (V4 pool 0xcd7cceb1…, fee=0, hook 0xe5e70264…)
       0.016145 SPY    ->  35,170.0368 JOE
REVERSE
LEG 3  JOE  -> SPY   0.015808 SPY
LEG 4  SPY  -> WETH  0.00489068 WETH

ROUND TRIP: 0.00500000 -> 0.00489068 WETH  =  -2.19%
```

**-2.19% over four legs, against -6.3% / -6.9% measured on SINGLE-hop 4663 routes.** The two-hop
route is CHEAPER than routes we already accept, because the coin leg is a fee=0 pool and the bridge
leg is 0.05% on $706k of depth. The "two hops means double the tax" intuition does not hold here
and the spec does not rest on it.

This is one coin. It is an existence proof that the tax can be acceptable, NOT a distribution.
Section 7 requires the same measurement across a sample before arming.

## 3. Architecture: the route is CROSS-PROTOCOL, and that is the main design fact

The bridge and coin legs live on different protocols:

- **SPY / WETH** pool id is **42 chars** -> a V3 pair. Quoted by QuoterV2.
- **SPY / JOE** pool id is **66 chars** -> a V4 pool (keccak of its PoolKey). Quoted by V4Quoter.

So a single-protocol path encoding cannot express this route. Execution must compose a V3 leg and
a V4 leg, which the Universal Router can do in one transaction but which **no quoting path we have
today spans**.

### What already exists (do not rebuild)

- **V3 multi-hop is BUILT.** `evm_dex_quote.py` composes `(sell, connector, buy)` paths, encodes
  them with `_encode_path`, quotes via `QuoterV2.quoteExactInput(bytes,uint256)` (`cdca1753`), and
  picks the deepest across fee-tier pairs and deployments.
- **It is UNCONFIGURED on 4663.** `MEMECOIN_EVM_V3_CONNECTORS` contains `8453` only.
  `MEMECOIN_EVM_V2_CONNECTORS` has two entries for 4663 (exit-only per doctrine).

### The three-part change

| part | scope | notes |
|---|---|---|
| **A. Configure V3 connectors for 4663** | config only | Adds USDG (and WETH) as connectors. Unlocks every coin whose pool is V3 and quoted in a connector. Cheapest, smallest blast radius, no new code path. |
| **B. V4 multi-hop within V4** | new | For coin legs on V4 reachable via a V4 bridge pool. |
| **C. Cross-protocol V3->V4 composition** | new, largest | The JOE case. Needed for coins whose bridge is V3 and coin leg is V4. |

**Recommend shipping A first and measuring what it unlocks before building B or C.** A is a config
line against machinery that already has tests; C is the one that changes what a funded entry IS.

## 4. Fee screening on EVERY hop — non-negotiable

`_fee_is_sane` currently screens the coin-leg pool. JOE proves why it must extend to every hop: its
native pools charge 15% and 86.4%. **A two-hop route touching one trap leg is a 99% loss wearing a
routable label.**

Requirements:

1. Each hop's fee is screened independently, before the route is eligible.
2. A hop whose fee cannot be read is REFUSED, not defaulted — the existing fail-closed contract
   ("an unreadable fee is not a small one") applies per hop.
3. Do NOT modify `_fee_is_sane` itself. It correctly blocked 15% and 86.4% today and that is the
   clearest gate-works result we have.
4. The V3 path quoter encodes a fee per rung, so the fee is already known per hop and needs no
   extra RPC.

## 5. How a multi-hop quote presents to the execution premium cap

`MEMECOIN_REAL_MAX_EXEC_PREMIUM` is now live on the EVM leg (`688bce61`) and returns
`no_display_price` -> **fail-open** on a missing display price; a route that cannot be priced would
otherwise refuse or admit for the wrong reason.

**Contract:** a multi-hop quote presents to the premium gate exactly as a single-hop one does — as
**USD per token of the WHOLE clip**, computed end-to-end from WETH in to tokens out, with every
intermediate leg already inside that number. The gate must never see a per-leg price, and the
intermediate asset (SPY, USDG) must never appear in the comparison.

**The unit trap this must not reintroduce.** The 2026-07-30 defect valued 4663 quotes in WETH while
everything else was USD, producing $3.47M of fabricated PnL. A two-hop route has TWO non-USD
intermediate units. The spec requires: one conversion, at the end, from WETH to USD using the same
live ETH/USD source the single-hop path already uses, and an explicit test that a route through a
non-USD connector produces a USD-per-token figure equal to the single-hop convention.

## 6. Failure modes, enumerated before code

| # | failure | handling |
|---|---|---|
| 1 | Bridge leg has depth, coin leg does not (or the reverse) | Quote BOTH legs both directions before admitting. A leg quoting one way is not evidence it quotes back. |
| 2 | One hop is a fee trap | Per-hop screen (§4). |
| 3 | Exit path differs from entry path | The exit MUST traverse the same connector. Record the entry route in the position so the exit cannot silently pick a different one. |
| 4 | Connector depth evaporates while we hold | The coin leg is the thin one; the bridge is usually deep. `vol_death`/liveness applies to the COIN leg. A stranded position is stranded on the coin leg. |
| 5 | Intermediate asset dust left after a partial fill | Exits must be exact-balance on the intermediate too, or dust accumulates in SPY/USDG. Needs an explicit sweep rule. |
| 6 | Two-hop latency on the entry critical path | Two quotes instead of one. Measure before arming; 4663 entries already run p50 ~166ms per RPC. |
| 7 | Cross-protocol atomicity | V3+V4 in one Universal Router transaction, or not at all. A two-transaction route can leave us holding SPY. **This is the strongest argument for shipping A before C.** |
| 8 | Premium gate misprices the route | §5. |
| 9 | Slippage compounds across legs | `amountOutMinimum` must be set on the END-TO-END output, never per leg. |

## 7. Rollout and evidence gates

1. **Ship A (config).** Measure how many 4663 coins become routable, and the realized round-trip
   tax on the first fills.
2. **Before B or C**, produce the §2 round-trip measurement across at least 20 coins, reporting the
   DISTRIBUTION of the four-leg tax, not one example. A median tax worse than the TP band is a
   legitimate STOP.
3. Do not arm C without a two-direction quote on every candidate route in that sample.
4. Kill criterion: if the measured four-leg round trip median is worse than the single-hop median
   already on record (-6.3% / -6.9%), multi-hop is not worth its failure surface and this design
   should be abandoned rather than tuned.

## 8. Out of scope

- Holding USDG or any tokenized equity as a bankroll asset. The measured route needs no custody
  change; that is the point.
- Any change to `_fee_is_sane`.
- Arming more wallets. See the note handed to the operator separately: 8 of 1,389 wallets producing
  4663 buys are armed, but wallet SELECTION is the strongest result of the day
  (+$5.85/trade at 76.9% wins out-of-sample for picked wallets vs +$0.40 at 55.0% for the rest),
  and arming for supply is the move that produced the -$662. The 4663 roster question is open and
  wants its own chain-specific evidence, not volume.

---

# Amendment 1 — three corrections, one of them to my own sequencing

Appended 2026-08-30 after Alpha's review. All three verified in the code before amending.

## A1.1 "A is config-only" was WRONG

`MEMECOIN_EVM_V3_CONNECTORS` (`sniper/settings/memecoin.py:1943`) is a hardcoded dict literal with
a single `"8453"` key. The env vars `MEMECOIN_EVM_V3_CONNECTOR_8453` and
`MEMECOIN_EVM_V3_CONNECTOR_FEE_8453` parameterize **that entry's VALUES only**. There is no env
path that ADDS a chain.

So A requires a code edit on the funded routing path: Protocol C and an Alpha deploy, like
everything else. Setting a `.env` variable and restarting would change nothing and look like a
mystery. Recorded because that is an hour someone would otherwise lose.

**While in there, make the dict env-driven per chain** the way `_evm_rpc_pool()` already is, so the
next chain is genuinely configuration. Do not special-case 4663 in a literal.

## A1.2 The precedent is stronger than this spec first claimed

`settings/memecoin.py:1940`, above that dict:

> Base memecoin pools overwhelmingly quote in canonical WETH while custody is canonical USDC, so a
> single-hop USDC/token quote misses the route. The USDC/WETH 500 pool is the fixed first/last rung.

That is this document's finding, already diagnosed, solved and shipped — on Base. **4663 is the
same problem with the assets swapped**: there custody is USDC and pools quote WETH; here custody is
WETH and pools quote USDG.

This is therefore **not a new mechanism. It is an existing, shipped mechanism applied to a second
chain that needs it.** That materially lowers A's risk and is the strongest argument for doing A
before B or C.

## A1.3 JOE's -2.19% is evidence for C, NOT for A

JOE's bridge leg is V3 (42-char id) and its coin leg is V4 (66-char). That is the cross-protocol
case, which **C** unlocks. A — V3 connectors — cannot reach a V4 coin leg.

The §2 round trip therefore may not be cited as evidence for A. A needs its own measured round trip
on coins A actually unlocks. Corrected here before it could be quoted the wrong way.

## A1.4 NEW, and it cuts against this spec's own sequencing

Measured on coins we ACTUALLY attempted (2-day attempt set), quote-asset x protocol across their
pools:

| quote x protocol | pools |
|---|---|
| USDG x **V4** | **20** |
| USDG x V3 | 4 |
| WETH x V4 | 2 |
| WETH x V3 | 1 |
| tokenized equities (AAPL, NVDA, MU, MSFT, AMD, cbBTC…) x V4 | 9 |

**On our real signal coins the USDG pools are predominantly V4, 20 against 4.** If that holds, **A
unlocks less of our actual population than B does**, and the cheap-first sequencing is cheap
without being effective.

**This is n=2 COINS.** GeckoTerminal rate-limited the rest of the sample, so it is a signal, not a
rate, and it must not be quoted as one. The top-page chain-wide pools show the opposite skew (V3 7
: V4 5 for USDG), and those are dominated by RWA tokens rather than the memecoins we trade — which
is itself the likely explanation for the difference.

**Requirement added to §7:** before A is written, measure the quote-asset x protocol split across
at least 20 ACTUAL 4663 signal coins, with the GT rate limit respected so the sample is not
silently truncated. If USDG/V4 dominates, A is still worth shipping for its low risk and the Base
precedent, but B carries the unlock and the sequencing rationale changes from "cheap first" to
"safe first, and the payoff is in B".

## A1.5 Requirements for A, carried from Alpha's review

- Exact connector address(es) and fee tier per rung, **verified on-chain**, not copied from a doc.
- Respect `evm_dex_quote.py:252` — only one endpoint may be the chain's exact quote token.
- Per-hop `_fee_is_sane` on BOTH rungs. The filter itself is not to be touched.
- The premium-gate contract in §5: USD per token for the whole clip, end to end, intermediate asset
  never in the comparison, with the test.
- A measured round trip on the coins A unlocks — not JOE.

---

# Amendment 2 — the §7 measurement, run. It re-ranks A and B, and finds a cheaper fix than either

Run 2026-08-30 on Alpha's ruling that this measurement gates the SEQUENCING, not just A's
implementation. Population: coins **our armed wallets actually bought** on 4663 in 7 days (173
coins), sampled at random, not chain-wide top pools. Truncation reported per Alpha's constraint.

## A2.1 Which change unlocks which coin (n=30 graded of 31 attempted; 1 not indexed by DexScreener)

Classified by the coin's DEEPEST pool:

| unlocks the deepest pool | coins | share |
|---|---|---|
| already routable (native ETH, V4) | 14 | 47% |
| **B** (V4, non-native quote) | 7 | 23% |
| already handled (V3 native) | 7 | 23% |
| **A** (V3, non-native quote) | **2** | **7%** |

**A unlocks 2 coins; B unlocks 7.** The n=2 signal in Amendment 1 holds at n=30. Cheap-first is
cheap without being effective.

## A2.2 The spec's premise was WRONG for our population

Pool counts and liquidity across those 30 coins:

| quote x protocol | pools | liquidity |
|---|---|---|
| **ETH x V4** | 77 | **$6,116,511** |
| USDG x V4 | 69 | $235,563 |
| WETH x V3 | 24 | $2,503,419 |
| USDG x V3 | 4 | $85,488 |

**On the coins we actually trade, native ETH is the deepest quote by 26x over USDG.** This
document opened by saying 4663's liquidity "is predominantly quoted in USDG and tokenized
equities". That is true of the CHAIN-WIDE top pools — which are RWA tokens (NVDA, GME, SPCX, gld)
— and FALSE of our signal population. Two different populations; I measured the wrong one first
and the correction belongs in the record.

## A2.3 A cheaper fix than A, B or C: native ETH vs the WETH TOKEN

DexScreener distinguishes `ETH` (native `0x0`) from `WETH` (the token `0x0Bd7D308…`). Our V4 path
unwraps WETH to native and quotes native ONLY, so a pool quoted against the WETH **token** is
unreachable.

ETH-side routability, n=22 graded of 25 attempted (3 had no V4 pairs; reported, not hidden):

| | coins | share |
|---|---|---|
| sane native-ETH pool exists — **routable today** | 12 | **55%** |
| no sane ETH-side pool at all (needs B) | 8 | 36% |
| **only a sane WETH-TOKEN pool — blocked today** | 2 | **9%** |

**JOE is one of the 9%.** Its pools ranked by reserve: SPY fee=0 $41,936; **WETH-token fee=7%
$563 (SANE)**; then native ETH at 86.4%, USDG at 15%, and six more native pools between 80% and
93%. Every native-ETH pool is a fee trap; the single honest ETH-side pool is quoted against the
WETH token, which we never try.

Adding the WETH token as a second input currency is smaller than A, B or C and unlocks 9%. It
should be scoped as **step 0** and measured before the multi-hop work.

## A2.4 THE ACTUAL REASON 4663 HAS NOT FILLED — and it is not routing

- Armed-wallet 4663 supply: **131 buys across 81 distinct coins in 24h = 3.4 coins/hour.**
- Since the 16:10:25Z deploy (~1.4h): **1 armed-wallet buy.** Below the 24h average.
- 4663 attempts since deploy: 15 across **3 distinct coins**.
- Solana, same window: 11 attempts, **4 fills**.

55% of armed-wallet 4663 coins are routable today. The lane has not filled because supply is ~3
coins/hour, the handful that arrived since the deploy fell in the blocked buckets, and — the
structural part — **`MEMECOIN_MAX_CONCURRENT` is GLOBAL across chains at 1**. Solana produces far
more signals and takes the single slot; a 4663 signal arriving while a Solana position is open
cannot enter regardless of its route.

**That makes Beta's per-chain concurrency work the highest-value item for 4663 fills, ahead of
everything in this document.** Multi-hop widens which coins are reachable; per-chain concurrency
decides whether 4663 ever gets a slot to reach them with.

## A2.5 Revised recommendation

1. **Per-chain concurrency** (Beta, in flight) — without it 4663 competes with Solana for one slot
   and loses on volume.
2. **Step 0: WETH-token as a second input currency** — 9%, smallest change here.
3. **B** (V4 multi-hop) — 23%.
4. **A** (V3 connectors) — 7%, but lowest risk and it has the shipped Base precedent. Worth doing;
   not worth doing first.
5. **C** (cross-protocol) — the JOE-style case, largest surface, needs the atomic Universal Router
   requirement in §6.

---

# Amendment 3 — A2.4 was WRONG. Concurrency is not the blocker; routing is. Plus the step-0 design.

Appended 2026-08-30. Amendment 2 §A2.4 claimed the global concurrency slot starves 4663 and
resequenced Beta's per-chain work to the top. **That is retracted.**

## A3.1 The retraction, and my half of the error

Measured since arming at 14:35 (reproduced independently, not taken on report):

| chain | attempts | reached market_data | **GOT A SLOT** | quoted | fills | **slot refusals** |
|---|---|---|---|---|---|---|
| 4663 | 59 | 37 | **36** | 36 | **0** | **0** |
| solana | 35 | 15 | 14 | 14 | 5 | 0 |

**Thirty-six 4663 attempts got a slot and were quoted. None was ever turned away for one.** 4663
receives MORE slots than solana (36 vs 14) and converts them to zero fills while solana converts 14
quotes into 5. That is the opposite of A2.4's claim.

Every concurrency refusal in the ledger, windowed:

| chain | n | window |
|---|---|---|
| 4663 | 4 | 2026-07-18T22:11 .. 2026-07-19T03:02 |
| solana | 13 | 2026-07-18T22:20 .. 2026-08-28T20:47 |
| 8453 | 1 | 2026-08-13 |

`MEMECOIN_MAX_CONCURRENT` was **7** in July, went 7->3 on 08-19 and 3->1 on 08-20. Those four 4663
rows are evidence that a SEVEN-slot book filled once in July. They say nothing about a one-slot
book today.

**My half of it:** Alpha supplied stale evidence, but I asserted the mechanism independently, from
config plus solana's fill rate — and I had the 12h refusal breakdown in hand showing ZERO
concurrency refusals. I noticed that and wrote the claim anyway. A mechanism that exists in the
code is not a mechanism that is firing, and the refusal ledger was the instrument that would have
said so.

**Per-chain concurrency should still ship, on CORRECTNESS grounds, not fill-rate.** It is a
precondition, not a cause. Nobody should read a flat fill count afterwards as that change failing.

## A3.2 STEP 0 — the WETH token as a second input currency

The top routing item, ahead of B, A and C.

### The gap

`evm_v4_quote` module contract today:

```
BUY  (WETH bankroll -> token): pre-tx WETH.withdraw(amount); UR [V4_SWAP] with tx value=amount
SELL (token -> WETH):          UR [PERMIT2_PERMIT, V4_SWAP, WRAP_ETH]
```

We ALWAYS unwrap to native and quote **native ETH only**. A V4 pool whose currency is the **WETH
ERC-20** (`0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`) is a different currency in the PoolKey and
is never quoted.

Measured, n=22 graded of 25 attempted (3 had no V4 pairs):

| | coins | share |
|---|---|---|
| sane native-ETH pool — routable today | 12 | 55% |
| no sane ETH-side pool at all (needs B) | 8 | 36% |
| **only a sane WETH-TOKEN pool — blocked** | 2 | **9%** |

JOE (`0x94d84dac…`), the live operator alert, is one of the two. Its pools by reserve: SPY fee=0
$41,936; **WETH-token fee=7% $563 SANE**; native ETH 86.4%; USDG 15%; then six more native pools
between 80% and 93%. Every native-ETH pool is a fee trap. The one honest ETH-side pool is quoted
against the WETH token.

### The change

1. Build TWO candidate PoolKey sets per coin: currency = native `0x0`, and currency = the chain's
   WETH ERC-20. Both already come back from discovery; only the native ones are tried today.
2. Quote both, apply `_fee_is_sane` to each (unchanged — the filter is not to be touched), and
   keep the best `amountOut`.
3. **The unwrap becomes CONDITIONAL.** `WETH.withdraw` runs only if a native-currency pool wins.
   A WETH-token route skips it, which is one fewer pre-transaction — strictly simpler and cheaper
   on the entry critical path, not more.
4. Exit mirrors: a WETH-token entry exits through a WETH-token pool and skips `WRAP_ETH`.

### Exit parity — the requirement that dominates, same standard as §2

- **Record the input currency on the position.** The exit must not be free to pick the other one:
  a coin with an honest WETH-token pool and only trap native pools would exit into a 86% fee.
  This is the entry/exit path-binding requirement from §6 failure mode 3, and it applies here even
  though there is only one hop.
- **Quote BOTH DIRECTIONS before admitting**, on a real coin, as Beta did for the single-hop case
  and as §2 does for multi-hop. A WETH-token pool quoting in is not evidence it quotes out.
- Per-currency fee screen. JOE is the proof that the two currencies have completely different fee
  profiles on the same coin.

### Premium gate

Unaffected in shape. Both currencies are ETH-denominated, so the §5 contract holds unchanged: USD
per token for the whole clip, one conversion at the end. **No new non-USD intermediate is
introduced** — which is precisely why this is smaller than B or C.

### Evidence gate before arming

A measured round trip through a WETH-token pool on at least one live coin, both directions, with
its realized tax — the §2 standard. JOE at $563 of depth is a poor first candidate: a $65 clip is
11% of that pool. **Route availability is not the same as route capacity**, and step 0 must not be
graded on whether it produces a fill on a pool too thin to take our clip. Report the depth
distribution of the WETH-token pools it unlocks alongside the count.

## A3.3 Revised order (final)

1. **Step 0 — WETH token as a second input currency.** 9%, smallest routing change, unblocks coins
   today, no new unit in the premium gate.
2. **B** — V4 non-native quote. 7 of 30 coins.
3. **Per-chain concurrency** — ships on correctness, not fill-rate.
4. **A** — V3 connectors. 2 of 30, lowest risk, has the shipped Base precedent.
5. **C** — cross-protocol, largest surface, needs atomicity.

---

# Amendment 4 — step 0's activation gate FAILED. Recommend it stays dark.

Run 2026-08-30 under Alpha's read-only authorization: `eth_call` quotes only, no signing, no
approval, no broadcast. 49 RPC calls of a 260 budget, so no truncation.

## A4.1 The gate, and it does not pass

Sample: armed-wallet 4663 coins. **26 graded of 29 attempted** (3 had no V4 pairs; reported).

**Step 0 unlocks 2 of 26 coins (8%)** — consistent with the earlier 9% at n=22. But the gate was
never the count. It was the DEPTH, and the depth answers no:

| unlocked coin | pool depth | fee | outcome at the live $32.81 clip |
|---|---|---|---|
| `0x94d84dac` (JOE) | **$85** | 7.0% | **round trip -34.82%** |
| `0x42afa2124c` | **$3,003** | 5.0% | **no quote at any size** |

Clip as a share of pool depth across the unlocked set: 1 in the 1-5% band, 1 above 10%. **None
comfortable.**

The second coin was re-tested at $32.81, $10.00, $2.00 and $0.50 — **no quote at any of them.**
That is not a capacity limit, it is an unquotable pool that passes the fee screen. Worth knowing:
`_fee_is_sane` admits it, and only an actual quote attempt reveals it.

**JOE's WETH pool has also thinned from $563 to $85 since this morning.** The 9% figure was never
wrong; the pools behind it are simply too small to trade, and they move.

## A4.2 Recommendation: do NOT activate. Keep the code.

The code is correct and the two blockers are repaired. It is harmless with the flag off, and the
address-ordering and permit2 work stands on its own — those defects existed in the design the
moment the buy branch was written, and they are now pinned by 29 tests and mutation-checked at
four call sites.

But **route availability is not route capacity**, which this document has said since Amendment 3,
and the measurement now says the routes step 0 unlocks are not worth taking. Activating it would
buy the right to trade two coins, one of which moves its own pool by 39% and the other of which
cannot be quoted.

## A4.3 This re-ranks step 0 BELOW B, reversing Amendment 3

Amendment 3 put step 0 first because it was the smallest change. That was a cost argument, and
A4.1 is the value argument that overrides it:

- **Step 0's 9%** are the chain's dregs: $85 and $3,003 pools.
- **B's 36%** are coins with no sane ETH-side pool at all, and their liquidity is where the depth
  actually is — the SPY-quoted pool on JOE alone is **$41,936**, against $85 on its WETH pool.

The coins step 0 reaches are thin BECAUSE the real liquidity sits in the non-ETH quote currencies
that B addresses. Small and cheap is not the same as valuable, and this is the second time in this
document that ordering by cost produced the wrong sequence.

**Revised final order:** B, then per-chain concurrency (already shipped), then A, then C. Step 0
stays built, dark, and available if the population changes — it costs nothing off, and if a deep
WETH-quoted pool appears it is one flag away.

## A4.4 What would change this verdict

Re-run A4.1 when either holds: the unlocked set exceeds ~10 coins, or the median unlocked depth
exceeds roughly 20x the live clip (about $650 at $32.81). Both are cheap to re-measure with the
script used here. Until then the answer is no.

---

# Amendment 5 — B's activation gate PASSES. The same gate step 0 failed.

Run 2026-08-30, read-only, `eth_call` quotes only. 102 RPC calls of a 300 budget on the survey.
Phase is scoped OUT of B's decision path per Alpha's ruling: the gate is "has a sane V4 pool",
which is observable, and phase is recorded as evidence instead.

## A5.1 Addressable set and depth — the gate

Armed-wallet 4663 coins, **26 graded of 29 attempted** (3 had no V4 pairs; reported).

| | step 0 (FAILED) | **B (PASSES)** |
|---|---|---|
| coins unlocked | 2 of 26 (8%) | **8 of 26 (31%)** |
| median depth | $1,544 | **$6,471** |
| max depth | $3,003 | **$49,487** |
| clip <1% of pool (comfortable) | **0** | **5 of 8** |
| clip >10% (moves the pool) | 1 of 2 | 2 of 8 |

Bridge currencies required: **USDG 5 coins**, RDDT 1, AI 1, SPY 1. USDG dominates, and it is
already a configured V2 connector (`MEMECOIN_EVM_V2_CONNECTORS['4663']` contains
`0x5fc5360d0400a0fd4f2af552add042d716f1d168`), so the codebase already treats it as significant.

## A5.2 B is PURE V4. It does not collapse into C.

This mattered and was nearly got wrong. A fuzzy provider text search suggested a V4 WETH/USDG
bridge at $315k; querying the WETH token directly shows it has **exactly one** V4 pool on the
chain (PLTR/WETH). Querying from the USDG side gives the real picture:

| bridge | depth | fee |
|---|---|---|
| **USDG / ETH (native)** | **$2,503,797** | **460 = 0.046%** |
| USDG / ETH | $1,208,019 | dynamic (hook) |
| USDG / WETH | $315,698 | 200 = 0.02% |

So the deepest fixed-fee bridge is **native ETH ↔ USDG at $2.5M**, which suits the existing
unwrap path with no new currency handling. Our $32.81 clip is **0.0013%** of it. B is a
single-protocol V4 multi-hop; cross-protocol composition stays scoped to C.

## A5.3 Two-direction round trip through the FULL path

`ETH -> USDG -> coin -> USDG -> ETH`, both directions, live quotes:

| coin | coin-leg depth | coin-leg fee | four-leg round trip |
|---|---|---|---|
| `0x532c558367` | $9,083 | 3.0% | **-6.34%** |
| `0x186efb6d54` | $4,816 | 0.0% | **-5.52%** |
| `0xc1a0c3929f` | $368 | 3.0% | -15.51% |

**Against the single-hop 4663 baseline of -6.3% / -6.9% already on record, B's FOUR legs cost the
same as the two legs we accept today** — on coins we currently cannot trade at all. The bridge is
cheap enough (0.046% on $2.5M) that the coin leg dominates the cost, which is why adding two legs
does not double the tax.

Compare step 0, which the same measurement killed: -34.82% on an $85 pool, and a second coin
unquotable at any size.

## A5.4 Required guards, derived from the measurement

1. **A MINIMUM COIN-LEG DEPTH.** The $368 pool round-trips at -15.51% while the $4.8k and $9.1k
   pools sit near -6%. The cost is driven by clip-vs-depth, not by the hop count. A floor around
   50x the clip (about $1,600 at $32.81) separates the two groups cleanly on this sample; it is a
   parameter, not a constant, and needs re-deriving as the clip moves.
2. **EXCLUDE DYNAMIC-FEE BRIDGE POOLS.** Several deep bridges carry `fee = 0x800000`, the
   dynamic-fee sentinel, where a hook prices each swap. `_fee_is_sane` allows it deliberately, so
   the fee screen CANNOT bound their cost. The measurement above excluded them and used the
   fixed-fee 0.046% bridge. Using a hook-priced pool on the bridge leg means an unbounded and
   unmeasured cost on every entry and exit.
3. **QUOTE ALL FOUR LEGS BEFORE ADMITTING.** The coin leg is the thin one and it is where a
   position gets stranded. A buy quote is not evidence of a sell quote.
4. **Depth is perishable** — one pool thinned $563 to $85 within hours today — so a depth check is
   an entry-time measurement, never a cached fact.

## A5.5 Status

B's gate passes on addressable set (31%), depth (5 of 8 comfortable), bridge quality ($2.5M at
0.046%), and round-trip cost (parity with existing single-hop). Recommend proceeding to
implementation with the four guards above. Not built; nothing activated.
