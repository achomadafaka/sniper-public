# On-chain lane latency: why sends take it, and two cuts to decide-to-send (spec)

Status: C1 and C2 BUILT on lambda/onchain-c1c2, 2026-10-01 (operator order after LILMET 68051:
build both; the order covers activation once Gamma is CLEAN). Each cut sits behind its own
default-off knob. C3 stays a census.

## 1. What was measured

The sample is Gamma's bot-speed rows (`~/.cache/gamma/botspeed/chain_rows.jsonl`, 159 sends,
2026-09-26 to 10-01): 59 on-chain lane (O) sends and 100 slot-lane (L) sends. Each O send was joined
to:
- its entry attempt (stage durations and trace substages);
- its `onchain_wallet_detections` row (detection basis and latency);
- the slot lane's decision line for the same handle and source slot, in `logs/memecoin_runner.log`.

All numbers below are read-only from those sources.

**Where the 59 come from** (detection basis):

| basis | n | how it is detected | land minus source slot, p50 |
|---|---|---|---|
| `slot_lane_routed` | 33 | the slot lane's processed socket pairs a routed (USDC-paid) buy and hands it over at once | S+4 |
| `pump_event_net_sol` | 13 | the confirmed watcher, 1.26 s after the block (p50) | S+8 |
| `route_owner_quote_out` | 12 | the confirmed watcher, 1.53 s after the block (p50) | S+6 |
| `slot_lane_handoff` | 1 | a slot-lane refusal handed over (pool unknown) | S+6 |

**Why they did not take the slot lane:**

| reason | n | what it is |
|---|---|---|
| CPMM pool quoted in another token | 16 | the slot lane refuses `slot_lane_cpmm_quote_not_supported`; the routed pairer hands the buy over |
| USDC-paid routed buy | 16 | the slot lane never builds a routed buy, by design: it hands it over at processed time |
| no slot-lane decision at all | 14 | 6 pump-curve buys and 8 routed buys that only the confirmed watcher saw, 1.3-1.5 s late |
| slot-lane gate refusal | 9 | scaled clip 3, mcap 2, pool learning 2, quote not SOL 1, source 1 |
| slot-lane send failed on chain | 3 | slippage 2, not enough tokens 1; the on-chain lane then filled |
| handoff | 1 | |

**Decide-to-send on the on-chain lane** (the sum of the stages from `market_data` through
`transaction_submitted`), p50 / p90:

| path | n | decide-to-send | market_data | reservation | quote | signing | submit |
|---|---|---|---|---|---|---|---|
| routed handoff | 33 | 764 / 1,170 ms | 211 / 281 | 8 / 21 | 450 / 573 | 51 / 450 | 3 / 33 |
| confirmed watcher | 26 | 693 / 1,435 ms | 223 / 366 | 35 / 92 | 110 / 438 | 201 / 614 | 24 / 36 |

The watcher path adds its detection lag, 1.3-1.5 s after the block, and 230 ms p50 from the detection
row to decision start.

**Two serial waits sit on that path.**

1. **The quote waits for the market-data GET.**
   - `market_data` is one HTTP read (the substage `dex_pair_selection`, ~200-250 ms wall time, ~1 ms
     CPU).
   - `memecoin_early_quote` exists to run the quote beside that GET, but `OnchainEv.their_market_cap`
     is always None (`memecoin_onchain_lane.py:209`). So `cheap_prescreen` skips the early quote with
     `mcap_unknown` for every on-chain lane decision.
   - The early quote ran on at most 1 of the 59 sends. The `"early": true` fields in these traces
     belong to the gate balance pre-reads, not the quote.
2. **On a thin route, Raydium is asked only after Jupiter answers.**
   - `_quote_with_fallback` (`solana_real_seams.py:1030`) asks Raydium for a second opinion when
     Jupiter's impact exceeds 5%, and only once Jupiter has answered.
   - 28 of the 61 quotes took that branch, at p50 442-511 ms, against 107 ms for the 33 that did not.
   - Raydium won 4 of the 28, lost 9, was distrusted on 3, and had no quote on 12.

## 2. Proposal

**C1. The early quote on the on-chain lane** (knob `MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN`, default 0).
- For `signal_source='onchain_watch'` only, an unknown market cap no longer refuses the early quote in
  `cheap_prescreen`. The scope is in the code, not only in the knob: a J7 event with an unknown mcap
  still skips (Gamma L1).
- Everything else in `memecoin_early_quote` stays as it is:
  - the early thread runs the reservation's own risk envelope (`sizing_fn`) before quoting;
  - the executor uses the early quote only for the same approved clip and a quote at most 1.5 s old;
  - a refusal is confirmed on a fresh quote.
