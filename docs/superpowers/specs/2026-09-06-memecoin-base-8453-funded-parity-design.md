# Memecoin Base (chain 8453) funded parity — design and build list

Date: 2026-09-06. Owner: Alpha (sniper-fc). Operator: "bring the Base funded wallet to the same
production as Solana/Robinhood or better; make it so the builder can get it in one go; operator
inputs are the last step and must not block the process." Builder: sniper-f0. Alpha reviews,
deploys, and holds the activation lever. Status: scoped; build approved by the operator's request.

## 1. Where Base stands today (measured 2026-09-06, ledger + code)

Signal flow (last 14 days):

| network | feed buys | wallets | armed-wallet buys | funded attempts | fills |
|---|---|---|---|---|---|
| 4663 | 59,071 | 6,943 | 3,521 | 11,994 | 154 |
| solana | 22,157 | 4,626 | 1,493 | 4,915 | 512 |
| 8453 | 4,605 | 1,502 | 253 | 938 | 0 |

Base attempts end as `not_approved` (934 of 938) because no wallet is armed on 8453, plus 2
`insufficient bankroll`. Funded positions on 8453 ever: 0. Paper positions on 8453 ever: 0.

The paper and V6 quote path has been dead on Base since 2026-07-29: `opened` events stop then
(0x retirement), `skipped_no_executable_quote` runs 08-03 to 08-29 (324 events), and since 08-29
every Base quote fails `IdentityInvalid` (450 events) because
`memecoin_quotes._validated_identity` accepts only `{"solana", "4663"}`. So there is NO current
Base evidence, paper or funded. The activation below starts the paper arms on Base on day one for
that reason.

Custody wallet on Base (`0x898be9c2…`, read 2026-09-06 via the custody RPC): ETH 0.00173, WETH 0,
USDC 0.97. Unfunded.

Plumbing that already covers Base, verified in code:

- `MEMECOIN_EVM_ALLOWLIST=8453,4663`; `MEMECOIN_EVM_EXEC_VENUE_8453=direct`;
  `MEMECOIN_EVM_RPC_URL_8453` = the chain owner's public endpoint (`mainnet.base.org`); the free
  endpoint registry carries `8453:base_standard` and the preconfirmation endpoint with identity
  `eth_chainId 0x2105`.
- V3 quoter, router and connector for 8453 (`MEMECOIN_EVM_V3_*_8453`); `CHAIN_USDC["8453"]`;
  the hot exit lane (`HOT_CHAINS = ("4663", "8453")`); the tick stream's 8453 watched-pool tape;
  the EVM gas top-up (chain parameter); EVM token safety (chain-generic); the arming API and
  dashboard `allowlist_by_chain` (solana, 8453, 4663); the EVM exit executor; `_PAPER_RULE_CHAINS`
  includes 8453; bridge scripts Solana <-> Base (Relay.link) exist.

Missing for parity, verified in code:

| gap | where | 4663 today | 8453 today |
|---|---|---|---|
| read RPC pool (hedged reads, quarantine, identity) | `settings._evm_rpc_pool` | custody + 2 | none, custody URL only |
| rung ladder chains (V4, multihop) | `MEMECOIN_DEX_BLIND_CHAINS` default `4663` | on | off |
| V4 address map | `evm_v4_quote.V4` | set | absent |
| GeckoTerminal network slug | `GT_NETWORK`, `GT_OHLCV_NETWORK` | `robinhood` | absent |
| quote identity for paper/V6 | `memecoin_quotes._validated_identity` | accepted | `IdentityInvalid` |
| V2 router and connectors | `MEMECOIN_EVM_V2_ROUTER/CONNECTORS` | set | absent |
| multihop bridges / second bridges | `MEMECOIN_EVM_MULTIHOP_*_4663` | USDG | absent |
| quote currency | `evm_wallet.CHAIN_QUOTE` | WETH (kind eth) | USDC (kind usd) |
| push lanes (transfers, Initialize) | `MEMECOIN_EVM_WS_URL_4663`, watcher `CHAIN = "4663"` | live | none |
| launchpad-derived pool keys | `MEMECOIN_EVM_V4_LAUNCHPADS_4663` | 1 registry | none |
| nano quote-chase / display-drift knobs | `MEMECOIN_REAL_4663_NANO_*` | set | falls to generic |
| per-chain concurrency | `MEMECOIN_MAX_CONCURRENT_4663`, `_BY_CHAIN` | set | unset |
| relay rung (exit last resort) | `evm_relay_quote` 4663-only | on | none (accepted) |

