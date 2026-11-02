# Funded exit price tape: decision previews, fills, and a post-exit follow-up (spec)

Date: 2026-09-30. Author: Lambda. Order: Alpha, item 2 after the per-wallet TP/SL read stopped.
Reviewer: Gamma. Status: Gamma CLEAN on f3377b74c (1 MEDIUM and 3 LOW folded in below); spec only.
**The operator decides whether any part is built.**

**Amendment 2026-10-01 (operator decisions; Alpha's order).** The operator decided:
- build A;
- build B on the EVM research hosts;
- build Solana B1 and C on the paid research Helius key. The Solana parts are exempt from the
  flat-book rule.

Building surfaced an error in §2, so **Part A is narrowed to A-lite, §3a** (Alpha approved
2026-10-01). §3 is kept as history. The fill copy and its chain time move into the Part B process.

## 1. Why

The per-wallet TP/SL read (`docs/audits/2026-09-30-per-wallet-tp-sl-preregistration.md`, amendment
A1) stopped for want of data:

- **Solana has no whole-bag executable series during a hold.** The Solana fast-lane marks are nominal
  per-token spot or trade prices.
- **No chain records an executable price after our exit.** Every cell above tp 25 or below stop 35
  was censored.
- **4663 failed the instrument check** (Lambda, `lambda/tpsl-replay` 3e44fcffd; Gamma confirmed
  independently): 30% of own-contract replays reproduced our fill, and 90% was needed. Gamma: the
  last `fastlane_rpc` mark before our decision (median age 0.0 s) against the booked return gives
  p10 -12.3, p50 0.0 and p90 +11.7 points; only 40% land within 2 points. The movement between
  decision and fill is the thing to measure.

Gamma's requirement: store the preview AT the decision and the fill's whole-bag value with
sub-second stamps, so that a later read can separate trigger choice from fill slippage.

## 2. What the sweep already buys (code reading; part A measures it)

| chain | whole-bag preview during the hold | where it goes today |
|---|---|---|
| 4663, 56, 8453, 5042 | `memecoin_fastlane.refresh_open_positions`, every open position, about 2 s | `memecoin_position_marks` `source='fastlane_rpc'` (per token, keyed by mint, not by position) |
| solana | WO-4 pre-pass `_wo4_miss_prepass`, only for exact-relevant positions that pass the DEMAND GATE (the cheap mid near a rail), 8 threads | kept in memory: `record_exact_quote` (vol_death witness, pruned), `pos['_exit_bag_quote']` |
| solana | a rail's confirmation preview (`_timed_preview`), only when a rail fires | not stored; accumulated time only |
| all | the executor's submit quote | `quote_price` on the exit fill row, per token |

So on Solana a whole-bag price exists **near the default rails only**: the demand gate skips
positions far from every rail. A cell at tp 10 or 15 never gets a Solana exact price when the mid
crosses +10. Part A alone cannot make those cells gradable; part C would.

## 3. Part A: decision and fill tape (every chain, no new quote call)

**What.** Every whole-bag preview the exit path already buys becomes one row, and so does every exit
fill:

- `purpose`: `prepass`, `own`, `confirm`, `submit` or `fill`;
- `position_id`, `network_id`, `mint`, `qty_atomic` (the balance quoted), `expected_out_atomic`,
  `quote_usd`;
- `provider` and `route_target`;
- `quoted_at` (provider stamp where given), `host_at`, `host_mono`;
- `lane`: `periodic_sweep`, `fastlane_wake` or `hot_pull_fallback`;
- a `status` for failures: a failed preview is a row with no price, never a 0.

The `fill` row carries the booked whole-bag proceeds and the fill's own chain time (EVM block
timestamp, Solana slot time). Rows are keyed by position, not by mint.

**Pairing (Gamma LOW 2).** Every `confirm`, `submit` and `fill` row carries the exit attempt id, or
the close or de-risk claim id. A decision preview then pairs with its own fill without a time join,
which is the point of storing both.

**Where.** Its own file, `data/memecoin_exit_price_tape.db`, never `sniper_tracker.db`. The ledger's
WAL stays untouched, as for the tick stream.

