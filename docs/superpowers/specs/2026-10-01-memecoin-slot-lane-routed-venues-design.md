# Slot lane builds the routed handoffs itself (spec, step 2)

Status: PROPOSED, 2026-10-01 (Lambda; operator order after LILMET 68051, via Alpha). Doc only.
Decision D1 (§3) is the operator's. The DAMM v2 builder (§4, B2) does not depend on D1.

## 1. What the handoffs are (census, read-only)

Script and rows: `~/.cache/lambda/handoff_census/` (outside the repo, because it reuses Gamma's
chain helpers).
- Method: the ledger joined to the chain.
  - For each handoff, the source transaction was read on the research Helius key (flat-book gated,
    2 req/s).
  - The token leg is the DEX instruction that moved the coin out of a pool vault; aggregator
    programs are excluded.
- Window: 2026-09-27 to 10-01, 4.8 days, because the routed handoff began on 09-27. There are no 14
  days to read.

- 375 handoff signatures. Every one was paid in USDC by the source and routed by an aggregator:
  DFlow 165, Jupiter 92, proVF4pM 77, Flash 32.
- 35 reached a send and 34 filled. The rest were refused by gates: cooldown, source floor,
  one-per-coin, mcap.
- **Today these 35 land at p50 S+4:** S+2 on 3, S+3 on 11, S+4 on 5, S+5 on 10, S+6 on 5, S+7 on 1.

Sends by the coin's pool (the token leg):

| coin pool | quote of that pool | sends | land - S |
|---|---|---|---|
| Raydium CPMM | a non-SOL/USDC token | 17 | p50 S+5 (2-7) |
| Meteora DAMM v2 | SOL | 5 | p50 S+4 |
| Raydium LaunchLab | a non-SOL/USDC token | 3 | 3, 4, 6 |
| pump AMM | non-SOL (2), SOL (1), USDC (1) | 4 | 3, 3, 4, 6 |
| DLMM + CPMM split | SOL + other | 2 | 3, 5 |
| CLMM, CPMM-SOL, DBC-other, DAMM v2-MET | | 1 each | |

Meteora DLMM appears in 147 handoffs but only 2 sends. CLMM appears in 46 handoffs with 1 send.

**Where the non-SOL quote token comes from.** Every hop of the 35 send transactions was decoded:
- The source buys the quote token in the same transaction, on a concentrated-liquidity pool:
  DLMM, Raydium CLMM or Orca Whirlpool. That is USDC→quote, then quote→coin on CPMM, LaunchLab,
  DBC or pump AMM.
- The quote tokens: xStocks (`Xsc9qvGR`, `XspzcW1P`, `Xs3oZwbH`, `XsoBhf2u`), pre-IPO and
  stock tokens (`oPAiAikW`, ANTHRP `Pren1FvF`, `NFLX7qV5`, `DJTu7vi8`, `SPCXxcqX`, `CARDSccU`),
  and USELESS, GRASS, MET and others.
- 26 of the 35 sends go through such a quote token, spread over about 15 distinct mints.
- LILMET 68051 is one of them: USDC→SOL on Jupiter, SOL→MET on DLMM, MET→LILMET on DAMM v2. It
  landed S+3.

**USDC is not the lever.** Every lane buy is funded from our own prefetched USDC→SOL leg, whatever
the source paid. USDC-quoted coin pools appear in 8 handoffs and 1 send. What blocks the lane is
the coin pool's venue and its quote token.

## 2. Why the lane cannot take them today

- **The lane is fast because nothing on its fill path calls a provider.** The USDC→SOL leg is
  prefetched (Jupiter, every ~1 s, on the funded key), and the coin swap is built from the source's
  own event.
- **A non-SOL quote needs a USDC→quote leg.**
  - Today it exists only for ZEC: a warm leg, about 1.1 funded Jupiter calls/s, off until ZEC goes
    live.
  - It also exists as the shadow on-demand worker. That worker takes the leg after the signal on
    the paper key, and its build takes 410-470 ms (cpmm shadow lines, 2026-10-01). That is no
    faster than the on-chain lane after C1/C2.