## 2. Decisions

1. Base quote currency becomes WETH (`0x4200000000000000000000000000000000000006`, kind `eth`),
   the same shape as 4663. Reason: every 4663 path then applies unchanged: V4 single-hop on
   native/WETH pairs, the multihop cash leg, the WETH -> ETH gas top-up, the ETH/USD sizing read,
   the preflight WETH entry quote. Keeping USDC would need a USDC -> WETH leg on every route, a
   USDC -> ETH gas top-up that does not exist, and would leave V4 single-hop unusable. The bridge
   scripts still deliver USDC; a one-shot operator script swaps it to WETH (item A7).
2. Relay stays 4663-only. Base exits use V3, V2 and V4 exit quotes; the hot lane already covers
   8453. No new last-resort venue.
3. Submission stays receipt-first on Base (2026-07-28 spec). Base blocks are 2 s; the 4663
   pipelined approval path is not enabled on 8453.
4. Launchpad-derived discovery ships with an EMPTY Base registry in v1. Base pools are discovered
   by GeckoTerminal, DexScreener, and the PoolManager `Initialize` push lane. Clanker/Zora getters
   are a follow-up canary, not a blocker.
5. All 4663-suffixed knobs become per-chain by suffix with the 4663 value as the default for
   8453, so nothing changes on 4663 and Base can be tuned without code.
6. The measured entry ladder, chase caps, mcap floor, premium cap and source fallback apply to
   Base as they do to 4663; they are chain-generic.

## 3. Build list (one branch, one deploy)

A1. Settings and registries (`sniper/settings/memecoin.py`, `sniper/execution/evm_wallet.py`,
    `sniper/execution/evm_v4_quote.py`, `sniper/execution/geckoterminal_quote.py`,
    `sniper/execution/free_endpoint_registry.py`):
    - `_evm_rpc_pool` builds 8453 from `MEMECOIN_EVM_RPC_URL_8453` plus `MEMECOIN_EVM_RPC_POOL_URLS_8453`,
      default fallbacks the two registry endpoints (`mainnet.base.org`, `mainnet-preconf.base.org`),
      identity `0x2105`, same hedge/quarantine/identity code as 4663.
    - `MEMECOIN_DEX_BLIND_CHAINS` default `4663,8453` (so `MEMECOIN_EVM_RUNG_LADDER_CHAINS` includes Base).
    - `V4["8453"]`: pool manager, quoter, universal router, permit2, `gt_network: "base"`. The
      builder VERIFIES the four addresses against the Uniswap v4 deployments page and records
      the source URL and `eth_getCode` non-empty checks in the commit message. Do not copy from memory.
    - `GT_NETWORK["8453"] = "base"`, `GT_OHLCV_NETWORK["8453"] = "base"`.
    - `MEMECOIN_EVM_V2_ROUTER["8453"]` and `MEMECOIN_EVM_V2_CONNECTORS["8453"]` (WETH, USDC;
      verify the Uniswap V2 router on Base the same way).
    - `MEMECOIN_EVM_MULTIHOP_BRIDGES_8453` default USDC; `MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_8453`
      default USDC. `CHAIN_QUOTE["8453"]` -> WETH kind `eth`; `QUOTE_SYMBOL["8453"] = "WETH"`.
    - Per-chain suffix reads for the 4663-only knobs (nano quote chase, nano display drift,
      tickstream tick, resolve-unknown, max concurrent) with the 4663 value as the 8453 default.
A2. Quotes (`sniper/services/memecoin_quotes.py`): `_validated_identity` accepts 8453 with the
    same 0x-address validation as 4663; the EVM route provider resolves 8453 through the same
    ladder; V6/paper quotes on Base carry `provider="evm_route"` as 4663 does.
