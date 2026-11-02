# Liquidation Cascade Detector + Exploitability Test (design)

Date: 2026-06-25
Status: DRAFT (brainstorming output, pre-plan)
Branch: research/hl-measurement-audit-leadlag
Related: [[project-hyperlive-scanner]], `2026-06-22-day-trading-techniques-sourced-research.md`
(sourced verdict: liquidation cascades are among the few **taker-viable** plays).

## 1. Problem

We want to know whether liquidation cascades on Hyperliquid are *exploitable*, and if so, build a
proper detector. This is the strategic fork: **exploitable → build it; not → pivot to the
copy/info system** (`2026-06-25-hl-analyzer-cleanup-and-copy-honesty-design.md`).

### Why the current approach is too weak to answer the question

The existing cluster path (`run_cluster_map.py` → `cluster_map.py` → `cluster_audit.cluster_cascade_edge`)
reconstructs *potential* liquidation levels from sampled open positions' `liquidationPx`:

- Harvests **~250 addresses** from a 20s trade-stream window, polls each `clearinghouseState`,
  bins liquidation prices in 10bps buckets, calls the densest near+big bin a "cluster."
- `run_cluster_cascade_audit.py:21` itself admits the notional is a **small-sample estimate**
  (~250 addresses = a sliver of HL's book); thresholds are relative to *sampled* scale, not the
  true book.
- Verified 2026-06-25: **no public HL liquidation feed** and no populated `hl_liquidations` table.
  Only per-position `liquidationPx` exists. We cannot build a true full-book liquidation map.

A cluster reconstructed from 0.1–1% of the book can't reliably anticipate where price gets pulled.
The anticipation ("magnet") thesis is sampling-bound.

## 2. Reframe: two theses, pre-register BOTH

### Thesis 1 — Magnet (anticipation): price drifts TOWARD a near+big liq cluster before it triggers
The current thesis. Keep it, but only as a **selection signal**, and fix the sampling:
- Harvest from multiple sources (leaderboard top-N by account value + large recent trades over a
  longer window + a persisted whale set), de-duplicated, to widen and de-time-bias the sample.
- **Whale-weight** clusters by the largest individual positions (the accounts whose liquidation
  actually moves price), not equal-weight across tiny accounts.
- Honest ceiling: still partial-book. We test it but do not expect it to carry alone.

### Thesis 2 — Realized cascade (reaction): trade the AFTERMATH of a cascade we detect from its footprint  ← PRIMARY
A cascade leaves a public footprint we already capture: a rapid adverse price move + a volume/
trade-count spike + an open-interest drop, within a short window. We don't need a liquidation feed —
we detect the event from price/OI/volume and ask whether the *aftermath* is predictable.

- **Detect:** `cascade_event` when over window W: `|ret| ≥ k·σ_recent` AND `OI drop ≥ d%` AND
  `trade_count ≥ m·median`. Direction = sign of the move.
- **Exploit question:** after a detected cascade, is there a predictable T-minute **continuation**
  (momentum — the cascade overshoots and keeps going) or **snapback** (mean-reversion — forced
  sellers overshoot, price recovers) that clears the 9 bps taker fee?
- Cascades are *big* moves (move ≫ fee), so unlike crypto micro lead-lag this is taker-viable if the
  aftermath has any persistence.

**Recommendation:** Thesis 2 is primary (tractable, taker-viable, uses existing data, no
unobtainable full-book map). Thesis 1 (improved magnet) is a secondary selection input. Pre-register
exploitability tests for both under one stats rig.

## 3. Detector design (Thesis 2 primary)

### Collection
**Source correction (verified 2026-06-25):** `hyperlive_scan_log` holds periodic **ctx snapshots**
(mid, `day_ntl_vlm`) — it does **not** contain a true short-window trade count or per-window traded
notional. Snapshot deltas are the wrong instrument for detecting a fast cascade. So:
- **`trade_count_w` and `traded_notional_w` are WS-derived:** subscribe to the HL **trades WS** per
  tracked asset and sum actual trade prints (count + Σ price·size) over the rolling window W. Do not
  infer them from snapshot-to-snapshot mid/volume differences.
- **OI is market-wide, from asset context:** `open_interest` from `metaAndAssetCtxs` /
  `activeAssetCtx` `openInterest` (true book OI). Do **NOT** sum `clearinghouseState` across sampled
  addresses for market OI — that repeats the ~250-address sampling error. `clearinghouseState` is
  reserved strictly for the Thesis-1 whale-state sampling, never for market aggregates.
- **Series row:** new append-only `hl_cascade_obs` (asset, ts_ms, mid, oi, ret_w, sigma_w,
  oi_delta_w, trade_count_w, traded_notional_w) at a fixed cadence.
- Forward-only (no backfill); pre-register the accrual window before reading verdicts.

### Pre-registered detector constants (FROZEN before accrual; change → new `detector_version`)
- **W** (event window): 60s.
- **σ lookback**: trailing 30 min realized vol (per asset), min-obs gate before events allowed.
- **k** (move threshold): `|ret_w| ≥ k·σ`, k = 3.0.
- **min move bps**: absolute floor 30 bps (so a tiny-σ regime can't trigger on noise; both k·σ AND
  the floor must be exceeded).
- **d** (OI drop): `oi_delta_w ≤ −d`, d = 1.0% of prior OI.
- **m** (trade spike): `trade_count_w ≥ m × median(trade_count_w)`, m = 3.0.
- **cooldown C**: no new event within 300s of a prior event for the same asset (one cascade = one
  event, not many).
- **OI availability**: if OI is missing/stale for an asset at event time, **skip the event**
  (fail-closed — never assume OI dropped).

### Event labelling + forward measurement
- `detect_cascades(series, params)` → list of events (pure function).
- **Persist events, not just observations:** append-only `hl_cascade_events` (event_id, asset,
  ts_ms, direction, ret_w, sigma_w, oi_delta_w, trade_count_w, traded_notional_w, magnitude_bucket,
  detector_version). Events are the unit of analysis and must be reproducible + version-stamped.
- For each event, measure forward aligned move at horizons (60s/180s/600s) for BOTH hypotheses:
  continuation (aligned = move_dir × fwd) and reversion (aligned = −move_dir × fwd).
- Grid over magnitude bucket × asset-class × horizon × {continuation, reversion}.

### Negative controls (falsification battery — an edge must survive ALL)
Run alongside the real test; each should produce a NULL if the edge is real:
1. **Direction flip** — predict the opposite direction; the "edge" must die (else it's not directional).
2. **Asset shuffle** — pair each event with a random *other* asset's forward return; must null
   (else it's a market-wide drift, not a cascade response).
3. **Matched non-event windows** — same asset, same time-of-day bucket, NO cascade; forward move must
   be ~0/insignificant (else the "edge" is just time-of-day).
4. **Event-time shift** — shift event time ±X min; a real cascade response decays with the shift. If
   the signal is identical when shifted off the event, it's an artifact, not a reaction.

### Canonical exploitability gate (referenced by §3 AND §7 — single source of truth)
An (hypothesis × cell) is an EDGE iff ALL hold:
- after-fee = mean − 9.0 bps > 0
- clustered-t (cluster by **asset × hour**) > 2.0
- n ≥ min_n (start 40)
- survives **Benjamini-Hochberg FDR** across the whole grid (q = 0.10)
- positive in **≥2 of 3** chronological thirds
- passes the **negative-control battery** above
Continuation and reversion are reported separately (mutually exclusive predictions).

## 4. Components

| Unit | File | Purpose |
|---|---|---|
| Trades-WS capture | new `run_cascade_capture.py` | subscribe trades WS + asset-ctx OI → write `hl_cascade_obs` (`trade_count_w`, `traded_notional_w`, `oi`) |
| Cascade detector | new `sniper/hyperlive/cascade.py` | `detect_cascades`, `forward_aligned` (pure) |
| Exploitability audit | new `scripts/run_cascade_exploit_audit.py` | grid + FDR + negative-control battery + canonical gate |
| Magnet sampling fix (Thesis 1) | `cluster_map.py` / `run_cluster_map.py` | multi-source harvest + whale-weight (clearinghouseState = whale-state ONLY) |
| Schema | `sniper/hyperlive/persistence.py` | `hl_cascade_obs` + `hl_cascade_events` (+ keep `hyperlive_cluster_history`) |
| UI | route + app.js | forward-test status panel (accruing / edge / no-edge per thesis) |

## 5. Data flow

trades-WS + asset-ctx OI → `hl_cascade_obs` → `detect_cascades` → `hl_cascade_events` (versioned) →
forward aligned move (continuation & reversion) → grid + FDR + negative-control battery → canonical
gate → verdict (accruing/edge/no-edge) → UI + decision gate.

## 6. Testing

- `detect_cascades`: a synthetic series with a planted move+OI-drop+volume-spike fires exactly one
  event with correct direction; a slow drift fires none; cooldown C suppresses a second event inside
  the window; the min-move-bps floor suppresses a low-σ false trigger.
- `forward_aligned`: continuation vs reversion signs correct; fee subtracted once.
- FDR: a grid with one true cell and many null cells keeps the true cell, screens the nulls.
- **Negative controls:** direction-flip, asset-shuffle, matched-non-event, and event-time-shift each
  return NULL on a planted-true-edge fixture (proves the controls actually kill artifacts).
- Magnet whale-weight: a single whale position dominates the cluster notional as designed.

## 7. Decision gate (the fork)

Pre-register an accrual window (e.g. N cascade events or T calendar-days of forward data). At the
milestone, apply the **canonical exploitability gate** defined in §3 (after-fee>0, clustered-t>2,
n≥min_n, FDR, ≥2/3 thirds, **and** the negative-control battery). Then:
- **Either thesis clears the canonical gate** → write an implementation plan for the live paper detector.
- **Neither clears** → declare cascade null (honest, like the microstructure A/B) and pivot:
  the copy/info system becomes the primary HL play. No sunk-cost continuation.

## 8. Risks / honest framing

- Cascades are rare → slow accrual; the verdict is forward, not immediate.
- Aftermath could be efficiently priced (no continuation, no exploitable snapback) — that is the
  likely null and must be acceptable.
- Asset-ctx `openInterest` is a point-in-time read; OI *delta* (`oi_delta_w`) requires our own
  forward series (no historical OI feed to backfill).
- Magnet thesis is sampling-bound by construction; treat as selection context, never standalone.
- Read-only / selection labels until the gate clears; paper-only after. No live orders in scope.
