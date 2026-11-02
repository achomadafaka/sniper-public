# Anchor-checked EVM exit — design

**Date:** 2026-10-01 · **Author:** Beta · **Order:** the operator, via Alpha: neither "a floor that leaves
the bag stuck" nor "sell at -98% when only a bad route answers". · **Status:** FINAL proposal (Alpha + Gamma's rules after her reviews of 68864c224 and 4864c6832), measured;
Gamma CLEAN (85d092e77 + her required edit); no build before the operator's go. No build before Gamma's review
and the operator's go. Alpha's recommendation to the operator: X 50, max-hold 60 s, the UI button, all
EVM chains.

## 1. The problem

The EVM sell ladder takes the first venue that answers, by priority. Its only guard is the dust check
(`evm_real_seams._sell_plausible_fn`): an output below 1/20 of what we PAID waits up to
`MEMECOIN_EVM_SELL_DUST_WAIT_S` (3.5 s) for the other venues and takes the largest. That guard is relative
to the entry cost, not to what the bag is worth now: a bag that tripled can sell at -90% of its value
and still clear 1/20 of cost. After 3fec0b131 a Base exit that falls to `v4_multihop` sells instead of
reverting, and a multihop route can be far worse than the bag's own pool (the proof route round-tripped
-98.22%).

**Why the anchor cannot be `last_executable_price` (Gamma HIGH).** `real_exit_sweep` writes the sweep's
own preview before the rails and the exit run (`_keep_route_quote(pos, preview, "exit_decision")`, then
`set_position_last_executable(exact_mid)`, memecoin_real_runner.py:5945 and :5997). On a HOMER tick that
preview IS the connector route at 1e-11 BNB, so a trigger on that tick would read a dust anchor and the
dust route would pass the check. The anchor is therefore its own sticky value (§3).

## 2. Measured (EVM, 2026-09-01 to 10-01; `scripts/research/anchor_exit_measure.py`)

Exit ticks (`memecoin_route_series`, seam `exit_decision`, real positions): 4663 47,980; 56 18,030;
8453 19,902; 5042 668. Cadence 1.49 s at p50, 2.1 s at p90.

**Sticky anchor, frequency read** (the last tick that passed; a low tick never moves it; for THIS table
only, the anchor expires 60 s after it was set, the earlier draft's rule — the final rule's per-episode
read, with the HOLD_S re-base and no expiry, is §4), threshold 50%:

| | 4663 | 56 | 8453 | 5042 |
|---|---|---|---|---|
| ticks below 50% of the anchor | 66 | 5 | 0 | 0 |
| episodes (runs of low ticks) | 5 | 5 | 0 | 0 |
| run lengths (ticks) | 1, 1, 2, 30, 32 | 1, 1, 1, 1, 1 | — | — |
| how runs end | 1 recovered, 2 anchor aged out at 60 s (58.7 s, 59.6 s), 2 open at the end of the data | 5 recovered | — | — |
| recovered within 2 / 10 / 30 / 60 s | 1/1/1/1 of 1 | 4/5/5/5 of 5 | — | — |

- **Two families, as the rule needs.** The crash family persists: the two long 4663 runs (54015, 50693)
  stay below the anchor until it ages out. The dislocation family does not: all 5 BSC episodes are HOMER
  (67676), where the direct coin/WBNB pair went silent for one tick while the V2 connector path
  coin -> 0xb594...7777 -> WBNB quoted 10,096,166 wei (about $0) for a bag worth about $100; the direct
  pair answered again 1.8 to 2.1 s later. No exit triggered on those ticks.
- **Real sells:** 360. Paid below 50% of the previous tick: 2, both genuine 4663 crashes (52064 `b_stop`
  0.189, 53402 `catastrophe` 0.214); below 80%: 12. Venue identity on 4663 rows starts 2026-09-11.
- **Base:** 0 of 19,902 exit ticks were won by `uniswap_v4_multihop` (it reverted at execution before
  3fec0b131, and its previews are cut at the ladder grace). The -98% case has no history on Base; it is
  prospective.
