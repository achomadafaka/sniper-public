# Source-impact chase allowance on 4663 — design

Date: 2026-09-02
Author: Beta
Status: DESIGN AND MEASURE ONLY. No wiring. Widens a live funded gate; the operator moves it, not us.
Requested by: Alpha, after the operator said raising the flat chase cap "wouldn't solve anything"
and asked for "more wiggle room depending on the size of the coin, the size of the signal".

## The mechanism already exists and has never once fired

`source_impact_pct` + `effective_chase_cap` (`memecoin_real_runner.py:5022`, added 2026-08-29 at the
operator's request) widen the chase cap per event by the copied trade's own measured footprint.
`MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT=25` is set and live.

    populated on 27 of 21,838 attempts
    chase_decided_by = base_cap on ALL 27. Zero allowances ever granted.

## Why: THREE blockers, and the third is a BUILD not a removal

Corrected 2026-09-02 after Gamma's review. This section previously said two, which implied that
removing a guard and repointing a quote function would open the path. It would not.

1. **The Solana-only guard.** `memecoin_real_runner.py:5078` —
   `if network != "solana": return None, IMPACT_NOT_SOLANA`.
2. **The quote function is hardcoded Jupiter.** At the call site the caller passes
   `quote_fn=lambda usd: jupiter_entry_quote(ev.mint, usd)` regardless of chain. Removing the guard
   alone would call Jupiter for a 4663 token and get nothing.
3. **`source_impact_pct` cannot consume an EVM quote at all.** It does not read a quote; it reads
   ONE FIELD — `float(quote.get("priceImpactPct"))`. Verified across `sniper/`: that key is produced
   by Jupiter and by Raydium (normalized percent→fraction at `solana_real_seams.py:385`) and by **no
   EVM provider**. `dex_v3_quote`, `dex_v4_quote`, `dex_v4_multihop_quote`, `dex_pons_v2_quote` and
   `dex_relay_quote` return `amount_out` plus calldata, shaped for the venue executor; grep for any
   impact field across `sniper/execution/evm_*.py` returns nothing.

   So repointing `quote_fn` at a 4663 quoter yields `quote.get("priceImpactPct") is None`,
   `float(None)` raises, and the function returns `IMPACT_UNPARSEABLE`. **The refusal population
   moves from `not_solana` at 22 of 22 to `unparseable_impact` at 100% — a different label on the
   same zero**, and one that reads like a provider defect rather than a missing component, which is
   worse for whoever debugs it.

An EVM impact estimator has to be WRITTEN. Blockers 1 and 2 are edits; blocker 3 is a component.

### The derivation, and why it must not reach for an external mid

Do NOT divide against a DexScreener mid. That mixes two providers with different pool selection and
different timestamps, and the cross-source mix is the defect class that produced the WETH/USD error
on 2026-07-30.

Quote the source's size and a DUST size **on the same pool, in the same batch**:

    impact = price_at_size / price_at_dust - 1

One provider, one book, one instant, no external mid, and it needs no field a quoter refuses to
emit. The 4663 quote path is already JSON-RPC batched and a batch of 4 costs 1.15x a single call
(Gamma, 2026-08-30), so the second quote is close to free — it is the same round trip.

### One good consequence, and it shrinks the transfer problem

That derivation returns the **pool-price move**, which is the same quantity `model_impact_pct`
estimates. So the 4663 pairing would be exact-quote-pool-move against constant-product-floor-
pool-move: **same baseline, not merely the same space.**

That is a CLEANER pairing than the Solana one, where a pool-move floor is graded against Jupiter's
realized route loss including fees and hops. On 4663 the baseline caveat below would not apply, and
`model >= quote` becomes straightforwardly interpretable instead of the informative exception.

It also shrinks the transfer problem raised above: you would not be carrying a Solana calibration
onto a different pool shape. You would be grading 4663 against 4663.

## The inputs exist on 4663

| input | 4663 | note |
|---|---|---|
| `their_size_usd` | **100% populated** (n=6,827, 30d) | the signal-size input is there |
| `mcap` | 59.2% | **not needed** — impact measures depth directly, so the allowance sidesteps the 40% mcap gap |

`chase_source_size_usd` reading 0.1% is not a missing input. That telemetry is written only on the
would-refuse path, so it is a REFUSAL count, not an evaluation count.

## Cost: the extra quotes land only where we were already refusing

The call site is inside `if ... not chase_ok(...)`, and its own comment says so: *"WOULD-REFUSE ONLY.
The extra quote fires here and nowhere else, so the accept path's quote budget is unchanged —
measured at 4.3 of these a day."* Entry latency on the accept path is untouched.

**CORRECTED 2026-09-02.** An earlier draft of this section read "22 times since 2026-08-03, about
0.7/day". The denominator was invented. The gate cannot have fired on 4663 before 2026-08-30,
because `FOMO_TO_DEX` had no 4663 entry until then, so `our_price` was `None` and `chase_ok`
fail-opened on every 4663 signal. The 22 rows span **2026-08-30T19:49 to 2026-09-01T04:42 — 32.9
hours**, not 29 days.

The rate is not a rate. It is bursty and coin-driven:

| day | 4663 attempts | chase refusals |
|---|---|---|
| 2026-08-30 | 602 | 2 |
| 2026-08-31 | 1,320 | **19** |
| 2026-09-01 | 1,377 | 1 |
| 2026-09-02 (to 11:30Z) | 306 | 0 |

19 of 22 land on one day, and within it they cluster: 8 CINEMA rows inside 22 minutes, 4 Rabbit rows
inside 23 minutes. A single-number daily rate over this window is meaningless; the planning figure
is the **burst**, not the mean. Worst observed burst is 19/day.

Note the population: only ~12% of 4663 attempts reach a market-data read at all (`gate_inputs_json`
present on 171/1,377 on 09-01), because `not_approved` refuses ~1,045/day before that stage. The
chase gate's evaluation population is roughly 170/day, not 1,377.

## The 4663 impact estimate

Jupiter returns `priceImpactPct` (a lost-input-value fraction) and the code converts it to a paid
premium as `i / (1 - i)`. The 4663 ladder returns only `buyAmount` for a `sellAmount`, so the
equivalent must be derived from two quotes:

    p_ref = buyAmount(ref_usd)  / ref_usd        unit price at a small reference size (~mid)
    p_src = buyAmount(size_usd) / size_usd       unit price at the SOURCE's size
    premium_pct = (p_ref / p_src - 1) * 100

That is algebraically the same quantity `i/(1-i)` produces, without the intermediate: if
`p_src/p_ref = 1 - i` then `p_ref/p_src - 1 = i/(1-i)`. Same formula constant, so
`chase_impact_formula` must record a DIFFERENT value for the derived variant — the registered
control recomputes historical refusals and must not grade two estimators as one.

## The saturation check, which the derived version needs its own version of

Jupiter's sentinel is a `priceImpactPct` of exactly 1 that does not move with size (measured: '1' at
$499, $100 and $10). The existing guard rejects it because *a value that does not move with size is
a sentinel, not a measurement*.

