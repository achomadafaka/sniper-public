# Refused-mint forward capture — design

**Date:** 2026-08-26 · **Author:** Gamma · **Review:** Alpha · **Status:** BUILT 2026-08-26, commit
`a17f507d`, **default OFF**. Enabling it is an operator/`.env` action and a §5.2 baseline is its
pre-condition. Sections marked MEASURED carry build-time findings that correct this document's own
assumptions; where they conflict with the original text, the measurement wins.

**Two deviations from this design shipped, both deliberate and both in the module docstring:**

1. **Enqueue reads the durable refusal record** (`memecoin_entry_attempts`), it is not an in-process
   hook on the funded entry path. Every refusal is already persisted with its mint, origin and exact
   reason, so a read-only poll sees the same event stream while adding **zero code to the funded
   entry path**. That converts §1 rule 1's independence from a convention into a structural fact. The
   cost is that a capture starts up to one poll interval after the refusal, which shows up honestly
   as a first `age_s` of a second or two and is never backfilled.
2. **It yields to the funded runner on Jupiter**, which this design did not require because it
   assumed the RPC pool was binding. See §3.

## Why this is first-class, not a replay accessory

`docs/audits/2026-08-26-memecoin-entry-gate-saved-cost.md` (and its ERRATUM) established that **no
forward exact-route series is persisted anywhere in the system, for any coin, held or refused.**

- `memecoin_position_marks` looks route-shaped and is not: `executable_exit_value` equals `mid` on
  all 3,246,299 rows, and `route_quote_id` is NULL or empty on all 54,519 rows since 2026-08-23. Its
  sources are `dexscreener`, `geckoterminal`, `fastlane_*`, `web`.
- `memecoin_quote_ledger` is dexscreener/geckoterminal and its unique index `(mint, ts_minute)` caps
  it at one row per minute per mint.
- `memecoin_positions.route_quote_value` is populated on **0 of 105** current-era funded rows.
  `executable_peak_price` (90 of 105) is a ratcheted scalar, not a series.

Exact route quotes **are computed** on the funded exit path and then discarded. Beta's finding that
the historical replay is demoted to ordered comparisons with a ±10–20 point band follows from the
same root cause. This capture is therefore the only proposed instrument that would produce
route-space forward evidence, and it should be designed as the primary one.

## 1. Trigger and scope

**Enqueue on:** a refusal by a **live strategy gate**, identified by EXACT `terminal_reason`, from
the inventory in the step-2 audit: m5 turnover floor, m5 turnover ceiling, execution premium, chase
past-their-entry, chase first-seen, losing re-entry, re-entry cooldown.

**Never enqueue on:** plumbing and roster refusals — `not_approved`, `unroutable_chain`, `disarmed`,
staleness, `insufficient bankroll`, `insufficient SOL`, `one position per coin`, `source buy under
floor`. Those say nothing about the coin.

**Never enqueue on** the retired mcap band (bare `over_ceiling` / `under_floor`). Gate identity is
the exact string; a prefix match merges a retired gate with a live one, which is the defect the
step-2 audit had to correct.

**Identity and dedupe:** one capture per `(network_id, mint, origin_event_id)`. `origin_event_id` is
the decision unit — the step-2 audit measured 803 refused attempts against 803 distinct origins, so
attempts and decisions coincide today, but the key must be the origin so a future retry path cannot
silently double-capture.

**Scope guard:** Solana only in v1. Chains are never pooled and no other chain currently carries an
armed wallet.

### Composition with the revival watch

`sniper/services/memecoin_revival_watch.py` consumes **the same refusal event stream** for m5-floor
refusals. The two must not depend on each other:

| | revival watch | this capture |
|---|---|---|
| purpose | delayed re-ask for a real entry | research evidence |
| horizon | 30s window, 10s poll | ~21 min |
| instrument | DexScreener `stats_fn`, the same one that refused | exact route quote |
| authority | none — re-injects through the full gate stack | none — never touches admission |
| scope | m5 floor only, sub-100k mcap | all live strategy gates |

Rules:

