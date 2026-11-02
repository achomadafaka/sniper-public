# Wallet Execution Profile — Design

**Date:** 2026-04-23
**Version:** v1
**Status:** spec (ready for plan)
**Companion to:** [wallet-understanding brain design](./2026-04-22-wallet-understanding-brain-design.md), [execution-layer scope memo](../../research/scopes/2026-04-22-execution-layer-scope.md), [MVE results](../../research/scopes/2026-04-22-execution-mve-results.md), [MVE arb control](../../research/scopes/2026-04-22-execution-mve-arb-control.md)

---

## 1. Goal

Give the wallet-understanding brain a way to measure **execution quality** on
Polymarket fills so Layer 3's `entry_timing_edge` and `exit_management_edge`
can promote out of `unknown` with real path evidence instead of heuristic
proxies.

The scoped output is a per-wallet `execution_profile` block populated only
for shortlisted wallets, stored inline in the extractor envelope, fed into
Layer 3.

## 2. Non-goals

- Not a book-replay reconstruction. Polymarket does not store historical
  order books; no attempt to rebuild them.
- Not an all-wallet pass. API cost forbids running this on every candidate.
- Not a new `execution_edge` Layer 3 dimension. Execution evidence is the
  **path-level substrate** the existing `entry_timing_edge` and
  `exit_management_edge` already flag as required via their `notes` field.
- Not a tagger that replaces Layer 1/2. Execution-profile findings never
  override Layer 1 terminals (`paired_outcome_arb`, `two_sided_inventory_trader`,
  `multi_outcome_basket_builder`).
- Not a copyability scorer. Layer 5 is downstream.

## 3. Locked decisions

Taken from the scope memo + MVE results + arb control. All four scope-memo
decisions and three MVE refinements are locked:

| Decision | Locked value | Source |
|----------|--------------|--------|
| Reference price (primary) | Preceding minute-bar from `clob.polymarket.com/prices-history?fidelity=1` | MVE Q1 (100% coverage, meaningful skew) |
| Reference price (confidence check) | 5-minute tape midpoint from `/trades`, excluding the wallet's own fills | MVE Q2 |
| Scope | Shortlist-only via explicit `--with-execution` flag + watchlist/followed promotion trigger | scope §4 |
| Storage | Inline in extractor output under `execution_profile` | scope §4 |
| Edge surface | Feed existing `entry_timing_edge` / `exit_management_edge`; clear their "needs path-aware replay" / "needs price-path replay or line-move context" notes when execution evidence is present | scope §4 |
| Self-reference | Drop covering-bar entirely; preceding-bar has zero self-reference cost and full coverage | MVE §Q4, arb §What-changes |
| Drift horizons | `min(H_target, time_to_resolution - 300s)` for `H_target ∈ {1h, 6h, 24h}` | MVE surprising #1 |
| `tape_trade_count_median` | First-class feature in the output | MVE surprising #2 |
| Per-fill tape-confidence gate | `tape_trade_count >= 3` in 5-min pre-window required for that fill to contribute to `skew_vs_preceding_bar` | arb §What-changes #2 |
| In-play sports gate | Fills with `time_to_resolution < 2h` are excluded from skew; kept for drift with `in_play=true` flag | arb §What-changes #3 |
| Calibration criterion | `(central-tendency near zero) + (drift near zero)` — NOT tight stdev | arb §TL;DR |
| Sample-size tiers | n ≥ 30 qualifying BUYs → `medium`; n ≥ 100 → `high`; below 30 → `unknown` | scope §6 |

## 4. Layer fit

**This is a Layer 1 / Layer 3 hybrid, not a new Layer 5.**

- Layer 1 gets new *observations*: per-fill skew, drift, tape counts.
- Layer 3 consumes aggregated observations and upgrades `entry_timing_edge`
  and `exit_management_edge` from the current-data-starved `unknown` default
  to evidence-backed `low` / `medium` / `high`.
