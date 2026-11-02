# Slot lane on every Solana venue: design (lever 1)

Operator order 2026-09-27 via Alpha: "fast fills on ANY venue, slowness is unacceptable", and
"don't stop until we fill on the same slot or we are capable of reliably, any chain, any venue, any
launchpad". This document covers lever 1: native slot-lane builders for the venues the lane cannot
build today. Status: DESIGN. No money-path edit before Alpha's GO.

## 1. What the census says (scripts/research/venue_census.py)

Window 2026-09-20T21:59Z to 2026-09-27T22:02Z (7.0 d). CENSUS v2 (0ea51cdb) labels a buy by the pool
its COIN came from: the owner of the account with the largest outflow of the coin mint. v1 used the
first DEX program in log order, which on multi-hop routes is the first hop, not the coin's pool;
v1's table is withdrawn.

CORRECTED 2026-09-28: v2's buys/day counted UNARMED wallets too. The census universe took every
watcher detection, and the watcher watches more wallets than are armed; only the J7 events had the
"wallet not armed" filter. Armed-at-buy (from the memecoin_wallet_approvals timeline) equals "has an
entry attempt" on every venue: every armed buy was judged by some lane, and no unarmed buy was. The
first column is now armed buys/day, with v2's all-detections number in brackets; reserved and fills
are unchanged (all armed). venue_census.universe filters to armed-at-buy by default since then.

| venue (v2) | armed buys/day (all) | reserved (7 d) | fills (7 d) | decision->fill p50 / p90 ms |
|---|---:|---:|---:|---|
| Raydium CPMM | 52.2 (105.0) | 45 | 25 | 2290 / 3217 |
| PumpSwap | 55.0 (100.6) | 59 | 29 | 1784 / 2975 (4 fills via the slot lane) |
| Meteora DLMM | 35.1 (46.5) | 2 | 1 | 2283 |
| pump curve | 35.4 (42.4) | 58 | 21 | 807 / 1962 (12 via the slot lane) |
| Raydium CLMM | 11.2 (14.7) | 4 | 2 | 2401 / 2814 |
| Raydium LaunchLab | 5.7 (8.4) | 25 | 16 | 2405 / 3693 |
| Meteora DAMM v2 | 3.7 (6.1) | 9 | 4 | (section 10) |
| Meteora DBC | 3.7 (4.0) | 8 | 1 | (section 10) |
| Orca Whirlpool | 2.6 (2.8) | 5 | 2 | 1627 / 1776 |
| GatorSwap (gatorLx9aC1e5...) | 0.9 (1.0) | 0 | 0 | not buildable: a proprietary AMM under the DFlow router, upgradeable, no public IDL |

Three facts decide the design:

1. The slot lane sends 17-37 ms after its decision. The general path takes 1.1-1.6 s p50. Confirmation
   after the send is the same on both (~0.6-0.8 s), so the whole gap is before the send.
2. The general path's time (filled attempts, p50): market data ~230-280 ms (one DexScreener GET),
   reservation ~280 ms (the gate reads; B, df9985d4, now hides most of it: 8.9 ms on the first live
   row), Jupiter quote 90-300 ms, build + sign 340-690 ms (DLMM 686 ms).
3. 98% of source buys are ROUTED: the venue program runs under CPI from a bot router or an
   aggregator. A builder cannot copy the source's instruction; it builds its own swap on the pool.

## 2. Venue order

Alpha's order (22:0xZ): Raydium CPMM first as the PROVING build (static accounts, reserves in the
source's own SwapEvent), then Meteora DLMM (the biggest flow, 84/day), then Raydium CLMM and Orca.
Difficulty:

| venue | pool identity at processed | state a build needs | exact output |
|---|---|---|---|
| Raydium CPMM | SwapEvent in the logs (pool, vault balances before) | pool state (static accounts) + vaults | constant product; vault balances hold accrued fees, so reserves = vault - protocol/fund/creator fees from the pool state |
| Raydium CLMM | SwapEvent in the logs (pool, post sqrt price, tick) | pool + the tick arrays the swap crosses | concentrated-liquidity step math |
| Orca Whirlpool | to verify on a live frame | pool + tick arrays | concentrated-liquidity step math |
| Meteora DLMM | NOT in the logs: Swap is emitted by self-CPI (inner-instruction data) | lb_pair + bin arrays around the active bin + oracle | bin walk with base + variable fee |

## 3. Design for one venue (written for DLMM, the hardest; CPMM in section 6)

Signal. The lane's routed pairer already sees, at processed, the owner's coin going up and USDC or
SOL going down in one slot, plus the owner's logs frame for that slot, which names the venue
program. For DLMM the frame does not name the pool. Two ways to get it, both to measure:

- (a) a mint -> DLMM pool cache, learned from our own tape and pool bindings and refreshed by an
  account subscription. A fresh coin's first buy misses the cache.
- (b) a full-transaction stream at processed (Helius transactionSubscribe, which Gamma probed on
  e1f95763: +0.25 s over J7 at p50, 50 ms after the slot is first seen). The transaction carries
  the pool and the swap event. It puts a paid key on the funded path: operator call.

State. lb_pair (active bin, bin step, fee parameters, reserves, mints) and the bin arrays around the
active bin, warm through an account subscription for pools in the cache, else one
getMultipleAccounts at decision (~50-100 ms on the funded read lane).

Exact output. Port the DLMM swap math (bin walk, base fee, variable fee from the volatility
accumulator). Acceptance: replay N >= 30 real source buys from the census (pre-trade state + amount
in) and predict each actual amount out to the unit; no build ships before this holds.

Build. DLMM swap2 with our own token accounts, a min-out from the exact output and the lane's
slippage model, the USDC -> SOL leg the lane already prefetches when the pool quotes in SOL. Every
instruction is checked against a live frame, and every new build shape is simulated on chain
(research key, flat book) before it is trusted.

Admission. The same in-memory subset the lane uses for pump (armed on solana, kill switch, mcap
ceiling from price x supply, source-buy floor), then reserve_entry. No DexScreener on the path.

## 4. Rule 5 (for the DLMM build; repeated per venue)

1. Money path: yes, entry. It adds a new funded entry route on the slot lane.
2. Speed: removes the Jupiter quote and build (p50 ~430-990 ms combined) and the DexScreener GET;
   adds one state read when the pool is not warm. Measured p50/p90 in shadow before live.
3. Concurrency: the pool caches are shared by the lane thread and the refresher; single-writer
   holders as the PumpSwap cache does today. The wallet lock and one-per-coin stay as they are.
