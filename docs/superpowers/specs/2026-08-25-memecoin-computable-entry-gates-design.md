# Memecoin Computable Entry Gates — Design

Date: 2026-08-25
Status: DRAFT — operator approved the direction. No code exists. No gate changed.
Author: Claude (sniper main session), with review by the parallel Codex/Claude session.

---

## 1. Problem

The funded entry check uses one number. That number is the route price divided by the trader's
fill price. The number contains two different quantities at the same time:

1. **The structural cost.** A thin pool moves against our clip. The route comes back above the
   chart before the coin does anything. Measured median, funded fills, LIFETIME SAMPLE:
   `<30k +36.7% (n=76)`, `30-100k +23.9% (n=80)`, `100-250k +14.5% (n=43)`, `>250k +9.8% (n=68)`.
   **These are a lifetime aggregate across configuration eras. §9 retracts them as a forward
   description.** Since 2026-08-23 the same measure reads `<30k +27.4% (n=22)`,
   `30-100k +29.6% (n=28)`, `>100k +19.8% (n=14)` — no ordering at all. Use the current-era
   figures for any forward claim.
2. **The chase.** The coin moves between the trader's fill and our decision. Measured median:
   `<30k -11.0%`, `30-100k -10.5%`, `>100k -3.3%`. The value is NEGATIVE. The chart is usually
   BELOW the trader's fill when we decide.

A flat cap on the product of the two is strict on small coins and permissive on large coins. A
small coin fails the cap because its pool is thin. A large coin passes the cap while it runs.

The two errors partially cancel today. At `<30k` the structural cost is +36.7% and the chase is
-11.0%. The product is about +22%. A 30% cap admits it. The current gate works at the small end
by accident, through two offsetting errors.

The same defect exists on the paper path. `sniper/services/memecoin_copy_trader.py:512` computes
`gap_pct = (price / their_price - 1.0) * 100.0` and compares it to `MEMECOIN_CHASE_MAX_PCT`.
Paper skips sub-30k coins at 20.9% against a 2.1% share of opens — ten times the rate.

---

## 2. Scope

### In scope

1. Split the funded entry check into two independent gates.
2. Apply the same split to the paper copy path.
3. Raise the mcap ceiling to $500,000.
4. Move the corrupt-mcap rejection from the ceiling to mcap resolution.
5. Add a paper arm that mirrors funded entry without the mcap constraint.

### Out of scope — the operator rejected these

- **Clip size by structural cost.** The operator rejected this on 2026-08-25.
- **An mcap floor.** The evidence cannot support a floor. See §3.
- **Broad paper widening across every band.** The operator rejected this. Item 5 replaces it.
- **Any repair to the move-capacity check.** §6 shows the outcome variable is not measurable from
  stored data. No threshold change, coverage repair, or input swap can satisfy the requirement.

---

## 3. Why there is no floor

The sign of the smallest band depends on the cutoff date:

| Cutoff | n | Return per dollar |
|---|---|---|
| since 2026-08-19 | 20 | -16.11% |
| since 2026-08-21 | 11 | +1.69% |
| since 2026-08-23 | 11 | +1.69% |

Nine trades move the result from -16% to +2%. No floor is defensible on that evidence.

The operator also learned, and accepted as knowledge only, that sub-30k coins produce 6 of the 22
fills that returned +50% or more — 27% of the whole book's large tail. Sub-30k wins 41% of the
time. The band loses because the entry cost is high, not because the coins fail to move.

---

## 4. The two gates

### Gate A — structural cost

- **Quantity:** `route_price / display_price`.
- **Question:** is this pool unusually expensive for a coin of this size?
- **Threshold:** ONE GLOBAL WIDE BOUND. Do NOT build a per-band percentile table.
  The quantity and the bar are different things. `route_price / display_price` is computable for
  the coin in front of us and needs no history. Only the BAR needs history, and the history is too
  thin: each band holds 14 to 28 closes under the current configuration, and the band curve is
  monotone across the lifetime sample and NOT monotone since 2026-08-23. A rolling per-band
  percentile over 14 to 28 rows would track noise inside a live money gate.
  The bound catches the rare catastrophe, not the marginal case. The precedent is the
  execution-premium firing distribution, which is bimodal: survivors reach at most 1.136x and
  refusals start at 2.14x, with nothing between. A wide bound in that gap needs no tuning and
  cannot be curve-fit.
