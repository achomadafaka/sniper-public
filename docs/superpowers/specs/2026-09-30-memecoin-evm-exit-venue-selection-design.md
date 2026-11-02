# EVM exit venue selection — design

Date: 2026-09-30. Builder: Beta. Order: Alpha. Review and measurement: Gamma.
**Status: PROPOSED, round 3.** Round 1 `00cc4326e` (Gamma 3 MEDIUM, 4 LOW) and round 2
`6205a5cca` (Gamma CLEAN for building the shadow; 3 LOW and one design answer) are folded in, with
Alpha's directions of 2026-09-30. **Built: the shadow's first slice (§5.6), which changes no
exit.** Selection changes only after its read and Alpha's ruling.

Goal (Alpha): an exit never sells through a venue that pays materially less than another the
ladder can already quote, and nothing slows the first sell.

## 1. The defect

The EVM quote ladder runs every rung at once. A BUY waits for the rungs and takes the best
measured round trip. A SELL takes the first rung that hits, in a fixed order:

`launchpad_curve, flap_curve, v3, v2, pons_v2, v4, v4_multihop, mixed_v3_v2, kyber, relay`
(`sniper/execution/evm_real_seams.py:1213`, selection at `:1295-1320`).

A coin's drained side pool on a higher rung always answers. The bag is then sold into it.

Evidence, chain-1 fork rehearsal (2026-09-30, production context, 24 coins, $20 clips;
`beta/eth-mainnet` `8147497a8`, `docs/audits/2026-09-30-eth-step7-fork-rehearsal.md`):

| coin | bought on | sold on | back of $20 |
|---|---|---|---|
| SNOWMOON | V4 pool (round trip -2.1%) | V3 pool holding ~0 WETH | $4.20 (-79%) |
| STOCKER | V4 pool (round trip -6.1%) | V2 pair holding 0.04 WETH | $17.49 (-12.6%) |
| the other 22 | | | $19.17 to $19.88 |

Live exposure, funded EVM positions opened 2026-09-16 to 09-30 by stored entry route
(`memecoin_positions.evm_route_json`), 144 positions:

| chain | entry route | positions | exit today |
|---|---|---|---|
| 4663 | `uniswap_v4` | 43 | priority ladder: v3, v2 and pons_v2 are asked before v4 |
| 4663 | `uniswap_v4_multihop` | 23 | the stored route first (already fixed, `:3471`) |
| 4663 | `relay_same_chain`, `kyberswap` | 25 | priority ladder: every direct rung is asked first |
| 4663 | `launchpad_curve`, none | 9 | priority ladder |
| 56 | `uniswap_v2` | 32 | priority ladder: v3 is asked before v2 |
| 56 | `pcs_mixed_v3_v2` | 5 | the stored route first (`:3451`) |
| 56 | `pancake_v3`, `flap_curve`, none | 5 | priority ladder |
| 8453 | `uniswap_v2`, `uniswap_v3` | 2 | priority ladder |

On 4663, 71 of 176 entry ladders in the same window had two or more rungs hit: a coin with
several venues is common there.

Gamma's ledger read (pre-registered 2026-09-30T15:35:59Z, funded EVM closes since 09-16, relayed
by Alpha; code confirmed on main `f923d6a85`):

- **4663:** the exit venue differs from the entry venue on 13 of 95 closes, 12 of them a rung
  EARLIER in the ladder. First exit quote against the entry's own measured round trip: same
  venue p50 +0.3 points; differing venue p50 -5.0 points, p10 -18.3.
- **Position 63808:** five quote pairs 2-6 s apart, `uniswap_v2` 12-18 points under relay every
  time. The ladder used V2 whenever it answered.
- **Position 64487 (DOGGO):** entered through relay at -3.1% round trip. The only exit quote,
  10 s later, was `uniswap_v4` at -84.0%. The catastrophe stop fired on it and sold there:
  -$71.78. Rug or decoy pool is unknown until Gamma's chain read.
- **56:** 39 of 40 closes on the entry venue. That says nothing about a better one: the ladder
  never asked.

So the defect has two halves. The venue that EXECUTES the sell, and the quote that DECIDES it:
the mark that drives the stop and the catastrophe rail comes from the same priority ladder.

