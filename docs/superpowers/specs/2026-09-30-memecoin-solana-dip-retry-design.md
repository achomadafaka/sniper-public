# Solana dip retry: fill a chase-refused buy when the exact quote comes back under the cap

Beta, 2026-09-30. **BUILT, live per wallet only (operator order, 2026-09-30).** Gamma's read
(8c3bbaba2): no window paid pooled across wallets; DegenCapitalLLC +22.4% [+7.0, +32.2] on n=8, every
dip within 15 s. The operator: "make the dip retry only for account like degen ... this is only for
the most botted accounts, not for everyone, make it live for degen". So:

- `MEMECOIN_REAL_DIP_RETRY_WALLETS` is an explicit list (source_user_id or handle, case-insensitive).
  Empty means nobody. This replaces "empty = every armed wallet" below.
- No chain knob: v1 is Solana only, in code.
- `MEMECOIN_REAL_DIP_RETRY_POLL_S` defaults to 1 s (Degen's dips are all within 15 s).
- A hit is logged to `memecoin_dip_retry_log` (fomo_event_id, watched premium, cap, verdict,
  position_id), which is how the dip fills are graded apart from first-try fills.
- Other wallets join only when the operator names them; the dip shadow (cd4f6f8bc) grades
  candidates forward.

The design below is as specced, with those changes.

## What already exists (read before building)

- `run_entry_retry_lane` (`sniper/services/memecoin_real_runner.py`) re-runs the WHOLE decision on
  `memecoin_entry_retry_queue` rows. Enqueue happens only for `_is_transient_entry_miss`. Live
  values: `MEMECOIN_ENTRY_RETRY_WINDOW_S=60`, `_INTERVAL_S=6`, and `_MAX_ATTEMPTS` at the code
  default of 12.
- **Slippage reverts already retry** (`status == "failed"` and "slippage" in the reason), except on
  the slot lane: `_retry_payload` does not serialise slot-lane events on purpose.
- **Chase-cap refusals already had a retry**, and it is OFF. The reason "exact quote is +N% above
  source price" re-queues only when `MEMECOIN_REAL_CHASE_RETRY_ENABLED`. The operator set it to 0
  on 2026-09-24 after STOOK 64575: it was refused at +47% and +38% against a 35% cap, filled 22 s
  later on the third attempt, and closed at -$101.57.
- **The slot lane's slippage failures already cascade into the chase cap.** In 19 of the 20
  slot-lane `BuySlippageBelow*` failures from 09-27 to 09-30, an `onchain_fallback_after_slot_lane`
  attempt on the same signal ran about 1 s later. It was mostly refused by the chase cap (+30% to
  +239% against 30%), and one filled.

So "retry after a slippage failure or a chase refusal" is, in practice, **retry a chase refusal**.
What is new is (1) a watch cheap and fast enough to catch a short dip, (2) a deadline counted
from the signal, (3) a source-sold stop, and (4) chain and wallet scope. No new gate.

## Design

1. **Enqueue.**
   - Scope: a Solana BUY refused by the chase-quote gate (the `chase_quote` capture family: "exact
     quote is ... above source price"), on a chain in scope, from a wallet in scope.
   - It goes on the existing queue as a dip row, marked by one new nullable column
     `dip_deadline_at`.
   - Deadline: `min(signal time + W, the lane's own age limit, MEMECOIN_REAL_ENTRY_MAX_AGE_S)`. The
     signal time is the one the max-age gate already reads from the event. An
     `onchain_fallback` row therefore ends at most 45 s after detection
     (`MEMECOIN_ONCHAIN_LANE_MAX_AGE_S`).
   - Enqueue runs after the refusal is recorded, on the path `_maybe_enqueue_retry` already uses,
     so the first attempt's latency is unchanged.
   - A slot-lane slippage failure needs no enqueue of its own: its fallback attempt is the one the
     chase cap refuses.
2. **Watch (new, off the loop).**
   - One daemon thread in the runner. For each live dip row it takes an exact BUY quote for the
     reserved clip every `MEMECOIN_REAL_DIP_RETRY_POLL_S`.
   - It computes the premium against the SAME chase reference the gate used: the source's
     post-trade price on Solana, with the average-fill fallback. It calls the gate's own function;
     it never copies it.
   - Premium within the cap: the row goes into an in-memory due set under a lock, and the thread
     wakes the loop.
   - The thread never reserves, signs or writes the ledger. It reads the queue through its own
     read-only connection.
3. **Hit.**
   - `run_entry_retry_lane` runs the full hook once for a due dip row. Every gate re-runs,
     including mcap, age from the original signal, safety, exposure and the chase cap on the
     funded quote.
   - Fill: dequeue. A chase refusal again: the row stays in the watch until its deadline. Any
     other refusal: dequeue.
   - Dip rows never run on the 6 s blind cadence. The watch is their only trigger.
4. **Exactly once.**
   - The queue's primary key is `fomo_event_id`.
   - One position per coin, and the reservation and unresolved-signed-transaction guards.
   - Dequeue on fill.
   - At most `MEMECOIN_REAL_DIP_RETRY_MAX_HITS` full-hook runs per row (default 3).
5. **Source sold: stop.**
   - Before every watch pass and every hit, a sell by the same `source_user_id` on the same mint
     after the signal dequeues the row: `memecoin_events` with `side` = sell, through the
     `(source_user_id, created_at)` index.
   - Verify at build that the J7 and on-chain lanes' sells land in that table. If they don't, read
     the lane's own record as well.
6. **Knobs, all read at call time, in `sniper/settings/memecoin.py`:**
   - `MEMECOIN_REAL_DIP_RETRY_S`: W. Default 0, which is off. At 0 no dip row is enqueued and live
     dip rows are dropped on the next pass.
   - `MEMECOIN_REAL_DIP_RETRY_CHAINS`: default `solana`. v1 accepts only solana.
   - `MEMECOIN_REAL_DIP_RETRY_WALLETS`: default empty, meaning every armed wallet on the chains in
     scope. A comma list of `source_user_id` restricts it.
   - `MEMECOIN_REAL_DIP_RETRY_POLL_S`: default 2.
   - `MEMECOIN_REAL_DIP_RETRY_MAX_HITS`: default 3.
   - `MEMECOIN_REAL_CHASE_RETRY_ENABLED` is untouched and independent. A dip row never takes the
     old 6 s path.

## Quote load and budget

- **Signals.** 111 Solana chase-quote refusals from 09-16 to 09-30, all lanes (about 8 a day).
  J7 and fallback copies of one signal are counted twice, so there are fewer distinct signals.
- **Watch quotes.** At most signals/day x (deadline / poll). With the on-chain lane's 45 s and a
  2 s poll, that is 8 x 22, about 180 a day. They go through the funded Jupiter seam
  (`_quote_with_fallback`), as Alpha specified.
- **Funded Jupiter today.** From `memecoin_jupiter_load` (role `funded`): 12,952 quotes on 09-27,
  10,796 on 09-28 and 12,978 on 09-29, with 0-2 `host_429` a day. The watch adds about 1.5%. The
  `slot_leg` role runs about 190k a day with 0 x 429.
- **Host cooldown risk.** A 429 or 401 on the funded seam cools that host for 60 s
  (`JUP_HOST_COOLDOWN_S`), and exits share it. So the watch:
  - skips any pass while a funded host is cooling;
  - stops entirely for 60 s after it sees a 429 or 401 itself;
  - makes at most one quote per poll interval across all rows, not per row.

  The watch is a funded decision input, not a display thread. It is named here because the
  display-thread rule exists for exactly this cost.
- **Hits.** Each hit is one full decision (one funded quote plus safety reads), at most 3 per
  signal.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It adds funded fills on signals the chase cap refused once. STOOK is the
   known bad outcome of this class. Gamma's pre-registered W read decides the build.
2. **Speed on the fill path.**
   - The first attempt is untouched: enqueue follows the refusal.
   - The watch runs on its own thread and never blocks `run_once` or any other signal.
   - A hit costs one decision on the loop, the same as any retry today.
   - Measure main-loop poll p50/p90 and `run_entry_retry_lane` pass p50/p90 for 24 h, before and
     after. Never estimate them.
3. **Concurrency and global state.**
   - The watch reads only: its own read-only connection, plus an in-memory due set behind a lock.
   - DB writes stay on the main thread.
   - It mutates no module attribute, env var or resolver.
   - It only READS the funded host-cooldown map (`_jup_host_cooldown`). Its own quotes can still
     set a cooldown; that is item 5.
4. **Absent versus zero versus unknown.**
   - A failed watch quote is unknown: no hit and no dequeue.
   - An unknown chase reference: no hit (the gate refuses it anyway).
   - A failed source-sold read is unknown: no hit this pass. It fails closed on the money side, and
     the deadline still ends the row.
5. **Dependencies.**
   - Jupiter 429 or 401: the watch pauses 60 s.
   - Timeout: skip the pass.
   - A wrong watch quote costs one full decision, which re-quotes on the funded path and refuses.
     It never produces a fill on its own.
   - A runner restart empties the due set. Dip rows persist and resume until their deadline.
6. **Units and identity.**
   - The premium is in percent, the same function and reference as the gate.
   - W is in seconds from the signal time the max-age gate reads, never from queue time. Queue time
     is host time, and `created_at` is not source chain time
     (finding-events-created-at-is-not-source-chain-time).
   - Identity is `(network_id, mint)` plus `fomo_event_id`.
7. **The sample.**
   - Dip fills are selected by our own trigger. Grade them against the capture counterfactual of
     the same refusals (`chase_quote` and `slippage_fail` series), never against first-try fills.
   - Keep `dip_retry` visible on the position so its rows can be separated.
8. **Rollback.**
   - Set `MEMECOIN_REAL_DIP_RETRY_S=0`. Live dip rows are dropped on the next pass and the watch
     idles.
   - A half-applied state (dip rows from before the rollback) cannot fill: the knob is read on
     every pass.
   - The column is nullable and harmless.

## Checks to write (red first)

1. An in-scope chase refusal enqueues a dip row whose deadline is counted from the signal. Out of
   scope (chain, wallet, or W=0) enqueues nothing.
2. The watch marks a row due only when the premium against the gate's own reference is within the
   cap.
3. A due row runs the full hook exactly once:
   - a fill dequeues it;
   - another chase refusal keeps it until its deadline;
   - any other refusal dequeues it;
   - MAX_HITS bounds it.
4. A source sell after the signal dequeues the row without running the hook.
5. A row past its deadline never runs the hook, including one queued late.
6. A watch quote that blocks forever does not delay `run_once` or another signal's decision.
7. The watch skips while a funded host is cooling and pauses 60 s after its own 429.
8. Two hits on one signal produce at most one fill.
9. W=0 drops live dip rows on the next pass.

## Open, for Gamma's read

- W, and whether dips pay after TP25/SL35/+300 s. Gamma is also running the sized variant: a dip
  counts only if a buy at least our clip's size filled under the limit. The watch quotes the
  reserved clip, so it is sized by construction.
- Whether pump-venue dips dominate. If they do, a phase 2 can read the pump curve and PumpSwap
  pool state directly (`pump_curve_buy` / `pumpswap_buy` math) and spend no Jupiter quotes.

## EVM extension (2026-09-30, operator order via Alpha): BUILT, default off (Gamma review 491be6812 folded in)

Scope: DegenCapitalLLC on 4663. Since 09-16 Degen has 111 attempts and 15 fills on 4663, with 7
refusals `route quote is Nx the source price (cap 1.50x)` (the latest is 346c152f, DOTSLNCH, today)
and 10 `our quote is +N% above source price (cap N%)`. There are 0 such refusals on 56 or 8453. No
graded EVM dip evidence exists yet, so the grade comes from the forward log.

### What an EVM dip row is

- **Family, v1: the execution premium, source basis only.** The reason is
  `route quote is <R>x the source price (cap <C>x)` (`evm_swap_executor._premium_refusal`), status
  `skipped_chase`.
  - Reference = the gate's own source price. The premium gate is called with
    `source_price=pending_source_price`, and the refusal carries
    `source_drift_pct = (quote_price / pending_source_price - 1) x 100` (`buy_drift`, same source). So
    `reference = quote_price / (1 + source_drift_pct / 100)` is that price exactly, with no rounding.
    On all 61 source-basis refusals in the ledger (every EVM chain, all time), `source_drift_pct`
    equals `(R - 1) x 100` to the printed 2 decimals. Degen has 20 of them on 4663.
  - Cap = the gate's own cap, parsed from `(cap <C>x)` and stored as `(C - 1) x 100` percent, so the
    watch keeps the Solana rule unchanged: `premium_pct <= cap_pct`.
  - **Capped again at the rerun decision's quote-over-source cap** (2026-09-30, Gamma on
    DegenCapitalLLC 4663, dip log 8/9). The rerun also refuses our quote at or above
    `quote_over_source_max_pct_for(chain)` over its chase reference (30% on 4663, chain-wide, not
    per band or wallet). A hit in (30%, 50%] fired at +38.6%, burned a decision and a funded quote,
    and was refused at +49%. The stored cap is now min(the premium cap, that cap), read from the
    same settings function. The chase reference is within 2% of the source price or above it (the
    post-trade ratio is accepted down to 0.98; 15 of 55 live 4663 ratios are under 1), so a hit in
    about (27.4%, 30%] can still meet that gate's refusal. Kept on purpose (Alpha, 2026-09-30): the
    band is a tenth of the old one, and a stricter watch would skip hits that fill at a ratio of 1
    or more. The market chase gate (chart vs reference) is not predicted: the
    watch sees no chart. Solana has no quote-over-source gate: its rerun applies the refusing gate
    with the same cap.
  - Clip = the reserved notional (`entry_attempt_notional`), as on Solana.
- **Not handled in v1, stated:**
  - `... the chart mid (cap Cx)`. Its reference is the live chart price, which moves; a stored
    reference would be stale, so it is not enqueued.
  - `our quote is +N% above source price (cap N%)` (quote-over-source, 10 on Degen 4663). Its
    reference is the chase reference (post-trade or average fill), which the refusal does not carry.
    The percent is printed rounded, and adding the field means an executor change. Phase 2, if the
    premium family pays.
  - `coin is +N% above source price (cap N%, basis market)`. It measures the chart, not our quote.
- **Knob** `MEMECOIN_REAL_DIP_RETRY_CHAINS` (canonical chain ids, comma list). Default `solana`,
  which leaves today's behaviour unchanged. EVM stays off until the operator adds `4663`. Both
  `maybe_enqueue` and `run_hits` read it at call time. A live row whose chain has left the knob is
  dropped on the next pass, the same way `W=0` drops rows.
- **Parsing.** Solana keeps its `(cap N%)` form, and the EVM `(cap C.CCx)` form is added. There is
  a test for each form, and a test that the chart-mid and quote-over-source forms enqueue nothing.

### The EVM watch: research host only, never the funded RPC

- **A second watch thread** (`memecoin-dip-watch-evm`), started only when the knob names an EVM
  chain. A 6 s route scan therefore never delays a Solana dip, whose dips are all within 15 s.
- **Quoter.** It owns one `memecoin_evm_exact_capture.ExactLane`, the same code the refusal capture
  uses.
  - Its hosts come from `usable_read_urls`, over `MEMECOIN_CAPTURE_EVM_READ_URL_<chain>`. The
    domain guard proves that no funded variable, read pool or free-submission endpoint shares the
    host. Rule 0724bc08: 4663 reads on the research key.
  - The capture module is imported lazily inside that thread's start. The existing pin, "importing
    the runner never loads the lane", still holds, and a new test pins the dip module's import too.
  - This does NOT give the capture module admission authority. The watch's number only nominates;
    the hit re-quotes on the funded path, and every gate decides again.
- **Price.**
  - `amount_in = clip_usd / eth_usd` in quote-token atoms, where eth_usd is the lane's own plain
    Binance GET, cached 60 s.
  - The lane's buy route gives `out` atoms. On-chain decimals come from one `decimals()` call,
    cached.
  - USD per token = `clip_usd / (out / 10^decimals)` = the pool's ETH price x the lane's ETH/USD.
    So a Binance error moves the watch premium 1:1 (Gamma LOW 1; the first draft said it cancels,
    which was wrong). Against a 50% cap that is small, and the hit re-decides on funded pricing,
    but a 60 s-cached rate can nominate or miss a dip at the edge.
- **Calls per quote.** Measured 2026-09-30 on the 4663 research host, mint `0x04013246…` (DOTSLNCH,
  $29.62 clip):
  - first quote (route scan: V4 stored keys, V3 deployments, V2): **26 eth_calls, 5.8 s**;
  - each later quote on the cached route (60 s TTL): **1 eth_call, 55-96 ms** (n=5);
  - `decimals()` once, 1 call;
  - Binance: 1 GET per 60 s while a row is live.

  At `POLL_S=1` over a 30 s window, one refusal costs about 26 + 29 + 1 = 56 research calls.
  Degen's 4663 rate is 7 refusals in 14 days, so this is below 1 a day.
- **Route hint (Gamma LOW 2).** The refusal's `quote_route_target` names the router. The 4663
  Universal Router means V4 (31 of 52 premium refusals); the configured SwapRouter02 means V3 (5).
  The row stores `route_kinds`, and the watch scans only that venue: a few calls instead of 26.
  Any other target scans every venue.
- **Limits.**
  - A 429, 403, limit code, timeout, transport error or 5xx holds that chain's host for 60 s
    (`yield_s=60`). While held, the lane returns nothing and nothing becomes due.
  - The refusal capture process reads the same research hosts on its own lane (4 rps). The watch's
    bucket is 2 rps but bursts one second of rate; an unhinted first scan measured ~4.5 rps
    (Gamma LOW 2), so the worst case is ~8.5 rps briefly on a host probed clean at 4. The route
    hint makes that rare. It is research-only: a hold costs capture gaps, never funded reads.
- **The funded host** is touched only by a hit's full decision: one fresh funded quote.
- 8453's research host is refused today by the domain guard (measured), so an 8453 row could
  never become due. The knob default leaves 8453 out.

### Shadow first, and a denominator (Gamma MEDIUM 1 and 2)

- **`MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS`** (default empty). A chain in it (and in
  `_CHAINS`) is watched exactly as live, but when a row comes due the retry lane logs
  `shadow_hit` and ends the row. No funded decision runs and no funded quote is spent. There is no
  EVM dip evidence yet (only 6 of Degen's 20 4663 source-basis refusals have any capture series,
  1 exact), so the recommended activation is 4663 in both knobs until a pre-registered read. Going
  live instead needs a written stop rule from the operator.
- **Every queued row ends in exactly one log row**, with the watch's view of it:
  `min_watch_premium_pct`, `watch_quotes` and `watch_unknown` (a held host, no route or unknown
  decimals). The terminal statuses are `expired`, `knob_off`, `source_sold`, `onchain_skip`,
  `shadow_hit` and `error`, or the hit's own verdict. A hit that keeps the row logs its verdict,
  and the row's end logs again. `watch_unknown` x `POLL_S` bounds the seconds the watch was blind.
  After a restart the stats are NULL (in memory only), never zero.
- **The 4663 wallet** read $4.56 at 06:5xZ, under the $5 min-trade floor (Gamma). While that
  holds, a live 4663 hit spends one funded quote and refuses at the floor; the shadow spends
  none.

### Hit and stops (unchanged path, now network-aware)

- A due EVM row runs `hook.attempt` once, on the main thread. That is the whole EVM decision: route,
  execution premium and chase on a fresh funded quote, safety, balance, one-per-coin,
  approve-at-entry and nonce floor. Age is counted from the original signal
  (`ev.created_at`, which the max-age gate reads).
- Stops are the same three: 3 hits, the deadline, or a source sell. `source_sold_since` reads J7
  (`memecoin_events`) and the Solana watcher (`onchain_wallet_sells`). For EVM it now also reads the
  EVM watcher's `onchain_evm_sell_shadow` (`detected_at`, host clock, for want of a block time).
  A sell detected after the signal stops the row. Degen's 4663 sells arrive on J7 (70 since 09-16).
- `run_hits` passes the row's own `network_id` everywhere; the literal "solana" goes.
- **Logging.** `memecoin_dip_retry_log` gains `network_id` (nullable; NULL on old rows = solana), so
  Gamma grades EVM apart from Solana.

### Rule 5 (EVM delta)

1. **Money path: yes.** It adds funded 4663 fills on signals the execution premium refused once.
   The operator activates it per chain via the knob.
2. **Fill path.**
   - The first attempt is untouched: enqueue follows the refusal.
   - The dip pass inside `run_entry_retry_lane` with no dip rows is measured on the live ledger
     (read-only, n=2000): before **p50 0.0033 ms, p90 0.0034 ms, p99 0.0055 ms**, after the build
     **p50 0.0027 ms, p90 0.0028 ms, p99 0.0046 ms**. Both are the query plus an early return; the
     second watch's `take_due` runs only when dip rows exist.
   - The EVM watch is its own thread and never blocks `run_once`.
3. **Concurrency.**
   - Each watch has its own read-only connection and its own `ExactLane`. No module global is
     mutated.
   - Due sets are locked per watch; `run_hits` unions them on the main thread.
4. **Absent / zero / unknown.**
   - no_route, a revert, a held host, an unreadable decimals, or no ETH/USD is unknown: never a hit,
     never a dequeue.
   - A missing `source_drift_pct` or `quote_price` on the refusal, or an unparsed cap, means no row
     is queued (logged).
5. **Dependencies.** Covered in Limits above. A wrong watch quote costs one funded decision, which
   refuses on its own quote.
6. **Units and identity.**
   - EVM mints are lowercase in `memecoin_events` (3,230 of 3,230 on 4663). The watch lowercases.
   - The cap is stored in percent; `(C - 1) x 100` is exact for the printed 2 decimals.
   - Identity is `(network_id, mint)` plus `fomo_event_id`.
7. **Sample.** Dip fills are chosen by our trigger. Grade them against the exact refusal capture of
   the same refusals, never against first-try fills, and per `network_id`.
8. **Rollback.** Remove the chain from `MEMECOIN_REAL_DIP_RETRY_CHAINS`. Its rows drop on the next
   pass and the EVM watch idles. The new column is nullable and harmless.

### Checks (red first)

1. A 4663 `route quote is 1.60x the source price (cap 1.50x)` refusal enqueues a row with
   `reference_price = quote_price / 1.60…` and `cap_pct = 50.0`, deadline from the signal. The same
   refusal with 4663 not in the knob enqueues nothing. The Solana `(cap 30%)` form still enqueues.
2. Chart-mid, quote-over-source and market-basis refusals enqueue nothing.
3. The EVM watch marks a row due only when `clip / out` is within the cap of the reference. A held
   host, no_route or unknown decimals never marks it.
4. The EVM watch never runs a Solana row, and the reverse.
5. A 4663 source sell in `onchain_evm_sell_shadow` after the signal stops the row, and one before
   it does not.
6. A row whose chain left the knob is dropped without a decision.
7. A hit logs `network_id`.
8. Importing `memecoin_dip_retry` does not load `memecoin_evm_exact_capture`.

## Rebuild: the watch prices exactly as if we held the position (operator order 86d053ce, 2026-09-30)

**Order (hard):**
- No DexScreener or display feed, ever, for the dip retry or any fill question.
- A refused signal is watched exactly as if we held it: executable exact-route quotes on the route
  kind the funded entry used, at open-position cadence.
- The order fires the moment the executable price at our clip is inside the cap.

This replaces the ExactLane watch of the EVM extension above. What the live rows showed: the first
two 4663 dip rows expired blind (watch_quotes == watch_unknown), because ExactLane cannot price
pons_v2, Kyber, launchpads, flap or V4 multihop. That is ~31% of 4663 premium refusals (Gamma).

### Price source, per chain: the funded read-only quote functions

- **EVM:** the chain context's `venue_quote_with_miss_fn(quote_token, mint, amount_atomic)`.
  - This is `_venue_quote_combined`, the executor's own full ladder: pons_v2, V4, V4 multihop, Kyber
    where enabled, launchpad and flap curves, V3 and V2. It returns the quote plus a typed miss, and
    writes nothing.
  - NOT `entry_quote_probe_fn`: that one records a route-miss row per miss, which would spam the
    table once a tick.
  - The ladder is already used read-only off the execution lock (the early quote, token safety).
    Its trace is thread-local (`_preview_trace`).
  - Price = `clip_usd / (buyAmount / 10^token_decimals)`, with `amount_atomic` sized from the funded
    quote-token USD read. That is the same unit the premium gate compares (`quote_price` in USD
    per token).
- **Solana:** the funded Jupiter entry quote (`jupiter_entry_quote`), as today, with the host
  cooling checks it already has.
- There is no "unpriceable" skip. A route kind that cannot be quoted is a bug, fixed in the ladder.

### Cadence: the first price has no gap

- **Tick 0 is the refusal's own executable quote** (`quote_price` at refusal). The watch
  therefore has a price at t=0, not after a scan.
- The watch is not woken at enqueue. It reads the queue every floor period (0.25 s on EVM), so
  its first own quote starts at most one floor after the refusal. Tick 0 covers that gap.
- It then quotes back-to-back, one quote in flight per chain, as fast as the rung answers, with
  a floor (`MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S` on EVM, `MEMECOIN_REAL_DIP_RETRY_POLL_S` on
  Solana). Measured funded quote stage since 09-23:
  - 4663: p50 2.05 s, p90 4.21 s (n=83);
  - 56: p50 0.62 s, p90 4.23 s (n=46);
  - Solana: p50 116 ms, p90 519 ms (n=337).
  So a full-ladder cycle on 4663 is about 2 s. After tick 0 the EVM watch does not run the full
  ladder: it re-quotes the rung that priced the refusal. See "Winner-rung ticks" below.
- **One watch thread per chain**, so a 2 s 4663 quote never delays a Solana tick.

### Winner-rung ticks (Alpha, 2026-09-30)

Alpha: 2 s ticks on 4663 are too slow for a sub-second dip. After tick 0 the EVM watch re-quotes
only the route that priced the refusal: the same rung, pool and path. It falls back to one
full-ladder tick after two consecutive winner misses.

**Measured, not estimated.** Each winning rung's own duration comes from `evm.quote_initial.rungs[]`
in `memecoin_entry_attempts.trace_json`, entry attempts completed since 2026-09-23. A winner
re-quote costs one run of that rung (`only_rung`), so this is the tick cost.

| chain | winning rung | n | p50 ms | p90 ms |
|---|---|---|---|---|
| 4663 | v4 | 34 | 273 | 646 |
| 4663 | kyber (routes + build + round trip) | 30 | 2043 | 4958 |
| 4663 | relay | 37 | 2839 | 3920 |
| 4663 | v4_multihop (pool discovery + path quotes + round trip) | 20 | 2970 | 3775 |
| 4663 | pons_v2 | 8 | 1888 | 3478 |
| 4663 | launchpad_curve | 5 | 2956 | 4282 |
| 56 | v2 | 26 | 585 | 941 |
| 56 | mixed_v3_v2 | 6 | 479 | 598 |

Stage timings inside those rungs (the HTTP tap, recent attempts only, small n):
- v4_multihop `path_quotes` (the executable quote of known paths): p50 170, p90 198 ms (n=6).
  The rest of the rung is pool discovery through `_ds_pools_ranked` / `_gt_pools_ranked`, which
  are DexScreener and GeckoTerminal pool lists.
- kyber `routes`: p50 380, p90 483 ms (n=6); `build`: p50 379, p90 405 ms (n=6).

4663 premium and chase refusals since 09-23 by route target (n=18): Universal Router (v4 or
v4_multihop) 6, Kyber 4, Relay 3, GMGN router 3, other 2.

**So `only_rung` alone meets the 500 ms target only on v4.** v1 therefore uses the rung's own
executable price call, without the transaction build and the round-trip exit quote, on the two
rungs where that is one call:

- **v4_multihop:** `evm_v4_multihop._quote_path` on the refusal's `route.legs_path` (the exact
  pools). One quoter `eth_call`, measured p50 170 ms. This also removes the per-tick
  DexScreener/GeckoTerminal pool-list reads, which order 86d053ce forbids in the watch.
