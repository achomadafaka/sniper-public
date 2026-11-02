# Fast-lane decision-time features: design

Status: approved by Alpha 2026-10-14 with notes, folded in below: option A is in scope; every attempt that reaches classification is stamped, refusals included; the 20 µs p90 ship gate stays; option B is out and goes on the operator list. (Lambda, 2026-10-14.) Order: the operator via Alpha, "the possibility to apply our filters better". Scope from Alpha: 4663 first, then the Solana slot lane. Beta mirrors the schema on the BSC Flap lane. Nothing here changes a gate, a size, an exit or a funded contract.

## 1. The gap, measured

Gamma's loss-pattern universe (`docs/audits/2026-10-14-fastlane-misses/loss-patterns.md`) covers funded fills from 10-01 to 10-14T08:20:52Z. On the fast lanes, the decision-time features `facts.pair_age_s`, `facts.volume_m5`/`facts.volume_mcap` and `classification.lifecycle` are missing as follows:

| lane | fast fills | none of the three | all three |
|---|---|---|---|
| solana `slot_lane_processed` | 141 | 141 | 0 |
| 56 `bsc_flap_fast_lane` | 3 | 3 | 0 |
| 56 `bsc_fast_lane` / `bsc_v3_fast_lane` | 7 | 0 | 7 |
| 4663 `rh_fast_lane` / `rh_fast_lane_deposit` | 5 | 1 | 4 |

Why each lane has the gap:

- **4663, native routes:** these already classify with the lane's cached market stats (`LaneDispatch` passes `self.cache.stats(mint)` into `live.handle(..., stats=stats)`).
- **4663, the one gap:** a `pons_shared` route on 10-11 16:22. Shared routes call the funded hook without the cached stats, and the hook fetches stats only when the mcap is unresolved. That fill's mcap resolved from history (`mcap_basis=history`), so it never fetched them.
- **Momentum:** `classification.momentum_phase` is provider-based and `unknown` on every fast fill. The lanes' own-tape momentum already exists, in the classification's `facts.momentum_own` when the read was ready and otherwise in the trace substage `momentum_own_late`. On 4663 two fills have `climbing`. On Solana the own read mostly says `no_own_marks`: own marks exist only for coins we hold or paper, so a fresh coin has none.
- **Solana slot lane:** it classifies with `market_stats={"price": ...}` only, and holds no provider stats, pair creation time or m5 volume for a fresh coin. What it does hold is the source event's own reserves and, for PumpSwap, the pool cache.

## 2. Rules

1. **Measurement only.** The stamp lands in the attempt trace as the top-level `trace_json` key `decision_features`, written at finish (amended 2026-10-14 at build: a substage or `source_evidence` would be serialized into the shell-row INSERT before the send). It is never written into `classification_json`, `gate_inputs_json` or any funded contract.
   - Reason: on the slot lane, `classify_funded_entry` runs before reservation and its output is hashed into `funded_contract_json`. New facts there would change the contract bytes, and through the lifecycle label could reach clip-ceiling logic.
   - Nothing on the decision path reads `decision_features`.
2. **Held data only.** Every value is something the lane already has in memory at its decision instant, from a cache filled before the signal or from the source event itself. There is no new provider call, database read, lock or wait before dispatch.
3. **Unknown is not zero.** A value the lane does not hold is `null`, with a reason in `<field>_reason`. Never a default.
4. **Every value states its basis and its age.** `basis` names the source. `age_s` is the decision instant minus the source's own observation time, when that time is known.
5. **Stamp every attempt that reaches classification, refusals included** (Alpha): refused-buy features are what the filter study needs. **Stamp after the outcome when possible.** The values are captured at decision (immutable references), but the dict is built and marked when the attempt records its outcome. The fill path then pays only for keeping a reference.

## 3. Schema `decision_features_v1` (`trace_json.decision_features`)

