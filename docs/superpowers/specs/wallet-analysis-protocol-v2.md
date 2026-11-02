---
memory_name: methodology/wallet-analysis-protocol-v2
memory_type: knowledge
memory_description: Pre-registered protocol for per-wallet edge analysis on Polymarket. Supersedes v1. Adds Stage 0–3 pipeline, mode router (held-analyst / flipper / mixed), flipper branch FH1–FH6, hierarchical tag inference, mixed-mode decision matrix, and 5 new limitations. All v1 fixes locked: CUSUM calibration placeholder, eventSlug-only independence, H5 confidence_flag=low_population.
memory_tags: [methodology, protocol, wallet, pre-registered, inference, sound, v2]
---

# Wallet Analysis Protocol v2

**Date:** 2026-04-25
**Status:** pre-registered, locked
**Author:** research-methodologist agent
**Supersedes:** wallet-analysis-protocol-v1 (docs/superpowers/specs/wallet-analysis-protocol-v1.md,
  brain entry `methodology/wallet-analysis-protocol-v1`)
**Parent review:** `finding/methodology-review/wallet-protocol-flipper-blindspot-2026-04-25`
**Methodological standards applied:** brain entry `research-12-methodological-standards-2026-04-02`
**Self-review:** Appendix A (methodology-review format)

v1 was sound for held-analyst wallets. This version adds the flipper branch (FH1–FH6) that
v1 was structurally blind to, introduces a mode router, formalizes Stage 0–3 pipeline stages,
extends multi-tag inference to hierarchical tags, and resolves three v1 follow-ups that were
required before shipping `analyze_wallet()`:

1. CUSUM heavy-tail calibration — placeholder in §9.1, populated by `cusum-calibration-2026-04-25`
2. Independence rule simplified to eventSlug-only — §7 (24h+tag clause dropped)
3. H5 `confidence_flag=low_population` — already implemented, locked in §5.5

Do not modify hypotheses or inference procedures after running analysis on a wallet. Changes
require wallet-analysis-protocol-v3 with a new date.

---

## 1. Scope and intent

This protocol determines whether a specific Polymarket wallet has defensible trading edge,
what kind, whether it is durable, and — critically — whether the wallet's mode of trading is
one the analysis pipeline can even observe. For wallets that flip positions before resolution,
the v1 held-analyst hypotheses are blind; this version adds a parallel flipper branch with its
own pre-registered tests.

**Archetype label mapping (important):** The operator uses "market_maker" as shorthand for
non-directional inventory management. The code (`scripts/wallet_archetype_extractor.py:772`)
defines `_LAYER1_TERMINAL = {"paired_outcome_arb", "two_sided_inventory_trader"}` — there is
no label `market_maker`. These two labels correspond to what the operator means. Throughout
this protocol, "hedge archetype" refers to `paired_outcome_arb` OR `two_sided_inventory_trader`.

---

## 2. Protocol stages

The analysis pipeline has four stages. This protocol covers Stages 1 and 2 in full.
Stage 0 (nomination) and Stage 3 (action decision) are out of scope.

```
Stage 0 — Detection: manual nomination OR auto-detection (separate pipeline, not this protocol)
Stage 1 — Cheap-reject gates (§3): PASS / REJECT_<reason>
Stage 2 — Deep analysis (§4–§11): this protocol's body
Stage 3 — Decision: copy / engineer derivative / skip (downstream of this protocol)
```

**Important about "cheap": Stage 1 gates require the same data fetches as Stage 2** — all
four API calls in §4 must complete before Stage 1 can check `n_closed_positions`, `n_open_dead`,
and `last_activity`. "Cheap" means skipping the expensive statistical analysis (bootstrap,
BH-FDR, CUSUM, hypothesis testing) when a gate fails — it does not mean skipping data fetch.

---

## 3. Stage 1 — Cheap-reject gates

Run gates in this order after completing data fetch (§4). Record the gate result in the report.
The first failing gate is the REJECT reason; do not evaluate subsequent gates.

### 3.1 Hard rejections (REJECT — no analysis)

**R1 — Hedge archetype**
Layer 1 archetype from `wallet_archetype_extractor.py` is `paired_outcome_arb` OR
`two_sided_inventory_trader`. These wallets are structurally non-copyable. Tag-level inference
(H5/FH4) is also invalid because their winning and losing legs are correlated by construction.
Reject; record `REJECT_hedge_archetype`.

Note: these wallets' positions ARE included in peer distributions for tag baselines in B1/B2.
The peer-baseline includes rejected wallets; it is not survivorship-biased. If this changes
(e.g., B1 filters its peer set), update this note.

**R2 — Insufficient sample**
`n_closed_positions < 30`. Analysis will be underpowered on every Stage 2 test. Record
`REJECT_insufficient_sample`. (A wallet with n_closed=30 that passes Stage 1 may still be
underpowered on specific Stage 2 tests — that is expected, not a bug; see power table §5.8.)

**R3 — Dormant**
`last_activity > 90 days ago`. Regime changes in an idle wallet render historical analysis
non-projectable. Record `REJECT_dormant`.

### 3.2 Caution flags (PROCEED with flag — do not reject)

These do not block analysis. Record them in the "Data quality flags" section of the report.
They affect interpretation, not the decision to analyze.

**F1 — Sloppy operator**
`n_open_dead > 50` AND `n_open_dead / n_open_positions > 0.50`. Wallet holds large numbers
of expired-and-worthless open positions. Win-rate estimates using `/positions` unrealized PnL
are stable (dead positions already at curPrice=0), but the wallet's operational discipline is
poor. Note: this is a behavioral flag, not a rejection criterion.

**F2 — Single-bet concentration**
Top 3 positions by notional represent > 70% of total closed realized PnL. Edge estimates
derived from small samples dominated by one or two big bets are noisy; CI will be wide.

**v3 amendment B (procedural gate, not just a flag):** When F2 fires, the held-analyst
branch ALSO runs an `H1_sub_top3` diagnostic — a second winsorized bootstrap on the
P&L series excluding the three dominant positions. The diagnostic does NOT enter the
BH pool (it would inflate multiplicity); it is reported in `H1.details.sub_top3` and,
when its CI lower bound > 0 while H1 (winsorized) is `inconclusive`, the report flag
`EDGE_CONDITIONAL_ON_TOP3_EXCLUSION` is emitted. Operators reading that flag should
interpret: "the rest of the book is profitable, but the top-3 lottery prevents the
H1 CI from clearing zero — verdict is conservative."

