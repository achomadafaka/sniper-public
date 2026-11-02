# Memecoin BSC (chain 56) funded parity — design and build list

Date: 2026-09-06. Owner: Alpha (sniper-fc). Operator: "start doing the same thing for a Binance
funded copy wallet, same as base sol and robin" (2026-09-06), to begin after the pool-key work and
the four queued fixes. Builder: sniper-f0. Alpha reviews, deploys, holds the activation lever.
Status: scoped, with a measurement gate before the build list is final.

## 1. Where BSC stands today (measured 2026-09-06, ledger + code + live public RPC)

Signal flow, last 14 days:

| network | feed buys | wallets | armed-wallet buys | funded attempts | fills |
|---|---|---|---|---|---|
| 4663 | 59,342 | 6,969 | 3,539 | ~12,000 | 154 |
| solana | 22,302 | 4,647 | 1,502 | ~4,900 | 512 |
| **56 (BSC)** | **10,045** | **2,735** | **602** | **1,988** | **0** |
| 8453 (Base) | 4,629 | 1,511 | 253 | 938 | 0 |

BSC carries **2.2x Base's feed volume and 2.4x its armed-wallet buys**, and is the third busiest
chain we see. Every one of its 1,988 funded attempts was refused `unroutable_chain`: chain 56 is
not in `MEMECOIN_EVM_ALLOWLIST` (`8453,4663`), so the entry path stops before any quote. The paper
and V6 quote path is dead the same way Base's was, 1,182 `IdentityInvalid` plus 114
`skipped_no_executable_quote` in 14 days. Positions on 56, paper or funded, ever: **zero**. No
marks, no pool keys.

Custody wallet on BSC (`0x898be9c2…`, read live via `bsc-dataseed.bnbchain.org`): BNB 0, WBNB 0,
USDT 0, USDC 0. Completely unfunded.

Live endpoint probe: `bsc-dataseed.bnbchain.org` answers `eth_chainId` `0x38`, 282 ms round trip,
measured block time **0.45 s** — the fastest chain we would trade, twice Robinhood's rate.
`bsc-rpc.publicnode.com` returns HTTP 403 to a header-less request, the same way Base's did; it
needs the runner's user agent before it can be judged.

Gaps against 4663, verified in code. This is a superset of Base's list:

| gap | where | status on 56 |
|---|---|---|
| chain is not funded-capable | `memecoin_chains.CHAINS` | `"funded": False` |
| entry allowlist | `MEMECOIN_EVM_ALLOWLIST` | absent -> every attempt `unroutable_chain` |
| execution venue | `MEMECOIN_EVM_EXEC_VENUE_56` | unset (`disabled`) |
| read RPC pool, identity, quarantine | `settings._evm_rpc_pool` | absent |
| free endpoint registry | `free_endpoint_registry` | no 56 entry, no `0x38` identity constant |
| rung ladder | `MEMECOIN_EVM_RUNG_LADDER_CHAINS` | absent |
| V3 / V2 / V4 address maps | settings, `evm_v4_quote.V4` | absent |
| quote identity for paper/V6 | `memecoin_quotes._validated_identity` | rejects 56 |
| market-data slug | `GT_NETWORK`, `GT_OHLCV_NETWORK` | absent |
| quote currency | `evm_wallet.CHAIN_QUOTE["56"]` | USDC, kind `usd` (wrong shape, see decision 1) |
| hot exit lane | `memecoin_evm_hot.HOT_CHAINS` | excluded |
| push lanes | watcher, `MEMECOIN_EVM_WS_URL_56` | none |
| per-chain nano / concurrency knobs | settings | unset |

Already generic and expected to work once the above lands: EVM token safety, the EVM gas top-up,
the exit executor, the arming API, `_PAPER_RULE_CHAINS` (already lists `56`), the measured entry
ladder, chase caps, mcap floor, premium cap and source fallback.

## 2. PHASE 0 — venue census. This gates the build list and must run first.