- **kyber:** `evm_kyber_quote._routes(...)["amountOut"]`, the call the rung itself prices on.
  Measured p50 380 ms. Build and verification run in the full decision after a hit.
- **v4 (single hop):** `evm_v4_quote.pinned_buy_out` on the refusal's own `route.pool_id`, with
  the key from the cache. One quoter `eth_call`. NOT `only_rung="v4"` (Gamma M1 on b2e2d2fb6):
  that rung rediscovers pools through `_pools_for_mint`, which reads GeckoTerminal and
  DexScreener pool lists (10-minute cache; a failed lookup is not cached and repeats every tick).
- **every other on-chain rung (v3, v2, pons_v2, the curves, mixed):** the ladder with
  `only_rung=<winner>`, the same code path as the executor's winner refresh (`_requote`).
  - **Correction (Gamma MEDIUM on 4a4a4773c): my grep was wrong.** On a chain in
    `MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS` or `MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS` (both default
    56) these rungs DO read a DexScreener pool list: the v2 rung through
    `evm_v2_exit_quote.pair_quote_tokens`, the v3 path after a full miss, `mixed_v3_v2`, and the
    ladder's `probe_v3_v2` after a full buy miss.
  - So the dip retry does not run on such a chain at all: `memecoin_dip_retry.chains()` leaves it
    out (no enqueue, no watch thread, a live row ends `knob_off`) and logs that once.
  - Runtime proof on the REAL ladder, no network: on 4663 and 8453 the watch's ladder tick and
    `only_rung` for v2, v3, pons_v2, launchpad_curve and mixed_v3_v2 make no display-provider or
    raw HTTP read. The control runs the same call on 56 and the DexScreener read happens.
