# Wallet Execution Profile v2 — `drift_to_resolution` + sell-side metrics

**Date:** 2026-04-23
**Version:** v2 draft
**Status:** spec draft, not yet planned
**Companion to:** [v1 execution profile spec](./2026-04-23-wallet-execution-profile-design.md), [SwissMiss edge interpretation](../../research/scopes/2026-04-23-swissmiss-edge-interpretation.md)
**Supabase brain:** `knowledge/patterns/execution-profile-falsification-not-discovery-2026-04-23`, `reference/vocabulary/wallet-mechanism-buckets`

---

## 1. Goal

Two correct-horizon metrics the wallet brain is missing:

**1a. `drift_to_resolution` (BUY side).** v1's `drift_1h/6h/24h` fails on
long-hold wallets because the measurement window doesn't span the wallet's
actual holding period. v2 measures each BUY fill against the **market
resolution price**.

For every resolved BUY fill:
```
drift_to_resolution = resolved_price - fill_price
```

Aggregated per wallet, this is the **true outcome-attributed signal**
for position entries, not a proxy at an arbitrary horizon.

**1b. Sell-side measurement.** v1 is BUY-only. The current
`exit_management_edge` heuristic uses `sell_ratio` count alone — it cannot
distinguish "sold to protect from loss" (skill) from "sold a winner too
early" (anti-skill). Both show the same sell_ratio. v2 adds two
sell-side metrics:

```
sell_skew_vs_preceding_bar = sell_price - preceding_minute_bar
    # mirror of BUY-side skew; measures sell-side execution quality vs tape

sell_quality_vs_resolution = sell_price - outcome_native_resolved_price
    # positive  → escaped a losing position (sold above the 0 it would have become)
    # negative  → exited a winner too early (sold below the 1 it would have become)
    # zero      → sold at resolution; indifferent
```

These catch the "active management" skill our current pipeline doesn't
measure. User-requested addition 2026-04-23.

## 2. Why v2 (what v1 gets wrong)

### 2a. BUY side — drift_1h/6h/24h is sample-mix noise

The v1 rollout on SwissMiss (n=69) produced `drift_1h mean = -0.075`
which moved Layer 3's `entry_timing_edge` to `low` ("fading fills").
Yet SwissMiss is profitable.

Root cause: drift_1h on a sports market that resolved in 45 minutes
samples the post-resolution flat line (0 or 1). On a political market
resolving in 3 months, drift_1h samples a barely-moved price. Mean of
the mixed sample is dominated by whichever category is over-represented
in the fetch window. **It is noise + signal, not signal.**

drift_to_resolution eliminates this. Every sample measures the same
thing: how far did the market move toward the wallet's side between
fill and resolution. For an insider, this is large and positive. For a
noise trader, zero. For an adverse-selection bot, negative.

### 2b. Sell side — current heuristic is count-only

`exit_management_edge` today fires on four rules that use `sell_ratio`
and `redeem_ratio` only (see `_build_edge_hypotheses`). A wallet selling
5% of trades ends up as `medium` if `positive_share >= 0.6`, and `low`
if `sell_ratio < 0.02 and redeem_ratio > 0.03`. Neither rule looks at
**whether the sells were good.** The same sell_ratio can describe:

- A wallet that sells only when a thesis breaks (good — preserves capital)
- A wallet that sells when price dips temporarily (bad — locks losses)
- A wallet that sells winners too early (bad — leaves money on table)
- A wallet that takes profit at good levels (good — active management)

Those four behaviors all look identical to the v1 heuristic. Sell-side
execution + outcome metrics in v2 separate them.

## 3. Where this lives in the brain

**Layer 4 — Outcome Attribution**, not Layer 3.

Layer 3 (edge hypotheses) asks "what may be generating this wallet's
performance?" Layer 4 (outcome attribution) asks "where did the
realized winnings come from?"

drift_to_resolution is realized-outcome data. It belongs in Layer 4.

**Impact on Layer 3:** `entry_timing_edge` gets a second evidence
source — v1 drift at fixed horizons (short-timescale skill) PLUS v2
drift_to_resolution (outcome-realized skill). A wallet with positive v2
drift but neutral v1 drift is a market-selector, not a short-term timer.
A wallet with positive both is a rare true entry-timer.

## 3.1 Design principle — opaque tags, no hardcoded taxonomies

The v2 pipeline treats every categorical label from an external source
(gamma tags, outcome names, condition IDs) as an **opaque identifier**.
No code branch depends on a specific value; no semantic grouping is
encoded in the module; no "this tag means X" mapping exists anywhere.

Consequences:
- When Polymarket adds a new tag, v2 ingests it automatically. No code
  change.
- When a tag goes extinct, v2's per-tag stats simply stop growing on new
  fills. Old per-tag stats survive as historical record. No code change.
- Pre-registered thresholds operate on *distributions of fills across
  tags*, not on specific tag names (e.g. "≥2 tags agree on sign", not
  "`politics` must show +drift").
- Existing hardcoded taxonomies (`_THESIS_CATEGORY_MAP` in
  `polymarket_api.py`, any agent rule that branches on `category ==
  "politics"`) are legacy and removed during v2 Phase 1.

Every illustrative tag slug in this spec (`<tag-slug>`, `<tag-A>`, etc.)
is a placeholder. Real slugs are whatever gamma serves at runtime.

## 4. Non-goals

- Not replacing v1. The fixed-horizon drifts still detect short-horizon
  fading or leading on fast-resolving markets. Keep both.
- Not reimplementing outcome attribution wholesale. The existing
  `_build_outcome_attribution` already slices realized PnL by category /
  bucket. v2 adds a per-fill metric alongside, not a rewrite.
- Not a new edge-classifier label. The output is a distribution, not a
  categorical claim.
- Not a cross-wallet ranking. Magnitudes are comparable only within the
  same category/time-window mix.

## 5. Locked decisions

