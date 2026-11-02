# Solana micro band: an hourly turnover floor on the gate's own organic figure

Status: DRAFT for the operator's yes (Alpha's order 2026-09-28 ~18:12Z: "draft the rule-5 specs,
no code"). Author: Beta. Evidence: Gamma, `docs/audits/2026-09-28-move-possible-floor-by-band.md`
(pre-registered 3abfd975 + amendment 1faba957, result 1d4fc852; `scripts/research/move_possible_floor.py`,
`logs/acceptance/move_possible.jsonl`). Operator's order, via Alpha: "a floor that guarantees that a
move is possible ... different sized coins behave differently." Money path, entry side.

## Evidence (Gamma), and the one thing this spec settles

On Solana micro (n=244 decisions since 09-14), provider h1 turnover predicts whether the coin could
move at all: AUC 0.641 [0.546, 0.729]. The pre-registered floor rule is SUPPORTED at the lower
tercile: P(move) +0.188 [0.048, 0.316] above it, P(dead) +0.231 [0.075, 0.370] below it. Money
does not contradict it: the micro fills below the cut are n=27, -$139.31 [-391.37, +95.24], and
those above are n=67, +$865.13 [+129.70, +1,595.11]. The money CI spans 0. On nano, money says NO
floor (the quiet nano fills made +$286.77), so nano is untouched.