4. Absent/zero/unknown: an unknown pool, stale state or unreadable bin array is a refusal handed to
   the on-chain lane (today's path), never a guess. A zero bin is a real zero.
5. Dependencies: read-lane timeout or rate limit -> hand-off; a wrong answer is the main risk,
   hence the unit-exact replay gate and on-chain simulation of every shape.
6. Units: bin prices are raw-unit ratios; token decimals from the mint; lamports vs SOL vs USDC
   atomic kept separate as the pump builders do.
7. Sample: the census window above; after live, a per-venue decision->send and S+k landing table.
8. Rollback: per-venue flag (MEMECOIN_SLOT_LANE_VENUES), default shadow; shadow builds, simulates
   and logs predicted vs actual without sending.

## 5. Plan

1. Capture live frames: 30+ DLMM source buys from the census (research key, flat book).
2. Decoder + swap math offline, unit-exact on those frames (pytest on real frames).
3. Builder offline; simulateTransaction on the research key for each shape.
4. Shadow in the lane: build + simulate + log, no send.
5. Live behind the flag after Alpha's review and the operator's yes.

## 6. Raydium CPMM, the proving build (Alpha's order)

Verified on three live source buys (research key, 2026-09-27 22:04Z; frames kept for the tests):

- The source's logs carry `SwapEvent` as `Program data:` under `CPMMoo8L...invoke [2]`, discriminator
  `40c6cde8260871e2` (sha256 "event:SwapEvent"), 170 bytes: pool (32), input_vault_before,
  output_vault_before, input_amount, output_amount, input_transfer_fee, output_transfer_fee (u64
  each), base_input (bool), input_mint (32), output_mint (32), trade_fee, creator_fee (u64),
  creator_fee_on_input (bool). The pool, both mints and the pre-trade curve reserves are in the frame
  the lane already receives; no transaction fetch is needed.
- The swap is `swap_base_input`, discriminator `8fbe5adac41e33de`, data amount_in u64 + min_out u64,
  13 accounts: payer, authority (GpMZbSM2...), amm_config, pool_state, input token account, output
  token account, input vault, output vault, input token program, output token program, input mint,
  output mint, observation state.
- Output, unit-exact on 3 of 3: net_in = amount_in - input_transfer_fee - trade_fee - creator_fee
  (creator fee on input); out = floor(output_vault_before * net_in / (input_vault_before + net_in)).
  The fee split fits: total = ceil(amount x (trade_rate + creator_rate) / 1e6), creator =
  floor(total x creator_rate / (trade_rate + creator_rate)), trade = total - creator. Rates seen:
  trade 10000 or 2500 ppm, creator 500 or 10000 ppm.
- Output transfer fee (Token-2022), on the 83 census swaps: 3% on 56, 1% on 16, none on 11, rounded
  up (exact on 72 of 72). The first three frames happened to be 1%, which an earlier draft of this
  line generalised. The min-out the program checks is after that fee.

Design. Signal: the routed pairer's owner balance change (coin up, quote down, one slot) paired with
the CPMM SwapEvent for that coin in the owner's logs frame, input mint SOL or USDC. Price: post-trade
reserves from the event (in_before + net_in + the LP share of the trade fee; out_before - out).
Static pool accounts (amm_config, vaults, programs, observation) from pool_state, read once per pool
and cached; amm_config rates cached per config. Build: swap_base_input on our clip with the lane's
prefetched USDC -> SOL leg for WSOL pools (as PumpSwap does), Token-2022 output ATA from the pool's
token program; min-out = exact output after the transfer fee x (1 - lane slippage). Shadow first:
MEMECOIN_SLOT_LANE_CPMM = off | shadow | live, default off; shadow builds, simulates on the read lane
and logs predicted output and decision->build ms, never sends.

Acceptance before shadow: the decoder and the math on >= 30 real CPMM source buys, every output
predicted to the unit from the event alone; the post-trade reserve rule checked against the next
trade on the same pool wherever the census holds two; one built transaction simulated clean on chain.

## 7. Raydium CPMM live mode: one source buy, at most one funded attempt (design, 2026-09-27)

Status: DESIGN, for Alpha's read after the shadow data and the operator's yes. Today CPMM is in
SHADOW_ONLY_VENUES and the routed hand-off writes every routed buy for the on-chain lane first.

How the pieces already dedup a source buy (read from the code, not assumed):
- The on-chain lane judges a detection row (routed, watcher or hand-off; INSERT OR IGNORE by
  signature) only after `memecoin_onchain_lane.slot_lane_outcome(signature)`: `filled`,
  `in_flight` and `decided` defer to the slot lane's `slotlane:<sig>` attempt; `failed` (a
  reservation released on a CONFIRMED failure, or a scaled-clip refusal) allows ONE ordinary
  attempt; None (no slot attempt row) is the ordinary path.
- The slot lane writes no ledger row for a refusal before its reservation (the J7 lane records
  its own verdict on the same buy); from the reservation on, the attempt row exists.
- `route_routed_buy` already implements the pump rule (Lambda, MEMECOIN_SLOT_LANE_ROUTED_PUMP): a
  pump buy the LIVE lane takes is not written for the on-chain lane.

The CPMM rule, the same shape:
1. Routing. When the CPMM conversion succeeds and CPMM is live for that quote asset, the buy goes to
   the lane only; the routed row is not written. Anything else: routed row first, as today.
2. A refusal before the reservation (admission, stale input, signal age, leg, build, pool miss
   that the MissLearner could not retry in its budget) writes the routed row at once
   (RoutedHandoff.write on the RoutedBuy the CPMM SlotBuy carries), so a refused buy reaches the
   on-chain lane exactly as it does today, late by the lane's decision time only (<= 0.5 s by the
   signal-age gate). The generic Handoff cannot do this: it sizes PumpSwap buys only and returns
   `skipped_unsized` for a CPMM buy.
3. From the reservation on, the attempt row decides through slot_lane_outcome. A `failed` outcome
   gets its one ordinary attempt from the watcher's later row (the watcher classifies USDC spends);
   no second routed row is written.
4. J7 and every other lane still meet the one-pending-reservation-per-coin rule, so the lane's
   reservation refuses their duplicate for as long as it is open.

What decides the min_out our S+1 send carries (unchanged from shadow; `capped_min_out`, the pump
lane's rule):
- expected = our exact output (after the coin's transfer fee) on the pool AS THE SOURCE LEFT IT at
  S: the event's pre-trade vault balances plus the source's net input minus the protocol/fund/
  creator counters (post_trade_reserves), our own fees from the pool's config.
- output floor = expected x (1 - MEMECOIN_REAL_CHASE_MAX_PCT/100); price floor = the tokens our USDC
  cost buys at the source's own post-trade price x (1 + cap/100). min_out = the larger; a floor above
  expected refuses before any reservation. The cap is read from the running process's settings,
  never from this document.
- The program checks min_out AFTER the coin's transfer fee (simulated: net passes, net + 1 fails
  6005). A buy landing after another trade at S+1 or later gets less; below min_out the whole
  transaction fails, the leg included (one atomic transaction), and costs the fee only.
- No slot guard: a CPMM send can land at S+2 or later; min_out is the only protection, as it is for
  the general path today.

Leg residue, per quote asset:
- USDC: none (the clip is the input).
- SOL: the wSOL leg delivers >= min_out lamports, the swap spends min_out, CloseAccount returns the
  rest as native SOL (gas money, never stranded).
- ZEC: the rest stays as ZEC in custody (up to the leg slippage, 1.5% of the clip). Blocker below.

Rollback: MEMECOIN_SLOT_LANE_CPMM back to `shadow` in the runner's environment and a restart (or the
per-quote live list emptied): route_routed_buy writes the routed row first again, the venue is back
in SHADOW_ONLY_VENUES. No schema change. Half-applied (a restart mid-attempt): an attempt row
already written finishes under the lane's reservation and pending-entry rules; the watcher's later
row is judged by slot_lane_outcome, so it cannot become a second funded attempt.

