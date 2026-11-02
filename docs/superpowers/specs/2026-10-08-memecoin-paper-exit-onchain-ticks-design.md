# Paper exit decisions on on-chain ticks, not DexScreener mids — design

Date: 2026-10-08. Builder: Beta. Order: Alpha, after the operator's complaint ("didn't we fix
paper trades to use realistic subsecond reads, not a DexScreener mid chart"). Review: Gamma.
Status: APPROVED 2026-10-08 (Gamma PASS f8fa4dd90; Alpha GO on parts a and b; operator rulings
in §6 amend §3.2, §4 and §5 for the Solana key and 56). Building.

## 1. What is wrong today (measured 2026-10-08)

- **Fills are already executable.** Paper entries price on `paper_quote`; paper exits on
  `executable_exit_fill` (an exact route quote on the PAPER Jupiter key), never on a mid.
- **Decisions are not.** `memecoin_runner.bracket_pass` runs every ~15 s on its own thread. It reads
  one DexScreener batch (GeckoTerminal for blind EVM chains) and `memecoin_exits.sweep_exits` fires
  every paper rail (TP, stop, trail, timecap, derisk) on that mid. The only on-chain input is
  `RecentSwapMarks` (Solana swap/state marks) overlaid as `supplemental_price_map`, and those exist
  only for coins a FUNDED position also holds: `fastlane.open_swap_mark_positions` subscribes a paper
  mint only when a real position shares its origin (cap 16).
- Mark tape since 2026-10-06: Solana 81,786 DexScreener vs 4,534 `fastlane_sol_swap`, 568
  `fastlane_sol_curve`, 236 `fastlane_sol_state`; 4663 14,342 DexScreener vs 1,078 `fastlane_rpc`;
  56 5,142 vs 729; 8453 1,709 vs 58 (the EVM `fastlane_rpc` rows are funded positions only).
- Funded exits decide on sub-second on-chain marks (Solana vault/curve stream) or exact previews
  (EVM `fastlane_rpc`, MEMECOIN_FASTLANE_DECIDES=1), with stop confirmation 1 sweep / 0 s.
- So a paper close since 10-06 (Solana funded_clone n=1,065: real_tp 461, b_stop 374, b_timecap
  123, unquotable 95) was decided 0-15 s late on a mid that lags the pool. Paper does not track
  funded, and every paper verdict inherits that.

## 2. Goal and non-goals

Paper exit decisions use the same KIND of tick funded uses: the pool's own on-chain state, read
off paper-only connections. DexScreener and GeckoTerminal leave exit decisions entirely. A missing
or stale tick HOLDS the price rails (censored, never a mid); time rails still fire.

Non-goals: no change to paper entries, paper fills, funded exits, the funded swap-mark lane, or
any funded seam. No Jupiter calls for ticks. The DexScreener batch may stay for display and the
mark tape, labelled as such; it never decides a paper exit under the new engine.

## 3. Design

### 3.1 Engine versioning (Strategy Lab doctrine: never mix rows)

- New paper exit engine id `onchain_ticks_v1`, frozen into each paper position at ENTRY (the
  position's frozen policy JSON gains `"exit_engine": "onchain_ticks_v1"`), behind
  MEMECOIN_PAPER_EXIT_ENGINE (default `dexscreener_sweep_v0`, today).
- A position opened before the switch keeps `dexscreener_sweep_v0` (absent key = v0) and closes on
  today's sweep, unchanged. `sweep_exits` skips a v1 position ENTIRELY, time rails included (Gamma
  1: otherwise both loops race the v1 timecap and the data cannot say which one decided); only the
  arm-agnostic 14-day backstop still sees it. The v1 loop owns every v1 rail.