**How, off the fill path.** The preview call sites append a small tuple to a bounded in-process
queue: `collections.deque(maxlen=N)` under a lock, drop-oldest with a counter. A daemon writer
thread drains it every second on its own connection. No network call and no ledger write are added
to the sweep.
- Rule 5.2: the added work is one append per preview. Its p50 and p90 are measured at build over at
  least 10,000 appends, next to the sweep's own p50 and p90, before and after.
- If the writer dies, the queue fills and drops, and the counter pages. The sweep never waits.

**Measurement first (step A0).** Before persisting anything, a sampled counter of previews per
position-minute, by chain, lane and purpose, over 24 h. That answers "per tick or only near a rail"
from the running runner, not from code reading.

## 3a. Part A-lite: the confirmation tape (2026-10-01; this replaces §3 for the build)

**The §2 error.** `memecoin_route_series` (ledger table, live since 2026-08-26) already keeps the
exit path's whole-bag previews. It is buffered in memory and flushed by the maintenance pass. Its
seams are:
- `exit_decision`: the sweep's prepass and own previews, every chain;
- `exit_audit`: a preview fetched at the confirmation;
- `peak_preview`, `equity_mark` and `execution_quote`.

Gamma counts 104,971 Solana `exit_decision` rows on 917 positions, at about a 2 s cadence. The §2
table's "Solana: not stored" is false. That corrects the TP/SL A1 withholding (Gamma A2,
474644ac8).

Coverage, closed funded exits over the 7 days to 2026-10-01: an `exit_decision` or `exit_audit` row
lies within 2 s before `attempted_at` on:
- solana 183 of 204;
- 4663 35 of 47;
- 56 24 of 26.

**What route_series lacks, and A-lite adds:**
1. **An exact pairing key.** `observed_at` is the wall clock at record time and often lies AFTER
   `attempted_at`, so pairing a decision preview to its attempt is a time join.
2. **Failed previews.** route_series drops a preview without a price.
3. **Lane, monotonic stamps and the refusal code.**

**What.** One in-memory append at the confirmation site of `real_exit_sweep`, for every reason in
`_EXECUTABLE_PRICE_REASONS`. It runs after `record_exit_trigger_audit` and before the stuck backoff
and `exit_fn`. One row per confirmation, holding:
- `position_id`, `network_id`, `mint`, `reason`, `lane`;
- `decision_at`: the sweep's `now_iso`;
- `allowed` and `refusal_code`;
- `preview_src`: `confirm` (fetched at this site), `own` or `batch` (reused);
- `status`: `ok`, a `QuoteFailure` value, or `no_preview`. A failed preview is a row with a NULL
  price, never a 0;
- `expected_usdc`, `amount_atomic` (TEXT), `quoted_at`, `provider_id`, `provider_group`, `venue`,
  `route_target`, `expected_output_atomic` (TEXT), `context_slot`;
- `preview_ms`;
- `host_at`, `host_mono`.

