# EVM funded entry speed: where 4663 and 56 decide-to-send time goes, and what to cut (design)

**Author:** Beta. **Order:** Alpha, 2026-10-01 ~19:00Z, for the operator's interest in fill speed
beyond Solana: spec only, no build, with measured stage timings from traces. Verify the stage
semantics first. Every cut gets its own default-off knob and a CLAUDE.md rule-5 map, and no safety
check is skipped (spec 2026-07-28 4663 entry critical path). Gamma does the EVM bot-speed breakdown
in parallel. **Status:** historical proposal; see the dated BSC C1 amendment below.

## 2026-10-11 amendment: RH discovery preparation and receipt clocks

RRR used the Pons shared route and spent 269.358 ms deriving/proving its eventual V4 winner. The current RH change warms the existing discovery pipeline off-path, without transferring native safety PASS or changing exact approval and live quote/safety gates. It also preserves original ingress clocks and timestamps the first successful receipt already read; no additional receipt polling is granted. Confirmation observation is an upper bound on execution, while historical submission marks end at the sender reply. Exact design and evidence contract: `2026-10-11-rh-entry-speed-design.md`. Organic improvement remains unproved until post-cutover canonical witnesses exist.

## 2026-10-03 amendment: BSC attempt-local early quote implemented

### 2026-10-06 amendment: PHOOK approval reads and balance overlap

PHOOK `7a9b3c8d` spent 217 ms in approval after 591 ms of cold safety work.
BSC approvals now share signing's existing request-local gas worker: gas and nonce
reads overlap; the caller still allocates the nonce, signs and submits only after
all gates. Call count, gas memo lifetime, exact allowance and ambiguous acceptance
semantics are unchanged. Other chains retain serial approval reads.

The operator-authorized runtime activation sets the existing C3 balance overlap
for chain 56. It reads under the executor's wallet lock and joins before any
mutation; retries still read fresh. Removing 56 rolls that activation back.
Cold storage-layout discovery and Relay origin verification remain mandatory.
Public BSC read-only nonce/gas pairs measured serial p50/p90 348/409 ms versus
overlap 149/182 ms (10 pairs each); this is not organic fill-speed evidence.
Exact incident evidence and receipt fix: `docs/audits/2026-10-06-phook-plt-entry-fixes.md`.

### 2026-10-05 amendment: Base shares the existing overlap

HOOK's Base attempt exposed the BSC-only restriction on the same helper. Base now reuses the
existing attempt-local overlap, with authority, reentry, trials, caps and sizing on its resolved
chain. All exact-envelope/deadline/safety/approval checks remain unchanged. The operator-approved
runtime Base grace is zero rather than one second; a first measured within-budget hit decides
without waiting for later routes. This is a speed/route-quality trade-off, not guaranteed better
pricing. Evidence and limitations: `docs/audits/2026-10-05-base-hook-entry-speed.md`.

The BSC quote portion of C1 now runs through `memecoin_evm_early_quote.py` and the original
funded executor, beside an already-required market-data read. It reuses the existing
`MEMECOIN_ENTRY_EARLY_GATE_READS_EVM` switch rather than adding the proposed C1 shadow knob.
The ordinary safety worker and other chains are unchanged by this build.

Unlike the proposed C1 text below, a matching in-flight quote retains its original ladder
deadline and a completed typed miss is consumed without another full ladder. This avoids the
duplicate provider work the original discard-on-miss proposal would cause. Unknown sizing or
price memo, different final clip/atomic amount/slippage/custody, stale results and invalid
quotes retain ordinary fallback; every approval/signing/preflight invariant still applies.
V3 BSC delivery also uses fresh lane stats and coalesces ordinary watcher duplicates, while
explicit retries remain eligible. Required cold reads and exact quotes are not removed.

The remaining proposal, historical timings and shadow decision rule below do not describe
this narrower operator-authorized BSC build. Current contract:
`2026-10-02-memecoin-bsc-56-fast-lane-design.md` dated amendment. Evidence:
`docs/audits/2026-10-03-bsc-v3-shared-fast-lane.md`. Fixture overlap is not organic fill-speed proof.

