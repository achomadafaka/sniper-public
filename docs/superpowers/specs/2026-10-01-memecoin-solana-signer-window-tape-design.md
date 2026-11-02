# Solana signer window tape (spec, for the operator's morning ruling)

**Date:** 2026-10-01 night · **Author:** Lambda · **Status:** APPROVED by the operator 2026-10-02 (relayed by Alpha), BUILDING (build section at the end) · **Order:** Alpha,
overnight operator order: "spec (a) ... measure Solana signals a day, calls a signal and the
research Helius quota, so the cost is a number, not an estimate."
**Revised** after Gamma's review of 27c3762ca: off-curve owners excluded, pool-side prices, the
decision slots read first, and the replay's entry and exits per the prereg (de2d04af0).

## Why

- **Paper funded_clone is the only measure that predicts our funded return**
  (Gamma prereg ae67353d1: +0.33).
- **Paper exists only for wallets we already track.** A wallet we do not track can be scored for what
  WE would make copying it only from chain data: who bought, at which slot, and the price path after.
- **No stored Solana tape has that.** `memecoin_sol_tape.db` covers only pools we held. The Wallet
  Map's Helius reads keep aggregates (p0, ret_60, slot companions), not swaps.
- **4663 has the shape already:** `research_signal_windows.db` keeps every swap in each signal's
  pool windows. But its makers are routers, so identity is lost there. On Solana, the trader is in
  every transaction.

## What

For each Solana signal, read the coin's succeeded transactions from S - 150 to S + 4500 slots (about
-60 s .. +1,800 s) in the order below, and store one row per trader leg. A signal is an entry attempt
on a source buy, filled or refused; S is the source slot (found per lane, below). The read is Helius
`getTransactionsForAddress` on the mint (`transactionDetails: full`, `limit: 100`, slot filter,
status succeeded); the call is already in `wallet_link_graph.helius_slot_fetch`.

- **S per lane** (Gamma ask 3). Solana entry attempts 2026-09-24..10-01, `memecoin_entry_attempts`:

  | lane | how S is found | attempts | fills |
  |---|---|---|---|
  | slot lane (`slot_lane_processed`) | `buy.slot` (in `memecoin_wallet_slot_reads`) | 149 | 49 |
  | on-chain (`onchain_watch`, `onchain_fallback_after_slot_lane`) | `source_slot` in `source_valuation_json` | 1,397 | 63 |
  | J7 raybot (`swap:raybot:<signature>:...`) | the signature's slot, one lookup | 3,101 | 38 |
  | J7 Fomo app (`swap:<uuid>`), source has a confirmed Solana address | that address's buy leg of the mint nearest before `created_at`, found in the read itself | 539 | 3 |
  | J7 Fomo app, no confirmed address | none: OUT OF SCOPE | 1,759 | 18 |
  | J7 pump list (`j7_pump`, uid `pump:<address>`) | that address's buy leg, as above | 217 | 0 |
  | J7 shadow (`j7_shadow`) | by event format, as above; its 258 Fomo-app rows counted out | 492 | 0 |
  | manual | none: out of scope | 9 | 8 |

  In scope: 153 of 179 funded Solana fills (85%) and 5,637 of 7,654 non-manual attempts (74%). The
  J7 Fomo-app signals without an address are the gap: their source wallet is not known on chain.