- **Limits:** exit ticks store only the winning route, so "the anchor venue missed" is measured as "another
  venue won", an upper bound on misses: fine for frequency, useless for the crash branch. The runtime
  has the real per-rung outcomes in memory (the ladder's `completed` results); §5 stores them from day
  one on every held tick.

## 3. Rule (FINAL: Alpha + Gamma, 2026-10-01; EVM chains in `MEMECOIN_EVM_EXIT_ANCHOR_CHAINS`, default empty)

No venue identity (Gamma's MEDIUM A and B on 4864c6832 existed only to tell "the anchor venue missed"
from "it answered low"; the measured episodes do not need that split). No token-typed skip: it is
unattributable without per-pool typing inside a rung, and on HOMER the v3/v4 rungs return `no_route` on
every tick, so it would have fired on every HOMER tick.

### 3.1 The anchor (sticky, its own fields, no wall-clock expiry)

New nullable columns on `memecoin_positions`: `exit_anchor_price` (USD per token) and `exit_anchor_at`.
`last_executable_price` is unchanged (the loss rails read it).
- **Seeded at the fill** from the entry fill's unit price.
- **Moved by a passing tick:** in `real_exit_sweep`, before the sweep's own write, a preview whose unit
  value is >= X x anchor moves the anchor to it. A tick below X never moves it.
- **Re-based by a persistent low level:** when ticks below X persist for HOLD_S over >= 2 ticks, the anchor
  re-bases to the latest of them. A real crash therefore becomes the new level within HOLD_S; a one-tick
  dislocation never does.
- **Written in the same statement** as `set_position_last_executable`: one UPDATE, one commit per tick.

### 3.2 The decision, at the sell (non-blocking)

1. The selected sell quote pays >= X x anchor x qty sold: **sell as today.** One multiplication.
2. It pays less: **do not sign.** The executor returns `held_anchor` at once: no sleep, no loop. The next
   sweep tick re-quotes as it already does.
3. The first tick that quotes >= X x anchor **sells on it** (`sold_recovered`).
4. Once the low level has persisted HOLD_S over >= 2 ticks, the anchor re-bases (§3.1), so the next sell
   quote passes and **sells**; the sale pages the operator (`sold_at_bound`).
   **The executor bounds the hold too** (Gamma, required on 85d092e77): the re-base counts PREVIEW ticks,
   while the hold is decided on the SELL quote, a separate ladder run (and `_exit_venue_quote` tries a
   stored `pcs_mixed_v3_v2` or `v4_multihop` route first). If previews keep passing while the sell quote
   keeps coming back low, the anchor never re-bases. So the executor's per-position hold state (§7.3)
   also ends it: once HOLD_S has passed since the first `held_anchor` AND >= 2 sell attempts were held,
   the next attempt sells the largest answer (`sold_at_bound`, paged). No new call.
5. The UI **"sell at any price"** button on a held row sells the largest answer at once (`sold_override`).
6. A manual UI Close obeys the same rule; its hold shows on the row at once.
7. Partial exits and de-risk: the anchor scales with the fraction sold (a whole-bag unit value, so the
   ratio is biased slightly up on partials; harmless, stated).

**HOLD_S = 6 s, X = 50%** (`MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S`, `MEMECOIN_EVM_EXIT_ANCHOR_MIN_PCT`). X is a
policy threshold: the sample cannot estimate it.

## 4. Measured for the final rule (EVM exit ticks, 30 d, sticky anchor, X = 50%)

Per episode (a run of ticks below X), at HOLD_S = 6 s; `scripts/research/anchor_exit_measure.py`, part 1d.

| chain | position | outcome | after | USD vs the first low tick |
|---|---|---|---|---|
| 56 | 67676 HOMER | caught | 1.8 s | +102.03 (hold save, had an exit fired) |
| 56 | 67676 HOMER | caught | 2.1 s | +104.49 |
| 56 | 67676 HOMER | caught | 1.8 s | +101.60 |
| 56 | 67676 HOMER | caught | 1.9 s | +101.63 |
| 56 | 67676 HOMER | caught | 2.0 s | +99.19 |
| 4663 | 53170 | caught | 1.8 s | +24.42 |
| 4663 | 50693 LARP | bound (re-based) | 6.9 s | 0.00 (crash cost) |
| 4663 | 54015 ROBSTR | bound (re-based) | 7.7 s | -0.18 (anchor $10.18, first low $0.76) |
| 4663 | 47260 EQUINOX | sold inside the window (real `b_stop`) | — | unobserved |
| 4663 | 52064 SOLANO | sold inside the window (real `b_stop`) | — | unobserved |

- Every dislocation ended within 2.1 s; 6 s catches all six. At 10 s and 20 s nothing more is caught, and
  54015's cost grows to -$0.29 and -$0.36 (0.76x / 0.61x / 0.52x of the first low quote at 6 / 10 / 20 s
  on the tick at or after the bound, Gamma's read); 50693 stays flat.
- The 2 unobserved are the real crash sells: the series stops at the sale and the 4663 tick tape stopped on
  2026-09-04, so the price over a 6 s delay is unknown; the worst case is the value at the first low tick
  (EQUINOX $22.77 of a $144.70 anchor, SOLANO $29.94 of $155.77). 53402 (HASH, `catastrophe`) crashed between
  its decision tick and the fill: not a series episode; under the rule its sell quote would hold up to 6 s.
- The hold saves are POTENTIAL: no exit fired on those ticks. They show what one firing would have saved.

## 5. Latency (Rule 5.2)

- **Normal path: no call, no wait, no lock;** a comparison on values in memory. The build measures it with
  `perf_counter_ns` on the production context (p50/p90) before claiming zero.
- **Sweep write:** the anchor rides the existing `set_position_last_executable` UPDATE.
- **Outlier path:** non-blocking; the sale waits for the sweep's next ticks, at most about HOLD_S + one tick.

## 6. Records and the forward grade (Gamma, stamped)

- `memecoin_exit_anchor_audit`: one row per held tick and per held sale (position, chain, observed_at,
  selected route, its USD value, the anchor, seconds held, decision, the tick's rung ledger as compact JSON).
- **Unit:** a held episode. **Delta** = realized - the quote today's code would have signed at the first
  hold, where realized = the quoted value of the route actually signed (the booked fill is reported beside
  it). **Pass** at 10 episodes on sum(delta) >= 0, every episode listed. **Early stop** on any single delta
  < -$25 (switch off: `MEMECOIN_EVM_EXIT_ANCHOR_CHAINS` empty).
- **Fail** (sum < 0): `MEMECOIN_EVM_EXIT_ANCHOR_CHAINS` empty pending a new decision; X and HOLD_S are not
  retuned on these 10 episodes; a retune is a new stamp on fresh episodes.
- **Build check (Gamma LOW 3):** a test that a held manual close intent survives the `held_anchor` return
  and is retried on the next tick, not dropped after one attempt.

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It delays a sell by about HOLD_S (6 s) at most; every hold is recorded.
2. **Fill path:** exits only; a comparison on the normal path; no extra commit; nothing blocks.
3. **Concurrency:** the anchor is written only by the sweep; the hold state is per position in a dict under
   a lock in the exit executor, shared with a manual close.
4. **Absent / zero:** no anchor or a zero one disables the rule (today's behaviour).
5. **Dependencies:** a provider outage during a hold ends with the re-base at HOLD_S.
6. **Units:** USD per token on both sides, with the preview's own quote-asset USD. On taxed V2 coins (BSC)
   the preview values a fee-on-transfer exit as the whole bag less the allowance
   (`evm_real_seams._with_exit_value`), while the sell quote keeps the 15% max-tax cut, so the
   sell-vs-anchor ratio reads up to about 15% low there. Harmless at X = 50; stated.
7. **Sample:** every EVM exit tick and real sell for 30 days (§2, §4).
8. **Rollback:** `MEMECOIN_EVM_EXIT_ANCHOR_CHAINS` empty (the default) is today's behaviour byte for byte;
   the new columns and table are additive.

**Not handled (named):**
- A passing tick from another venue can move the anchor down in steps of up to 50% (no venue identity).
- A rug's sale waits up to HOLD_S (no token-typed skip).

## 8. Decisions (operator; Alpha's recommendation)

X = 50%, HOLD_S = 6 s, the UI "sell at any price" button, all funded EVM chains (4663, 56, 8453, 5042),
switched by `MEMECOIN_EVM_EXIT_ANCHOR_CHAINS` (default empty).