| Decision | Value | Rationale |
|----------|-------|-----------|
| Metric | `resolved_price - fill_price` (signed, outcome-native per ADR-003) | Matches ADR-003; YES fill measured against YES resolution |
| Inclusion | Only *resolved* fills (`resolved_price ∈ {0.0, 1.0}` after threshold) | Open positions have no resolution yet; predictions must be falsifiable |
| Resolution source | `market_outcomes.resolved_price` keyed on `(condition_id, outcome)` — the table has a composite PK. Fallback to `market_metadata.end_date_iso + 1d` lower-bound staleness check, then gamma API single-market lookup. | Same source that paper-trader uses for PnL |
| Win/loss thresholds | `RESOLUTION_WIN_THRESHOLD = 0.97`, `RESOLUTION_LOSS_THRESHOLD = 0.03` (inherit from web_settings) | Consistent with rest of the system |
| Aggregation | Distribution summary (n, mean, median, stdev, min, max) | Matches v1 helper `_dist_summary` — reuse |
| Sample tiers | n ≥ 30 → `medium`, n ≥ 100 → `high`, else `unknown` | Same as v1 — spec §6.4 |
| Gating on run | Same as v1 — skip Layer 1 terminals unless force, min activity | No reason to diverge |
| Storage | Inline under `execution_profile.drift_to_resolution` (same envelope as v1) | Single read for consumers |
| Layer 4 consumer | New `_build_outcome_attribution` subsection: `resolution_drift` — slice by **tag** + size + entry-price bucket | Existing function already slices PnL; add one more dimension |
| Categorisation taxonomy | **Polymarket gamma tags (raw slugs), treated as opaque strings**. No hardcoded tag list. No "other" bucket. Markets without tags → explicit `unclassified` count, not averaged into the main metrics. | Polymarket's taxonomy is live and evolving (ukraine, trump-zelenskyy, fed, earn-4, pre-market, etc. — all time-bound). Any hardcoded tag list will rot. Our current `_THESIS_CATEGORY_MAP` in `polymarket_api.py` is exactly this anti-pattern and will be removed. |
| Tag handling principle | Tags are **opaque identifiers** — no code branch depends on a specific tag value; no semantic grouping in the code; no "this tag means politics" mapping. Every tag appearing in gamma responses is ingested verbatim. Stats are computed per distinct tag the data carries. Agents and consumers see tag keys as human-readable labels only. | Future-proof: a tag that is hot today (`ukraine`) and gone tomorrow just stops appearing in new fills; no code change required. A tag that didn't exist yesterday (`gaza-ceasefire`) ingests automatically the first time a market carries it. |
| Tag storage | New table `market_tags (condition_id TEXT, tag TEXT, PRIMARY KEY (condition_id, tag))`, populated by the existing gamma event fetcher. Backfill job runs once pre-v2. | Normalised, supports tag-count indices, allows multi-tag membership naturally. Keeps `market_metadata` stable. |
| Cluster feature tag-set | **Derived per-wallet from the wallet's own fill distribution.** Top-K tags by that wallet's fill count (K=20 default). Long-tail tags (wallet-local frequency < 3) collapse into a single `other_tags` binary indicator — NOT a named category. | Per-wallet feature set, not a global enum. Different wallets have different tag vocabularies. Clustering still works when Polymarket retires every current tag. |
| Pre-registered thresholds | Operate on tag **distributions**, not tag **names**. Example: "Layer 3 promotion requires ≥2 tags with n≥10 agreeing on sign" — never "promotion requires `politics` to show +drift". | Thresholds outlive the current tag set. |
| Sell-side skew | `sell_price - preceding_minute_bar`, same gates as BUY (tape_n ≥ 3, not in_play) | Mirror of v1 BUY-side. No new helpers needed — `_preceding_bar` and `_tape_midpoint` already take an asset+ts, direction-agnostic. |
| Sell-side outcome quality | `sell_price - outcome_native_resolved_price` for sold fills whose condition later resolved | Direct answer to "did the sell improve PnL vs hold". Same `market_outcomes` dependency as BUY-side v2. |
| Exit-management Layer 3 consumer | Upgrade `exit_management_edge` rules: positive sell_quality AND sell_ratio ≥ 0.03 → `medium`/`high`; negative sell_quality → `low` regardless of sell_ratio | Replaces count-only heuristic with outcome-attributed evidence. |

## 6. Output schema extension

Inline addition to the existing `execution_profile` block (spec v1 §5):

```json
"execution_profile": {
  "status": "populated",
  ...existing v1 fields...,
  "drift_to_resolution": {
    "n": 12,
    "mean": 0.34,
    "median": 0.28,
    "stdev": 0.41,
    "min": -0.78,
    "max": 1.00,
    "win_share": 0.67,
    "by_tag": {
      // Keys are whatever tag slugs gamma returns for this wallet's markets.
      // The slugs below are purely illustrative — do not hardcode any of them.
      // In production code, iterate `by_tag.items()`; never branch on a
      // specific key.
      "<tag-slug-1>": {"n": 7, "mean": 0.42, "win_share": 0.71},
      "<tag-slug-2>": {"n": 5, "mean": 0.55, "win_share": 0.80},
      "<tag-slug-3>": {"n": 3, "mean": 0.60, "win_share": 1.00}
      // ... as many keys as distinct tags the wallet's fills touched
    },
    "unclassified": {"n": 2, "mean": 0.55, "win_share": 1.00}  // markets with no gamma tags; NOT averaged into main metric
    "by_entry_price_bucket": {
      "0.00-0.20": {"n": 2, "mean": 0.65},
      "0.20-0.40": {"n": 5, "mean": 0.40},
      "0.40-0.60": {"n": 4, "mean": 0.22},
      "0.60-0.80": {"n": 1, "mean": 0.15}
    }
  },
  "sell_skew_vs_preceding_bar": {
    "n": 8, "mean": 0.004, "median": 0.002, "stdev": 0.012, "min": -0.01, "max": 0.03
  },
  "sell_quality_vs_resolution": {
    "n": 6,
    "mean": 0.18,
    "median": 0.12,
    "stdev": 0.24,
    "min": -0.35,
    "max": 0.70,
    "escapes": 4,        // sold above 0 on positions that resolved to loss
    "cut_winners": 1,    // sold below 1 on positions that resolved to win
    "neutral": 1,        // sold within 3c of terminal resolution
    "by_tag": {
      // Illustrative only — slugs are whatever gamma returned.
      "<tag-A>": {"n": 3, "mean": 0.30, "escapes": 2, "cut_winners": 0, "neutral": 1},
      "<tag-B>": {"n": 2, "mean": -0.05, "escapes": 1, "cut_winners": 1, "neutral": 0},
      "<tag-C>": {"n": 1, "mean": -0.10, "escapes": 0, "cut_winners": 1, "neutral": 0}
    }
  }
}
```