**F3 — Recent-regime concentration**
> 70% of closed positions fall within the last 90 days. Edge estimates may not generalize
to different market regimes. (This overlaps with §6.2 tag-time interaction.)

**F4 — Heavy CTF**
Layer 1 execution profile shows > 50% of fills appear to be CTF redemptions rather than
CLOB trades. Some wallet activity may be post-resolution position claims, not trading decisions.

---

## 4. Data retrieval — mandatory before any analysis

**Complete all four steps before looking at performance numbers.** Computing the mode-router's
`flip_rate` (§5) requires `/activity`; do not peek at WR or PnL from `/closed-positions`
before committing to the analysis plan.

### 4.1 Closed positions

Fetch `/data-api.polymarket.com/closed-positions?user=W` exhaustively:
- `limit=50` (server cap; see brain entry `research-polymarket-closed-positions-pagination-2026-04-25`)
- `sortBy=TIMESTAMP`, `sortDirection=ASC` for stable pagination
- `max_pages=100`; log pages fetched
- If `n_pages == 100` and last page full: log "pagination cap hit — lifetime history may be truncated"

Never use default sort (DESC by realizedPnl) — returns only 50 biggest wins, producing
100% WR on net-losing wallets.

### 4.2 Open positions

Fetch `/data-api.polymarket.com/positions?user=W` exhaustively:
- `limit=100`
- For each open position: `unrealized_pnl = (curPrice - avgPrice) * size`
- Count `n_open_dead` = positions where `curPrice <= 0.01`

### 4.3 Activity

Fetch `/data-api.polymarket.com/activity?user=W&type=TRADE` up to 3000-row cap.
- Log `n_activity_rows` returned
- If `n_activity_rows == 3000`: flag "activity cap hit — behavioral analysis covers recent
  trades only; flip-rate may be undercounted"
- Do NOT use activity rows for win rates or PnL (partial history)

**Activity-cap + mode-router interaction (important):** A high-volume flipper's recent
activity may show fewer flips than lifetime behavior (old flips truncated by cap). This
under-counts `flip_rate` and can mis-route the wallet to the held-analyst branch when it
is in fact a flipper. This is undectable from the data alone. If `n_activity_rows == 3000`,
report `mode_router_confidence = low` and flag "flip_rate may be understated — mis-routing
to held-analyst branch is possible."

### 4.4 Execution profile

Run `scripts/wallet_archetype_extractor.py <wallet> --with-execution`. Store `execution_profile`.
Stage 1 Layer 1 archetype comes from this step.

### 4.5 Net PnL (pre-analysis computation)

```
net_pnl = sum(closed_realized_pnl) + sum(open_unrealized_pnl)
```

Never use `closed_realized_pnl` alone. (SemyonMarmeladov pattern: apparent +$17.7M,
true -$1.54M once open dead positions included.)

### 4.6 Tag attribution

Join closed + open positions to `market_tags` on `condition_id`. Unmatched → tag = `"unclassified"`.
For each position record all tags, not just first. For inference, each position contributes
independently to every tag grain it belongs to (multi-tag membership, see §8).

**Skip list — filter before any inference (not topic tags):**
```
hide-from-new, recurring, 5m, 1h, 4h, 15m, up-or-down, neg-risk, multi-strikes,
all, today, weekly, daily, uptspt-* (any uptspt prefix)
```
Remove these from the tag set before computing tag-grain membership. Do not run H5/FH4
on them. They are internal tooling / time-series format / market-mechanic flags, not
topic tags.

**Ambiguous tags — flag for operator resolution before inference runs:**
`awards`, `world`, `tech` (overlaps with the nav-bar `Tech`), `economy`/`finance` boundary.
When any of these appear in a wallet's top-5 tags by position count, add to the report's
"Data quality flags" section with note: "ambiguous nav_category mapping — verify
`tag_nav_category` lookup before treating nav-grain verdict as reliable."

### 4.7 Flip reconstruction (new in v2 — required before flipper branch)

FIFO-match BUY→SELL pairs from `/activity` per `(wallet, conditionId, outcome)`:
1. Sort activity rows by `timestamp ASC` per `(conditionId, outcome)`
2. FIFO: each BUY is matched to the next SELL. Unmatched BUYs (no subsequent SELL) = held
3. Per matched pair: `flip_roi = (sell_price - buy_price) / buy_price`, `flip_notional = buy notional`, `hold_duration_seconds = sell_ts - buy_ts`
4. `n_flips = count(matched pairs)`
5. `flip_rate = n_positions_with_pre_resolution_sells / n_closed_positions`
   where "pre-resolution sell" = SELL event on a condition_id before it resolved (use resolution
   timestamp from `/closed-positions`; if unavailable, proxy with curPrice ∈ {0,1})

**Fail loudly:** If `flip_rate > 0.50 AND n_activity_rows == 3000`, record `FLIPPER_ANALYSIS_TRUNCATED`
and do NOT run flipper branch. Return "cannot analyze — activity cap prevents reliable flip
reconstruction for high-volume flipper." This is honest "cannot analyze" territory until
Subgraph integration provides full lifetime history.

---

## 5. Mode router

Compute `flip_rate` from §4.7, then route:

| flip_rate | Branch |
|-----------|--------|
| < 0.30 | held-analyst only |
| 0.30–0.70 | BOTH branches; two independent verdicts; do not aggregate |
| > 0.70 | flipper only |

Override: if `FLIPPER_ANALYSIS_TRUNCATED` → held-analyst only (with flag).
Override: R1 hedge archetype → analysis rejected, no routing.

**Threshold justification:** 0.30 and 0.70 are judgment calls with no empirical grounding.
They encode: "mostly held" and "mostly flipped" with a mixed zone in between. Revisit when
labeled wallet set exceeds 50 wallets with known mode. State this openly in every mode-router
output.

---

## 6. Stage 2 — Held-analyst branch (H1–H7)

v1 hypotheses, infrastructure, and inference procedures carry forward unchanged. The three
v1 follow-ups that were required before shipping are locked here.

### 6.1 Hypotheses

H1–H7 are defined in wallet-analysis-protocol-v1 §3. Read that document for the full
specification. Changes from v1 are listed below; all other v1 clauses remain in force.

**H2 — Sub-0.80 entry filter (ADR-002):** Filter applies in held-analyst branch. Held-analyst
wallets who systematically buy near resolution are certainty-buyers, not informed traders.
This filter does NOT apply in the flipper branch (§7.2).