**I do not know which venue holds BSC memecoin liquidity, and I will not guess it.** On Robinhood
it is Uniswap v4; on Base the builder measured it as Uniswap v4 with v2 worth $42 in total. BSC has
at least four plausible venues — PancakeSwap v2, PancakeSwap v3, PancakeSwap's concentrated
successor, Uniswap v3 and v4 deployments, and the four.meme bonding-curve launchpad — and the
wrong choice wastes the whole build. Base proved the method: take the coins the feed actually
carried and ask where their liquidity is.

Read-only, public endpoints only, no funds, no custody key:

1. Take the 20 most recent chain-56 coins from `memecoin_events`, weighted toward armed-wallet
   buys. For each, list every pool the price providers know with its venue, pair token and USD
   liquidity. Report: which venue is deepest per coin, the venue totals, and the pair-token
   distribution (WBNB vs USDT vs USDC vs other).
2. Report the median and maximum **pool age** for those coins, and the `eth_getLogs` range limit
   each candidate endpoint enforces. Base's 10,000-block cap against 308-hour-old pools is what
   broke its v4 rung; BSC's 0.45 s blocks make any block-count cap far worse in hours. State the
   reach in HOURS, not blocks.
3. Identity, round trip and rate-limit behaviour of at least four public endpoints, including the
   `bsc-dataseed*.bnbchain.org` family and publicnode with the runner's user agent. Say which are
   chain-owner endpoints, since doctrine prefers custody plus chain-owner official public.
