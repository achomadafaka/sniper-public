# Spec — Copy-Latency Decay (capture + `copy_latency_surface`)

**Date:** 2026-05-22
**Status:** design — ready for implementation
**Origin:** Item 5 of the analyzer "shortsightedness" backlog. Feasibility
established in `docs/audits/2026-05-22-copy-latency-feasibility.md`: the
current `price_history_lookups` is too sparse to measure short-horizon
drift, so this needs a capture-layer change, not a pure surface.

## 1. Problem

A wallet with excellent entry timing can still offer **zero copyable
edge**. By the time a copier observes the wallet's trade (poll the data
API → decide → submit — minutes later) and acts, the price has moved. The
analyzer measures the wallet's *own* entry but never asks how much of that
entry advantage **survives to a copier entering `latency` minutes later**.
Copy-latency decay measures exactly that.

## 2. Scope

One feature, two coupled pieces:
1. **Capture-layer change** — the price-history capture additionally
   records spot prices on a **post-entry grid** (`entry`, `entry+5m`,
   `entry+15m`, `entry+60m`) per closed position, as a **separate
   `fidelity=1` capture pass** into a new `WalletData.copy_latency_prices`
   field. It is NOT folded into `price_history_lookups`: that dict carries
   H6/FH6 prices at the default (coarse) fidelity, and a single
   `"<asset>|<ts>"` key cannot safely hold two fidelities for the same
   `(asset, ts)`.
2. **Display-only surface** `copy_latency_surface` — a pure function of
   the captured grid; measures per-horizon edge decay.

**Out of scope:** sub-minute latency (below the price-history API's
`fidelity_minutes` granularity); order-book depth / slippage modelling;
**running a live re-capture** (live API — operator-gated). The surface
ships **dormant**: it reports `data_unavailable` on every pre-grid fixture
and only produces real output after an operator runs a capture with the
grid enabled.

## 3. Latency horizons

`HORIZONS_MIN = (5, 15, 60)` — minutes. Minute-grained because the
price-history API's finest fidelity is `fidelity_minutes`; sub-minute is
physically unmeasurable. 5 min ≈ a fast automated copier; 60 min ≈ a slow
/ manual one. **+15 min is the primary horizon** (a realistic poll +
decide + submit cycle) — it drives the headline label. Three horizons
keep the added capture cost bounded.

## 4. Capture-layer change

The price-history capture (`scripts/record_fetch_snapshot.py`,
`_capture_price_history_lookups`) resolves a set of `(asset, ts)` pairs
into a dict keyed `"<asset>|<ts>"`.

- A new pure helper `copy_latency_grid_pairs(closed_positions,
  activity_rows) -> set[tuple[str, int]]` computes the grid: for each
  closed position, find its **first BUY** in `activity_rows` (earliest by
  timestamp, matched on `conditionId`+`asset`) → `(asset, entry_ts)`; emit
  `(asset, entry_ts)` and `(asset, entry_ts + h*60)` for each `h` in
  `HORIZONS_MIN`.
- The grid pairs are resolved by a **separate `_capture_price_history_lookups`
  pass** whose `price_fn` is built with **`fidelity=1`** — the finest
  resolution, matching the minute-scale-drift precedent in
  `scripts/execution_profile.py`. The result lands in the new
  `WalletData.copy_latency_prices` field. The H6/FH6 capture into
  `price_history_lookups` is unchanged (its own fidelity, its own pass).
- Gated by `--no-copy-latency-grid` (default: **on**; opt-out for cheap
  captures) — mirrors `--no-price-history` / `--no-execution-profile`.
  When `--no-price-history` is set the grid is skipped too (it shares the
  price-history machinery).
- `replay_surface_meta["copy_latency_grid"]` is written **after** the
  grid pass completes — status (`captured` / `partial` / `skipped` /
  `failed`), source, `n_pairs`, `n_entries`, `n_missing` — and is marked
  `failed` in the pass's exception path. It is never written `captured`
  before the lookups resolve.
- **Cost:** up to `n_closed × 4` additional point lookups per capture,
  resolved concurrently via `--price-history-workers`. This is the
  operator-weighed tradeoff named in the feasibility memo. The batch-range
  API (`ledger/price_api.fetch_batch_price_history`) is the v2
  optimization if capture wall-time becomes the bottleneck — explicitly
  deferred; v1 reuses the existing point-lookup path.

`copy_latency_prices` is a new `WalletData` / fixture field, serialized by
`replay.py` alongside `price_history_lookups`. Pre-grid fixtures lack it
(`None`); the surface degrades (§7).

## 5. Surface — `compute_copy_latency_surface()`

`sniper/analysis/wallet_protocol/copy_latency.py`:

```
compute_copy_latency_surface(closed_positions, activity_rows,
                             copy_latency_prices) -> dict
```