**Instrument:** `scripts/research/evm_entry_critical_path.py` (this branch; read-only ledger, no
network, `--selftest` pins the wall arithmetic). Every number below comes from it or from the queries
named beside it. Window: the current regime only, decisions since 2026-09-28T12:30Z (see §1.3), to
2026-10-01 ~19:00Z.

## 0. Answer first

| chain | decide → broadcast accepted, p50 / p90 | n sent | the biggest walls (share of total) |
|---|---|---|---|
| 4663 | **1,851 / 4,518 ms** | 20 | ladder 30%; market data 18%; safety wait 12%; WETH unwrap pre-tx 12%; broadcast 9%; approval 7% |
| 56 | **1,327 / 1,501 ms** | 18 | ladder 42%; approval 21%; market data 16%; safety wait 6% |
| Solana (Alpha's read) | 390 / 1,500 ms | | |

- **Alpha's 7-day numbers mix two regimes.** 4663 was 4.0 / 8.2 s and 56 was 1.5 / 6.5 s. The 4663
  ladder waited a 1.5 s grace after its first hit until 2026-09-28 ~12:25Z. The 56 tail was
  receipt-first approvals: all nine slow 56 sends were on 09-25/26, at 3.5-6.9 s, with approval or
  pre-tx at 2.2-2.6 s. Since 09-28 every 56 send is ≤ 1,558 ms.
- **Gamma's parallel read agrees once windowed the same way.** Gamma had 4663 n=47, 4.0 / 6.5 s,
  quote_initial p50 1.77 s and the pre-tx pipeline 1.07 s. This script's 7-day run gives the same:
  ladder p50 1,767 / p90 3,432 ms. The "quote_initial is worth whole seconds" lever was pulled on
  09-28 (grace 0). The current-regime ladder wall is p50 384 / p90 1,586 ms.
- **The ladder is not serial.** Rungs race on threads. Since 09-28 the ladder ends at the first
  measured hit (grace 0 on 4663 and 56; ladder minus first hit ≤ 7 ms on every ladder).
- **Safety already runs beside the quote** on 4663 and 56. Now that the ladder is fast, the safety
  check outlasts it and the join waits: 4663 safety wait p50 294 ms / p90 450 ms.
- **There is no prefetch window at signal time on J7.** Observed → decision start is 2-3 ms at p50
  (p90 6 ms on 4663 J7). The on-chain lane has a tail: 56 p90 422 ms over all 439 decisions
  (on-chain rows alone: p50 453 / p90 819 ms, the watcher, C4). What can move on J7 is when the
  read-only work starts inside the decision (cut C1).
- **Off the decide-to-send clock, the 56 on-chain lane loses p50 453 / p90 819 ms** before the
  decision starts, in the watcher's receipt read (cut C4). 4663: 63 / 103 ms.

Proposed cuts, by measured size:

| cut | chain | what it removes (measured, current regime) | knob (default off) | build? |
|---|---|---|---|---|
| C1 early read-only start (ladder + safety at the cheap-gate point), shadow first | 4663, 56 | the market-data and gate walls that sit before the ladder, plus the 4663 safety wait. Estimate 0.2-0.5 s p50 until the shadow measures it | `MEMECOIN_EVM_EARLY_READS` | yes, shadow first |
| C2 native float refill | 4663 | the confirmed WETH unwrap: 6 of 20 sends, p50 976 ms each | existing `MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE` | no build: operator sets the float |
| C3 balance reads beside the ladder | 4663, 56 | `balance_pre` p50 31 ms, p90 50-64 ms (4663 max 325) | `MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS` | small |
| C4 56 watcher receipt read | 56 | watcher decode p50 504 / p90 866 ms (4663: 134 / 155) | `MEMECOIN_ONCHAIN_EVM_RECEIPT_WAIT_CHAINS` | yes |
| C5 standing one-clip quote-token allowance | 4663, 56 | the approval acceptance: 4663 p50 210 ms; 56 p50 240 / p90 491 ms | not proposed without an operator ruling | operator decision |

Note only: broadcast acceptance on 4663 is ~200 ms. That is the Robinhood sequencer's round trip
(the 07-28 spec measured ~0.22 s), and it cannot be cut from our side. The 4663 ladder tail is set by
kyber (p50 1.8 s) and v4_multihop (p90 1.26 s) when v4 misses. The store-first multihop knob already
waits on the operator (spec 2026-09-27 §4). Base 8453 has n=1 and Arc 5042 n=0 in this regime, so
they are noted only (§6).

## 1. Stage semantics, verified

### 1.1 The stage columns cannot be summed or read as the path

`memecoin_entry_attempts.*_ms` comes from `EntryAttemptTrace.mark` / `mark_submission`
(`sniper/services/memecoin_entry_trace.py`):
- A `mark()` without an explicit duration records the **gap since the previous stage mark**. Only
  `reservation` and `safety_checks` pass an explicit duration on the EVM path.
- `safety_checks_ms` is the **safety worker's own duration** (`memecoin_evm_leg._mark_safety`
  passes it explicitly). The worker starts before the reservation and overlaps the reservation and
  the quote ladder. It is not a wall on the decision thread. Its mark also advances the gap clock,
  so the time from the reservation mark to the safety mark is in **no** column.