A derived impact has the same failure mode and no sentinel value to look for: a ladder that returns
a cached or size-independent `buyAmount` yields a premium that is pure artifact of the ratio. So the
derived estimator needs **three** points, not two, and must require the premium to increase
monotonically with size. A premium that does not move between `ref`, `size/2` and `size` is
unusable and must return a distinct skip reason rather than an allowance.

Cost is therefore 3 quotes on the would-refuse path, against Jupiter's 1.

## What it would buy — UPPER BOUND, 9 rows but only 6 COINS

Applying `base_cap + the full 25-point ceiling` to every recorded 4663 chase refusal:

    ADMITTED  9 rows   Rabbit x4 (15.7-38.4%, source $6.0k-$15.5k), PF, ARCADS, S'NEW, Meowdani, POOPIE
    REFUSED  13 rows   HENT 58%, NVision 65%, HOUSE 83%, CINEMA x8 at 301-422%

**Count coins, not rows.** The 22 refusals are 12 distinct mints; the 9 admissible rows are **6
distinct mints**. The four Rabbit rows are one coin (`0xcd1cca2b3d…`) hit by three different armed
handles inside 23 minutes. Under the one-position-per-coin rule they yield **one** position, not
four. So the upper bound in funded positions is **6 over 26 hours**, against `MEMECOIN_MAX_CONCURRENT=3`
and `MEMECOIN_MAX_TOTAL_EXPOSURE_USD=700`.