**v3 amendment — H2_sub080 conditional promotion:** When full-sample H2 is REFUTED
AND H2_sub080 has `n_held >= H2_MIN_N` (i.e., sufficient sample after filtering out
≥0.80 entries), H2_sub080 joins the primary consultation set used by §11. The full
H2 result remains in the BH pool; H2_sub080 is always in the BH pool regardless of
H2's outcome (independent test). Only the §11 verdict-derivation step gives H2_sub080
"primary" weight conditionally — and only on the held branch (not flipper).

Additionally, when H2 is refuted AND H2_sub080 ran with sufficient n, the report
emits the data-quality flag `CERTAINTY_BUYER_DETECTED`. Operators interpret this
as: "The wallet is a certainty-buyer; the meaningful entry-timing test is the
sub-0.80 sample. If H2_sub080 is also refuted, the wallet is neither informed
nor a certainty-buyer with edge."

**v3 amendment — H1 winsorized:** H1's verdict is driven by a winsorized bootstrap
(top+bottom 5% clipped) on per-position P&L. The raw sum bootstrap is preserved in
`H1.details.raw_sum_ci` for transparency. Rationale: empirical P&L distributions
have kurtosis ≈ 918 (calibration sample), which breaks percentile-bootstrap CI on
the sum — the eatpraylove case (1383 positions, +$449K realized) had raw-sum CI
[−$294K, $1.4M] while the central-tendency mean was clearly positive. Ref: Wilcox
(2012) ch. 4. Brain memory: `methodology/wallet-protocol-v2-gaps-from-eatpraylove`.

**H3 — Skip condition:** In addition to v1's archetype override (two_sided_inventory_trader,
paired_outcome_arb), skip H3 if mode_router routed to flipper-only. H3 is N/A for pure flippers.

**H5 — confidence_flag=low_population (locked):** Refuse inference when peer_count < 10.
Set `confidence_flag=low_population`, verdict = `inference_not_run`. This was follow-up
item 3 in v1; it is now a hard requirement.

**H7 — CUSUM calibration (RESOLVED):** Use `h = 25.0` (k = 0.5σ). Calibrated 2026-04-25
against empirical P&L from 26,359 closed positions across 21 wallets (excess kurtosis 918;
~1000× heavier-tailed than Gaussian). At h=5 (manufacturing default), false-alarm rate was
63.2% under the null; at h=25.0, false-alarm rate is 3.1%. ARL₀ ≈ 6,500 positions. Brain
memory `cusum-calibration-pnl-heavy-tail-2026-04-25` documents the simulation. Power
trade-off: at h=25 the test has lower power to detect real regime changes, but false
detection (which truncates the analysis sample on a spurious boundary) is the worse
failure mode. If brain memory is missing at runtime, fall back to `h=5` and flag
`CUSUM_UNCALIBRATED` — this indicates a deployment error, not an acceptable operational state.

### 6.2 Independence rule (simplified from v1)

**v2 rule:** Two positions are in the same cluster if they share the same `eventSlug`.

The v1 rule added a second clause: timestamps within 24h AND same primary tag. That clause
produced transitive over-clustering in validation (Russia + Venezuela + Pokrovsk merged
because they shared both the `geopolitics` tag and consecutive trade dates, despite being
independent geopolitical markets). Dropping the 24h+tag clause eliminates this.

**Known limitation (document in every report):** Cross-event news-cycle correlations are not
captured. A wallet that bets heavily on all Iran-related markets during a crisis spike will
show many different eventSlugs, and the independence assumption will treat those positions as
uncorrelated even though the underlying information event is shared.

### 6.3 Temporal separation

Same as v1 §5: time-split 70/30 for wallets with `n_closed_positions >= 60`. Re-run H2,
**H2_sub080** (v3 amendment), and H3 on training; if supported, re-run on test. The
spec's original two-test loop {H2, H3} is extended to {H2, H2_sub080, H3} so that
ADR-002's sub-sample is also gated by OOS survival.

### 6.4 Power table (held-analyst branch)

Same as v1 §3.8. "Inconclusive" is expected for most wallets at n < 100.

---

## 7. Stage 2 — Flipper branch (FH1–FH6)

All tests in this branch operate on the flip-pair dataset from §4.7. The unit of observation
is the FIFO-matched (BUY, SELL) pair — not the position record, not the /closed-positions row.
This must be stated explicitly in every flipper-branch output section.

### 7.1 FH1 — Mean per-flip ROI positive (gate hypothesis)

**What:** `mean(flip_roi) > 0` across all reconstructed flips.
**Null:** Zero mean. Bootstrap CI, 10,000 samples.
**Minimum n_flips:** 30. Below 30: "underpowered."
**Fee note:** ~89.6% of Polymarket fills are zero-fee maker orders. Default to zero-fee unless
execution profile indicates heavy taker usage. When maker/taker split unknown (Data API does
not expose), flag `fee_treatment=assumed_maker_zero`.
**ADR-002 does NOT apply:** Flippers may profitably flip at 0.85→0.88 (capturing 3pp with
tight risk). The sub-0.80 entry filter is a held-analyst construct that is wrong for flippers.
**Falsification:** CI lower bound <= 0 → refuted.

### 7.2 FH2 — Position size correlates with per-flip ROI (conviction signal)

**What:** Larger flips (USDC notional at entry) produce higher `flip_roi`.
**Test statistic:** Spearman ρ(entry_notional, flip_roi). Permutation null (shuffle `flip_roi`
labels 10,000 times).
**Minimum n_flips:** 30. Below: "underpowered."
**Falsification:** permutation p > 0.10 → no conviction signal.

### 7.3 FH3 — Per-flip ROI exceeds implied round-trip bid-ask

**What:** Mean `flip_roi` exceeds the implied round-trip bid-ask spread at the time of the flip,
estimated from `tape_midpoint` BUY/SELL pairs in the execution profile.
**Approximation:** `round_trip_cost = 2 * (fill_price - tape_midpoint) / tape_midpoint` for
BUY fills. Coarse; no L2 reconstruction possible.
**Flag:** When matched pairs in execution profile < 20: `confidence=low_spread_estimate`.
**Purpose:** Distinguishes "edge from direction" vs "edge from spread capture." Neither is wrong
to copy; the distinction matters for size-scaling.
**Falsification:** `mean(flip_roi) <= round_trip_cost_estimate` → flagged "spread-or-worse."