**Pairing (the deviation from §3's carried id).** The executor writes
`memecoin_exit_execution_attempts.attempted_at = now_iso`, the same `now_iso` the sweep hands to
`exit_fn`, on both the Solana seam and the EVM seam. So:
- **confirmation to attempt** is an exact join on `(position_id, decision_at = attempted_at, reason =
  exit_reason)`;
- **attempt to fill** is an exact join on `sig = memecoin_fills.tx_sig`.

A test on the production sweep path with the production EVM executor pins this.

**Where and how.**
- **Own file:** `data/memecoin_exit_price_tape.db`. `start()` refuses the ledger path.
- **Queue:** `collections.deque(maxlen=4096)` under one lock, with a drop counter.
- **Writer:** a daemon thread drains the queue every second on its own connection to that file. It
  never opens the ledger.
- **Knob:** `MEMECOIN_EXIT_TAPE_ENABLED`, default off. Off, or not started, makes the append a
  no-op.

route_series stays the hold-time source; A-lite records nothing per tick.

**Edge cases (A-lite).**
1. **Money path.** The site is between the confirmation and `exit_fn`. The added work is an
   in-memory append, with no file, DB or network I/O. A raise inside the append is caught, logged
   with `logger.exception` and the row dropped. The exit is never changed.
2. **Speed.** Rule 5.2 asks for p50 and p90 of the append, measured over at least 10,000 calls at
   build. It runs only when a rail fires an executable reason, never per tick.
3. **Concurrency.** Several sweep lanes may append at once, and the lock covers the append and the
   drop counter. The module holds one tape reference. It is assigned once at runner start, before
   any sweep thread runs, and never reassigned.
4. **Absent, zero, unknown.**
   - No preview: status `no_preview`.
   - Failed preview: its `QuoteFailure` value with a NULL price.
   - A 0 is only a real 0 from the quote.
   - Atomic amounts are TEXT.
5. **Dependencies.** If the writer dies, the queue fills and drops, the drop counter rises in the
   heartbeat, and the sweep never waits. A failed batch write is logged and the batch is dropped.
6. **Units and identity.**
   - `decision_at` is the decision stamp and `host_at` the record stamp.
   - Neither is the fill time.
   - The fill's chain time comes in Part B.
7. **The sample.** Our confirmations only: our rails, wallets and hours.
8. **Rollback.** Turn the knob off. The file is separate and deletable.

## 4. Part B: post-exit follow-up (measurement-only, every chain)

**What.** After every funded full exit, whole-bag exact quotes of the EXITED quantity:
- every 2 s for the first 120 s, then every 10 s up to 900 s (Gamma's gap rule: at most 5 s early,
  at most 30 s later);
- 138 quotes per exit.

The rows go to the part A file with `purpose='post_exit'`. They never feed any decision, inventory
or rail. B covers EVERY funded full exit, which removes the selection bias of today's post-close tape
(`ada94cb5`: it covers only the newest 14 closed 4663 positions).

**Route (Gamma LOW 1).** On EVM the exited quantity is quoted on the SAME route our exit used: the
pinned pool or multihop path from the exit's own evidence. An aggregator exit (Kyber, Relay) cannot
be pinned at a block, so it is quoted on the entry's pool or path and its `basis` says so.

**Horizon (Gamma LOW 3).** 900 s is adequate:
- `max_hold_s` is 14,400 on every chain in the window;
- actual holds have p90 517 s on solana, 796 s on 4663 and 335 s on 56, and only 5% to 10% exceed
  900 s.

Expected censoring: a cell whose trigger would come more than 900 s after our exit is censored, on
roughly that 5% to 10% of units. It is measured, not assumed, once B runs. Option: add 30 s steps from
900 s to 1,800 s, +30 quotes, 168 per exit; this lowers that share. It is decision 2b below. The model is the V6 post-exit follow-up (`docs/superpowers/specs/2026-07-27-memecoin-v6-post-exit-followup-design.md`).

**Its own process**, under its own watchdog, reading exits from the ledger read-only (cursor on
closed positions). Nothing is added to the runner.

**Hosts and budget.** Exits over the last 7 days: solana 33.1 a day, 4663 8.1, 56 4.6, 8453 0.3,
5042 0.3.

| chain | quote | host (never a funded host) | budget |
|---|---|---|---|
| 4663 | on-chain quoter `eth_call` with the exited qty | research QuickNode, 0.4 s spacing | about 1,120 calls a day |
| 56 | on-chain quoter | operator's keyed dRPC account (`MEMECOIN_RESEARCH_DRPC_RPC_URL_56`), 1/1.5 s through the account's shared pacer (was https://1rpc.io/bnb until 2026-10-02) | about 635 a day |
| 8453 | on-chain quoter | base.gateway.tenderly.co | about 40 a day |
| 5042 | — | BLOCKED: both Arc hosts are in `taken_budgets()` | — |
| solana | see below | see below | about 4,570 quotes a day |

Every lane stops on its first 429 and backs off exponentially (2 s doubling to 300 s). A dropped
horizon is recorded as `status='skipped'`, never interpolated.

**Solana is the hard constraint, and it is an operator decision.** A quota that can *never* 429 the
funded Jupiter lane is not available on this host:
- Jupiter limits by IP as well as by key. The 2026-08-29 incident (funded position 45003 blinded 48
  times in 62 s) was per-IP contention between two consumers holding different keys
  (`solana_real_seams.py`, "ATTRIBUTION CORRECTED"). The paper and capture keys
  (`MEMECOIN_PAPER_JUPITER_API_KEY`, `MEMECOIN_CAPTURE_JUPITER_API_KEY`) do not isolate the IP.
- The research Helius key is flat-book gated, so it would read almost never right after a funded
  exit.

Options:
- **(B1) Pool-state quotes via a separate provider account.** A separate Helius account, as the
  slot-end shadow's live variant requires (decision `1e97eebe`), reads the pool's own reserves and
  prices the exact whole-bag sale by the venue's math. It covers constant-product venues (PumpSwap,
  Raydium CPMM, the pump curve), which are most funded Solana coins. CLMM and DLMM are recorded as
  `unpriced`. No Jupiter volume at all.
  - **Effective reserves, never raw vaults (Gamma MEDIUM, her 09-29 finding).** A raw-vault spot is
    wrong on both main venues:
    - PumpSwap prices on the vault plus a virtual quote reserve; a raw spot reads too low.
    - Raydium CPMM vaults include accrued fees; a raw spot reads too high.

    B1 uses the effective reserves: the PumpSwap virtual quote; the CPMM vault minus the accrued
    fees; on the pump curve, vT = the token account + 73,000,000.
  - **Validation before any B1 row is graded.** B1's whole-bag value at our own exit slot is
    compared with our booked fill proceeds, on at least 30 Solana exits per venue. The tolerance is
    stated before the check (proposed: within 2 points on at least 90% of exits). A venue that fails
    stays `unpriced`.
- **(B2) Jupiter on the paper key from another egress IP.** It needs a second network path.
- **(B3) Jupiter on the paper key from this IP**, with a hard 1-quote-per-2 s cap and a pause while
  the funded lane reports any 429. It *reduces*, but cannot exclude, contention with funded quotes.
  Not recommended against the order's "can never 429".

**Recommendation: B1 for Solana, and the EVM lanes as tabled.**

## 4a. Part B build, EVM (2026-10-01)

**Process.** `scripts/memecoin_exit_tape_follow.py` is one job, started each minute by
`scripts/memecoin_exit_tape_follow_watchdog.sh`, under its own run lock. The watchdog is
release-pinned and restarts the job on a stale heartbeat. It is default off and starts only with
`.env` `MEMECOIN_EXIT_TAPE_FOLLOW_ENABLED=1`. `DB_PATH` is pinned to the state root's ledger,
because the V4 PoolKey cache reads through it. Cron
restarts it within a minute if it dies. Its threads:
- **Main thread:** polls the ledger every second on a `get_readonly_connection` handle.
- **One worker per chain:** owns that chain's `ExactLane` (the refusal-capture lane class) and its
  own connection to the tape file.

Nothing is added to the runner.

**Ledger reads.**
- New full exits: `memecoin_fills.id > cursor` on real positions, `kind NOT IN ('entry','derisk')`,
  `tx_sig IS NOT NULL`. That is `bracket_exit`, `manual_close` and `mirror_exit`. A write-off has no
  tx and is excluded.
- Their attempts: `memecoin_exit_execution_attempts.sig IN (...)`, one batch per poll. `sig` is
  unique among non-NULL rows: 1,706 of 1,706.
- No correlated subquery and no open transaction.
- The first start sets the cursor to the newest fill, with no backfill: a past exit's 900 s horizon
  is already gone.

**Tables in the tape file.**
- `exit_fills`: one copied row per full exit, with its attempt and, later, its chain block and time.
- `exit_followups`: one per exit. Fields: status, anchor, exited quantity, route and route basis.
- `post_exit_quotes`: one row per slot, `UNIQUE(fill_id, slot_s)`.
- `meta`: the cursor.

**Schedule.** Slots at 2 s steps to 120 s, then 10 s steps to 900 s: 138 slots. Slot `k` may be
quoted from its due time until the next slot's due time; after that it is a `skipped` row. Every row
stores its real `age_s`, so a reader applies the gap rule (at most 5 s early, at most 30 s later)
itself.

The anchor is `closed_recorded_at`, the host clock when the fill was booked. The chain block and
time of the fill are read after the horizon, when the host is idle, and stored on `exit_fills`.
Early slots never wait on them.

**Exited quantity.** Two sources:
- Preferred: the A-lite confirmation's exact `amount_atomic`, joined on `decision_at = attempted_at`.
- Otherwise: the fill's `qty` times the token's decimals, read on the research host.

`qty_basis` names which was used.

**Route (Gamma LOW 1).** One scan at the entry clip (`ExactLane._scan`). The chosen route is
recorded with a `route_basis`:
1. the V4 pool our exit quoted, from the last `memecoin_route_series` evidence row before the close:
   `exit_pool`;
2. else the entry's receipt-proven pool, `memecoin_positions.executed_pool`: `entry_pool`;
3. else the best buy route: `scan_best`.

When a V4 pool is known, the scan asks the V4 quoter only, at most 8 pools. A full V4+V3+V2 scan cost
28 calls on 4663 in the live check, the first five slots. If the V4-only scan finds nothing, a full
scan runs.

Every later slot SELLS the exited quantity on that route. Aggregator, mixed V3/V2 and Kyber exits
fall to `entry_pool` or `scan_best`, and the basis says so.

**Live check (2026-10-01, scratch tape, research hosts).** Two real exits, anchored at "now":
- 4663 fill 136712 (a V4 take-profit): route `v4:0x2635…`, basis `exit_pool`, quoted at 42 ms.
- 56 fill 136643 (a `pcs_mixed_v3_v2` exit): route `scan_best` V2 via the flap pair token. The scan
  took about 30 s at 1/s, so slots 2 to 36 were `skipped`.

Expected on 56: 1rpc.io returned 429 after 35 calls at 1.05 s spacing (2026-09-30). A 56 follow-up
(about 20 scan calls plus 138 quotes) will hold, and its tail will hold for longer. It shows up as
`held` rows, never as silence.

**Shared research key on 4663 (Gamma LOW 1, 2026-10-01).** The follower is a SUSTAINED consumer of
`MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663`: up to 2.5 calls/s for 15 minutes per exit.

Per exit it makes:
- at most 8 V4 scan calls;
- 138 quotes;
- 2 chain-time reads.

That is about 150 calls. Funded 4663 full exits ran 48 in the 7 days to 2026-10-01, about 7 a day,
so the load is about 1,000 calls a day. At Gamma's upper case of 40 exits a day it is about 6,000.
The plan's monthly credit allowance is not known to this build; the operator confirms it before the
switch goes on.

The key draws a 429 on bursts of about 10 calls in 1.5 s. The lane's bucket caps any one second at
twice its rate, 5 calls.

Coordination:
- While a 4663 exit is followed, any other research read on this key must expect 429s.
- Before a burst, check the follower's heartbeat (`data/memecoin_exit_tape_follow.heartbeat`,
  `workers.4663`):
  - `active` greater than 0 means an exit is being followed;
  - `limits` and `hold_s` show whether it is already being refused.