1. **One producer, two independent consumers.** The refusal hook emits the event; each subscriber
   decides for itself. Neither blocks, waits for, or reads the other's state. If the capture is at
   capacity the watch still runs, and if the watch is disabled the capture still runs.
2. **Hand off on clearance, never double-quote.** If a revival watch clears and the re-injected event
   becomes a funded entry, the capture for that `origin_event_id` **stops** and records
   `handed_off_to_position`, with the position id. From that moment the coin is a held position and
   the funded mark lane owns it. Two quote streams on one mint would compete for the same provider
   budget and produce two disagreeing series for one coin.
3. **A cleared-but-still-refused re-injection is a NEW decision.** It carries a new
   `origin_event_id`, so it enqueues its own capture. That is correct: the second refusal is a
   different judgement at a different time.
4. **The capture must not become the watch's instrument.** The watch judges clearance with the
   instrument that refused, deliberately. Feeding it route quotes would let it admit on a
   disagreement between two data sources. This design does not expose a read path to it.

## 2026-09-11 amount correction

For `roundtrip_cap`, the worker reads the exact reservation amount linked to the refused entry attempt.
The first buy quote uses that amount. Each later sell quote uses the exact quantity from that buy quote.
An absent or invalid reservation amount produces `not_captured_size_unknown`. The worker does not substitute the configured observation amount.
Other refusal families retain their configured amount, except for the September 16 extension below.
Historical $20 captures cannot validate a refused $175 trade.
Independent quotes do not simulate a buy followed by a sell after reserve changes. Cap assessment must retain this limit.
This correction changes observation size only. It does not change funded admission, cadence, provider capacity, or the capture horizon.

## 2026-09-16 evidence correction

The reservation-size requirement also applies to impact, chart premium, source premium, and quote-chase refusals.
An unknown size produces `not_captured_size_unknown`. The worker never substitutes $20 for these families.
The classifier keeps `route_over_mid`, `route_over_source`, `chase_quote`, and `evm_chase_quote` separate.
EVM observations remain display marks. Their presence does not establish an executable return.

Solana chart-premium captures use a uniform five-second cadence across the existing capture horizon.
Other families retain their configured cadence. Capacity limits, separate provider credentials, and provider-pressure pauses remain active.
This change increases quote demand only for the chart-premium family. Missing observations remain gaps.
The September 9–16 audit found 16 chart-premium refusals, before duplicate and evidence exclusions.
At an 810-second horizon, five-second cadence requires about 162 observations instead of 32 at 25 seconds.
Old quotes cannot reconstruct a refusal-time quote at another size. Reports must separate old and new evidence.

## 2. Scheduler — uniform cadence to the rail, capacity refusal, never silent degradation

Peak concurrency is the binding constraint, not daily volume: **23 distinct refused mints/day**, each
needing 5s quotes for the whole rail window from its refusal, and refusals overlap.

**Cadence: uniform 5s for the whole rideable life. No tiers.** (Operator directive, 2026-08-26:
"only capture the hypothetical 13 min the position would ride so we get full history.")

The horizon is **`MEMECOIN_REAL_MAX_HOLD_S`**, currently **780s**. This is not a percentile — it is
the **timecap rail**. Under the funded exit rails no position can ride past it, so a rails simulation
over this data can never need a price beyond it. Capture to the rail and the series covers the entire
life a simulated position could have, at full resolution, with nothing to label.

Evidence that the rail is the right bound (`memecoin_positions`, current era, n=105):

| exit reason | n | median hold | max hold |
|---|---|---|---|
| `real_tp` | 49 | 106s | 694s |
| `b_stop` | 25 | 265s | 738s |
| `b_timecap` | 19 | 617s | 788s |
| `manual` | 11 | 198s | **1259s** |
| `catastrophe` | 1 | 926s | 926s |

Every automatic exit lands at or under the rail; `b_timecap` clusters at 780–788s, which is the rail
firing plus completion slop. The only rows past it are one **manual** exit at 1259s — an operator's
hand, which a rails simulation cannot and should not reproduce — and one catastrophe at 926s.