- `quote_ms`, `signing_ms`, `transaction_submitted_ms` and `chain_confirmation_ms` are projected to
  the **latest** submission attempt. On a min-out retry, `quote_ms` is the retry's gap and the first
  ladder vanishes.
- Worked example, 4663 fill `cb783f95` (2026-10-01T15:33Z):
  - the columns read quote 1,741, safety 1,400, signing 360 and submitted 188 ms;
  - the path was: ladder 1,869 ms (kyber), refused preflight, winner retry 1,704 ms, approval 294 ms,
    broadcast 188 ms, total 4,484 ms;
  - the safety's 1,400 ms overlapped the ladder and cost 0 ms of wait.

So Alpha's stage p50s (quote 1.8 s, safety 1.0 s, sign 0.38 s) are column reads. They are not
additive and do not locate the wall.

### 1.2 What the instrument does instead

Every substage event carries `elapsed_ms` from one `perf_counter` anchored at the attempt start. The
EVM executor and the hook run on the decision thread, so consecutive substage marks partition that
thread's wall time. The script attributes each wall to the mark that ends it, up to the first
`evm.broadcast`. The walls add up to the broadcast's elapsed time exactly (the selftest asserts it).
Explicit-duration marks on other threads (`evm.token_safety`, `evm.quote_initial`) are read twice:
their wall is the wait on the decision thread, and their own duration is reported separately.

The denominator is the FIRST attempt per `origin_event_id` (trap e3a2c165), manual entries excluded.
"Sent" means the attempt reached `evm.broadcast`.

### 1.3 The regime boundary

- **4663 grace.** The ladder overran its first measured hit by 1.5 s until the last such ladder at
  2026-09-28T11:13:29Z. From 12:25:43Z the overrun is ≤ 7 ms. That matches
  `MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_4663=0` and `_56=0` in `.env`.
- **56 approvals.** Every `evm.approval` on 56 since 09-28 has `outcome=broadcast` (pipelined),
  with `MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS=4663,56`.
- The window starts at 2026-09-28T12:30Z.

## 2. Measured path, current regime

### 2.1 4663 (n=20 sent of 982 first attempts; 11 on-chain lane, 9 J7)

| wall (adds up to the total) | p50 | p90 | mean | share |
|---|---|---|---|---|
| ladder (first quote, all rungs racing) | 384 | 1,586 | 735 | 29.8% |
| market data, caller GET (J7 events without a source mcap) | 2 | 658 | 323 | 13.1% |
| market data, hook `dex_pair_selection` (events with a source mcap) | 92 | 245 | 110 | 4.5% |
| safety wait (join; own duration p50 672 / p90 1,480) | 294 | 450 | 297 | 12.1% |
| WETH unwrap pre-tx, confirmed before the swap (6 of 20) | 0 | 1,051 | 297 | 12.1% |
| broadcast acceptance | 203 | 231 | 213 | 8.7% |
| approval acceptance (pipelined, 14 of 20) | 210 | 291 | 171 | 7.0% |
| rework (1 min-out winner retry, 1,704 ms) | 0 | 0 | 88 | 3.6% |
| everything else (balance_pre 31, preflight 28, volume 18, gate balance 14, signing 4, misc) | | | | ~9% |

