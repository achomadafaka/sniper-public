# Slot lane prefetch cadence: fewer funded Solana reads, same fill path

Status: PROPOSED, 2026-10-01 (Lambda, on Alpha's order; revised for Gamma's review of 68bd8915a).
Two steps:

- **Step 1, telemetry, built on a branch:** `lambda/prefetch-telemetry` 58c878a5a. Deploy goes to
  the operator.
- **Step 2, cadence:** spec only, until 24 h of step-1 data exist. Its activation is the operator's
  decision.

The 2 s cadence does not change in step 1.

## 1. What the counter shows

From the heartbeat counter (`solana_rpc_pool.calls_by_label`, live since the 10:34:04Z deploy of
d83c8b63e), read at 10:56:53Z, 22.8 minutes in:

| label | getTokenAccountsByOwner | getBalance | getLatestBlockhash | total |
|---|---|---|---|---|
| `thread:slot-lane-prefetch` | 572 | 572 | 572 | 1,716 |
| all other labels | | | | 382 |

That is 25.1 calls a minute of each method and 75 in all: 82% of funded Solana pool calls, about
108,000 a day. Gamma's first minutes read ~17 a minute of each; Gamma's hourly rate replaces this
window when quoted.

The lane decided 194 buys on 2026-09-29 and 187 on 2026-09-30, and filled 25 from 2026-09-29T04:17Z
to 2026-10-01 (`logs/memecoin_runner.log`). That is ~570 prefetch calls per lane decision.

## 2. Trace

**Who calls.** `LanePrefetcher.refresh_once` (`sniper/services/memecoin_slot_lane.py:1170` on
d83c8b63e) on the `slot-lane-prefetch` thread. `prefetch_loop` (`:1427`) runs it, then sleeps
0.5 s, forever.

**Cadence and trigger.** A fixed timer, `PERIOD_S` (`:1148`):

- `sol_usd`: 10 s;
- `balances`: 2 s (`usdc_fn` = `getTokenAccountsByOwner`, `sol_fn` = `getBalance`);
- `blockhash`: 2 s (`getLatestBlockhash`).

Nothing in it depends on an armed wallet, a slot or a buy.

**The same thread does more than pool reads** (Gamma M3). Every pass also runs:

- the `sol_usd` Jupiter quote (every 10 s);
- `extra_refresh_fn`, which re-quotes the Jupiter legs (each venue at most every 1.5 s);
- the roster refresh (every 10 s).

A slow Jupiter call ages the balances and the blockhash exactly as a slow pool read does.

**It runs when no Solana entry is possible.** No check of the armed roster, the kill switch, a full
book, the lane mode, or whether the balance covers a clip. The roster refresh feeds admission and
the socket's owner list only.

**What each read feeds on the fill path** (`SlotLane.handle`, `:395-575`). The path makes no network
read.

- `stale_input` (`:104`) refuses when an input is older than `MAX_AGE_S` (`:60`): balances 10 s,
  blockhash 20 s, sol_usd 30 s. The J7 or confirmed path then sees the same buy about a second
  later, so a stale refusal costs the lane's speed. The entry itself is not necessarily lost.
- Balances go to `reserve_entry`. Free cash is `usdc_balance - pending USDC`
  (`sniper/services/memecoin_risk.py:328`); the SOL-funding route checks `sol_balance`; the
  prefetched leg is sized from the same snapshot.
- The blockhash signs every lane transaction. It is valid ~60 s, and an older one never makes a
  fill slower.

**How a funded read fails** (Gamma M2).

- Reads go through `PooledHttpClient` (`memecoin_real_runner.py:1044`). Each attempt has a 1.5 s
  read timeout (`MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS`, default 1,500, not set in `.env`) and 3 s
  connect, write and pool timeouts (`fast_http.py:46`).
- The pool moves to the next of its 5 members on a transport error, a 429 or a 5xx. A read-timeout
  outage therefore costs ~7.5 s per call, and ~15 s if connects time out.