**An earlier draft of this spec used a 1259s horizon taken from `max(hold)`. That was wrong**: the
maximum of a distribution whose tail is human intervention is not a rail, and building a tiered
cadence to reach it degraded the tail for no simulable gain. Same instrument error as the rest of
this file's ERRATUM lineage — a number that was arithmetically correct about the wrong quantity.

**Bind the horizon to the config value, never hardcode 780.** `MEMECOIN_REAL_MAX_HOLD_S` has already
moved 600 → 2592000 → 600 inside a single day. A hardcoded horizon would silently desync from the
rail it exists to mirror, and the desync would be invisible in the data. Read it at capture start and
store it on the row (`horizon_s`) so every series records the rail it was cut to.

**Settle margin:** capture to `MEMECOIN_REAL_MAX_HOLD_S + 30s`. The rail *decides* at the horizon;
the 30s lets a simulation also price the exit at realistic latency rather than assuming an
instantaneous fill.

Cost per mint: 810s ÷ 5s = **162 quotes**, against 124 for the tiered draft and 251 for a flat 5s
over the old 1259s horizon. At 23 mints/day that is **≈3,730 quotes/day, 0.04 req/s average** — a 26%
increase over the tiered draft, for the removal of the entire fidelity-tier problem. Cheap trade.

**What this buys, and it is the point of the change.** There is no `tier` column, no reduced-fidelity
tail, and no caveat travelling with any downstream estimate. Every capture is one uniform instrument
at the resolution the 5-sweep 5-second stop confirm requires
(`MEMECOIN_REAL_STOP_CONFIRM_SWEEPS=5`, `MEMECOIN_REAL_STOP_CONFIRM_SECONDS=5`), across the whole
window a position could occupy. A simulated stop is reproducible wherever it fires.

**Accepted exclusion: fault-lengthened holds are not reproduced, and that is correct.** The evidence
table above shows one AUTOMATIC exit past the rail — the catastrophe at 926s, position 44624, which
falls inside the worst mark-blind window Beta measured on 2026-08-24 (87 blind minutes, zero alerts,
because the rails-blind streak counter incremented inside the sweep and a sweep that does not run
cannot count). Degraded conditions can hold a position past `MEMECOIN_REAL_MAX_HOLD_S` **because the
rails themselves were starved of data**. That is a fault, not policy. A capture that stopped at the
rail will not reproduce such a hold, and it should not: simulating policy against a fault-lengthened
hold blends a policy question with an outage. A future reader who finds the 926s row and sees this
capture stop at 810s is looking at a deliberate exclusion, not a bug.

**What it gives up, stated:** the capture cannot answer "what would this coin have done if held
longer than the rail". That is not a question the funded system can act on while the rail stands. If
the timecap is ever raised, the horizon follows it automatically by the binding above, but series
captured under the old rail stay short and must not be pooled with longer ones — `horizon_s` on every
row is what makes that checkable.

**Completeness is checked from OUTSIDE the capture loop.** Every `outcome` value in §4 is written by
the capture worker itself, so none of them can report the worker not running. A capture that stalls,
hangs, or dies mid-series simply stops writing rows, and a consumer counting those rows cannot tell
"the coin stopped quoting" from "we stopped asking". This is the failure that hid 87 mark-blind
minutes on 2026-08-24: a streak counter incremented inside the sweep, and a sweep that does not run
cannot count itself as missing.

The expected observation count is deterministic — `horizon_s / cadence_s`, currently 162. So a
**separate reaper**, on its own clock, scans captures whose `refused_at + horizon_s` has passed and
compares actual rows against expected. A shortfall is stamped `truncated_worker` with both counts.
The trigger is **staleness** — a capture that should have finished and has not — never a failure
tally the worker would have had to be alive to increment.

No estimate may consume a capture that has not been reaped, and every downstream number reports its
complete/truncated split alongside the capacity and provider censoring.

**Capacity: refuse to start, never thin an in-flight capture.** A global concurrency cap
(`MEMECOIN_CAPTURE_MAX_CONCURRENT`) bounds simultaneous captures. On overflow the capture is **not
started**, and a row is written with `outcome='not_captured_capacity'` carrying the mint, the gate,
and the timestamp.