- **The trader** is the OWNER of the token account whose balance of the mint moved, and only an
  ON-CURVE owner (`Pubkey.is_on_curve()`, the rule `onchain_wallet_watch.py:552` already uses).
  Off-curve owners are excluded: pool vaults, pump bonding-curve accounts and router or program
  authorities. Every swap moves a vault, so without this rule each vault becomes a "trader".
  - **Limit:** a smart wallet (a Squads vault) is a PDA too and drops out. A trader who uses one is
    invisible to the tape.
  - **Bundles** stay one leg per owner. The fee payer is stored beside each leg: a relayer, a Fomo
    embedded wallet or a bundler can pay for someone else (slice 3b's signer-paid rule), and one fee
    payer across several owners in a slot is the bundler signal.
- **Side and token amount** come from that owner's pre/post token balances of the mint.
- **Quote and price come from the POOL side**, never the trader's SOL change. The trader's SOL change
  also carries Jito tips, ATA rent and the WSOL wrap and close: a 1-10% price error on small buys.
  - pump curve: the TradeEvent in the logs (sol and token amounts, POST-trade virtual reserves;
    decoders in `memecoin_slot_signal.py` and `memecoin_pump_tape.decode_trade`);
  - PumpSwap: the BuyEvent/SellEvent (PRE-trade pool reserves; the same module);
  - other AMMs: the pool vaults' token balance deltas in the same transaction give that swap's
    price. Vault balances are the reserves only on constant-product pools; on DLMM and CLMM they
    are not the price (brain 680ba272: median 4.40x off), so those legs carry the swap price and
    no clip price.
  On the curve and constant-product pools the reserves after each transaction give the executable
  price at (slot, tx index) for any clip size, with the event's fee bps
  (`pumpswap_buy.expected_base_out`, `total_fee_bps`). That is what the replay needs for our clip's impact. A venue none of these
  covers stores the leg with the price unknown, never a price from the trader's SOL change. The
  trader's SOL change is kept as a diagnostic column only.
- **No DexScreener anywhere**, and no stored mark table.
- **Overlapping windows on one mint** (several sources, minutes apart) are read once, as their union.
- **Storage:** a research DB `data/research_signal_windows_solana.db` with the 4663 schema (signals,
  ticks with `maker` = owner, plus `fee_payer`, `tx_index`, `slot` and the pool reserves after the
  transaction). Read-only on the ledger.
- **When:** windows are read AFTER they close (S + 4500, about 30 minutes, is in the past). Helius serves history, so
  the job can run only while the Solana book is flat (the research-key rule 573d8817), or on the
  operator's explicit exception, as the exit tape got. A window not yet read is pending, never empty.

## What it enables

1. **Candidates with identity.** Per owner across windows:
   - buys BEFORE our source (leaders, by slot and tx index);
   - buys in the same slot (copiers, ahead or behind);
   - repeat early buys on coins that ran;
   - and the bot, bundler and dev screens on the same rows (owner == creator, a buy in the creation
     slot).
2. **An admissible copy-replay** under Gamma's prereg (de2d04af0):
   - **Entry lag is measured from our own landing slots.** Our funded wallet is a leg in the same
     window whenever we filled, so its landing slot minus S, per `delivery_lane` of the entry
     attempt, gives k for the slot lane, the on-chain lane and J7 empirically. A signal we did not
     fill takes k from its lane's distribution. J7 transit is NEVER computed as host time minus
     block time (the corrected-clock trap).
   - **Entry price** is the pool's executable price for our clip at (S + k, after the last
     transaction before ours), so the copiers ahead and our impact are in it.
   - **Exits run the funded exit policy over the path,** priced from the pool reserves out to
     +1,800 s. Funded Solana closes since 09-21 (Gamma, n=269): 41% within 60 s, 81% within
     300 s, p90 538 s, 98% within 1,800 s. A +300 s window would censor about 19% of our exits.
   - **Fixed +60 s and +300 s horizons are diagnostics only,** never the replay's return.
   - It is calibrated against funded % under the prereg once enough pairs exist.

## Read order (Gamma change 3)

Busy coins hold tens of thousands of transactions in six minutes. The first spec read [S - 150, S + 750]
ascending with a 25-page cap; on the busiest coins that cap is spent before the source slot. So the
window is read in five segments, decision slots first. Each segment reads 100-transaction pages.

| order | slots | read | why |
|---|---|---|---|
| 1 entry | S - 20 .. S + 30 | every transaction | copiers ahead, our landing slot per lane, the price at S + k |
| 2 leaders | S - 150 .. S - 21 | every transaction, at most 10 pages: 5 ascending from S - 150 and 5 descending from S - 21 | the earliest buyers (the real leaders) and the nearest ones (mostly bots on the same signal) |
| 3 first minute | S + 31 .. S + 150 | one page per 2 s (5 slots) | exit path; 41% of funded exits are within 60 s |
| 4 path | S + 151 .. S + 750 | one page per 5 s (12 slots) | exit path to +300 s |
| 5 exit tail | S + 751 .. S + 4500 | one page per 30 s (75 slots) | exit path to +1,800 s |

- **"One page per bucket"**: a page that ends inside its bucket jumps to the next bucket's first
  slot; a page that reaches the next bucket continues. A quiet coin is therefore read whole.
- **If our landing slot is past S + 30**, the entry segment extends to it plus 5 slots.
- **Limit, and its calibration** (Gamma ask 1). Solana funded exits are event-driven: the hot exit
  reacts per swap (`MEMECOIN_HOT_TP_MIN_INTERVAL_S` 0.25), not on ticks. The 2 s buckets with
  100-transaction pages are near-dense to +60 s, where 41% of funded exits happen; the 5 s and 30 s
  buckets (+60 .. +1,800 s, 59% of exits) miss intra-bucket extremes, so a stop triggers late at a
  worse price and a TP spike can be missed. So the sampler is CALIBRATED on our own funded
  positions inside the windows: replay the exit policy on the sampled path, compare its exit time
  and price with the real exit, and print that error beside every replay number.
