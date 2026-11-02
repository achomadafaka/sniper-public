# EVM entry gates on on-chain sources (DexScreener out, phase B)

Date: 2026-10-08. Author: Lambda. Status: REVISED after Gamma REVISE on 9a1396b33; operator decisions
recorded in section 6 (Alpha's pick on the delegated cap, 2026-10-08).

Operator order (relayed by Alpha, 2026-10-08): take DexScreener off the money path. Entry gates E1, E2,
E3, E7 and E11, plus the executor's market-basis chase, decide on on-chain sources:
- the source's post-trade reference from the pool;
- our own tape for volume and sell share;
- supply x on-chain price for mcap.
An unknown input refuses or censors exactly as it does today. It never falls back to DexScreener.

Also (Alpha, from Gamma): wire our own tape into the EVM volume gate when the provider is missing. 16
4663 attempts since 10-01 logged `no_volume_data` while our tape held a measured 5-minute sum.

Inventory and ids: `docs/audits/2026-09-28-dexscreener-removal-inventory.md`. Switch:
`MEMECOIN_DEXSCREENER_OFF` (per gate, per chain, for example `E7:4663`).

## 1. What already exists (no new code needed)

| Gate | On-chain path today | Live where |
|---|---|---|
| E1 mcap/band | `MEMECOIN_EVM_OWN_MCAP_CHAINS`: source fill price x on-chain `totalSupply`, read beside the GET (`_start_evm_supply_read`, wait <= `MEMECOIN_EVM_OWN_MCAP_WAIT_MS`=150). 4663 bands on its blind sources (`evm_source_cached`, GT) | 56 own; 4663 blind; 8453, 1 and 5042 still on DexScreener |
| E2 volume | `gate_evidence`: our tape's clean 5-minute organic sum (source tx excluded), a LOWER BOUND over known pools; `onchain_h1_lower_bound` | tape on 4663 and 8453 (V4/V3 writer), 56 (BSC V2 observer, source pool only) |
| E7 sell share | `_sell_share_input`: our tape's buy/sell counts on the top pool when `E7:<chain>` is off | own on 4663 and 8453 (switch exists, off); 56 keeps DexScreener |
| E3 / chase reference | `memecoin_evm_post_trade`: the pool's price right after the source's transaction, from the source receipt (V4/V3 sqrtPrice, V2 Sync) | `MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS=4663,56,8453,1` |

## 2. Measured inputs (EVM attempts since 2026-10-01, traces in `memecoin_entry_attempts`)

- Post-trade reference resolved: 1 34/57 (60%), 4663 88/142 (62%), 56 49/71 (69%), 8453 12/14 (86%).
  The rest fall back to the average fill (`avg_fill`).
- Post-trade price / DexScreener display price (`execution_premium` rows with both): p10/p50/p90
  1 0.93/1.12/1.24 (n=8), 4663 1.07/1.25/1.57 (n=37), 56 1.06/1.28/2.48 (n=17), 8453 1.11/1.23/1.36
  (n=7). The post-trade price sits about 25% ABOVE the display price at the median. The display price
  lags, and it excludes the source's own impact.
- E7 own split vs DexScreener split, |difference| in share points: 4663 median 4.4, p90 27.8
  (n=75); 8453 median 1.5, p90 5.4 (n=13).
- E2 `no_volume_data` with our tape measured (4663, n=16): 12 had an mcap. All 12 proved the m5
  floor and stayed under the ceiling; 16 of 16 had >= 2 swaps. Wiring the tape changes the label on
  this sample, not one decision.
- E2 by chain, provider h1 present / own tape present: chain 1 has provider h1 on 56 of 58 and own
  tape on 0. 5042 had 1 attempt.
- E1 own supply read on 56: ok 74, wait_expired 1; read p50 101 ms, p90 149 ms (n=74); it waited
  0 ms because the GET hid it. The GET (`market_data_ms`) is p90 246-276 ms on every EVM chain.
- Source price present on ~100% of EVM attempts (1 364/364, 8453 177/179, 56 1307/1311).
- The live E11 cap is 1.5 (`MEMECOIN_REAL_MAX_EXEC_PREMIUM=1.5`; cap=1.5 on all 148 `execution_premium`
  rows since 10-01: 1 18, 4663 67, 56 48, 8453 15), not the 2.0 cliff named in the docstring.
- E2 decides admission only on 4663, 5042 and Solana: `MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS=56,8453,1`
  records a would-refuse and admits (`memecoin_real_runner.py:4130`). The dead-coin gate is separate
  and enforces nowhere (`MEMECOIN_REAL_DEAD_COIN_CHAINS` unset).

## 3. Design per gate

### E1 market cap and band (8453, 1, 5042)

Add the chain to `MEMECOIN_EVM_OWN_MCAP_CHAINS`: mcap = source fill price x on-chain `totalSupply`.
`E1:<chain>` off then never reads `st["market_cap"]` (`dex_mcap_allowed`). Unknown (read not done in
the wait, no supply, no price): `mcap_unresolved`, refused on EVM, as today.

With the GET gone, the supply read is no longer hidden: the band waits up to 150 ms (56 p50 101 /
p90 149). Today the GET costs p90 ~260 ms on the same path, so the path gets faster at p90, not
slower. Start the read at the same point it starts now (`_start_evm_supply_read` beside the GET), so
nothing moves earlier into the fill path.

Known basis difference: DexScreener `marketCap` vs our FDV (all supply x price). Already accepted on
56 and pinned for 4663 (`dex_mcap_allowed` docstring: 18 of 39 band flips were supply basis). Per
chain, record both for 1 day before the switch (shadow: `evm_own_supply` substage already writes
our mcap; add the provider's beside it).

### E2 volume gate: our tape when the provider is missing, then without the provider

Step 1, now (Gamma's 16 rows, no provider dependency change). In `volume_alive_decision`, when the
provider number is None and our tape has a measured sum:
- m5 floor: the tape sum is a lower bound. `bound >= floor` proves the floor (exists); `bound < floor`
  is unknown, not a refusal (exists).
- m5 ceiling: unchanged, the provider's number only. The tape sums every known pool of the coin
  (arbitrage pools run up to 20x one pair's count) while the ceiling was fitted on one pair's m5,
  so a tape sum over it proves nothing (standing property `test_a_lower_bound_can_admit_but_never_refuse`).
  A tape ceiling needs a single-pool m5 shadowed against it first.
- h1 legs: NEW. `h1_floor_lower_bound >= need` passes the turnover floor and the flat floor (proven);
  below it, the leg stays unknown, so today's `no_volume_data` admit applies.
- txns floor: NEW. The busiest pool's swap count, source excluded, is a lower bound (one pool, so
  a transaction touching two of the coin's pools is not counted twice): `>= floor` passes; below it
  is unknown (today: admit unless `MEMECOIN_REAL_TXNS_M5_REQUIRED`).
- The label `own_tape_proven` needs every ACTIVE floor leg (m5 USD ratio floor, m5 dollar floor,
  trade count, hourly turnover or flat floor) proven by the tape or answered by the provider, and at
  least one tape leg; one unknown active leg keeps `no_volume_data` (Gamma on 6e08fb97a).
Built: lambda/e2-own-tape-proves 3a739076d.

Step 2, `E2:<chain>` off: the provider's `volume_h1`, `volume_m5` and `txns_m5` are never read. Only the
step-1 rules decide.

- Chains with a tape (4663, 8453, 56): the tape gives proven passes and proven ceiling breaches.
  A coin our tape cannot prove keeps today's unknown posture.
- Chains without a tape (1, 5042): every leg is unknown and admits (decision (a), C). On chain 1 the
  gate is already would-refuse only; on 5042 the guard is gone until a tape exists (the vol_death
  plan's step 3 witness feeds E2 there).

The 56 tape needs the coin's pair from DexScreener today (`read_m5(st["pair_address"], ...)`).
Replace it with the pool of the swap that moved the MINT in the source receipt, never the wallet's
first Transfer (BSC source fills route through Relay solvers, so the first Transfer `to` is a
solver). A Pancake V3 / Infinity pool is outside the V2-only BSC tape: unknown.

### E3 pre-quote chase and the executor's market-basis chase

Both ask "how far did the COIN move since the source filled", today as DexScreener display price /
source price. The replacement is our tape's latest swap on the source's pool against the source's own
swap on that pool:

    move = coin price after the latest swap on the pool / coin price right after the source's swap - 1

Both prices are POST-swap spots on the same pool (V3/V4 sqrtPrice after the swap, V2 Sync reserves
after it), the same quantity as the E3 post-trade reference, never an execution average. Decimals
cancel in the ratio; TOKEN ORDER DOES NOT: when the coin is token1 its price is 1/sqrtP^2, so every
read is oriented by `pool_meta.quote_side` (the V2 reserves ratio too), and a test pins the coin as
token1. It is local (`data/memecoin_onchain_volume.db` / the BSC store), with no RPC.

The method is the one `memecoin_evm_tape_marks` already uses for held coins. Freshness is the tape
health rules from that module (feed TTL, protocol lag, no open tick gap, canonical block hash). A
quiet pool keeps its last swap's price.

When the tape saw the source swap and nothing later, the move is 0. When the tape did not see the
source transaction, is unhealthy, or the pool is unbound, the move is unknown. Then:
- E3 skips, which is fail-open as today when there is no display price;
- the executor chase uses its existing `quote_fallback` basis (quote / reference), as today when the
  display price is missing.

New: `memecoin_onchain_volume.move_since_source(chain, mint, source_tx) -> (ratio, why)`, read-only.
Chains with a tape: 4663, 8453, 56. Chain 1 and 5042: unknown, so today's fallbacks apply.

The chase cap was fitted on display/source-average, so this basis gets the same shadow and refit as
E11 before it decides (build step 2). Until then, with E11 off the executor chase takes today's
missing-display basis, `quote_fallback` (quote / reference), where the reference is already the
source's post-trade price on 4663, 56, 8453 and 1: no DexScreener number reaches it.

**2026-10-10 measurement amendment:** chase tape SQL runs on bounded local shadow workers, and the
decision only peeks. Missing/full/expired/failed work remains unknown. Late evidence is recorded on
its original attempt at result, retry-parent or error completion, with its measurement clock; it
does not become a decision-time observation or change a gate. The two running workers plus 32
queued reads share a two-second queued-age limit with momentum measurement. No new entry wait.

### E7 sell share

- 4663, 8453: switch `E7:<chain>` off. The code exists.
- 56: add buy/sell counts to the BSC `m5_bound`. A V2 Swap carries unsigned amount0In/1In/0Out/1Out,
  so the side is: quote In > 0 is a buy of the coin. Then 56 joins the list in `_sell_share_input`.
- 1, 5042: no tape. With E7 off the input is `{}`, so "sell share unknown", admit, as today.

The 4663 disagreement tail (p90 27.8 points) comes from which pool each source picks. The tape's pick
is the pool with the most quote USD in the window; that pick matched DexScreener's pool on 84.4% of
coins (n=141).

### E11 execution premium

Today: reference = DexScreener display price, with the source price as a floor. When the display
price is missing, the reference falls back to the source price at the same cap.

Replacement: reference = the source's post-trade price (USD = average fill x post-trade ratio).
Unknown means today's no-display fallback: the source average fill at today's cap, or abstain.

The quantity changes. quote / post-trade is about quote / display / 1.25 at the median, so the live
1.5x on the new reference would loosen the gate. Decision (b): scale it, 1.5 / 1.25 = 1.2x, as
`MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE=1.2`. `MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET` entries
move with it (today only a Solana entry, which this does not touch). Shadow, on every attempt: the
verdicts at 1.2x and 1.6x on post-trade and at 1.5x on quote/display (while the display price is
still in hand).

## 4. Rule 5 (whole change)

1. **Money path: yes.** Entry admission on every EVM chain. All of this is behind per-chain switches,
   default today's behaviour. Exits are untouched.
2. **Speed: faster at p90.** It removes the market-data GET (p90 246-276 ms) once every consumer on a
   chain is off (E13). It exposes the supply read wait (<= 150 ms, 56 p90 149) and local SQLite reads
   (tape m5 measured cpu ~8 ms). No new RPC, lock or retry on the fill path. The tape reads already
   happen for the volume gate.
3. **Concurrency.** Read-only SQLite handles on the tape stores (`get_readonly_connection`), as today.
   No new shared state. The supply read pool stays 4 workers; a queued read past the wait is
   unknown.
4. **Absent vs zero vs unknown.**
   - A tape sum of 0 with the source swap present means a measured quiet market.
   - A tape that is not healthy, or cannot see the source transaction, is unknown.
   - A lower bound proves only passes for floors, and only breaches for ceilings.
   - No swap after the source gives move 0; an unbound pool gives unknown.
5. **Dependency failures.**
   - Tape store unavailable, stale feed, or header gap: unknown (`gate_evidence` reasons as today).
   - Supply read timeout: `mcap_unresolved`, refused.
   - Receipt or post-trade read failure: `avg_fill` reference, as today.
   - A swallowed tape error must not read as 0. The read returns `{usd: None, reason}`, never 0, and
     every new rule tests `is None` before any comparison.
6. **Units and identity.**
   - Ratios from sqrtPrice and reserves cancel decimals; USD only via the tape's own quote prices,
     as today.
   - Chain ids are network ids ("1", "8453", "56", "4663", "5042").
   - The pool is the source receipt's pool, not a provider pick.
   - E1 is FDV (total supply), not circulating.
7. **Sample.** The gate refusals these change are on our own attempts. Measure the before/after on
   the same attempts by recording both sources (shadow) for 1 day per chain before each switch: E1
   mcap pairs, E11 both references, the E3/chase move next to the display move.
8. **Rollback.** Remove the chain's id from `MEMECOIN_DEXSCREENER_OFF` / `MEMECOIN_EVM_OWN_MCAP_CHAINS`.
   Half-applied, each gate is independent per chain; a chain with some gates off still reads the GET
   for the rest.

Not handled here: E5 (pair age), E8 (ticker), E10 (V4 pool discovery on 8453/56), E12 (revival watch).
These keep the GET alive on a chain until each is off; E13 (dropping the GET) is the last step per
chain.

## 5. Build order (each one branch, red tests first, Gamma)

1. E2 step 1: our tape decides where the provider is silent, with the label rule. Built
   (lambda/e2-own-tape-proves).
2. E11 on the post-trade reference at 1.2x behind `E11:<chain>`, with the shadow verdicts (1.2x and
   1.6x post-trade, 1.5x quote/display); shadows for E1 (the provider's mcap beside ours) and for the
   chase (`move_since_source` beside the display move).
3. `move_since_source` + E3 / executor chase on the tape, behind `E3:<chain>` / a new `EX:<chain>`
   id, after its shadow and refit.
4. 56: the pool from the source receipt's mint-moving swap for the BSC tape; buy/sell counts.
5. E1 on 8453/1/5042 via `MEMECOIN_EVM_OWN_MCAP_CHAINS`; E7 off on 4663/8453/56.
6. E2 step 2 per chain per decision (a); then E13 per chain when E5/E8/E10/E12 are also off.

DATED READ, 2026-10-15 (one week after step 2 deploys): refit the E11 cap on the post-trade reference
from filled closes (realized return by quote/post-trade band, the same table as the docstring's
quote/display one), and read the chase shadow against the display basis. The read names the cap
and the chase cap; it does not change them by itself.

## 6. Operator decisions (2026-10-08)

(a) E2 on chains with no tape (1, 5042): C, admit unknown, no DexScreener read. Chain 1 is
would-refuse only already; 5042 loses the guard until its swap witness exists.

(b) E11 on the post-trade reference: C, scaled to keep today's strictness. The operator delegated the
number; Alpha's pick is 1.2x (1.5 / 1.25). The 1.6x first relayed came from this spec's wrong 2.0
base. Shadow 1.2 / 1.6 / quote-display 1.5 for the 2026-10-15 refit.

## 7. Paper path: DexScreener readers and replacements (plan only)

| Reader | Where | Feeds | Replacement |
|---|---|---|---|
| Paper price pass `dexscreener_stats_batch_keyed(keys)` | `memecoin_runner.py:931` (`run_exit_sweep`) | v0 sweep rails (`sweep_exits`) for arms not on `onchain_ticks_v1`; the 14-day backstop | The v1 engine's sources per chain (4663/8453 tape spot, 56 bag quote, Solana vault stream). Move the remaining v0 arms to v1, then drop the batch |
| Same map into `write_marks` | `memecoin_exits.write_marks` (now `paper_dexscreener`) | paper chart, candles | The v1 tick, written as the mark (tape spot or bag quote), labelled by source |
| Same map into `record_quote_observations` | `memecoin_runner.py:966` | dense quote ledger (exit-search study input) | The v1 tick stream (a dense on-chain series) or retire the ledger for on-chain arms |
| Same map into `record_paper_path` | `memecoin_paper_path.py:83` | per-position paper price path | The v1 tick |
| `mark_positions` (`dexscreener_stats`) | `memecoin_marks.py:71` | paper unrealized PnL | The v1 tick's last price, or the bag quote on 56 |
| GT batch + `evm_price_batch` (4663 blind) | `memecoin_runner.py:944-962` | 4663 paper price and mcap | The tape spot already in v1; mcap = tape price x pinned supply |
| Paper entry gates | shared classification (`stats_fn`) | paper admission | Inherit the funded gates above (same code, per-chain switches) |
| Paper ticks engine | `memecoin_paper_ticks.py` | v1 decisions | Already on-chain; no DexScreener read |

Paper is measurement only. Its order is: move the arms to v1, switch the marks and the path to the
v1 tick, and then delete the price pass. No funded seam or key is involved.