- If it is active, the reader throttles to the leftover rate, about 1 call/s, or waits.

Every follower read path backs off: scan, slot quote and chain time. A new limit doubles the next
hold, 2 s to 300 s. The heartbeat carries `limits`, `held_rows` and `hold_s` per chain.

**Watchdog test (Gamma LOW 2).** `tests/test_memecoin_watchdog_exit_tape_follow.py` runs the
watchdog only inside `unshare -c -p -f --mount-proc`. It covers six cases:
- a cold start;
- the env switch;
- old-release replacement;
- a stale heartbeat;
- a healthy follower left alone;
- a foreign state root never signalled.

Its name keeps it inside the covering-set exclusion `test_memecoin_watchdog_`, because it acts on a
process table.

**Hosts.**

| chain | host | rate |
|---|---|---|
| 4663 | `MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663` | 2.5/s |
| 56 | `MEMECOIN_RESEARCH_DRPC_RPC_URL_56` (keyed dRPC) | 1.5/s, and the account's one cross-process pacer at 1/1.5 s shared with every other dRPC reader |
| 8453 | `https://base.gateway.tenderly.co` | 2/s |

- `MEMECOIN_EXIT_TAPE_READ_URL_<chain>` overrides a host.
- **56 moved off https://1rpc.io/bnb (Alpha 2026-10-02).** That host seeds the BSC fast lane's hop
  pairs (`memecoin_bsc_v2_live.HOP_SEED_URL`), so research load there could 429 a production tape.
  The hop seed is now a taken budget for every guard, so no override can name it. A dRPC limit
  answer (HTTP 429 code 15) pauses every reader of the account for 60 s. Every `post_exit_quotes`
  row records `read_budget` and `read_pace_s` (the minimum spacing between reads), so 56 rows say
  they came from the coarser shared account.