- **Each segment records the last slot it reached.** A segment cut by its cap is marked; the entry
  segment has no cap.

## Cost (measured)

Census: 30 signal windows (`memecoin_wallet_slot_reads`, newest 1,000 rows, seed 23, distinct mints),
signatures-only `getTransactionsForAddress` (10 credits per 1,000 signatures) on the research key with
the Solana book flat, 2026-10-01 23:24-23:44Z. 512 calls, 5,120 credits. The read orders below are
priced from each window's transaction slots: a full page is 100 transactions and 10 credits, and an
empty or partial page still costs 10.

| read order | credits a window: mean, p90, max | credits a month at 250-340 windows a day |
|---|---|---|
| first spec: ascending S-150..S+750, 25 pages | 127, 250, 250 | 0.95M-1.3M; loses +60 s on 7 of 9 busy windows (refused) |
| every transaction to +1,800 s | 1,598, 6,830, 8,000 (lower bound) | 12M-16M |
| Gamma's order, dense to +300 s, tail per 10 s | 935, 3,500, 4,250 | 7.0M-9.5M |
| **proposed (table above)** | **446, 1,170, 1,440; upper bound 521, 1,540, 1,760** | **3.3M-4.6M; upper bound 3.9M-5.3M** |

| item | value | source |
|---|---|---|
| Solana coins a day with a decision | 197 to 270 (09-24..10-01) | memecoin_entry_attempts |
| (wallet, coin) first buys a day | 285 to 407 | same |
| windows over 2,500 transactions in S-150..S+750 | 9 of 30 | census |
| calls a window, proposed | mean 44.6, upper bound 52.1 | census |
| job time a day, proposed | about 4.3 to 7 hours (about 1.4 s a full call: 1.1 s spacing plus transfer) | first measurement: 25 pages in 33-35 s |
| plan credits a month | Developer 10M, Business 100M, Professional 200M | Helius; the research account's plan is the operator's to state |

- **Upper bound.** The census itself stopped at 40,000 signatures a segment on 5 windows (one window at
  S + 475, the tails at S + 1,198 .. S + 2,497), so their transactions past that point are unseen.
  The upper bound gives every bucket past the census one page, the most a sampled read can spend.
- **Job time is the real constraint, not credits.** 4-7 hours of reads a day does not fit the
  flat-book windows on a busy day. Hence decision (2).

## Rule-5 map

1. **Money path:** none. A research job on the research key, after windows close. It never runs
   on the funded key or the funded read lane.
2. **Speed:** nothing on any fill path.
3. **Concurrency:** one job with a lock file, as the 4663 recorder has.
4. **Absent, zero or unknown:**
   - an unread window is "pending";
   - a segment cut by its cap is "truncated": its rows are kept and its last slot reached is stored;
   - a sampled segment stores its bucket width, so a gap is never read as "no trades";
   - a failed read is retried, never stored empty.
5. **Dependencies:**
   - a quota 429 (`max usage`) stops the job;
   - a rate 429 backs off (the existing `funded_exit_truth.rpc`);
   - the research key's spacing is 1.1 s, measured 2026-09-27;
   - a flat-book flip stops the read mid-window (`helius_slot_fetch` checks before each page).
6. **Units:** slots, not seconds, for the window and the lag. Token amounts are atomic, with
   decimals from the token balance rows.
7. **The sample:** only coins our sources bought. Candidates found here are wallets that trade
   alongside our sources, by construction, never a census of the chain. Say so wherever a candidate
   is shown.
8. **Rollback:** stop the job. The DB is research-only and can be deleted without effect on anything.

## Not proposed

- Real-time reads during open positions.
- 4663's equivalent: tx.from for pre-window swaps. That is a separate, smaller spec (QuickNode
  research key, metered).

## Measurement

### First run: 15 windows, full transactions

15 recent Solana slot-lane signals (`memecoin_wallet_slot_reads`, newest 400, seeded sample), on the
research key `e1f95763` with the Solana book flat. Each window is slots S - 150 .. S + 750, at most
25 pages of 100 full transactions, ascending.