Rationale, and this is the whole point of the choice: a queue that delays a capture produces a series
that starts late and silently misses the first minutes, which are exactly the minutes the stop
semantics need. Thinning an in-flight capture produces a series whose cadence varies with unrelated
load, so its fidelity is unknowable after the fact. **Refusing outright makes the censoring explicit,
per-mint, and countable.** On busy days the honest output is "n captured, m refused for capacity",
and any downstream estimate must report both.

## 3. Key pool — disjoint from funded, by assertion not by convention

**Constraint origin.** On 2026-08-21 the on-chain watcher hit a Helius usage cap and hammered the
429 every 2 seconds — roughly 7,000 requests over 4 hours — on `MEMECOIN_SOLANA_RPC_URL`, **the
funded runner's own shared quota** (fixed `22ffcbaf`). The same day, an RPC key edit caused a 20
minute funded outage (06:26–06:46Z) because the funded pool requires distinct URLs. Research load on
a funded key has already cost funded uptime twice. This design treats that as a hard boundary.

**Design:**

- Own variable, `MEMECOIN_CAPTURE_RPC_POOL_URLS`, read only by the capture worker.
- **Reuse `RotatingSolanaRpcHttp`** (`sniper/execution/solana_rpc_pool.py:148`). It already rotates
  on `-32429`, benches an exhausted endpoint, and re-admits it after a read-only genesis probe. It
  is the behaviour this needs and a second implementation would drift from it.
- **Startup assertion:** the capture URL set must be **disjoint** from custody, the funded pool, TPU,
  and research URLs. Overlap fails startup loudly. Convention is not sufficient — the 08-21 outage
  happened because a key appeared in two roles.
- **Degradation is silence, never borrowing.** If every capture endpoint is benched, captures record
  `outcome='not_captured_provider'` and stop. The worker never falls back to a funded endpoint.

### MEASURED 2026-08-26 (build, commit `a17f507d`) — the question above is settled, and the answer inverts this section's premise

The section above predicted the RPC pool was not the binding dependency. It is not, and the margin is
larger than the prediction implied. Real requests, through the placed capture key, against eight real
refused mints:

| | per observation | per mint, for the whole capture |
|---|---|---|
| Jupiter GET | 1 | 162 |
| Solana RPC | 0 | 1 (`getTokenSupply`, decimals — immutable, cached) |

Latency, warm: Jupiter p50 59 ms / p95 77 ms; RPC 150 ms. All 8 refused mints quoted successfully —
refused coins **are** routable, which was not guaranteed for a population the volume floor rejected.
At the §2 horizon the ratio is **162 Jupiter : 1 RPC**.

**So the key disjointness in this section protects 1/162 of the load, and the side that matters has
no key to make disjoint.** Jupiter takes no API key, no tip and no subscription; it rate-limits **by
IP** (`solana_real_seams.py`, live-verified 2026-08-14 when `lite-api` answered 429 to every request
from this host for over an hour, cost live entries RATIO and boden, and left position 37108
rails-blind for 700 sweeps at −67%). The funded runner quotes from the same two hosts on the same IP,
and `_jup_host_cooldown` is **process-local**, so a separate capture process cannot see the funded
runner's benching and would keep asking a host funded has already given up on.

The startup assertion still holds and still ships — it is correct, it is cheap, and the 2026-08-21
outages it was written for were real. It is simply not sufficient on its own.

**Load, measured rather than assumed.** Capture triggers, by the shipped exact-identity matcher:
**18.3/day over 7 days, but 54 on 2026-08-25** — so 2,960/day average against ~8,750 on a burst day.
Funded Jupiter load could not be measured retrospectively, because **it is not logged or persisted
anywhere** (which is itself the §5.2 finding: the baseline must be instrumented, it cannot be mined).
Its bound from config and exposure: `MEMECOIN_EXACT_QUOTE_PER_MINUTE=8` over 2.4 position-hours of
open exposure in 24 h, plus 47 entry-stage quotes — order 1,200/day, likely well under. **The capture
would therefore be the dominant Jupiter consumer on this IP: roughly 2.5× funded on an average day
and ~7× on a burst day.** That is the opposite of the "small research add-on" this design assumed.