| field | type | meaning |
|---|---|---|
| `schema` | str | `"decision_features_v1"` |
| `lane` | str | `rh_fast_lane`, `slot_lane` or `bsc_flap_fast_lane` |
| `decision_at` | ISO str | The attempt trace's own `decision_at` (its anchor), passed in at finish: 4663, the funded hook's decision instant; Solana, the slot classification's `decision_at`. The lane reads no clock for it. |
| `stats_source` | str or null | 4663 only: `lane_cache` (the lane's SafetyCache entry) or `inline_read` (the shared hook's own provider read on a cache miss). |
| `venue` | str or null | Solana only: the slot buy's venue. |
| `pair_age_s` | float or null | Seconds from pair or pool creation to `decision_at`. |
| `pair_age_basis` | str or null | `provider_pair_created_at` (cached provider stats) or `migration_observed` (Solana PumpSwap, section 4.2). |
| `volume_m5_usd`, `volume_h1_usd` | float or null | Provider volumes from the cached stats. |
| `stats_age_s` | float or null | Host time of the decision's stats read minus the cached stats' `fetched_at` (same host clock). |
| `liquidity_usd` | float or null | Liquidity in USD. |
| `liquidity_basis` | str or null | `provider_cached`, `pool_pre_trade_reserves` or `curve_post_trade_reserves`. |
| `lifecycle` | str or null | The classifier's own lifecycle rule (`lifecycle_for`) applied to `pair_age_s`: fresh or young. Null when the age is null (the age's reason) and past 86400 s (`runup_not_held`: revival vs mature needs the runup, which this stamp does not hold). |
| `curve_real_token_atomic`, `curve_real_quote_atomic` | int or null | Bonding-curve real reserves after the source trade, from the source event. |
| `curve_virtual_token_atomic`, `curve_virtual_quote_atomic` | int or null | Bonding-curve virtual reserves after the source trade, from the source event. |
| `pool_quote_reserve_atomic`, `pool_base_reserve_atomic` | int or null | AMM pool reserves before the source trade, from the source event. |
| `pool_virtual_quote_atomic` | int or null | PumpSwap pool virtual quote from the pool cache. It prices trades but is not liquidity. |
| `quote_mint`, `quote_decimals` | str, int or null | The quote asset of the reserves above. |
| `source_impact_pct` | float or null | `their_usd / liquidity_usd × 100`. Null when either input is null. |
| `<field>_reason` | str | Present only when the field is null: `not_held`, `stats_not_read` (the decision never read stats), `stats_not_cached` (absent or past the cache TTL; `SafetyCache.stats` cannot tell them apart without a second read), `not_in_stats`, `fetched_at_unknown`, `not_in_event`, `quote_usd_not_held` (a quote the lane holds no USD price for), `migration_not_observed`, `migration_after_decision`, `runup_not_held`. |

A capture that raises at finish is written as `{"schema": "decision_features_v1", "error": "<exception type>"}`; the trace and the result are unchanged.

There are no momentum fields here. Momentum already has a home: `facts.momentum_own` when ready at classification, `momentum_own_late` otherwise. Section 6 gives the reader rule.

Lifecycle reuse: the classifier's thresholds (`age <= 3600` fresh, `<= 86400` young, revival by runup, else mature) move into one pure helper in `memecoin_market_state` that both call. This is a behaviour-preserving extraction, pinned by the existing classifier tests.

## 4. Sources per lane

### 4.1 4663 rh fast lane (`memecoin_rh_fast_lane.py`)