## 2. Why the dust check did not fire

The sell ladder has a dust check (`:1322-1353`, 2026-09-11, SUIT). It asks the caller whether the
winner's output is plausible, and when it is not it waits up to `MEMECOIN_EVM_SELL_DUST_WAIT_S`
(3.5 s) for the other rungs and takes the largest output.

"Plausible" is `_sell_plausible_fn` (`:3408`): output value at least **1/20 of what the position
cost** (`entry_notional`).

- SNOWMOON's $4.20 is 21% of $20. The floor was $1.00. Plausible.
- STOCKER's $17.49 is 87%. Plausible.

The check catches a bag priced at -95% or worse. A venue that pays 20% of the bag's value passes.

## 3. Latency, measured

Source: rung records in `memecoin_entry_attempts.trace_json` (substage `evm.quote_initial`),
2026-09-16 to 09-30, read-only. Milliseconds, rungs that FINISHED.

| rung | 4663 n | p50 | p90 | cut | 56 n | p50 | p90 | cut | 8453 n | p50 | p90 | cut |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| v3 | 173 | 178 | 308 | 3 | 54 | 284 | 477 | 0 | 15 | 76 | 131 | 0 |
| v2 | 159 | 470 | 606 | 17 | 46 | 600 | 941 | 8 | 15 | 283 | 529 | 0 |
| pons_v2 | 174 | 244 | 670 | 2 | | | | | | | | |
| v4 | 169 | 393 | 2,216 | 7 | | | | | 13 | 135 | 898 | 2 |
| v4_multihop | 142 | 1,515 | 3,688 | 34 | | | | | | | | 15 |
| mixed_v3_v2 | | | | | 25 | 290 | 619 | 8 | | | | |
| kyber | 46 | 1,773 | 4,204 | 39 | 19 | 1,583 | 2,632 | 20 | 4 | 1,046 | 1,384 | 1 |
| relay | 65 | 2,722 | 3,855 | 39 | | | | | | | | |

How to read it (Gamma LOW 1):
- These are BUY ladders. A buy rung also runs its reverse leg, so a finished rung is an UPPER
  bound for the same rung on a sell.
- `cut` counts records the ladder cut at its grace or deadline. They are not in the percentiles.
  A cut rung was slower than the cut, so for a rung with cuts the p90 is a LOWER bound: kyber
  and relay on 4663 were cut about as often as they finished.
- The exit's own quote stage today (`memecoin_exit_execution_attempts.stage_ms_json`, per
  quote): 4663 p50 775, p90 2,563 (n 82); 56 p50 391, p90 432 (n 18).
- Not measured anywhere on the funded pool: one pinned V4 quote in the sell direction, and a
  simulated whole-bag sell. Gamma reports the pinned quote's p50/p90 off the funded pool with
  READ C, as a lower bound; the shadow measures both on it.

Round 1 carried an "added wait" table built from these ladders. It is withdrawn: it subtracted
the slowest higher rung instead of the moment the priority winner was known. The shadow measures
that gap directly.

## 4. Candidates

- **(d) The stored entry route FIRST, the ladder as the fallback.** What `:3451` and `:3471`
  already do for mixed and multihop routes, for every pinnable entry venue. One venue per
  position: one mark series, no comparison, no threshold.
- **(a) Entry venue beside the priority winner, take the better.** Catches a side pool that
  really pays more. Two venues per position: the mark can switch venue between ticks.
- **(b) All rungs, best output.** Waits for the slowest rung: on 4663 the slowest finished rung
  of a ladder is p50 2.0 s, p90 3.9 s, and cut rungs are slower. Slows every exit. Rejected.
- **(c) A floor on `last_executable_price x qty` that forces a second venue.** Circular: that
  price is written from the exit preview, which runs the same priority ladder. SUIT's previews
  read its bag at $0.97 from the dust pool. Rejected as the primary; a floor on COST stays as a
  backstop (5.5).

## 5. Proposal

**(d) is the primary design. (a) is the alternative the shadow grades against it** (Alpha,
Gamma MEDIUM 2). (d) reuses a pattern already in the code and has nothing to tune; it is
preferred unless the shadow shows it loses.

