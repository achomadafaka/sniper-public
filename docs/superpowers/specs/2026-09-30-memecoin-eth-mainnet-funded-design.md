# Memecoin Ethereum mainnet (chain 1) funded leg — design

Date: 2026-09-30. Builder: Beta. Order: operator via Alpha ("live ASAP, just as good as Solana or
better"). Template: the Base (88164e58) and BSC (2026-09-06 spec) parity releases.

**Status: v3, PROPOSED** (v2 53cac26ed: Gamma no HIGH, 3 MEDIUM, 7 LOW, all folded in here; steps
0-1 CLEAN). Built: step 2 (private branch) `735d89e70`; chain-1 paper and prewarm kept off Infura
`6935b4260`. **Status of v2, kept for the record:** v1 (677e51f9f) was reviewed by Gamma: NOT CLEAN, 3 HIGH and 7 MEDIUM, all
accepted and folded in below. v2 also carries the operator's decisions of 2026-09-30:
- Flashbots Protect fast ONLY, failing closed; an approved deviation from the 07-25 doctrine;
- a fixed $20 clip;
- Infura as custody;
- the race is measured in BLOCKS.

H3 (de-allowlisted chains strand exits) shipped first as its own fix, `ee7bf1112`, for every EVM
chain. Built so far on `beta/eth-mainnet`:
- step 0, `a237996c9`: J7 address proofs for the three wallets;
- step 1, `46715f7c2`: config plus a pinned manifest that blocks every chain-1 entry.

## 1. Where chain 1 stands (measured 2026-09-30, read-only)

**Signals.**
- J7 roster ETH buys 09-23..09-30: 571 buys from 45 wallets on 83 coins (Alpha: 570). Every one was
  dropped with the network drop reason `unroutable_chain`.
- The source buy averages $2,589, and the rate is rising: 100-175 buys a day on 09-27..29.
- J7 `lag_s` on chain 1 is p50 0.15 s and p90 34.7 s.

**The operator's first wallets:**

| Wallet | ETH buys since 09-23 | Source size p50 | J7 lag p50 | Registry proof |
|---|---|---|---|---|
| Roveryaps | 18 | $159 | 0.13 s | Solana only |
| badabeepp | 24 | $2,497 | 0.15 s | Solana only |
| 0xsneakky | 17 | $989 | 0.18 s | Solana only |

None of the three has an EVM address in `memecoin_address_registry`, so at launch the lane is J7
only. The arming script `data/arm_eth_wallets_20260930.py` refuses with `chain_not_funded` until
§4 lands and "1" is in the allowlist.

**Venue** (Alpha's census over the 83 coins, public getPair/getPool against WETH):
- 73 have no Uniswap V2 or V3 WETH pool, so they trade on V4 with native ETH;
- 5 are V3 only, 3 are V2 only, 2 are on both.

**V4 is the must-have.** It means reusing the 4663/8453 V4 path.

**Gas, live:** base fee 0.2319 gwei and `eth_gasPrice` 0.2319 gwei (publicnode, 2026-09-30).
Alpha read ~0.25 gwei base plus ~0.3 gwei tip. That is roughly $0.3-0.6 per swap and $1-1.5 per
round trip with an approval.

**Gaps in code today** (current values; each is suffix-scoped to "1" in the build):

| Gap | Where | Today on "1" |
|---|---|---|
| chain not funded-capable | `memecoin_chains.CHAINS` `eth` | `funded: False` |
| entry allowlist | `MEMECOIN_EVM_ALLOWLIST` | absent, so every attempt is `unroutable_chain` |
| quote currency | `evm_wallet.CHAIN_QUOTE["1"]` | USDC (6 dp, kind `usd`), the wrong shape |
| V4 address map | `evm_v4_quote.V4` | keys 4663, 8453, 5042 only |
| V3 / V2 addresses | `memecoin_evm_v3_addresses`, V2 router/connectors maps | None |
| read RPC pool + identity | `settings._evm_rpc_pool` | no "1" entry |
| submission registry | `free_endpoint_registry` | no "1" entry |
| rung ladder | `MEMECOIN_EVM_RUNG_LADDER_CHAINS` | {56, 4663, 8453} |
| quote identity (paper/V6) | `memecoin_quotes._EVM_QUOTE_NETWORKS` | rejects "1" |
| market-data slugs | `geckoterminal_quote.GT_NETWORK` / `GT_OHLCV_NETWORK` | none |
| hot exit lane | `memecoin_evm_hot.HOT_CHAINS` | (4663, 8453, 56) |
| address proof | `scripts/research/verify_axiom_addresses.py --chain` | choices lack "1" |
| gas in PnL | `memecoin_evm_tx_fees` | empty on every chain; only the OP-stack reader writes it |
| exits after de-allowlisting | `build_real_evm_contexts` | only Ink keeps a context while inventory is open (`ink_recovery_required`) |

**Addresses, proved on chain** (publicnode, `eth_getCode` non-empty; factories derived on chain,
never copied). The build commit cites developers.uniswap.org v2/v3/v4 deployments besides.

| Contract | Address | Code bytes | Derived |
|---|---|---|---|
| WETH | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` | 3,124 | = `SwapRouter02.WETH9()` = `V2Router02.WETH()` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` | 9,152 | same as 8453 |
| V4 PoolManager | `0x000000000004444c5dc75cB358380D2e3dE08A90` | 24,009 | = `V4Quoter.poolManager()` |
| V4 Quoter | `0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203` | 5,820 | |
| V4 UniversalRouter | `0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af` | 19,499 | |
| V3 Factory | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | 24,535 | = `SwapRouter02.factory()` = `QuoterV2.factory()` |
| V3 SwapRouter02 | `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` | 24,497 | |
| V3 QuoterV2 | `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` | 8,273 | |
| V2 Router02 | `0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D` | 21,943 | |
| V2 Factory | `0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f` | 13,859 | = `V2Router02.factory()` |

**Custody (operator, 2026-09-30):** `MEMECOIN_EVM_RPC_URL_1` is Infura mainnet (`eth_chainId`
0x1). It is the only infura.io entry in `.env`, disjoint from every other chain's pool. No
publicnode or Alchemy member is ever added to chain 1's pools: publicnode is in the funded
4663/8453/56/5042 pools (5042's primary IS publicnode), and Alchemy keys serve 4663/8453/56 (Gamma
M7).

**Endpoints** (`eth_chainId`, round trip, 2026-09-30):

| Endpoint | chainId / result | RTT |
|---|---|---|
| `ethereum-rpc.publicnode.com` | 0x1 | 59 ms |
| `cloudflare-eth.com` | 0x1 (but `eth_blockNumber` "Cannot fulfill request") | 44 ms |
| `eth.drpc.org` | 0x1 | 81 ms |
| `eth.llamarpc.com` | HTTP 525 | 90 ms |
| `rpc.flashbots.net`, `rpc.flashbots.net/fast` | 0x1 | 313-329 ms |
| `rpc.mevblocker.io` | 0x1 | 51 ms |

**`eth_getLogs` on the V4 PoolManager `Initialize` topic:**
- publicnode returns 5,000 blocks (~16.7 h, 2,201 logs). One read in the first probe was refused
  "archive requests require a personal token"; the retry served it.
- drpc refuses 800 blocks on the free plan.
- cloudflare caps the range at 800 blocks.
- V4 creates about 130 pools an hour on mainnet.

## 2. The race is in blocks (operator, 2026-09-30)

Every copier reacting to the source's buy in block N lands in N+1 at the earliest. Our speed target
is therefore:
1. the share of our fills landing in N+1;
2. our position inside N+1 against the other buyers of the coin.

This is the analog of Solana's S+1. A 12 s block leaves ~10 s to detect, decide, sign and submit
before N+1 is built, which is easier than Solana's 400 ms. v1's "parity is on PnL, not speed" is
withdrawn.

- **Detection:** J7 plus the on-chain watcher, first arrival wins through the existing dedupe.
  J7 already carries chain 1 (571 buys since 09-23). J7's `lag_s` is on J7's own clock, not chain
  time (Gamma L1), so step 0's receipts are extended: J7 receive time against block timestamp,
  measured on the 59 buys of the three wallets before the canary. The watcher covers the three
  proven wallets from go-live.
- **Submission targets N+1:**
  - Flashbots Protect fast shares with all builders.
  - A chain-1 tip knob, `MEMECOIN_EVM_TIP_GWEI_1`, is added on top of the buffered node gas price.
    It STARTS at 0 extra gwei (Gamma LOW 4): the 1.5x-buffered legacy price already pays ~0.5x base
    fee plus the node's suggested tip. It is raised only on the canary's blocks-behind
    distribution.
  - The gwei spike cap refuses entries only.

## 3. Edge cases (CLAUDE.md rule 5, in full)

1. **Money path: yes.** This is a new funded chain. Nothing is armed, funded or allowlisted by the
   build: the pinned manifest (step 1) refuses every chain-1 entry by name until each capability
   is evidenced, and activation is the operator's (§6).
2. **Fill-path speed, other chains (Gamma M6).**
   - The funded hook runs INLINE in the J7 loop (`memecoin_runner.py`, the `real_hook(conn, ev)`
     call). A native V4 buy on chain 1 is two L1 inclusions: `WETH.withdraw`, then the swap. On
     receipt-first that holds the loop ~2 blocks. That is arithmetic, not a measurement.
   - So chain-1 execution runs on its OWN worker thread (§5 step 4). The J7 loop, the slot lane
     and the on-chain lane enqueue a chain-1 decision and return at once, and no chain-1 receipt
     wait ever runs on a shared thread.
   - Chain-1 exits run on that worker too. Chain 1 is NOT in `HOT_CHAINS`: the hot exit worker is
     shared with 4663/8453/56.
   - Named limit (Gamma LOW 6): chain-1 entries and exits share the worker AND the per-(wallet,
     chain) exec lock. An exit can therefore wait behind an in-flight entry, about 2 inclusions,
     ~24 s. A second thread alone would not fix this, because the lock serializes them anyway.
   - Measured before and after on the pinned release, and never estimated: J7-loop pass p50/p90
     and Solana entry-stage p50/p90.
3. **Fill-path speed, chain 1.**
   - The Flashbots RPC round trip is 313-329 ms, measured.
   - The gas guard reuses the executor's gas read: 0 extra calls.
   - Queue wait on the chain-1 worker is recorded per attempt.
4. **Concurrency and global state.**
   - One chain-1 worker thread on its own ledger connection, with a bounded queue. A full queue
     refuses the newest event with a named reason; nothing is dropped silently.
   - Execution is serialized per `(wallet, chain)` (`exec_lock` key `evm-1-<addr>`), so chain 1 never
     blocks another chain's lock.
   - No module attribute, env var or resolver is swapped.
   - The pipelined approval stays 4663-only.
5. **Absent, zero, unknown.**
   - An unreadable gas price refuses the entry, never "0 gwei".
   - A gwei cap of 0 is off.
   - An unknown ETH/USD aborts before broadcast.
   - An unresolved V4 PoolKey is `no_route` by name, never "no liquidity".
   - A wallet with no chain-1 proof has its watcher lane off; that is not "no activity".
   - Gas the fee reader could not read is NULL, never 0.
6. **Dependency failures: submission (Gamma H2, operator decision 1).**
   - `build_evm_backend` always adds the custody RPC as a sender, in `pooled_rpc` and in `auto_free`
     (`submission_backends.py`). The legacy `rpc` mode sends raw bytes to custody. And
     `MEMECOIN_EVM_SUBMIT_MODE` is GLOBAL. So a relay in the free-endpoint registry would still
     broadcast through custody, and a rollback flipped for another chain's incident would make
     chain 1 public.
   - v2 builds a **chain-1 private branch**: chain-1 raw bytes go ONLY to the code-pinned Flashbots
     Protect fast URL, in EVERY submit mode.
   - Flashbots unreachable, 5xx, 429 or timeout is `submitted_unknown` if bytes may have left and
     `not_sent` only if provably not sent. There is never a fallback to custody.
   - A chain-1 send attempted outside the private branch refuses with a named reason.
   - **Fail-closed covers EXITS and stops too (Gamma MEDIUM on `735d89e70`).** There is one
     provider, no health gate and no fallback, by the operator's choice. While Flashbots is down,
     rate limiting or answering 5xx, a chain-1 bag cannot be sold, stops included.
   - So it is seen at once: `RELAY_DOWN_AFTER` (3) chain-1 sends in a row that the relay did not
     answer (ambiguous, or proved not sent) page `RELAY-DOWN`. Any answer, an acceptance or the
     relay's own rejection, ends the streak, and the first answer after a page pages `RELAY-UP`.
     Counted on the caller thread in `submission_wiring.make_evm_send_fn`.
   - The private branch clamps its timeout and deadline to the validated maximum (60 s) in `rpc`
     mode, which no config validation covers. `rlp` is imported at module top, so a missing
     decoder fails at startup instead of switching the byte guard off.
   - The test: in every mode, no chain-1 `eth_sendRawTransaction` reaches the custody URL or any
     URL but Flashbots.
7. **Dependency failures: private inclusion and the dead proof (Gamma H1).**
   - A private transaction is invisible to custody's pending pool. Flashbots documents a pending
     state of "up to 6 minutes" (30 blocks), and signed EVM bytes stay valid until the nonce is
     used.
   - Today's dead proofs read custody `pending`: `evm_entry_never_admitted` (300 s) and
     `evm_close_provably_dead` proof 2 (60 s). A block count is NOT a proof.
   - v2 uses **protocol expiry**, the `last_valid_height` analog. Every chain-1 swap carries its
     own short deadline:
     - `MEMECOIN_EVM_SWAP_DEADLINE_S_1`, entry 60 s and exit 60 s (proposed; 5 blocks).
     - V4: Universal Router `execute(commands, inputs, deadline)`.
     - V3: SwapRouter02 `exactInputSingle` (`0x04e45aaf`) has NO deadline, so the chain-1 V3 swap
       is wrapped in `multicall(uint256 deadline, bytes[])` (`0x5ae401dc`). The selector is already
       verified in `evm_mixed_hop_router.py`.
     - V2: the router's own `deadline` argument.
     - Today's deadlines are 600 s on V4, 300 s on the V2 exit and none on V3. Chain 1 overrides all
       of them.
   - A chain-1 send is released only when, **all at ONE block**:
     - that block's timestamp > `deadline + MEMECOIN_EVM_EXPIRY_MARGIN_S_1` (24 s, 2 blocks);
     - AND the account nonce at that block still equals the transaction's nonce (unspent);
     - AND (entry) the coin balance at that block is exactly 0, or (exit) the bag at that block is
       intact;
     - AND, as the caller's precondition, no receipt for its hash.
   - **One block, not "custody's latest" (Gamma MEDIUM on `5b87e0b1e`).** The code does not pin a
     host: `eth_getBlockByNumber`, `eth_getTransactionCount` and `eth_call` are all hedged reads,
     so at "latest" each can be answered by a different pool member at a different height, and a
     load-balanced URL does the same behind one name. A chain time 24 s fresher than the nonce and
     the bag released a sell that had landed in between.
     - `evm_real_seams.eth_proof_snapshot` (ctx `proof_snapshot_fn`) reads the latest block ONCE,
       number and timestamp, then the nonce and the balance at that block NUMBER.
     - A member that does not have the block answers an error. Any error, missing field or
       unreadable word gives no snapshot, and no snapshot holds the row.
     - Why the nonce at that block is the decisive fact: had the swap landed in any block up to
       it, the nonce there would be past the swap's. Every later block is later in time than the
       deadline, so the swap can only revert there.
     - On chain 1 the close path takes proof 1 (nonce spent by another transaction, bag whole)
       from the same snapshot.
     - Why not "quote balance intact" (Gamma M-C): on the native V4 path `WETH.withdraw` moves WETH
       to native and the gas top-up re-wraps it, so the quote balance moves for reasons unrelated to
       the swap.
   - Named residual: a reorg deeper than the 2-block margin could revive a transaction released
     as dead (Gamma LOW 3).
   - Relay windows stop mattering.
   - Approvals and `WETH.withdraw` carry no deadline, but a late landing cannot create a bag.
   - Exits keep the nonce-exclusive release as well.
   - Open and named, unsolved: whether Flashbots accepts a same-nonce replacement while it holds
     the old bytes. v2 never replaces before the deadline has passed, so the design does not depend
     on the answer.
8. **Units and identity.**
   - Chain "1" / `0x1`. WETH is the 18 dp quote; USDC is the connector only.
   - Native V4 currency is `0x0`. Mints are lowercase.
   - Gas is `gasUsed x effectiveGasPrice` in wei, priced with the sizing ETH/USD.
   - Caps are in gwei. Deadlines are in UNIX seconds against the block timestamp (chain time),
     never host time.
9. **The sample (Gamma M3, M-B).**
   - The operator chose three wallets, and their own Solana records differ widely. Gamma, 14 d
     funded: Roveryaps 29 closes +12.0%/trade; badabeepp 5, -14.7%; 0xsneakky 1, -31.4%.
   - The PRIMARY comparison is therefore the same three wallets on Solana versus on chain 1, in the
     same window, per wallet, and only where BOTH sides reach 10 closes in the window.
   - Today only Roveryaps is armed on Solana (real_chains 5042,8453,solana). badabeepp (5042,56)
     and 0xsneakky (4663,5042,56) will have no forward Solana closes. Their chain-1 results are
     reported but not paired, unless the operator also arms them on Solana.
   - The all-roster Solana comparison is secondary and labelled roster against roster.
10. **Rollback.**
    - Remove "1" from the allowlist. Entries stop in routing (`route_real_entry` drops a
      non-allowlisted chain); `evm_admission_refusal` is defense in depth.
    - The H3 fix (`ee7bf1112` + `fd117441a`, Gamma CLEAN) keeps a recovery-only exit context
      while inventory is open. The runner's context gate drops that chain's buys before any read,
      and the venue switch stays closed.
    - The chain-1 worker drains its queue as refusals.

**Not handled by the first release:**
- tax/rebase tokens beyond `evm_token_safety`;
- hooked V4 pools with unknown hooks;
- non-Uniswap venues;
- an L1 dip retry;
- bridging;
- MEV Blocker (operator: Flashbots only).

## 4. Decisions

1. **WETH quote; V4 first, then V3 and V2 rungs.** Suffix-scoped, receipt-first, relay and LI.FI off.
   The pinned coverage manifest decides admission. Done in step 1.
2. **Submission: Flashbots Protect fast ONLY, fail closed** (operator). It is the private branch
   of §3.6 and never touches custody or any public endpoint.
3. **Clip: fixed $20 per entry on chain 1** (operator; per-chain sizing, not bankroll).
   - **The four parameters are approved and are code defaults (operator 2026-09-30, "I give all
     permissions needed", applied by Alpha to the proposed values). No `.env` line is written.**
     They live in `sniper/settings/memecoin.py`, where every per-chain memecoin knob lives, each
     with a suffix-scoped override:

     | parameter | default on chain 1 | accessor | override | enforced today |
     |---|---|---|---|---|
     | clip | $20 fixed | `evm_fixed_clip_usd_for` | `MEMECOIN_EVM_FIXED_CLIP_USD_1` | yes, in `build_evm_risk_context` |
     | round-trip gas cost cap | 10% of the clip | `evm_roundtrip_cost_cap_pct_for` | `MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1` | NO: the gas guard is step 5 |
     | gas ceiling for entries | 5 gwei | `evm_max_gas_gwei_for` | `MEMECOIN_EVM_MAX_GAS_GWEI_1` | NO: step 5 |
     | canary stop | -25% from close 5 | `evm_canary_stop_for` | `MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE_1`, `..._RETURN_PCT_1` | yes, in `evm_entry_allowed` |

   - Fixed means fixed: a WETH balance under $20 refuses ("insufficient bankroll"); it never
     trades a smaller clip. The regime risk scale still scales the clip down, and a manual entry
     still names its own size.
   - Every other chain is unchanged: no fixed clip, the global cost cap, no gas ceiling, no
     canary stop.
   - Gas is ~$1-1.5 per round trip, **5-7.5% of the clip**.
   - The chain-1 cost check replaces the global 3% cap, which would refuse every $20 entry:
     `MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1` (proposed 10%) against the live round trip.
     - The round trip is: entry `WETH.withdraw` (native V4 buys, Gamma M5), entry swap, exit
       approval (Permit2), and exit swap.
     - Gas units come from the fork rehearsal and live gas from the executor's read.
   - `MEMECOIN_EVM_MAX_GAS_GWEI_1` refuses ENTRIES above the spike cap (5 gwei, approved). Exits
     are never refused by it.
   - **The 5 gwei ceiling stays as a spike guard, although the 10% cost cap binds first**
     (Alpha, 2026-09-30): at 5 gwei and ETH $2,738 the $2 cap allows only 146,104 gas units per
     round trip, against 730,521 at 1.0 gwei. The ceiling names a spike as a spike and is checked
     first.
   - **Stated plainly:** at $20 the gas drag alone (5-7.5%) exceeds Solana's funded mean return of
     +2.9% per trade (Gamma's sample of 350). Net of gas, "as good as Solana" needs a chain-1
     gross edge above Solana's of at least the gas drag. §7 measures it; it is not assumed.
4. **Custody Infura; no publicnode or Alchemy in chain-1 pools.**
5. **Chain-1 worker thread for all chain-1 execution** (§3.2).

## 5. Build list (TDD, each step to Gamma)

0. Done, `a237996c9`: J7 address proofs (`--chain 1 --wallets`); the dry run confirms all three
   wallets. Add now: J7 receive time against block timestamp for the 59 buys (Gamma L1), as a
   measurement.
1. Done, `46715f7c2`: config, the proved address maps, and the pinned manifest.
2. Done, `735d89e70`: **private submission branch** (§3.6). Flashbots fast only, code-pinned,
   every mode, with byte guards on both send primitives. The manifest capability `submission` flips
   only after review and a first real send.
3. **Protocol expiry and dead proof** (§3.7): per-chain swap deadlines (V3 through
   `multicall(deadline)`), with the entry and close release gated on block timestamp > deadline +
   margin. Manifest capability `recovery`, together with H3 (`ee7bf1112`).
   - 3.1 done, `4d8fc9cf5` (Gamma CLEAN). The deadline is set from the HOST clock and judged by
     the chain: host skew shifts the validity window, never the proof, which compares chain time
     with the decoded deadline. `MEMECOIN_EVM_SWAP_DEADLINE_S_1` is clamped to 300 s, because the
     release waits for whatever deadline was signed.
   - 3.2 built (awaiting review): the dead proof on an expiry chain is the swap's own deadline.
     - One allowlisted decoder, `evm_real_seams.decode_signed_swap_deadline`: UniversalRouter
       `execute` word 3, SwapRouter02 `multicall` word 1, Router02 FOT swap word 5. It reads the
       entry row's raw hex and the exit intent's base64 form, legacy and typed envelopes. An
       unknown selector, a short head, an implausible number or undecodable bytes give None, and
       None holds the row. Round-trip tests: the deadline each funded builder encoded is decoded.
     - `evm_swap_expired(network, payload, chain_ts)`: chain time must be past deadline +
       `MEMECOIN_EVM_EXPIRY_MARGIN_S_1` (24 s, never under 12 s). The chain time, the nonce and
       the balance come from one block (`proof_snapshot_fn`, see §3.7). A failed read holds.
     - Entry (`evm_entry_never_admitted`): on chain 1, `pending == latest` is not evidence and the
       300 s age floor is not used. Required: nonce still unspent, expiry, and a CHECKED coin
       balance of exactly 0. A bound row with no payload is held.
     - Close (`evm_close_provably_dead` proof 2): on chain 1 it requires the sell's own expiry
       instead of `pending == latest`. Proof 1 (nonce spent by another transaction, bag whole)
       and the entry's "nonce passed every stored nonce" release are unchanged.
     - Other chains are byte-for-byte as before and never pay for the block-time read.
     - Guard (Gamma LOW): every rung the chain-1 ladder asks with default config is in
       `EXPIRY_PROVEN_RUNGS` (v3, v2, v4, v4_multihop). A rung added without a deadline fails the
       test, instead of being held forever by the "unknown selector: hold" rule.
     - Named, not closed:
       - An aggregator rung (kyber, relay) switched on for chain 1 by config would build swaps
         this decoder does not know; their rows would be held. The guard runs on default config
         only.
       - On receipt-first chain 1 the WETH unwrap goes out BEFORE any write-ahead. A crash between
         that send and the swap bind leaves an unbound row, which releases as "never broadcast"
         while the unwrap may still land. Money-harmless (ETH <-> WETH, re-wrapped by the gas
         top-up), but it is a gap against "persist identity before any network I/O".
4. **Chain-1 worker thread** (§3.2): enqueue from every entry lane, and run exits there too.
   Measure J7-loop and Solana entry p50/p90 before and after.
5. **Gas:** built (awaiting review), with Alpha's four rulings of 2026-09-30.
   - **Gas guard** (manifest `gas_guard`): `memecoin_evm_risk.evm_gas_guard_refusal`, asked once
     by the executor (`gas_guard_fn`) after the safety gate and before the first wallet mutation.
     - Order: gas price unreadable, then the gwei ceiling, then unmeasured gas units, then an
       unknown native price or clip, then the cost cap. Every unknown REFUSES.
     - It reads `_node_gas_price()`, the read the signer makes next, memoized for 1 s. When the
       first signature follows within that second the guard adds no request; otherwise it adds
       one `eth_gasPrice` read to a chain-1 entry. Its latency on the chain-1 custody host is
       NOT measured yet (no chain-1 entry has run). No other chain is asked.
     - Refusal: status `aborted`, reason `gas guard: ...`, reservation released with tag
       `gas_guard`, nothing approved, signed or sent.
     - Round-trip gas UNITS are `MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1`. Unset means unmeasured, and
       the guard refuses. The fork rehearsal (step 7) measures them. No number is invented.
   - **Gas reserve amounts** (ruling 1): chain 1 does not inherit the global
     `MEMECOIN_EVM_GAS_*_NATIVE` values, which are tuned to an L2. While
     `MEMECOIN_EVM_GAS_{ENTRY,APPROVAL,EXIT}_NATIVE_1` are unset, every chain-1 entry refuses with
     the missing names, manual and override entries included. Set from step 7's measurement.
   - **Tip:** `MEMECOIN_EVM_TIP_GWEI_1` (default 0) is added on top of the buffered node gas price.
   - **Fee accounting** (manifest `fee_accounting`): `evm_op_fees.read_l1_tx_fee`, from the
     canonical receipt: `gasUsed x effectiveGasPrice`. The existing off-path fee reconcile thread
     records it in `memecoin_evm_tx_fees`. `l1_data_wei` and `operator_wei` are NULL on L1 (those
     fees do not exist there), with status `complete`. A missing receipt field is `unknown`.
     - Named gap: the reconcile covers FILLS (entry and exit swaps). The WETH unwrap and the
       Permit2 approval are not fills, so their gas is not recorded. Same on Ink today.
   - **Gas top-up** (ruling 2): the existing WETH -> native top-up, now scoped by chain:
     `MEMECOIN_EVM_GAS_TOPUP_CHAINS` (default `4663`). 4663 is unchanged.
     - Chain 1 needs its OWN levels, `MEMECOIN_EVM_GAS_TOPUP_{LOW,TARGET,MIN_WETH_RESERVE}_NATIVE_1`;
       while one is unset the pass does not run (`levels_unset`).
     - It runs on its own worker or the maintenance loop, never on the entry path.
     - On chain 1 it does nothing, before any read, while any reservation on the chain is
       unresolved (`reservation_in_flight`), and it holds the wallet lock entries and exits hold.
     - Its sends go through the private branch like every chain-1 send.
     - Named: a top-up withdraw carries no deadline. If the relay holds it, its row stays
       unresolved (and blocks further top-ups) until the account nonce passes it.
   - **The first real chain-1 send** (what proves `submission`): the operator funding script's
     WETH wrap (step 9), sent through the private branch. Alpha's reading is confirmed, with its
     limit: it proves the relay accepts our signed bytes and that the transaction lands and is
     read by receipt on custody. It proves nothing about swap expiry (`recovery`) or a full exit
     (`route_full_exit`), which the fork rehearsal covers. The script must use the private
     transport: a direct `eth_send_raw` to custody is refused by the byte guard.
6. **V4 PoolKey coverage census** on the 83 coins (manifest `v4_pool_keys`). It measures the
   share resolved by each path:
   - (a) the provider pair plus hash-verified key families;
   - (b) the `Initialize` scan on the custody host;
   - (c) the source transaction's Universal Router calldata, hash-verified.

   If (a)+(b) cover under 80% of the 73 V4 coins, (c) is built first.
7. **Fork rehearsal on chain 1** (the G3 harness): buy, whole-balance sell, partial exit, crash
   injection with recovery, and deadline expiry. It measures gas units. Manifest
   `route_full_exit`.
   - Run 2026-09-30, 41 cases passed on 24 coins: `docs/audits/2026-09-30-eth-step7-fork-rehearsal.md`.
     Round-trip gas units p50 428,903, p90 622,791, max 904,983.
   - Fixed by it: `2dbb00f80` (the context dropped `proof_snapshot_fn`) and `b65af29de` (mainnet's
     Universal Router takes the four-field `ExactInputParams`).
   - `route_full_exit` stays unsupported: the exit ladder takes the first venue by priority, and
     two of 24 coins sold through a drained side pool (-79% and -12.6%). See the audit's §6.
8. **Watcher lane** `--chain 1` for the proven addresses, on Infura (subscription or bounded poll).
9. **Operator funding script:** wrap ETH to WETH and keep the gas reserve. Dry-run by default;
   refuses under an in-flight reservation.

## 6. Activation (operator, through Alpha; last)

1. Deploy: chain-1 paper arms start, and funded entries refuse on the manifest.
2. Each capability is flipped to `supported_tested` in the manifest ONLY with its evidence SHA,
   reviewed by Gamma. `funded_activation: ready` is the last flip.
3. Operator:
   - funds ETH and WETH;
   - runs the arming script;
   - adds "1" to `MEMECOIN_EVM_ALLOWLIST` and sets `MEMECOIN_EVM_EXEC_VENUE_1=direct`.

## 7. Pre-registered canary (written before any chain-1 fill; Gamma M1-M4)

- **Chain-1 paper** stays off (Gamma M-A, `6935b4260`). Paper quotes and the pre-gate key prewarm
  would read the metered Infura custody key for every chain-1 J7 buy, so they wait for a
  non-custody paper host and a measured load.
- **Operability canary:** 20 submitted chain-1 attempts (the funded entry-speed rule). It makes NO
  PnL claim. It reports:
  - fills, refusals by class, and deadline expiries;
  - private-branch outcomes;
  - blocks-behind-source (the N+1 share) and in-block index against the coin's other buyers in
    that block;
  - decision to inclusion p50/p90;
  - J7 receive against block timestamp;
  - gas USD per round trip p50/p90.
- **Parity claim:** at >= 60 chain-1 closes, never before.
  - The metric is gross realized return per dollar of clip (`realized_pnl / entry_notional`). Gas
    is reported as its own line. Solana's `realized_pnl` excludes its network and priority fees
    and ATA rent (Gamma M2), so gross is compared with gross; chain-1 net of gas is shown
    separately.
  - The test is non-inferiority: the lower 90% bootstrap bound of (chain-1 mean − Solana mean)
    must be > −δ, with **δ = 5 points per trade**, pre-registered here.
  - The primary comparison is same-wallet (§3.9); roster against roster is secondary.
  - Gamma's simulation shows why n=20 cannot carry this: the CI width is ~23 points at n=20 and
    ~14 at n=60.
  - Power, stated (Gamma LOW 1): at δ=5, n=60 and se ≈4.4 points, P(pass) is 0.30 at exact parity,
    0.52 at +2.5 and 0.73 at +5. "Not passed" does NOT mean worse; it means not shown at this n.
- **Stop rule (APPROVED by the operator 2026-09-30 at −25% from the 5th close; Gamma M4):**
  - Checked at every close from the 5th: turn "1" off if the cumulative gross return on deployed
    clip is below −25%, or at once on any fill later proven unsellable.
  - **Built:** the first clause. `memecoin_evm_risk.canary_stop_reason` reads
    `db_memecoin.evm_canary_book` (automatic real closes on the chain: count, realized PnL,
    deployed clip) inside the reservation, and `evm_entry_allowed` refuses while it holds.
    - It has no latch of its own: with entries off, the number can only move when an open
      position closes.
    - A close with unknown PnL or notional is left out of all three numbers, never counted as 0.
    - Manual entries are not part of the canary and are not refused by it.
    - Cost: one aggregate query per chain-1 reservation, measured on the live ledger at p50
      4.59 ms, p90 4.79 ms (n=30), inside the reservation's write lock. Other chains pay nothing.
    - **Exposure before the first check, stated plainly:** the rule is not evaluated before
      close 5, so up to 5 clips ($100) can be lost before it can trip.
  - **NOT built:** the second clause ("at once on any fill later proven unsellable").
  - Gamma's false-stop rates at exact Solana parity, checked continuously from close 5, are the
    reference: 2-3% at −25% and 5-8% at −20%. The operator picks the threshold.
  - This stop is GROSS, so gas (5-7.5% a trade at $20) is outside it (Gamma LOW 2). A net-of-gas
    stop is the operator's alternative.

## 8. Rollback

Remove "1" from `MEMECOIN_EVM_ALLOWLIST`: entries stop at once, and exits and recovery keep a
context while inventory is open (`ee7bf1112`). No DB backup is part of any step unless the operator
asks.


## 2026-10-06 activation evidence amendment

The original 83-coin window is reproduced: 558 persisted source buy observations in
`2026-09-23 <= observed_at < 2026-09-30`, 83 distinct mints. The prior 73-V4 estimate
inferred V4 from absence of V2/V3. It was not a measured V4 denominator.
Current providers identify V4 pools for 43 of those historical coins; 36 have a
provider-pair/family key that hashes exactly to the PoolId. The other 40 coins are
unknown, including withdrawn or unindexed pools; they are not successful coverage.
Full-range Infura Initialize reads explicitly refuse spans over 10,000 blocks.
The original 80% condition against 73 is not met by path (a) alone.

Path (c) reuses the current source-receipt worker instead of adding a Universal Router
calldata parser: its exact PoolId hash proves currency pair, fee, spacing and hook,
independently of router encoding. Ethereum is now in the canonical V4 event registry.
Twenty-four independently address-verified source receipts contain 47 V4 swap legs:
26 legs yield 12 distinct exact keys, spanning 11 of the 12 bought tokens. Other legs
include connector pools and unresolved hook identities; those remain typed unknown.
This is the required source-based fallback before funding. Quiet hooks retain the
bounded off-path Initialize fallback. No source-key recovery blocks an entry.

The new fork run passes 23 cases on six selected route exemplars: hookless V4,
native hooked V4, WETH hooked V4, V3 and V2; partial/whole exits, private-only bytes,
canonical crash booking exactly once and protocol expiry are included. Whole exits
return $19.56-$19.86 from a $20 buy before gas. These are operability proofs, not PnL
or live latency evidence. The prior 24-coin fork maximum round trip was 904,983 gas;
activation uses a conservative 1,000,000-unit bound, not a fitted mean. Current gas
maxima and signed-limit headroom are retained in the new evidence JSONL.

The operator selected six wallets (the existing September 30 arming order):
Roveryaps, badabeepp, 0xsneakky, memeticpower, picadura and Onchainmetrics. All six
now have four exact buy-history address matches and no contradictions, on at least
two distinct mints. Initial budget is $100 trading plus $10 gas, explicitly approved
October 6. The initial Ethereum canary has one open position at a time; other-chain
caps and the global three-position cap are unchanged. Receipt waits, retries, dips,
recovery and exits run on its own ledger-owning worker. Lost unsigned queue jobs
on restart and unproven venues are not covered; durable signed claims are covered.

Evidence: `docs/superpowers/evidence/eth/2026-10-06-{fork-roundtrips.jsonl,pool-key-census.json,source-receipt-keys.json}`.
The manifest stays blocked until the live private WETH wrap and canonical fee proof.


### October 6 funded activation

The private WETH wrap confirmed through Flashbots Protect fast, with a canonical
receipt and complete L1 fee recorded. The hashed manifest references that proof,
the 23 fork cases, the source-receipt keys and the censored historical census.
The initial $110 transfer leaves $100 WETH and $9.07 native gas at the funding
price after bridge/wrap costs. Ethereum remains capped at one automatic open
position and $20 per entry. Automatic Ethereum gas maintenance stays off for
this canary; the existing insufficient-gas guard stops further entries when
the native reserve is consumed. Other-chain gas maintenance is unchanged.

### 2026-10-14 amendment: nonterminal Protect diagnostics

A simError on PENDING is the current relay simulation diagnostic, not proof that inclusion is impossible. The warning leaves final inclusion unknown and the existing watcher continues to its terminal outcome; later INCLUDED remains valid evidence. Alert wording changes no submission, replacement, expiry or recovery authority.