- Reports and Gamma's reads split by `exit_engine`; v0 rows are a labelled legacy diagnostic,
  never pooled with v1. The cutover time is recorded once (a brain fact plus the knob's .env line).

### 3.2 Tick sources, per chain

| Chain | Tick | Read path | New provider load |
|---|---|---|---|
| Solana | pool vault reserves / pump curve account, as funded's `fastlane_sol_swap/state/curve` | a PAPER vault stream (own process thread, own WebSocket on a NON-funded key) | WS subscriptions; reads below |
| 4663, 8453 | the coin's busiest pool's last swap (`pool_ticks` sqrt price / v2 reserves) | the existing on-chain swap tape `data/memecoin_onchain_volume.db`, read-only | none |
| 56 | an exact whole-bag sell quote on the paper entry's own route (`bag_quote`) | a paper poller on the dRPC 56 research key through `drpc_paced` | ≤ 1 eth_call per open 56 paper position per 5 s (§4) |

**Solana paper stream.** Reuse `memecoin_solana_swap_marks`' pieces (`VaultTape`, `CurveTape`,
`build_price_book`, the STATE-mark corroboration band, the curve pricing) in a second instance with
its own cache, its own socket and its own binding table rows. It never shares `_swap_mark_cache`,
the funded read endpoint (`solana_read_rpc`, the public IP the funded lane uses), or the funded
Helius keys.

**Binding (Gamma 3): the pool our paper fill used, as funded binds to its own fill.**
- At paper ENTRY, persist the paper Jupiter quote's last-hop pool (`routePlan[-1].swapInfo.ammKey`)
  as `memecoin_positions.executed_pool`, `executed_pool_source='paper_quote_route'`. Today 0 of
  1,084 Solana paper positions since 10-06 carry one.
- The tick binds to that pool. Only when the entry recorded none, the SOURCE swap's pool is the
  fallback: `swap:<venue>:<sig>:...` and `pump:` origins carry the source signature, and one
  `getTransaction` of it resolves the pool with the funded resolver. Which one bound is recorded
  per position (`paper_tick_binding` = `entry_route` | `source_swap` | `unbound`).