- **Ladder:** first ladder p50 271 / p90 1,529 ms (n=39 ladders). Winners: v4 21, v4_multihop 7,
  pons_v2 6, kyber 4, v2 1. Rungs answer at:

  | rung | p50 | p90 |
  |---|---|---|
  | launchpad_curve | 92 | 156 |
  | v3 | 116 | 242 |
  | pons_v2 | 119 | 214 |
  | v4 | 269 | 549 |
  | v2 | 395 | 456 |
  | v4_multihop | 665 | 1,264 |
  | kyber | 1,777 | 1,989 |

  These rung figures are from a run at 2026-10-01T19:13Z on the same 39 ladders; an earlier draft
  printed figures from a pre-relabel run (Gamma LOW 3).

  Grace 0 cuts slower rungs at the first measured hit.
- **Safety components (own time, p50):** sell quote 511 ms, slot discovery 84 ms (the proof is
  already persisted per (chain, mint, spender), `memecoin_evm_leg.load_slot_proof`), curve state
  46 ms, code 24 ms, simulation 24 ms. The sell quote is the same venue ladder run in the SELL
  direction (`evm_token_safety.build_evm_token_safety(quote_fn=ex["venue_quote_fn"])`).
- **Market-data tail.** The caller GET is `stats_fn` in `memecoin_real_runner._hook_raw` (the
  live-mcap fallback). Its wall burns 1-5 ms of CPU against 200-3,583 ms of wall, so the thread
  waits on I/O: 3,583, 679, 656, 352 and 308 ms on the five slowest rows.

### 2.2 56 (n=18 sent of 439 first attempts; 12 on-chain lane, 6 J7)

| wall | p50 | p90 | mean | share |
|---|---|---|---|---|
| ladder | 585 | 625 | 551 | 42.3% |
| approval acceptance (pipelined, 18 of 18) | 240 | 491 | 270 | 20.8% |
| market data (hook 194 p50; caller p90 265) | | | 203 | 15.5% |
| safety wait (own duration p50 648 / p90 806) | 67 | 170 | 76 | 5.8% |
| gate balance read | 38 | 93 | 43 | 3.3% |
| balance_pre, broadcast (36), preflight (28), signing (3), other | | | | ~12% |

- **Ladder:** p50 589 / p90 727 ms. Winners: v2 14, mixed_v3_v2 5, v3 1. v2 answers at p50 580 ms,
  which is slow for a V2 read. Its rung records carry no `calls`/`http` tap, so the time cannot be
  split from the trace (§5, gap G1).
- **BSC p90 tails: no tail remains in this regime.** The max is 1,558 ms. The 6.5 s p90 came from
  the 09-25/26 receipt-first approvals (2.2-2.6 s) and pre-tx waits (2.2-2.3 s).

### 2.3 Before the decision: the on-chain lane (not in decide-to-send, but in fill speed)

Watcher stamps (`onchain_wallet_detections.source_valuation_json`: `decoded_at`, `commit_at`) are
joined on the exact signature:

| chain | arrival → decoded | arrival → decision start | why |
|---|---|---|---|
| 4663 | 134 / 155 ms | 63 / 103 ms | tx + receipt read 50-70 ms on the tx host |
| 56 | **504 / 866 ms** | **453 / 819 ms** | the first receipt read returns null: 1,119 `null read retry` lines in `logs/onchain_evm_armed.56.log` against 75 detections. Reads take 120-200 ms each on the 56 read host, plus a 0.1 s step (`MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S=56:0.1`) |

The watcher and the runner stamp from two processes. Commit → decision reads −50 ms on every row,
so their corrected clocks differ by ~50 ms. Differences under 100 ms across the two are noise.

### 2.4 Same-mint repeats (the prefetch question)