- **Refusals:** a host whose registrable domain any non-research role uses is refused. That is the
  capture guard's derived set, with `*RESEARCH*` variables excluded. So is a host that answers
  `eth_chainId` with another chain.
- **Pending:** a host that does not answer yet is retried on the capture lane's backoff.
- **5042:** blocked.
- **solana:** status `no_lane` until stage 3.

**Edge cases (B, EVM).**
1. **Money path.** None: own process, ledger read-only, nothing in the runner.
2. **Fill path speed.** Unchanged. One PK-range read a second and one small `IN` read per batch, on
   a read-only handle with no long transaction, so the WAL checkpoint is not pinned.
3. **Concurrency.** Each chain's lane, cache and connection are owned by one thread. The main
   thread hands exits over through a queue. No module global changes.
4. **Absent, zero, unknown.**

   | status | meaning |
   |---|---|
   | `skipped` | the slot's window passed, never interpolated |
   | `no_route` | the quoter reverted or priced nothing |
   | `held` | the host was held at the slot, a 429 or a transport failure |
   | `price_unknown` | the quote-token output was kept but the native USD was unknown |

   A 0 is only a real 0 from the quoter; a revert is None.
5. **Dependencies.**
   - A limit holds that chain's host only. The hold doubles from 2 s to 300 s and resets on a good
     quote.
   - An identity mismatch refuses the chain for good.
   - The process dies: cron and flock restart it. Active follow-ups resume, and slots past their
     window become `skipped` rows.