### 7.4 FH4 — Tag-conditioned flip ROI

**What:** For each tag grain (leaf + parent, per §8) with `n_flips >= 10`: is mean flip ROI
positive within that tag?
**Test statistic:** `mean(flip_roi)` for flips within that tag grain.
**Null:** Bootstrap from that tag grain's peer flip-ROI distribution (if population flip data
available), else zero.
**CI:** 10,000-sample bootstrap.
**Multiple testing within FH4:** BH FDR q=0.10 applied across ALL tag-grain tests for this
wallet in a single correction (leaf + parent pooled under one BH run — see §8.3).
**Minimum n per tag grain:** 10. Below: descriptive only, no inference.
**Population baseline:** Same caveat as H5. If peer population for a tag grain has fewer than
10 wallets with flip data, flag `confidence_flag=low_population` and do not claim FH4 supported.

### 7.5 FH5 — Hold-duration distribution (descriptive)

**What:** Distribution of hold durations across all reconstructed flips.
**Output:** median, p25, p75, fraction under 1h, fraction under 24h.
**Regime labels:**
- `scalper`: median < 1h
- `news_arb`: median 1h–24h
- `slow_flipper`: median 1d–7d
- `mixed`: no single regime dominant (p25 < 1h, p75 > 24h)
**This is descriptive only — no significance test.** Regime label informs copyability assessment
downstream (Stage 3). A scalper's edge may depend on latency; slow flippers are more copyable.

### 7.6 FH6 — Directional skill during hold window

**What:** Win rate on flips where market price moved in the wallet's direction during the
hold window, vs rate where market moved against. Separates directional skill from spread
capture.
**Data:** `prices-history` at 1h fidelity for each flip's hold window. For each flip: observe
market price at entry and exit. `price_direction = sign(exit_price_midpoint - entry_price_midpoint)`.
Flip "wins with market" if wallet bought YES, market moved up; or bought NO, market moved down.
**Test statistic:** WR when market moved in wallet's direction vs WR when it moved against.
**Null:** Market direction does not predict flip outcome. Permutation test (shuffle market
direction labels).
**Minimum n_flips:** 20. Below: "underpowered."
**Price-history coverage gate (locked per self-review I2):** Require `n_price_history_bars
>= 0.80 * n_flips` matching at least one bar in each flip's hold window. Below this:
"underpowered" with reason `insufficient_price_history_coverage`. Short-hold scalper flips
may exit before the next hourly bar appears, making FH6 untestable for high-frequency
flippers without sub-1h fidelity.
**Falsification:** p > 0.10 → no directional signal above flip base rate.

### 7.7 BH-FDR correction (flipper branch)

Apply BH FDR q=0.10 across {FH1, FH2, FH3, FH4-aggregate, FH6} for this wallet.
FH4 per-tag tests are combined under a single BH correction together with the branch-level tests.
FH5 is excluded (descriptive only).

### 7.8 Time-split (flipper branch)

For wallets with `n_flips >= 60`:
- Sort flips by `buy_timestamp ASC`
- Training: first 70%; test: last 30%
- Re-run FH1 and FH2 on training; if supported, re-run on test
- Underpowered test-set (n < 20): "insufficient history for OOS validation"

### 7.9 Power table (flipper branch)

Same structure as v1 §3.8 but for flips:

| n_flips | Min detectable mean flip ROI (FH1) | Min detectable Spearman ρ (FH2) |
|---------|-------------------------------------|----------------------------------|
| 30      | ~12pp                               | ~0.48                            |
| 60      | ~8pp                                | ~0.34                            |
| 100     | ~6pp                                | ~0.26                            |
| 200     | ~4pp                                | ~0.18                            |

Approximate. "Inconclusive" is the expected verdict for most flippers unless n_flips > 60.

---

## 8. Tag inference — two-grain model

### 8.1 Taxonomy model (LOCKED — flat lookup, not DAG)

**The DAG/tree assumption from earlier v2 drafts is dropped.** Web-researcher investigation
2026-04-25 (brain memory `research/polymarket-tag-taxonomy-2026-04-25`) found:
- Polymarket's `/tags` endpoint returns 600+ flat slugs with no `parentCategory` field
- The legacy `/categories` endpoint (2022-era, 9 roots, 54 children) does NOT match current
  nav-bar categories — Iran, Geopolitics, Esports, Elections, Economy, Weather, Mentions
  are all absent from it
- **Polymarket already multi-tags markets at BOTH leaf AND nav-category levels simultaneously.**
  A market tagged `{iran, geopolitics, middle-east}` carries both `iran` (leaf) AND
  `geopolitics` (nav) directly in `market_tags`. No hierarchy traversal needed.

**Primary reference table:**
```sql
CREATE TABLE IF NOT EXISTS tag_nav_category (
  tag          TEXT PRIMARY KEY,
  nav_category TEXT NOT NULL,  -- one of the 13 nav-bar slugs, or 'other'
  confidence   TEXT NOT NULL DEFAULT 'manual'  -- 'manual' | 'api-derived' | 'inferred'
);
```

**The 13 nav-bar categories** (the parent grain): `politics`, `sports`, `crypto`, `esports`,
`iran`, `finance`, `geopolitics`, `tech`, `culture`, `economy`, `weather`, `mentions`,
`elections`. Plus `other` for unclassified.