- **aggregators (kyber, relay), Gamma M2:** an aggregator quote carries no block and can be stale.
  On HOOKERS Kyber read 1.82x while the pool stood at 1.46x, and Kyber never misses, so the
  ladder tick would never have run. Each aggregator tick is therefore ALSO priced on the V4
  pools of the mint whose key we already hold (`pinned_buy_out` over the durable key table, which
  `9023f80d8` warms off the entry path), and the better output counts. The first pool that
  answers is pinned for the row. A failed held-pool quote never costs the aggregator's tick.

Expected 4663 tick p50 under 500 ms on v4, v4_multihop and kyber refusals (10 of 18 above).
Relay (p50 2.8 s) stays slow.

**Correction (2026-09-30, same day): the pons_v2 and launchpad_curve rows in the table are a
regime mix, not a current wait.** Split by day, every slow hit is from 09-23:

| rung | 09-23 | since 09-26 |
|---|---|---|
| pons_v2 | n=4, 1888-4979 ms | n=4, 79-140 ms |
| launchpad_curve | n=4, 1444-4282 ms | n=1, 177 ms (6 serial `eth_call`s of 25-34 ms) |
| v4_multihop (whole rung) | n=4, 3287-3807 ms | 09-28: n=6, p50 658, p90 875 ms |