### 5.1 The entry venue

From the position's stored route (`memecoin_positions.evm_route_json`):

| stored route | entry venue |
|---|---|
| `uniswap_v4` with a `pool_id` | that pool, pinned |
| `uniswap_v2`, `uniswap_v3`, `pancake_v3` | that rung |
| `uniswap_v4_multihop`, `pcs_mixed_v3_v2` | unchanged: the stored route is asked first today |
| `relay_same_chain`, `kyberswap` | the same aggregator (shadow only, 5.6) |
| `launchpad_curve`, `flap_curve`, none | none: today's ladder |

### 5.2 What counts as an answer (both designs; Gamma MEDIUM 1)

**Only a SIMULATED whole-bag sell on the entry venue is an answer.** A quoter number moves no
tokens. It is blind to what the token does on transfer: a sell tax raised after entry, a
blocked seller, a transfer hook.

- The answer is the entry venue's real sell transaction for the whole bag, built at the usual
  slippage, run as one `eth_call` from our own address. The wallet holds the bag, so no balance
  override is needed; the allowance override is used only where the approval is not standing
  (the slot is cached after first sight, as token safety does).
- It succeeds or it does not. Success: the venue sells the bag for at least the quote's
  minimum. A revert: the venue cannot sell the bag now, whatever its quoter says.
- The rug shape this exists for: the token's sell tax is set to 99%. The V2 rung's measured cut
  reads -99%, which is true. A pinned V4 quote still reads the old price. With a bare quote the
  entry venue would overrule the truth and the sell would then revert on its minimum. With the
  simulation the V4 answer is a revert, so it is no answer.
- Cost: one quote and one simulation per tick for a pinned V4 pool; one simulation for a V2 or
  V3 entry venue, whose quote the ladder already takes. Measured in the shadow.

### 5.3 Design (d): the entry route first

1. The exit preview and the sell start the entry venue AND the ladder together.
2. When the entry venue answers (5.2), it is used. The ladder's result is dropped.
3. When it does not answer, the ladder's result is used exactly as today, with no extra wait:
   it has been running from the start.
4. The mark the rails read therefore comes from one venue while that venue can sell the bag.
5. **Dead-pool switch for the MARK** (Gamma, round 2). When the entry venue's simulated sell is
   under `F` x cost while the ladder's winner is over it, the position moves to the ladder's
   venue for good: a fresh anchor there, no flip back. Both under `F`: the larger, as the dust
   path does.
6. **The SELL has no floor.** When the sell is built it takes the better of the entry venue's
   answer and the ladder's result if both are already in hand, with no wait for either. It is
   never worse than (d) or (a), the mark series stays on one venue, and a dust pool that still
   "sells" loses to any real venue.

**A genuinely drained entry pool (a rug).** The simulated sell reverts, or returns an output
under the cost floor (5.5). Either way it is not used: the ladder's result stands and the exit
leaves through whatever venue the ladder finds, at once. A pool that was dumped but still
sells answers its low, true price: the rail fires on it and the sell goes there.

### 5.4 Design (a): better of two (the alternative)

1. The same two starts. The sell takes the priority winner unless the entry venue's answer pays
   more, and waits for the entry venue at most `W` after the winner is known.
2. The preview values the bag at the better of the two.
3. **A rail compares a value only with an anchor from the same venue** (Gamma MEDIUM 2).
   Position 63808's two venues sat 12 to 18 points apart on alternating ticks; a mark that
   falls back from one to the other reads that gap as a price move, and a trail anchored on the
   higher venue's peak fires on it. This is the 2026-09-27 anchor defect. So every executable
   mark stores its venue, `executable_peak_price` keeps the venue it was taken on, and a loss
   rail that would fire on a tick from another venue needs that venue's own confirmation.
4. "Pays more" is net of the approval: a venue with no standing allowance costs a transaction
   and a receipt before the sell (Gamma LOW 2).

**A genuinely drained entry pool.** The entry venue answers nothing or a low number; the
priority winner stands, the rail fires as today, and the sell does not wait past `W`.

