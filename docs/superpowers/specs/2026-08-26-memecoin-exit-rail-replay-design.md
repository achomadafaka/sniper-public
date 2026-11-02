# Exit-Rail Replay Design

**Status:** Draft for review 2026-08-26

## Goal

Decide whether the funded exit rails should change — specifically whether a LADDERED take-profit
beats the current flat one — using the historical mark series, and state up front the precision that
series can actually support.

Not a goal: producing an expected-PnL number. The instrument cannot carry one, for reasons measured
below.

## Why the rails are the question at all

Measured 2026-08-25/26 on the funded book:

| | |
|---|---|
| wins | n=13, avg **+32.4%** |
| losses | n=12, avg **−33.2%** |
| breakeven win rate | **50.6%** |
| actual win rate | **52%** |

The book is structurally at breakeven. Take-profits cap near +27.6% because the TP triggers at 25%
and barely overshoots; stops realise near their nominal level. So the rails, not the entries, are
where the distribution is set.

An earlier claim that STOP TRIGGER LAG was the dominant cost is **RETRACTED**. Measured against each
position's own contract stop level, median slip past nominal is **+2.1 points**, five of seven inside
2.6. The standing −56.9% figure mixes a 50% and a 35% rail and does not describe the current era.

## THE INSTRUMENT, AND ITS CEILING

This section is the design. Everything downstream is bounded by it.

### The mark series has no executable prices

```
memecoin_position_marks              3,246,299 rows
mid == executable_exit_value on      3,246,299 rows   (100.0000%)
rows where the two differ                    0
```

`executable_exit_value` is a **reserved alias for `mid`**, not a measurement. Four separate files say
so; it was still nearly built on, and only measuring caught it. The column name is itself a hazard.

Consequence: **at tick granularity the chart is all there is.** The only genuinely executable numbers
per position are three points — the entry fill, the exit fill, and `executable_peak_price` (a
position-level high-water ratchet, present on 72 of 84 recent closes).

### How wrong the chart is, measured

Exit fill against the contemporaneous mid, restricted to closes whose last mark was fresher than 3s
so staleness is not read as slippage:

```
n=69   median +0.28%   p10 −9.65%   p90 +20.14%   min −33.4%   max +76.4%
```

Centrally almost unbiased; dispersion enormous. **That dispersion is the resolution limit of any
mid-space replay**, and it is wider than most differences we would be trying to detect between ladder
variants.

The unrestricted version (n=91, max +134.7%) is contaminated: with a stale mark, slippage and a real
price move are not separable. A sampled series cannot distinguish them, which is a limit of the
method rather than of this dataset.

### What follows for the output format

The replay reports **ordered comparisons with a stated error band**, never point estimates:

- Variants whose separation falls inside the band are reported **indistinguishable**, not ranked.
- No result is expressed as expected PnL, dollars, or per-trade edge.
- "Everything indistinguishable" is a valid and useful outcome: it would say historical series cannot
  rank exit variants on this book, and forward measurement is the only instrument. That is worth
  knowing on its own.

## Cohort and censoring

### Density floor

```
marks/second while open:  median 0.313   p10 0.026   p25 0.065   p75 0.529
floor ≥0.05/s keeps 78/98 (80%)   ≥0.1/s keeps 71/98 (72%)   ≥0.2/s keeps 64/98 (65%)
```

**Proposed floor 0.1/s**, keeping ~72%. Positions below it are censored and COUNTED, never
interpolated — reconstructing a path through a gap invents the very prices the replay is measuring.

### Zero-mark positions are excluded, and two of them are a separate fault

`44626, 44630, 44631, 44636, 44646` have no marks despite 9–246s holds. For **44626 and 44646 no
marks existed for ANY mint** during their windows — a writer outage, not a coverage gap. That finding
produced the rails-deaf detector (`cefaf8de`) and is not a replay concern beyond exclusion, but the
ids are recorded here so the cluster is not silently absorbed into censoring.

### Per-position reporting

Every position carries its mark density and censoring reason into the output, so a reader can see
what the denominator was rather than trusting a headline n.

## The rails model — a NAMED SHAREABLE COMPONENT

The rails simulator is a separate module with a documented interface, not logic welded inside the
replay. Gamma's refused-mint capture consumes the same contract as its simulation layer, so a
counterfactual computed on a refused coin and one computed on a held coin mean the same thing.

```
rails_outcome(series, policy) -> RailsResult
```

- `series`: ordered `(t_seconds_from_open, price)` observations. Price space is declared by the
  caller and carried on the result; the model never assumes which one it is.