4. Whether any of them serves `eth_subscribe` (Base's chain owner did not).
5. Whether the bonding-curve launchpad, if it is a material share, needs its own rung the way Pons
   v2 does on 4663, or whether its graduated pools are reachable through a standard venue.

Report the numbers into section 6 before writing code. If the census says the deepest venue is one
we have no quoter for, STOP and tell me: that is a new rung, which is a bigger piece of work than
this spec covers, and I will re-scope rather than have it improvised.

## 3. Decisions I am making now, independent of the census

1. **Quote currency is WBNB** (`0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c`, kind `eth`), not
   USDC. Same reasoning as Base, and the census will confirm the pair distribution: the wrapped
   native shape is what every 4663 path already speaks — v4 single-hop on native/wrapped pairs, the
   multihop cash leg, the wrapped-to-native gas top-up, the native/USD sizing read, the preflight
   wrapped-entry quote. `CHAIN_QUOTE["56"]` is USDC today and must change. Note BSC's dominant
   stable is USDT, not USDC, so any stable bridge leg uses USDT.
2. **Token safety is the elevated risk on this chain, not routing.** Honeypots and transfer taxes
   are endemic on BSC in a way they are not on Robinhood. The existing `evm_token_safety` is
   chain-generic and stays authoritative, and the 2026-07-14 finding applies with full force: a
   sell simulation from a zero-token wallet flags every token, so the sell-sim path must be
   verified working on 56 before any funded entry, with a test.
3. **Receipt-first submission.** No pipelined approval on 56. The 4663 fast path is scoped to that
   chain and stays there.
4. **Every default is suffix-scoped to 56.** Nothing changes on 4663, Solana or Base; a test
   asserts it, exactly as the Base release did.
5. **Relay and LI.FI stay off 56.**

## 4. Build list — written after Phase 0, following the Base release's shape

The Base release (88164e58) is the template and most items map one to one: chains registry
`funded: True`, allowlist, exec venue, RPC pool with the `0x38` identity, endpoint registry entry,
rung ladder membership, address maps for whichever venues the census names, `_validated_identity`
accepting 56, market-data slug, `CHAIN_QUOTE` to WBNB, hot-lane membership, per-chain knobs with
the 4663 values as defaults, watcher `--chain 56` when an endpoint serves subscriptions, and an
operator funding script mirroring `memecoin_base_usdc_to_weth.py` (USDT or USDC to WBNB).

Two things the census may add, and the reason it comes first: a new venue rung if BSC's liquidity
does not sit on a venue we already quote, and a launchpad rung if the bonding curve is material.

Every address verified against the official deployments page AND by a non-empty `eth_getCode` on a
public endpoint, with the source URL in the commit message. Factories derived on chain from the
routers, the way the Base work did. Never from memory.

Tests in `tests/test_memecoin_bsc_parity.py` mirroring the Base file, including that 4663, Solana
and Base do not move. The deploy gate's own selection, run with explicit paths, with the passed
count reported.

## 5. Activation, and the operator's part is last

Deploying the release starts the paper arms on 56 and nothing else: funded entries keep refusing
until a wallet is armed on 56 and the wallet is funded, both operator actions, neither blocking the
build or the deploy. When the operator wants it live: bridge to BSC and swap to WBNB with the new
script keeping native BNB for gas, arm the wanted wallets on 56 in the dashboard, and confirm the
per-chain concurrency (proposed: 2) inside the global exposure cap.

Alpha sets per-chain concurrency and restarts on the pinned release once that is answered.

## 6. Phase 0 census results (sniper-72, 2026-09-06, read-only, public endpoints)

**Venue: PancakeSwap is decisive.** Across the 20 most recent armed-wallet buys on chain 56, all
20 have a PancakeSwap pool and PancakeSwap is the deepest venue for 20 of 20, no exceptions:
$5.77M across 73 pools. A Uniswap deployment appears on 4 of 20 coins ($609K). The version split
is **V2 $4.89M / 49 pools (85%)** against V3 $884K / 24 pools (15%), so V2 dominates.

**Pair tokens confirm the quote-currency decision.** WBNB $4.33M (67.9%), USDT $1.20M (18.9%),
no other pair above 2.5%. USDT is the real stable here, not USDC.

**Pool age and getLogs reach — worse than Base on every endpoint but one.** Deepest-pool age
across the 20: min 2.26 h, median 667.7 h (~28 days), max 8009 h (~334 days); 5 of 20 are under
24 h old, so both the fresh case and the long tail matter. Reach in hours at 0.45 s blocks:

| endpoint | eth_getLogs reach |
|---|---|
| `bsc-dataseed{,1,2}.bnbchain.org` (chain owner) | **refuses entirely**, "limit exceeded" down to a 1-block span |
| `bsc-rpc.publicnode.com` | 9,000 blocks ~= **1.12 h**, 10,000 fails ("archive requests require a personal token") |
| `1rpc.io/bnb` | 50 blocks = **22.5 seconds** |
| `bsc.meowrpc.com` | method not supported |
| `bsc.drpc.org` | 429 before a test was possible |

**Endpoints.** All reachable ones confirm `0x38`. Chain-owner dataseed 234-370 ms, no user agent
needed. `publicnode` is the fastest at ~80 ms but returns 403 without the runner's user agent,
the same trap as Base. `bsc.drpc.org` rate-limits on the first request. `binance.llamarpc.com`
does not resolve and must never appear in a list.

**Subscriptions.** The chain owner returns HTTP 404 on the WebSocket and serves no subscriptions,
the same pattern as Base. `publicnode` connects, subscribes to `newHeads` and delivered a real
head with full block data — a working push option, on a non-chain-owner public endpoint.

**Bonding curve is not material.** four.meme / flapsh pools appear on 3 of 20 coins and every one
had already graduated to a PancakeSwap pool before the armed wallet bought it, with $0 curve
liquidity remaining. No case where the curve was the deepest or only venue. No rung needed.

**Stop condition NOT triggered.** PancakeSwap V2 and V3 are Uniswap V2/V3 forks with
CREATE2-deterministic factories, so pool discovery for the dominant venue is one `eth_call`
(`getPair` / `getPool`) per candidate pair or fee tier and needs **zero `eth_getLogs`**. Given
the table above, that is what makes this chain buildable at all.

## 6b. Revised build shape, from the census

1. **PancakeSwap V2 is the primary rung**, discovered deterministically through `getPair`. V3 is
   the secondary rung through `getPool` per fee tier. Both are Uniswap forks, so the existing
   generic V2/V3 quoter path applies once pointed at Pancake's addresses.
2. **No V4 rung, no launchpad rung, no log-scan discovery on 56.** The getLogs table above is the
   reason; do not build a discovery path that depends on it.
3. **Endpoint pair is custody plus publicnode**, with publicnode standing in for the chain owner's
   push role because `bnbchain.org` offers neither getLogs nor subscriptions. The dataseed family
   remains a healthy read member. Every publicnode call carries the runner's user agent.
4. **Quote currency WBNB**, stable bridge leg USDT.
5. **Addresses are the next concrete step and are NOT yet verified.** Every PancakeSwap address
   (V2 factory and router, V3 factory, router and quoter, WBNB, USDT) must be confirmed against
   the official deployments page AND by a non-empty `eth_getCode`, with the factory derived on
   chain from the router the way the Base work did. Never from memory.
6. **No API key is required** for reads, quotes or subscriptions on this plan. Only archive-range
   `eth_getLogs` needs one, and this design does not use it.


## 7. Pre-registered read, 14 days after the first funded BSC fill or 50 BSC closes

Fill rate and refusal classes against 4663 over the same window; share of fills at or below −30%
on the first exact exit quote within 15 s; round trip by rung; quote latency p50/p95 against the
4663 baseline (p50 545 ms, p95 2.2 s); loser rate and dollars per fill with the paper arm beside
funded; and, specific to this chain, the count of token-safety refusals and any fill that later
proved unsellable.

Stop rule: if BSC fills show a first-exact-quote-underwater share above the 4663 baseline on 20 or
more fills, or any confirmed honeypot fill, or three consecutive days beyond −$100, Alpha sets BSC
concurrency to zero and reports before any tuning.

## 8. Not in scope

Ethereum mainnet (1). Any change to 4663, Solana or Base behaviour. A new venue rung, which
Phase 0 may force and which gets its own spec.

## 6c. Verified PancakeSwap addresses (sniper-72, 2026-09-06)

Every address confirmed by non-empty `eth_getCode` on a public BSC endpoint, and every factory
DERIVED on chain from its router/quoter via selector `0xc45a0155` rather than copied.

| contract | address | code | factory() derived |
|---|---|---|---|
| V2 Factory | `0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73` | 19,084 B | is the factory |
| V2 Router | `0x10ED43C718714eb63d5aA57B78B54704E256024E` | 21,936 B | matches V2 Factory |
| V3 Factory | `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865` | 5,151 B | is the factory |
| V3 SwapRouter | `0x1b81D678ffb9C0263b24A97847620C99d213eB14` | 12,154 B | matches V3 Factory |
| V3 QuoterV2 | `0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997` | 8,331 B | matches V3 Factory |
| WBNB | `0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c` | 3,124 B | n/a |
| USDT (Binance-Peg BSC-USD) | `0x55d398326f99059fF775485246999027B3197955` | 4,413 B | n/a |

Live sanity: V2 `getPair(WBNB, BEN)` returns a non-zero pool. V3 `getPool(USDT, BEN, ...)` returns
ZERO at fee tiers 100, 500 and 2500 and a real pool only at **10000 (1%)**, whose address matches
DexScreener's reported pair exactly. Build the V3 candidate tiers around 1% first; the low
stable-pair tiers are the wrong shape for these coins.

PROVENANCE CAVEAT, recorded rather than hidden: `developer.pancakeswap.finance` is bot-walled (403
on every fetch), so the brand binding rests on BscScan's project-applied labels cross-checked
against independent sources, PLUS the on-chain factory derivation and the DexScreener pair match.
The on-chain derivation is the load-bearing evidence — it proves router, quoter and factory are one
internally consistent deployment — while the label is what ties that deployment to PancakeSwap. If
the primary docs page is ever wanted for the record it needs a browser session, not `WebFetch`.

TOKEN-SAFETY SIGNAL, and it upgrades decision 2 from a worry to a measured risk: the census coin
BEN carries only **45 bytes** of contract code, the EIP-1167 minimal-proxy signature. That is the
same shape as OURO on 4663 (2026-09-04), where a minimal proxy defeated storage-layout discovery
and cost roughly half of that chain's real entries. If BSC memecoins are commonly minimal proxies,
the minimal-proxy path in `evm_token_safety` is a BUILD BLOCKER on 56, not a follow-up: it must be
exercised against real chain-56 tokens with a test before any funded entry.