6. **Units.**
   - Atomic amounts are TEXT.
   - The output is in quote-token atoms (WETH or WBNB).
   - `value_usd` uses the lane's Binance native rate at the slot (60 s cache). `quote_asset_usd` is
     stored on every row.
   - Fill proceeds are booked USD.
   - Ages run from `closed_recorded_at`. The chain time is stored beside it, never mixed in.
7. **The sample.** Every funded full exit on a chain with a usable host, from the first start. The
   quotes are at our own size, so the impact is ours. `route_basis` says whether the route is our
   exit's.
8. **Rollback.** Remove the cron line and kill the process. The tables are in the tape file only.

## 4b. Stage 3a: B1 validation on our own exits (2026-10-01, 0 credits)

Script `scripts/research/exit_tape_b1_validation.py`; rows in
`logs/acceptance/exit_tape_b1_validation.jsonl`. The input is every funded Solana full exit since
2026-09-01 (596). The transactions are the clip-ceiling study's cache (465), with nothing read. The
131 exits after 09-27 are uncached and not needed: both passing venues already exceed 30.

The pre-trade state our own exit tx saw is the state a B1 read at that slot would return. Two checks
per exit:
- **math:** B1's model against the program's own output;
- **e2e:** B1's first-hop output in USD (SOL at the Binance 1-minute close) against our booked proceeds.

The tolerance is 2 points on at least 90%, with at least 30 exits per venue.

| venue | n | math | e2e within 2 pts | verdict |
|---|---|---|---|---|
| PumpSwap | 156 | gross exact 156/156; event reserves = raw vault pre-balances 156/156 | 135/135, p10 -0.02%, p50 +0.03%, p90 +0.29% | **pass** |
| pump curve | 39 | gross exact 39/39 (reserves from the event: fee arithmetic only) | 39/39, p50 +0.03%, p90 +0.15% | **pass** |
| Raydium CP | 131 | effective reserves (event `vault_before`) exact 131/131; RAW vault 89/131, p50 +0.84%, p90 +8.9% | 6/6, only 6 WSOL/USDC-quoted | **math pass, e2e n<30** |