- Layer 2 is unchanged. Pattern labels stay mechanical.
- Layer 4 (outcome attribution) is unchanged in this spec but gains an
  optional cross-reference surface in v2: "did the wallet's good-drift fills
  map to realized PnL?"
- Layer 5 is unchanged. Operational implications stay downstream.

## 5. Output schema

Inline under the existing extractor envelope:

```json
{
  "wallet": "0x...",
  "as_of_ts": 1776820000,
  "observations": {...},
  "execution_profile": {
    "status": "populated",
    "n_trades_total": 40,
    "n_trades_skew": 29,
    "n_trades_drift_1h": 35,
    "n_trades_drift_6h": 0,
    "n_trades_drift_24h": 0,
    "n_trades_in_play": 2,
    "confidence_tier": "medium",
    "coverage": {
      "preceding_bar": 1.0,
      "tape_5min": 0.225,
      "drift_1h": 0.875,
      "drift_6h": 0.0,
      "drift_24h": 0.0
    },
    "tape_trade_count_median": 0,
    "skew_vs_preceding_bar": {
      "n": 29,
      "mean": 0.00752,
      "median": 0.005,
      "stdev": 0.02731,
      "min": -0.044,
      "max": 0.160
    },
    "skew_vs_tape_5min": {"n": 9, "mean": ..., "median": ..., ...},
    "drift_1h": {"n": 35, "mean": -0.00917, "median": -0.005, "stdev": ..., ...},
    "drift_6h": {"n": 0, ...},
    "drift_24h": {"n": 0, ...},
    "calibration_signature": {
      "central_tendency_near_zero": true,
      "drift_near_zero": true
    },
    "data_quality": {
      "in_play_excluded_from_skew": 2,
      "thin_market_excluded_from_skew": 8,
      "insufficient_sample": false
    }
  },
  "edge_hypotheses": {
    "entry_timing_edge": {
      "level": "medium",
      "rationale": [
        "drift_1h_mean=+0.023",
        "n_drift=35 (>=30)",
        "midpoint_buy_ratio=18%"
      ],
      "notes": ""
    },
    "exit_management_edge": {...},
    ...
  }
}
```

When `--with-execution` is not set, or gating criteria exclude the wallet,
emit a stub:

```json
"execution_profile": {
  "status": "not_run",
  "reason": "shortlist_gate_not_met"
}
```

Valid `status` values: `populated`, `not_run`, `insufficient_sample`,
`api_failure`.

## 6. Algorithm

### 6.1 Inputs

Per wallet:
- Ordered list of BUY activity rows from `iter_activity` (already paginated
  and hardened).
- `PolymarketClient` for `get_prices_series` and `get_trades_window`.

### 6.2 Per-fill computation

For each BUY trade `(trade_ts, condition_id, asset, fill_price)`:

1. Fetch `history = get_prices_series(asset, start=trade_ts-1800, end=trade_ts + 24*3600 + 300, fidelity=1)`.
   Batchable per `(asset, earliest..latest)` span — one call per distinct
   asset the wallet traded, not one per fill.
2. `ref_preceding = last bar with ts < trade_ts within [trade_ts-1800, trade_ts)`.
   If none, skip fill's skew contribution, flag `thin_reference_window`.
3. `ref_tape, tape_n = _tape_midpoint(condition_id, asset, trade_ts, wallet)`
   — mean of OTHER wallets' BUY prices on same asset in `[trade_ts-300, trade_ts)`.
4. `drift_H = bar_at_offset(history, trade_ts, H) - fill_price` for
   `H ∈ {3600, 21600, 86400}`. Adaptive window: if
   `H > (resolve_ts - trade_ts) - 300`, record `null` (too close to resolution).