**The mitigation, and why it is behavioural rather than structural.** There is no key to separate, so
the only lever is conduct. The capture is research; nothing waits on it; it may lose observations at
no cost. It therefore **yields**: any Jupiter 429 it observes shuts it for
`MEMECOIN_CAPTURE_JUPITER_YIELD_S`, it never retries, and it never fails over to the second Jupiter
host — that host is the one funded falls back to. Lost observations are censoring, and censoring is
reported alongside every downstream number, exactly like the capacity refusals in §2.

**Rejected: pinning the capture to `api.jup.ag` so it never touches funded's primary.** Whether the
two hosts hold separate quota buckets is unverified, and the only way to verify it is to drive one to
429 deliberately — which would bench a host the funded runner needs. Not testable at acceptable risk
on a live funded system, so not claimed.

**Still unmeasured, and it should gate any cadence increase:** whether the capture's own load can
provoke a 429 that funded then pays for. The yield bounds the damage after the first one; it does not
prove the first one will not happen. §5.2's 24 h baseline is now the pre-condition for enabling, not
merely for attribution.

## 4. Storage

Proposed table `memecoin_refusal_captures`, one **immutable** row per quote observation. Rows are
append-only: no update path, no backfill of a missed observation, no interpolation. A gap is a gap.

<!-- schema:external=memecoin_refusal_captures -->

| column | type | note |
|---|---|---|
| `id` | INTEGER | PK |
| `capture_id` | TEXT | one per `(network_id, mint, origin_event_id)` |
| `origin_event_id` | TEXT | the decision that triggered it |
| `network_id` | TEXT | solana in v1; never pooled |
| `mint` | TEXT | |
| `gate` | TEXT | exact live-gate identity from the step-2 inventory |
| `refused_at` | TEXT | decision time, the series origin |
| `observed_at` | TEXT | this quote's own time; never inherited from a retry |
| `age_s` | REAL | `observed_at - refused_at`, the simulation clock |
| `horizon_s` | REAL | the `MEMECOIN_REAL_MAX_HOLD_S` rail this series was cut to, plus settle margin. Series captured under different rails are never pooled. |
| `quote_price` | REAL | exact route, output per input unit |
| `quote_input_usd` | REAL | the hypothetical clip priced |
| `quote_route_target` | TEXT | which route produced it |
| `quote_id` | TEXT | provider quote identity |
| `provider` | TEXT | |
| `endpoint_index` | INTEGER | which pool slot served it, for §5 |
| `latency_ms` | REAL | for §5 |
| `http_status` | INTEGER | for §5 |
| `quality` | TEXT | `ok`, `provider_error`, `no_route`, `timeout` |
| `expected_obs` | INTEGER | `horizon_s / cadence_s` at capture start — the reaper's denominator, fixed before any observation exists |
| `outcome` | TEXT | terminal per capture: `complete`, `handed_off_to_position`, `not_captured_capacity`, `not_captured_provider`, `truncated_provider`, `truncated_worker` (reaper-assigned, see §2) |

Index `(network_id, mint, refused_at)` and `(capture_id, age_s)`. **Any filter used by a consumer
must appear in an index** — an unindexed predicate is a table scan, which cost 46ms per call on a
136k-row table in the 08-23 incident. Register the table with `scripts/brain_index.py generate` and
keep `scripts/validate_schema_refs.py` clean before the schema is used in any query.

`observed_at` is stamped per observation and never inherited, because an inherited timestamp cannot
separate a repeat from a duplicate — `feedback/check-the-instrument-not-the-arithmetic-2026-08-24`.

## 5. Cost measurement plan

The step-2 audit refused to estimate per-quote RPC cost and said the number must come from runtime.
This is how the build produces it, wired from day one rather than added later.