Pure; no I/O; deterministic. `copy_latency_prices` is the fidelity=1 grid
captured per §4. Same key derivation as §4 (`copy_latency` owns the shared
`copy_latency_grid_pairs` helper; the capture imports it).

### 5.1 Eligibility

A closed position is eligible when:
- it has a first-BUY entry in `activity_rows` → `(asset, entry_ts)`;
- it is **resolved** — `curPrice >= 0.97` or `<= 0.03`;
- it is **binary-priced** — `avgPrice` strictly in `(0.02, 0.98)`;
- `copy_latency_prices` contains the **entry spot** key
  `"<asset>|<entry_ts>"` with a non-`None` value that is **also strictly
  in `(0.02, 0.98)`** — a near-certain entry spot (≈0/≈1) makes
  `roi_entry` tiny and `edge_retained` noisy, so it is not eligible.
  (The horizon spot `p_h` is *not* band-bounded — a horizon spot near 1.0
  is a legitimate "edge fully decayed" signal, not noise. But a horizon
  spot **outside `(0, 1]`** — `<= 0` or `> 1`, e.g. negative-risk /
  synthetic-short weirdness — is dropped for that horizon and raises an
  `OUT_OF_RANGE_HORIZON_PRICE` caveat, since it would feed nonsense into
  `roi`.)

A position resolving none of the post-entry-horizon keys still counts as
eligible-but-uncovered (feeds degradation).

### 5.2 Per-position, per-horizon metric