5. Flags:
   - `in_play = (resolve_ts - trade_ts) < 7200` — market resolves within 2h.
   - `thin_market = tape_n < 3`.

   `resolve_ts` is not exposed by `ActivityRow`. Source, in priority order:
   1. Local SQLite `market_metadata.end_date` (populated by streamer for
      markets the system has seen) — fast, one query per distinct
      condition.
   2. Gamma API `/events/{slug}` or `/markets?conditionId=X` fallback for
      markets not in the local table. One call per cache miss. Surface
      through a helper like `_resolve_ts_lookup(condition_id)` with
      in-memory caching for the extractor run.
   3. If both fail: record `resolution_ts_unknown=true` per-fill; treat
      `time_to_resolution` as infinite for the in-play gate (conservative;
      keeps the fill). Covered under §9.4.

### 6.3 Aggregation rules

For `skew_vs_preceding_bar`:
- Include only fills where `in_play == false` AND `thin_market == false`.
- This is the per-fill tape-confidence gate (locked decision 9).

For `skew_vs_tape_5min`:
- Include only fills where `tape_n >= 3`.

For `drift_H`:
- Include all fills with non-null `drift_H`. `in_play` fills are kept here
  (drift is about subsequent path, not about reference-price trust).
- Label the distribution with `n_trades_in_play` alongside total `n`.

For `tape_trade_count_median`:
- Median of `tape_n` across all fills, regardless of gate.

### 6.4 Sample-size tiers

After gating:

```
n_skew = len(skew_vs_preceding_bar.samples)
n_drift = len(drift_1h.samples)   # or drift_6h / drift_24h if those are dominant
n_eff = min(n_skew, n_drift) if both exist else max(n_skew, n_drift)

if n_eff >= 100: confidence_tier = "high"
elif n_eff >= 30: confidence_tier = "medium"
else:             confidence_tier = "unknown"  # status = "insufficient_sample"
```

### 6.5 Calibration signature

```
central_tendency_near_zero = abs(skew.median) <= 0.02 and abs(skew.mean) <= 0.03
drift_near_zero            = abs(drift_1h.median) <= 0.02 if drift_1h.n >= 10 else null
```

Two flags, both booleans-or-null. They are **descriptive**, not gates. They
appear in `execution_profile.calibration_signature` and are consumed by Layer
3 mapping rules below.

### 6.6 Layer 3 mapping

`entry_timing_edge`:

```
if execution_profile.status != "populated" or n_drift < 10:
    unchanged (stay "unknown" with existing notes)

elif drift_1h.mean >= 0.02 and drift_1h.median >= 0.01:
    level = "medium"
    rationale += ["drift_1h_mean=+X", "drift_1h_median=+Y", f"n={n_drift}"]
    notes = ""   # promoted — path evidence exists

elif drift_1h.mean >= 0.05 and drift_1h.median >= 0.03 and confidence_tier == "high":
    level = "high"

elif drift_1h.mean <= -0.02:
    level = "low"
    rationale += ["drift_1h_mean=-X", "negative subsequent drift"]

else:
    keep heuristic level (midpoint_buy_ratio-based) but append
    rationale lines noting drift exists and is near zero.
```

`exit_management_edge`:

In v1, `exit_management_edge` keeps its current `sell_ratio`-based heuristic.
v2 will add a per-fill "exit quality" measurement comparing sell-fill price
against subsequent path, but that needs a sell-tape mirror of the buy logic
and is out of scope for the first ship.

When v1 execution_profile is populated, append to the existing rationale:
- `drift_1h_mean=X` (informational — cross-reference for reviewers)

### 6.7 Storage & caching

- Inline in extractor output. No new table in v1.
- Cache the raw prices-history per `(asset, span)` in an ephemeral
  in-memory map for the extractor run only. No disk cache in v1.
- If per-wallet extraction time exceeds 5 min (hard ceiling), truncate
  input to the most recent N BUYs prioritizing distinct conditions, record
  `execution_profile.data_quality.truncated_input = true`.

## 7. Gating

### 7.1 When execution profile is computed

All three must be true:

1. `--with-execution` CLI flag set, OR wallet is in watchlist/followed set
   and extractor invoked by scheduled shortlist runner (see 7.3).
