# Desk execution and seat plumbing — design (PROPOSED)

Status: PROPOSED, design only. No code, no `.env`, no money actions.
Revised 2026-10-21 for the operator ruling relayed by Alpha (brain decision 61859b83,
actor:operator). On the desk lane **the model is the trader**:
- It picks the coin.
- It decides ENTER now, WAIT for a code-checkable 1 s-bar condition with an expiry, or SKIP.
- It sets tp/sl/trail inside operator bounds. Code checks the bounds and rejects a value outside
  them; it never clamps.
- It re-reads at event boundaries and may TIGHTEN or EXIT.
- The call sits before the desk fill; 1–3 s is accepted and measured.
- Emergency exits (LP pull, catastrophe floor) stay deterministic, and the copy fill path is
  untouched.
- Max open and the daily halt are operator-tuned knobs with no prescribed value.
Author: Beta, 2026-10-21. Order: operator via Alpha, 2026-10-21 (brain decision a10fde26 plus that
day's order), "fully automated, LLM-controlled trading system", Beta's slice = execution and seat
plumbing.
Out of scope here: seat prompts, which signals the desk trades, and the strategy thesis. Those
belong to the other crew slices.

## 0. Summary

1. **There is no new fill engine.** A desk ENTER enters through the queue the operator's manual
   buys already use (`memecoin_manual_intents`, drained by the runner into `make_real_buy_fn` and
   then `real_entry_hook`). That path already reaches every funded chain, builds the frozen funded
   contract inside the runner, runs token safety, the impact cap, the round trip and bankroll
   checks, and ends every request in a terminal state a person can read.
2. **The same-slot copy lanes cannot serve the desk.** The slot lane, the BSC, Robinhood and Base
   fast lanes are all source-triggered: each fires on a source transaction S and races for S+1.
   A desk ticket has no S. The desk therefore never touches the copy fill path (rule 2).
3. **A desk entry needs its own origin, `signal_source='desk'`.** It must not reuse `'manual'`,
   because manual is exempt from more than the selection gates. It also skips the concurrency cap
   (`memecoin_real_leg.py:283`, `memecoin_evm_leg.py:810`), quiet hours, trials, and the copy
   analytics partitions.
4. **Hyperliquid has a real order adapter, but its runtime is parked.**
   `sniper/hyperlive/hl_exchange.py` + `real_exec.py` place IOC orders with explicit slippage and
   cloid recovery. The adapter is isolated-1x only, and every HL cron has been `PAUSED-HL` since
   2026-08-12. A leveraged lane needs three new pieces: a leverage setter with a cap, a resting
   reduce-only stop held on the exchange, and a desk cloid domain.
5. **The 1-second candle premise needs correcting.**
   - `memecoin_candles.py:52` (`to_candles`) buckets `memecoin_position_marks.sweep_ts`, so it has
     data only for coins we already hold. It does not read `pool_ticks`.
   - The main tick DB's `pool_ticks` holds 110 rows, all chain 8453 from 2026-10-19; the 4663 tape
     ended on 2026-09-04.
   - The batch-clock problem is real on the Solana tape (`data/memecoin_sol_tape.db`): one
     `observed_at` second holds 2,580 rows spread over 511 slots.
   - Proposal: bucket on `block_no` (the slot), convert to time only through `block_times`, and
     extend coverage to desk candidates (§4).

## 1. Engine inventory — what can fill a non-copy desk entry today

Measured queue latency for manual intents, read-only, `sniper_tracker.db`,
n=445 claimed intents between 2026-08-27 and 2026-10-20:

| Stage | p50 | p90 |
|---|---|---|
| `requested_at` → `claimed_at` (waiting for the main loop pass) | 3.54 s | 11.93 s |
| `claimed_at` → `resolved_at` (entry hook + submit + confirm) | 1.71 s | 3.38 s |

Outcomes: 315 filled, 114 refused, 16 unresolved (3.6%).

### 1.1 Generic funded entry (the desk path) — all funded chains

- **Entry API:**
  - Producer side: `memecoin_manual_intents.enqueue(conn, mint=, network_id=, size_usd= | size_pct_equity=, rails=, rails_mode=, ttl_s=)`.
  - The runner drains with `drain(conn, make_real_buy_fn(contexts), now_fn=, max_per_pass=2)`, from
    `scripts/memecoin_runner_loop.py:3246`, after exit work on every main-loop pass.
  - `buy_fn` builds `_ManualEv` (`memecoin_real_runner.py:9973`, `signal_source="manual"`) and calls
    `real_entry_hook`.
  - Solana fills through the Solana executor. EVM chains go through `evm_entry_fn=_make_evm_entry_fn(contexts)`.
- **Chains reached:** every chain with a funded context: solana, 4663, 56, 8453, 1, 5042. Their
  concurrency knobs are present in `.env`.
- **Exit menu, without new code:**
  - Default: the runner's funded policy by band, frozen into the contract at entry (TP, de-risk,
    trail, stop, catastrophe, timecap).
  - Typed rails per intent: `rails` ⊂ `RAIL_FIELDS = (tp_pct, tp_target_mcap, sl_pct,
    sl_target_mcap, trail_pct, derisk_pct)` with `rails_mode='custom'`, armed by `apply_rails`
    before the intent resolves.
  - A later sell: `memecoin_manual_exit_intents.enqueue(position_id, action ∈ {close, partial,
    close_any_price}, fraction)`, drained by `ManualExitWorker`, which runs on its own thread.
  - There is no mirror sell, because a desk entry has no source to mirror.
- **Clip limits today:**
  - Manual clip: `MEMECOIN_MANUAL_TRADE_SIZE_USD` (code default 20; not set in `.env`).
  - Override cap: `MEMECOIN_MANUAL_OVERRIDE_MAX_USD` (175).
  - `size_pct_equity` ∈ (0, 1].
  - `reduce_clip_for_route_capacity` can lower a 4663 clip.
- **Book-wide risk:**
  - `MEMECOIN_DAILY_LOSS_LIMIT_USD` (live 250) latches through `memecoin_daily_loss_trips`. It
    counts every real realized PnL, so desk losses count against copy.
  - Concurrency is global plus per chain (`MEMECOIN_MAX_CONCURRENT_TOTAL`, `_BY_CHAIN`). Manual
    entries skip it today.

### 1.2 Solana slot lane (`memecoin_slot_lane.py`) — not usable for desk

- **Entry trigger:** owner-signed shred/processed frames of an armed wallet's buy (RabbitStream,
  Shyft). The lane decides and sends against the source slot S, with a measured 452 ms
  decide→fill.
- **Why it cannot serve the desk:** every gate keys on the source transaction (chase reference,
  source_not_landed exit, first-buy, S/S+1 attribution).
- **Reusable piece:** the exotic warm legs (`raydium_launchlab_buy.WarmExoticLegs`) and the
  preparation cache are keyed by quote identity. A desk that knows its coin in advance could
  pre-warm a leg, but that is a later optimization and not part of v1.

### 1.3 EVM fast lanes (`memecoin_bsc_fast_lane.py`, `memecoin_rh_fast_lane.py`, `memecoin_base_fast_lane.py`) — not usable for desk

- **Entry trigger:** a source swap in block B or the mempool, raced for the S+1 front with a gas
  bid (the 56 bid order) and native float.
- **Why it cannot serve the desk:** their admission depends on source attribution: the deposit
  proof, the chase verdict on source price, the mcap refusal binding.
- **Desk EVM entries** take the generic `evm_entry_fn` path through §1.1. That path is slower
  (receipt-first on Base, the pipelined approval on 4663) and is still correct.

### 1.4 Hyperliquid (`sniper/hyperlive/`) — usable after a re-arm

- **Entry API:** `HlExchange.market_open(coin, is_buy, sz, cloid_hex)` is an IOC limit order with
  explicit `entry_slippage`, because the SDK's 5% default is forbidden.
  `real_exec.entry_decision(...)` gates on `enabled`, `breaker_latched`,
  `HL_REAL_MAX_CONCURRENT`, `HL_REAL_MAX_TOTAL_NOTIONAL`, chase bps against the paper twin, and
  `HL_MIN_ORDER_USD=10`.
- **Exit menu:** `market_close(coin, cloid)` (reduce-only IOC, whole position). No resting
  TP/SL order exists on the exchange; exits come from our polling loop.
- **Leverage:** `set_isolated_1x` only.
- **Identity:** `derive_cloid(deployment_id, copy_id, leg)` is bound to a paper copy row.
- **Runtime:** `run_hl_real.py` is not running. Every HL cron line is `# PAUSED-HL 2026-08-12`.
  The alt-short evidence is `PAUSED` according to the 2026-07-29 HL desk spec.
- **What a desk HL lane needs** (build later, its own spec):
  1. `derive_cloid(deployment_id, desk_ticket_id, leg)`, a separate id domain, so that a paper
     `copy_id` and a ticket id can never collide.
  2. `set_isolated_leverage(coin, n)` with `HL_DESK_MAX_LEVERAGE`. It must refuse rather than
     clamp when the seat asks for more.
  3. A **resting reduce-only stop trigger order placed at entry**. Our polling loop dies when the
     host dies (host uptime 89% in mid-September). A leveraged position without a stop held on the
     exchange can be liquidated during a host outage.
  4. A liquidation-distance check at entry: refuse when the stop is beyond the liquidation price.

### 1.5 What a `desk` origin needs to be attributed and kill-knobbed