520 of 982 4663 decisions (53%) and 215 of 439 56 decisions (49%) had the same mint decided on that
chain within the previous 10 minutes. Under the 07-28 invariant (no price, balance, quote or
token-safety cache on the funded path), only immutable facts may be reused. They are already reused
or cheap:
- the slot proof is persisted;
- decimals and code are read in 21-24 ms;
- V4 pool keys have the store-first path.

The large per-mint costs (market data, ladder, sell-quote safety) are dynamic state. They can be
moved earlier (C1) but not cached.

## 3. Alpha's questions

1. **Is the 4663 ladder serial? Can the rungs race?** They race already. `venue_quote_with_v2_fallback`
   (`sniper/execution/evm_real_seams.py`) submits every rung to a ThreadPoolExecutor under one ladder
   deadline (`MEMECOIN_EVM_LADDER_WAIT_S=8`). On buys it takes the best measured round trip among the
   rungs answered when the per-chain grace ends, and the grace is 0 on 4663/56. What is serial is the
   ladder's position in the path: it starts only after market data, the gates and the reservation (C1).
2. **Can the safety sim run beside the quote instead of before it?** It already does on 4663 and 56
   (`parallel` mode, `memecoin_evm_leg.evm_real_entry`: a thread started before the reservation,
   joined by `safety_gate_fn` before the first wallet mutation). What costs time now is that it
   starts at the same point as the ladder and runs longer (672 vs 271 ms p50). Starting it earlier is
   C1.
   - Rejected: feeding the safety the buy ladder's winner reverse quote instead of its own sell
     ladder. That makes the safety wait for the ladder: ladder 271 + slot/sim ~110 ≈ 380 ms after
     the ladder starts, against today's 672 ms in parallel, so it gains at the p50 only if the sell
     quote's executable transaction is equivalent, which nobody has verified. It also ties a safety
     verdict to a buy-side quote. Not proposed.
3. **What can be prefetched per mint at signal time?** Nothing at signal time on J7: the signal
   reaches the decision in 2-3 ms at p50. The 56 on-chain tail (p90 422 ms over all decisions) is the
   watcher's receipt read, which C4 addresses. Immutable per-mint facts are already persisted or cheap (§2.4). The
   prefetch that pays is moving the read-only ladder and safety to the cheap-gate point (C1), in
   shadow first.
4. **Where do the BSC p90 tails come from?** In the 7-day read: receipt-first approvals and pre-tx
   waits on 09-25/26, gone since 09-28 (§2.2). In the current regime decide-to-send has no tail. The
   remaining 56 loss is before the decision: the watcher's null first receipt read (C4).

## 4. The cuts

Every cut below is read-only or wallet-shape only. None removes or weakens a gate. Token safety,
price gates, post-peak confirmation, entry preflight, exact approval, write-ahead identity and
balance-delta accounting all still run, in order, on the quote that is signed. Each knob is
default off, and its activation is the operator's.

### C1. Early read-only start (ladder + safety), shadow first

**What.** At the cheap-gate point in `memecoin_real_runner._hook_raw`, start the buy ladder and the
token-safety worker on their own threads, beside the market-data GET and the hook gates instead of
after them. The cheap-gate point is after identity, route-open, source-identified and staleness, and
before the `stats_fn` GET. Both are read-only.

**Size, sent attempts only.** The ladder currently starts after these walls:
- 4663: market data p50 ~92-200 ms (tail 3.6 s) plus reservation and gates;
- 56: market data p50 ~194 ms plus the gate balance read 38 ms.

Overlapping them with the ladder saves min(those walls, ladder) per entry. Starting safety at the
same earlier point removes most of the 4663 safety wait (294 ms p50).

**This is an estimate,** and it is smaller in the tail. When the pre-ladder walls exceed
`MEMECOIN_EVM_PREFLIGHT_MAX_QUOTE_AGE_MS` (750 ms; the market-data tail reaches 3.6 s), the early
quote is too old at preflight and `evm.winner_refresh` re-asks one rung. The saving there is the
ladder minus that one-rung refresh, not the whole wall (Gamma LOW 2). The shadow below measures the
saving before any live use.