Findings:
- **CP fees.** `in_amount` is already net of a Token-2022 transfer fee. Output = `out_before *
  (in_amount - trade_fee) / (in_before + net)`, less `creator_fee` on the output side. Subtracting
  the transfer fee again ran 1% low.
- **CP quote assets.** 125 of 131 CP exits are coins paired against ANOTHER token: other
  memecoins, PUMP, JUP and tokenized stocks (`Xs…`). Only 4 are WSOL and 2 USDC. 21 of 156 PumpSwap
  exits are the same. B1's USD value for those needs a second-hop read of the quote token. Until
  that read exists they stay `unpriced`.
- **CP read model.** The live CP model is vault minus the PoolState fee fields. This equals the
  event's `vault_before` by the program's definition, but 3a cannot read a past PoolState. The first
  3b reads must compare the two on the same slot before any CP row is graded.
- **Curve read model.** The live curve read uses the bonding-curve account's virtual reserves. The
  3a check is fee arithmetic only, so the first 3b reads must match an account read against the next
  TradeEvent.

**Scope, as measured (Gamma CLEAN 2026-10-01).**
- **Live-read validated: PumpSwap only.** The event reserves equal the raw vault pre-balances on
  156/156, and e2e is 135/135 within 2 points (p50 +0.03%).
- **Model-only: the pump curve and Raydium CP.**
  - The curve reserves came from the event (open item 2).
  - CP's raw vault priced only 89/131 within 2 points (open item 1).

  Neither is graded in 3b until its live read is validated against same-slot events.
- **Coverage.** B1 prices about 37% of the cached funded Solana exits: 135 SOL-quoted PumpSwap plus
  39 curve, out of 465.
  - 139 cached exits are on other venues.
  - Non-SOL quotes are unpriced (open item 3).
- **The 3b rule.** Grade only rows on a live-validated venue with a SOL or USDC quote. Every 3b
  result carries the per-venue coverage. An unpriced exit is censored and counted, never dropped
  silently.

3b and 3c wait on the operator's credit budget.

## 4c. Stage 3b build: Solana B1 follow-up (2026-10-01, operator GO, budget covered)

**Switch.** `MEMECOIN_EXIT_TAPE_SOLANA_ENABLED=1`, its own knob, default off, inside the same
follower process (so `MEMECOIN_EXIT_TAPE_FOLLOW_ENABLED=1` and the cron line are needed too). Host:
`MEMECOIN_RESEARCH_HELIUS_RPC_URL`, the paid research key. Flat-book exempt (operator order). The key is
refused if any non-research variable holds the same key (`key_id`), not by domain: the funded pool is
on the same provider domain.

**Per exit** (`SolanaWorker`, `sniper/services/memecoin_exit_followup.py`):
- One `getTransaction` of our exit. It gives the block time (stored on `exit_fills`, no read after the
  horizon) and our own PumpSwap SellEvent: the exact `base_amount_in` (`qty_basis='exit_event'`) and the
  fee bps (lp + protocol + creator) the exit paid.
- One `getMultipleAccounts` of the pool: its base mint must be the position's mint and its quote WSOL
  or USDC.