THE COHORT READ Alpha takes to the operator (pre-registered 2026-09-27, before any data):
`python3 scripts/research/slot_lane_shadow_read.py --since <the deploy carrying the CPMM
simulation>` over the verifier lines. Live is asked for only when, on SOL- and USDC-quoted CPMM
builds:
- >= 20 builds were simulated (each once, never sent, at a slot after S: a stricter landing test
  than our S+1 send);
- >= 80% would have landed (no error with our own min_out at the simulated slot);
- our predicted gross equals the simulated one to the unit on every landed build whose fresh read
  found the pool unchanged since S (a miss there is a model defect and blocks);
- every failure is named (slippage 6005, or listed individually).
Below 20 builds the answer is "not enough data", not a rate.
Also reported (Alpha, pre-registered the same night, before any data): live CPMM must be FASTER, not
only correct. For every would-have-landed build, the same source buy's funded fill on whichever lane
filled it (joined by the line's sig=): the lane's receipt -> built ms next to that fill's receipt ->
fill ms, and the fill's landing slot minus S (`--slots`, research key, flat book). If the existing
lanes already fill most landed builds at or before S+2 (within one slot of the lane's S+1), the read
says so: CPMM live would then add speed only on the margin. Landed builds with no fill are where
live adds fills.

THE FILL UPSIDE, the other half of the case (2026-09-28; read only). Census v2's CPMM and LaunchLab
coin-side source buys (7.03 days to 2026-09-27 22:02Z), each with the LEDGER's own verdict on the same
source buy, split by the quote asset the source's own venue swap spent (CPMM: the SwapEvent into the
coin in each source transaction, 738 of 738 read on the research key, flat-gated; LaunchLab: the
pool's quote mint).
ARMED-AT-BUY ONLY (corrected 2026-09-28: a first version counted the census's unarmed detections
too, 390 CPMM and LaunchLab buys that are not a lane input).
| venue | quote | armed buys (/day) | refused: mcap ceiling / source-buy floor / chase cap / one per coin / cooldown / other gate | passed the gates | of which filled | decision -> fill p50 / p90 ms |
|---|---|---:|---|---:|---:|---|
| CPMM | SOL/USDC | 25 (3.6) | 13 / 1 / 1 / 1 / 3 / 2 | 3 | 3 | 1971 / 2602 |
| CPMM | exotic | 342 (48.6) | 230 / 31 / 16 / 12 / 8 / 17 | 27 | 24 | 2309 / 3357 |
| LaunchLab | SOL/USDC | 2 (0.3) | 0 / 0 / 0 / 0 / 0 / 0 | 2 | 1 | 2651 (n=1) |
| LaunchLab | exotic | 38 (5.4) | 1 / 13 / 8 / 0 / 1 / 0 | 15 | 15 | 2296 / 3727 |
(1 CPMM buy in each quote group is unclassified: an attempt with no gate reason and no reservation.
Every armed buy has an attempt row: none is unjudged.)

How the columns are read:
- armed = the wallet was armed on Solana at the buy (memecoin_wallet_approvals timeline);
- refused = the first gate named in terminal_reason;
- passed = a reservation, or a fill, with no gate refusal (a reserved attempt that then failed in
  execution counts as passed, not filled);
- decision -> fill = the filled attempt's decision_started_at to its position's opened_recorded_at.

Writers:
- memecoin_entry_attempts: the row, origin_event_id and decision_started_at by
  memecoin_entry_trace.EntryAttemptTrace.start; reservation_completed_at by that trace's reservation
  mark; terminal_status and terminal_reason by db_memecoin.finish_entry_attempt.
- memecoin_positions.opened_recorded_at and attempt_id: db_memecoin.open_position at fill booking.
- The venue label: venue_census.py fetch2 (the owner of the coin's largest outflow in the source
  transaction's own token balances).
- The CPMM quote: raydium_cpmm_buy.swap_events on the same transaction's logs.
The verdicts are the gates AS THEY STOOD that week, not today's settings.

What it says:
- The exotic groups are the volume. CPMM exotic passed 27 and filled 24 (1 in 13 armed buys);
  LaunchLab exotic passed and filled 15 (39% of its armed buys). SOL/USDC is small on both venues.
- Every fill took 2.0-3.7 s on the slow path. The lane's decision -> send is 17-37 ms (census), plus,
  for an exotic quote, the on-demand leg (not yet measured live).
- There is no coverage hole: every armed buy was judged by some lane. (The first version of this
  table read 350 CPMM exotic buys "never judged by any lane" as upside; all 350 were unarmed wallets'
  buys the watcher sees, before the routed lane existed.)
- Upside if live, per week: speed on the 43 buys that filled on the slow path (3 + 24 + 1 + 15) and
  on 4 more that passed the gates and failed in execution. None of it is a PnL claim: whether faster
  fills earn more is the cohort read's second half.

BUILT with the live list empty (2026-09-27, (ii)): raydium_cpmm_buy.CPMM_LIVE_QUOTES (empty in code,
pinned) and cpmm_live_for(quote); slot_lane.shadow_only(buy) replaces the venue-set check;
route_routed_buy sends a live-quote CPMM buy to the lane only; SlotLane._routed_fallback writes the
routed row for a live CPMM buy refused before any reservation (never after one, never for a pool
miss the MissLearner took, whose own hand-off writes it); a test drives a CPMM buy through the
lane's live path (real ledger, fake executor) and checks every validity field the executor reads.

Exotic quotes (later, not now): the on-demand leg of LaunchLab phase (b) applies to CPMM too; the
first live case was ANTHROPIC-quoted (ResellCalendar, AjxqTiS6, via HumidiFi), and since 4682e8ca the
shadow line names every unsupported quote so the reader can count them.

Blockers before live, each with its check:
- ZEC residue: the ZEC leg delivers outAmount but the swap spends min_out, so up to the leg
  slippage (1.5% of the clip) stays as ZEC in custody per fill. ZEC pools stay shadow while CPMM is
  live on SOL/USDC, until an ExactOut leg (Jupiter swapMode=ExactOut for the swap's exact input) or
  a sweep exists.
- The post-trade reserve rule is unverified: the shadow verifier's reserve_in_delta /
  reserve_out_delta against fresh reads must be 0, or explained by a later trade on the pool
  (fresh_slot > S), on >= 20 events.
- Simulation: DONE in the commit carrying this section. The CPMM verifier simulates each build once
  on the lane's watch key (2 s timeout, lane-idle gate, the error decides, only our pool's event is
  read) and logs sim_ok / sim_out / gross / sim_slot for the cohort read above.
- Coin extensions: CPMM admission refuses any coin extension outside transferFee/metadata (as
  LaunchLab's coin_facts); a transfer hook needs accounts the build does not pass.
- The executor: the lane's live path (reservation, sign, send, balance-delta accounting) was
  written for the pump builders; the CPMM build's validity dict must carry every field the
  executor and the trace read. One test drives a CPMM buy through `_traced` with a fake executor.
- The at-most-one test: for every lane outcome class (refused before reserving, retry exhausted,
  reserved then failed, reserved then filled, submitted_unknown) the lane side produces exactly
  one of {attempt row, routed row}, and a watcher row for the same signature is judged by
  slot_lane_outcome as above.
- Measurement: decision -> signed on the lane against the general path's decision -> fill on the
  SAME source buys; live only if clearly faster.
- Live flag: MEMECOIN_SLOT_LANE_CPMM gains a real `live`, per quote asset; SHADOW_ONLY_VENUES
  becomes the set of venues whose mode is not live. The operator's yes, then Alpha's deploy.

## 8. Raydium LaunchLab (2026-09-27; 0750a0ed, 6e3b0fd9, 999318c8)

59 census buys, all routed buy_exact_in; TradeEvent (147 bytes) in the owner's logs frame.
Math exact on 59 of 59 (raydium_launchlab_buy.py docstring); on-chain simulation exact on 5 live
pools (SOL, ZEC, NVDAx, GP with a 3% quote fee, ANTHROPIC with a changing fee schedule).
31 distinct quote mints over 59 buys (55 on one platform config); SOL, USDC and ZEC cover 9 buys
and 2 of the 16 fills. Phase (a), shadow: those three quotes, the build simulated once per event
off the lane thread. Phase (b), GO'd shadow-only: an on-demand USDC -> quote leg on a bounded
worker (at most 2 in flight, never on the lane thread), ExactOut where Jupiter routes it, measured
p50/p90 against the general path on the same buys, Token-2022 quotes refused unless simulated.

### 8.1 The quote-residue sweep (design only, 2026-09-27; no code yet)

What it is for. An ExactIn leg (USDC -> quote asset) delivers the quote's outAmount, and the venue
buy spends only the leg's minimum, so the difference stays in custody as that quote asset: up to the
leg slippage (1.5% of the clip) per fill. That covers ZEC on CPMM and the 22 exotic LaunchLab quotes
(b). ExactOut would avoid it, but it routes for 12 of 29 census quote mints and for none of the
xStocks, Backpack stocks or PreStocks, so the answer is ExactIn plus a sweep. SOL has no residue
problem: the wSOL account is closed in the fill and the rest is gas. USDC needs no leg.

When it exists. Only once a venue with a non-SOL ExactIn leg is LIVE. In shadow nothing is bought,
so nothing is left. Flag MEMECOIN_QUOTE_RESIDUE_SWEEP (proposed), default off.

Source of truth: the fill receipt, never the quote.
<!-- schema:external=memecoin_quote_residue -->
- At fill booking, the executor already reads the entry's balance deltas. It appends one row per
  fill with a non-zero residue to a new append-only table `memecoin_quote_residue` (proposed):
  network_id, quote_mint, residue_atomic (our quote account's post minus pre balance), attempt_id,
  entry_sig, recorded_at, and swept_by (null until a sweep claims it).
- The sweep sells at most the recorded, unswept residue of a mint, capped by the live balance:
  min(recorded - swept, balance). It NEVER sells a whole balance. A quote mint can also be a coin
  the runner copies (Fartcoin, retire, JUP, APE and PENGU are census quote assets and memecoins).
- It skips a mint with an open or reserved funded position. An exit sells the exact whole balance,
  so it may take the residue with it; the next sweep then finds balance < recorded and marks the
  shortfall swept by that exit, never re-selling.

Trigger and threshold.
- A cycle on the runner's maintenance cadence (every 10 minutes, proposed) considers the mint with
  the largest unswept value. It sweeps when that value is >= MEMECOIN_QUOTE_RESIDUE_SWEEP_MIN_USD
  (proposed $5: fee and priority are ~$0.001-0.01 and route slippage <= 1%, so $5 clears it with
  margin), or when the oldest unswept row is older than 7 days and worth >= $0.50. The second rule
  stops pausable or permanent-delegate issuer risk from being carried forever.
- The value comes from the sweep's own quote -> USDC ExactIn quote, never a separate price feed.

Off the fill path. Rules:
1. Never while an entry holds the wallet lock. The sweep takes wallet_exec_lock(key, blocking=False),
   skips the cycle if the lock is held, and holds it across quote, sign, send, confirm and the
   balance-delta booking, as an exit does. Balance-delta accounting of a concurrent fill would
   otherwise count the sweep.
2. Quiet window only. It starts only when the slot lane has handled no buy for 30 s, no reservation
   is pending and no exit is in flight. Its lock time (~1-2 s) can still overlap a source buy: the
   lane then refuses slot_lane_wallet_busy. That is the design's one cost, and it is counted.
3. One mint per cycle. No multi-mint transaction (packet size, and one balance delta per booking).
4. The funded Jupiter seam is allowed here: it is a funded action, taken only once a venue is live.
   It gets its own role label (quote_sweep) in the load counter. A 429 or 401 ends the cycle without
   a retry, and the next cycle is 10 minutes away.
5. Write-ahead identity, as a withdraw intent: an intent row with sig and last_valid_height before
   any network I/O. A crash after send is ambiguous: the next cycle reconciles by balance and never
   resubmits signed bytes.

Accounting. Proceeds (the USDC delta) are booked on the sweep row. Position PnL keeps the full clip
as cost (conservative; residue is not attributed back to a position). A daily line: residue created
vs swept vs outstanding, per mint.

Failure modes. A paused mint (xStocks carry Pausable) fails the transfer: skip, count, retry next
cycle, page after 3 in a row. A quote transfer fee (GP, ANTHROPIC 3%) is paid by the sweep, and the
threshold covers it. Permanent delegate: the issuer can take the residue; accepted, and bounded by
the 7-day rule. No route: skip, and page at the 7-day age.

Rule 5.
1. Money path: yes, a funded sell; live venues only, flag off by default.
2. Speed: never on the fill path; it costs a wallet-busy refusal only when a buy lands inside its
   ~1-2 s lock, in a window chosen to be quiet.
3. Concurrency: the wallet lock serialises it with entries and exits.
4. Absent vs zero: an unreadable balance skips the cycle; it is never read as zero.
5. Dependencies: route, 429 and pause each skip the cycle.
6. Units: quote atomic per mint decimals.
7. Sample: fills with a recorded residue.
8. Rollback: the flag. The residue table keeps recording either way.

Checks to leave with the build: the sweep never runs while the wallet lock is held; never sells more
than recorded; skips a mint with an open position; reconciles an exit that took the residue; a 429
ends the cycle without a retry.

BUILT (2026-09-28, sniper/services/memecoin_quote_residue.py, flag MEMECOIN_QUOTE_RESIDUE_SWEEP off).
No new sell path: the sweep only ENQUEUES the operator's reviewed holding sell
(memecoin_other_holdings, memecoin_holding_sell_intents), which runs only on a flat Solana book,
under the wallet lock taken non-blocking, with the protected mints re-read inside the lock,
write-ahead and chain-settled. That sell takes the whole balance, so the sweep enqueues a mint only
when the other-holdings snapshot (the Solana meta row's observed_at, newer than the mint's newest
residue row) shows no more of it than the recorded unswept residue x1.02; above that it logs
foreign_balance once and never enqueues (Alpha: an operator's own JUP, Fartcoin, PENGU or APE is not
residue). Rows live in memecoin_quote_residue; a failed or expired intent frees them again. The
recorder (record_fill_residue, residue_from_receipt) is wired in the first exotic-quote live commit.

## 9. Raydium CLMM: findings, PARKED (2026-09-27, Alpha)

Parked because v1 would cover about 2 buys/day on a venue with 2 fills in 7 days. The findings stand
for a later build. Every read was on the research key, flat-gated.

Sample: 103 census v2 coin-side buys (79 armed at the buy) over 20 coin pools; the frames hold 120 CLMM swaps with
router hops included, all at CPI depth 2. Instructions: swap_v2 (2b04ed0b1ac91e62, 83) and swap (37),
with 11-18 accounts; the remaining accounts are the bitmap extension PDA
["pool_tick_array_bitmap_extension", pool] plus tick arrays ["tick_array", pool, start_index i32 BE]
in traversal order.

The program is the NEW CLMM: limit orders, dynamic fees, a fee_on flag. There is no on-chain IDL;
the published one is raydium-io/raydium-idl raydium_clmm/raydium_clmm.json.
- SwapEvent: the IDL's 205 bytes (pool, sender, two token accounts, amount_0, transfer_fee_0,
  amount_1, transfer_fee_1, zero_for_one, sqrt_price_x64 u128 POST, liquidity u128 POST, tick i32
  POST), then two more u64 the IDL does not list: fee_0 and fee_1, the trade fee charged in each
  token. It is 221 bytes, with the same discriminator as CPMM's SwapEvent, so it is attributed by
  the invoke stack.
- PoolState: 1544 bytes, packed. amm_config @9, mint_0/1 @73/105, vault_0/1 @137/169, observation
  @201, decimals @233/234, tick_spacing @235, liquidity @237, sqrt_price_x64 @253, tick_current @269,
  status @389, fee_on @390, tick_array_bitmap @904 (bit i = array index i - 512), dynamic_fee_info
  @1096 (dynamic_fee_control u32 @1102). Checked: every pool's sqrt price lies inside its tick.
- AmmConfig: 117 bytes. protocol_fee_rate @43, trade_fee_rate @47 (ppm), tick_spacing @51,
  fund_fee_rate @53.
- Fees: on the census buys the fee is on the INPUT. fee_on=1 (token0) always coincided with token0
  as the input; fee_on=0 means input. A fee_on that names the output token was never seen and must
  refuse. Dynamic-fee pools (4 of 29; 15 buys) charge above their base rate (1816-2090 ppm on a 1800
  base): refused until modelled.

Math: single-range, Q64.64, the Uniswap v3 port's rounding.
- less_fee = floor(amount x (1e6 - rate) / 1e6).
- token0 in: next = ceil(L<<64 x P / ((L<<64) + less_fee x P)).
- token1 in: next = P + floor((less_fee << 64) / L).
- in-step = delta rounded up; out = delta rounded down; fee = amount - in-step.
It reproduces 34 census trades EXACTLY, output and fee, solving for the pre-trade price from the
event's post-state. The other 71 do not fit one range, which is consistent with tick crossings
(for example a fee 1 unit over, from per-step rounding).

On-chain simulation (scratchpad script, not in the repo): 6 of 6 live pools exact to the unit.
- Pools: USDC 0.1% fee_on 0 x2; SOL 1% fee_on 1 x3, two of them with a 3% Token-2022 coin; SOL 1%
  fee_on 0.
- The build used the interval boundary as sqrt_price_limit_x64, so it can never cross a tick, and
  the first initialized tick array from the pool bitmap plus the extension PDA.
- In each case: min_out = predicted net passes; net + 1 fails TooLittleOutputReceived 6018. The
  check is after the coin's transfer fee.

Coverage, and why it is parked:
- Coin-side quote assets: WBTC 42, SOL 25, RAY 14, USDC 14, others 8.
- SOL/USDC with a static fee: 24 buys. From the source's post-state a $100 buy stays inside one tick
  spacing interval (no tick-array read needed) for 14 of them (in-interval capacity p10 $23, p50
  $213, p90 $529).
- v1 (single interval) is therefore about 2 buys/day.

v2 scope, if CLMM earns it:
- multi-step crossing with tick arrays (liquidity_net) and the new limit-order fills at crossed ticks;
- dynamic fees (volatility accumulator);
- WBTC and RAY quotes via the paper-key on-demand leg (LaunchLab phase (b)).

## 10. Meteora DBC and DAMM v2: the read (2026-09-28), DAMM v2 GO (shadow), DBC no build

Census v2's "other:11111111" row (71 buys over 7.03 days) is exactly two system-owned pool-authority
PDAs. The census now labels them: meteora_dbc (FhVo3mqL...) and meteora_damm_v2 (HLnpSz9h...).
Coin mints were read on the research key, flat-gated.

- DAMM v2 (cp-amm cpamdpZC..., IDL cp_amm 0.2.0 on chain): 43 buys (6.1/day; 26 armed, 3.7/day), 18 coins, 9 reserved,
  4 filled. None of the 18 coins carries a transfer hook; 37 buys are classic SPL coins and 6 are
  Token-2022. A native builder reaches coins we can hold: GO, shadow first.
- DBC: 28 buys (4.0/day; 26 armed), 8 coins, 8 reserved, 1 filled. 16 buys on 2 coins are gated by the Fomo
  co-sign transfer hook: hook programs eyesEswW (11 buys) and HoDLrkxn (5), hook authority FhVo3mqL,
  the DBC pool authority. None of them filled. HOOK AT BUY TIME: 3 of the 12 buys whose mint shows no
  hook today were refused "launchpad-gated: transfer hook eyesfsib..." when they happened. The hook
  can be cleared after graduation, so a mint read later understates the gating at buy time. The
  buildable remainder is classic SPL curves, about 1-2 buys/day. No DBC build.

DAMM v2 build findings (2026-09-28; 43 of 43 census frames, on-chain IDL cp_amm 0.2.0):
- swap (f8c69e91e17587c8) and swap2 (414b3f4ceb5b5b88); 14-15 accounts. The authority is
  HLnpSz9h..., followed by an optional referral account, event_authority and the program.
- THE SWAP EVENT IS NOT IN THE LOGS. EvtSwap2 (bd4233a826507599, 180 bytes: pool, direction,
  collect_fee_mode, params, swap_result with next_sqrt_price and each fee, transfer-fee amounts,
  timestamp, reserves after) is emitted by self-CPI through event_authority. It sits in
  inner-instruction data on 44 of 44, and in the logs on 0. The owner's logs frame carries only
  "Program log: Instruction: Swap2": the lane learns that a DAMM v2 swap happened, not the pool,
  the amounts or the post-trade state. The same class as DLMM.
- FEES ARE SCHEDULED. On the census swaps the fee is on the input (collect_fee_mode 1 on 39, 0 on
  5) at 2.0%, 3.0%, 4.0% and 4.67%, varying by pool and time. The base fee comes from a time,
  market-cap or rate-limiter scheduler (three account types in the IDL), plus a dynamic fee.
- So a build needs a coin -> pool cache and a fresh pool read at minContextSlot = S on the decision
  path (one RPC call, ~50-100 ms: rule 5.2), or the paid transactionSubscribe (the operator's call;
  it would open DLMM too); and unit-exact fees mean porting three schedulers and the dynamic fee.

## 11. The transaction-stream case (for the operator's morning read, 2026-09-28)

The question: put a Helius transactionSubscribe stream on the slot lane? It is the operator's
call: it moves a funded lane onto the research key e1f95763 (the operator's upgraded key).

What it unlocks: the two venues whose swap event the lane's logs frame never carries.
| venue | buys/day (census v2) | reserved (7 d) | fills (7 d) | where the swap event is |
|---|---:|---:|---:|---|
| Meteora DLMM | 35.1 armed (46.5 all) | 2 | 1 | self-CPI, inner-instruction data (Lambda's frames, 72/72) |
| Meteora DAMM v2 | 3.7 armed (6.1 all) | 9 | 4 | self-CPI, inner-instruction data (44/44; 0 in the logs) |
Together: about 39 armed buys/day and 5 fills a week, all on today's ~2.3 s general path. Without the
stream, a build needs a coin -> pool cache plus a fresh pool read at S on the decision path (one RPC
call, ~50-100 ms, which rule 5.2 refuses by default), and it still lacks the source's exact
post-trade state. With the stream, the transaction arrives whole: the pool and the post-trade
state are in the frame, and nothing is read on the decision path. DAMM v2 would still need its fee
schedulers ported (section 10).

What Gamma measured on e1f95763 (processed; 2026-09-27 09:48-10:33Z and 10:41-11:01Z; atlas host):
- It works on this key; the earlier -32600 "not available on the free plan" was an older key.
- Lead over J7: p50 +0.247 s, p90 +0.683 s (n = 8 buys J7 relayed, of 11). Lead over the confirmed
  watcher: p50 +0.471 s, p90 +2.316 s (n = 11). Arrival: p50 50 ms after the slot is first seen.
- Against the lane's own logs socket: the same moment (n = 1 buy, 0.2 ms apart). The stream is not
  faster than the logs frame; its gain is content: the whole transaction, with payment proof and
  any venue, and no getTransaction.
- Load: 22 frames in 20 minutes for 40 owners, excluding notdecu; 18,224 frames in 45 minutes
  including notdecu (6.7 per second).

Not measured (say so before relying on it):
- Whether the frames carry innerInstructions. The probe requested transactionDetails=full (which,
  per the Helius API, includes meta), but it stored summary rows only. The first check of any build
  is one raw DLMM or DAMM v2 frame from the stream showing the event.
- Credit cost per frame: unknown. The notdecu volume decides it.
- Delivery on the mainnet host (atlas only so far), and its behaviour over hours (45 + 20 minutes
  sampled, 0 reconnects).
- The logs-vs-stream race at scale: n = 1.

## 12. J7 Solana decision speed: what was measured, what was dropped, and the native quote (B1) — decision record (2026-09-28)

Alpha reordered the lane work: J7 carries 76% of Solana fills, and its decision->signed time is ours.
This is what we measured, what we changed and what we decided. Read-only numbers come from
`memecoin_entry_attempts` (trace_json and stage columns). Fills are J7 Solana fills since 2026-09-21
(n=105) unless a line says otherwise.

What holds a J7 event before the decision: nothing.
- Receipt->decision is p50 3 ms / p90 12 ms / max 0.70 s on the 94 first attempts.
- The 8.3 s p90 over all fills came entirely from 11 RETRIES of a refused or aborted first attempt
  (the same origin_event_id). Each was measured from the original receipt at the 10/20/30 s retry
  interval.
- The first attempts were refused by: the chase cap 4, route build 3, VolumeCapExceeded 2,
  slippage 1, Jupiter transport 1.

Decision->quote was about 643 ms p50, measured as the time since the previous stage mark:
- market_data 234 ms: the DexScreener GET.
- reservation 288 ms: no network call. It is classification reads, an attempt-fields write, the
  wallet chase-cap and first-seen reads, exemptions, preflight and reserve_entry.
- safety 30 ms.
- quote 91 ms.
L2 (`_PreBalanceReads`, MEMECOIN_ENTRY_OVERLAP_PRE_READS=1) already overlaps the pre-send balance
reads with the quote: timing_balance_wait = 0 since 09-27 about 19:00Z. Earlier traces show the
serial reads (usdc 64, token 172 ms).

Changes:
- Probes (cb61d0be, dd6ebf4b; trace only): attempt_row, probe_pre_source_floor,
  probe_conn_select1, probe_pre_classify, probe_classified, probe_pre_volume, probe_pre_leg,
  probe_leg_start, probe_pre_preflight, probe_pre_reserve. They split the reservation stage and
  the first-substage time. A substage's duration_ms counts from the previous substage, so
  compare across deploys on elapsed_ms.
- DROPPED, a mint-keyed market-stats cache. A 3 s cache would have served 0 of 105 J7 fills
  (0/18 slot lane, 4/4 on-chain fallback after a slot-lane refusal). The 24% of reads that repeat
  a mint within 5 s are LATER duplicate-lane decisions on a buy J7 already decided.
- DROPPED, token safety beside the quote: 30 ms p50 is not worth an executor contract change.
- PENDING THE PROBES (20 fills), writing the shell row asynchronously. The row would stay in memory
  from decision start and be persisted at the first of (a) just before reserve_entry, which keeps
  the write-ahead identity the reservation needs, or (b) finish. The cost: a crash between decision
  and reservation loses that attempt's row (observability only; no money moves before
  reserve_entry).

The DexScreener GET (234 ms) stays on the critical path. The Solana gates read price, market_cap,
liquidity, volume m5/h1, txns and pair age from it. Own state (on-chain supply times pool price)
can replace only mcap and price, and only for known pools. Volume and txns have no own source for
unwatched coins.

The native quote (B1). GO for SHADOW only.
- Qualifying decisions: J7 Solana decisions whose Jupiter coin-side hop is Pump.fun or Pump.fun
  Amm with SOL input. That is 75 of 253 quoted decisions in 7 days and 36 of 105 fills (PumpSwap
  45 + 10 multi-hop, curve 20). The curve PDA and the canonical PumpSwap pool are derivable from
  the mint (the PumpSwap creator is PDA["pool-authority", mint], index 0, wSOL).
- Today on those decisions: Jupiter quote p50 87 / p90 404 ms, and /swap-instructions build p50 32
  / p90 47 ms. The reverse quote is already native (about 0 ms).
- Natively: one reserve read (about 45 ms, unmeasured for this read) plus local math and build.
  The expected saving is p50 about 75 ms, p90 about 390 ms.
- The contract (B1): a native quote seam inside execute_entry.
  - quote_fn returns a Jupiter-shaped quote from the fresh reserves, carrying the lane builder's
    instructions.
  - The build uses those instead of /swap-instructions.
  - USDC->SOL comes from the lane's WARM leg only when the leg's clip equals the reservation's
    clip. Otherwise the whole decision uses Jupiter.
  - Every execute_entry gate runs unchanged on the native numbers.
  - Sign, send, confirm and USDC-delta accounting are unchanged.
- THE FEE SOURCE (the blocker found before any code). The lane takes fee bps from the source's
  TradeEvent, and a J7 decision has none. pump.fun fees come from the pump_fees FeeConfig (flat
  plus market-cap tiers), which no code here decodes. Plan: decode FeeConfig from pump's IDL, and
  first check it read-only against 20+ live TradeEvents (decoded tier bps == event bps). The
  latest routed TradeEvent on the same curve or pool serves as a cross-check.
- Modes: MEMECOIN_ENTRY_NATIVE_QUOTE = off | shadow | live, default shadow. Shadow computes beside
  Jupiter, in parallel, adds no time, logs both (out, min_out, price, ms, reserves slot) and never
  uses the native quote. Live is refused in code until Alpha's GO.
- The shadow gate for live: n >= 20 qualifying decisions, native out within 0.5% of Jupiter's out
  on the same reserves slot, and 0 model misses.
- Rule 5:
  1. Money path: yes, when live (about 30% of J7 Solana entries). Every gate is kept; min_out uses
     capped_min_out.
  2. Speed: one reserve read replaces quote plus build; a 150 ms read timeout falls back to
     Jupiter.
  3. Concurrency: the pool caches are read whole-dict. The warm leg is read-only, and the wallet
     lock serializes sends.
  4. Absent or unknown: no pool, an unreadable account, a foreign creator, a stale leg or an
     undecodable fee means Jupiter, never a guess.
  5. Failure modes: a read failure means Jupiter; a model miss fails min_out (fee only), and the
     shadow catches it first.
  6. Units: lamports, token atomic, Token-2022 fee.
  7. Sample: the 75/253 over 7 days.
  8. Rollback: the mode knob.

The early entry quote (option 4, Alpha GO 2026-09-28; `sniper/services/memecoin_early_quote.py`).
- What it does. The Jupiter entry quote starts at decision start on its own thread. It runs beside
  the DexScreener GET and the reservation stage, for the clip the risk envelope would approve. It
  hides up to about 500 ms of pre-quote time (Alpha's figure). The quote itself is p50 91 /
  p90 502 ms.
- The pre-screen, on the decision thread with no I/O: a manual or override entry, the quiet window,
  and the J7 event's mcap (unknown, or over the wallet's ceiling) start nothing and are counted.
- The sizing, on the thread. It runs the same envelope reserve_entry runs
  (`build_memecoin_risk_context` + `memecoin_entry_allowed`). The inputs are the gate's own early
  balances, the market regime scale (`market_risk_scale`) and a read-only connection. The kill
  switch, cooldown, one-per-coin and daily loss all refuse there, and a refused envelope never
  quotes.
- The use. The executor takes the quote only for the EXACT clip the reservation approved (1e-9)
  and while it is younger than MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S (1.5 s). It waits up to 2 s for
  a quote still in flight. Every other case discards the quote, counted by reason, and quotes as
  before. Every gate after the quote judges whichever quote it got. The oversize retry never takes
  it.
- The native shadow starts beside the early quote on the same thread. The executor starts none when
  it uses that quote, so the shadow still compares two reads taken together.
- Heartbeat `early_quote`: skipped, started, quoted, sizing_refused, used, discarded,
  wasted_quotes (quoted - used) and untaken.
- Measured on the live ledger, read-only, 60 runs, 2026-09-28T07:36Z:
  - The decision thread adds p50 0.32 / p90 0.42 ms before the GET (pre-screen, db path, thread
    start).
  - The sizing takes p50 7.7 / p90 7.9 ms on the thread before the quote starts.
- Rule 5:
  1. Money path: yes. This is the quote the entry signs.
  2. Speed: +0.3 ms on the decision thread. The quote leaves the critical path whenever it is used.
  3. Concurrency:
     - The counters sit under one lock, and the object belongs to one decision.
     - The object is bound to its trace, so a re-hooked event never sees an older attempt's quote.
     - The read-only connection is opened and closed on the thread. SQLite WAL readers never block
       the reservation's BEGIN IMMEDIATE.
     - The quote-miss record is thread-local, so a discarded early quote leaves the executor to
       type its own miss.
  4. Absent versus zero: an unknown mcap or an unread balance means no early quote, never a guess.
  5. Failure modes:
     - A raised, empty or late quote is discarded and the executor quotes as before.
     - A 429 on an early quote cools that Jupiter host for JUP_HOST_COOLDOWN_S for the executor
       too. That is the cost of the accepted +87% load (about 1.3 quotes an hour).
  6. Units: USD clip, float equality to 1e-9. The quote is USDC->coin exactly as today.
  7. Sample: the counters describe our own J7 Solana decisions that reached a market-data read.
  8. Rollback: MEMECOIN_ENTRY_EARLY_QUOTE=0 and a runner restart. Each decision reads the flag.
- Not handled:
  - A venue excluded by a failed same-coin buy inside the ~0.5 s window is still in the early
    quote. One-per-coin makes this near impossible.
  - The classifier's hot_narrow regime and per-wallet cooldown exemptions size differently.
    Those entries discard or skip the early quote and take today's path.
  - The quote's age at use rises from about 0 to about 0.4 s typical (1.5 s at most).
- A REFUSAL IS CONFIRMED FRESH (Alpha's doctrine call, 2026-09-28).
  - The rule's purpose: a post-peak refusal must be confirmed by a fresh exact quote, so a stale
    reading cannot kill a live buy.
  - When the early quote would refuse at the chase or the post-peak check, the executor re-runs the
    entry once on a fresh quote. Every gate runs again, and the reservation is still held.
  - The refusal stands only if the fresh quote confirms it.
  - An early quote that passes both checks is used. The signed min_out still bounds the drift.
  - Heartbeat: refusal_requoted{kind} and refusal_agreed{kind}. Trace substage:
    early_quote_refusal_requoted.
  - Impact, round-trip and route refusals on an early quote stand as they are. Alpha's call named
    chase and post-peak only.
  - Stage columns keep the first "quote" mark, so a re-quoted entry's quote stage reads short. The
    substage carries the re-run.

## 13. The transaction stream on the slot lane: design, sized to venue coverage (2026-09-28)

Operator yes: the research key e1f95763 may carry a funded-lane signal socket. Alpha's framing: J7
arrives only about 50 ms after the slot lane, and the stream tied the logs frame (section 11), so the
stream buys CONTENT, not speed. It carries the whole transaction, including the inner-instruction
data where Meteora DLMM and DAMM v2 emit their swap events (self-CPI), which the logs frame never
has.

What it is worth (census v2, armed only):
- About 39 buys a day: DLMM 35.1, DAMM v2 3.7.
- About 5 fills a week, all on the ~2.3 s general path. DLMM reserved 2 and filled 1; DAMM v2
  reserved 9 and filled 4.
- The lane would make those fills fast, and might add some that the slow path reserved and then
  failed (DLMM 1, DAMM v2 5).
Modest, and the builders (DLMM bin walk, DAMM v2 fee schedulers) are the larger cost. So the design
is phased, and every phase stops unless the one before it justifies it.

Phase 0 (read-only, research key, no lane change). It answers what section 11 left open:
1. One raw DLMM buy frame and one raw DAMM v2 buy frame from transactionSubscribe
   (transactionDetails full, maxSupportedTransactionVersion 1, processed), showing the swap event
   in meta.innerInstructions (the anchor event-CPI discriminator, then the event). Decode both.
   Pin the frames as fixtures.
2. Load with the tight filter: ONE subscription per armed owner, {accountRequired: [owner],
   accountInclude: [DLMM, DAMM v2]}, so only that owner's DLMM or DAMM v2 transactions arrive.
   Compare it with section 11's {accountInclude: owners} (6.7 frames/s with notdecu). Record frames
   per hour and Helius credits per hour (from the key's usage before and after a timed window).
3. Delivery on the mainnet host, over at least 2 hours: reconnects, gaps, and arrival minus slot
   first seen.
Gate to phase 1: both events decode from the frames, the tight filter's credit rate is acceptable
to the operator, and there are 0 unexplained gaps over the window.

Phase 0 result (2026-09-28, scripts/research/tx_stream_venue_capture.py, research key on the atlas
host, 33 armed owners, 33/33 subscriptions accepted, 0 errors, 0 reconnects):
- Windows at a flat book: 06:54:40-07:54:40Z, 07:55:24-07:59:35Z and 08:04:48-10:01:29Z, about
  3 h in all. The script stops itself when a real Solana position opens; the last two windows ended
  that way.
- DLMM: 5 frames, 4 of them armed-owner buys (by run: 2, 0, 2), consistent with the census (35.1
  a day is about 4.4 in 3 h). Every
  kept frame carries the Swap event-CPI in meta.innerInstructions (145 bytes, event discriminator
  516ce3becdd00ac4). A second 163-byte event (2e7452d7941b544d) rides beside it, unidentified. The
  3 kept frames are pinned in tests/fixtures/memecoin/meteora_dlmm_frames.jsonl.
- Load with the tight filter: about 2 frames an hour. Credits per hour were not measured.
- DAMM v2 PARKED (Alpha, 2026-09-28): 0 armed buys in 3 h. Revisit only if a funded refusal names
  the venue. (The census's 3.7 a day predicts about 0.5 in 3 h, so the zero is consistent with it.)
- Not done: step 3 (delivery on the mainnet host over 2 hours) and the credit rate.

Phase 1 (shadow). A second signal socket, ADDITIVE:
- Its own daemon thread, its own socket on the research key, and the tight per-owner filter. It
  never touches the logs socket or its subscriptions; a stream failure leaves the lane exactly as
  it is today.
- A DLMM or DAMM v2 frame becomes a RoutedBuy. The owner's delta comes from the frame's own
  pre/post token balances (no programSubscribe pairing is needed). venue_events come from the inner
  instructions.
- FIRST-WINS PER SIGNATURE: one bounded set (8,192, FIFO) under a lock, checked by both producers
  before a RoutedBuy is handed to route_routed_buy. The second arrival is dropped and counted.
  With the DLMM/DAMM v2 program filter, overlap with the logs producer should be near zero; the
  counter proves it.
- The lane builds and simulates in shadow, as CPMM did: the ShadowVerifier and the same cohort read
  (>= 20 builds, >= 80% would land, exact model on unchanged pools).

Phase 2 (live), per venue, on the shadow read and Alpha's plus the operator's GO:
- DLMM: section 3's bin walk with base and variable fees, and the bin arrays around the active
  bin from the frame's post-trade state.
- DAMM v2: section 10's fee schedulers.
- Live exactly as CPMM went live: SOL/USDC first, one path per buy, the hourly cap and the canary.

Rule 5 (phase 1):
1. Money path: none in shadow. In phase 2, the CPMM rules apply.
2. Speed: the stream socket runs off the lane thread. The first-wins check is one set lookup under
   a lock on each producer's thread, nothing on the decision path.
3. Concurrency: two producer threads share only the first-wins set (locked, bounded). The lane
   queue is already thread-safe.
4. Absent vs unknown: a frame without inner instructions, or with an undecodable event, is counted
   and dropped. It never becomes a buy with guessed fields.
5. Failure modes: a stream disconnect, a credit exhaustion or a key refusal stops the stream only,
   with an alert on sustained silence. The logs socket keeps the lane's pump, PumpSwap, CPMM and
   LaunchLab coverage.
6. Units: DLMM bin ids, per-bin amounts, token decimals and Token-2022 fees.
7. Sample: armed owners only, and the census numbers above are armed-at-buy.
8. Rollback: a mode knob MEMECOIN_SLOT_LANE_TX_STREAM = off | shadow. Off closes the socket.

### 2026-10-14 amendment: receipt atomic validation

Before caching a sold-holding receipt or calculating exit residue/position credit, require complete pre/post token-balance arrays, unique in-range account indexes and unambiguous resolved account public keys, unsigned u64 atomic amounts, valid integer decimals and stable per-account mint/owner/decimals across sides. Accounts of one mint must agree on decimals. Malformed evidence remains unknown and uncached so a later valid read can settle it; it never becomes a known zero or an append-only credit. Signature, custody payer, successful landing and exact receipt identity binding remain required. Permitted created/closed accounts retain their existing one-sided balance semantics.