A3. Push lanes (`sniper/services/memecoin_onchain_push.py`, `scripts/research/onchain_evm_wallet_watch.py`,
    `scripts/memecoin_watchdog.sh`): the watcher takes `--chain`; the watchdog launches one per
    chain in `MEMECOIN_EVM_ALLOWLIST` that has `MEMECOIN_EVM_WS_URL_<chain>`; the Initialize filter
    uses that chain's pool manager; heartbeat per chain. `MEMECOIN_EVM_WS_URL_8453` is verified by
    the canary (the chain owner's public WebSocket, if it serves `eth_subscribe`; else the free
    registry's next candidate; else the poll fallback runs, which is cheap on 2 s blocks).
A4. Exits: assert by test that V3/V2/V4 exit quotes and the hot lane resolve on 8453 with the new
    addresses; the reversed multihop exit reads `legs_path` unchanged.
A5. Deploy gate (`scripts/deploy_memecoin_runtime.sh`): the LI.FI manifest check stays 4663-only;
    the real-position reconcile and pending-reservation checks must already iterate every funded
    chain; the builder confirms and adds a test if they do not.
A6. Operator script `scripts/memecoin_base_usdc_to_weth.py --amount-usd N --confirm`: exact-in
    USDC -> WETH through the configured V3 router, min-out from the quoter with the funded
    slippage setting, prints the quote and refuses without `--confirm`. Alpha never runs it; the
    operator does.
A7. Tests (real objects, fake RPC, no network) in `tests/test_memecoin_base_parity.py`:
    a. `_evm_rpc_pool` yields the Base pool with identity `0x2105`; a member answering `0x1237` is excluded.
    b. The ladder on 8453 runs v3, v2, v4, v4_multihop and no relay.
    c. `_validated_identity("8453", <0x addr>)` passes; a non-hex mint fails as on 4663.
    d. `CHAIN_QUOTE["8453"]` is WETH kind `eth`; a $150 clip sizes to WETH atomic from the ETH/USD read.
    e. The 4663-suffixed knobs resolve for 8453 to the 4663 values when the 8453 suffix is unset.
    f. The push watcher on 8453 stores an Initialize key from a Base pool manager log and ignores a foreign manager.
    g. The multihop search on 8453 finds WETH -> USDC -> coin when the coin is USDC-paired.
    Run the deploy gate's own selection (core set plus files matching the changed sources) with
    explicit paths and report the passed count.
A8. Read-only canaries before deploy (public Base endpoints only, no funds moved, no custody
    key): (1) identity and latency of both registry endpoints; (2) the ladder quotes 10 recent
    Base armed-wallet buys and reports the round trips per rung; (3) 10 minutes on the Base
    WebSocket: `newHeads` liveness and `Initialize` count; (4) `eth_getCode` non-empty on every
    address added in A1. Numbers go in the commit message and in section 6 of this document.

## 4. Activation (Alpha lever; operator inputs last and non-blocking)

B1. Deploy the release. Paper arms (`funded_clone`, `profile_matched_v1`) start opening on Base
    immediately; funded Base entries keep refusing as `not_approved` / `insufficient bankroll`
    until the operator's items land. Nothing here waits on the operator.
B2. Operator items, in any order, whenever convenient:
    1. Fund the Base custody wallet: bridge USDC from Solana with
       `scripts/memecoin_bridge_solana_base.py`, then `scripts/memecoin_base_usdc_to_weth.py`
       for the bankroll, keeping about 0.02 ETH native for gas (the 2026-07-10 spec's estimate for
       three concurrent full-book exits). Suggested first bankroll: $300 of WETH.
    2. Arm the wallets wanted on 8453 in the dashboard (per-wallet chain toggle).
    3. Confirm or change the proposed caps: 2 concurrent on 8453, Base exposure inside the global
       `MEMECOIN_MAX_TOTAL_EXPOSURE_USD`.
B3. Alpha sets `MEMECOIN_MAX_CONCURRENT_BY_CHAIN` for 8453 and restarts on the pinned release
    once B2.3 is answered; until then the 4663 default applies.

## 5. Pre-registered read (14 days after the first funded Base fill, or 50 Base closes)

- Fill rate: attempts, fills, refusal classes on 8453 against 4663 over the same window.
- Entry quality: share of Base fills at or below −30% on the first exact exit quote inside 15 s
  (4663 baseline before the measured ladder: 12 of 123), round-trip distribution by rung, quote
  latency p50/p95 (4663 baseline p50 545 ms, p95 2.2 s).
- Outcome: loser rate and dollars per fill, paper `funded_clone` on Base beside funded.
- Infrastructure: identity failures, quarantine events, WebSocket reconnects, push detections
  ahead of the feed.
Stop rule: if Base fills show a first-exact-quote-underwater share above the 4663 baseline on 20
or more fills, or three consecutive days of negative funded PnL beyond −$100, Alpha disarms Base
entries (per-chain concurrency 0) and reports before any tuning.

## 6. Canary results

Run by the builder on 2026-09-06 against PUBLIC Base endpoints only. No custody key was loaded, no
funds moved, every call was `eth_call` / `eth_getCode` / `eth_getLogs` / `eth_chainId`.

### A8.1 — identity and latency of both registry endpoints

| endpoint | `eth_chainId` | identity ok | latency ms (min/med/max, n=5) | head |
|---|---|---|---|---|
| `8453:base_standard` (`https://mainnet.base.org`) | `0x2105` | yes | 139 / 141 / 145 | 50,951,916 |
| `8453:base_flashblocks` (`https://mainnet-preconf.base.org`) | `0x2105` | yes | 138 / 141 / 145 | 50,951,917 |

### A8.4 — `eth_getCode` on every address added in A1

Every one non-empty on `https://mainnet.base.org`:

| address | bytes | source |
|---|---|---|
| V4 PoolManager `0x498581ff718922c3f8e6a244956af099b2652b2b` | 24,009 | v4 deployments page |
| V4Quoter `0x0d5e0f971ed27fbff6c2837bf31316121532048d` | 5,820 | v4 deployments page |
| UniversalRouter `0x6ff5693b99212da76ad316178a184ab56d299b43` | 19,499 | v4 deployments page |
| Permit2 `0x000000000022d473030f116ddee9f6b43ac78ba3` | 9,152 | v4 deployments page (same singleton as 4663) |
| V2Router02 `0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24` | 17,891 | v2 deployments page |
| WETH `0x4200000000000000000000000000000000000006` | 2,041 | `V2Router02.WETH()` returned it |
| USDC `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913` | 1,852 | already configured |
| V3 QuoterV2 `0x3d4e44eb1374240ce5f1b871ab261cd16335b76a` | 8,273 | already configured |
| V3 SwapRouter02 `0x2626664c2603336e57b271c5c0b26f421741e481` | 24,497 | already configured |

The two factories were derived ON CHAIN rather than copied: `SwapRouter02.factory()` and
`QuoterV2.factory()` both returned `0x33128a8fc17869897dce68ed026d694621f6fdfd` (24,535 bytes) and
`V2Router02.factory()` returned `0x8909dc15e40173ff4699343b6eb8132c65e18ec6` (13,859 bytes).
`V2Router02.WETH()` returned the quote token above, which confirms decision 1 independently.

The GeckoTerminal `base` slug was probed live before it was added: `token_price` returned WETH
2502.04 / USDC 1.005 in 253 ms, and `networks/base/pools/{pool}/ohlcv/minute` returned HTTP 200
with 100 candles in 584 ms. It is added to `GT_OHLCV_NETWORK` ONLY — see the deviations below.

### A8.3 — the Base WebSocket: THE SPEC'S FIRST CHOICE DOES NOT EXIST

`wss://mainnet.base.org` and `wss://mainnet-preconf.base.org` both answer the WebSocket upgrade
with **HTTP 405**. Base's chain owner does not serve `eth_subscribe` at all, so A3's first branch is
unavailable and its second (the free registry's next candidate) has no other member. Per A3's own
decision tree the poll fallback runs, and `MEMECOIN_EVM_WS_URL_8453` is None by default.

Two THIRD-PARTY public sockets were measured serving it correctly, for the operator's information
only — neither is adopted, because neither is a chain-owner endpoint:

| socket | heads in 27s | Initialize in 27s | first head |
|---|---|---|---|
| `wss://base-rpc.publicnode.com` | 14 | 4 | 0.27 s |
| `wss://base.drpc.org` | 14 | 1 | 0.67 s |

Setting `MEMECOIN_EVM_WS_URL_8453` to either turns the push lane on with no code change, and the
watchdog then launches a second watcher automatically.

### A8.2 — the ladder against 10 recent Base armed-wallet buys ($20 clip)

FIRST RUN, before any fix: **1 of 10 quoted**. The one hit was v3 direct at a −8.10% round trip.
Per-rung, on all ten:

- `v4` returned `poolkey_read_failed` **10 times out of 10**.
- `v4_multihop` returned `no_bridge_candidates` (9) or `bridge_pool_unusable` (1).
- `v3` missed on 5 and returned `round_trip_unmeasured:v3` on 4 — it quoted forward and the reverse
  quote failed, so the measured ladder refused it, which is the guard working.
- Quote latency ran 3.0 s to 44.4 s. The 4663 baseline is p50 545 ms.

The V4 failure has one cause, measured directly: **Base's chain-owner endpoints refuse any
`eth_getLogs` range over 10,000 blocks** (HTTP 413, `-32614 "eth_getLogs is limited to a 10,000
range"`). The pool-key scan pages at 100,000 blocks, tuned to 4663's endpoint, so the FIRST page was
refused, the scan stopped (a refused page says nothing about the range below it), and the
`0x0`->`latest` fallback was refused for the same reason. `publicnode` and `dRPC` refuse the same
shape differently: "Archive requests require a personal token" and "ranges over 10000 blocks are not
supported on free plan".

Two fixes followed, both in this release:

1. The scan page width is now per chain — 10,000 on Base. Six pages then reach ~33 h of calendar at
   2 s blocks, MORE than the ~17 h 4663 gets from 608k blocks at 0.10 s.
2. The Base V3 connector moved from WETH to USDC. `dex_v3_quote` skips a connector equal to the
   sell token, so once the bankroll became WETH the WETH connector left Base with NO two-hop V3
   path at all. This is a direct consequence of decision 1 that the build list did not name.

A THIRD change was made, measured, and REVERTED. The scope asked for
`MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_8453=USDC`; `MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS` is 3 in the
live config, so adding it switched on the three-leg search on Base. The canary went from 3-44 s per
quote to 25 s, 48 s, 101 s, 118 s and **436 s**, produced ZERO extra routes (every candidate ended
`bridge_pool_unusable` or `coin_leg_missing_or_under_floor`), and both Base endpoints quarantined
themselves under the load (`hedged_read_failed`). Base has far more pools per coin than 4663, so the
search that is cheap there is combinatorial here. There is no Base second bridge in this release.

**Base pool age is the finding that outlives these fixes.** DexScreener `pairCreatedAt` for the 12
deepest pools of those same coins: median **308 h**, min 3.5 h, max 521 h, with real liquidity
($140k-$1.24M). Only **1 of 12** is inside the 33 h the log scan can now reach. On 4663 the coins we
quote are a median 1.41 h old. Base is a structurally different population, and log-scan pool-key
resolution is the wrong mechanism for it at any page width — 308 h is ~554,000 Base blocks, 55 pages,
about 19 seconds. See the open question below.

### The second ladder run, after both fixes: still 0 of 10

Re-run on the ten most recent Base coins (five overlap the first run; the feed moved): **0 of 10
quoted**, 5.5 s to 121.7 s per quote. The rung ledger moved, though, and it says where Base is now:

| rung | outcome | n |
|---|---|---|
| v3 | quoted FORWARD, refused `round_trip_unmeasured:v3` (the reverse quote failed) | 7 |
| v3 | no route at all | 3 |
| v4 | `poolkey_read_failed` | 9 |
| v4 | resolved, no quote | 1 |
| v2 | no route | 9 |
| v4_multihop | `no_bridge_candidates` / `bridge_pool_unusable` / under depth floor | 10 |

v3 forward coverage rose from 5 of 10 to 7 of 10, which is the USDC connector opening the two-hop
path. Every one was then refused on the reverse leg. DO NOT READ THE TWO RUNS AS A BEFORE/AFTER:
the coin sets differ and the endpoints were throttled during the second, so the honest summary is
1 of 10 before and 0 of 10 after, with no route on Base either way.

### Why Base cannot quote: it is a VENUE gap, not a config gap

DexScreener pools for the 15 most recent Base coins the feed carried, 2026-09-06:

| venue | pools | total liquidity | deepest pool for N of 15 coins |
|---|---|---|---|
| Uniswap **v4** | 164 | $12.08M | **11** |
| Uniswap v3 | 28 | $2.09M | 2 |
| Aerodrome | 24 | $3.21M | 2 |
| Hydrex CLAMM | 6 | $0.79M | 1 |
| Uniswap v2 | 1 | $42 | 0 |

Base memecoin liquidity is on **Uniswap V4**, and V4 is the one rung that cannot resolve a pool key
on Base. Uniswap V2 is a rounding error ($42 across one pool), and the two chains' shapes are
opposite: on 4663 the V2 rung is a real exit path, on Base it is nothing. Aerodrome and Hydrex hold
the deepest pool for 3 of 15 coins and we do not support either venue.

So the ceiling on this release, with every address correct and every rung wired, is roughly the 2 of
15 coins whose deepest pool is V3 — and even those are refused today on the reverse leg.

### THE BLOCKER, for Alpha to scope (not resolved in this release)

V4 pool-key resolution on Base needs a mechanism that is not a log scan, and this is the critical
path, not a nice-to-have: 11 of 15 coins are unreachable without it.

The pool id IS `keccak(abi.encode(PoolKey))`, and DexScreener and GeckoTerminal already return the
pair's two tokens for pools they index. The key can therefore be DERIVED from the provider's pair
plus a small candidate set of (fee, tickSpacing, hooks) and then VERIFIED by hashing back to the
pool id we already hold. Verification is exact equality, so a hit is proof rather than a guess, it
costs ZERO RPC, and it is indifferent to how old the pool is — which is the whole problem on Base,
where the median pool we would trade is 308 hours old and the scan reaches 33.

The same mechanism would help 4663, where the scan currently costs up to six `eth_getLogs` pages per
unseen pool. `evm_v4_launchpad.key_from_struct` / `pool_id_for_key` already do the hashing half.

Two smaller open items, in order of value:
1. The v3 reverse-quote failure (7 of 10) is unattributed between "no reverse pool at that size" and
   "the public endpoint was throttled". One targeted both-directions probe settles it.
2. Aerodrome is the deepest venue for 2 of 15 coins and is not a supported venue on any chain.

### Deviations from the build list, and why

Five items in section 3 were NOT implemented as written. Each is a case where the literal
instruction would have changed Base for the worse, and each is asserted by a test.

1. **`MEMECOIN_DEX_BLIND_CHAINS` stays `4663`.** A1 asked for `4663,8453` "so
   `MEMECOIN_EVM_RUNG_LADDER_CHAINS` includes Base". Those are two different questions and the code
   split them deliberately on 2026-09-04 after conflating them disabled the V4 rung on 4663. Base is
   NOT DexScreener-blind, so adding it to the pricing list would route Base price lookups to a
   secondary source — the RUSSELL lesson, which `memecoin_evm_price.price_for` refuses anyway.
   `MEMECOIN_EVM_RUNG_LADDER_CHAINS` now defaults to `4663,8453` in its own right, which is also the
   value that takes effect: `.env` pins the blind list but not the rung list.
2. **`GT_NETWORK` does not gain Base; `GT_OHLCV_NETWORK` does.** A1 asked for both. `GT_NETWORK`
   governs which chains GeckoTerminal may PRICE, and its own docstring names Base as deliberately
   excluded because DexScreener covers it. Candles are a different question and the `base` slug was
   probed live, so the OHLCV map is safe and useful. Pricing is not.
3. **`MEMECOIN_EVM_MULTIHOP_BRIDGES_8453` stays unset.** A1 asked for USDC. Configuring a first
   bridge TURNS PER-COIN DISCOVERY OFF for that chain (`_candidate_bridges`), which is a narrowing,
   not an enablement; 4663 runs with discovery on and it is the most profitable route on its book
   (+$143.76 over 13 fills). Parity with 4663 means discovery, so Base has no first bridge.
4. **`MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_8453` stays unset.** Implemented, measured, reverted —
   see the 436-second canary above.
5. **`MEMECOIN_TICKSTREAM_4663_TICK_S` and `MEMECOIN_RESOLVE_UNKNOWN_EVM_4663` do not become
   per-chain with the 4663 value as the Base default.** Both would make Base worse: the slow tick
   exists because 4663 scans the whole chain while Base uses a watched-pool tape, and the
   resolve-unknown gate is a DENY list that 4663 is on and Base is not, so inheriting the 4663 value
   would remove existing Base flow. `MEMECOIN_RESOLVE_UNKNOWN_EVM_8453` exists and defaults to ON,
   which is today's behaviour made explicit.

One item was added that the build list did not name: the Base V3 connector had to move from WETH to
USDC, because decision 1 made WETH the sell token and `dex_v3_quote` skips a connector equal to it.
Left alone, Base would have had no two-hop V3 path at all.

### Infrastructure note

The public Base endpoints did not sustain the ladder's read volume during the canary: both members
were quarantined for `hedged_read_failed` under back-to-back runs. Base entry latency will not reach
the 4663 baseline on keyless chain-owner endpoints alone.

## 7. Not in scope

- Clanker/Zora/Virtuals launchpad getters (follow-up canary).
- A relay or LI.FI venue on Base.
- Any change to 4663 or Solana behaviour; every new default is suffix-scoped to 8453.