- **Keeping a warm leg per quote mint does not scale.**
  - About 15 mints at 1.1 calls/s each would be about 16 extra funded Jupiter calls a second.
  - A lazy warm set (a mint stays warm after it is seen) would have caught only 9 of 26 sends
    with a 6 h window, and 13 of 26 with 24 h.

## 3. Decision D1: how the lane holds the quote token (operator)

| option | fill-path latency | S-offset target | cost / risk |
|---|---|---|---|
| **(a) Quote-token inventory** (recommended) | none: the coin swap spends quote tokens already in the wallet, built natively like today's SOL/USDC pools | S+1 (the lane's own: 68/100 at S+1, 8 at S+0) | capital held in quote tokens and their price risk; a replenish swap after each use, off the fill path |
| (b) Warm Jupiter legs for the top K quote mints | none for those K; the rest go on-chain as today | S+1 for those K | about 1.1 funded calls/s per mint at today's 1 s refresh (0.2/s at 5 s, with older legs); K=3 covers 8 of 26 sends |
| (c) Promote the on-demand worker to the funded key | 410-470 ms build | about S+3, no better than C1/C2 | funded calls per buy; not recommended |

**(a) in detail:**
- Hold a target balance of each quote token in the warm set.
  - The set: the top quote mints by the last 7 days of armed-wallet handoffs.
  - The balance: one clip each, the current Solana clip.
- The lane builds quote→coin on the coin's pool with that inventory. CPMM and LaunchLab builders
  already take a quote amount. pump AMM and DBC with a non-SOL quote follow the same shape.
- After a fill, a replenish swap (USDC→quote, Jupiter, funded key) runs on its own thread. It never
  runs on the fill path, and never while an entry holds the wallet lock.
- The existing quote-residue sweep (`MEMECOIN_QUOTE_RESIDUE_SWEEP`) sells a quote token that drops
  out of the set.
- Cost at the census rate: 26 sends in 4.8 days, so about 5 replenish swaps a day. The held value
  is the warm-set size times the clip, for example 5 tokens × $50 = $250 of quote-token exposure.
- Needs the operator's yes on the set size, the per-token balance, and holding those tokens.

## 4. Build order (each step default-off, each with its own canary)

- **B1. pump AMM handoffs (4 sends): the smallest change.**
  - The lane already builds pump AMM.
  - These 4 were handed off for `pump_quote_not_sol` (non-SOL quote, D1) or `pool_unknown`.
  - Unknown pools go to the miss learner (it exists for the lane's own AMM buys); non-SOL quotes
    wait for D1. Target S+1.
- **B2. Meteora DAMM v2, SOL-quoted (5 sends, p50 S+4): a new builder, independent of D1.**
  - It spends the lane's existing USDC→SOL leg.
  - The swap instruction's accounts come from the pool account (vaults, mints, token programs),
    cached the way the CPMM pool cache does it.
  - The routed pairer decodes the source's DAMM v2 swap event for the post-trade price.
  - Min-out is the lane's price floor: source price × (1 + chase cap). Like the other builders, it
    is never a provider quote.
  - Live sends are capped per hour at first, as CPMM's were (`MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR`).
  - Target S+1.
- **B3. D1 inventory, for CPMM, LaunchLab, DBC and pump AMM with a non-SOL quote (24 sends).**
  - The warm set, the replenish thread, and the builders' inventory funding mode.
  - Target S+1.
- **Not now:** DLMM, CLMM and Whirlpool as COIN pools. They carry 147, 46 and 8 handoffs but 3
  sends in all. The volume there is refused by gates, not lost to latency.

Projected S-offset after each step:
- today's 35 sends land at p50 S+4;
- after C1/C2, about S+3 for every handoff (estimate, read per attempt);
- after B1 and B2, 9 of 35 at the lane's S+1;
- after B3, about 33 of 35 at S+1.
- These are targets, measured per step from `slot_receipt.source_slot` and landing.

## 4b. B2 as built (lambda/routed-b2; Gamma on 87a4a017c)

- **Why the pool is learned.** DAMM v2 emits its swap event by self-CPI, not in the logs. The
  processed socket sees only that cp-amm ran (a `DammV2Seen` marker), never the pool or the amounts.
  - The coin's pool is learned on the pool cache's own thread, from the first sighting's confirmed
    transaction: its swap instruction names the pool. Then the pool account and both mints are read.
  - That first buy is handed off as today.
- **Price basis (Gamma LOW).** The min-out is the price floor at the source's ROUTED average (USDC
  spent / tokens received). That average includes the source's aggregator and USDC→SOL leg costs. So
  it sits a little above the DAMM pool's own price and loosens the cap by those fees.