| mint | transactions | calls | truncated | owners (fee payers) | seconds | credits |
|---|---|---|---|---|---|---|
| 8SMMso8M | 111 | 2 | no | 30 | 2.7 | 20 |
| 4QsNTBKw | 2500 | 25 | yes | 544 | 34.4 | 250 |
| biddm3N7 | 2500 | 25 | yes | 83 | 33.9 | 250 |
| Atf7KaBL | 130 | 2 | no | 37 | 2.7 | 20 |
| DhVe3oYN | 165 | 2 | no | 52 | 2.9 | 20 |
| 9h5AzEQz | 25 | 1 | no | 17 | 1.2 | 10 |
| 98kfF7rm | 2500 | 25 | yes | 56 | 34.6 | 250 |
| 7Fj8vzRG | 574 | 6 | no | 181 | 8.5 | 60 |
| bELnp2cS | 250 | 3 | no | 34 | 4.0 | 30 |
| 98K6agk5 | 139 | 2 | no | 60 | 2.6 | 20 |
| FRVhmJio | 260 | 3 | no | 117 | 4.1 | 30 |
| 7GuTYEM2 | 1618 | 17 | no | 405 | 24.5 | 170 |
| 8Ko3GWjA | 2500 | 25 | yes | 378 | 33.6 | 250 |
| 458eEcXu | 2500 | 25 | yes | 203 | 33.6 | 250 |
| ER2Xobje | 2500 | 25 | yes | 358 | 32.4 | 250 |

### Where the 6 truncated windows stopped (Gamma change 3)

A signatures-only recount of the 6 truncated windows (78 calls, 780 credits). The first run did not
store S, so S is recovered from `memecoin_wallet_slot_reads` for the mint (one of its reads where it
has several).

| mint | transactions S-150..S+750 | of them by S+150 | slot of the 2,500th, from S |
|---|---|---|---|
| 4QsNTBKw | 4,204 | 1,108 | +290 |
| biddm3N7 | 15,000 or more | 15,000 or more | -130 |
| 98kfF7rm | 15,000 or more | 14,423 | -96 |
| 8Ko3GWjA | 15,000 or more | 4,348 | +85 |
| 458eEcXu | 15,000 or more | 15,000 or more | -114 |
| ER2Xobje | 12,905 | 283 | +310 |

- **The first spec's claim "the cap keeps the part that matters" was wrong.** 4 of 6 truncated
  windows stopped before +60 s and 3 of 6 before the source slot. The census (30 new windows) agrees:
  7 of 9 busy windows stop before +60 s, 4 of 9 before S. Exactly the busiest coins lose their
  decision slots: a selection bias. Hence the read order above.
- **The counts are fee payers, not owners.** The trader rule above is the build's job; these
  counts are a size estimate only.
- **Spent on measurement:** 1,880 credits (first run) + 780 (recount) + 5,120 (census) = 7,780, plus
  an earlier killed run whose credits were not recorded.

**Decision for the operator:**
(1) Build the tape on the research key with the proposed read order: about 3.3M-5.3M credits a month.
    The research account's plan decides whether that fits (Developer is 10M).
(2) Job time is 4-7 hours a day. Run it only while the Solana book is flat (default; it will fall
    behind on busy days, and a window not read is pending, never empty), or give it the exit tape's
    exception (the paid research key outside the flat-book rule).
(3) Resolution: 2 s / 5 s / 30 s as proposed, or finer at more credits (dense to +300 s with a 10 s
    tail is 7.0M-9.5M a month).

## Build (2026-10-02, Lambda, branch lambda/signer-tape)

The operator approved the tape (relayed by Alpha, 2026-10-02). Unless the operator rules otherwise,
the build takes the spec's defaults: the proposed read order, reads only while the Solana book is
flat, and no cron line until Alpha installs one.

### Slice 1 (this branch)

- **One script**, `scripts/research/sol_signal_window_tape.py`. Same shape as the 4663 recorder:
  one pass per run, a lock file, read-only on the ledger, writes only `--db`.
- **Signals:** Solana buy attempts whose origin carries the source signature:
  - `slotlane:<sig>`
  - `onchain:<sig>`
  - `swap:raybot:<sig>:...`
  - `pump:<sig>:...`

  One signal per (source signature, mint).
- **Not read in slice 1:** `swap:<uuid>` (Fomo app) and manual entries. Each run counts them in
  `runs.out_of_scope`. Slice 2 adds the Fomo-app rows that have a confirmed address.
- **S** is the source signature's slot, from `getSignatureStatuses` with history search (256 to a
  call). Our landing slot comes from the same call, on the filled attempt's `tx_sig`.
- **Due:** a window is read once the chain tip (one `getSlot` per run) is past S + 4500 + 32.
- **Union:** each mint keeps a coverage table: slot ranges already read, with their bucket width
  (0 = every transaction). A segment reads only the part of its range that no equal-or-finer read
  of that mint already covers.