- **Graduation:** a position bound to a pump curve rebinds to the coin's migrated PumpSwap pool
  when the curve completes (the funded lane's graduation path names that pool); until the rebind
  lands the position HOLDs, and the rebind is counted.
- A coin whose pool cannot be bound has no tick and HOLDS for its whole life (counted, never a mid).

**EVM tape.** The tape holds every V3/V4 swap on 4663 and V2/V3 on 8453 for about an hour (~91k
4663 rows/h). A v1 position's tick is the newest swap of its mint's busiest pool (the M1 decide
code's `TapeState` choice), converted to USD at the tape's own quote price. Older than
MEMECOIN_PAPER_TICK_MAX_AGE_S: no tick.
- Gamma 4: funded EVM decides on an exact whole-bag preview (`expected_usdc / open_qty`), a tape
  tick is the last swap's post-trade SPOT. So a V2 pool is priced at bag size from its reserves
  (the exact constant-product amount out for our open quantity, `tick_kind='v2_bag_exact'`); a
  V3/V4 pool keeps the spot (`coin_price`), labelled `tick_kind='v3_spot'`/`'v4_spot'`. On a thin
  V3/V4 pool spot fires a TP earlier and a stop later than funded: reads split by `tick_kind`.
- **56 (BSC): a paced exact-quote poller** (operator ruling 2026-10-08, overriding time rails only).
  There is no 56 tape, so the tick is what funded EVM decides on: an exact whole-bag sell quote,
  `amountOut(open_qty) / open_qty` in USD, `tick_kind='bag_quote'`. It is the paper entry's own
  persisted route (`evm_route_json`, written at paper entry from the quote's `route_binding`), read
  by `_evm_bound_amount_out`: V2 `getAmountsOut` or V3 quoter, one eth_call per read once the route's
  pools are bound. A position with no persisted route is `unbound` and HOLDs (no discovery sweep).
- Host and pacing: ONLY `MEMECOIN_RESEARCH_DRPC_RPC_URL_56`, every call through
  `memecoin_research_pacer.drpc_paced` (the dRPC account's one cross-process pacer, shared with
  the BSC hop seeds, the exit tape, the verify-j7-56 cron and the buy-tax shadow). Each open 56 v1
  position is read at most once per MEMECOIN_PAPER_TICK_56_PERIOD_S (default 5 s); nothing is read
  while no 56 v1 position is open.
- Stop on 429: a limit answer pauses the whole dRPC account 60 s (the pacer's existing pause). The
  poller then skips without blocking while `drpc_paused_s(url) > 0`; price rails HOLD. A quota
  answer (-32001 "usage limit") or a third limit inside one hour stops the poller for the life of
  the process, logged and paged once (`PAPER-TICKS-56`); price rails HOLD until restart.
- Unset key: no poller, price rails HOLD, reported as `no_price_rail` (today's 56 group).

### 3.3 The decision loop

- One paper tick thread per process (`memecoin-paper-ticks`), waking on a new paper mark (Solana)
  or every 1 s (EVM tape), never on the funded loop. It evaluates only v1 positions, through the
  SAME rail functions the v0 sweep calls for that arm (`_frozen_real_shadow_reason` for
  `funded_clone`/`real_shadow`, `trail_d_exit_reason`, etc.), so the rails are unchanged and only
  their input changes.
- Stop confirmation: the position's frozen funded contract values (today 1 sweep, 0 s), read in
  ticks, not 15 s sweeps.
- Price used for a decision: a tick no older than MEMECOIN_PAPER_TICK_MAX_AGE_S (default 5 s, the
  funded swap-mark freshness). Stale or absent: price rails HOLD; the hold is counted per position
  (`paper_tick_hold_s`, beside the hold time).
- **Pre-declared censor (Gamma 2a):** the primary v1 read takes positions whose live-tick share of
  hold time is >= 80%; the rest are reported separately, never pooled into a verdict.
- **Decision mark source, funded and paper (Gamma 2b):** every exit decision records the mark it
  decided on: `source` (e.g. `fastlane_sol_swap`, `fastlane_rpc`, `dexscreener`, `paper_sol_state`,
  `tape_v4_spot`), its age in ms and its `tick_kind`, in the existing `exit_intent_json`. Today no
  funded Solana close since 10-06 (n=54) carries one, and whether funded falls back to the
  DexScreener mid when its swap mark is stale is believed, not measured. This is the one funded
  touch: a field written beside a decision already being made, after it is made; it changes no
  rail, no order and no timing. Its own edge entry is below.
- Time rails (timecap, max-hold) fire on the clock with or without a tick.
- The close itself is unchanged: `_close` → `executable_exit_fill` on the paper key. An
  unavailable quote is today's `_noquote` censor.

## 4. Load (concurrent open paper positions, 2026-10-05..08)

- Solana p50 18 / p90 25 / max 30 → at most ~30 curves or ~60 vaults subscribed. ~500 paper opens
  a day → ~500 `getTransaction` binds a day. Seeds: one read per subscribed account per epoch; at a
  300 s paper epoch (funded uses 30 s) that is ≤ 60 × 288 = ~17k reads a day. Frames: unmeasured;
  the heartbeat counts frames and bytes per minute from the first minute.
- 4663 p90 8 / max 10, 8453 p90 1 / max 2: local SQLite reads of the tape only, no RPC.
- 56 p90 4 / max 5: ≤ 5 × one read per 5 s = 1 call/s at most, ~86k a day if 5 were open all day
  (operator-approved figure); at p50 open counts it is well under that. The pacer allows 1.5/s
  account-wide, so at the cap the poller holds two thirds of the account's pacer slots and every
  other dRPC 56 user waits longer (FIFO slots, no starvation). The hop seeds warm the BSC fast lane
  but run off its fill path. Calls and limit answers are counted in the heartbeat.

## 5. Edge map (CLAUDE.md rule 5)

1. **Money path:** no. Paper only. It must not touch a funded seam: its own socket and key, its own
   cache and thread, read-only on the tape and ledger except the paper rows it already owns. A test
   pins that the paper stream never receives the funded cache or the funded read URL.
2. **Speed on the fill path:** none. Nothing runs on the funded loop or the funded lanes.
3. **Concurrency:** a new thread with its own connections. The v0 sweep and the v1 loop never
   decide the same position (engine column, plus `claim_close` already makes a close single).
4. **Absent / zero / unknown:** no tick = HOLD and counted, never 0 and never a mid; a pool that never
   binds = blind for its whole life, counted, and censorable in reads; a 0 price from a drained pool
   is a real tick (the stop should fire) but goes through the existing collapse quarantine.
5. **Dependency failures:** a dropped socket or 429 backs off on the paper key only; meanwhile price
   rails HOLD. A wrong STATE mark on concentrated pools is the measured trap (DLMM median 4.40x):
   the same corroboration band as funded. A tape gap (writer down) reads as stale, so HOLD.
6. **Units:** pump curve vs AMM pricing, token decimals, quote mint (WSOL vs USDC) as in the funded
   lane; EVM sqrt-price orientation (`coin_price` already tested). Host epoch for tick ages.
7. **Sample:** v1 rows are the paper positions opened after the switch. Blind ticks are counted, so
   "fraction of hold with a live tick" is reported beside every v1 result. Positions on 56 read a bag
   quote, not a spot tick, and are reported by `tick_kind`; 56 positions that never bind (or run
   while the poller is stopped) are the `no_price_rail` group.
   *Caveat (2026-10-08, engineering review C09):* until the fix that binds EVM tape health to the
   pool's protocol, a 4663/8453 tick was "live" whenever the header socket delivered, even while
   that pool's V4/V3 log stream lagged; its last swap was then carried as current. The new rule
   only adds conditions, so live-tick seconds and shares on EVM v1 rows before the fix are at least
   what the new rule would have counted, and price-rail decisions inside a lagging interval may
   have used a stale swap. Those intervals are not reconstructed; read pre-fix EVM rows with this
   caveat and never pool them with post-fix rows as one live-share cohort.
8. **Rollback:** MEMECOIN_PAPER_EXIT_ENGINE back to v0 for new entries; open v1 positions keep
   being decided by the v1 loop until they close (never switched mid-life), or the loop stops and
   the 14-day backstop closes them, labelled.
9. **The funded decision-source field (Gamma 2b):** money path yes (the funded exit decision), but
   write-only and after the decision: the source/age come from the mark object already in hand, no
   new read, no lock, no I/O beyond the existing exit_intent write. An absent mark records
   `source=null`, never a guessed one. Test: a funded close's exit_intent_json names the mark the
   rail read, and the rail's verdict is byte-identical with and without the field.

## 6. Rulings (operator, through Alpha, 2026-10-08)

- **Solana paper key:** the second research key, `MEMECOIN_RESEARCH_HELIUS_RPC_URLS` entry 1
  (key id `dea05c94`), continuously, as an operator exception to its flat-book rule. It gets its own
  explicit knob, MEMECOIN_PAPER_TICKS_HELIUS_URL (unset: no Solana paper stream, Solana price rails
  HOLD). Startup refuses the stream when that URL's key id equals any funded Solana key
  (`MEMECOIN_SOLANA_RPC_URL`, `MEMECOIN_SOLANA_RPC_POOL_URLS`), `ONCHAIN_WATCH_HELIUS_RPC_URL`, or
  `e1f95763` (option C's deposit stream, `MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL`); the refusal is
  logged with the key ids only. Open: research lanes that rotate through
  `MEMECOIN_RESEARCH_HELIUS_RPC_URLS` can still land on dea05c94, and Helius credits are per
  ACCOUNT: if dea05c94 and e1f95763 share an account, the paper stream spends option C's credits.
- **56:** the dRPC poller of §3.2 (supersedes time rails only).
- **Cutover:** every paper arm at once (today only `funded_clone` opens).

## 7. Tests to leave behind

- `sweep_exits` never decides a v1 position, time rails included; only the 14-day backstop.
- A paper entry persists its quote's last-hop pool; the tick binds to it, the source swap only as
  fallback, and the binding kind is recorded; a completed curve rebinds to its PumpSwap pool.
- V2 bag-size pricing equals the constant-product amount out; V3/V4 ticks carry `tick_kind`.
- Every exit decision, funded and paper, records its mark source and age.

- A v1 position is never decided by the v0 sweep; a v0 position never by the v1 loop.
- No tick or a stale tick: price rails hold, timecap still fires, hold seconds counted.
- The same rail function gives the same verdict on the same price for v0 and v1 (only the input
  changes).
- The paper stream's binding from a recorded source swap (pump curve, PumpSwap, CPMM fixtures).
- STATE-mark corroboration rejects an uncorroborated concentrated-pool mark.
- The paper stream never sees the funded cache, key or read URL.
- EVM: a 4663 tape row prices a v1 position; a tape older than the bound holds.
- 56: the poller reads only through `drpc_paced` on the dRPC 56 URL, skips while the account is
  paused, stops for the process on a quota answer or a third limit in an hour, reads nothing with
  no open 56 v1 position, and an unbound position holds.
- The Solana paper stream refuses a funded key, the watcher's key and e1f95763.