- One balances refresh is 2 sequential calls; the blockhash is a third.
- A JSON-RPC error inside HTTP 200 (`-32603 account index service overloaded`) does not rotate. It
  fails at once.

**Failures and stale refusals at today's 2 s** (Gamma M1; the old reading of 0 failures was blind).

- The pool inputs do not raise when they fail, so the prefetcher's own warning never fired:
  - `usdc_fn` returns None and logs `usdc balance read failed`;
  - `latest_blockhash` returns None and logs `slot lane blockhash read failed`;
  - `sol_fn` returns `NativeSolBalanceRead(ok=False)` and logs nothing at all.
- `logs/memecoin_runner.log-20260929T001701`: 44 blockhash failures (unstamped) and 64 USDC
  failures. 45 of the USDC failures fall in 2026-09-28T21:18-21:32Z, a ReadTimeout outage.
- Since 2026-09-29T04:17Z: 2 USDC failures, at 2026-09-30T05:36:50Z and 05:37:25Z, both `-32603`.
- SOL failures: never recorded.
- **6 `slot_lane_stale_*` refusals at today's 2 s:**
  - balances at 2026-09-26T21:44:12Z, 21:45:28Z and 2026-09-27T12:53:49Z;
  - sol_usd at 2026-09-27T12:32:55Z, 12:55:33Z and 13:17:05Z.

  The thread has already gone past the balance gate three times at today's period.

## 3. Proposal

### Step 1: telemetry, built (58c878a5a, branch only)

On the prefetch thread only. It never raises, takes no new lock, and nothing changes on the lane
thread or the send path. The snapshot values, the stamps and the 2 s cadence are unchanged.

- **Every failed input logs one stamped line**, the SOL read included:
  `slot lane prefetch read failed name=<input> ms=<read ms> reason=<why>`. The reason is the
  exception class, the SOL read's failure code, `none` or `invalid`.
- **The whole loop is timed** (`loop_once`): the roster refresh when due, the sol_usd quote, the
  usdc, sol and blockhash reads, and the Jupiter leg refresh (`extra`). A pass over 2 s logs
  `slot lane prefetch slow loop work_ms=<n>` with every phase.
- **Peak age per input:** the gap between two good refreshes, or the current age while an input is
  still failing. It is the oldest value the lane could have read.
- **One report line every 10 minutes:** loops, work p50/p90/p99/max, the maximum of each phase, peak
  ages, and failure counts.
- **Measured overhead** per prefetch pass with every input due: +1.9 µs p50, +2.5 µs p90 (base
  3.40 / 14.54 µs). The report costs 21 µs per 10 minutes. On the lane thread and the send path the
  cost is zero.

### Step 2: cadence (spec only, after 24 h of step-1 data)

**P1. Two cadence knobs, default today's 2 s.**

- `MEMECOIN_SLOT_LANE_PREFETCH_BALANCES_S` and `MEMECOIN_SLOT_LANE_PREFETCH_BLOCKHASH_S`.
- Candidate pairs (balances / blockhash): 4 s / 8 s or 5 s / 10 s.
- Settings refuse balances above 7 s and blockhash above 15 s at startup. `MAX_AGE_S` does not
  change.
- Selection rule from step-1 data: take e, the 24 h p99 of `age_max_s - 2` per input, i.e. how far
  past today's period the age reaches. Pick the largest candidate P with P + e at least 2 s under
  that input's gate. If neither candidate passes, the input stays at 2 s.

**P2. Re-read balances when anything that moves them without our read has changed.**

- **Change key:** the Solana pending totals (count, USDC and SOL funding:
  `m.pending_totals`, `m.pending_funding_totals`, `sniper/persistence/db_memecoin.py:11199`,
  `:11224`), plus the withdraw intent state (`m.unresolved_withdraws` and the latest resolved
  withdraw).