- Each of the 138 slots: one `getMultipleAccounts` of the pool and its two vaults. Value = (quote vault
  + the Pool's virtual quote) x qty / (base vault + qty), less the exit's fee bps; USD at the Binance
  SOLUSDT price (60 s cache), or 1.0 for USDC.

**The 3b rule, as built.** Valued only: our single PumpSwap sell in a SOL or USDC pool. Every other
exit is an `unpriced` follow-up with its reason in `route_label`: `pump_curve`, `raydium_cp`,
`other_venue`, `multi_leg`, `pumpswap_quote_other`, `pumpswap_mint_mismatch`, `pumpswap_pool_unread`.
The curve and CP stay unpriced until their live reads are checked against same-slot events (open
items 1 and 2).

**Budget.** 140 calls per valued exit (1 + 1 + 138), at most 2 calls/s. At 33.1 Solana exits a day
that is about 4,630 calls a day, about 139,000 a month, if every exit were valued. Unpriced exits cost
one call. The Helius credit weight of each method was not checked.

**Edge cases (3b).**
1. **Money path.** None: own process, ledger read-only, a research key no funded role holds.
2. **Fill path speed.** Unchanged; nothing in the runner.
3. **Concurrency.** The reader and its hold are owned by the Solana worker thread.
4. **Absent, zero, unknown.** `held` (429, 5xx or transport), `rpc_error`, `no_route` (an account
   missing), `price_unknown` (SOL/USD unread), `skipped` (window passed), `unpriced` (venue or quote
   not validated). An exit tx not yet visible is retried each second until its horizon.
5. **Dependencies.** A limit holds the key 2 s doubling to 300 s; a read that answers resets it. The
   graph and other research jobs share this key: expect 429s on both while an exit is followed.
6. **Units.** Atomic amounts are TEXT; quote atoms are 9 (WSOL) or 6 (USDC) decimals; the fee bps are
   the exit's own (a dynamic fee tier change inside 900 s is not modelled).
7. **The sample.** Every funded Solana full exit while the switch is on; the valued share is about the
   PumpSwap SOL/USDC share (135 of 465 cached exits in 3a), the rest counted as unpriced.
8. **Rollback.** Set the switch to 0 and restart the follower; the rows live in the tape file only.

## 5. Part C (optional): Solana hold-time grading quotes

To grade cells far from the default rails on Solana, a whole-bag price every 2 s during the hold,
from the same lane as B (B1 source). Budget: 33 positions a day x median hold x 0.5/s. It is
measured from the hold-time distribution before any build. Operator's call. Without it, Solana cells
stay gradable only near 25/-35.

## 6. Edge cases (CLAUDE.md rule 5)

1. **Money path.** A: preview call sites inside the exit sweep, which is money-adjacent. The only
   change is an append; no decision reads the tape. B and C: none; own process, read-only on the
   ledger.
2. **Fill path speed.** A adds one locked append per preview, measured (p50, p90) at build. B and C
   add nothing to the runner.
3. **Concurrency.** The queue is the only shared state: `deque` with `maxlen`, one lock, one writer
   thread. The drop counter is atomic under the same lock. No module globals change elsewhere.
4. **Absent, zero, unknown.** A failed or skipped quote is a row with a status and no price. An
   `unpriced` venue under B1 is explicit. A 0 output is only a real 0 from the quoter.
5. **Dependencies.** Timeout, 429, a wrong answer (the quoter reverts), or a changed pool: each is a
   status. A stop-on-429 pauses its lane only. The writer thread dying pages and drops; the sweep is
   unaffected.
6. **Units and identity.**
   - Atomic amounts are TEXT (over 2**63).
   - `qty_atomic` is the balance actually quoted.
   - The fill row is keyed by position with its tx.
   - Chain time comes from the block or slot, never from a delayed receipt read.
   - `closed_at` is NOT the fill time on 205 of 357 closed 4663 attempts (it equals the decision
     stamp); `closed_recorded_at` and the chain time are.
7. **The sample.** The tape is per funded exit, so it describes our exits: our wallets, rails and
   hours. B quotes the exited quantity, our size, so impact is ours. Post-exit quotes do not include
   our own sale's effect on the pool after the fill beyond what the chain shows.
8. **Rollback.** A and B are each behind their own knob, default off. The files are separate and
   deletable. Nothing downstream reads them except the research replay.

**Check left behind at build:**
- a guard test that no decision path imports the tape module;
- a test that a failed preview writes a status row with a NULL price;
- the A0 counter's 24-hour report.

## 7. Decisions for the operator

1. Build A (and run A0 first)?
2. B on EVM (4663, 56, 8453) on the research hosts? 2b: extend the horizon to 1,800 s (168 quotes
   per exit: 4663 about 1,360 a day, 56 about 770, 8453 about 50)?
3. B on Solana: B1 (a separate Helius account to provision), B2 (a second egress IP), or no Solana?
4. C (Solana hold-time quotes)?


## Amendment 2026-10-09: Alpha handoff completion

Solana B1 reads Pool.virtual_quote as signed i64. Effective quote vault plus virtual quote <= 0 produces no_route with null output and USD value; a negative virtual quote with positive effective reserves is still valued. No historical rows are rewritten or funded policy changed.

Evidence and limits: `docs/audits/2026-10-09-codex-alpha-handoff-closeout.md`.