- pons_v2: `3ef5b1776` (09-23 03:09, "read the Pons getters in parallel") landed before the fast
  rows. The slow rows completed at 06:42-07:05 on 09-23; whether that commit was deployed by
  then was not checked.
- launchpad_curve: the per-call tap did not exist before `7f4cbf028` (09-24), so the 09-23 rows
  carry no calls and their cause cannot be read from the traces. n=1 since.
- So `only_rung` reaches the target on pons_v2 and the curve as well. Kyber (09-26..09-30: n=11,
  1618-2192 ms for the whole rung) and relay remain the slow rungs.

**What the refusal carries.** The premium refusal result gets one plain dict, `dip_route`:
`{rung, sell_token, sell_atomic, buy_atomic, legs_path}`. It holds no callable, so nothing that
serializes the result can break, and it is written into `dip_json`, so it survives a restart.
- A row with no `dip_route` (queued before this build) has no input and output to compare: it is
  unknown on every tick and ends `expired`. No quote is spent on it.
- A refusal whose quote did not name its rung, and a multihop refusal with no `legs_path`, are
  watched on the ladder tick (without the multihop rung) every time.

**Units: token outputs for the same input, so there is no ETH/USD read per tick.** Every tick
quotes the refusal's own `sell_atomic`. Premium at tick t is
`(1 + refused_premium) * buy_atomic_0 / buy_atomic_t - 1`, against the same gate reference. A
quote-token move inside the 30 s window is ignored by the trigger. The full decision re-prices
with live ETH/USD.