**Attribution.** `memecoin_positions.signal_source` and `memecoin_entry_attempts.signal_source`
already exist, so a desk row is partitioned with no schema change to them.
`memecoin_entry_attempts.origin_event_id` carries `desk:<ticket_id>`.
**`memecoin_positions.strategy` stays `MEMECOIN_REAL_POLICY`** (correction, build step 1):
`real_exit_sweep` selects open real positions by that strategy (`memecoin_real_runner.py:8439`),
so a desk-only strategy name would leave a desk position with no exits at all. Desk rows are
identified by `signal_source='desk'`, and duplicates are stopped by the R3 coin guard, not by the
open-unique index.

**Sites to change.** Every `== "manual"` site splits into one of three behaviours:

| Behaviour | Sites (from grep, 2026-10-21) | Desk rule |
|---|---|---|
| Selection gates skipped (the operator, or the seat, is the decision) | `real_entry_hook` `_manual_entry` branches (~20: chase, first-seen chase, first-buy window, revival/volume, dead-coin, sell-share, coin age, source-buy floor, re-entry); `memecoin_early_quote.py:106`; `memecoin_source_initiation.py:252`; `memecoin_market_state.py:527` | **skip, same as manual.** Change `is_manual_entry` into `bypasses_selection(ev)`, which is true for `{'manual', 'desk'}`. |
| Capital and capacity gates | `memecoin_real_leg.py:221,283`, `memecoin_evm_leg.py:691,810` (quiet hours, concurrency) | **enforce for desk.** These sites compare `== "manual"` literally, so desk is enforced with no edit. Pin that with a test. |
| Trial and wallet machinery | `_trial_for_entry`, `memecoin_wallet_caps.py:36`, demotion, `memecoin_follow_sync` | **not applicable.** The desk has no wallet. `source_user_id` is NULL, so the build must confirm that the per-coin one-position guard refuses a duplicate when `source_user_id` is NULL (the open-unique index does not, because SQLite treats NULLs as distinct). |
| Analytics partitions | `db_memecoin.py:149` (`manual`/`copy`/`unknown`), `memecoin_profile_fit.py:81`, `memecoin_entry_shadow.py`, `memecoin_entry_classifier.py:76`, `memecoin_addon_label.py:109`, `memecoin_telegram.py:1017`, `memecoin_funded_policy.py:456` | **new `desk` bucket.** It must never fold into copy, because that would contaminate copy's reads. It must never fold into manual either, because the operator's manual partition already went -$217.90 across 237 closes. |
| Override / token-safety-warning | `enqueue(override=)`; the `token_safety_warning` path | **forbidden for desk.** The producer refuses `override=True`, and token safety stays a refusal. |

**Kill knobs and limits.** These are new settings in `sniper/settings/memecoin.py`. The operator
sets the values; the defaults below follow the order: knob ON, small clip.

| Knob | Meaning | Proposed default |
|---|---|---|
| `MEMECOIN_DESK_ENABLED` | Master switch. When off, the producer writes nothing and the runner refuses queued desk intents with reason `desk_disabled`. | **1** (in `.env`) |
| `MEMECOIN_DESK_CHAINS` | Chain allowlist for desk intents. | **`solana`** (in `.env`) |
| `MEMECOIN_DESK_CLIP_USD` | Fixed clip. A seat may scale it down, never up. | **25** (in `.env`) |
| `MEMECOIN_DESK_MAX_CONCURRENT` | Open desk positions, counted on top of the global caps (never in place of them). Operator-tuned. | **5** (in `.env`) |
| `MEMECOIN_DESK_DAILY_LOSS_USD` | Desk-only latch on desk realized PnL, a new row kind in `memecoin_daily_loss_trips` keyed `desk:<day>`. The book-wide latch (live 250) still counts desk losses too, so a full desk halt (200) leaves copy 50 USD of book headroom that day. Operator-tuned. | **200** (in `.env`) |
| `MEMECOIN_DESK_RAIL_BOUNDS` | Allowed range per rail (`tp_pct`, `sl_pct`, `trail_pct`, `derisk_pct`), plus which rails are **required**. `sl_pct` must be required: see §2.3. | operator |
| `MEMECOIN_DESK_WAIT_MAX_S` | Longest WAIT expiry a seat may ask for. A longer expiry is rejected. | operator |
| `MEMECOIN_DESK_BOUNDARY_MAX_S` | Longest gap between two boundary reads of an open position, and also the fastest timer-driven re-read allowed. | operator |
| `MEMECOIN_DESK_ENTER_TTL_S` | TTL of a desk ENTER intent. Kept short, so a fill can never land long after the decision it came from. | operator |
| `MEMECOIN_DESK_SEAT_COOLDOWN_S` | Minimum gap between two seat calls on the same `(network_id, mint, seat)`. | 900 |
| `MEMECOIN_DESK_HINT_SCAN_PER_MIN` | Cap on producer H's confirmed wallet scans; sequential, off the entry and stream paths. | operator |
| `MEMECOIN_DESK_SEAT_CALLS_PER_HOUR` | Model-call budget; once spent, logged SKIP `budget`. | operator |

**Live `.env` values, read 2026-10-21.** These are not yet read by any code, because the settings do
not exist yet. Two mismatches go to the operator:
- **The book differs from Gamma's map.** Gamma's pre-registered record F (`docs/audits/2026-10-21-desk-opportunity-map.md` §8) books "max 2 open, 50 USD halt". `.env` has 5 and 200. The read must name which one it ran under.
- **The capture knob alone does nothing.** `MEMECOIN_OPPORTUNITY_CAPTURE=1` is set, but the opportunity worker crons are `STOPPED 2026-08-21` and nothing runs. That capture is also a WebSocket `logsSubscribe` runtime (`sniper/settings/memecoin_opportunity.py`), not the gRPC universe of §2.0.

## 2. `desk_tickets` and the seat contract

### 2.0 Candidate producers (operator ruling 2026-10-21 ~10:3xZ)

**Slice 1** is Solana coins at 30–100k USD entry mcap. Two producers write candidates, and every
candidate opens a ticket at `hard_filter`. **Neither producer is a trigger.** A candidate is a coin
for the funnel to judge, never an instruction to buy.

**H — hint-scan (unarmed roster wallets).**
- **Input:** J7 hint rows for roster wallets that are not armed (`memecoin_events`, `hint_only`
  since 2026-10-12). Gamma measured about 50 in-band coins/day (range 13–87).
- **A hint is not evidence.** J7 relays carry `swap:<uuid>`, which cannot identify a transaction
  (finding 2026-10-02: J7 is not a wallet signal source). A hint only **wakes a bounded confirmed
  scan of that wallet** through the watcher's funded read client, decoder and rate-limit handling,
  as in `docs/superpowers/plans/2026-10-18-j7-native-fallback.md`.
- A candidate exists only once the scan finds the wallet's **confirmed** buy: signer, payment and
  acquisition receipt, canonical signature, and slot.
- **Scope change.** The 2026-10-18 contract limits scans to the **verified armed** registry.
  Scanning unarmed roster wallets widens that scope. It needs its own cap
  (`MEMECOIN_DESK_HINT_SCAN_PER_MIN`, scans sequential and off the entry and stream paths) and must
  never write an armed-lane entry attempt.
- **Fact sheet:** the confirmed roster buy goes in as `inputs.roster_buys`: wallet, signature,
  slot, USD, and age in slots at decision.
- **Timing:** the desk is always after the roster wallet here. That is the cost this producer
  carries, and the reason producer U exists.

**U — universe (Pump.fun gRPC).**
- **Purpose:** the desk can enter **before** roster wallets do.
- **This producer does not exist today.**
  - The pump tape (`data/pump_snipe_tape.sqlite`) has been dormant since 2026-10-12 and stored
    0 trades.
  - The opportunity capture is WebSocket-based and stopped.
  - `memecoin_sol_tape.db` covers only our own positions.
- **What it must do:**
  - Run a gRPC subscription on the Pump.fun program (curve buys and sells plus migration).
  - Keep a per-mint curve state.
  - Emit a candidate when the **curve-reserve mcap** (not a provider mid) enters 30–100k.
  - Emit again only after it leaves and re-enters, under the seat cooldown.
- **Edges:**
  1. Separate connection and credential from the funded slot-lane and RabbitStream streams, so a
     universe flood can never back up a funded frame. Which provider plan carries it needs an
     operator yes and is not decided here.
  2. Volume is about 40k launches/day (Gamma, 09-28..10-11). How many cross into the band is
     **unmeasured**, so the hard filter runs in code before any seat call, and the seat-call budget
     is a knob (`MEMECOIN_DESK_SEAT_CALLS_PER_HOUR`). Once the budget is spent, the result is a
     logged SKIP `budget`, never a silent drop.
  3. A stream gap, reconnect or decode failure leaves the curve state **unknown**, not stale. The
     candidate carries `null` and the seat gets `inputs.missing`. This reuses the gap pattern of
     §4.7.
  4. Graduation moves the coin to PumpSwap. The candidate re-binds to the canonical pool before any
     candle or quote is read from it, as the paper vault stream already does.
- **Fact sheet:** curve mcap, slot, buys and sells over the last N slots, creator, plus
  `roster_buys` (usually empty at this point).