- Expected cut: the quote leaves the critical path for as long as the GET runs. That is about 210 ms
  on the routed path (its quote is longer than the GET) and about 110 ms on the watcher path.

**C2. Ask Raydium beside Jupiter on entry quotes** (knob `MEMECOIN_ENTRY_RAYDIUM_PARALLEL`, default
0).
- On the entry quote seam only (not exit previews, rail quotes or the round-trip cap), start the
  Raydium quote together with Jupiter's.
- The decision rule stays as it is:
  - Raydium's answer is used only if Jupiter answered with a thin route;
  - a Jupiter transport error still returns no quote, as today. A Jupiter 429 or 4xx miss still
    falls back to Raydium, as today (the 2026-08-14 reversal). It now reads the Raydium answer that
    was begun beside Jupiter's call. (The first draft of this spec wrongly said a 429 ignores
    Raydium; the code, unchanged here, falls back.)
  - a route that is not thin discards it.
- The parallel Raydium call runs on a small bounded executor (2 workers). When both workers are busy
  (a Raydium hang under a burst), the quote takes today's serial path. Orphan threads therefore
  cannot pile up in the funded process (Gamma L2).
- Expected cut: Jupiter's time on the 46% of quotes that are thin, about 100-150 ms.

**2026-10-06 TANG amendment: entry reverse-route proof.** TANG's fallback admission waited
259 ms for an optional Raydium comparison after Jupiter had returned a valid sell route.
With both existing Raydium parallel/nonblocking knobs enabled, that comparison now starts on
the existing bounded worker only after a thin Jupiter proof. An immediately completed result
may still win; otherwise Jupiter's proof proceeds through the unchanged sell-route and
round-trip gates, and the late answer is recorded only as a shadow. Busy workers never cause
a serial second opinion here. Low-impact proofs start no extra call. Missing Jupiter routes
retain their typed fallback; held-position exit previews and rails keep their existing venue
comparison. The nonblocking knob at zero restores the prior admission behavior.

The receipt independently identifies TANG's fast-attempt failure as the funding conversion:
$175 USDC expected 622,577,148 quote-token atomic units at 150 bps, but the conversion produced
393,583,624. Jupiter 6001 occurred before PumpSwap ran. No widening of that funding floor or
the chase cap is justified. This scheduling change cannot guarantee the funding conversion,
same-slot execution, or profit, and does not change sizing, exit thresholds or signing policy.

The regression held Raydium unfinished and reproduced a blocked entry proof before the fix.
Offline 12-observation fixture with 10 ms Jupiter + 50 ms Raydium no-quote: proof latency
p50/p90 60.37/60.42 ms before, 10.34/10.46 ms after. These are scheduling measurements,
not organic execution improvements. Run `tests/test_memecoin_raydium_nonblocking.py`.

**Together**, if both medians hold: routed decide-to-send ~764 → ~440 ms p50, and the watcher path
~693 → ~580 ms p50. At ~0.4 s a slot that is about one slot earlier. That is an estimate; it is
measured after (§6).

**C3. Coverage, measured before anything is built.**
- The 14 sends the slot socket never decided are the costliest: the watcher found them 1.3-1.5 s
  later. A census over the last 7 days of watcher-only detections of armed, registry-verified owners
  would say which it was:
  - the subscription (owner not in the socket set at the time);
  - the decode (transaction shape);
  - or a socket gap (reconnects).
- The 16 CPMM pools quoted in another token already reach the on-chain lane at processed time. Their
  slot-lane path is the on-demand leg worker (shadow today), promoted only on its shadow grade.

**Not proposed.**
- Skipping or caching the market-data GET: Solana's volume gates read it.
- Speculative signing.
- The signing tail (p90 450-614 ms, `timing_swap_build` up to 735 ms) is an open item for a later
  read.

## 3. Load

- **C1 adds funded Jupiter quotes on decisions a later gate refuses.** Over 2026-09-26 to 10-01, 655
  on-chain lane decisions reached the GET; 487 were refused before the quote, 402 of them at the mcap
  ceiling, which only the GET can see.
  - The early thread's own risk envelope would stop the cooldown, one-per-coin, daily-loss and kill
    refusals (62).
  - About 400 quotes would be wasted over 5.5 days, ~3 an hour.
  - For scale, the slot lane already re-quotes its legs about 2.7 times a second on the same key.
  - The heartbeat's `early_quote.wasted_quotes` counts them.
- **C2 adds one Raydium API call per entry quote**, about 30 a day on the on-chain lane plus the J7
  lane. That is Raydium's public API, not the funded Jupiter key. A Raydium error keeps Jupiter's
  quote, as today.