**Bootstrap path:** Manually map ~50 high-frequency tags from `market_tags` (covers >90% of
position volume). Backfill remainder via `gamma-api.polymarket.com/events/slug/{event_slug}`
which returns each event's `category` string. Use `market_tags → market_metadata.event_slug
→ gamma category` join.

**Skip list — exclude before any inference (not topic tags):**
```
hide-from-new, recurring, 5m, 1h, 4h, 15m, up-or-down, neg-risk, multi-strikes,
all, today, weekly, daily, uptspt-* (any uptspt prefix)
```
These are internal tooling / time-series format / market-mechanic flags. Filter them out
of `market_tags` before computing tag grain membership.

**Ambiguous tags — flag for operator resolution:**
`awards`, `world`, `tech` (overlaps with the nav-bar slug `Tech`), `economy`/`finance`
boundary. When any of these appear in a wallet's top-5 tags by position count, add to
the report's "Data quality flags" with note: "ambiguous nav_category mapping — verify
`tag_nav_category` lookup before treating nav-grain verdict as reliable."

**Fallback when `tag_nav_category` not yet seeded:** Use direct tag membership only (no
nav-category grain testing) and flag `hierarchy_source=none`. The rarity-proxy approach
from earlier drafts is **removed** — it approximated a hierarchy that is already encoded
explicitly by Polymarket's multi-tagging, just at a different mechanism.

### 8.2 What "tag grain" means

Two grains are active:

- **leaf grain:** a specific tag slug (e.g. `iran`, `nba`, `bitcoin`). Position belongs to
  leaf grain `X` if `X ∈ market_tags` for its condition_id, after skip-list filtering.
- **nav grain:** one of the 13 nav-category slugs from `tag_nav_category`. Position belongs
  to nav grain `G` for every tag in its `market_tags` that maps to `G` in the lookup table.
  A position can belong to multiple nav grains if its tags map to multiple nav categories.

A position tagged `{iran, nuclear-weapons, geopolitics}` contributes to:
- leaf grains: `(iran, leaf)`, `(nuclear-weapons, leaf)`, `(geopolitics, leaf)`
  (`geopolitics` is also a nav slug — it can appear as a leaf tag directly if multi-tagged
  alongside more-specific tags)
- nav grain: `(geopolitics, nav)` — assuming `tag_nav_category` maps `iran` and
  `nuclear-weapons` to `geopolitics`

The BH correction (§8.3) handles the multiplicity inflation.

### 8.3 Multi-grain BH-FDR

Within H5 (held branch) or FH4 (flipper branch):
1. Enumerate ALL tag grains for which `n >= threshold` (10 for inference per H5/FH4),
   at BOTH leaf and nav grains, after skip-list filtering (§4.6)
2. Run one test per grain
3. Apply a single BH correction across ALL grains for this wallet in this branch
   (leaf + nav pooled, q=0.10)
4. Do not run separate BH corrections per grain level — that inflates family-wise error rate
5. If a nav grain has `confidence='inferred'` or matches an ambiguous tag in
   `tag_nav_category`, run the test but flag the row before reporting

### 8.4 Output format for tag inference

For each grain with inference run:

```
tag_grain: {slug} (leaf | nav)
hierarchy_source: tag_nav_category | none (table not seeded)
n_held (or n_flips) at this grain: {n}
WR_held (or mean_flip_roi): {value}
pop_baseline: {value}
95% CI: [{lower}, {upper}]
BH-adjusted p (within-branch grain pool): {value}
verdict: supported | refuted | inconclusive | underpowered | inference_not_run
```

For grains with verdict `supported`, also output:

```
leaf_vs_nav_agreement: agree | leaf_specialist | nav_generalist | conflict
```

- `agree`: both leaf and nav grain verdict = supported
- `leaf_specialist`: leaf supported, nav inconclusive (wallet focuses within one sub-topic)
- `nav_generalist`: nav supported, no individual leaf grain has sufficient n (wallet has
  edge across the parent category but no single sub-topic dominates)
- `conflict`: leaf refuted but nav supported, or vice versa — report both, flag for operator

**When conflict:** Output plain-language summary:
"Wallet shows edge at [nav] nav grain overall; individual leaf tag [X] is
[refuted/inconclusive]; [other leaf] has insufficient sample to assess."

### 8.5 Multi-tag membership mechanics

A position contributing to multiple tag grains (leaf and nav) contributes its `WR_held`
(or `flip_roi`) to EACH grain independently. Position count inflates per grain but the
single BH correction over the grain pool corrects for multiplicity. Do not deduplicate
across grains — deduplication would lose legitimate leaf signal that is distinct from nav signal.

---

## 9. Stationarity

### 9.1 CUSUM parameterization

Standard tabular CUSUM on cumulative realized PnL (or cumulative flip PnL for flipper branch):

```
cusum_t = max(0, cusum_{t-1} + (pnl_t - μ - k))
```

where `μ` = in-sample mean per-unit PnL, `k = 0.5σ`.

**Threshold (LOCKED):** `h = 25.0` (k = 0.5σ).

Calibrated 2026-04-25 against empirical P&L distribution from 26,359 closed positions
across 21 wallets. Distribution is heavy-tailed (excess kurtosis ≈918; Gaussian baseline
= 0). At the manufacturing default `h=5`, false-alarm rate under the null was **63.2%** —
unusable. At `h=25.0`, false-alarm rate is **3.1%**. ARL₀ ≈ 6,500 positions, well above
realistic per-wallet sample sizes (50–1500). See brain memory
`cusum-calibration-pnl-heavy-tail-2026-04-25` for full calibration details.

**Power trade-off:** At h=25.0, the test has lower power to detect real regime changes
than at h=5. This is intentional — false regime detection truncates analysis on spurious
boundaries, which is the worse failure mode. Wallets with gradual strategy drift over
~100 positions may not trigger CUSUM at this threshold. When stationarity is critical for
a specific wallet, supplement with visual inspection of cumulative PnL curve.

**Recalibrate when:** B1 population expands beyond ~50 wallets, since the empirical
distribution shape may shift (current sample is biased toward speculator_bots from the
discovery pipeline). Re-run `scripts/calibrate_cusum_pnl.py`.

If brain memory `cusum-calibration-pnl-heavy-tail-2026-04-25` is unavailable at runtime,
fall back to `h=5` and flag `CUSUM_UNCALIBRATED` in the report. This indicates a
deployment error; the calibrated value is locked and should not be absent.

### 9.2 Regime behavior

When CUSUM signals:
- Record signal-point as regime boundary
- Run held-analyst hypotheses on post-signal regime only if `n_post >= 20`
- Run flipper hypotheses on post-signal flips only if `n_flips_post >= 20`
- If post-regime sample too small: "regime change detected but insufficient post-regime
  sample for inference"

When CUSUM does not signal: analyze full history, report "no structural break."

---

## 10. Null and baseline construction

### 10.1 Wallet-level null (H1)

Empirical per-position PnL distribution across all wallets in B1 population.
Draw `n` samples (matching wallet's n_positions) with replacement, 10,000 times.
p-value = fraction of null draws >= observed net_pnl.

Population baseline caveat: B1 population is biased by our discovery pipeline. State in
every report until population expansion is complete.

### 10.2 Tag baseline (H5 / FH4)

Peer distribution of `win_rate_held_only` (H5) or mean `flip_roi` (FH4) from `wallet_tag_pnl`
for that tag grain. If fewer than 10 peers: `confidence_flag=low_population`, switch to
market-implied null (H5) or zero (FH4).

---

## 11. Mixed-mode decision rule

When `flip_rate` is 0.30–0.70, both branches run independently. DO NOT aggregate or average
the verdicts. Report both. Apply this matrix to produce the `overall_verdict`:

| Held verdict | Flipper verdict | Overall verdict |
|---|---|---|
| supported | supported | edge_detected (both modes) |
| supported | refuted | conflict — operator_review |
| supported | inconclusive | edge_detected (held) |
| refuted | supported | edge_detected (flipper) |
| refuted | refuted | no_edge |
| refuted | inconclusive | no_edge (held refutes) |
| inconclusive | supported | edge_detected (flipper) |
| inconclusive | refuted | no_edge (flipper refutes) |
| inconclusive | inconclusive | underpowered |

**"Supported" means:** BH-adjusted p < 0.10 for at least one of {H2, H3, H5} (held) or
{FH1, FH2, FH4} (flipper), AND survived time-split if applicable.

**v3 amendment — H2_sub080 conditional primary on held branch:** When full-sample H2
is REFUTED (the certainty-buyer signature) and H2_sub080 has `n >= H2_MIN_N`,
H2_sub080 is added to the primary set FOR THE PURPOSES OF THIS verdict-derivation
step only. The module-level primary tuple is unchanged; the BH pool is unchanged
(H2_sub080 is always in it). Without this amendment, certainty-buyer wallets with a
genuine sub-0.80 edge incorrectly resolve to `underpowered`. See gap analysis at
`docs/superpowers/specs/wallet-protocol-v2-gaps-from-eatpraylove.md`.

**"Refuted" means:** CI excludes positive performance (H1/FH1 bootstrap CI lower bound <= 0,
OR at least one primary test shows CI firmly excludes meaningful effect).

**"Conflict" verdict:** Both branches have strong opposing signals. Flag for operator review.
Do not force a resolution — this is methodologically honest. Common cause: wallet occasionally
flips losing positions but holds winners; the two behaviors have different edge characteristics.

---

## 12. Output report template

Every wallet report must include these sections in this order. Mark missing as "N/A — [reason]."

```
## Wallet Report: {wallet_address}
**Protocol version:** wallet-analysis-protocol-v2
**Analysis date:** {ISO 8601}
**n_closed_positions:** {n}
**n_held_to_resolution:** {n}
**n_open_positions:** {n}
**n_open_dead:** {n}
**net_pnl:** {$ value}
**activity_rows_fetched:** {n} {"(CAP HIT)" if n==3000}
**n_flips_reconstructed:** {n} | "N/A — held-analyst only" | "TRUNCATED — activity cap"
**flip_rate:** {value} | "N/A"
**mode_router:** held_only | flipper_only | mixed | truncated_held_fallback
**mode_router_confidence:** normal | low (activity cap — flip_rate may be understated)
**archetype (Layer 1):** {label}
**regime change detected:** yes | no