2. Layer 1 classification is **non-terminal** per
   `scripts/wallet_archetype_extractor.py:_LAYER1_TERMINAL`, which today
   contains `{paired_outcome_arb, two_sided_inventory_trader}` only. Those
   two wallets' execution signatures are already understood from the MVE
   (kch123 + `0x542ba1d3...`); skipping them saves API calls.
   `multi_outcome_basket_builder` has `copy_policy="manual_review"` and is
   **not** terminal — it DOES get profiled (basket arbs can have real
   selection/portfolio edges worth measuring). Exception: if
   `--with-execution --force` is set, run anyway (useful for calibration / QA).
3. `resolved_position_count >= 10` OR `buy_count >= 30` — enough activity
   to make execution measurement possible.

### 7.2 When skipped

Emit stub with `status="not_run"` and `reason` ∈ `{shortlist_gate_not_met,
layer1_terminal, insufficient_activity, api_rate_limited}`.

### 7.3 Shortlist runner (future, not in v1)

A scheduled process that reads `followed_wallets ∪ watchlist_wallets`, runs
the extractor with `--with-execution` on each, and writes results to the
brain store. Out of scope for v1 implementation, but the output format must
support this runner.

## 8. API cost model

- One `get_prices_series` call per `(asset, span)` — batchable per wallet
  to ~`distinct_assets` calls, not `trade_count` calls. For SwissMiss-tier
  wallets (~500 trades, ~150 distinct assets): ~150 calls.
- One `get_trades_window` call per fill for tape midpoint. This is the
  expensive leg — no batching possible because the window is per-fill.
  For 500 fills: 500 calls.
- Budget: target ≤ 700 API calls per shortlisted wallet, ≤ 10 minutes
  wall-clock at current pacing (0.25s sleep between fills).
- At shortlist scale (~50 wallets/day): ~35k API calls/day. Well inside
  Polymarket's public rate limits but close enough to warrant a scheduled
  off-peak runner (3 AM UTC).

## 9. Failure modes

### 9.1 API rate-limit or transient failure

- Retry with existing client backoff (already implemented).
- On persistent failure for a given fill: skip that fill, increment
  `data_quality.api_skips`.
- On persistent failure for ≥ 50% of fills: abort, emit
  `status="api_failure"`, surface in telemetry.

### 9.2 Thin markets dominate

- Recorded via `tape_trade_count_median`. If that is 0 and
  `thin_market_excluded_from_skew / n_trades_total >= 0.5`, the skew
  distribution is untrustworthy — emit `execution_profile` with skew
  fields present but Layer 3 mapping must read `confidence_tier` + the
  thin-market share and refuse to promote `entry_timing_edge` above
  `unknown` on skew alone. Drift can still be used if coverage is ok.

### 9.3 In-play sports dominate

- Same pattern, recorded in `in_play_excluded_from_skew`. Drift still
  contributes; skew does not.

### 9.4 Resolution-time missing

- Some markets have null `endDate` / `resolveTs` in metadata. In that
  case:
  - Treat `time_to_resolution` as infinite for the in-play gate (conservative,
    keeps the fill).
  - Drift windows compute normally until bars run out.
  - Flag `resolution_ts_unknown=true` per-fill, surface count in
    `data_quality`.

### 9.5 Covering bar fallback considered and rejected

Covering bar (the minute bar spanning `trade_ts`) was dropped per MVE §Q4:
on thin markets it produces ~5bp self-bias and on the arb wallet it
collapsed to near-zero skew trivially because the arb's own fill dominated
the minute. Preceding bar has 100% coverage and zero self-reference cost.

Do not reintroduce covering bar. If preceding bar is missing (fill within
30s of first tradable bar for the asset), skip the fill's skew contribution.

## 10. Test plan

### 10.1 Unit tests

- `_preceding_bar` returns the strictly-earlier bar within window; boundary
  cases at 0s, 1s, 1800s, 1801s before fill.