- `policy`: TP level(s) and fractions, stop, trail arm/distance, max hold, catastrophe.
- `RailsResult`: exit reason, exit time, exit price, fraction-weighted return, and the observation
  index that triggered it — so a result can be traced to the tick that caused it.

Properties the component must hold, each pinned by a test:

1. **Pure.** No I/O, no clock, no DB. The same series and policy always give the same result.
2. **Price-space agnostic and price-space honest.** It never converts spaces and it labels which one
   it was given. Mixing a chart series with a route-priced policy silently is the failure it exists
   to prevent.
3. **Censoring-aware.** A gap wider than the policy's shortest reaction time returns
   `undetermined` for that position rather than a guess.
4. **No forward leakage.** A decision at observation `i` may read only observations `≤ i`.

## Candidate policies

Baseline is the live rail: flat TP 25%, stop 35%, max hold 780s, no trail, no de-risk.

| family | parameters |
|---|---|
| single-level TP | 15 / 20 / 25 / 30 |
| ladder | sell fraction f ∈ {0.5, 0.6, 0.75} at TP, remainder on the executable ratchet |
| ladder trail | arm ∈ {15, 20, 30}%, distance ∈ {10, 15, 20}% on the remainder |
| stop | 30 / 35 / 50 |

### A first-order sweep already warns about the naive reading

Over peaks alone (not the tick series), restricted to TP levels **at or below** the live 25%, which
is the only range that sweep can evaluate honestly:

```
current (TP 25 live)   −0.62 pts/trade
TP 20                  +1.05
TP 15                  −1.31
```

That sweep **cannot evaluate raising the TP**: a position that filled at 25% keeps its win in the
model even though a 30% TP would have missed it, which inflates every level above current. Any
result above 25% from a peak-based method is an artefact. The tick replay exists partly to remove
that bias.

## THE PRE-EXIT BOUNDARY

**The historical series ends at our exit.** For a ladder, the remainder is held past that point, and
no historical data prices it. The replay therefore evaluates **ladder mechanics on pre-exit data
only** — the partial at TP, and the remainder taken to a stop or trail *within the observed window*.

The post-exit leg is not approximable and is not attempted here. It is measured forward by the
post-TP follow-up collector (`2ff0306c`), which takes real route quotes at +60/+300/+900/+1800s after
each take-profit. That instrument answers the money question in route space; this one answers only
whether the mechanics order sensibly.

## PRE-REGISTERED DECISION RULE

Written before any number is produced.

1. A ladder **ships only if** the collector's measured post-TP route value covers the remainder's
   giveback risk. If the tails are chart fiction, nothing ships.
2. The replay alone **cannot** promote a ladder. It can only eliminate variants whose pre-exit
   mechanics are worse than baseline beyond the error band.
3. Any variant separated from baseline by less than the mid-space band is reported
   indistinguishable, whichever direction it points.
4. Results are reported per-cohort with the density floor and censored count stated. No pooling
   across rail eras — the stop moved 50 → 35 on 2026-08-25 and the two are different policies.

## Closing the observation space

Three instruments, one question each, and together they cover the coin lifecycle:

| instrument | covers | status |
|---|---|---|
| post-TP follow-up (`2ff0306c`) | coins we EXITED | live |
| route-series persistence (below) | coins we HELD | designed, unbuilt |
| refusal capture (Gamma) | coins we NEVER ENTERED | built, dark |

### Route-series persistence

The funded exit path already COMPUTES exact route quotes — executable-peak previews, exit quotes,
de-risk previews — and discards every value, keeping only the ratcheted scalar. `route_quote_value`
is populated on 0 of 105 current-era funded rows. There is no route-space series because nothing ever
persisted one, not because nothing computed one.

Persisting them is keeping, not fetching: **zero marginal API cost**. Constraints:

- Observation-hook doctrine — never raises, never blocks, holds no lock the entry path wants,
  counters for written and missed.
- No writes inside the tight exit decision loop; buffer and flush from the maintenance side.
- Label WHICH SEAM produced each quote, because a peak preview and a de-risk preview price
  different sizes and are not interchangeable.

Payoff: a true route series for held coins, which is the calibration set for the mid→fill dispersion
above and the precondition for a replay V2 that is real rather than bounded.

## Verification

- Rails-model property tests: purity, price-space labelling, censoring, no forward leakage.
- A hand-computed fixture series with a known correct outcome per policy family.
- Censoring tests: zero-mark and below-floor positions are excluded and counted, never interpolated.
- A test asserting no reported comparison omits its error band.
- Focused suites only; no broad run required.