- **Missing input:** `display_price` is absent on 29.3% of attempts. The gate fails OPEN, which
  matches the chase-guard convention already used in this codebase.

### Gate B — chase

- **Quantity:** `display_price / source_price`.
- **Question:** did the coin move up since the trader bought?
- **Threshold:** approximately flat across bands, because the measured value moves only from
  -11.0% to -3.3% between the smallest and largest bands.
- **Missing input:** the gate fails OPEN, as above.

### Why the split is safe to reason about

Each gate reads one quantity. Each gate answers one question. An operator can read a refusal and
know which of the two conditions caused it. The present refusal string cannot express this.

**WARNING:** the split changes admission in BOTH directions. It is not a loosening. Small coins
that today pass through two offsetting errors may fail Gate A. Large coins that today pass a
generous cap may fail Gate B.

---

## 5. Ceiling and corrupt data

- Set `MEMECOIN_REAL_MAX_MCAP_USD` to `500000`. The current value in `.env` is `250000`.
- **This change is nearly a no-op and the operator must be told so.** Armed wallets since
  2026-08-19, n=676 with a decision mcap: 226 attempts were refused `over_ceiling`, and only 8 of
  them (3.5%) sit between $250k and $500k. 218 stay blocked. The realized record in that band is
  too thin to read (lifetime n=14 +5.4%; since 08-19 n=2 -21.5%). The move carries almost no risk
  and it will NOT earn the evidence to move the ceiling again. §7 does that work, not §5.
- Do NOT add a corrupt-data bound at the ceiling. A $50M bound rejects 465 of 2779 over-ceiling
  attempts (16.73%). That is a strategy decision, not a data check.
- Add the corrupt-mcap rejection at mcap resolution instead. The corruption is 11 rows above $10B
  (0.40%), with a maximum of $1.53T. A resolution-level check survives any later ceiling change.

---

## 6. Move capacity — UNBLOCKED ON A SCHEDULE, not impossible.

The operator states the priority: enter only a market that shows enough volume for the move to
happen.

**This cannot be built today, and the reason is not the one we expected.** Three explanations were
proposed and tested during this design. The first two are wrong and the third is refuted.

### What we tested and what happened

1. **"The input is missing."** Partly true and not the blocker. `volume_alive_decision`
   (`sniper/services/memecoin_real_runner.py:3306`) already scales with mcap and its logic is
   correct. Coverage, on attempts that REACHED the gate (n=697) — most attempts never reach it,
   because `not_approved` and `unroutable_chain` refuse before the market read:

   | Fact | Present at the gate |
   |---|---|
   | `volume_h1` | 20.1% |
   | `liquidity` | 35.0% |
   | `effective_mcap` | 94.0% |
   | `regime_current_buyers` | 100.0% |

   The gate fails OPEN when the field is absent, so the requirement is unenforced on about 80% of
   entries. But repairing coverage does not help, for the reason in §6.3.

2. **"Use our own tape instead of the vendor."** REFUTED. The `regime_*` facts have 100% coverage
   and do not predict movement. Uncontrolled they appear to (`25.3 / 27.9 / 39.9`). Controlled for
   forward observation count the tertiles read `14.3 / 12.7 / 17.6` — no separation.
   **`regime_current_buyers` is a BUYER COUNT.** It was therefore maximally exposed to the density
   artifact described in §6.3: the outcome is a maximum over forward observations, so any feature
   correlated with the NUMBER of observations inherits a false signal. Forward-point count alone
   predicts the outcome at `16.5 / 23.4 / 53.0`. A future reader who reaches for "use our own tape"
   will reach for this field first. It is the wrong field, and the reason is mechanical.