New Layer 4 subsection under `outcome_attribution`:

```json
"outcome_attribution": {
  ...existing fields (pnl_by_tag etc)...,
  "resolution_drift": {
    "source": "execution_profile.drift_to_resolution",
    "dominant_tags": [                                           // top-3 tag slugs by PnL share (whatever gamma returned for this wallet)
      {"tag": "<tag-slug>", "pnl_share": 0.51},
      {"tag": "<tag-slug>", "pnl_share": 0.23},
      {"tag": "<tag-slug>", "pnl_share": 0.12}
    ],
    "concentration_hhi": 0.42,
    "edge_interpretation": "medium"
  }
}
```

## 7. Algorithm

### 7.1 Per-fill computation

For each BUY fill `(trade_ts, condition_id, asset, fill_price)`:

1. Look up `resolved_price` via:
   - `SELECT resolved_price FROM market_outcomes WHERE condition_id = ? AND outcome = ?` (SQLite) — fast path. `outcome` is the YES/NO side the wallet held, matched off `ActivityRow.outcome`.
   - If row missing but `market_metadata.end_date_iso + 1 day < now()`, mark the fill as `settlement_backlog` (excluded from metric, counted in `data_quality`).
   - If `end_date_iso` is in the future or absent, mark as `pending_settlement` (position still open — excluded).
   - Fallback to gamma API single-market lookup if both miss.