### 5.5 The cost floor, a backstop for both

When no entry venue exists or it gave no answer, and the ladder winner's output is below `F` x
the position's cost, the existing dust path runs (wait up to `MEMECOIN_EVM_SELL_DUST_WAIT_S`
for the other rungs, take the largest). Today `F` is 1/20. Its value is set from the shadow.

### 5.6 The shadow (the only thing built first)

`sniper/services/memecoin_exit_venue_shadow.py`. **4663 first** (Alpha, Gamma).

**Where it runs: the capture process, not the runner.** The runner already stores every
exit-decision quote with its venue in `memecoin_route_series` (one per open position about every
1.2 s, with the winner's input, output and minimum). The shadow reads those rows through a
read-only ledger handle and asks the entry venue itself. Nothing on the exit path imports it, so:

1. **It adds ZERO wait to a sell and to a preview**, by construction: there is no code of it in
   the runner (a test pins that the runner's modules never name it).
2. **It never signs** (Alpha's hard condition, Gamma LOW 2). It is given the wallet's ADDRESS. A
   V4 sell's real calldata carries a Permit2 permit, which is a custody signature; the shadow's
   simulation has no permit and sets Permit2's `allowance[owner][coin][router]` with a state
   override on the `eth_call`. The storage layout was verified on mainnet state (3 of 3 Permit
   events: `eth_getStorageAt` equals the packed allowance).
   - Found while building this: today's PREVIEWS sign. `preview_exit_fn` runs the ladder, and its
     V4 sell rung builds the permit with `Account.sign_typed_data` on every tick
     (`evm_v4_quote.py:1777`), because the executor reuses the preview's transaction. Reported to
     Alpha; not changed here.
3. **Writes** go to the captures store (`memecoin_refusal_captures.db`, table
   `memecoin_exit_venue_shadow`), never the funded ledger. One immutable row per stored quote;
   the first write wins.
4. **Its own host and a ceiling** (Gamma LOW 3). Each chain is shadowed only when
   `MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_<chain>` names a host; empty by default. Every request
   goes to that URL as given, with no pool rotation.
   - **The host must serve historical state** (every read is pinned to a past block) **and must
     not be a funded host.** `build_worker` refuses a host whose NAME is the chain's custody RPC
     (`MEMECOIN_EVM_RPC_URL_<chain>`, or the generic one), a member of its funded pool, or of its
     archive pool: another key on the same host spends the same provider. The research QuickNode
     host is none of them on 4663 (checked 2026-09-30).
   - **Native-quote chains are refused** (Arc 5042): a native pool quotes in the native unit (18
     decimals) while the runner stores the quote token's (USDC, 6), so the two are not comparable
     until a slice records the scale. At most 30 `eth_call`s per minute per chain;
   the first HTTP 429 pauses the chain 60 s; a tick inside a pause or over the ceiling is a row
   that says so. The counts are in the capture process's heartbeat
   (`data/memecoin_refusal_capture.heartbeat`, key `exit_venue_shadow`).
   - A tick costs 10 to 11 calls: head, up to 5 block timestamps, two entry quotes, the coin's
     approval to Permit2, the simulation and the simulation at 99%. A position is sampled at
     most once per 20 s, a pause or a full ceiling included: a tick that is not read is one row
     per interval, not one per pass. So the ceiling allows 2 to 3 ticks a minute per chain,
     shared by its open positions. The ceiling is also enforced on every single call. The block
     time is measured over 10,000 blocks, once per 10 minutes (one more call).
   - **Spacing** (Alpha). Seen on the research QuickNode host: about 10 calls inside 1.5 s
     returned one HTTP 429; about 250 calls spaced 0.4 to 0.6 s returned none. The shadow never
     makes two calls inside 0.4 s (2.5 a second), inside a tick and across ticks. Its own thread
     waits, so a tick takes about 5 s; the reads are pinned to past blocks, so that costs nothing.
5. **What one row holds:** the runner's quote (venue, pool, route target, input, output,
   minimum, slippage, its time, the quote token's USD rate), the entry venue (protocol, pool,
   hooked or not), the head, the chain's block time, the two blocks read and the entry pool's
   quote at each, whether the coin's approval to Permit2 is standing, the two simulations
   (minimum, passed or reverted, the revert data), the calls spent, and an outcome.
6. **The simulated OUTPUT** (Gamma LOW 1). A V4 `eth_call` returns nothing, so the output itself
   cannot be read. The simulation asks for the EXECUTOR's minimum: the entry pool's quote less
   the slippage the runner stored with its own quote (1,000 bps today). When `sim_ok` is 1 the
   bag sells for at least that; when it is 0 the revert data says why. After a pass a second
   simulation asks for 99% of the quote (`sim99_ok`): a pass at the minimum and a revert at 99%
   is a loss between quote and proceeds, a transfer tax. Without a stored slippage nothing is
   simulated (`no_slippage`): the minimum is not guessed.
7. **Every read is pinned before the runner's quote** (Gamma MEDIUM, round 3). The shadow
   reaches a tick about a second after the runner stored it. On the tick that fires a rail the
   sell has landed by then: at "latest" the wallet has no bag (the simulation reverts) and the
   entry pool is the pool after our own sell.
   - **Which block the runner's quote read is not stored.** Measured on 13 stored quotes of
     three positions whose winner was the entry pool (the block where the pool's quote equals
     the stored output): 4 to 9 blocks, 0.4 to 0.9 s, before `quoted_at`, and not one constant.
     These pools move 10 to 45% inside a second on a stop tick. Evidence:
     `docs/superpowers/evidence/exit-shadow/2026-09-30-pin-calibration.md`.
   - **So a tick is read at two blocks that bracket it:** `early_block` (`quoted_at` - 1.0 s)
     and `pin_block` (`quoted_at` - 0.3 s). Equal quotes: the pool did not move across the
     window and the answer is exact. Different: the row is a bound, not an answer. The
     allowance and both simulations are read at `pin_block`.
   - **The chain's clock, not the host's.** Block timestamps are whole seconds. The first block
     of the quote's own second is found by a search on timestamps and checked against the
     blocks on both sides; a block's place inside the second is its count from there, at the
     chain's measured block time. A head behind the quote's second, or blocks that are not where
     the block time says, give a row with no pin and no read.
   - **The control is in the rows.** Where `winner_pool_id` equals `entry_pool_id` and the two
     quotes agree, the entry quote must reproduce `winner_output_atomic`. The share that does
     not is the instrument's own error, and it moves if the runner's RPC pool changes.
   - A node that does not hold the pinned state answers with an error that is not a revert.
     That is an `error:rpc_...` row, never "the pool reverted" or "the sell fails".
   - **The real fix is one field in the runner:** store the block its quote read
     (`context_slot` is already in the evidence, empty on EVM). Not built: it touches the
     preview path.

8. **The last quote of a closed position is always read.** The quote that matters most is the
   one a rail fired on. A position is sampled once per 20 s and the runner stores a quote about
   every 1.6 s, so sampling alone reads about one fire tick in twelve: 43 V4 single-hop
   positions closed on 4663 in 14 days, so about 4 fire ticks in 14 days.
   - Pinned reads do not expire. When a position is no longer open, its last stored quote is
     read ahead of the sampling. It waits for a pause to end and for room under the ceiling; it
     is never skipped, and its row replaces a row that said the tick was not read.
   - The worst case a tick is admitted on grows with its age (the timestamp search is wider):
     12 calls for a fresh tick, 23 for one 31 hours old.
   - Real state: position 66853's stop tick (series 161963), read 31 hours later in 20 calls:
     `second_block` 75553367, `early_block` 75553363 (9592621526602724), `pin_block` 75553370
     (6624563126979191, exactly the runner's stored output; same pool). The pool moved 45%
     inside the window, and the late pin is the state the runner read.
   - A last quote the HOST did not answer (a 5xx, a dropped or refused connection, a timeout) is
     read again, up to 3 attempts spaced 60 s and then 300 s apart (a host outage of minutes
     still gets its fire tick read), then kept as the error it is. A node answer (a revert, state it
     does not hold) is kept at once.
   - Not covered: the fire tick of a PARTIAL sell (the position stays open), and a position
     that closed before the capture process started.

**Instrument check** (real 4663 state, read-only, the research host): at the block before four
past funded V4 sells (positions 66853, 66678, 66668 on hooked pools, 63649 on a hookless pool)
the shadow's simulation of the whole bag passes, and the same call WITHOUT the Permit2 override
reverts (`AllowanceExpired`). On the hookless pool it also reverts at 100.5% of the quote
(`V4TooLittleReceived`), so the minimum is what is tested. The host serves state overrides and
block tags days back.

**Slices.** Built: pinned Uniswap V4 entry pools. Next, each to Gamma:
- V2 and V3 entry venues (56, 8453);
- the 60 s follow-up after a loss-side close the entry venue did not support (Gamma MEDIUM 3):
  the entry venue's simulated sell and the ladder winner for the SOLD quantity, off the sweep
  thread, immutable rows, the post-TP residual tail as the pattern;
- aggregator entry venues (relay, Kyber; DOGGO's shape): refreshed every 15 s per open
  position, at most 2 positions at a time per chain, 429s counted and the first one pausing the
  refresh 60 s. An aggregator answer has no block: it may count against a winner's quote only
  when it was FETCHED AFTER that quote (Gamma LOW 4).

`W`, `D`, `F` and the aggregator age are NOT set here. The shadow records raw gaps and timings;
the values come from Gamma's pre-registered rule on that read.

What neither design ever does: refuse an exit, widen slippage, re-quote after signing, pick a
venue that did not answer, or wait for a venue on the way out.

## 6. Edge cases (CLAUDE.md rule 5)

1. **Money path:** yes, exit venue and exit decision on 4663, 56, 8453 (and chain 1 when it
   lands). The shadow changes neither. The operator sees this spec before a selection change.
2. **Speed:** nothing may slow an exit. (b) is rejected for that. The shadow has no code on the
   exit path. In (d) the ladder runs from the start, so a silent entry venue costs no time. In
   (a) the sell waits at most `W`. Measured p50/p90 on the exit path are a condition of any
   selection change, from the shadow, never an estimate.
3. **Concurrency:** the shadow is one thread in another process; it shares nothing with the
   runner but a read-only ledger handle. A selection change would add one future on the
   ladder's pool per tick.
4. **Absent, zero, unknown:** no stored route: today's behaviour. A simulated sell that REVERTS
   is a fact (the venue cannot sell the bag). A read that FAILED is unknown: the ladder's result
   is used for that tick, and the tick is recorded as unknown for the entry venue, not as a
   low price. How often a failed read switches a position's venue under (d) is a shadow count.
5. **Dependencies:** the quote and the simulation are `eth_call`s on the chain's read pool. A
   timeout or a 429 is a miss for the entry venue only. The aggregator refresh has its own
   pause.
6. **Units:** both sides are whole-bag executable sells in atomic quote-token units. A V2
   fee-on-transfer quote is a cut amount (`_fot_exit`); comparisons use the whole-bag value
   (`_with_exit_value`).
7. **Sample:** the shadow's rows are our exits on our coins. They describe our book.
8. **Rollback:** the per-chain knob, default empty; remove the chain and restart.

## 7. Measurement (Gamma)

1. **Ledger read, done for 4663 and 56** (§1). Open: READ C, the venues at the block before
   each funded sell, DOGGO included.
2. **The shadow read**, pre-registered by Gamma when the row format exists. At n >= 30 exits on
   4663:
   - (d): how often the entry venue answered; the value gap against the priority winner; how
     often a failed read switched the venue.
   - (a): how often the entry venue paid more, net of the approval, and by how much; venue
     switches per position.
   - loss-side fires the entry venue did not support, and the 60 s follow-up after each.
   - p50/p90 of the pinned quote, of the simulation, and of the entry venue's time after the
     winner was known.
   - added `eth_call`s per open position per minute; aggregator calls and 429s.

## 8. Rulings so far (Alpha, 2026-09-30)

1. (d) primary, (a) the graded alternative.
2. Shadow on 4663 first, behind a per-chain knob that is empty by default.
3. Aggregator refresh in the shadow only, with its call count reported.
4. No threshold is set before the shadow's read.