3. **"Depth predicts movement, inversely."** REFUTED, and this one nearly reached the operator.
   Uncontrolled, thin pools appeared far more likely to move. Controlled for forward observation
   count, the effect looked stronger still (`thin 83.0% / mid 8.5% / deep 2.1%`, n=47 per cell).
   It is an artifact of the outcome measure. **89.3% of `memecoin_events` rows are buys**
   (all time; 90.4% since 2026-08-19 — do not mix the two figures). The
   outcome was `max(their_price)` over forward trader fills. In a thin pool every buy moves the
   price up, so a max over a buy-dominated tape ratchets upward hardest exactly where depth is
   lowest. The measure substantially reports the price impact of the observed trades, with depth
   in its denominator by construction. "Thin pools move" is close to a tautology under it.

### The real block, and why it lifted

The first version of this section said the outcome variable was not measurable from stored data.
**That was wrong, and the correction matters more than the error.** The argument was that
`memecoin_events` is 89.3% buys so it reports its own footprint, and that `memecoin_position_marks`
exists only for coins we opened. Both true. But a THIRD source was dismissed for the wrong reason.

`pool_ticks` in the local Solana tape is a two-sided on-chain swap record, not a copy feed:

| source | one side | other side |
|---|---|---|
| `memecoin_events` (copy feed) | buy 89.3% | sell 10.7% |
| `pool_ticks` (on-chain swaps) | quote-in 57.7% | quote-out 42.3% |

n=32,625 decodable swaps, zero undecodable. The footprint argument that killed the outcome measure
does NOT apply: this tape records both sides of the book. Every swap carries both legs, so
`|amount1 / amount0|` is a transacted price. Measured over 400 consecutive swaps on the busiest
pool across 12 minutes: min 6.3178e-05, median 6.4509e-05, max 6.4817e-05, max/min = 1.0259, zero
points more than 10x off the median. That is a clean price series.

**Why it was dismissed:** `pool_ticks` covered only the pools of coins we had already HELD, which
is the same selection bias that disqualified `marks.mid`. But that bias was never a property of the
tape. It was a property of what fed it, which resolved pools on position open. **The prewarm lane
(`scripts/memecoin_pool_prewarm.py`, committed 4d22032d) resolves pools on FIRST SIGHTING**, so the
tape now covers coins we never trade. The "depth-independent price tape on coins we did NOT trade"
that this section named as the prerequisite is not a thing to build. It was built the same day and
not recognised.

So the constraint is DATA COLLECTION TIME, not impossibility.

### What this does and does not give us

It gives LABELS. It does not give a signal. Nothing in this document says any feature predicts move
capacity — §6.2 and §6.3 refuted the two candidates we had, and those refutations stand unchanged.
What changed is only that the outcome a future feature would be scored against is now obtainable.

Still true, and none of it moved:

- No threshold can be honest until the lane has recorded observations against real entries.
- Enforcement remains a SEPARATE change, roughly a week out, justified by outcomes.
- `execution_premium_decision`'s 2.0x remains the only threshold in this codebase with a realized
  outcome anchor behind it, and it is the standard any new bar must meet.

### Pre-registration — the turnover ceiling, stated BEFORE the data lands

**Registered 2026-08-25, before any forward observation exists. Do not adjust this level after
seeing forward data; a level moved to fit its own result is not evidence.**

- **Quantity:** `turnover` from `flow_observation` — `gross_quote / reserve_quote` over the
  pre-entry window. Depth-anchored, so it needs no per-band constant.
- **Registered level:** **0.30.** Refuse ABOVE it. Direction: a BUSY pool is the refusal, not a
  quiet one.
- **Not shipped.** No code enforces this and none should until the criterion below is met.

**Evidence behind the direction** — three independent measurements, all agreeing:

| measurement | quiet | middling | busy |
|---|---|---|---|
| h1 turnover on realized PnL (n=55) | +1.8% | +3.4% | −7.9% |
| tape turnover tertiles (n=49) | +17.9% | −21.3% | −8.2% |
| ceiling sweep at 0.30 (n=49) | keeps 27, +$69.50 | — | blocks 22, −$116.92 |

The sweep is monotone: every level tested blocks a losing cohort and keeps a better one.

**Why 0.30 and not 0.15.** 0.15 keeps only 14 of 49 rows. That is too aggressive a cut to take on a
confounded sample, and the tighter level is the one most likely to be fitting noise.

**The floor is REFUSED, explicitly.** A distinct-buyer floor was tested at every level and blocks
PROFITABLE trades — the blocked cohorts are the best performers in the sample. The same shape
appears in the timecap closes: the three quietest (4, 5 and 8 makers) returned +2.5%, +9.4% and
−2.1%, while every large loser was busy (31 makers −29.9%, 18 makers −32.1%, 28 makers −23.9%,
27 makers −21.2%). Scaling a floor by mcap cannot rescue it, because the raw direction is wrong.
**Mcap normalisation is also already built** — turnover IS volume/mcap, which is what
`volume_alive_decision` already does.

**Two instrument limits that bound this claim:**

1. **The window measures AT-entry flow, not PRE-entry.** The pre-entry window returned ZERO
   coverage on all 49 rows because the tape did not exist before we bought. So part of what reads
   as "busy predicts a loss" may be the loss itself. Removing this confound is the entire purpose
   of the prewarm lane.
2. **`swaps_n` is CENSORED at 40** by `MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK`. Verified on the live
   tape: 54 buckets land on exactly 40, against 7 at 39 and 1 at 41. Any ceiling expressed in
   SWAP COUNT is therefore unmeasurable. `turnover` survives because it is anchored on reserve, and
   the censoring understates `gross_quote` on busy rows — so the true separation is WIDER than
   measured and a ceiling set on it is conservative by construction. **Do not raise the budget to
   fix this**; it costs RPC and buys nothing turnover does not already give.

**Evaluation criterion — when this may be shipped.** Not before ALL of:

1. The prewarm lane has run long enough for a PRE-entry window to be populated on funded entries.
2. At least 30 funded closes carry a pre-entry turnover reading.
3. The 0.30 split reproduces on that forward cohort with the same sign.

If the forward cohort contradicts it, this pre-registration is the record that says so. That is what
it is for.

### Prerequisite

The prewarm lane must be RUNNING. It is committed, tested, verified end to end, and DEFAULT OFF,
not in cron — the operator enabled it, saw the cost, and switched it off.

**Measured cost, armed-wallet-scoped, cron */15.** Stated as a RANGE: a mean would hide a 3x
spread, which is the same failure as a wrong point estimate in a smaller coat.

| | discovery | refresh | total/day |
|---|---|---|---|
| quiet (median occupancy) | 69 | 576 | **~645** |
| typical (p90) | 69 | 1,152 | **~1,221** |
| peak (max) | 69 | 1,920 | **~1,989** |

Against **~31,680/day as it actually ran unscoped**. Discovery is 23 NEW armed Solana mints/day at
~3 calls each — first sighting only, network-filtered exactly as the lane's own WHERE clause does.
An earlier estimate of 65/day counted ACTIVE mints, which includes coins resolved on a previous day
that discovery never pays for again.

**Discovery is 3-11% of the bill at every level; refresh is 89-97%.** So the cost levers are cron
cadence and `MEMECOIN_POOL_PREWARM_KEEP_WARM_MIN` — halving either roughly halves the total.
Removing discovery entirely would save under 100 calls/day and destroy the lane's purpose.