**Fallback.** Two consecutive winner misses (None, typed miss or raise) lead to one full-ladder
tick through `venue_quote_with_miss_fn`. That tick adopts its own winner for the next ticks.
**Alpha's ruling (2026-09-30): the ladder tick never runs the v4 or the v4_multihop rung**
(`exclude_rungs=("v4", "v4_multihop")`), because their discovery reads DexScreener/GeckoTerminal
pool lists. The watch never reads those. V4 is priced on pinned pools and paths instead, and a
ladder tick also asks the held V4 pools of the mint. The funded entry's own use of both rungs is
unchanged.

**Cadence and load.**
- The EVM floor is a new knob, `MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S`, default **1.0 s** (Gamma
  M3; first built at 0.25 s). A hit is a full decision (4663 ladder p50 1,857 ms, n=77) and the
  dips that were fillable lasted 2.6-15 s, so a faster tick only adds load on the funded RPC pool
  and the Kyber budget, which exits share. Lower it only on measured exit-quote p50/p90. Solana
  keeps `MEMECOIN_REAL_DIP_RETRY_POLL_S`.
- Worst case on one 30 s row at 1.0 s: about 30 ticks. A tick costs 1 quoter `eth_call` (v4,
  multihop), 1 Kyber `routes` call plus 1 `eth_call` (kyber), or the rung's own calls
  (pons_v2: 8).