**And 6 is still an upper bound that cannot be reached.** The real allowance is
`min(measured_impact, ceiling)`, so the true count is <= 6. It assumes every one of those coins
would have measured a >=25% source footprint, which is exactly what has never been measured on 4663.

### The Rabbit cluster is not the clean case — it is the one with no guard behind it

    Pasterniq    $9,308   gap 23.5%   20:08
    smol_intern  $5,985   gap 15.7%   20:30
    smol_intern  $11,128  gap 19.7%   20:30
    smol_intern  $15,455  gap 38.4%   20:31

Three handles, $41.9k of copied buying, one coin, 23 minutes. That is the single largest beneficiary
of this change — and per the 2026-08-30 parity audit, **cluster admission does not run on 4663**
(the EVM branch returns before the cluster block). Widening the chase cap here admits a coordinated
multi-wallet pile with nothing checking coordination.

**Shipping the widening alone would sell the operator a size-aware cap and deliver an unguarded
cluster admission.** That is not a side effect. It is the single largest measured beneficiary of the
change, and it is the argument that decides the order.

~~Cluster admission on 4663 therefore lands **before or with** this change.~~
**PREREQUISITE WITHDRAWN, 2026-09-02.** The ruling was wrong in both directions at once: the
prerequisite was larger than stated and the risk it guarded was smaller.

**It was never a port.** Cluster admission has never made a decision on ANY chain — 0 of 21,865
attempts across seven chains since 08-01 have any cluster column populated.
`MEMECOIN_REAL_CLUSTER_GATE_ENABLED` defaults to `"0"`, is absent from `.env` and from the running
process's environment, no socket exists at `data/memecoin_cluster_gate.sock`, and no worker runs.
`_cluster_gate_enabled()` takes no chain argument — it is one global boolean — and
`memecoin_real_leg.py:164` returns `evm_entry_fn(...)` before the cluster block at 203. So "turn the
crowd check on for 4663" is really: first-ever production activation of an unproven gate on the
chain carrying most of the book, PLUS an EVM path build. Lines 208-209 fail closed
(`cluster_client_missing`), so a bare flag flip STOPS funded Solana buys rather than passing them.

**And the risk is smaller than the Rabbit cluster made it look.** One-position-per-coin caps a
coordinated pile at ONE position, and the funded record confirms the cap holds:

| funded positions since 2026-08-01 | n |
|---|---|
| positions | 737 |
| distinct coins | 537 |
| coins traded more than once (ever) | 101 |
| coins with a genuinely CONCURRENT second position | **2** |
| …of those, genuine MULTI-WALLET piles | **0** |

Both concurrent pairs (`7ssJZGFT3twGqe…`, `EUB1eZBt4m3X4F…`, both 2026-08-13) share one
`source_user_id`, carry near-identical notionals, and close at the same instant with exit reason
`real_tp_shared_balance`. They are one wallet's re-entry booked as two rows and exited together, not
two wallets piling in.

*Read this as confirmation the cap works, not as a measure of how often piles are attempted.* The
admitted record cannot show attempted concurrency, because one-position-per-coin is what prevents
it; the refusal record (the Rabbit four) is where attempts appear.

A coordinated pile therefore yields one position, and one position at an elevated premium is the
ordinary risk the chase cap already exists to price. That does not justify a first-ever activation
of an unproven gate on the busiest funded chain.

**What replaces it is measurement, not a gate:** record multi-handle coincidence on the refusal path
beside the impact telemetry — how many distinct source handles bought this mint in the preceding
window. Then "is an admitted coin a crowd event" is a readable column instead of an argument. No new
network call; absence stored as NULL. Suggestion, not a ruling, and behind the operator's word on
the chase lane either way.