- `_tape_midpoint` excludes the probing wallet's own trades.
- Adaptive drift window: `time_to_resolution = 3600`, `H_target = 21600`
  → returns null (not a stale bar).
- `in_play` gate: `(resolve_ts - trade_ts) = 1h 59m` is in-play; `2h 01m`
  is not.
- Aggregation: skew distribution correctly excludes `in_play` and
  `thin_market` fills; drift includes them.
- Sample-size tiering: n=29 → unknown; n=30 → medium; n=100 → high.
- Calibration signature flags: mean 0.01 + median 0.005 → true;
  mean 0.05 → false.

### 10.2 Integration tests (offline fixtures)

- Snapshot of kch123 trades + mocked prices/trades → produces known
  profile: tight central tendency, n=0 drift windows (intraday resolution).
- Snapshot of SwissMiss trades + mocked prices/trades → produces
  positive-drift profile that maps `entry_timing_edge` → `medium` with
  `notes=""` (promoted).
- Snapshot of arb wallet (`0x542ba1d3...`) → skew gating excludes the
  two in-play esports fills; central tendency passes; drift near zero.

### 10.3 Smoke test

- Running `extract_wallet_archetype` without `--with-execution` must
  leave the envelope schema unchanged except for the stub
  `execution_profile.status="not_run"`. No API calls to prices-history
  or trades.
- Running with `--with-execution` on a Layer 1 terminal wallet must
  skip the expensive loop and emit `status="not_run"`, `reason="layer1_terminal"`.

### 10.4 End-to-end calibration replay

Keep the two committed MVE probes + the arb control probe as acceptance
tests. The production aggregation must reproduce (within tolerance) the
numbers from:
- `/tmp/mve_kch123.json` (if retained) — near-zero central tendency,
  tight stdev.
- `/tmp/mve_swissmiss.json` — positive drift_1h.
- `/tmp/mve_arb.json` — central tendency near zero after in-play gating,
  drift near zero.

If these three calibrations don't hold in the production path, the
pipeline has drifted from the MVE.

## 11. Rollout

1. Land code + tests behind `--with-execution` (opt-in, zero production
   risk).
2. Run manually on 5–10 known wallets from `knowledge/wallets/` to
   validate against hand-authored dossiers.
3. Compare Layer 3 mappings before/after — expect SwissMiss's
   `entry_timing_edge` to move from `unknown` → `medium`.
4. Schedule the shortlist runner (out-of-spec, follow-up work).

## 12. Open questions

These are acceptable open questions. They do not block v1.

1. Should drift be measured against the **fill asset** (current design) or
   against the fill-side-normalized price across re-hedges? For v1, fill
   asset is correct because wallets hold outcome-native positions (ADR-003).
2. Should `exit_management_edge` get its own sell-side execution measurement
   in v1 or v2? v2 per spec — adds complexity for a secondary signal.
3. Should Layer 4 (outcome attribution) consume `execution_profile` in v1
   to cross-validate drift vs realized? Cheap extension, but out of scope
   unless the plan has room.
4. Minimum bar-fidelity: `fidelity=1` (1-minute) vs `fidelity=0`
   (all snapshots). v1 uses `fidelity=1` as the scope memo locked;
   revisit if thin-market skew still looks pathological at scale.
5. Should `in_play` threshold be 2h or be market-category-dependent
   (e.g. 4h for politics, 30min for in-play tennis)? 2h is a safe
   default that worked for arb control; revisit with more data.

## 13. Bottom line

The execution profile adds real path evidence to the brain. It does not
replace Layer 1, does not create a new edge dimension, and does not
presume to decide copyability. It unblocks Layer 3's two edge dimensions
that explicitly noted they needed this substrate.

All decisions are locked by the scope memo, the MVE, and the arb control.
The implementation surface is small: one reference-price helper, one
tape-midpoint helper, one aggregator, one Layer 3 mapping update, one CLI
flag. Tests + integration fixtures can be borrowed from the three MVE
probe outputs.

Ready for an implementation plan.