### Stage 1 result
- Gate result: PASS | REJECT_{reason}
- Caution flags: {list active flags from §3.2, or "None"}

### Data quality flags
{List all mandatory flags from §4 that triggered, or "None."}

### [HELD-ANALYST BRANCH] — H1–H7
(N/A if mode_router=flipper_only)

#### H1 — Net P&L gate
- observed: {$}
- bootstrap 95% CI: [{lower}, {upper}]
- verdict: supported | refuted | inconclusive

#### H2 — Held-to-resolution WR vs market-implied
- n_held_to_resolution: {n}
- WR_held: {%}
- mean_entry_price_held: {%}
- excess WR: {pp}
- 95% CI: [{lower}pp, {upper}pp]
- BH-adjusted p: {value}
- sub-0.80 filter: WR={%}, excess={pp}, CI=[{lower}, {upper}]
- verdict: supported | refuted | inconclusive | underpowered

#### H3 — Size-win correlation
- n: {n}
- Spearman ρ: {value}
- permutation p: {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered | N/A (archetype)

#### H4 — Entry timing (execution profile)
- execution_profile.status: {status}
- sample_tier: {unknown | medium | high}
- mean drift_6h: {value}
- 95% CI: [{lower}, {upper}]
- verdict: supported | refuted | inconclusive | underpowered

#### H5 — Tag-level edge concentration (hierarchical)
(See §8 for grain definitions.)
hierarchy_source: tag_nav_category | none (table not seeded)

For each tag grain with n_held >= 10:
  tag_grain: {slug} ({level})
  peer_count: {n}
  confidence_flag: ok | low_population
  n_held: {n}
  WR_held: {%}
  pop_mean_WR: {%}
  95% CI: [{lower}pp, {upper}pp]
  BH-adjusted p (grain pool): {value}
  verdict: supported | refuted | inconclusive | underpowered | inference_not_run
  leaf_vs_parent_agreement: {if applicable}

Tags with n_held < 10: {list with counts, "descriptive only"}
Leaf-specialist narrative: {plain-language summary of any conflict verdicts, or "None"}

#### H6 — Exit quality
- n_sell_events_available: {n}
- fraction where holding 24h would improve: {%}
- binomial p (null=0.50): {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered

#### H7 — Stationarity
- CUSUM_status: calibrated | CUSUM_UNCALIBRATED (h=5 fallback — deployment error)
- h_used: 25.0 (calibrated) | 5.0 (UNCALIBRATED — deployment error)
- k: 0.5σ
- CUSUM result: no break | break at position {k} on {date}
- pre-regime: n={n}, WR={%}, mean_pnl={$}
- post-regime: n={n}, WR={%}, mean_pnl={$}
- inference basis: full history | post-regime only | insufficient post-regime

#### Held-analyst time-split
- n >= 60: yes | no
- If yes: H2 training p={value}, test p={value}, holdout: survived | failed | underpowered
           H3 training p={value}, test p={value}, holdout: survived | failed | underpowered

#### Held-analyst independence check
- n_clusters (eventSlug rule): {n}
- fraction in clusters of size >= 3: {%}
- cluster-reduced H2: excess={pp}, CI=[{lower},{upper}]
- flag: concentrated | normal
- Note: 24h+tag clause removed in v2; cross-event news-cycle correlations not captured

#### Held-analyst overall verdict
Decision rule: "edge detected" requires AT LEAST ONE of {H2, H3, H5} supported (BH p < 0.10)
AND survived time-split. H1 must not be refuted.

- Held-branch verdict: edge_detected | no_edge | underpowered | data_insufficient
- Primary edge type: entry_timing | size_conviction | tag_specialist | unknown
- Tag specialist narrative (if applicable): {plain-language, leaf vs parent breakdown}

### [FLIPPER BRANCH] — FH1–FH6
(N/A if mode_router=held_only. "TRUNCATED — cannot analyze" if FLIPPER_ANALYSIS_TRUNCATED.)

Unit of observation: FIFO-matched (BUY, SELL) pair per (conditionId, outcome)
n_flips: {n}

#### FH1 — Mean per-flip ROI positive
- mean flip_roi: {%}
- bootstrap 95% CI: [{lower}%, {upper}%]
- fee_treatment: assumed_maker_zero | {actual if known}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered

#### FH2 — Size-ROI correlation
- Spearman ρ(entry_notional, flip_roi): {value}
- permutation p: {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered

#### FH3 — ROI vs implied spread
- matched_pairs for spread estimate: {n}
- spread_estimate_confidence: ok | low (< 20 pairs)
- estimated round-trip cost: {%}
- mean flip_roi: {%}
- verdict: exceeds_spread | spread_or_worse | underpowered

#### FH4 — Tag-conditioned flip ROI (hierarchical)
hierarchy_source: tag_nav_category | none (table not seeded)

For each tag grain with n_flips >= 10:
  tag_grain: {slug} ({level})
  peer_count: {n}
  confidence_flag: ok | low_population
  n_flips: {n}
  mean_flip_roi: {%}
  pop_baseline: {%}
  95% CI: [{lower}%, {upper}%]
  BH-adjusted p (grain pool): {value}
  verdict: supported | refuted | inconclusive | underpowered | inference_not_run
  leaf_vs_parent_agreement: {if applicable}

Tag grains with n_flips < 10: {list, "descriptive only"}
Leaf-specialist narrative: {or "None"}

#### FH5 — Hold-duration distribution (descriptive)
- median hold: {duration}
- p25: {duration}
- p75: {duration}
- fraction under 1h: {%}
- fraction under 24h: {%}
- regime_label: scalper | news_arb | slow_flipper | mixed
- copyability note: {one sentence — scalper edge may be latency-dependent; slow flipper more replicable}

#### FH6 — Directional skill during hold window
- n_flips with price-history: {n}
- WR when market moved in wallet's direction: {%}
- WR when market moved against: {%}
- permutation p: {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered

#### Flipper time-split
- n_flips >= 60: yes | no
- If yes: FH1 training p={value}, test p={value}, holdout: survived | failed | underpowered
           FH2 training p={value}, test p={value}, holdout: survived | failed | underpowered

#### Flipper-branch overall verdict
Decision rule: "edge detected" requires AT LEAST ONE of {FH1, FH2, FH4} supported (BH p < 0.10)
AND survived time-split.

- Flipper-branch verdict: edge_detected | no_edge | underpowered | data_insufficient
- Primary edge type: directional | spread_capture | tag_specialist | unknown
- FH5 regime: {scalper | news_arb | slow_flipper | mixed}

### Overall verdict (all modes)
{Apply mixed-mode matrix from §11 if mode=mixed, else use the single active branch verdict.}

- mode_router: {held_only | flipper_only | mixed}
- Overall: edge_detected | no_edge | underpowered | data_insufficient | conflict (operator_review)
- Active edge type(s): {list}
- Copyability note: {one sentence}
- Open questions: {what would change the verdict}

### Execution quality
- BUY fills analyzed: {n}
- skew_vs_preceding_bar mean: {value}
- drift_6h mean: {value}
- tape confidence: {n_high_confidence_fills}/{n_total_fills}
- short-horizon entry-timing edge: ruled out | not ruled out | underpowered
- v2 limits: [list from §13 relevant to this wallet]

### Multi-tag notes
- tags with any exposure: {list with position counts per grain}
- hierarchy_source: {tag_nav_category | none}
- Multi-tag grains active (both branches combined): {n unique grains tested}
- Secondary-tag exposure counted in inference: yes (multi-grain) | no (flat mode only)

### Limitations applicable to this wallet
{From §13 — mark which limitations specifically apply, one line each.}
```

---

## 13. Limitations

State these in every wallet report. Mark which specifically apply to the wallet under analysis.

**Inherited from v1 §10 (all still apply):**

1. **Survivorship in observation set.** We see wallets that traded enough to surface. Edge
   estimates are upward-biased relative to true Polymarket distribution.
2. **Closed-positions self-selection.** `/closed-positions` returns only redeemed/sold positions.
   Unredeemed dead positions only visible via `/positions`. Wallets with high `n_open_dead`
   have WR estimates that overstate true resolution rate.
3. **Activity 3000-cap.** For wallets with >3000 lifetime trades, `/activity` covers recent
   behavior only. H4/H6/FH1–FH6 reflect current style, not lifetime.
4. **No order-book reconstruction.** True slippage uncomputable. FH3 spread estimate is
   coarse (tape_midpoint only).
5. **Population baseline bias.** Population derives from our discovery pipeline.
   Percentile estimates have wide CIs. Treat within-tag rank as illustrative peer comparison
   until B1 population expansion.
6. **Independence assumption is heuristic.** eventSlug-only rule unvalidated (24h+tag clause
   dropped in v2 to avoid over-clustering; cross-event news-cycle correlations not captured).
7. **No causal inference.** Supported H3/FH2 (size ~ win/ROI) does not prove private information.
8. **Zero-fee venue effects.** ~89.6% maker fills are zero-fee. Taker fills (10.4%) apply.
   Maker/taker not exposed by Data API; FH1 defaults to assumed_maker_zero.

**New in v2:**

9. **Maker vs taker undetectable.** Data API does not expose maker/taker classification.
   Maker-rebate harvesting is systematically invisible to this protocol.
10. **True NBBO slippage uncomputable.** No L2 order book reconstruction. FH3 round-trip
    cost is an approximation from tape_midpoint BUY/SELL pairs, not a true NBBO comparison.
11. **Latency-sensitive arb undetectable.** CLOB timestamps are coarser than sub-second.
    Timing advantages within one timestamp window are invisible at any protocol version.
12. **3000-row activity cap truncates flipper analysis.** High-volume flippers' lifetime
    flip history is invisible. For these wallets, FH1–FH6 cover recent behavior only, or are
    suppressed entirely (FLIPPER_ANALYSIS_TRUNCATED). Cannot fix without Subgraph integration.
13. **Mode-router threshold is unjustified.** 0.30/0.70 thresholds are judgment calls. A
    wallet with flip_rate=0.31 gets both branches; at 0.29 it gets held-analyst only. This
    is arbitrary. Revisit when labeled wallet set >= 50.
14. **Activity cap can mis-route mode.** A high-volume flipper's recent activity shows fewer
    flips than lifetime. This underestimates flip_rate and can route the wallet to held-analyst
    branch when it is factually a flipper. See §4.3.
15. **Multi-grain hierarchy may produce contradictory verdicts at different levels.** Protocol
    reports both honestly. Operator must reconcile; protocol cannot. Example: wallet is a
    geopolitics generalist but leaf-specialist on iran only.
16. **CUSUM `h=25` has lower power for real regime detection.** The wide control limit was
    calibrated to achieve ≤5% false-alarm rate on heavy-tailed P&L (kurtosis ≈918). Trade-off:
    real regime changes must be large to signal. A wallet that gradually shifted strategy over
    ~100 positions will likely not trigger CUSUM at this threshold. When stationarity is
    important for a specific wallet, supplement CUSUM with visual inspection of cumulative
    PnL curve. See `cusum-calibration-pnl-heavy-tail-2026-04-25`.
17. **`tag_nav_category` is manually seeded and may be incomplete.** Tags not yet mapped
    default to `nav_category='other'` or are omitted from nav-grain inference. Verdicts
    for `nav_category='other'` are not meaningful. Check table coverage for a wallet's top tags
    before reporting nav-grain results as reliable.
18. **Ambiguous tag-to-nav mappings exist.** Tags `awards`, `world`, `tech` (overlaps nav `Tech`),
    and the `economy`/`finance` boundary have contested nav-category membership. Nav-grain
    verdicts derived from these tags must be treated as provisional until operator resolves
    the mapping in `tag_nav_category`.

---

## 14. Operationalization notes

```python
def analyze_wallet(wallet: str) -> ProtocolReport:
    ...
```

`ProtocolReport` dataclass: one field per output-template section. Sentinel fields:
`{"verdict": "underpowered", "reason": "..."}`.

**Key dependencies (extends v1):**
- `scripts/wallet_archetype_extractor.py` — Layer 1 archetype + execution profile
- `polymarket_api.fetch_closed_positions(wallet)` — ASC/TIMESTAMP, limit=50, max_pages=100
- `polymarket_api.fetch_open_positions(wallet)` — for `n_open_dead`
- `polymarket_api.fetch_activity(wallet)` — for flip reconstruction + mode router
- `db.wallet_tag_pnl_rows(conn, wallet)` — for H5/FH4 peer distribution
- `clob.polymarket.com/prices-history?fidelity=60` — H6 and FH6 reference prices
- `scipy.stats.spearmanr` — H3, FH2
- `statsmodels.stats.multitest.multipletests(method='fdr_bh')` — BH correction
- Tag taxonomy (when available) OR rarity-proxy logic from `tag-analytics.md`

**Mode router computation order:**
1. Fetch all four data sources (§4.1–§4.4)
2. Evaluate Stage 1 gates (§3.1) → REJECT or continue
3. Compute `net_pnl` (§4.5), tag attribution (§4.6), flip reconstruction (§4.7)
4. Compute `flip_rate`
5. Route (§5)
6. Run routed branch(es)

**CUSUM:** `ruptures` library or direct implementation. Use calibrated `h` from brain memory;
fallback `h=5` with flag.

**Bootstrap:** 10,000 iterations, `np.random.seed(42)`.

**BH-FDR:** Single correction per branch. H5/FH4 grain tests pooled under one branch-level
correction (not a nested correction).

**Session guard:** Store `protocol_version = "v2"` in report. V1 and V2 reports are not
directly comparable due to mode-router changes.

---

## 15. Open empirical work

These gaps are honest. Do not paper over them.

1. **CUSUM calibration — RESOLVED.** Brain memory `cusum-calibration-pnl-heavy-tail-2026-04-25`
   is populated. `h = 25.0` is locked. Recalibrate when B1 population exceeds ~50 wallets by
   re-running `scripts/calibrate_cusum_pnl.py`. No further action needed for current population.
2. **Mode-router threshold validation.** 0.30/0.70 are judgment calls. Labeled set of
   ≥ 50 wallets with known trading mode needed to fit empirically.
3. **Flip reconstruction validation.** FIFO matching against known wallet trade histories.
   Verify on 5 wallets with manually confirmed flip records.
4. **FH3 spread-estimate calibration.** tape_midpoint-based round-trip cost is coarse.
   Compare against execution profiles from wallets with known edge to check if FH3 is
   informative or noise.
5. **Tag taxonomy model — RESOLVED.** Web-researcher confirmed Polymarket multi-tags markets
   at both leaf and nav-category grain simultaneously; flat `tag_nav_category` lookup is the
   correct model (not DAG/tree). The earlier rarity-proxy placeholder is removed. See brain
   memory `research/polymarket-tag-taxonomy-2026-04-25`.
6. **`tag_nav_category` table bootstrap.** Manual mappings exist for ~50 high-frequency tags;
   remaining ~550 tags need backfill via `gamma-api.polymarket.com/events/slug/{event_slug}`
   `category` field, joined through `market_metadata.event_slug`. Until complete, lower-frequency
   tags fall into `nav_category='other'` and are suppressed from nav-grain inference.
7. **Ambiguous tag mapping resolution.** `awards`, `world`, `tech`, `economy` vs `finance`
   require operator decisions in `tag_nav_category`. Run `SELECT tag, COUNT(*) FROM market_tags
   WHERE tag IN ('awards','world','tech','economy','finance') GROUP BY tag` and map each.
8. **Population baseline expansion.** B1 peer population inherits discovery-pipeline bias.
   Full population expansion needed before within-tag percentile claims are reliable.
9. **Subgraph integration for lifetime /activity.** High-volume flippers are blocked on
   Subgraph access. Validate Subgraph endpoint; add as optional §4.3 data source.
10. **FH6 price-history fidelity.** Verify `prices-history?fidelity=60` delivers 1h bars and
    has sufficient coverage for short-hold scalper windows. Add explicit `n_price_history_bars
    >= n_flips * 0.80` underpowered gate to FH6 (per self-review §B I2 in the v2 review).