The operator may accept or override any of this; it is theirs, not ours.

CINEMA at 300-422% is correctly refused under any allowance.

## Why the flat cap is the wrong instrument (Alpha's data, restated)

Drift by coin size, filled attempts since 08-01: mcap <50k p90 **+51.7%**; 50-250k p90 +39.5%;
250k-1M p90 +15.4%; >1M p90 **+14.7%**. One 17% threshold is punitive below 250k and essentially
never binds above 1M. Raising the flat number moves the arbitrary line rather than removing it.

## A cheaper estimator already exists in the tree — measure before building the quote ladder

`sniper/execution/solana_quote.py:211` already computes this quantity:

```python
def slippage_frac(notional, liquidity, k=None, cap=None):
    """Modeled price impact of a paper fill: fraction of mid the fill moves against
    us. Constant-product-ish FLOOR = k*notional/liquidity, capped. Unknown/zero
    liquidity -> cap (fail conservative). Real impact is usually >= this."""
```

`slippage_frac(ev.their_size_usd, st["liquidity"])` takes two inputs the chase gate **already holds
at the moment it refuses**. It costs no quote, no reference size, no extra latency, and no provider
load. If it is good enough, decisions 1 and 2 both disappear.

Three reasons it is not an obvious drop-in, in order of severity:

1. **Its unknown-liquidity sentinel inverts for this caller.** Returning `cap` (0.5) is conservative
   for a paper fill — it makes the modeled fill worse. For an allowance it is conservative in the
   WRONG direction: absent liquidity would grant the maximum allowance, clamped to the 25-point
   ceiling, exactly when we know least. Missing liquidity must mean **no allowance**, matching the
   existing `IMPACT_NO_SIZE` / skip-reason convention. A helper reused across a change of caller
   keeps its failure direction, and that direction was chosen for someone else.
2. **`k=1.0` and `cap=0.5` are paper-fill calibration.** They are load-bearing constants carried
   into a funded gate that never justified them — the same objection Alpha raised against reusing
   the 25-point ceiling across a change of estimator. Own knob, provisional, ungraded.
3. **Constant-product is the wrong model for 4663.** `k*notional/liquidity` is the V2 formula. 4663
   funded entries run the verified **V3** path, and some pools are V4 on our own PoolManager. Under
   concentrated liquidity the same TVL can carry wildly different depth at the current tick, so the
   model's error on this chain specifically is unknown and could be large in either direction.

**So do not choose between them by argument. Record both and compare.**

## The telemetry gap that blocks decision 1, found while trying to answer it

I tried to pick the reference size from stored rows rather than by spending live quotes. It cannot be
done today, and the reason is the subject of this document's own correction.

`gate_inputs_json` carries `liquidity` on **345 of 346** 4663 rows since 08-30. That looked like
ample coverage. It is written at `memecoin_real_runner.py:1961`, **after** the chase gate at 1736 —
so it exists only for attempts the chase gate let through. On the 22 chase refusals, the population
the reference size would actually be used on, liquidity is available on **0 of 22**.

A 99.7% population rate measured on the wrong side of the branch. Third instance today of
[[finding-a-refusal-only-column-is-not-a-coverage-metric-2026-09-02]], and this one landed on the
fix rather than on the thing being fixed.

Liquidity IS in `st` when the chase gate runs — `dexscreener_stats` parses `liquidity.usd`, and the
2026-08-30 parity audit measured it returning on 4663. It is simply never persisted on the refusal
path.

### The one change worth making now

Measurement-only, no behaviour change, no new network call. On the chase refusal path, record:

- `liquidity` from `st` at decision time
- the `slippage_frac` model estimate for `their_size_usd` against it

Both are free at that point. After a week of rows, decision 1 (reference size) and decision 2
(three quotes) are answered from the real distribution of the refused population instead of from my
judgement — and on any row where a quote-derived estimate later exists, the model can be graded
against it directly on 4663's own V3/V4 pools.