- A typed `provider_rate_limit` or a Kyber rate-limit raise pauses that chain's watch for 60 s.
- The measure-and-stop rule above applies unchanged: exit quote p90 +20% on a chain turns that
  chain off.

**Next step, named and not built: event-driven pricing.** The pool's Swap logs arrive over the
watcher's WebSocket, and the executable price is computed locally from pool state. That is
ms-level, runs on the research host, and costs no quote per tick. It covers V4/V2/V3 pools
(Kyber routes through them too); it does not cover relay.

### Hit: immediate, full decision, one per source tx

- On a tick inside the cap, the row is marked due and the **dip hit lane** is woken at once. That
  lane is one runner thread with its own connection (`_dip_hit_worker` in
  `scripts/memecoin_runner_loop.py`), and it runs the WHOLE decision, with every gate re-checking
  on a fresh funded quote.
  - Why not the main loop: its measured period is 5.00 s (runner log, `period=5.00s`), so a hit
    there waited up to 5 s.
  - Why not the on-chain lane: a hit is a full funded decision (seconds on 4663) and would hold
    that lane's detections for as long. Nothing may slow a fill.
  - The hook is the one the main loop and the on-chain lane already call concurrently.
  - No two passes overlap (`memecoin_dip_retry.HITS_LOCK`, taken without blocking), because two
    would end and log one row twice.
  - The lane cannot fail silently: its loop catches everything, and if it stops passing for
    `LANE_STALE_S` (15 s) the main loop runs the hits again (5 s late, never lost).
  - A 1 s poll covers expiries and a lost wake.
  - A lane HUNG inside a decision holds the lock, so no thread can host. Two signals cover it:
    the runner heartbeat field `dip_lane` (`expected`, `last_pass_age_s`, `alive`), and one
    `DIP-LANE-STALE` page when the lane has not passed for `LANE_PAGE_S` (120 s) while dip rows
    are queued, repeated every 10 minutes. 120 s, not 15 s: a slow decision is not a hang.