- **Rows:**
  - `ticks`: one per on-curve owner whose balance of the mint moved. Columns: signed atomic delta,
    pre, post, decimals, fee payer, slot, tx index, block time. `own_sol_lamports` is diagnostic
    only. `kind` is `liquidity` when the owner's coin and quote tokens all moved the same way (an LP
    add or withdrawal), else `swap` when the transaction has a pool-side swap of the mint, else
    `transfer` (a plain transfer or an airdrop is not a trade). The smoke run on 2026-10-02 found a
    DLMM withdrawal beside a 7-unit dust swap that the transaction-level rule called a 6.6M-unit buy.
  - `pool_swaps`: one per pool side of the mint in the transaction.
    - pump curve: the TradeEvent's fixed head (amounts, POST-trade virtual and real reserves,
      fee bps). The tail is not read, because older events are short (brain 9241badf).
    - every other venue: the off-curve owner whose coin balance and one quote (WSOL, USDC or
      USDT) balance both moved, in opposite directions. Its post balances are the reserves after
      the transaction, and |quote| / |coin| is the swap's price.
    - `cp = 1` only on constant-product programs (PumpSwap, Raydium AMM v4, Raydium CPMM, Meteora
      DAMM v1). Meteora DAMM v2 is cp=0: its pools can carry sqrt_min/max price bounds, so its vaults are
      not the price (Gamma agreed, LOW 1 on 8cd18b685). On other programs the reserves are not the price.
    - The quote is whatever the pool pairs the coin with. It is usually WSOL, USDC or USDT, but on a
      coin/coin pool the price is in the other token's units, and `quote_mint` says which (LOW 2).
    - PumpSwap pools that carry a virtual quote reserve read low on the vault reserves. The Pool
      account is not read; the venue is named so a reader can tell.
  - `pages`: every call's filter, order, first and last slot, size and whether it was full.
  - `coverage`, `signals`, `runs`.

### Rule-5 map (build)

1. **Money path:** none.
   - A research script on the research key (`funded_exit_truth.rpc`, which refuses a funded key)
     through the key's shared pacer.
   - It never imports the runner or calls a funded Jupiter or RPC seam.
   - Ledger access is a `mode=ro` connection with `query_only`.
2. **Fill path:** nothing on it. No process of the funded runner imports this script.
3. **Concurrency:**
   - one job, with a lock file;
   - one sqlite writer, the job itself;
   - the pacer is cross-process, so the job shares the key's 1.1 s gap with the other research
     readers.
4. **Absent, zero or unknown:**
   - **Window status:** `pending` means not read. `no_slot` means the signature was not found
     (retried on later runs, then `unresolved` after 3 tries). `read` and `truncated` are stored
     per segment in `pages` and `coverage`.
   - **Sampled range:** stores its bucket width, so a slot gap inside it is "not read", never
     "no trades".
   - **Price:** a swap with no pool side has a NULL price, never a price from the trader's SOL.
   - **Stopped read:** a read stopped mid-window leaves the window `pending`. Finished segments
     stay covered and are not read again.
5. **Dependencies:**
   - A quota 429 or another HTTP error from the key's `Stop` ends the run.
   - A rate 429 backs off inside `fet.rpc`.
   - The book is checked before every call; it may open mid-run.
   - `getSlot` failure: no window is due this run.
   - The entry segment has a safety cap of 300 pages. A segment cut by its cap is marked
     truncated.
6. **Units:**
   - slots for every range;
   - atomic token amounts with decimals;
   - lamports;
   - block time in epoch seconds as Helius returns it.
7. **The sample:** coins our sources bought, as section 7 of the spec says. The tape is not a
   census of the chain.
8. **Rollback:** stop the job (it has no cron line until Alpha adds one), then delete the DB.

### Smoke run (2026-10-02 08:57Z, live research key, Solana book flat, 60-call budget)

- **Signals:** 52 in scope and 80 out of scope since 06:00Z. S resolved for all 52; our landing slot for 3.
- **One busy window** (8nnaeWCw, about 30 transactions a slot), partly read before the budget ran out, so it stays pending:
  - entry: 16 dense pages;
  - leaders: cut at 5 + 5 pages;
  - minute: one page per bucket, and pages that crossed a bucket continued.
- **Rows:** 390 ticks and 439 pool swaps (PumpSwap 383, DLMM 56), none with a NULL price.
- **Balance check:** on 380 of 381 trade transactions, the traders' coin delta equals minus the pool side's.
  The one exception was the liquidity withdrawal above.
- **Dust swaps:** a dust pool swap (7 coin units) gives a price 1,000x off. A reader weights or floors by size.