- **Order (Gamma M4).** Each loop reads the change key BEFORE the balance read, and stores that
  pre-read key as the base. A confirmation that lands between the two reads then differs from the
  base on the next loop, and forces a re-read. Read after the balances, the same confirmation would
  be stored as already seen, and free cash would stay overstated for a full period.
- **A failed key read (Gamma L6)** does not count as "changed" on every loop. It drops balances
  back to today's 2 s period until the key reads again.
- **Cost:** `pending_totals` plus `pending_funding_totals` measure p50 0.14 ms, p90 0.17 ms, max
  2.3 ms on the covering index (Gamma, read-only).
- **Coverage (Gamma).** `reserve_pending` (`db_memecoin.py:10147`) is the only writer of pending
  rows. Its Solana callers are `memecoin_risk.reserve_entry`, which is reached from
  `real_leg.real_entry` (main, follow, on-chain and manual lanes), and the slot lane (`:555`,
  `:565`).

**Expected effect.**

| read | calls per minute now | 4 s / 8 s | 5 s / 10 s |
|---|---|---|---|
| balances (2 methods) | ~50 | ~27 | ~22 |
| blockhash | ~25 | ~7 | ~5.5 |
| total | ~75 | ~34 (-54%) | ~28 (-63%) |

P2 adds about 4 reads per entry (reserve and confirm), plus one per resolved withdraw.

**Not proposed.**

- *Pausing while no entry is possible.* The idle share of the day is not measured, and after a
  close the first buy would meet stale inputs.
- *WebSocket `accountSubscribe`.* A new dependency; a silent socket would show a stale value as
  fresh.

## 4. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** Balances size the clip, gate free cash and pick the SOL route; the
   blockhash signs every lane transaction. Steps 1 and 2 change only telemetry and the read
   timing. Every value is still judged against the same `MAX_AGE_S`.
2. **Speed on the fill path.** No read, lock or retry is added to the path.
   - Step 1 costs +1.9 µs p50 per prefetch pass, on the prefetch thread.
   - After step 2, check lane `lane_ms` p50/p90 from the trace before and after.
3. **Concurrency and global state.**
   - The prefetch thread is the only writer of the snapshot and of the telemetry.
   - Peak age is computed outside the snapshot lock.
   - The step-2 knobs are read once at start.
   - P2 reads on the prefetch thread's own read-only connection, which ends its read transaction
     every loop (`ro.rollback()`).
4. **Absent versus zero versus unknown.**
   - A failed read keeps the old value and stamp, and ages out (unchanged).
   - A balance of 0 is a valid read, not a failure.
   - Step 1 names the failure reason: `none` (no value), `invalid` (negative or non-finite), the
     exception class, or the SOL read's code.
   - P2: an unread change key is unknown, so balances go back to 2 s. Unknown is never taken as
     "unchanged".
5. **Dependency failures and margin.**
   - **Retry.** A failed read retries on the next loop, not at the next period: `_due` advances
     only on success.
   - **Fast failures.** Gamma's reading of the 2.39 s effective period gives a loop of ~0.8-1.2 s,
     L ≈ 0.3-0.7 s; the table uses L ≈ 1 s, which is conservative. Worst-case age is P + L + k·L:

     | input (gate) | P | 0 failures | 1 | 2 |
     |---|---|---|---|---|
     | balances (10 s) | 2 s (today) | ~3 s | ~4 s | ~5 s |
     | balances (10 s) | 4 s | ~5 s | ~6 s | ~7 s |
     | balances (10 s) | 5 s | ~6 s | ~7 s | ~8 s |
     | blockhash (20 s) | 2 s (today) | ~3 s | ~4 s | ~5 s |
     | blockhash (20 s) | 8 s | ~9 s | ~10 s | ~11 s |
     | blockhash (20 s) | 10 s | ~11 s | ~12 s | ~13 s |

   - **Slow failures** (Gamma M2 figures). A pass stalls by ~7.5 s for each call that times out on
     all 5 members, and a balances refresh makes 2 calls. How long one stall can last before the
     lane refuses:

     | input | today | 4 s / 8 s | 5 s / 10 s |
     |---|---|---|---|
     | balances | ~7 s | ~5 s | ~4 s |
     | blockhash | ~17 s | ~11 s | ~9 s |

     A full 5-member timeout on one balance call (~7.5 s) already exceeds today's balance margin.
     That is consistent with the 3 balance refusals on 09-26/27. A slow Jupiter call stalls the
     same way. Step 1 measures the real distribution of both.
   - **What a refusal costs.** The J7/confirmed path sees the buy ~1 s later: a slower fill.