- Dedupe by (network, source tx) across lanes is already built (`6525f1e8d`).

### Evidence: executable only, per tick

- The terminal log row carries `watch_series_json`: `[{t, premium_pct, rung, buy_atomic, miss}]`
  per tick, at most 120 ticks, kept in memory by the watch and written by the hit lane with the
  terminal row. `t` is seconds since the signal. Tick 0 is the refusal's own quote.
- The price is executable; a miss is typed (`provider_rpc_transport`, `no_route`, ...), and a miss
  is never a price.

### Rule 5

1. **Money path: yes.** The watch now spends the FUNDED quote budget and triggers funded decisions.
2. **Fill path and exits.** This is the risk that matters.
   - Bound: one quote in flight per chain; at most `MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED` rows
     watched per chain (default 2; the oldest past its deadline drops first); each row lasts
     <= `MEMECOIN_REAL_DIP_RETRY_S` (30 s).
   - Worst case on 4663: ~15 ladder quotes per row.
   - Pause: the Solana watch skips while any funded Jupiter host cools and pauses 60 s after its
     own 429. An EVM watch pauses 60 s when its quote reports a typed rate limit
     (`provider_rate_limit`, `provider_rpc_rate_limit`). A pool quarantine is not read by the
     watch; the stop rule below is the guard for it.
   - **Measured before and after, 24 h, never estimated:**
     - funded exact-EXIT quote p50/p90 per chain (the exit rail's `memecoin_route_series`
       exit_decision seam);
     - funded entry quote_ms p50/p90;
     - the RPC-pool 429 count per chain;
     - Jupiter funded-role load (`memecoin_jupiter_load`; Gamma has the baseline).
   - Stop rule: if exit quote p90 worsens by > 20% on a chain, the watch for that chain is turned
     off by knob.
