# Memecoin Post-Buy-Pump Instrumentation — Design

**Date:** 2026-07-07
**Status:** Draft (autonomous build — decisions documented inline per operator full-autonomy directive)
**Follows:** `2026-07-07-memecoin-selection-data-upgrade-design.md`. Motivated by `finding-memecoin-pool-arm-mapping-refuted` — hold is a weak per-trader proxy; the *actual* front-run edge is "does this trader's large buy pump after they buy?", which we don't measure.

---

## Goal

Measure, per large-buy sighting, the token's **forward price path after the buy**, and aggregate it into a **per-trader pump profile** ("when this trader makes a large buy, does price pump — how much, how fast, how often"). This is the direct front-run edge signal, replacing the hold/coverage proxies for *selection*. Display-only first; a gate signal only once validated (same discipline as `fomo_realized_held`).

Non-goal: this does **not** measure our net capturable copy-PnL (that's the arms). It measures the trader's-buy → peak move as a **selection** signal.

---

## Why forward-only

We never recorded post-buy price paths, and DexScreener gives no reliable per-mint second-resolution history. So capture is **forward**: on each new large-buy sighting, record the reference price, then poll the token price at horizon marks as time passes. No backfill. The profile accrues as sightings mature. Copied sightings already get position peak-tracking (`time_to_peak_s`/`max_gain_pct`); this closes the gap for **non-copied** discovery sightings and unifies the measure across both.

---

## Design

### Reference price

`ref_price = event.their_price` — the price the trader bought at, from the fomo event. It is unit-comparable to DexScreener `priceUsd` (the copy chase-guard already compares the two). Post-buy pump `= (peak_mark / their_price − 1) × 100`.

Honesty note recorded in the spec and surfaced in the UI tooltip: `their_price` is the trader's fill, so this measures the **full** buy→peak move — it slightly overstates what we'd net-capture (we enter ~0.3–0.8 s later, after some of the move). It is a consistent **ranking** signal across traders, not a net-edge estimate.

Sightings with no `their_price` are skipped (no reference).

### Horizon marks

Config `MEMECOIN_PUMP_HORIZONS_S = [60, 300, 900, 3600]` (~1m / 5m / 15m / 60m). The 1m/5m marks match our fast front-run capture; 60m is the "did it keep going" tail. Each mark's price is filled the first time the capture pass runs at-or-after `buy_ts + horizon`; the **actual** age at fill is stored (`*_age_s`) so a mark polled late is honest, not silently mis-timed. Marks are sampled, so `max_gain` is a lower bound on the true intra-window peak — documented, acceptable for a ranking signal.

### Storage — `memecoin_signal_outcomes` (new table)

<!-- schema:external=memecoin_signal_outcomes -->

One row per large-buy sighting (keyed by `fomo_event_id`, dedup):
- `fomo_event_id` (PK), `source_user_id`, `handle`, `mint`, `ticker`, `buy_ts`, `ref_price`, `their_size_usd`
- `p_1m`, `p_5m`, `p_15m`, `p_60m` (nullable — filled as marks come due), and `p_1m_age_s` … `p_60m_age_s`
- `max_gain_pct`, `peak_horizon_s` (which mark peaked), `last_price`, `status` ('open' | 'matured')
- `created_at`, `updated_at`
- `max_gain_pct` / `peak_horizon_s` recomputed each fill from the filled marks; `status='matured'` once the last horizon is filled (or the 60 m mark is > `MEMECOIN_PUMP_ABANDON_S` overdue → mature with what we have, so a dead/dust token doesn't stay open forever).

### Capture

1. **Create** — in `run_once`, when a buy sighting is newly captured (`insert_event` True) and `their_size_usd ≥ MEMECOIN_DISCOVERY_MIN_USD`, insert an outcome row with `ref_price = their_price`, `buy_ts = created_at`, `status='open'`. Piggybacks on the existing discovery capture; no extra fomo call.
2. **Fill** — a `pump_capture_pass(conn, now_iso, stats_batch_fn)` runs on a cadence (~30 s, alongside the bracket pass): select open outcomes with an unfilled mark whose due time has passed, **batch** the DexScreener price for those mints (`dexscreener_stats_batch`, ≤30/call), fill each due mark with the current `priceUsd` + its actual age, recompute `max_gain_pct`/`peak_horizon_s`, and mature rows whose final mark filled (or abandoned). Fail-closed: a missing quote leaves the mark null and retries next pass.

### Aggregation — `trader_pump_profile(conn, min_signals=…)`

Per trader over **matured** outcomes: `n_signals`, `median_max_gain_pct`, `mean_max_gain_pct`, `pump_hit_rate` (fraction with `max_gain_pct ≥ MEMECOIN_PUMP_HIT_PCT`, default 20), `median_peak_horizon_s`, `p90_max_gain_pct`, `median_1m_gain`/`median_5m_gain` (early-move read). This is the front-run selection read: high median gain + high hit-rate = their buys move price.

### Surfacing

- Analyzer/Traders/Discovery: a `pump` column — `median_max_gain% (hit-rate%, n)`, greyed when `n < min_signals`. Display-only.
- **Not** a gate signal until validated against forward copy-PnL (operator's "add it if provable"). The gate stays lifetime/7d/30d/fresh.

---

## Testing

- `pump_capture_pass`: fixture outcomes at various ages + a stub batch price fn → assert only due marks fill, ages recorded, `max_gain_pct`/`peak_horizon_s` correct, maturation on last mark, abandonment path, missing-quote retry (mark stays null).
- Create path: a new buy sighting ≥ min$ inserts one outcome (dedup on fomo_event_id); < min$ or sell → none.
- `trader_pump_profile`: known matured outcomes → correct median/mean/hit-rate/n; excludes open rows; respects `min_signals`.
- Ref-price honesty: outcome with no `their_price` → skipped.

---

## Config (new, env-tunable in `settings/memecoin.py`)

`MEMECOIN_PUMP_ENABLED=1`, `MEMECOIN_PUMP_HORIZONS_S=60,300,900,3600`, `MEMECOIN_PUMP_HIT_PCT=20`, `MEMECOIN_PUMP_CAPTURE_PERIOD_S=30`, `MEMECOIN_PUMP_ABANDON_S=1800` (mature an overdue 60 m mark after +30 m so dust tokens don't linger), `MEMECOIN_PUMP_MIN_SIGNALS=5`.