**Exactness (the reason for the shadow).** The executor uses the early quote only when ALL of these
hold:
- it was asked for the same atomic sell amount the final sizing produced;
- same quote token, same chain, same mint;
- it is a measured hit, and its spender and route are still the ones the approval will cover;
- its age at preflight is within `MEMECOIN_EVM_PREFLIGHT_MAX_QUOTE_AGE_MS` (750 ms default). An
  older one goes through today's `evm.winner_refresh`, one rung.

Otherwise it is discarded and today's ladder runs. The early safety verdict is used only if it was
computed with the same `usd=trade_size` as the final clip. Otherwise the worker reruns exactly as
today.

The clip is sized from the bankroll at reservation: real 4663/56 entries since 09-28 range $28-$117.
So the early amount (from the early balance read `_start_entry_prereads` already starts) can differ.
The shadow counts how often it matches.

**Knob.** `MEMECOIN_EVM_EARLY_READS=<chain>:<mode>[,...]`, mode `shadow` or `live`, default empty.
- **shadow** starts the reads, never hands them to the executor, and records one trace substage
  `evm.early_reads` per attempt: match or mismatch with the reason, the would-have-saved wall, ladder
  and safety start/end, and the RPC call count.
- **The shadow is not free, so it runs on half the decisions (Gamma MEDIUM).** It is a second ladder
  and a second safety beside the live ones: the same funded RPC hosts, the same third-party APIs
  (kyber, relay) and the same executor threads.
  - **Assignment:** the shadow runs only when sha256(attempt_id) is odd. The other half is the
    control.
  - **Read:** the live ladder wall and the safety wait, p50/p90, shadow half against control half.
  - **Stop at once (operator or Alpha flips the knob off)** if the shadow half's live ladder or
    safety wait is more than 10 ms slower at p90, or if the funded RPC call counter (d83c8b63e)
    shows any rise in 429s while the shadow runs.
- **live** hands a matching result to the executor.

**Read at:** 30 sent entries per chain in shadow. Ship `live` only if the match rate and the saved
wall are positive. Gamma grades it.

**Rule-5 map:**
1. **Money path.** Yes: it feeds the entry quote. Nothing signed changes. Every gate reruns on the
   handed-over quote, and a mismatch falls back to today.
2. **Speed on the fill path.** It adds no synchronous call. Starting threads costs < 1 ms (to be
   measured as p50/p90 in shadow).
   - Extra read load lands on refused decisions: since 09-28, 126 4663 and 72 56 decisions passed
     the cheap gates and were then refused (~40/day and ~22/day). Each spends one ladder and one
     safety run.
   - On 4663, kyber and relay are third-party APIs, so the extra ladders count against their rate
     limits. The shadow records the call count through the funded RPC call counter (d83c8b63e).
   - Contention with the live ladder is measured by the half-split above, not assumed. Thread start
     alone is not the cost.
3. **Concurrency.** The ladder and safety already run on worker threads with per-future RPC taps
   (`evm_real_seams._run`), so no shared state is added.
   - The balance reads stay inside the wallet lock (C3 owns them). C1 must not move the pre-trade
     balance read before the lock, or an exit between the read and the swap corrupts the
     whole-wallet fill delta.
   - The early results live in a per-attempt holder, never module state.
4. **Absent vs zero vs unknown.** A missing early result means "use today's path", never "no
   route". An early ladder miss is not a decision: the executor reruns the ladder.
5. **Dependency failures.** An early read that times out, raises or is cut by the deadline is
   discarded. The executor's own ladder runs with its own deadline, so a hung early read cannot
   block the decision. Nothing gives the exit sweep priority over these reads; rate limits are
   shared. The protection is the 429 stop in the shadow and the half-split read (Gamma LOW 1).
6. **Units and identity.** Atomic sell amount, quote-token decimals, (network_id, mint), spender
   address and route identity must match exactly. No USD tolerance.
7. **Sample.** Sent entries only. A refused decision's saved wall is zero by definition, and its
   cost is counted separately.
8. **Rollback.** Remove the chain from the knob. While half-applied (shadow), nothing reaches the
   executor.

**Check left behind:** a test that a mismatched early quote (amount off by 1 atomic unit) is never
handed to the executor, proved red against a mutant that compares USD instead of atomic.