## Open questions for review

1. Density floor at 0.1/s (72% retention) — or 0.05/s for 80% at lower per-position confidence?
2. Should the replay report the ±band as a fixed figure from the n=69 dispersion, or bootstrap it
   per-comparison from the positions actually in that comparison? The second is more honest and
   costs more.
3. Rails-model interface: does Gamma's capture need `undetermined` distinguished from
   `no_trigger_in_window`? They are different facts and only one is censoring.

---

## OUTCOME — 2026-08-26, built and run (`61f3963e`)

**The replay returns UNRANKABLE.** The variant ordering flips between two settings of the censoring
knob, so the pre-registered rule permits eliminating nothing.

```
censor at own-gap q=0.90   0 of 10 variants separate from baseline
censor at own-gap q=1.00   7 of 10 separate, and the top three reorder
```

`TP 30%` goes first -> fifth, `TP 15%` ninth -> second. Baseline fidelity — the simulated live rail
against what actually happened, manual closes excluded — is **22/32 (69%)**. That bounds every claim
the instrument can make.

### Three corrections to this document

1. **Cohort is 41, not 71.** The 98/71 figures pooled rail eras. Cohorting by each position's own
   funded contract, **28 distinct parameter tuples** exist across 352 contracted funded closes. The
   live era (`follow-30-50-v1` 25/35/780) is 57 closes, **41** after the 0.1/s floor. The floor
   itself stands: it costs 9 positions, not 27.

2. **Quarantined marks were in the series.** The live exit path filters
   `quality != 'quarantined_spike'` (`memecoin_exits.py:317,434`); the first build did not. Position
   44690's window holds 30 accepted marks at ~0.0100 and **49 quarantined at 0.268-0.308** — the
   pair-identity case, a different pool for the same mint. Unfiltered, the simulated baseline
   returned **+2562%** and the ladder variants **-162 points**; filtered, the same ladder reads
   **-1.78**.

3. **The censor threshold fix was directionally right and quantitatively meaningless.**
   `GAP_TOLERANCE_MULT` was calibrated on a synthetic grid with ±50-250ms uniform jitter, whose
   max/median gap ratio is ~1.1x. The real tape: **median gap 1.53s, median MAXIMUM gap 9.18s** —
   4.0x at the median, 15.4x at p90. At 1.5x the median, **39 of 42 positions censored**. The knob
   is a monotonic sample-size dial, not a signal filter:

   ```
   basis x mult      determinate/41   agrees with realized
   median x1.5             3                1/3
   p90    x2.0            14               10/13
   p90    x3.0            23               16/22
   off (max x1.5)         33               22/32
   ```

### Open question 1 — answered

Density floor stays at 0.1/s. On the correctly-cohorted era it excludes 9 of 57.

### Open question 2 — answered

Bootstrap per-comparison, paired per position. The band covers **sampling error only**. The
price-space term is systematic and is reported separately and never folded in.

### Open question 3 — answered, and shipped

Yes. `undetermined` and `no_trigger` are distinct in the contract. The contract additionally gained
a **per-observation cadence hint** for Gamma's occupancy-throttled capture, because one series can
contain two deliberate cadences. At a tier boundary the **wider of the two** governs: the gap
arriving at observation `i` was produced under the previous tier, so `hint[i]` alone censors every
slow -> fast transition. Misaligned sequences are refused, not padded, and hints are carried through
the null-price filter rather than indexed after it.

### The entry anchor is a price space — new, and the largest error term

`memecoin_positions.entry_price` is our **fill**; the mark series is **chart mid**. On this cohort
they differ by a median **1.151x** (n=36, p10 1.003, p90 1.653, max 1.920) — the route premium. A
chart series anchored on a fill starts every position ~15% underwater before the price moves, over
half the way to a 35% stop.

The replay anchors on the **fill**, matching the live funded nominator: under `exact_policy`,
`decision_mid` is the exact route unit price and the rail compares it to `entry_price`. Measured both
ways against realized outcomes, fill reproduces 22/32 and chart 20/33.

**The live funded rails were checked for the same fault and do not have it.** Nomination and the
`executable_exit_allowed` confirmation both run in route-vs-fill space. Only the degraded
no-exact-quote fallback nominates chart-vs-fill, and the confirmer still gates it.

### What still can answer the ladder question

Nothing historical. The post-TP follow-up collector (route quotes at +60/300/900/1800s) and the
route-series persistence are the instruments, and both need forward time.

**Run:** `python3 scripts/research/memecoin_exit_rail_replay.py` (read-only).