This is the remedy named in the brain entry above: record WHY on the refusal path, and record that
the gate evaluated at all. Every number in this document's cost and benefit sections had to be
reconstructed because that was not done.

## On 4663 the model estimate is not a second opinion — it is the only one (Gamma review, 2026-09-02)

The telemetry commit `07b15249` was written to record two estimates "so the two can be graded
against each other on real rows". On 4663 that pairing does not exist and will not exist under
current code.

`source_impact_pct` returns `(None, IMPACT_NOT_SOLANA)` at
`memecoin_real_runner.py:5078` before it does anything else, so the quote-derived estimate is
unreachable off Solana. The stored rows agree exactly:

| chain | skip reason | n |
|---|---|---|
| 4663 | `not_solana` | **22** |
| solana | `ok` | 3 |
| solana | `saturated_impact` | 1 |
| solana | (null) | 1 |

`chase_source_impact_pct` is populated on **3 rows all time, every one Solana**. All 22 refusals in
the 08-30 → 09-01 window are 4663 and all 22 read `not_solana`.

**Consequences, in order:**

1. On 4663 today, `model_impact_pct` is the ONLY estimator that can grant an allowance. The commit's
   stated purpose understates it.
2. The paired grading it describes is possible only on Solana, where the population is **3**.
3. **Precision on "structurally cannot".** It cannot fire on 4663 *as currently coded*. That is
   exactly what this design proposes to change — blocker 1 is that `not_solana` guard and blocker 2
   is the hardcoded `jupiter_entry_quote` at the call site. Both must move together. So the quote
   path is not permanently closed to 4663; it is closed until this design ships, and the telemetry
   accumulates in the meantime under code where it stays closed.
4. **The transfer problem is new and nobody had written it down.** Any calibration graded on the
   Solana rows must then be carried to 4663, and 4663 pools are Uniswap V4 on our own PoolManager
   (Gamma, 2026-08-30 — GeckoTerminal labels them `pons-v2-dex`), not the AMM shape the Solana rows
   describe. That transfer needs its own justification and does not come free with the estimator.

### Same space, different baseline — for whoever grades this

Both columns are paid-premium percent, but they do not measure the same thing:

- `chase_model_impact_pct` is a constant-product **floor on the pool-price move**.
- `chase_source_impact_pct` is the **realized route loss** on the current book, including fees and
  any second hop.

So `model < quote` is the EXPECTED result, and a row where `model >= quote` is the informative one.
A grader who has not been told this will read a systematic gap as a defect.

## Decisions handed back — none of these are mine

1. **Reference size.** `ref_usd` sets the mid. Too small and the quote is noise or fails a minimum;
   too large and it carries its own impact and understates the premium. Needs measuring per chain,
   and it is a funded-gate parameter.
2. **Three quotes on the refusal path** is a latency and provider-load decision on a chain already
   under scrutiny. Bounded by the would-refuse path, but the bound is a **19/day burst**, not
   0.7/day — and the bursts are exactly the moments the route providers are busiest. Two mitigations
   worth pricing: the 4663 quote path is already JSON-RPC batched (a batch of 4 measured at 1.15x
   the cost of one call), so the three sizes should go out as one batch; and note that Jupiter is
   not the provider here, so this spends the 4663 route budget, not the funded Solana quota.
3. **Does the ceiling stay 25 on 4663?** It was chosen against Jupiter-measured Solana impacts. A
   derived estimator with different error is not obviously entitled to the same clamp.
4. **A distinct `chase_impact_formula` value for the derived variant**, so the registered control
   never grades two estimators as one.

## What this does NOT claim

That any of the 9 would have been profitable. Whether admitting them is +EV is a separate question
and the evidence does not exist: Alpha has already ruled NO on the ">17% drift band is +EV" result
(n=17, mean +$5.07, sd $61.92, t=0.34, and removing one +$169.78 trade flips the mean negative).
This design makes a size-aware cap POSSIBLE on 4663. It does not argue the cap should be looser.