1. **Counters, per request, already columns above:** `provider`, `endpoint_index`, `http_status`,
   `latency_ms`. Aggregate hourly into a small `capture_provider_stats` rollup: requests, error
   count by class, p50/p95 latency, and rotations observed.
2. **Baseline first.** Before the capture is enabled, record 24h of the funded pool's own request
   rate and rotation count. Without a before, an after cannot attribute anything.
3. **Steady-state window:** 7 days at production trigger rates, not a burst test. Report requests/day
   per endpoint, observed rate-limit responses, and the ratio of capture load to funded load.
4. **The number that decides the pool size** is peak concurrent in-flight quotes per minute, not the
   daily total — §2 establishes burstiness as the constraint.
5. **Publish the measurement before any threshold is derived from the captured data.** If the
   capture is itself rate-limited, its series have gaps, and a gap-bearing series must not be used
   for a stop-fidelity simulation without saying so.

## 6. Pre-registered use, and the rule on thresholds

**This data may be used to answer:**

0. **Volume share on refused mints (added 2026-08-26, Alpha).** The funded sell-share cap ships on
   DexScreener transaction **counts**, because pool resolution happens after entry on 100% of funded
   positions and a tape-based volume share is therefore unavailable at decision time. Counts are
   blind to size by construction: over 820 local-tape windows the two measures agree on only 81.5% of
   300 s windows at the 80 cap, and the disagreement is asymmetric — 117 windows where volume refuses
   and count passes against 35 the other way, with the worst case a 0.167 count share against a 0.996
   volume share (one whale sell, many dust buys). This capture is the only instrument that would
   carry real volume shares for refused mints, so it can eventually measure the count-versus-volume
   relationship the shipped cap has to assume.

1. **Gate saved/cost.** The refusal counterfactual the step-2 audit could not compute: rails-simulate
   an entry at the route price the gate saw and walk the captured series under the funded exit rails.
   Valid **only** for captures with `outcome='complete'`, and any estimate must report the capacity
   and provider censoring alongside it.
2. **Revival-watch validation.** For m5-floor refusals the capture spans the watch's 30s window, so
   it can measure what happened to coins the watch did and did not clear — without ever feeding the
   watch, per §1 rule 4.
3. **Whether route and chart diverge, and by how much.** Currently unmeasurable, because no route
   series exists. This is the first instrument that could quantify the 2.078x display gap recorded
   in `feedback/counterfactual-needs-realized-outcomes-2026-08-23`.

**This data may NOT be used to:** change a gate, cap, or clip on its own; feed the revival watch;
or grade a wallet.

### Thresholds come from outcomes, not from distribution shapes

The step-2 audit measured refusal margins clustering at 1.40–1.53x their caps, and that clustering is
**what any threshold on a continuous variable produces**. It is not evidence a cap is wrong. The
precedent that governs here is the premium cap: it sits where it does because entries at **≥2.0x
measured −52% over n=18** — a realized outcome cliff — and the 2026-08-25 attempt to move it to 1.15
on distribution reasoning held lifetime at +1.06% but was **−8.08%** on the recent window.

So: **no threshold derived from this capture may rest on where the distribution bunches.** It must
rest on a measured outcome difference across the candidate threshold, day-clustered, with its
censoring stated. A percentile is not a reason.

## Out of scope for v1

EVM chains. Feeding any live decision. Backfill of historical refusals — the series does not exist
and cannot be reconstructed.

**Persisting the funded path's own exact route quotes is out of scope here and is OWNED ELSEWHERE.**
Those quotes are already computed on the funded exit path and discarded, so persisting them is a
separate and cheaper win. Alpha assigned it to Beta's replay design on 2026-08-26, as a fail-soft
component with zero marginal API cost. Do not build it from this document — duplicating it would put
two writers on one series.

The two instruments are complementary and neither replaces the other: **held** coins get a true route
series from that persistence work, **refused** coins get one only from this capture. The held-side
series is also the calibration set for §6.3 — route-versus-chart divergence can be quantified on
coins where both a route series and a chart series exist, and that calibration is what makes the
refused-side numbers interpretable.