**Fact-sheet activity fields (Gamma's activity study, frozen as d0090988b; built in step 3).**
- `swaps_since_source_5s`: the count of swaps in the source's pool with slot time in (0, 5] s
  after the source fill, excluding the source's own swap and ours.
  - It is **null until 5 s have elapsed**, never an early 0.
  - It is also null when any slot in the window lacks a time (`DATA_INSUFFICIENT`, §4) or the
    stream had a gap.
- `swaps_since_source_2s` (same rule over 2 s) and `r_since_source` (the return since the source
  fill) sit beside it.
- **`no_activity_after_fill` is a CODE gate, not a model hint** (Lambda/Alpha correction,
  2026-10-21). `swaps_since_source_5s ≤ 1` is measured, pre-registered and deterministic, so it is
  a hard-filter stage.
  - **In parallel:** at the signal, the JUDGE call and the 5 s activity window both start.
  - **At t = 5 s** code evaluates the gate: ≤ 1 → SKIP `no_activity_after_fill`; ≥ 2 → pass;
    **null → no gate**.
  - **The fill goes out only when both hold:** model ENTER and the gate not SKIP. The decision is
    ready at max(5 s, model wall time) instead of 5 s + model.
    - A model ENTER before 5 s waits for the gate.
    - A gate pass with no model answer by the JUDGE deadline → SKIP `judge_timeout`.
  - **The ticket records:** `gate_evaluated_at`, the model's `response_at`, `gate_state`
    (`'pass'`/`'skip'`, with SQL NULL for ungated) and `gate_inputs_json` (the count and its
    anchor) on the `hard_filter` row (Gamma's views, cohort-read plumbing §5).
  - **The read splits on `gate_state`.** Null means the coin was never gated (venue off the
    whole-pool streams, a gap, or a slot with no time). Pooling null with pass would mix gated and
    ungated entries.
  - **Anchor:** the window is relative to the **source fill**, which only cohort H has (the roster
    buy). Cohort-U rows stay NULL: Gamma's cohort-U study (b96b32f3f) validated no U anchor.
  - The 2 s variant (0 swaps in (0, 2] s) is a knob, `MEMECOIN_DESK_GATE_2S`, default off.
- **Source of the counts: whole-pool streams only** (Gamma's study counted every pool swap).
  - Venue in {`pump_curve`, `pumpswap`}: counted from producer U's stream. Its filter is
    program-wide (`account_include` = the Pump.fun program, plus PumpSwap when that option is on),
    so every swap in those pools is already there, H coins included, with no watch-set slot.
    - If PumpSwap is not in U's filter, PumpSwap coins fall into the next case.
  - Any other venue (Raydium, Meteora DBC/DAMM, LaunchLab): the pool is subscribed at candidate
    time and takes a slot in `MEMECOIN_DESK_WATCH_MAX_POOLS` (§7). With no free slot, the fields
    are null.
  - **Never from the slot-lane stream**: it carries only armed/watched owners' transactions, so its
    count would read as a flat tape.
  - **Null never reads as flat.**

**Shared rules.**
- Both producers dedupe on `(network_id, mint)` into one open ticket.
- A coin already held by the copy runner is marked `inputs.copy_open=true`. The read states whether
  the desk may also hold it. Gamma §7 calls this out.
- **A roster wallet's confirmed buy after our entry is a boundary event** (§2.3). It is never an
  entry trigger, and it is never an exit trigger by itself.

### 2.1 Where the table lives

The table lives in its own file, `data/memecoin_desk.db`, not in the main ledger. Seats write
often and slowly (model latency). The funded runner owns `sniper_tracker.db`, and the WAL notes in
CLAUDE.md forbid giving long-lived observers writable ledger handles. Only one row ever crosses
into the ledger: the ENTER intent, plus TIGHTEN and EXIT requests drained by the runner (§2.4).

<!-- schema:external=desk_tickets -->
```sql
CREATE TABLE IF NOT EXISTS desk_tickets (
    id INTEGER PRIMARY KEY,
    ticket_id TEXT NOT NULL,             -- one ticket = one coin episode through the funnel
    producer TEXT NOT NULL CHECK (producer IN ('H','U')),  -- who OPENED the ticket; never pooled
    network_id TEXT NOT NULL,            -- identity is (network_id, mint), never mint alone
    mint TEXT NOT NULL,                  -- EVM lowercased; Solana base58 as-is
    pool TEXT,                           -- the pool the inputs were read from; NULL = unresolved
    stage TEXT NOT NULL,                 -- hard_filter|chief|crawler|vet|quant|size|fill|risk
    seat TEXT NOT NULL,                  -- 'code:<check>' for code kills, else the seat name
    verdict TEXT NOT NULL,               -- PASS|SKIP|KILL|ENTER|WAIT|HOLD|TIGHTEN|EXIT (closed set, 2.3)
    reason TEXT NOT NULL,                -- machine slug first, free text after ':'
    inputs_hash TEXT NOT NULL,           -- sha256 of the canonical inputs JSON (sorted keys)
    inputs_json TEXT NOT NULL,           -- exactly what the seat saw
    output_json TEXT,                    -- the raw model answer; NULL when the call failed
    model TEXT,                          -- NULL for code stages
    prompt_version TEXT,                 -- NULL for code stages
    latency_ms REAL,                     -- model or code wall time
    gate_state TEXT CHECK (gate_state IN ('pass','skip')),  -- hard_filter row only; NULL =
                                         -- ungated (U rows, non-whole-pool venue, gap, untimed slot)
    gate_inputs_json TEXT,               -- swaps_since_source_5s + its anchor (source fill sig/slot)
    response_at TEXT,                    -- model rows: the instant the answer was received (the
                                         -- shadow clock starts here); never ts minus latency_ms
    cost_usd REAL,                       -- NULL = unknown, never 0
    intent_id TEXT,                      -- set on the ENTER row once enqueued
    ts TEXT NOT NULL,                    -- UTC ISO, real clock (datetime.now(timezone.utc))
    UNIQUE (ticket_id, stage),           -- a stage judges a ticket once
    UNIQUE (network_id, mint, seat, inputs_hash)  -- identical inputs are never re-asked
);
CREATE INDEX IF NOT EXISTS idx_desk_tickets_coin ON desk_tickets(network_id, mint, ts);
CREATE INDEX IF NOT EXISTS idx_desk_tickets_verdict ON desk_tickets(verdict, ts);
```

**Cohort-read plumbing (Gamma, 2026-10-21, draft
`docs/superpowers/specs/2026-10-21-desk-cohort-read-plumbing.md`, 910930ca0).** It adds the two
columns above and two tables in the same desk DB:
- `desk_shadow_fills`: the SJ and SR records, insert-only.
  - Written by a shadow worker, off the fill path.
  - Uses exact-route quotes only.
  - `status` ∈ open | closed | censored | skipped_full.
  - SR keeps its own 5-slot book, and its draw is `sha256('desk:<ticket_id>')`.
- `desk_eligible_snapshots`: the eligible set per producer at `response_at`, so an SR draw can be
  re-derived.

**Dedupe and the producer column.** Dedupe across producers (§2.0) keeps **one open ticket per
coin**, and `producer` records which producer opened it. A coin first seen by U and later bought by
a roster wallet (H) stays a U ticket, and the roster buy is a fact-sheet field. The F record joins
through `origin_event_id='desk:<ticket_id>'`, the fill-stage `position_id`, and
`signal_source='desk'`. It does not join on strategy (see §1.5).

### 2.2 Seat input (typed JSON in)

```json
{
  "schema": "desk-seat-in/v1",
  "ticket_id": "…", "stage": "vet", "seat": "vet",
  "coin": {"network_id": "solana", "mint": "…", "pool": "…"},
  "asof": {"slot": 459269069, "block_ts": "2026-10-21T08:00:00Z", "read_at": "…"},
  "inputs": {"liquidity_usd": 41234.5, "holders_top10_pct": null, "candles_1s": [ … ]},
  "prior": [{"stage": "chief", "verdict": "PASS", "reason": "…"}]
}
```

- Every field the producer could not measure is `null` and is **listed in `inputs.missing`**. It is
  never 0 and never omitted, so that a seat reasoning about "holders 0" cannot be fed a failed read.
- `asof` pins the data snapshot. A seat never fetches anything itself.

### 2.3 Seat output (typed JSON out)

There are two decision seats. Earlier funnel seats (chief, crawler, vet) answer only `PASS` or
`SKIP`.

**Entry seat** (the trader). Its answer is one of ENTER, WAIT or SKIP:

```json
{"schema": "desk-entry-out/v1", "action": "WAIT", "reason": "slug: text",
 "wait": {"condition": {"bar": "1s", "field": "close", "op": "<=", "value": 0.000041,
                        "for_bars": 2},
          "expires_s": 45},
 "clip_scale": 0.5,
 "rails": {"tp_pct": 60, "sl_pct": 25, "trail_pct": 15, "derisk_pct": null}}
```

- `ENTER` means enqueue now, with these rails.
- `WAIT` means code arms a watcher on the coin's 1 s bars (§4). It enqueues only if `condition`
  holds before `expires_s`. The model is **not** asked again when the condition fires: the rails
  and clip are frozen at the decision.
- `SKIP` means done.
- **The condition grammar is closed**, so code can evaluate it with no model in the loop:
  - `field` ∈ {open, high, low, close, n_swaps, buy_usd, sell_usd};
  - `op` ∈ {<=, >=};
  - a numeric `value`;
  - `for_bars` ≥ 1.
  - Anything else is rejected. A bar that is `gap` or `DATA_INSUFFICIENT` never satisfies a
    condition: it counts as not met, and the WAIT simply expires.
- **Every condition field is range-checked, never defaulted** (Lambda's blind run produced
  `['mc','<=',0,0,0]`):
  - `field` must be in the enum;
  - `op` must be in the enum;
  - `value` must be finite and inside that field's operator bound (`MEMECOIN_DESK_WAIT_FIELD_BOUNDS`,
    for example price fields > 0, `n_swaps` ≥ 1, USD fields > 0);
  - `for_bars`, a window and `expires_s` must be positive integers, with `expires_s` ≤
    `MEMECOIN_DESK_WAIT_MAX_S`.
  - Any failure records SKIP `cond_out_of_range:<field>`. A zero, a missing value or a wrong type
    is never filled in.

**Boundary seat** (the same trader, re-reading). Its answer is one of HOLD, TIGHTEN or EXIT:

```json
{"schema": "desk-boundary-out/v1", "action": "TIGHTEN", "reason": "slug: text",
 "rails": {"sl_pct": 12, "trail_pct": 8}}
```

- **Boundaries** are: fill confirmed; TP or de-risk hit; **a confirmed roster-wallet buy of the coin
  after our entry** (from producer H's scan, a hint alone does not count); the mark crossing half the distance to a
  rail; `MEMECOIN_DESK_BOUNDARY_MAX_S` since the last read; and a WAIT expiring on an open
  position. A boundary seat never runs on a timer faster than that knob.
- **TIGHTEN may only tighten.** For each rail named, the new value must be no looser than the
  armed one: `sl_pct` and `trail_pct` lower, `tp_pct` lower.
  - A rail that is armed **must not be dropped**.
  - Turning a rail off (0) counts as loosening.
  - A TIGHTEN that loosens anything is rejected whole, never partially applied.
- `EXIT` means code enqueues `memecoin_manual_exit_intents(action='close')`.
- `HOLD` means nothing changes.
- The boundary seat cannot add size.

**Validation (code, after the model).** Any failure records SKIP for an entry, or HOLD for a
boundary, with the failure as its reason:

| Model answer | Recorded |
|---|---|
| Literal `null`, empty answer, unparseable JSON, a schema mismatch, an action outside the seat's set | `seat_null` / `seat_malformed` |
| Timeout, HTTP error, rate limit, refusal | `seat_call_failed:<kind>`. No retry inside the ticket. |
| `clip_scale` outside (0, 1]; a rail outside `MEMECOIN_DESK_RAIL_BOUNDS`; a required rail missing; `expires_s` > `MEMECOIN_DESK_WAIT_MAX_S`; a WAIT condition outside the grammar | `out_of_bounds:<field>`. **Rejected, never clamped**: a clamped value is a decision the model did not make. |
| TIGHTEN that loosens or drops a rail | `tighten_loosens:<rail>` |

- **`sl_pct` must be a required rail.** The carrier is `rails_mode='custom'`, and `apply_rails`
  (`memecoin_manual_intents.py:291`) writes **0.0, meaning OFF, for every percentage rail not
  named**. A seat that names only a take-profit would therefore open a position with no stop. The
  manual record shows the cost: custom rails went 3 of 11 (module docstring).
- **The catastrophe floor is not in `RAIL_FIELDS`.** It comes from the frozen contract
  (`_effective_catastrophe_pct`, `memecoin_real_runner.py:457`), so a seat cannot switch it off.
  LP-pull handling and the catastrophe floor stay deterministic. The build must pin, with a test,
  that a desk override cannot zero the catastrophe floor.
- **A boundary-seat failure means HOLD.** The armed rails, which the model already chose, keep
  running. A failure never triggers an EXIT and never loosens anything.

**"Never re-run until ENTER"** is enforced by the schema, not by convention:
- `UNIQUE(ticket_id, stage)`: a stage cannot be re-rolled on the same ticket.
- `UNIQUE(network_id, mint, seat, inputs_hash)`: re-asking with byte-identical inputs is
  impossible.
- `MEMECOIN_DESK_SEAT_COOLDOWN_S` per `(network_id, mint, seat)`: blocks re-asking after a trivial
  input change. Without it, a one-tick change to `inputs` would make a fresh hash and allow
  shopping for an ENTER.
- A WAIT that expires is final for that ticket. A new ticket on the same coin is subject to the
  cooldown.

### 2.4 Hand-off to the engine

1. **ENTER (or a WAIT whose condition fired).** The code `size` stage computes
   `size_usd = MEMECOIN_DESK_CLIP_USD × clip_scale`. It checks desk concurrency and the desk latch
   from a **read-only** ledger connection, and records its verdict.
2. **On PASS**, the producer calls `memecoin_manual_intents.enqueue(... signal_source='desk',
   origin_event_id='desk:<ticket_id>', rails=<seat rails>, rails_mode='custom',
   ttl_s=MEMECOIN_DESK_ENTER_TTL_S)`.
   - This needs two additive columns on `memecoin_manual_intents`: `signal_source` (default
     `'manual'`) and `desk_ticket_id`.
   - `drain` passes `signal_source` into `buy_fn`, which builds the event with it.
   - Rails are percentages from the **fill** price, as funded rails are today. The seat sees that
     fact in its input, so it never sets a level against a price it assumed.
3. **`apply_rails` needs a source argument.** Today it writes `source='operator'`,
   `actor='operator'`, and operator rows "carry full authority (may loosen)"
   (`db_memecoin.py:3389`). A desk row must be written as `source='desk'`,
   `actor='desk:<ticket_id>'`. Without that change, a desk decision is recorded as the operator's,
   and the tighten-only rule has nothing to bind to.
4. **TIGHTEN** calls `set_position_exit_override(conn, position_id, 'desk', ...)` from the runner
   side, carried by a new `rails` intent kind or a desk-only table drained by the same worker. It
   never writes from the desk process into the ledger directly. The tighten-only comparison runs
   against the **currently active** desk row, inside the same transaction as the write. This
   prevents a race in which two boundary reads both pass against a stale row.
5. **EXIT** enqueues `memecoin_manual_exit_intents(position_id, action='close')`.
6. The `fill` stage row records the intent's terminal state (`filled|refused|expired|unresolved`)
   and `position_id`. **`unresolved` counts as open desk exposure** until the reconcile settles it.

### 2.5 Codex review items (verified in code by Alpha, 2026-10-21)

**R1 — The desk policy is frozen before submission, and a failure to arm it is a refusal.**
- Clip, rails and WAIT expiry are written into the intent row (`size_usd`, `RAIL_FIELDS`,
  `rails_mode='custom'`, plus `desk_ticket_id`) **before** `enqueue` returns.
- The runner must carry that same policy into the reservation, the funded contract, the fill and
  the recovery/reconcile path. That way a fill recovered after a crash arms exactly the rails the
  seat chose, never the runner's default.
- Today `drain` calls `apply_rails` **after** the fill (`memecoin_manual_intents.py:361`). On
  failure it logs the error and still resolves `filled` with `RAILS NOT ARMED`. That is acceptable
  for the operator, who sees it. **For desk it is not.**
- **The desk rule:** rails are part of the frozen contract at reservation time. They are not an
  override written afterwards.
- **If the build keeps the after-fill path**, a failure to arm must:
  - resolve the intent `refused` with reason `desk_rails_not_armed`;
  - immediately enqueue a deterministic `close` exit intent for the position, because the money is
    spent and an unprotected desk position must not stay open;
  - page through `_safe_alert`.
- **Test:** an arm failure on a desk fill produces no `filled` intent without rails, and does
  produce a close intent.

**R2 — Override authority for `desk`.**
- `_EXIT_OVERRIDE_PRIORITY` (`db_memecoin.py:3379`) is
  `["operator", "micro", "scalp", "ride", "trail", "template", "auto-classify"]`. It has no
  `desk`, and an unknown source has no defined rank.
- **Proposal:** insert `desk` directly after `operator`.
  - The operator can still override a desk position, with full authority, including loosening.
  - No cohort overlay (`micro`, `ride`, `trail`, `template`, `auto-classify`) may replace the
    seat's rails on a desk position. Their writers must skip `signal_source='desk'` positions.
- **TIGHTEN semantics:**
  - The seat names only the rails it changes. Code builds the **complete** new effective policy
    by preserving every omitted field from the active desk row.
  - It compares that against the complete current effective policy (`effective_funded_policy`).
  - It writes the new row in one transaction, under the same lock as the read.
  - Any field looser than before rejects the whole answer.
  - An `operator` row that is active on the position means the desk cannot write at all. The
    seat's TIGHTEN is recorded and not applied, because the operator owns that position now.
- **Required `sl_pct`:** reject `null`, 0, NaN, ±inf, negative, and anything outside
  `MEMECOIN_DESK_RAIL_BOUNDS`. The same rule applies to every required rail.
- **Test:** each bad `sl_pct` value is rejected. A TIGHTEN naming one rail preserves the others.
  A cohort overlay never replaces a desk row.

**R3 — One reservation guard for desk and copy.**
- Today the coin-level unique guard covers only `memecoin_pending_entries` rows with
  `status='reserved'` (`idx_mc_pending_coin_uniq_v2`, `db_memecoin.py:437`).
- `_entry_pending` is keyed by `source_user_id` (`memecoin_real_runner.py:5812`).
- The open-position unique index includes `source_user_id` and `strategy`.
- **So an armed wallet can buy a mint the desk holds**, and the reverse: different `uid`,
  different `strategy`, no collision.
- **The rule:** one final guard on `(network_id, lower(mint))`, origin-blind. It refuses when any
  of these exists, from any origin:
  - a `reserved` pending entry;
  - an attempt in an unresolved state (`submitted_unknown`, `transaction_unresolved`,
    `retry_pending`, `needs_review`);
  - an open real position.
- It runs at reservation, inside the transaction that writes the reservation.
- What the copy lane does when the desk already holds the coin (refuse, or add) is the
  operator's call. v1 refuses both ways, so a coin is never doubled across lanes.
- **Test:** a desk reservation is refused while an armed copy entry on the same mint is
  unresolved, and the reverse.

**R4 — Producer H scans watch-only owners without widening funded authority.**
- The confirmed scanner's watchlist (`scripts/research/onchain_wallet_watch.py:666`,
  `registry_watchlist`) uses `armed_verified_owners()` only.
- Watch-only owners come from `wallet_observation.observation_owners()`
  (`sniper/services/wallet_observation.py:305`): current verified identities including watch-only
  pump profiles, with no authority filter.
- **The rule:**
  - Producer H takes its owner set from `observation_owners()` minus the armed set. It reuses the
    scanner's receipt verification (signer, payment, acquisition).
  - It writes **only** desk candidates in `data/memecoin_desk.db`.
  - It never writes `memecoin_events`, `memecoin_entry_attempts`, `onchain_wallet_detections`, or
    any row the funded copy lane reads.
  - Nothing in it marks a wallet approved or armed.
- **Test:** a confirmed buy by a watch-only owner yields one desk candidate and zero rows in the
  copy lane's input tables. An armed owner's buy is unaffected.

**Measured latency, end to end, per ticket.** Each row stores:
- the model's `latency_ms`;
- for a WAIT, the gap from the decision to the condition firing;
- the intent's `requested_at → claimed_at → resolved_at`.

The operator accepted 1–3 s for the model call. The read reports p50/p90 of each stage and never
an estimate.

**Drain worker.** Today intents wait for the main-loop pass (p90 11.93 s), and desk intents would
share `max_per_pass=2` with the operator's own buys. Proposal: a `ManualIntentWorker` that is a
copy of `ManualExitWorker` (`memecoin_manual_exit_intents.py:183`): its own thread, its own
connection, woken by `LaneWake`. Operator intents go first. This is the only runner-side threading
change in this note. Its claim lag must be measured (p50/p90) before and after.

## 3. Rule-5 edge-case map

1. **Money path: yes.** The desk reaches entry, sizing and signing through `real_entry_hook`. Every
   item below therefore goes to the operator before any edit.
2. **Speed on the fill path.**
   - No new call lands on the copy lanes: the slot lane, the fast lanes and the source-triggered
     paths are untouched.
   - The desk's own path adds no RPC. `real_entry_hook` runs exactly as it does for a manual buy.
   - The one runner change, the drain worker, takes work out of the main loop. Measured baseline:
     claim lag p50 3.54 s, p90 11.93 s.
   - Model latency (1–3 s, accepted by the operator) happens before enqueue, in a separate
     process, and is recorded per row with p50/p90.
   - A WAIT fires from code on 1 s bars with no model call in the loop.
3. **Concurrency and global state.**
   - Seats run in a separate desk process and hold nothing inside the runner.
   - The drain worker gets its own connection, never the main loop's.
   - `buy_fn` already builds a fresh event per call, and a desk event is a new instance, never a
     mutation of a class attribute (`_ManualEv.signal_source` is a class attribute today).
   - **Trap:** never flip `_ManualEv.signal_source` at runtime. Pass it to the constructor.
4. **Absent versus zero versus unknown.**
   - Seat inputs: `null` plus a `missing` list.
   - `cost_usd` NULL means unknown, never 0.
   - A model returning `null` means SKIP.
   - A NULL `source_user_id` on a desk position means "no wallet", and the duplicate guard must be
     proven to hold for it (§1.5).
   - Desk PnL with an unresolved fill means "unknown". It never counts as 0 for the desk latch.
   - A rail the seat left `null` under `rails_mode='custom'` becomes 0, which means OFF. That is
     why `sl_pct` is required.
   - A bar with no swaps, a gap, or a slot with no time: the condition is not met. It is never
     evaluated against a 0 price.
5. **Dependency failures.**
   - Model timeout, 429, outage or malformed answer: SKIP with a reason, never a retry that could
     flip the verdict.
   - Runner down: the intent expires visibly after its TTL.
   - Entry ambiguous: `unresolved`, counted as exposure.
   - HL host outage: covered only by the resting stop held on the exchange (§1.4).
   - A boundary-seat failure means HOLD on the armed rails, never EXIT, never loosen.
   - **A wrong answer**: the model says ENTER on a coin the code would kill. The funnel order makes
     code kills run first. Safety gates still run inside `real_entry_hook`.
   - Swallowed errors: every failed read becomes a `null` in `inputs.missing`, never an empty list
     presented as "no trades".
6. **Units and identity.**
   - `(network_id, mint)` everywhere. EVM mints are lowercased before hashing.
   - Size is USD.
   - Slot versus EVM block versus seconds: candles carry `block_no` ranges, and time comes only from
     `block_times` (§4).
   - `memecoin_positions.network_id` versus `memecoin_position_marks.chain_id`. This is a known
     naming trap.
   - The HL cloid domain is separate from paper `copy_id`.
7. **The sample.**
   - The 30-close read's denominator is desk ENTER/WAIT-fired tickets whose intent filled. Refused and expired
     intents are reported beside it, never folded in.
   - The shadow arm records SKIPs and code kills with their later price path (measurement only),
     so seat value is read against the coins the desk refused. The paper-first rule of 2026-10-01
     still binds what the shadow arm may decide.
   - Because desk entries pass through our own safety gates, the result describes desk+engine, not
     the seat alone.
8. **Rollback.**
   - `MEMECOIN_DESK_ENABLED=0` stops new tickets and new intents.
   - Queued desk intents are refused with `desk_disabled`, or expire.
   - Open desk positions keep their frozen contract and runner exits.
   - The two intent columns are additive (`DEFAULT 'manual'`), so the old drain reads them
     unchanged.
   - Half-applied state (columns added, worker not deployed) is the status quo: intents drain in
     the main loop.

**Exit-rails ruling (operator, 2026-10-21).** On the desk lane, rails come from the seat, are
checked against bounds by code, and are rejected rather than clamped. They ship live through the
`rails` / `rails_mode='custom'` carrier. The 2026-10-01 paper-first rule continues to govern the
funded copy policy; this ruling scopes the desk lane only.

**Checks to leave behind (build phase).** Each one must be shown to fail first:
- (a) a desk event is refused at the concurrency cap where a manual event is admitted;
- (b) `null`, malformed, timeout and out-of-bounds seat answers each write SKIP and enqueue
  nothing. The fixtures include the WAIT condition `['mc','<=',0,0,0]` and every single-field
  violation of the condition grammar (`cond_out_of_range`);
- (c) a second identical `(network_id, mint, seat, inputs_hash)` insert raises;
- (d) an ENTER without `sl_pct`, or with any rail outside its bounds, enqueues nothing;
- (e) a TIGHTEN that raises `sl_pct` or drops an armed rail changes nothing;
- (f) a desk intent's override row has `source='desk'`, never `'operator'`, and leaves the
  catastrophe floor intact;
- (g) a WAIT whose condition sits on a `gap` bar does not fire, and expires;
- (h) R1: a desk fill whose rails fail to arm ends `refused` plus a close intent, never `filled` unarmed;
- (i) R2: bad `sl_pct` values are rejected, TIGHTEN preserves omitted rails, and a cohort overlay never replaces a desk row;
- (j) R3: desk and copy reservations on one `(network_id, mint)` block each other, including unresolved attempts;
- (k) R4: a watch-only owner's confirmed buy writes a desk candidate and nothing the copy lane reads.

## 4. 1-second candles on the slot clock

**What exists.**
- `memecoin_candles.to_candles(rows, interval_s)` (`memecoin_candles.py:52`) buckets on
  `sweep_ts` epoch seconds over `memecoin_position_marks`. It therefore has data only for coins we
  hold, and its marks are our quote cadence, not the market's trades.
- `data/memecoin_ticks.db` → `pool_ticks` has **110 rows in total** (chain 8453, all from
  2026-10-19), so it is not a usable source. Its `block_times` hold 4663 up to 2026-09-04.
- `data/memecoin_sol_tape.db` → `pool_ticks` (kinds `sol_swap`, `sol_sync`, `sol_curve_swap`,
  `sol_curve_sync`, `sol_mint`, `sol_burn`):
  - `block_no` = slot, non-null on 200,000 of the newest 200,000 rows.
  - The latest swap was 2026-10-20 14:54Z.
  - Pools come from `sol_position_pools`, so coverage is scoped to positions we held.
  - Measured batch clock: 2,580 rows share one `observed_at` second (2026-07-29T01:44:53) across 511
    distinct slots; the next four worst seconds hold 2,458, 2,321, 1,240 and 970 rows. The figure of
    1,421 rows in one second was not reproduced on the main ticks DB, but the same artefact is
    present here.
  - `block_times` holds 91,992 Solana slots.

**Gamma's calibration read, 2026-10-21: the held-coin Solana tape FAILS and must not be reused for
desk bars** (brain finding 05b5ebfd; `docs/audits/2026-10-21-rug-exit-fill-vs-tape.md`, local
0ef4a5e04). Defects:
- **Price is off.** The Sync reserve ratio against `memecoin_fills.price` on normal fills ≤10 s
  apart has a median ratio of 1.91 for AMM pools (p10 0.26, p90 5,568, n=38) and 2.78 for pump
  curves (p10 0.80, p90 12.3, n=26).
- **Side and decimals are guessed.** `pool_meta.quote_side` and the decimals are NULL for these
  pools.
- **One slot carries several different reserves.** Multiple `sol_sync` rows per slot from
  different accounts disagree.
- **Coverage starts late and ends early.**
  - The first row lands after our entry in 16 of 17 positions (median 119 s).
  - It ends ≥60 s before the exit fill in 12 of 17.
  - Our own swaps are absent: 0 of 17 entries and 0 of 17 exits.
- **Held coins only.** Pools come from held positions, so there is no history before a candidate
  decision.

**The bar source must therefore be producer U's own stream** (§2.0), not this tape, with four
properties:
- the quote side and decimals are known from the program layout;
- **one reserve state per pool per slot**: the last write in the slot for the pool's own
  vaults/curve account;
- coverage starts at band entry, before the decision;
- our own fills appear in it.

**Pass bar before any desk reuse:** fill/tape within ±10% on normal fills. Until a bar source
passes, a WAIT condition cannot be evaluated, so v1 seats may answer ENTER or SKIP only (WAIT is
rejected as `cond_unavailable`).

**Proposal** (applies to the producer-U bar builder, not to `memecoin_sol_tape.db`).
1. **Bucket key = slot.** Swaps in a pool are grouped by `block_no`. A "1 s" candle is the set of
   slots whose `block_times.block_ts` falls in that second.
2. **A slot absent from `block_times` is `DATA_INSUFFICIENT`.** It is never converted with an
   assumed 0.4 s/slot. This is the rule already written in `db_ticks.py:186`.
3. The candle carries `slot_lo`, `slot_hi`, `n_swaps` and `n_slots_missing_time`. `observed_at` is
   kept only as ingest latency.
4. **Price is from the swap's own amounts** (signed `amount0/amount1` with the decimals from
   `pool_meta`), never from a provider mid. The DexScreener order of 2026-09-30 applies.
5. **Coverage for desk candidates** (coins not yet held) comes from producer U's stream, which sees
   the curve from launch. A graduated coin needs its PumpSwap pool vaults on the same stream. The
   watch set is bounded at N pools (`MEMECOIN_DESK_WATCH_MAX_POOLS`, §7). This never runs on the
   funded read lane (finding 2026-09-17).
6. **EVM candles are out of v1.** The main tick tape is dead, and reviving the EVM ingest is its
   own job. Desk v1 trades only on Solana for anything candle-dependent.
7. **Reusing crypto5m.** `polymarket_crypto5m/capture.py` gives the gap pattern to copy: a gap
   raises a quality flag and forces a re-snapshot, and an uncertain book is never treated as
   executable. Apply it to the candle builder: a slot gap in the tape (`tick_gaps`) marks the
   candle `gap`, and seats receive `null` for it.

## 5. Build order (each step its own branch, Gamma pass, knob ON at deploy)

1. **Origin split**, with no seats yet:
   - `bypasses_selection`, the desk analytics bucket, the two intent columns, and checks (a)/(c);
   - R3's origin-blind reservation guard and check (j);
   - R2's `desk` rank in `_EXIT_OVERRIDE_PRIORITY`, the overlay skip, and check (i);
   - R1's policy carried through reservation, fill and recovery, and check (h);
   - a hand-written desk intent proves the path end to end at the smallest clip.
2. **`ManualIntentWorker`**, with claim lag p50/p90 measured before and after.
3. **`desk_tickets` and the seat validator**, plus checks (b), (d), (e) and (g). A code-only funnel
   (hard filter → size) runs first. The model seats come after it.
4. **Producer H** (R4, check (k)), then **producer U** (Pump.fun gRPC; the provider plan needs an
   operator yes).
5. **Slot-clock candles** for held pools from `memecoin_sol_tape.db`, then the desk watchlist on
   the paper vault stream.
6. **HL desk lane**: its own spec (§1.4 items 1–4) and an operator re-arm of the HL runtime.

## 6. Rug and honeypot defense (operator priority, 2026-10-21, via Alpha)

The desk must not be fooled by a coin that looks good and is a rug or a honeypot, either before
entry or while it holds the coin. Everything in this section is **code**. A seat reads the
results; it never computes or overrides them.

### 6.0 What the funded system does today (read 2026-10-21)

- **Not working: there is no deterministic LP-pull exit on funded positions.**
  - `classify_lp_removal` (`sniper/analysis/memecoin_lp_removal.py`) is called only by
    `memecoin_exit_brain.py` and `memecoin_v6_engine.py`.
  - The exit engine has authority only on chains listed in `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS`,
    which is **empty** in `.env`. Missing or empty fails OFF
    (`sniper/settings/memecoin.py:705`, `engine_authoritative_chains`).
  - A funded position today exits on its rails, on `b_stop`, `vol_death`, a mirror sell, or the
    catastrophe floor (`MEMECOIN_REAL_CATASTROPHE_PCT=80`, frozen per contract). An LP pull shows
    up only as a price collapse through those rails.
  - So the earlier assumption "LP-pull emergency exits stay deterministic" does not hold for
    funded positions. The desk has to add one (§6.2).
- **Not running: the cluster admission gate.**
  - `MEMECOIN_REAL_CLUSTER_GATE_ENABLED` defaults to 0 and is not set in `.env`.
  - `scripts/memecoin_cluster_gate_worker.py` is not running (0 processes).
  - Its traces spend the research RPC quota (`ClusterQuotaError`).
- **Running and fail-closed: token authority** (`solana_token_safety.token_safety_ok`). It already
  runs inside `real_entry_hook` for every funded entry, manual and desk included.
- **Exists but advisory and fail-open: the holder scan.** `solana_holder_scan.holder_concentration`
  is called only by `scripts/research/memecoin_holder_scan.py`. No funded writer persists a holder
  snapshot. Its own caveat: the largest accounts include the LP vault, burn and CEX addresses.
- **Observation-only: mid-hold authority watch.** `memecoin_token_authority.py` watches at most 7
  held mints and is proven by grep to have no decision authority.
- **Research-only: serial-creator history.** Creator identity exists only in research stores
  (`sniper/research/memecoin_opportunity/`), fed by the stopped opportunity worker and the dormant
  pump tape. No live creator prior exists.
- **EVM:** `evm_token_safety.token_safety_ok_evm` (`evm_token_safety.py:668`) checks the sell route
  and simulates a sell. This is for desk chains after Solana.

**Operator rulings, 2026-10-21 (in chat, to Beta):**
1. **Copy-lane LP-catastrophic exit: yes, on the condition that it does not slow anything down,
   for sure.**
   - The design that meets that condition: the detector reads the reserve pairs the funded
     `SolanaSwapMarkCache` already keeps in memory for the `vol_death` silence rail
     (`reserve_witness`, `memecoin_solana_swap_marks.py:727`).
   - That means **no new RPC, subscription, lock or retry**, and nothing on the entry path. It
     runs in the exit sweep beside the existing rails, and its exit goes through the existing exit
     executor.
   - **Proof required before deploy:** the measured p50/p90 of the exit-sweep tick and of the
     entry decide→send, before and after, on the pinned release. A regression on either refuses
     the deploy.
   - v1 is Solana only. EVM positions need a separate reserve source, scoped later.
   - It is its own branch (copy lane), separate from the desk build.
2. **Cluster admission: yes, but for the desk only.** The funded copy path stays as it is
   (`MEMECOIN_REAL_CLUSTER_GATE_ENABLED` stays 0).
   - The desk calls the cluster gate service from its own `hard_filter`, through a desk-scoped
     client and worker, on the research quota.
   - It never sets the funded copy knob, and never runs on the copy entry path.

**Step-1 pass notes (Gamma via Alpha, 2026-10-21), for the step-3 hard filter:**
- **The desk must check market data itself.** Its intents ride manual's selection bypass, so
  `_market_data_unavailable_reason`, the dead-coin gate and the volume floors do not run for a desk
  entry. The desk `hard_filter` must therefore require market data: a coin with none is a KILL,
  never a pass.
- **The daily halt counts realized PnL only.** Open desk losses sit on top of it. The worst-day
  bound is 200 USD realized plus 5 open × 25 USD clip = **325 USD**, before slippage beyond the
  clip.

### 6.1 Pre-entry safety stack (desk `hard_filter`, cheapest first, each a code KILL)

Every check returns `pass | kill:<reason> | unknown:<failure>`. **`unknown` kills on the desk**: a
desk entry is discretionary, and nothing obliges the desk to buy a coin it cannot read. This is
the opposite of the copy lane's holder-scan fail-open rule, and it is deliberate. Each `unknown`
records its failure kind (timeout, rate limit, malformed, provider down). A provider outage then
shows up as many `unknown`s, not as a wave of "unsafe coins".

| # | Check | Source | Cost | Kill rule (bounds are operator knobs) |
|---|---|---|---|---|
| 1 | Mint/freeze authority, Token-2022 fee, permanent delegate, DBC hook | `solana_token_safety.read_mint_account` + `token_safety_verdict` | 1 `getAccountInfo` | same verdict as the funded gate; run here as well, so a kill happens before any seat call |
| 2 | Serial-creator prior | creator → prior launches and outcomes, built by producer U's stream (§2.0) | local read | `null` until U has history: the field goes into `inputs.missing` and is never treated as "clean". A kill rule waits for Gamma's result (§6.4) |
| 3 | Holder snapshot | **new writer**: `getTokenLargestAccounts` + `getTokenSupply` (reusing `holder_concentration`), with the **LP vault, bonding-curve account and burn address subtracted** before computing top-10 %. Creator % uses the creator's token accounts. | 2 calls | top-10 ex-LP % > bound, or creator % > bound, kills. Stored per ticket as `inputs.holders` with the account list, so §6.2 knows whom to watch |
| 4 | Cluster admission | `memecoin_cluster_admission.decide_cluster` via a **desk-scoped** cluster gate worker (operator: desk only, 2026-10-21) | N funder traces, research quota | the funded policy's verdict (`solana-funded-cluster-admission-v3` ratios). A quota error or timeout is `unknown`, which kills |
| 5 | Sellability probe (new) | exact-route **sell** quote of the exact quantity the buy quote says we would acquire, through the same route builder the exit uses | 1 buy quote + 1 sell quote | no route, or round-trip loss > bound, kills. Typed failures keep **token** (no route, honeypot shape) apart from **provider** (timeout, 429), as the V6 remediation rule requires. A provider failure is `unknown`, never "unsellable" |
| 6 | EVM sell-route + sell-sim | `token_safety_ok_evm` | per its module | for later desk chains only |

**Limits of a quote.** A sell quote shows that a route prices the sell. It does not show that the
token lets a holder sell. Freeze authority (check 1) and a Token-2022 transfer hook (check 1, DBC
case) are the Solana sell blockers we know of. A non-DBC transfer hook is **not covered by check
1** today. The desk must kill any mint with a transfer hook whose program is not on an allowlist.
That is a new rule for check 1 on the desk only, and the operator approves the allowlist.

**Rule 2 speed.** Checks 1, 3 and 5 sit on the desk's own decision path, before the seat call.
That is accepted under ruling 61859b83, and each is measured as p50/p90 per row. None runs on the
copy path. The new holder writer and the sell probe use the desk's research read key, never the
funded read lane.

### 6.2 In-position rug monitor (typed boundary events)

Each signal is a deterministic detector that emits a typed event into `desk_tickets`:

- `stage='risk'`
- `seat='code:<detector>'`
- `verdict='EXIT'` for code exits, otherwise `'PASS'`, with the event carried to the boundary seat
- `inputs_json` = the lead data

The boundary seat (§2.3) receives the event and may answer TIGHTEN or EXIT, never more. **Code
exits fire first, without waiting for the seat, and the seat cannot postpone them.**

| Event | Detector and lead data | Cadence and data source | Who acts |
|---|---|---|---|
| `lp_reserve_drop` | quote-side vault reserve against the max since entry. Lead data: `fraction`, `slot`, `signature`, `route_degraded` | **1 s tape**: Solana vault/curve subscription of the funded swap-mark lane, already per held position | `classify_lp_removal` → `catastrophic` means **code EXIT (new for desk, §6.0)**; `material` is a seat event; `minor` is logged only |
| `authority_change` | freeze or mint authority set, fee or delegate changed. Lead data: the field, before and after, slot | exit-sweep cadence (`memecoin_token_authority` poll, raise its 7-mint cap to cover the desk book) | freeze authority set means **code EXIT** (our account can be frozen); anything else is a seat event |
| `creator_or_top_holder_sell` | a sell from an address in the entry holder snapshot (§6.1 #3). Lead data: address, share of supply sold, slot, signature | **1 s tape**: `maker` on the pool's swap rows matched against the snapshot | seat event. Never a code exit by itself: a top holder trimming is not a rug |
| `sell_buy_flip` | over a rolling window of N slots, sell count or sell USD exceeds buys by a bound after being below it. Lead data: both windows | **1 s tape** (`pool_features`-style windows: `buy_n`, `sell_n`, `net_flow_quote`) | seat event |
| `maker_collapse` | distinct makers per window fall below a fraction of the entry window. Lead data: both counts | **1 s tape** (`makers_n`) | seat event |
| `sellability_fail` | the exit sweep's exact whole-balance preview returns a **token** failure (no route, or output collapses) on consecutive previews. Lead data: preview status, failure kind, consecutive count | exit-sweep cadence (the existing preview; no new call) | after K consecutive token failures, a **code EXIT** attempt through the existing exit path; provider failures never count (rule of 2026-07-27) |
| `roster_buy_after_entry` | §2.0 | producer H scan | seat event |

**Hard floors stay code.** `catastrophe_pct` (frozen contract), `b_stop`, the seat's own armed
`sl_pct`, the new `lp_reserve_drop` catastrophic exit, the freeze-authority exit, and the
`sellability_fail` exit all fire without the seat. A seat failure means HOLD (§2.3), and HOLD
never blocks a code exit.

**Absent versus zero:**
- A detector that cannot read its source emits `monitor_blind:<detector>` and never a quiet "no
  event".
- If the tape for a held pool is blind for longer than `MEMECOIN_DESK_MONITOR_BLIND_S`, that is a
  seat boundary with `inputs.missing=[...]`.
- The detectors' own thresholds are unset until §6.4 lands. Until then every detector emits its
  measurement as an observation row, and only the three code exits (LP catastrophic, freeze
  authority, sellability K) are armed, with operator-set bounds.

### 6.3 Which signals ride which cadence

- **Exit-sweep cadence** (the runner's existing loop, about one exact quote per held position per
  tick under `MEMECOIN_EXACT_QUOTE_PER_MINUTE`; no new provider calls):
  - `sellability_fail` (reads the preview the sweep already makes);
  - `authority_change` (bounded poll);
  - the rails and catastrophe floor.
- **1 s tape** (§4, slot-keyed): `lp_reserve_drop`, `creator_or_top_holder_sell`,
  `sell_buy_flip`, `maker_collapse`.
  - These need the pool's swap and vault stream while we hold.
  - For held funded Solana positions, the swap-mark lane already subscribes to the vaults. The
    desk detectors **read** its cache and add no second subscription on the funded key.
  - For pre-entry candidates, the paper vault stream's desk watchlist (§4.5) supplies the same.
- **Not done:** an LP reserve drop is never inferred from DexScreener or any provider mid (order
  2026-09-30). A slot with no swaps is no information, never "reserve unchanged".

### 6.4 Wiring to Gamma's pre-rug read

Gamma is measuring which of these signatures fired before our own funded rugs. Her result decides
three things:
- which detectors get a kill or exit threshold, and at what value;
- whether `creator_or_top_holder_sell` or `sell_buy_flip` earn a code exit or stay seat events;
- the serial-creator kill rule (§6.1 #2).

Until her read lands, those stay measurement plus seat events. Each detector's row carries the
`detector_version`, so her later grade joins on exact versions.

**Result, 2026-10-21 (Gamma, de92e4474).** 84 rugs in 2,287 funded closes, a 3.7% base rate.
- **Pre-entry, one variable at a time:**
  - token age < 600 s: 2.6× base (precision 9.9%, recall 43%);
  - token age < 120 s: precision 25%;
  - source buy < 100 USD: precision 9.9%;
  - everything else within 1.5× of base.
- **Authority and cluster could not be tested.** The `memecoin_token_authority` tables are defined
  but never created (no caller), and `cluster_reason` is empty on every fill.
- **In position:** of 24 Solana rugs on the tape, only 7 ever showed a 40% drop. Signatures against
  975 base windows:

  | Signature | Fired, of 7 |
  |---|---|
  | LP burn | 0 |
  | sell/buy flip | 1 |
  | maker collapse | 0 |
  | creator sell | 0 |
  | reserve −20% | 0 |

- 17 of 24 rugs booked ≤ −60% while the tape never showed the drop. Gamma is reading exit fill
  against tape for the copy lane, so the tape itself is suspect (§4).

**Consequences for the desk:**
1. The §6.2 detectors stay **measurement only**. Only the three code exits are armed (LP
   catastrophic, freeze authority set, K token-sellability failures). The evidence supports
   nothing more.
2. `token_age_s` goes on the fact sheet, and a hard-filter age floor becomes a candidate rule for
   the operator. It is the only pre-entry variable above 1.5× base.
3. **Persist the authority check result on every desk candidate**, pass or kill, with its raw
   response hash. Today a pass is never stored, which is why authority could not be graded.

### 6.5 Checks to leave behind (build phase)

- (l) a mint with a live freeze authority, a non-allowlisted transfer hook, or an unreadable mint
  account is killed at `hard_filter` and never reaches a seat;
- (m) a provider timeout in the sellability probe records `unknown:provider_timeout` and kills on
  the desk, but never increments a token-unsellable counter;
- (n) a top-10 % computed with the LP vault included differs from the ex-LP value, and the ex-LP
  value is the one used;
- (o) an `lp_reserve_drop` classified `catastrophic` enqueues a close intent with no seat call, and
  a seat answering HOLD does not delay it;
- (p) a blind tape emits `monitor_blind`, never a missing row.

Build order: §6.1 checks 1, 3 and 5, and the three code exits in §6.2, join **step 3** (before any
model seat). The 1 s detectors join step 5 with the candles. Desk cluster admission (its own worker)
joins step 3. The copy-lane LP-catastrophic exit is a separate copy-lane branch, gated on its
latency proof.

## 7. Data lifecycle (operator priority, 2026-10-21, via Alpha)

**Context:** `data/` is 69 GB (`memecoin_ticks.db` 34 GB, a 4663 tape that ended 09-04;
`memecoin_fill_timing.db` 17 GB), with 241 GB free. Alpha measured the Pump.fun stream at
320–400 KB/s, about 30 GB/day raw. The desk must never be the thing that fills the disk, and it
must never touch the ledger to save space.

### 7.1 Rules

1. **Raw gRPC frames never touch disk.**
   - Producer U decodes into a RAM ring buffer bounded in bytes
     (`MEMECOIN_DESK_UNIVERSE_RING_MB`). When full, it drops the oldest frames and counts the
     drops.
   - A drop marks the affected mints' curve state `unknown`, as in §2.0 U edge 3. It never fills
     anything in.
2. **Only derived rows are persisted:**
   - launch rows (op_launches-style, one per mint);
   - band-entry candidate rows;
   - 1 s candles **only for pools in the watch set**, bounded at `MEMECOIN_DESK_WATCH_MAX_POOLS`;
   - `desk_tickets`, seat calls and outcomes.
   - Seat `inputs_json` stores **references** to candles (pool, slot range, content hash), never
     the candle rows inline.
3. **Partition by day.** Each stream gets one SQLite file per UTC day:
   `data/desk/<stream>/<YYYY-MM-DD>.db`, for the streams `launches`, `candles_1s` and `candles_1m`.
   - Retention means **unlinking whole closed-day files**. The desk never runs DELETE+VACUUM on a
     live DB.
   - Today's file is never touched by retention.
   - A closed day may be converted to Parquet with the `sniper/sim/compact/` manifest pattern
     (`CompactManifest`: processed-file set, idempotent). The SQLite file is unlinked only after
     the manifest records the conversion.
   - `desk_tickets` stays in one file (`data/memecoin_desk.db`, §2.1): it is small and kept forever.
4. **Downsample, then expire.**
   - At day close, the closed `candles_1s` day is rolled into a `candles_1m` day.
   - 1 s days are kept `MEMECOIN_DESK_CANDLE_1S_RETENTION_D` = 7 days.
   - 1 min days are kept `MEMECOIN_DESK_CANDLE_1M_RETENTION_D` = 90 days.
   - Launch days are kept `MEMECOIN_DESK_LAUNCH_RETENTION_D` (operator value).
   - Tickets, decisions and outcomes are kept forever.
   - Same retention style as `AXIOM_RAW_RETENTION_DAYS` and `MEMECOIN_TICKSTREAM_RETENTION_D`.
5. **Hard disk caps** over every desk file (`data/desk/` plus `data/memecoin_desk.db`):
   `MEMECOIN_DESK_DISK_SOFT_CAP_GB` = 15 and `MEMECOIN_DESK_DISK_HARD_CAP_GB` = 20 (operator,
   2026-10-21: hard cap 20 GB total; soft cap is Beta's call). Same shape as
   `MEMECOIN_OPPORTUNITY_HOT_SOFT_CAP_MB` / `_HARD_CAP_MB`. At N=50 the worst-case 30-day footprint
   (§7.2) is about 5 GB. A watch set above about N=190 would hit the soft cap from the 1 s candles
   alone.
   - **Soft cap:** stop writing candles for the oldest watch-set pools, so new candidates keep
     their bars, and log it.
   - **Hard cap:** stop producer U and page. The desk then has no candidates, and that is the
     intended failure.
   - **Never the ledger**, and never another lane's DB.
   - Free disk under `MEMECOIN_DESK_MIN_FREE_GB` also counts as the hard cap.
6. **WAL.**
   - Every desk file is its own DB with its own WAL, and only **passive** checkpoints run (the
     `scripts/wal_checkpoint.py` rule).
   - Desk processes never open `sniper_tracker.db` for writing. The one ledger write is the intent
     row, done by the producer through `memecoin_manual_intents.enqueue` on a short-lived
     connection.
   - A closed-day file is released by its single writer, then opened read-only.
7. **Reuse:** `AXIOM_RAW_RETENTION_DAYS` and `MEMECOIN_TICKSTREAM_RETENTION_D` for the retention
   style, the hot soft/hard cap knobs for the cap shape, and `sniper/sim/compact/run.py` +
   `manifest.py` for idempotent closed-day compaction.

### 7.2 Bytes per day and 30-day footprint

**Row sizes:**
- **Launch row:** 354 B average payload, measured on `op_launches` in
  `data/memecoin_opportunity.db` (9 columns, n=121 rows, a small sample). Estimated at about
  500 B on disk with page and index overhead.
- **1 s candle row:** pool id (dictionary-coded INT), t, slot_lo, slot_hi, o/h/l/c, buy_usd,
  sell_usd, n_swaps, n_makers, flags. Estimated at about 130 B with the (pool, t) key. Only
  seconds with at least one swap are written.

| Stream | Median launches (40k/day) | p90 proxy (48.4k/day, 14-day max) | Kept | 30-day footprint |
|---|---|---|---|---|
| Raw frames | 0 B on disk (RAM ring only) | 0 B | n/a | 0 |
| Launch rows | 20 MB/day | 24 MB/day | operator (30 d shown) | 0.6–0.73 GB |
| 1 s candles, N=50 watch pools, **worst case** (a swap every second) | 11.2 MB/pool/day → 562 MB/day | same (bounded by N, not by launches) | 7 days rolling | 3.9 GB |
| 1 min candles, N=50 | 187 KB/pool/day → 9.4 MB/day | same | 90 days | 0.28 GB (30 d) / 0.84 GB (90 d) |
| desk_tickets + seat calls (500 seat calls/day × about 8 KB, candles by reference) | about 4 MB/day | about 4 MB/day | forever | 0.12 GB |
| **Total, 30 days** | | | | **about 4.9 GB (median) / 5.0 GB (p90), worst-case candle density** |

**Caveats:**
- The candle line is an upper bound. The real active-second fraction per pool is unmeasured, and
  most pump pools trade in bursts.
- N is the cost lever: N=200 makes the 1 s line 2.2 GB/day and 15.7 GB for 7 days. The soft cap
  must sit below that.
- The 48.4k figure is the maximum of Gamma's 14 days (09-28..10-11), used as a p90 proxy. The true
  p90 is unmeasured.

### 7.2a ML training archive (operator question via Alpha, 2026-10-21)

The decoded swap rows from producer U's Pump.fun stream are the forward training corpus. They are
**not** candles and are **not** deleted after 7 days.

- **Hot:** decoded rows land in that day's SQLite file `data/desk/swaps/<YYYY-MM-DD>.db`.
  - Lambda estimates 1.3–1.6 GB/day in SQLite.
  - Today's file and any closed day not yet compacted **count toward the 20 GB desk hard cap**.
    A compaction that falls two days behind therefore hits the soft cap, which is the intended
    alarm.
- **Nightly compaction** reuses `sniper/sim/compact/run.py` + `CompactManifest`.
  - It writes one zstd Parquet file per day per chain: `data/ml_archive/swaps/<chain>/<date>.parquet`.
  - mint, pool and actor are interned as integers, with a per-day dictionary file beside the
    Parquet.
  - It then unlinks the SQLite day file.
  - Estimate: about 200–300 MB/day compressed (Alpha's figure, not measured).
  - **Build note:** `ParquetTableWriter` (`sniper/sim/compact/writer.py`) exposes no compression
    argument today, so zstd and the interning are a small extension there.
- **Row-count check before any unlink.** The manifest records, per day file: the SQLite
  `COUNT(*)`, the Parquet row count, and both content hashes. If the counts differ, the day is
  **not** marked processed and the SQLite file is **not** unlinked. The job exits non-zero and
  pages. A day is processed only when the counts are equal and the Parquet reads back.
- **Retention:** 90 days, under its **own** budget `MEMECOIN_ML_ARCHIVE_CAP_GB` (operator value),
  separate from the desk cap. Over budget, the oldest whole Parquet days are unlinked first, and
  this never touches a desk or ledger file. 90 days at 200–300 MB is about 18–27 GB, so the
  operator's cap must sit above that or the effective retention is shorter. The job logs the
  effective retention.
- **Readers:** Gamma's studies and any future model, offline only. **Nothing live reads Parquet.**
  No entry, exit or seat path opens `data/ml_archive/`.
- **Check (u):** a day whose Parquet row count differs from its SQLite day file stays unprocessed,
  keeps its SQLite file, and exits non-zero.

### 7.3 Checks (build phase)

- (q) a full ring buffer drops frames, increments a counter, and marks the mints `unknown`; it
  never writes a frame to disk;
- (r) retention unlinks only closed-day files older than the window, and never today's file;
- (s) at the hard cap, producer U stops and pages, and no write reaches a non-desk DB;
- (t) a seat's `inputs_json` holds candle references, not candle rows.

## 8. Deploy runbook notes

- **Reclaiming `data/memecoin_ticks.db` (operator window; Alpha asks).** The file is 35.7 GB with
  33.1 GB of free pages and 2.6 GB live. Its live tables (`memecoin_exit_market_state`,
  `memecoin_exit_candidate_decisions`) are written by the runner, which holds the file open.
  - **Procedure:** stop every process holding the file (runner and any tick writer); confirm no
    holder with `fuser`; run `VACUUM INTO 'memecoin_ticks.db.new'`; run `PRAGMA integrity_check`
    on the new file; rename the old file to `.pre-vacuum` and the new file into place; start.
    Delete `.pre-vacuum` only after a healthy runner heartbeat.
  - **Do not run `VACUUM INTO` while the runner is live and swap later.** The snapshot freezes at
    the vacuum, so every exit-state row written between the vacuum and the swap would be lost. The
    vacuum belongs inside the stop window: about 2.6 GB to copy, so the measured duration sets the
    window length.



## 2026-11-01 amendment: independent observed-tape CROWD pilot

The separate `large-buyer-crowd-v1` pilot is governed by `2026-11-01-crowd-funded-pilot.md`. Its default-off `MEMECOIN_LARGE_BUYER_ENABLED` switch permits only that pilot's entries while `MEMECOIN_DESK_ENABLED=0` continues to refuse LLM desk entries. The historical master-switch table above describes the LLM desk; disabling that switch does not disable an independently enabled pilot. Global token safety must be enabled, and shared capital, duplicate, loss, sell-route and custody controls still apply. Disabling pilot admission must not stop existing exits.

Every new CROWD request freezes `history_basis=observed_tape` through reservation and the funded contract. No earlier sell means none in the observed tape, not proven absence across the wallet's lifetime; the cohort policy marks capture incomplete. This is an explicit evidence limit, not a token-safety pass. The operator's existing bounded-pilot request and delegation govern this implementation; source review does not grant additional runtime or `.env` authority.