2. If `resolved_price < RESOLUTION_LOSS_THRESHOLD (0.03)`: treat as 0.
   If `resolved_price > RESOLUTION_WIN_THRESHOLD (0.97)`: treat as 1.
   Intermediate: rare on binary markets — exclude with
   `invalid_resolution` flag (log-only, doesn't break aggregation).

3. Determine which side the wallet held. `ActivityRow.outcome` carries a
   human-readable label (e.g. `"Yes"`, `"No"`, `"sinner"`, `"raptors"`).
   `market_outcomes.outcome` uses the same human-readable label (verified
   on local DB — 1896 rows, samples include `cerundolo`, `wild`, `raptors`).
   Lookup join:
   ```sql
   SELECT resolved_price
   FROM market_outcomes
   WHERE condition_id = ? AND LOWER(outcome) = LOWER(?)
   ```
   No YES/NO inversion needed — the per-outcome row already stores the
   outcome-native resolved price (0.0 for losing side, 1.0 for winning).
   `drift_to_resolution = resolved_price - fill_price` is direct.
   Helper: `_outcome_native_resolution(condition_id, outcome_label)`.

4. `drift_to_resolution = outcome_native_resolution - fill_price`. Range
   in [-fill_price, 1 - fill_price], clamped to [-1, +1] for safety.

### 7.2 Aggregation

- `drift_to_resolution` distribution: `_dist_summary` on all valid
  resolved fills.
- `win_share`: `sum(drift > 0) / n` — fraction of fills that paid.
- `by_tag`: for each fill, look up the market's tags from `market_tags`
  (joined on `condition_id`). Each fill contributes to every tag it
  carries. Report distribution per tag with n, mean, win_share. A fill on
  a market tagged `[crypto, bitcoin, macro]` contributes to three
  separate per-tag distributions.
- `unclassified`: fills on markets with zero tags in `market_tags` are
  summarized separately (not averaged into `by_tag`). If
  `unclassified.n / total >= 0.2` warn — the backfill is incomplete for
  this wallet's market set.
- `by_entry_price_bucket`: 0.0–0.2, 0.2–0.4, 0.4–0.6, 0.6–0.8, 0.8–1.0.
  Size-weighted in v2.1 if needed; v2 reports equal-weight n only.

### 7.3 Sell-side per-fill computation

For each SELL fill `(trade_ts, condition_id, asset, sell_price, outcome)`:

1. **Skew vs preceding bar** (execution quality):
   - Fetch prices-history per asset the same way the BUY path does (reuse
     `_build_history_cache` — it already batches by asset).
   - `sell_skew = sell_price - preceding_minute_bar`
   - Apply the same gates: exclude if `in_play` or `thin_market` (tape_n<3).
   - Semantic: sells ABOVE the preceding tape are *better* exits; sells
     below are *worse*. Sign is reversed vs BUY-side skew's interpretation
     but the same sign convention holds (positive = price paid higher
     than reference; for sellers, paid = received, so positive = good).

2. **Quality vs resolution** (outcome quality):
   - Look up `resolved_price` via the same `market_outcomes` path as §7.1.
   - If the condition has not resolved yet: skip this fill's quality
     metric but keep the skew metric. Flag as `pending_settlement` in
     `data_quality`.
   - `sell_quality = sell_price - outcome_native_resolved_price`
   - Classify:
     - `escapes += 1` if `resolved = 0` and `sell_price > 0.05`
       (sold above loss)
     - `cut_winners += 1` if `resolved = 1` and `sell_price < 0.95`
       (sold below full win)
     - `neutral += 1` otherwise (sold within 3c of terminal)

3. Edge case — partial sells. Polymarket supports selling a portion of
   a position. For v2, treat each SELL row independently (size-indifferent).
   v2.1 could size-weight.

### 7.4 Sample tiers

Per spec v1 §6.4. Each metric counts towards its own tier independent
of the others. If multiple are populated, Layer 3 consumer combines
them (spec v2 §8).

### 7.5 Edge interpretation (Layer 4 new field)

```python
if n < 30:
    edge = "insufficient_sample"
elif win_share >= 0.70 and mean >= 0.30:
    edge = "strong"
elif win_share >= 0.60 and mean >= 0.15:
    edge = "medium"
elif win_share <= 0.40 or mean <= -0.10:
    edge = "negative"
else:
    edge = "weak"
```

Thresholds are pre-registered; tuning allowed only with an ADR.

### 7.6 Exit-management edge interpretation

Mirror of 7.5 but for the sell-quality-vs-resolution distribution.

```python
# sell_quality_vs_resolution distribution
if n < 10:
    edge = "insufficient_sample"
elif mean >= 0.15 and escapes / max(n, 1) >= 0.5:
    edge = "strong"    # sells protect capital and avoid drawdowns
elif mean >= 0.05 and escapes > cut_winners:
    edge = "medium"    # net positive exits with more escapes than premature-cut winners
elif cut_winners / max(n, 1) >= 0.5:
    edge = "low"       # pattern of leaving money on the table by exiting winners
elif mean <= -0.10:
    edge = "low"       # bad sells overall
else:
    edge = "weak"
```

## 8. Layer 3 revision

v2 upgrades `entry_timing_edge` mapping rules to consume BOTH v1 drift
AND v2 drift_to_resolution:

```
if execution_profile.drift_to_resolution.n >= 30:
    # Long-hold signal is authoritative when present
    v2 = drift_to_resolution
    if v2.mean >= 0.30 and v2.win_share >= 0.70 and tier == "high":
        entry_timing_edge = "high"
    elif v2.mean >= 0.15 and v2.win_share >= 0.60:
        entry_timing_edge = "medium"
    elif v2.mean <= -0.10:
        entry_timing_edge = "low"
    else:
        # Near-zero v2 → fall back to v1 heuristics + rationale
        ...existing v1 rules...

elif drift_1h.n >= 10:
    # No resolved signal yet; fall back to v1 short-horizon heuristic
    ...existing v1 rules...

else:
    entry_timing_edge = unchanged
```

v2 takes precedence when resolved data is available because it measures
what actually paid.

### 8.1 exit_management_edge revision

Replaces the count-only heuristic with outcome-attributed evidence
when sell-side data is present:

```
if execution_profile.sell_quality_vs_resolution.n >= 10:
    q = sell_quality_vs_resolution
    if q.mean >= 0.15 and q.escapes / q.n >= 0.5:
        exit_management_edge = "high"
    elif q.mean >= 0.05 and q.escapes > q.cut_winners:
        exit_management_edge = "medium"
    elif q.cut_winners / q.n >= 0.5 or q.mean <= -0.10:
        exit_management_edge = "low"
    else:
        # keep heuristic level but append v2 rationale
        rationale.append(f"sell_quality_mean={q.mean:+.3f}, escapes={q.escapes}, cut_winners={q.cut_winners}")

elif execution_profile.status == "populated":
    # Wallet was profiled but has too few resolved sells — keep v1 heuristic
    ...existing v1 rules...
```

A wallet that sells frequently AND profitably reads as `high` exit
management (active manager skill). A wallet that sells frequently AND
cuts winners reads as `low` despite high sell_ratio. The count-only
tie is broken by outcome.

## 9. Storage of `market_outcomes` dependency

`market_outcomes` table must be populated by the existing poller. Dry
check before v2 rollout:

```sql
SELECT COUNT(*) FROM market_outcomes;  -- resolved_price NOT NULL by schema default
```

If < a threshold share of resolved markets in `market_metadata`,
settlement poller needs a priming run. Not a code change in v2; a
runbook item.

## 10. Migration from v1

Additive only:
- New fields in `execution_profile` envelope — existing consumers
  ignore unknown keys.
- `_build_outcome_attribution` gains a new subsection — existing
  consumers unaffected.
- Layer 3 mapping change: v2 rule runs BEFORE v1 rule. If v2 data
  present, v1 is fallback only. Guarded by `drift_to_resolution.n >= 30`
  so sparse-coverage cases fall back cleanly.

No schema migrations. No new tables. No new services.

## 11. Test plan

### Unit tests

- `_outcome_native_resolution` case-normalizes outcome label (`"Sinner"` matches `"sinner"`)
- `_outcome_native_resolution` returns None when `(condition_id, outcome)` missing
- `win_share` computation rejects clamped resolution prices
- `by_tag` grouping puts fills on tagless markets into `unclassified`, NOT into an "other" bucket
- multi-tag fills contribute to every tag they carry
- tag count warning fires when `unclassified.n / total >= 0.2`
- `by_entry_price_bucket` boundary cases
- Adaptive resolution lookup: SQLite hit, SQLite miss → gamma, both miss
- Edge interpretation thresholds at exact boundaries
- Sell-side: `escape` / `cut_winners` / `neutral` classification boundaries
- Sell-side: `exit_management_edge` promotes to `high` on positive mean + majority escapes
- Sell-side: `cut_winners` majority demotes to `low` even with positive mean

### Integration tests

- Fixture: synthetic wallet with 50 resolved BUY fills + 15 SELL fills
  across 3 categories, predetermined drift distributions → aggregator
  returns expected envelope.
- Layer 3 v2 mapping (entry): v2 populated + strong drift → promotes
  to `high`; v2 populated + negative → `low`; v2 insufficient → falls
  back to v1.
- Layer 3 v2 mapping (exit): `sell_quality.n=12` + mean=+0.18 +
  escapes=7 → `high`; mean=+0.02 + cut_winners=8 → `low`.

### E2E

- Rerun SwissMiss with v2 code path — expect high resolution drift on
  fills tagged with her thesis tags (whichever gamma slugs dominate
  her by_tag output), near-zero on tags that represent short-horizon
  markets. Would
  validate the long-horizon-selector hypothesis from the research memo.
  Sell-side: she has `resolved_position_count=7` and very low sell_ratio,
  so sell_quality will be `insufficient_sample` — that is informative in
  itself (she is a holder, not a manager).

## 11a. Size-weighted aggregation

Every distribution metric (drift_to_resolution, sell_quality_vs_resolution,
skew_vs_preceding_bar) reports both equal-weight and size-weighted mean:

```python
mean = sum(values) / n
size_weighted_mean = sum(v * size_i for v, size_i in zip(values, sizes)) / sum(sizes)
```

**Why this matters.** A wallet that makes 30 small scouting bets at
random + 3 enormous thesis bets with strong directional signal will
read as "no edge" on equal-weight mean. Size-weighted mean captures
what happens to their *capital*, which is what actually pays.

**Implementation.** `ActivityRow.usdc_size` already provides per-fill
size. `_dist_summary` gains a second call variant or an extra `sizes`
argument. Additive, no schema change.

**Consumption rule (Layer 3).** If `abs(size_weighted_mean - mean) >
max(0.05, pooled_stdev)` the wallet's edge is *conviction-dependent* —
they win on their large bets and are noise on small ones. Flag
`conviction_dependent_edge = true`. This is a real wallet type; must
not be averaged away.

## 11b. Hold-duration attribution

For every BUY fill we can determine the **hold duration** by pairing
it with a subsequent SELL on the same `condition_id` (matched in
timestamp order, size-accumulated) or with the resolution timestamp
if unsold. Buckets:

- `intraday` (< 24h)
- `short` (1–7 days)
- `medium` (7–30 days)
- `long` (≥ 30 days)

All existing metrics report per-bucket: `drift_to_resolution.by_hold`,
`sell_quality_vs_resolution.by_hold`, etc.

**Why.** Strategy type correlates with hold duration more than with
market tag. A wallet whose intraday fills are tight-skew-near-zero
(inventory flip) and whose long holds are positive drift_to_resolution
(thesis) is running two strategies that look averaged when pooled.

**Matching algorithm** (FIFO within condition, outcome-native):

```python
def _pair_buys_to_exits(activity: list[ActivityRow]) -> list[FillPair]:
    # Per (wallet, condition_id, asset), walk activity in time order.
    # On BUY: push (ts, price, size) onto an open-lots queue.
    # On SELL: pop oldest lots up to sell size, record (buy_ts, sell_ts, sell_price, realized_size).
    # On market resolution: any remaining open lot pairs with (resolve_ts, resolved_price, remaining_size).
    # Returns list of (buy_ts, exit_ts, buy_price, exit_price, size, exit_type ∈ {sell, resolve, open}).
```

FIFO is the right match for Polymarket because outcome-native positions
are fungible; LIFO would produce identical aggregate metrics but
LIFO-matched lots tend to confuse hold-duration attribution when the
wallet scales in.

## 11c. Trim vs full-close distinction

When a SELL reduces position size but does not zero it, it's a **trim**.
When it zeros the position, it's a **full close**. Semantics differ:

- Trim: risk management / profit-taking while staying in the trade. Has
  different outcome dynamics than exit.
- Full close: thesis change or capitulation.

`sell_quality_vs_resolution` and `sell_skew_vs_preceding_bar` report
separately for `trims` vs `closes`. Layer 3 exit_management_edge
considers both but trim quality is typically a weaker signal.

Implementation: carry running-position size per condition through
`_pair_buys_to_exits`. `exit_type ∈ {trim, close, resolve}` is recorded
per pair.

## 11d. Regime-shift detection with changepoint analysis

The v2-baseline "half-split trip-wire" in §11e catches gross regime
shifts. Perfection requires actual changepoint detection.

**Approach.** Apply PELT (Pruned Exact Linear Time) changepoint search
to the time-ordered per-fill `drift_to_resolution` series. Report
detected changepoints as UTC timestamps; split the wallet's history
into regimes; recompute all aggregate metrics per regime.

**Library.** `ruptures` (Python, MIT, single dep). Current CLI-deep
tree already tolerates scikit-learn and numpy; adding `ruptures>=1.1`
is a one-line requirements addition.

```python
import ruptures as rpt
algo = rpt.Pelt(model="rbf").fit(drift_series)
changepoints = algo.predict(pen=5)  # penalty tuned via BIC on synthetic data
regimes = _split_by_changepoints(fills, changepoints)
for regime in regimes:
    regime.metrics = _aggregate(regime.fills)
```

**Penalty (`pen`) selection.** Pre-registered at `pen = 5` for n >= 50;
increased proportionally for longer series per the BIC heuristic. Above
this, PELT over-segments (noted in the ruptures literature). Tuned on a
synthetic fixture of known-stationary + known-regime-shift wallets.

**Output addition to envelope.**

```json
"execution_profile": {
  ...,
  "regimes": [
    {"start_ts": 1770000000, "end_ts": 1775000000, "n_fills": 42,
     "drift_to_resolution_mean": +0.32, "label": "political_directional"},
    {"start_ts": 1775000001, "end_ts": 1778000000, "n_fills": 27,
     "drift_to_resolution_mean": -0.08, "label": "drifted_noise"}
  ],
  "regime_shift_detected": true
}
```

**Layer 3 consumer.** When `regime_shift_detected == true`, Layer 3
edge promotion uses only the MOST RECENT regime's metrics, not the
pooled mean. Explicit note added to `entry_timing_edge.rationale`:
`"uses post-shift regime only (changepoint at 2026-03-15)"`.

**Why PELT over HMM/Jump Model.** HMMs need regime count specified;
Statistical Jump Models (2024 preferred) need larger samples than we
have per wallet. PELT is non-parametric, works on n=50+, and its
over-segmentation bias is controlled by the penalty. For our use case
(detect real shifts, accept false negatives over false positives) PELT
with conservative penalty is correct.

## 11e. Strategy clustering — sub-strategy detection

Adapted from Della Vedova (2024, *Statistical Predictions of Trading
Strategies in Electronic Markets*): cluster a wallet's fills on a
feature vector, then report per-cluster metrics. They found three
archetypes (directional / opportunistic / market-maker) using
multinomial logistic regression on limit-order-book features + PCA +
k-means on coefficients.

We don't have LOB depth. We do have per-fill features:

| Feature | Source | Bucket/normalize |
|---------|--------|------------------|
| entry_price_bucket | fill_price | 0.0-0.2 / 0.2-0.4 / 0.4-0.6 / 0.6-0.8 / 0.8-1.0 |
| size_bucket | usdc_size | log10 quintiles over wallet's own sizes |
| hold_duration_bucket | from §11b pairing | intraday / short / medium / long |
| tag_membership | market_tags (new table) | binary membership per tag in the wallet's top-K tag set (K=20 default, chosen by fill-frequency per-wallet). Multi-tag markets light up multiple dimensions. |
| tape_thinness | v1 tape_trade_count | 0 / 1-2 / 3-10 / 10+ |
| time_of_day_bucket | fill_ts UTC | night / morning / afternoon / evening |
| outcome_side | ActivityRow.outcome | YES-ish / NO-ish / named (team/candidate slug for multi-outcome markets) |

**Encoding.** One-hot all bucket features; tag_membership is already
binary. Total dimensionality: ~40–60 dims depending on tag count. PCA
to 8 dims (retaining ~85% variance per spec-time validation).

**Tag-set selection per-wallet.** The wallet's top-K tags by fill count
(default K=20) are used as clustering features. Long-tail tags (tags
appearing in <3 fills) are aggregated into an `other_tags` binary
indicator (present/absent) — used as a single feature, NOT an
unstructured bucket. Prevents dimensionality explosion on wallets that
touch hundreds of different tag slugs.

**Clustering.** Gaussian Mixture Model with k selected by BIC in
`k ∈ {1, 2, 3, 4}`. k=1 means no sub-strategy detected; k>=2 means
parallel strategies present. `scikit-learn.mixture.GaussianMixture`.

**Minimum sample.** n >= 50 fills, per Della Vedova empirical guidance
(they use 96 algorithms × 16 weeks of consistent activity). Wallets
below this emit `clusters: {"status": "insufficient_sample"}`.

**Per-cluster metrics.** For each detected cluster:
- All v1 metrics (skew, drift_1h/6h/24h)
- All v2 metrics (drift_to_resolution, sell_quality_vs_resolution)
- Top-3 distinguishing features (e.g. "mostly political + long hold +
  mid-price")
- Fraction of wallet's fills in this cluster
- Realized PnL share (from Layer 4) attributable to this cluster

**Output addition.**

```json
"execution_profile": {
  ...,
  "clusters": {
    "status": "populated",  // or "insufficient_sample" / "single_strategy"
    "k_selected": 2,
    "bic_delta_vs_k1": 42.3,
    "clusters": [
      {
        "id": 0,
        "share_of_fills": 0.72,
        "distinguishing_features": ["tag=<slug>", "tag=<slug>", "hold=long", "entry=0.2-0.4"],
        "metrics": {"drift_to_resolution": {...}, "sell_quality_vs_resolution": {...}},
        "pnl_share_from_layer4": 0.91
      },
      {
        "id": 1,
        "share_of_fills": 0.28,
        "distinguishing_features": ["tag=<slug>", "hold=intraday", "entry=0.4-0.6"],
        "metrics": {...},
        "pnl_share_from_layer4": 0.09
      }
    ]
  }
}
```

**Layer 3 consumer.** When `clusters.k_selected >= 2`, Layer 3
dimensions read per-cluster (not pooled). The wallet knowledge page
records each cluster's archetype separately. A wallet may be labeled
`selector` on cluster 0 and `inventory` on cluster 1 — this is the
correct structural representation.

**Layer 5 consumer.** When clusters detected, operational implications
issue per-cluster verdicts:

```json
"operational_implications": [
  {
    "cluster_id": 0,
    "verdict": "reverse_engineer_candidate",
    "rationale": "political long-hold cluster with strong drift_to_resolution"
  },
  {
    "cluster_id": 1,
    "verdict": "exclude_for_copy",
    "rationale": "sports intraday cluster shows negative drift + cut_winners"
  }
]
```

**Performance cost.** GMM fit on 100-500 fills × 8 dims is
milliseconds. PCA + one-hot is milliseconds. Negligible in the
shortlist-runner budget (already ~60s per wallet for the probe).

## 11f. Cross-metric consistency checks

Pre-registered invariants. Violations flag `inconsistency_detected`
and downgrade downstream confidence.

| Invariant | Check | Violation meaning |
|-----------|-------|-------------------|
| `drift_to_resolution.win_share` ≈ `returns.positive_share` | within `2/sqrt(n)` | Lookup bug or category-mix difference |
| Per-category positive_share × size agrees with `outcome_attribution.pnl_by_tag` | within 10% | PnL attribution inconsistent with win rate × size |
| `entry_timing_edge = high` → pooled drift_to_resolution mean >= 0.15 | hard rule | Shouldn't be reachable via promotion rules |
| `exit_management_edge = high` → sell_quality.escapes > 0 | hard rule | Promotion rule guard |
| Σ(cluster.pnl_share) ≈ 1.0 | within 5% | PnL attribution gap |

These are cheap. Run after aggregation, before Layer 3 mapping. Any
violation logs at WARN + adds to `data_quality.inconsistencies`.

## 11g. Multi-strategy and regime-shift handling (summary)

The wallet brain must handle three kinds of sub-structure a single
pooled metric hides:

1. **Parallel strategies.** Directional on political + inventory on
   sports + basket on election fields, all same wallet.
2. **Temporal shifts.** A wallet that was noise for 6 months then
   became a selector.
3. **Conviction-dependent edge.** Strategy works on large bets, is
   noise on small ones (or vice versa).

v2 addresses all three with the sections above, in order:

| Failure mode | v2 mechanism | Section |
|--------------|--------------|---------|
| Parallel strategies by category | `by_tag` stratification + sign-agreement guard | §6 schema, §11g rules |
| Parallel strategies beyond category | **Strategy clustering (GMM + BIC)** on per-fill feature vectors | §11e |
| Temporal shifts | **PELT changepoint detection** on per-fill drift series | §11d |
| Conviction dependence | **Size-weighted mean** reported alongside equal-weight; `conviction_dependent_edge` flag | §11a |
| Hold-duration strategy differences | `by_hold` stratification on all metrics | §11b |
| Active management flavor | Trim vs full-close distinction | §11c |
| Internal inconsistency | **Cross-metric consistency checks** | §11f |

Per-cluster outputs propagate through Layer 3 (per-cluster edge
dimensions) and Layer 5 (per-cluster operational verdicts).

**Hard promotion rule** (applies to all Layer 3 edge-dimension upgrades):

A wallet is NOT eligible for `medium` or `high` on any edge dimension
when any of these fire:
- `clusters.k_selected >= 2` AND the top-2 cluster means disagree on
  sign (use per-cluster metric, not pooled)
- `regime_shift_detected == true` (use post-shift regime only)
- `contradictory_sub_signals == true`
- Single category carries the pooled signal (`min(by_tag.mean) <=
  -0.10` while pooled `>= 0.15`) (use per-category metric, not pooled)

This is deliberately restrictive. The cost of over-promotion (false
confidence in an averaged mix) is higher than the cost of
under-promotion (a single-strategy wallet not getting `high`).

## 12. Open questions

1. **Tag taxonomy stability.** Polymarket's tag vocabulary is live and
   time-bound (tags that are hot this month may not exist in six months).
   v2's tag-handling is designed to survive this (§5 `Tag handling
   principle`): tags are opaque strings, never hardcoded, per-wallet
   top-K selection is data-driven. If Polymarket replaces their tag
   scheme wholesale, our code still runs; only the per-wallet tag
   vocabulary changes. No action needed until that happens.
2. **PELT penalty tuning.** Penalty = 5 is pre-registered for n >= 50
   per BIC heuristic. If production data shows systematic
   over/under-segmentation, re-tune on a labelled fixture set. ADR required.
3. **GMM covariance type.** Starting with `covariance_type="full"`
   (per-cluster covariance). `diag` or `tied` are cheaper but encode
   assumptions; default to the most expressive.
4. **FIFO vs LIFO lot matching.** FIFO is pre-registered (§11b). LIFO
   would only matter under size-weighted interpretations of hold
   duration, which we already report separately. FIFO stays.
5. **Partial resolutions.** Polymarket's `RESOLUTION_WIN_THRESHOLD = 0.97`
   leaves an ambiguity band 0.03–0.97. Markets that settle in that band
   (rare, usually disputed) are excluded from v2 metrics with an
   `invalid_resolution` flag. Separate research question if the frequency
   exceeds 1%.
6. **Agent retrieval.** Supabase knowledge entries
   `reference/tools/wallet-execution-profile` and
   `reference/vocabulary/wallet-mechanism-buckets` describe v1. After v2
   lands, both need updates. Runbook item.

## 13. Rollout

Phased to land smallest-risk first. Each phase is one commit with tests;
no phase breaks v1 consumers.

### Phase 1 — Foundations (no new Python dependencies)

1. Create `market_tags (condition_id TEXT, tag TEXT, PRIMARY KEY
   (condition_id, tag))` table via migration. Index on `(tag)` for
   reverse lookups.
2. Backfill script: walk existing gamma /events pages, insert all
   tag slugs verbatim into `market_tags` for every `conditionId`.
   Rate-limited 5 req/s; ~1 hour for full backfill.
3. Deprecate `_THESIS_CATEGORY_MAP` in `polymarket_api.py` (leave
   stubbed for existing callers, but mark `# DEPRECATED: v2 uses raw
   gamma tags via market_tags table`). Update `_fetch_event_category_by_condition`
   to also write raw tags into `market_tags`.
4. `_outcome_native_resolution(condition_id, outcome_label)` helper +
   tests against the 1896-row local `market_outcomes` table.
5. `_select_sells(activity)` mirror of `_select_buys`.
6. `_pair_buys_to_exits(activity)` FIFO matcher → `FillPair` dataclass
   with `exit_type ∈ {trim, close, resolve, open}`, `hold_duration_sec`.
7. `_tags_for_condition(condition_id)` helper: single SQL read into a
   set of tag slugs. Returns `set()` when unclassified; caller handles.
8. Extend `_aggregate` with `drift_to_resolution`,
   `sell_skew_vs_preceding_bar`, `sell_quality_vs_resolution`,
   size-weighted mean variants, hold-duration stratification,
   trim/close split, and `by_tag` / `unclassified` / `dominant_tags`.
9. `_cross_metric_checks(aggregated)` invariants from §11f.

### Phase 2 — Layer 3 / Layer 4 wiring

6. Revise `_build_edge_hypotheses` entry_timing + exit_management rules
   (spec §8, §8.1) with the sign-agreement guard and the hard promotion
   rule from §11g.
7. Extend `_build_outcome_attribution` with `resolution_drift` and
   `sell_quality` subsections.

### Phase 3 — Regime-shift detection

8. Add `ruptures>=1.1` to `requirements.txt`.
9. `_detect_regimes(fill_ts, drift_series)` → PELT with penalty=5,
   returns changepoints and per-regime metrics.
10. Envelope extension: `regimes` + `regime_shift_detected`.

### Phase 4 — Strategy clustering

11. Add `scikit-learn>=1.3` to `requirements.txt` (already a transitive
    dependency of several modules; explicit pin).
12. `_build_cluster_features(fills)` → one-hot + PCA to 8 dims.
13. `_cluster_strategies(features)` → GMM k∈{1..4} with BIC selection.
14. Envelope extension: `clusters` with per-cluster metrics.
15. Revise Layer 3 to read per-cluster when `k_selected >= 2`; revise
    Layer 5 for per-cluster operational verdicts.

### Phase 5 — Validation

16. Re-run SwissMiss + kch123 + arb fixtures:
    - **Arb**: drift_to_resolution win_share ≈ 0.50, mean ≈ 0. Clusters
      either `k=1` or reflect separate YES/NO legs. Expected.
    - **kch123**: drift_to_resolution win_share ≈ 0.50 (inventory
      outcome-neutral). Clusters may split by market tag structure if present.
    - **SwissMiss**: THE test. Expected: positive drift_to_resolution
      on her long-hold tag cluster (whichever gamma slugs dominate her
      by_tag output — the code never assumes specific slugs), near-zero
      on short-horizon tags. Either:
      (a) clusters k=1, wallet is coherent long-horizon selector
      (by_tag handles the split), OR
      (b) clusters k>=2, formally identifying the sub-strategies.
      Both answers are informative.
17. Synthetic fixtures for each new capability:
    - Regime-shift synthetic: 50 neutral fills then 50 +drift fills →
      PELT must detect the shift at fill 50.
    - Conviction-dependent synthetic: small-size fills neutral, large-size
      fills +drift → `conviction_dependent_edge` fires.
    - Multi-strategy synthetic: two deliberate sub-populations → GMM
      must pick k=2.
18. Supabase knowledge updates:
    - Update `reference/tools/wallet-execution-profile` with v2 fields
    - Update `reference/vocabulary/wallet-mechanism-buckets` with the
      per-cluster labeling pattern (a wallet can have multiple buckets)
    - New: `reference/tools/market_tags-table` — documents the raw-tag
      table and the opaque-tag principle (§3.1) so downstream agents
      never re-introduce a hardcoded category map.

### Phase 6 — Rollout

19. Run v2 on the 18-wallet knowledge/wallets/ shortlist with
    `--with-execution`. Spot-check the per-cluster verdicts for sanity.
20. Document surprises in a rollout memo at
    `docs/research/scopes/<date>-wallet-exec-v2-rollout.md`.

### Phase budget

- Phase 1–2: ~1 day (all additive, uses existing fixtures)
- Phase 3: ~0.5 day (ruptures is mature, well-tested)
- Phase 4: ~1-2 days (clustering needs synthetic validation)
- Phase 5: ~0.5 day live + ~0.5 day analysis
- Phase 6: ~0.5 day

Total: ~4-5 days. The v1 spec was shipped in one session with the
simpler scope; v2 is genuinely larger.

## 14. Bottom line

v2 is the complete wallet-brain measurement spec. Every failure mode
we identified in v1's rollout is addressed:

1. **Entry timing on long-hold wallets** → `drift_to_resolution`
   (correct-horizon BUY metric). §6, §7.
2. **Sell quality** → `sell_skew_vs_preceding_bar` (execution quality)
   + `sell_quality_vs_resolution` (outcome quality, with escape /
   cut_winner / neutral classification). §6, §7.3.
3. **Parallel strategies** → GMM clustering on per-fill features;
   per-cluster metrics propagate to Layer 3 and Layer 5. §11e.
4. **Temporal regime shifts** → PELT changepoint detection on
   time-ordered drift series; use most-recent regime only for Layer 3
   promotion. §11d.
5. **Conviction-dependent edge** → size-weighted mean reported
   alongside equal-weight; `conviction_dependent_edge` flag when they
   diverge. §11a.
6. **Hold-duration dependence** → FIFO buy-exit pairing, `by_hold`
   stratification on every metric. §11b.
7. **Active-management flavor** → trim vs full-close split in sell
   metrics. §11c.
8. **Internal inconsistency** → pre-registered invariants block bad
   data from propagating. §11f.
9. **Taxonomy rot** → tags treated as opaque strings, no hardcoded
   list anywhere in v2. Pipeline survives Polymarket's taxonomy
   evolution automatically. §3.1.

**Promotion is restrictive by design.** Any sub-structure flag fires →
no pooled-mean promotion allowed; consumers read per-cluster or
per-regime metrics instead. Cost of under-promotion < cost of
over-promotion.

**Dependencies added:** `ruptures>=1.1`, `scikit-learn>=1.3` (explicit
pin — already a transitive dep). `market_outcomes.resolved_price`
must be populated (1896 rows locally; runbook check).

**Known remaining limits (documented, not hidden):**

- GMM k capped at 4. Wallets genuinely running more than 4 distinct
  strategies would be under-identified. Raising k requires larger n
  per cluster; Polymarket wallets rarely have enough fills.
- Strategy clustering does not model correlations between clusters
  (e.g. hedging between political thesis + sports sizing). Out of scope.
- Per-cluster verdicts assume clusters are stable over time. If a
  wallet's cluster structure itself shifts, that's a regime change at
  a level PELT on pooled metrics won't detect. Rare; manual review
  catches via `inconsistency_detected`.

**Expected classifications after v2 rollout:**

- **Selectors** (SwissMiss-class): positive drift_to_resolution + flat
  drift_1h + long holds + low sell_ratio. Edge validated, copy-at-short-
  horizon blocked by negative drift_1h.
- **Timers** (directional, short-hold): positive drift_1h + positive
  drift_to_resolution + clustered on short hold durations. First
  confirmed member would earn the `timer` vocabulary bucket.
- **Active managers**: positive sell_quality_vs_resolution + escapes >
  cut_winners + modest sell_ratio. Exit skill validated.
- **Panic sellers**: negative sell_quality + cut_winners > escapes.
  Exit skill disproved; never promote `exit_management_edge` on
  count alone.
- **Mixed-strategy wallets**: clusters k>=2 with divergent per-cluster
  edge; Layer 5 issues per-cluster verdicts. The multi-strategy
  question answered in full.

None of these claims v1 alone could make. All of these are
falsifiable via the committed fixtures + synthetic test harness.

The pipeline is as complete as it needs to be for the measurement
question we set out to answer.

## Sources

- Della Vedova, Fouladirad, Marmi et al. 2024. *Statistical Predictions of Trading Strategies in Electronic Markets.* Journal of Financial Econometrics 23(2). https://academic.oup.com/jfec/article/23/2/nbae025/7826742
- Aminikhanghahi, Cook 2017 (and ruptures Truong et al. 2020). *ruptures* library — offline changepoint detection with PELT. https://centre-borelli.github.io/ruptures-docs/
- Hamilton 1989. *A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle.* Baseline for regime-switching economics.
- TRM Labs 2026. *How Prediction Markets Scaled to USD 21B in Monthly Volume.* Wallet-behavior segmentation by trade frequency. https://www.trmlabs.com/resources/blog/how-prediction-markets-scaled-to-usd-21b-in-monthly-volume-in-2026