**Which figure (Lambda's review).** Gamma's cut, 0.1645, is on RAW provider h1 over `volume_mcap`
(the provider's market cap, else `effective_mcap`). The gate's h1 legs judge a different figure:
ORGANIC h1 (`organic_volume`: provider h1 minus the source's own buy when the provider can contain
it, and at least the tape or feed lower bound) over `facts.effective_mcap`. The two denominators
agree on only 43 of 244 micro rows (effective/provider p10/p50/p90 0.823 / 1.013 / 1.281).
Recomputed on the same 244 decisions (organic approximated as max(0, h1 - source_size_usd); the gate's
index-lag rule and lower bound can only raise it):

| figure the floor reads | cut | decisions below | closed fills below | their PnL |
|---|---:|---:|---:|---:|
| raw h1 / volume_mcap (Gamma's) | 0.1645 | 82 | 27 | -$139.31 |
| organic h1 / effective_mcap (the gate's), SAME number | 0.1645 | 89 | 31 | **-$4.06** |
| organic h1 / effective_mcap, at **0.143** | 0.143 | 83 | **27 (the same 27 fills)** | **-$139.31** |
| organic h1 / effective_mcap, its own lower tercile | 0.1352 | 81 | 26 | -$130.54 |

Putting the literal 16.5% into the gate would refuse 4 more fills, all with organic turnover
0.146-0.150. They net +$135.25: three winners (+62.74, +47.38, +32.30) and one -$7.16. Refusing
them would erase the money case. **The floor reads the gate's own organic figure at 0.143.**
That refuses exactly the fill set the evidence graded, so Gamma's AUC, floor rule and money CI carry
over unchanged.

## Change

1. New knob `MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1`, default 0 (today). Its proposed value is
   0.143, on the operator's yes.
2. In `volume_gate_args` (`memecoin_real_runner.py:7641`), on Solana with `coin_band == "micro"`
   and a known mcap, `turnover_floor` = the new knob. The flat `abs_floor` stays 0, exactly as
   today (`solana_small_h1_off`). Nano, small, mid and every other chain are unchanged.
   `MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED` keeps its meaning: it turns on the GLOBAL h1
   floors for nano and micro, and it is not the switch for this floor.
3. The existing relative leg in `volume_alive_decision` decides: organic h1 < effective_mcap x
   floor refuses with `turnover_under_floor` ("h1 organic volume $X under floor $Y"). A missing h1
   leaves the leg silent. No new decision code: one argument changes for one band.
4. The trace's `h1_scope` label says `solana_micro_turnover` when this floor is set, so the
   forward read can split it.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: YES, entry admission.** It can only REFUSE: Solana micro coins whose organic
   hourly turnover is under 14.3% of mcap. Over 09-14..09-28 that is 83 decisions (about 6 a day),
   of which 27 filled (about 2 a day). It never admits anything new, and it does not touch exits,
   sizing or other bands.
2. **Speed.** None. It reads figures the gate has already fetched (`facts.volume_h1`,
   `effective_mcap`, the source size); it is one multiplication and one comparison. No I/O.
3. **Concurrency.** None. It reads a module constant once per decision, and nothing is written.
4. **Absent vs zero vs unknown.**
   - h1 missing: the leg is silent and the coin is admitted, as today.
   - Organic h1 exactly 0 (the provider's h1 equals the source buy, or lies under it): the coin
     is refused. That is not the m5 floor's "market not started" posture, but the evidence graded
     these rows in its lowest tercile.
   - mcap unknown: the coin has no micro band, so the floor never applies.
   - A source size missing on 19 of 244 rows makes organic = raw there, which is the gate's own
     behaviour.
5. **Dependency failures.** DexScreener's h1 lags about 25 s plus a 30 s CDN cache. On an hourly
   figure that is minor, and the `organic_volume` index-lag rule already handles a source buy the
   provider has not indexed. A wrong (under-counted) h1 refuses more: the known accepted bias of
   the floor legs.
6. **Units and identity.** USD over USD. The denominator is `facts.effective_mcap`, the gate's own,
   not Gamma's `volume_mcap`. That is why the number is 0.143 and not 0.165. The band is
   `classification.coin_band == "micro"`, the canonical band.
7. **The sample.** 244 Solana micro decisions, 09-14..09-28, one per (mint, 900 s), filled and
   refused alike, with the outcome from chain prints (Gamma). Our PnL is the market filtered
   through our gates, so the money CI spans 0. The regime changed inside the window:
   `MEMECOIN_REAL_CHASE_REFERENCE=post_trade` went live 15:01Z. Not re-run on organic with the
   outcome: the organic-figure rows above are my recomputation of the fill set, not a new AUC.
8. **Rollback.** The knob at 0 plus a runner restart. Half-applied: the knob unset is today's
   behaviour. `volume_gate_args` is shared by the entry hook and the revival watch, so both see the
   same floor.

**The input is still DexScreener (Alpha, 2026-09-28).** The operator ordered "no dex anywhere",
and Lambda is inventorying the removal. The gate's organic h1 on Solana is still DexScreener's
listed-pair h1 volume, raised by our tape's lower bound (h1_floor_lower_bound). This floor inherits
that input until the h1 source is replaced with our own tape. The cut 0.143 was graded on that same
provider figure, so a new source needs its own calibration before the value carries over.
A missing h1 is neither a pass nor a refusal in disguise. It keeps the relative leg's existing
unknown handling (the coin is admitted), and the volume_gate substage records
`h1_floor_input="missing"` (otherwise `"provider_h1"`) beside `h1_floor_scope="solana_micro"`.

**Not handled, stated:**
- The slot lane runs no volume gate: it fetches no provider h1. Gamma's population also excludes
  it, because it requires `facts.volume_h1`.
- 4663, small and mid: no evidence yet. The 4663 recorder keeps accruing.
- The relative h1 leg is not pro-rated by pair age. The evidence was graded un-pro-rated, and
  pro-rating would change the fill set.
- Chain activity in the 5 min before the decision predicted at least as well (nano chain SOL AUC
  0.706). It is exploratory and not built here.

## Check left behind

`tests/test_memecoin_solana_micro_h1_floor.py`:
- `volume_gate_args` gives turnover_floor = the knob only for Solana micro with a known mcap, and 0
  for nano, small, other chains and an unknown mcap;
- 0 is today's behaviour;
- one real micro decision below 0.143 refuses with `turnover_under_floor`, and one above admits,
  from `move_possible.jsonl` rows copied into a fixture;
- a missing h1 admits;
- the organic figure (the source buy subtracted) is the one compared, a mutant that reads raw h1
  fails, and the denominator is `effective_mcap`.