### C2. 4663 native float refill (existing knob, no build)

**What.** 6 of 20 4663 sends bought through a native-ETH V4 pool with `native_float=unwrap_kept`.
The executor then sends a WETH unwrap pre-transaction and waits for it to CONFIRM (667-1,217 ms,
p50 976). The swap cannot spend unmined ETH (attempt 7204b30c, 2026-09-03). The existing native
float refill keeps a native ETH float, so the entry skips the unwrap. It is
`MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE`, default 0 = off (spec 2026-09-28 Option B, gas pass).

**Size.** −976 ms on the ~30% of 4663 entries that unwrap. Mean −297 ms per send.

**Operator decision:** the float size. It holds that much of the 4663 bankroll as native ETH instead
of WETH (the same asset). The gas pass moves it.

**Rule-5 map:**
1. **Money path.** Wallet composition only.
2. **Speed.** Removes a confirmed pre-tx and adds nothing.
3. **Concurrency.** The gas pass runs in the runner's own pass, and its reservation and lock rules
   are unchanged.
4. **Absent/zero/unknown.** A float below the swap's need keeps today's unwrap.
5. **Dependency failures.** The refill is a wrap/unwrap transaction in the gas pass and never on
   the entry path.
6. **Units.** Wei, the native/WETH 1:1 pair.
7. **Sample.** n=6 unwraps in the regime.
8. **Rollback.** Set the knob back to 0.

**Check:** already covered by the gas-pass tests; read the next 10 4663 V4 native entries'
`evm.native_float` outcome after activation.

### C3. Balance reads beside the ladder

**What.** `evm.balance_pre` (USDC/WETH and token balances plus decimals) runs after the ladder
returns, serially: 4663 p50 31 / p90 50 ms (max 325), 56 p50 31 / p90 64 ms. Start it at executor
entry, beside the ladder, inside the wallet lock, as Solana did in 0df2e7ee. Join it where it is
read today.

**Knob.** `MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS`, default empty.

**Rule-5 map:**
1. **Money path.** Yes: the pre-trade balance anchors the fill delta. It is read under the same lock
   and before the first mutation, as today.
2. **Speed.** −31 ms p50, and it adds no call.
3. **Concurrency.** Two threads already read in `_read_balances`; this starts them earlier. The
   min-out retry re-reads them as today.
4. **Absent/zero/unknown.** Unknown decimals still abort (`token_decimals_unavailable`).
5. **Dependency failures.** Unchanged readers.
6. **Units.** Atomic.
7. **Sample.** All sends.
8. **Rollback.** Remove the chain.

**Check:** a test that the balance join happens before `approve_fn`, red on a mutant that joins
after it.

### C4. 56 watcher: read the receipt where the block already is

**What.** The 56 push socket delivers the Transfer at p50 0.13 s after the block, while the read host
shows it at p50 0.28 s (spec 2026-09-29 null-read step). The first receipt read is null, and one
retry follows after 0.1 s on a 120-200 ms host. Two options, the first preferred:
- **(a)** Do the first tx/receipt read only once the read host's head (from a `newHeads`
  subscription on that host) reaches the log's block. This waits exactly as long as needed, with no
  wasted null read.
- **(b)** Read the receipt from the push host itself.

**Size.** arrival → decoded p50 504 → an estimated ~200-300 ms (one read after the head, no retry).
Measure it on the watcher's existing per-row stamps.

**Knob.** `MEMECOIN_ONCHAIN_EVM_RECEIPT_WAIT_CHAINS`, default empty. The current null-read retry
stays as the fallback.

**Rule-5 map:**
1. **Money path.** Upstream of the decision. It changes when the detection row lands, not what it
   holds.
2. **Speed.** Watcher process only, not the funded thread.
3. **Concurrency.** The watcher's own loop. The head subscription is one more socket on one host.
4. **Absent/zero/unknown.** No head event within the budget means today's retry path, never a
   dropped transfer: `outcome` still records why.
5. **Dependency failures.** A head stream gap or reconnect falls back to the null-read retry.
6. **Units.** Block numbers in hex vs int. Compare as int.
7. **Sample.** 56 on-chain detections.
8. **Rollback.** Remove the chain.