**A measurement lesson worth keeping, because it is the hardest shape we hit today.** Scoping the
discovery half alone produced `candidates=0 rpc_calls=0` — both TRUE, and both meaning that half had
worked perfectly. The bill barely moved, because refresh had silently become the entire cost. Every
other silent-success instance this day had a metric that was WRONG. This one had a metric that was
RIGHT AND INCOMPLETE, which is harder to catch: the number you are watching confirms your change
worked while the quantity you care about sits somewhere you are not looking. Nothing in this section can begin until the operator turns it on, because
until then the tape keeps covering only coins we already bought.

### What to do in the meantime

Do NOT tune the volume thresholds, do NOT swap the input to `regime_*`, and do NOT add a depth
gate. Depth measures COST, not capability, and Gate A already prices cost.

DO record. Every funded Solana decision now writes the pre-entry flow it saw to
`trace_json.substage_events` (`observe_pre_entry_flow`, commit f939019c). That is what turns
collection time into evidence.

## 7. The mcap shadow paper arm

Add one paper arm. The arm mirrors the funded entry decision exactly, on the same wallet signals,
with every filter applied — except the mcap ceiling. Any signal that funded rejects SOLELY for
market cap opens a realistic paper position in this arm.

The arm produces the evidence needed to move the ceiling again. It risks no money. Because §5 is
nearly a no-op, **this section carries the entire evidence load of the project.**

### Construction rules — these are not optional

1. The arm MUST call the SAME funded decision function, with the ceiling injected as a parameter.
   Do NOT write a parallel copy of the decision path.
2. The arm MUST carry a divergence check. The check compares the arm's admission set against
   funded on every signal that funded did NOT refuse for mcap. Any disagreement fails LOUDLY.
3. **Reason:** a shadow arm that drifts from what it shadows is worse than no arm, because it
   produces confident evidence about a strategy nobody runs. This already happened once —
   `finding-memecoin-funded-policy-has-no-paper-twin-2026-08-19` records that no paper arm
   reproduced `follow-30-50`, so the funded policy had no measurable twin for weeks.

---

## 8. Verification

1. Run `python3 -m pytest tests/ -q -k "memecoin and (gate or chase or entry)"`.
2. Run `python3 scripts/validate_schema_refs.py`.
3. Deploy to paper first. Land Gate A and Gate B on the copy path before the funded path.
4. Confirm the paper skip mix changes: `skipped_chase` must fall for sub-30k coins.
5. Deploy the funded path only after the paper path runs clean for a full day.
6. Deploy with a flat book and zero pending reservations, per the funded deploy rules.

---

## 9. Evidence limits — state these to any reader

- The current filter set has run since 2026-08-23. The book is +$34.46 across 72 closes
  (+1.42% per dollar). This is the first positive stretch. It is NOT proof.
- Every band carries 11 to 18 closes under the current configuration. No band number in this
  document is large enough to become a threshold.
- **No band is reliably profitable in any slice.** A better gate reduces the bleed. It does not
  create an edge.
- The structural cost curve is monotone across the lifetime sample (36.7 / 23.9 / 14.5 / 9.8) and
  is NOT monotone since 2026-08-23 (27.4 / 29.6 / 19.8, n=22/28/14). **This is why no per-band
  table can be trusted — see §4.** The band ordering is not stable, and 14 to 28 closes per band
  cannot support a rolling percentile. Use the single global bound.
- **Gate A's distribution is NOT cleanly bimodal on the current era, so the bimodal precedent in §4
  gives the shape of the argument but not a value.** route/display since 2026-08-19, n=140:
  p50 1.281, p90 2.141, p99 3.794, max 3.934. Filled rows reach p90 1.745 and max 3.868. Only ONE
  attempt sits above the highest value we actually filled. There is no empty gap to place a bound
  in. Any Gate A bound is therefore a judgement, not a value the data hands us.
- **Gate B may be nearly inert and this must be measured before it ships.** display/their since
  2026-08-19, n=139: p50 0.889, p95 1.161, p99 1.208, max 1.217. The chart is almost never far
  above the trader's fill, so a chase bound above about 1.22 would never fire. State the expected
  firing rate before deploying it, or the gate is decoration.