- **The premium is read after the fact.** Gamma's pre-registered premium compares our receipt
  average against the source's pool-level average. The latter is not in the logs, so the grader
  reads it afterwards from the source's confirmed transaction (inner instructions or token
  balance deltas).
- **Token-2022 guard.** The coin's extensions are the only Token-2022 guard on this path; the slot
  lane runs no token-safety simulation.
  - A coin with an extension the builds do not cover is handed off and never built: a permanent
    delegate, a transfer hook, a pausable mint, anything outside transferFeeConfig, metadataPointer
    and tokenMetadata.
  - It is checked at conversion (`damm_slot_buy`) and again at admission and build. Tests cover
    each extension, and the ignore-refusal mutants are killed.
- **The shadow simulation host.** It runs on `ONCHAIN_WATCH_HELIUS_RPC_URL`, the slot lane's own
  watch key, the same host CPMM's and LaunchLab's shadow simulations use.
  - One `simulateTransaction` per shadow build, never while the lane thread handles a buy.
  - It is never the custody RPC.
  - It is a runtime read on the lane's own key, not a research read, so rule 0724bc08's research
    hosts do not apply.
  - Pool learning (one `getTransaction` and two account reads per new coin) runs on the custody RPC,
    the client CPMM's pool cache uses.

## 5. Edge cases (CLAUDE.md rule 5), for B2 and B3

1. **Money path: yes.** A new builder and a new funding mode on the lane's funded entries.
   - Every lane gate, the reservation, the funded contract, the price cap and the exit rails are
     unchanged.
   - The inventory is a holding the operator approves. It is never sized from trial data.
2. **Speed.** Nothing new on the fill path:
   - B2 reads cached pool facts and the source's event;
   - B3 spends inventory already held;
   - the replenish swap runs after the fill, on its own thread, outside the wallet lock.
3. **Concurrency.**
   - Inventory is reserved like the native SOL buffer: the reservation records the quote amount,
     and a second entry cannot spend the same tokens.
   - Replenish never runs while a reservation holds that quote token.
4. **Absent versus zero versus unknown.**
   - An unread inventory balance is unknown: the lane refuses that buy, and it goes on-chain as
     today. A balance of zero is a refusal too.
   - An unknown DAMM v2 pool is learned off the lane thread; the first buy on it hands off.
5. **Dependencies.**
   - A replenish failure leaves the token short. The next buy on that quote hands off, and the
     failure is alerted. It is never retried on the fill path.
   - A Jupiter 429 on replenish backs off.
6. **Units.**
   - Quote tokens carry their own decimals and Token-2022 transfer fees; the CPMM `quote_facts`
     already handles both.
   - The price cap is in USD, so the quote token's USD price at entry comes from the last
     replenish fill, aged and bounded like the leg price.
7. **The sample.** The census counts handoffs of armed wallets under today's gates. A venue's share
   moves with the roster, so it is re-read before B3's set is fixed.
8. **Rollback.** Each step has its own knob; set to 0, the buy is handed off as today. Held
   inventory stays in the wallet until the residue sweep or the operator sells it.

## 6. What I need

- **Alpha:** GO on B1 and B2. They need no operator decision.
- **The operator:** D1. Yes or no on holding quote-token inventory, and if yes, the set size and
  the per-token balance.
- **Gamma:** a pre-registered read per step: the S+0/S+1 share and the fill rate per venue, before
  and after.