| field | source held at decision | native routes | shared routes (v3/v4/pons/split) |
|---|---|---|---|
| `volume_m5_usd`, `volume_h1_usd`, `liquidity_usd`, `pair_age_s` | `SafetyCache.stats(mint, now=...)`, which returns `(stats, fetched_at)` within its TTL and is filled by the background safety worker | Already read before `live.handle`; reuse the same tuple (or the inline branch's re-read) | No extra read: the lane's `stats_fn` records what the hook's own read returned (`lane_cache` hit, or `inline_read` on a miss) |
| `stats_age_s` | host time of that read minus `fetched_at` | yes | yes |
| `lifecycle` | helper(`pair_age_s`) | yes | yes |
| `source_impact_pct` | the datagram's `paid_usd` / `liquidity_usd` | yes | yes |

How the stamp reaches the trace (as built):

- The lane sets `ev.decision_features = partial(from_cached_stats, lane="rh_fast_lane", held=..., their_usd=ev.their_size_usd)` on the `LaneEvent`.
- `EntryAttemptTrace.start` (through the hook's `_start_trace_best_effort`) keeps the reference without calling it; `finish` calls it with the trace's `decision_at` and writes `trace_json.decision_features`. A retry child trace started from the same event gets its own stamp.

On a stats miss, every provider field is null with `stats_not_cached`; a hook that never read stats gives `stats_not_read`.

### 4.2 Solana slot lane (`memecoin_slot_lane.py`)

Captured in `_reserve_and_execute` right after classification: the `buy` (SlotBuy, frozen), `facts['their_usd']`, `snap.sol_usd`, and for PumpSwap the lock-free `amm_pools.cached(pool)` and `amm_pools.migration_observed_at(pool)`, as `trace.note_decision_features(partial(from_slot_buy, ...))` inside a try/except that logs and never fails the entry. Built at `trace.finish`. Non-SOL curves (`virtual_sol_reserves == 0`, the `quote_curve_completed` rule) and non-SOL, non-USDC pools have `liquidity_usd` null with `quote_usd_not_held`.

| venue | liquidity / reserves | pair age | m5 / h1 |
|---|---|---|---|
| `pump_curve` | `curve_*` from the SlotBuy's post-trade virtual/real reserves; `liquidity_usd` = real quote × quote USD (basis `curve_post_trade_reserves`) | null, `not_in_event` (the TradeEvent has no create time) | null, `not_held` |
| `pump_amm` (pair age) | — | `migration_observed`: the corrected-UTC time the lane heard the migration (the Shyft signal's queue stamps it at put, the direct listener at its fan-in call), kept by `PumpSwapPoolCache.note_migration` under its existing lock and bounded by `store_cap`; only `on_migrated` writes it. A pool learned any other way (warm, miss-then-learn, candidates) is null with `migration_not_observed`. A restart empties it. | — |
| `pump_amm` | `pool_*` pre-trade reserves from the BuyEvent the lane already parses in `amm_market_facts`, plus the cached pool's quote mint and decimals; `liquidity_usd` = 2 × real pre-trade quote reserve × quote USD (basis `pool_pre_trade_reserves`); the virtual quote is stamped separately and never added | see the row above | null, `not_held` |
| `raydium_cpmm`, `raydium_launchlab`, `meteora_damm_v2`, `meteora_dbc` | only what each existing facts builder already returns; any field it does not compute is `not_held` | null, `not_held` | null, `not_held` |

This lane will still lack pair age and m5 after this change. Section 7 lists the two ways to get them; both need approval.

### 4.3 BSC Flap (Beta, mirrored)

From Beta's input:

- **Held:** the datagram (`paid_usd`, `source_price` upper bound, decimals, seq, `arrival_utc`) and the lane cache's `facts`/`facts_at`.
- **Not held today:** Portal `TokenBought.postPrice`/`fee` (decoded in the watcher but not forwarded), and Flap `curve_state` (read in the safety worker, then dropped).
- **Beta's options, Beta to decide with Alpha:** forwarding TokenBought words in the datagram, and keeping `curve_state`. Both need no new read.
- **Field names:** the same schema. Curve fields map to `curve_*` with Flap's own quote. Curve progress = `circulating_supply / dex_supply_thresh` goes in a lane-specific `curve_progress_basis`.

## 5. Edge cases (CLAUDE.md §5)

1. **Money path:** none. No gate, size or contract reads the substage. The lifecycle helper extraction is pinned by the existing classifier tests.
2. **Fill path speed:**
   - 4663: no extra cache read. Native routes keep the tuple already behind the hook's `cached`; shared routes record what the hook's own `stats_fn` read returned. Each adds one small dict and one `functools.partial`.
   - Solana: one reference capture before reservation; the dict is built after the outcome.
   - Required measurement: added CPU on the decision path, p50/p90, over the existing route tests and a 10,000-iteration microbenchmark. Ship only if p90 stays under 20 µs on the decision path.
3. **Threads:** only the cache's existing lock (4663) and frozen dataclasses or copies. No module-level state.
4. **Absent, zero, unknown:**
   - A cached stats entry with `volume_m5=0` is a known zero.
   - No cached entry is null with `stats_not_cached`.
   - An expired entry is null with `stats_not_cached` (the cache returns None past its TTL).
   - Reserves of 0 on a curve are known (a completed curve).
5. **Dependency failures:** none at stamp time; every source is in memory. A malformed event field becomes null with `not_in_event`. A stamp failure is logged and never changes the result.
6. **Units:**
   - Reserves stay atomic integers, with `quote_mint` and `quote_decimals` stamped beside them.
   - USD values use the decision's `sol_usd` or quote price, the same values the lane priced with.
   - Ages are seconds.
7. **Sample:** the stamp describes the coin at our decision, not after it. A reader must not mix a slow lane's post-GET stats with these without stratifying.
8. **Rollback:** revert the commit; nothing is persisted outside `trace_json`.

## 6. Reader rule (for Gamma's pre-registered cells)

- **P10 pair age, P11 m5:** the slow lanes read classification facts. The fast lanes read `decision_features.pair_age_s` and `volume_m5_usd` (and `volume_mcap` from classification facts). A null with a reason counts as unknown.
- **P7 lifecycle:** classification for the slow lanes, `decision_features.lifecycle` for the fast lanes.
- **P6 momentum:** classification `momentum_phase` (provider) on the slow lanes. For own-tape momentum on any lane, read `facts.momentum_own.phase` when its reason is `ok`, else the `momentum_own_late` substage's phase. Own-tape momentum and provider momentum are different measures; never pool them into one cell.
- **Stratification:** `decision_features.lane` together with `delivery_lane`.

## 7. Not in this change (each needs its own approval)

- **A. PumpSwap migration-observed time:** approved by Alpha and moved into section 4.2.
- **B. Immutable creation time after dispatch:** out (Alpha); it is on the operator list. A pair's creation time cannot change, so reading it after the send is not hindsight, but it is one off-path provider call per fast fill. m5 volume read after the send would be hindsight and is never proposed.

## 8. Tests

- **4663:**
  - A shared-route signal with cached stats stamps `decision_features` with the cached `pair_age_s`, `volume_m5_usd` and `stats_age_s`.
  - Without cached stats, the fields are null with `stats_not_cached`.
  - The funded hook's gate inputs are identical with and without the stamp (no behaviour change).
- **Solana:**
  - A pump_curve and a pump_amm slot-lane attempt stamp the event reserves and `liquidity_usd`, and the classification JSON is byte-identical to today's.
  - A refusal before classification stamps nothing, and nothing fails.
- **Refusals:** a slot-lane refusal after classification (e.g. `slot_lane_scaled_clip`) and a 4663 hook refusal both stamp `decision_features`.
- **Migration age:** a pool offered by the migration stream stamps `pair_age_s` with basis `migration_observed`; a pool learned only through `learn` stamps null with `migration_not_observed`.
- **The check that fails if the gap returns:** a fast-lane attempt fixture whose trace lacks `decision_features` fails the test.
- **Ship gate:** added decision-path CPU p50/p90, measured on the real route tests plus a microbenchmark, goes in the commit body; p90 must be under 20 µs.