**Host rule.** Check rule 0724bc08 (which hosts may serve 56 reads) before choosing (a)'s host. If
no permitted host has `newHeads`, C4 waits for the operator's BSC provisioning (decision 46d75aed).

### C5. Standing one-clip allowance (operator decision, not proposed for build)

The approval acceptance costs 4663 p50 210 ms and 56 p50 240 / p90 491 ms on every entry that
needs one: 14 of 20 on 4663, 18 of 18 on 56. Approving the NEXT entry's quote-token clip right
after each entry, an exact amount but standing, would remove it.

That leaves a one-clip allowance open between entries. It touches the 07-28 "exact-amount
approval" invariant and the operator's refusal of MAX approval (43aed9f2), so it is listed for a
ruling only. If ruled in, it gets its own spec, with a rule-5 map led by the allowance exposure.

### Interplay with approve-at-entry (`MEMECOIN_EVM_PREAPPROVE_CHAINS=4663`, live since 2026-09-30)

- **What it is.** After a 4663 V4 entry books, `evm_real_seams._preapprove_position` approves the
  BOUGHT token for Permit2 on a daemon thread. It uses the executor's own `_approve`, so it shares
  the nonce floor and the signing lock. It serves the exit and does not touch the entry's
  quote-token approval, which is the 210 ms wall above.
- **Measured overlap.** 8 pre-approvals so far (1 accepted, the rest `skipped_route` and similar)
  against 8 sends since activation. No entry started within 10 s of a pre-approval, so it has cost
  the entry path nothing yet. If entries cluster, a next entry's approval and swap can wait on the
  shared lock or nonce floor behind a pre-approval in flight.
- **For C1.** No interplay: read-only.
- **For C2.** The pre-approval pays gas in native ETH, and the float refill covers both.
- **For C3.** No interplay.
- **For C5, if ever ruled in.** It would ride the same `booked_fn` daemon and the same nonce
  floor. Two approvals per booking then queue on one lock, and its spec must measure the exit-side
  regression too.

## 5. Measurement gaps

- **G1.** The 56 v2 rung (p50 580 ms, the winner on 14 of 20) records no `calls` or `http` tap, so its
  time cannot be split into pair lookup, reserves and round trip. Add the tap first (instrument
  only), then decide whether batching its reads pays.
- **G2.** The stage columns stay misleading (§1.1). This spec does not change them. Any p50 quoted
  from `*_ms` columns for EVM should cite this script instead.
- **G3.** n is small (20 and 18 sends). Every estimate here is re-read at 30 sends per chain after
  any cut.

## 6. Base 8453 and Arc 5042 (note only)

8453 has 1 send since 09-28 (3,791 ms: ladder 1,778 with kyber the winner, post-approval requote
776, safety sell quote 2,029). Base stays receipt-first by the 07-28 spec. Arc 5042 has no sends in
the regime (2 before it, ~1.3 s). Neither is sized here.

## 7. Order of work if Alpha and the operator approve

1. C2: an operator setting, no code.
2. G1 instrument, then C4 (watcher only, off the funded thread).
3. C3 (small, funded executor) with Gamma review.
4. C1 in shadow. Read at 30 sends per chain. Live only on Gamma's grade and the operator's yes.
5. C5 only on an operator ruling.

### Amendment 2026-10-10: bounded reuse and route batching

`memecoin_evm_early_quote.py::EarlySafety` now records monotonic completion before publishing
its Event. Only a verdict completed by its original deadline may be reused, including when the
consumer arrives later; an expired/unknown PASS falls through to existing fresh safety. No new
wait or provider call. Shared V2 searches batch unchanged paths with strict integer reply IDs;
unsupported capability uses existing per-endpoint read-pool TTL, and unavailable batches retain
typed provider truth. Flap nested safety reads share one fresh check-local copied state, preserving
exact simulations and per-attempt rereads. Evidence and overlapping-stage interpretation:
`docs/audits/2026-10-10-bsc-entry-pipeline.md`. Organic latency improvement remains unproved.