For an eligible position, `won = curPrice >= 0.97`. For each horizon `h`
whose key `"<asset>|<entry_ts + h*60>"` resolves to a non-`None` price:
- `p_entry` = entry spot; `p_h` = the horizon spot.
- `drift_h = p_h − p_entry` (signed; positive = price rose = adverse for
  a copier buying the wallet's side).
- `roi(p, won) = (1 − p)/p if won else −1.0` — resolved-binary
  hold-to-resolution ROI (same arithmetic as `null_baseline_surface`).
- `roi_entry = roi(p_entry, won)`; `roi_h = roi(p_h, won)`.
- `decay_h = roi_entry − roi_h` — ROI a copier loses to `h` minutes of
  latency (≥ 0 when latency hurts, < 0 when post-entry drift *helps* the
  copier; exactly 0 on losers — latency only costs you on winners).

### 5.3 Aggregation

Per horizon `h`, over the **matched set** (positions with both `p_entry`
and `p_h` — entry baseline and copier figure computed on the *same* set,
so the comparison is apples-to-apples):
- `n` — matched positions.
- `mean_drift` — mean `drift_h`.
- `entry_ew_roi` — mean `roi_entry` over the matched set.
- `copier_ew_roi` — mean `roi_h`.
- `mean_decay` — mean `decay_h` (= `entry_ew_roi − copier_ew_roi`).
- `edge_retained` — `copier_ew_roi / entry_ew_roi` **only when
  `entry_ew_roi > 0`**; else `None` (you cannot "retain" a non-positive
  edge).

### 5.4 Headline `copyability` label

From the **+15 min** primary horizon:
- `inconclusive` — `data_state != "ok"`, the +15 m horizon is absent, its
  `n` is below `MIN_PER_HORIZON`, or its `edge_retained is None`.
- `copyable` — `edge_retained >= 0.50` (a copier 15 min late keeps at
  least half the entry edge).
- `edge_decays` — `edge_retained < 0.50`.

Rendered as "edge survives / decays at copy latency," never a trade
recommendation.

## 6. Surface schema

`report.copy_latency_surface: dict | None` — `None` only on the
pre-surface backward-compat path. When produced:

| Key | Type | Meaning |
|---|---|---|
| `surface_version` | int | schema version (1) |
| `data_state` | str | `ok` \| `proxy_only` \| `underpowered` \| `data_unavailable` (§7) |
| `copyability` | str | `copyable` \| `edge_decays` \| `inconclusive` (§5.4) |
| `primary_horizon_min` | int | 15 |
| `n_eligible` | int | positions eligible per §5.1 |
| `n_total_closed` | int | all closed positions |
| `horizons` | list[dict] | one per `HORIZONS_MIN`: `{horizon_min, n, mean_drift, entry_ew_roi, copier_ew_roi, mean_decay, edge_retained}` |
| `caveats` | list[str] | surface-local honesty caveats (NOT `data_quality_flags`) |

Numeric fields are `None` (unknown), never a false `0.0`.

## 7. `data_state` — worst-wins

Severity order (worst wins): `data_unavailable` > `underpowered` >
`proxy_only` > `ok`. Constants: `MIN_ELIGIBLE = 20`,
`MIN_PER_HORIZON = 10`, `COVERAGE_FLOOR = 0.50`.

- `data_unavailable` — no closed positions, OR no eligible position has
  **any** usable post-entry-horizon price. If no horizon keys exist, caveat
  `no_post_entry_grid_captured` marks the dormant / pre-grid case. If horizon
  keys exist but all were rejected as out-of-range, caveats include
  `all_horizon_prices_out_of_range` and `OUT_OF_RANGE_HORIZON_PRICE`.
- `underpowered` — `n_eligible < MIN_ELIGIBLE`, OR the +15 m horizon has
  `n < MIN_PER_HORIZON`.
- `proxy_only` — the +15 m horizon's matched `n` is below
  `COVERAGE_FLOOR × n_eligible` (the grid resolved for only a slice — the
  decay figure sees a subset). Adds a named caveat.
- `ok` — none of the above.

## 8. Hard wall — non-negotiable

Identical to the four shipped sibling surfaces:
- **Display-only.** The surface dict is read ONLY by the report renderer.
  It MUST NEVER feed `overall_verdict`, `gate_result`,
  `primary_edge_type`, `copyability_note`, branch/hypothesis logic, or the
  Strategy Pattern Audit. (Despite the field being named `copyability`, it
  is display text — it does not touch `copyability_note`.)
- **No `data_quality_flags` append.** Caveats live in `caveats`.
- **No live I/O in the surface.** `compute_copy_latency_surface` is a pure
  function of already-captured data. (The *capture* §4 does I/O — that is
  the capture layer, run operator-gated, not the surface.)
- `types.py` carries the field with the standard hard-wall comment.

## 9. Wiring

- `types.py` — `copy_latency_surface: dict | None = None` on
  `ProtocolReport`, hard-wall comment.
- `analyze_wallet.py` — one call alongside the `null_baseline_surface`
  call, fed `data.closed_positions`, `data.activity_rows`,
  `data.copy_latency_prices`.
- `report.py` — a `## Copy-latency decay` markdown block; the rendered
  Note states it is display-only and that, until a grid-enabled capture
  is run, the surface reads `data_unavailable`.
- `scripts/record_fetch_snapshot.py` — a separate `fidelity=1` capture
  pass over `copy_latency_grid_pairs` into `data.copy_latency_prices`;
  `--no-copy-latency-grid` flag; `replay_surface_meta` entry written
  after the pass.
- `types.py` / `replay.py` — `copy_latency_prices` field + its
  save/load serialization.

## 10. Acceptance tests

**`tests/test_wallet_protocol_copy_latency_surface.py`** (surface):
1. **Edge decays** — winners whose +h spot rose above entry spot →
   positive `mean_decay`, `edge_retained < 0.5`, `copyability =
   edge_decays`.
2. **Copyable** — winners whose +h spot ≈ entry spot → `edge_retained ≥
   0.5`, `copyability = copyable`.
3. **Favorable drift** — +h spot *below* entry spot → `mean_decay < 0`
   (latency helped); no crash.
4. **Losers contribute zero decay** — a lost position → `decay_h = 0` at
   every horizon (roi is −1 either way).
5. **Dormant / pre-grid fixture** — `copy_latency_prices` has entry
   keys but no `+h` keys → `data_state = data_unavailable`, `copyability
   = inconclusive`.
6. **Partial horizon coverage** — +5 m present for all, +15 m for a
   minority → +15 m `proxy_only` path, caveat present.
7. **Eligibility** — unresolved (`curPrice` mid) and out-of-band
   `avgPrice` positions excluded from `n_eligible`.
8. **`edge_retained` guard** — when a horizon's `entry_ew_roi <= 0`,
   `edge_retained is None` and `copyability` is not `copyable`.
9. **Division guards** — band-edge prices, empty input — no crash, no
   divide-by-zero.
10. **Hard wall** — a garbage-stub producer leaves verdict / gate /
    primary_edge_type / copyability_note / `data_quality_flags`
    byte-identical (mutation-proof).
11. Field present + dict-shaped on the live path; `None` only pre-surface.
12. Replay determinism — same input → byte-identical surface JSON.
13. Backward-compat — a `ProtocolReport` without the field → `None`.
14. Scope-guard field-set tests (`honesty_floor`, `tag_grain_rescue_floor`)
    updated to include `copy_latency_surface`.

**`tests/test_copy_latency_grid_pairs.py`** (capture helper):
15. `copy_latency_grid_pairs` emits `(asset, entry_ts)` + one pair per
    horizon, from the **earliest** BUY of each position; a position with
    no BUY in `activity_rows` emits nothing; deterministic ordering.

## 11. Out of scope

Sub-minute latency · order-book depth / slippage · the batch-range
capture optimization (v2) · running a live capture · verdict / gate /
Strategy-Pattern-Audit coupling. The surface measures post-entry price
drift and stops at display.