## 4. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes, both.** They change WHEN the entry quote is taken, never which quote is used or
   what gates it passes.
   - C1 reuses the shipped early-quote rules: same clip, age at most 1.5 s, refusal confirmed fresh.
   - C2 keeps `_quote_with_fallback`'s comparison byte-for-byte; only the start time of the Raydium
     call moves.
2. **Speed on the fill path.** Both remove waiting and add none.
   - C1's thread start is the same as on the J7 path today.
   - C2 never waits on Raydium when Jupiter's route is not thin: the result is dropped and the thread
     ends on its own HTTP timeout.
   - Measure the decide-to-send p50/p90 per path after activation; the estimates above are not a
     measurement.
3. **Concurrency.**
   - C1 runs the existing `EarlyEntryQuote` thread with its own read-only connection.
   - C2 adds one short-lived thread per entry quote. It shares no state and writes nothing; the
     quote result is returned to the caller's thread through a future.
   - No module attribute changes.
4. **Absent versus zero versus unknown.**
   - C1: unknown mcap passes the PRE-screen only, for the on-chain lane only. The mcap ceiling gate
     still refuses after the GET, as today. A J7 event's unknown mcap still skips.
   - C2: a Raydium answer that arrives after Jupiter's non-thin answer is dropped, never compared.
5. **Dependencies.**
   - A Jupiter 429 on C1's early thread discards that quote, and the executor quotes as before.
   - C2 keeps today's rules: a transport error returns no quote; a 429 or 4xx miss falls back to
     Raydium.
   - A Raydium hang costs nothing on a non-thin route; on a thin one it costs what it costs today.
6. **Units.** Unchanged: the same atomic in/out amounts and impact conversions (`_ray_quote` already
   divides Raydium's percent by 100).
7. **The sample.** 59 sends in 5.5 days, 33 and 26 per path: medians only, no tail claims. The
   decision counts in §3 are every on-chain lane Solana attempt in that window.
8. **Rollback.** Each knob is 0 by default, and setting it back to 0 restores today's path at the
   next restart. Half-applied (one knob on) is independent.

## 5. Checks to leave behind (with the build)

Two trace fields, so each cut is measured per attempt (Gamma MEDIUM). As built:
- **C1:** substage `early_quote` on every attempt, on-chain lane included.
  - `used` is `early_quote_consumed`.
  - `wait_ms` is `quote_wait_ms`: how long the executor blocked in `take()` for the quote.
  - A pre-screen skip marks `used=false, reason=skip:<why>`.
- **C2:** with the knob on, substage `quote_fallback` gains three fields whenever Raydium was asked
  (the thin branch or the miss branch):
  - `jupiter_done_ms` and `raydium_done_ms`, each from the quote start;
  - `raydium_parallel` (false when both workers were busy and the serial path ran).
  - With the knob off, the tag is byte-identical to today's.
  - Known timing effect (Gamma LOW on d30a439a5). With the knob on, a 5xx or transport miss is
    classified without waiting for the parallel worker. The miss `detail` may or may not carry the
    `; raydium ...` suffix. The miss `code` is built from Jupiter's calls only and does not vary.

- C1:
  - an on-chain lane event with unknown mcap starts the early quote when the knob is 1 and skips when
    it is 0;
  - a J7 event with unknown mcap still skips;
  - the executor uses the early quote only for the same clip (an existing test, extended to an
    `OnchainEv`).
- C2:
  - with Jupiter not thin, Raydium's answer is never used;
  - a thin route may use Raydium when ready; nonblocking mode keeps late comparisons shadow-only;
  - a Jupiter 429 retains Raydium rescue when it is the only route answer;
  - exit previews never start a parallel Raydium call.
  - an explicitly marked entry sell proof does not wait for its optional second opinion;
    requesting diagnostics alone must not change a held-position exit quote.

## 6. Pre-registered read after activation (amended per Gamma's MEDIUM)

Comparing medians of two periods of 20 cannot resolve 100 ms at this spread: p90 is 1,170 / 1,435
ms, and the coin mix and provider load change between periods. Each cut is therefore read on its own
instrument:
- **C1 counts** if, over the next 20 routed on-chain lane sends, the early quote is consumed on at
  least 70%, AND the median `quote_wait_ms` is at least 100 ms below today's quote-stage p50 of
  450 ms.
- **C2 counts** if the median per-quote saving, (J + R) - max(J, R) from `jupiter_done_ms` and
  `raydium_done_ms`, is at least 50 ms over the next 20 thin-route quotes. That saving is exact for
  each quote, with no period confound.
- **Secondary line:** decide-to-send and land minus source slot medians, before against after.

A cut that does not count goes back to knob 0.