6. **Units and identity.**
   - Durations are milliseconds and ages are seconds, both on `time.monotonic()`.
   - Blockhash validity is block height; `last_valid_height` is unchanged.
   - Balances are UI units.
7. **The sample.**
   - The call rate is the process-lifetime counter over one 22.8-minute window.
   - Failure and refusal counts come from two runner logs (2026-09-15 onward). The blockhash
     failures there are unstamped, which step 1 fixes.
8. **Rollback.**
   - Step 1: revert the commit; it has no knob, like the call counter.
   - Step 2: unset both knobs and restart. Half-applied (one knob) is safe.
   - Log volume from step 1: failure lines (108 in the 2026-09-15..29 log: 64 USDC, 44 blockhash), slow-loop lines, and 144
     report lines a day.

**Balances that move without a pending row** (Gamma L5). The post-confirm overstatement is not the
only harmful staleness.

- **(i) A withdrawal reaching sent.** The lane refuses only while a withdrawal is unresolved
  (`memecoin_slot_lane.py:441`). After it resolves, the balance overstates by the withdrawn amount
  for up to P. This is the largest case, and the reason the withdraw state is in P2's change key.
- **(ii) The Solana gas top-up** (`memecoin_gas_topup.py`): a fixed small USDC-to-SOL swap that
  writes no pending row. USDC overstates by the top-up for up to P. Each top-up is small and
  bounded by its daily cap.
- **(iii) Exit, de-risk and failed-transaction fees:** SOL overstates by one fee each, bounded by
  the gas floor.

## 5. Checks

Step 1 (in 58c878a5a, `tests/test_memecoin_slot_lane_prefetch_telemetry.py`, 7 tests). Each of
these mutants fails at least one test:

- the SOL failure reason dropped;
- the SOL failure not logged;
- the blockhash failure not logged;
- the Jupiter `extra` phase untimed;
- the roster outside the loop timer;
- peak age measured from the refresh start;
- the current age of a still-failing input ignored.

Also: a broken log never breaks the refresh, and a good pass logs nothing.

Step 2, to write with the build:

- The defaults reproduce today's `PERIOD_S` exactly.
- The settings refuse balances 7.5 and blockhash 16, and accept 7 and 15.
- A change in pending totals or withdraw state forces a balance read on the next loop.
- A confirmation between the key read and the balance read forces a re-read on the next loop. A
  mutant that reads the key after the balances fails this.
- A failed key read drops balances to 2 s, not to every loop.

## 6. After each step

- **Step 1:** 24 h of report lines; peak ages, work p99/max, phase maxima and failures per input.
  These feed the P1 selection rule.
- **Step 2:**
  - heartbeat `thread:slot-lane-prefetch` rate per method;
  - `slot_lane_stale_*` refusals compared with the step-1 window;
  - lane `lane_ms` p50/p90 and lane fills per day.

## 7. Next target (not in scope)

The lane's Jupiter legs re-quote every 1.5 s per venue (`LEG_REFRESH_S`; the comment at
`start_from_context` puts it at ~2.7 funded Jupiter calls a second, ~230,000 a day). That load is on
the funded Jupiter key and is larger than this one. It goes in a spec of its own.