3. **Concurrency.**
   - The ladder closures hold thread-local traces.
   - The watch threads never reserve, sign or write the ledger.
   - Due sets are locked per watch, and the main thread unions them (as built).
   - Series live in memory until the main thread writes the terminal row.
4. **Absent / zero / unknown.** A miss is unknown: never a hit, never a dequeue. A row with only
   misses ends `expired` with its series showing every miss.
5. **Dependencies.**
   - 429, timeout or quarantine: pause, as above.
   - A wrong quote costs one full decision, which re-quotes and re-gates.
   - A restart empties the in-memory series; the row's terminal log then has NULL stats, never 0.
6. **Units.** USD per token from our clip and `buyAmount`, sized with the same funded quote-token
   USD read as the gate. The premium is against the gate's exact reference (EVM:
   `quote_price / (1 + source_drift_pct/100)`).
7. **Sample.**
   - Dip hits are chosen by our trigger.
   - Grading uses the per-tick executable series of the SAME refusals, never a display feed.
   - Rows before this rebuild (ExactLane or display-graded) are excluded from any grade.
8. **Rollback.** Drop the chain from `MEMECOIN_REAL_DIP_RETRY_CHAINS`, or empty the wallet list.
   The watch idles and live rows drop on the next pass.

### Checks (red first)

1. The EVM watch prices through the context's `venue_quote_with_miss_fn`, never ExactLane.
   pons_v2-, Kyber- and V4-multihop-shaped quotes all produce a price.
2. Tick 0 is the refusal's quote and opens the series. It is never a hit: the gate refused that
   quote, so it is over the cap by construction.
3. One quote in flight per chain, and at most MAX_WATCHED rows. A 429 or quarantine pauses 60 s.
4. A hit wakes the dip hit lane, and the full decision runs once, on one host thread.
5. The terminal row carries the per-tick series; a miss is typed and never a price.
6. Other chains' threads are never blocked by one chain's slow quote.
7. After tick 0 an EVM row is re-quoted on its winner only: `_quote_path` on a multihop
   `legs_path`, Kyber `_routes` on a kyber winner, else `only_rung`. The full ladder is never
   called while the winner answers.
8. Two consecutive winner misses lead to exactly one ladder tick, which adopts its winner.
9. The premium is computed from token outputs at the refusal's `sell_atomic`, with no quote-token
   USD read on the watch thread.
10. `dip_route` is JSON-serializable. A row without it costs no quote and ends `expired`.
11. The ladder tick never asks the v4 or the v4_multihop rung. A test on the real ladder
    fails if `_pools_for_mint` or a provider pool list is read; its control reads it.
13. A v4 winner is quoted on its pinned pool; an aggregator winner is also quoted on the held V4
    pools, and the better output counts.
14. The heartbeat carries `dip_lane`; a stale lane with rows queued pages once per cooldown.
15. No dip row is queued and no watch thread starts on a chain in the pair-discovery or mixed-hop
    probe knobs. The real ladder reads no display feed on 4663 and 8453; on 56 it does (control).
16. A pinned quoter READ that fails (transport, timeout, rate limit) is a typed miss
    (`provider_<type>`) and pauses the chain's watch for 60 s at once. A pool that answers "no
    quote" is not blind. Funded callers of the same quoter keep their silent None.

**Follow-ups (task 22fee5c3, branch `beta/dip-followups`, after the merge of c618a17ac):**
- **Pause breadth (Gamma LOW 2 on c618a17ac, Alpha's ruling).** One failed pinned read of any
  type paused the chain for 60 s, longer than the 30 s window, so every live row expired
  unwatched. Now only a rate limit pauses; any other failed read is a typed miss and the next
  tick asks again. This replaces check 16's "pauses at once" for transport and timeout.
- **A rate limit is now knowable.** The read layer raised one generic error for a 429 and a 5xx.
  `evm_wallet._http_failure(status)` puts the status on that error (`http_status`); the message
  is unchanged. `pinned_blind` names `rpc_rate_limit` from it.
- **Which held pools (Gamma LOW 1 on 4a4a4773c).** With more than 8 stored pools for a mint,
  `pinned_buy_out` quotes the 8 deepest on our own tape (`_own_depths`, a local read), known
  depths before unknown. With 8 or fewer there is no depth read.
- **How often (Gamma LOW 3).** A held-pool scan that priced nothing waits `HELD_RESCAN_S` (5 s)
  before it runs again, instead of costing up to 8 `eth_call`s and a ledger open on every tick.
  A pinned pool is still asked every tick.
12. The hits run on the dip hit lane, never on the on-chain lane thread. The main loop takes
    them back only while the lane is not passing, and two passes never overlap.
