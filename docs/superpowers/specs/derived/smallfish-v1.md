---
memory_name: spec/derived/smallfish-v1
memory_type: reference
memory_description: Derived strategy spec from wallet 0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf (SmallFishInWhaleStomach). Type: convergence_copy_v1 (nearest fit; see § Strategy type). Primary edge: underdog-selection alpha in geopolitics + crypto public-sale commitment markets, retained ~66% through +15 min copy lag.
memory_tags: [strategy-spec, derived, geopolitics, crypto, hybrid-archetype, needs-human-review]
---

# Derived Strategy Spec — SmallFishInWhaleStomach v1

**Status:** SCAFFOLD — operator-completion required before any paper or live use.
**Spec author:** strategy-derivation agent (2026-05-27).
**Source wallet:** `0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf` ("SmallFishInWhaleStomach").
**Dossier:** `knowledge/wallets/0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf.md`
**Full protocol report:** `.claude/jobs/414082e3/overnight/reports/0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf.md`
**Survivorship study:** `~/Desktop/polymarket-research/studies/survivorship_smallfish.md`
**Brain entry:** `finding/survivorship-cohort-smallfish-2026-05-27` (Supabase `memories`)
**Curation source:** `finding-smallfish-copyable-edge.md` (watchlist-curated; operator marked "derive")

---

## 1. Source wallet and dossier reference

**Label:** SmallFishInWhaleStomach  
**Protocol version analyzed:** v2 (2026-05-23T12:47:55Z)  
**Activity window:** Oct 2025–Apr 2026 (~6 months)  
**Classification (legacy scorer):** `speculator_bot` / `execution_bot` — treat as stale; protocol report supersedes.  
**Gate result:** PASS (H1 supported, H3 supported; H2 inconclusive; H4/H6 underpowered)

This wallet is the **first real wallet** where the copy-latency surface activated as `copyable` on live data (not a dormant fixture). That is the primary reason for this spec.

---

## 2. Archetype envelope (Layer 1–3 summary)

### Layer 1 — Classification

| Field | Value | Source |
|---|---|---|
| trading_mode | `mixed_mode` | Protocol report |
| archetype L1 | `mixed_unresolved` | Protocol report |
| archetype L2 | `directional_unknown` | Protocol report |
| Becker archetype | `hybrid_trader_v1` | Survivorship study |
| flip_rate | 0.4588 | Protocol report |
| n_closed_positions | 170 | Protocol report |
| n_held_to_resolution | 88 | Protocol report |

### Layer 2 — Execution fingerprint (Becker lifetime)

All numbers from `survivorship_smallfish.md` — the Becker 404M-trade dataset, lifetime fingerprint.

| Metric | Value |
|---|---|
| n_total trades | 2,091 |
| pct_buys | 64.3% |
| pct_buys ≤ $0.55 (underdog band) | **40.1%** |
| pct_buys ≥ $0.95 (near-certainty/scraping band) | **34.3%** |
| pct_buys $0.40–$0.70 (mid-range) | 20.1% |
| avg buy price | $0.664 |
| avg buy USDC (mean; heavy-tailed) | $1,622 |
| median buy USDC | $52 |
| total deployed (Becker) | $2.18M |

Characterization: **true hybrid** — nearly equal weight in the underdog and near-certainty bands. Not a pure underdog accumulator; not a scraper. Neither band dominates.

### Layer 3 — Edge hypotheses

Three hypotheses from the protocol report and null-baseline surface. Confidence stated as-is from the data; do not promote.

**H-A: Underdog side-selection alpha (primary candidate)**
- `edge_vs_favorite = +12.4 percentage points` on the 83-position matched slice.
- Always-favorite baseline: −6.3% EW ROI. Always-underdog: +18.6%. Wallet: +6.1%.
- Wallet picks upsets selectively — not blindly buying underdogs, beating that blind baseline by 12.4pp.
- Source: null-baseline surface (49% coverage, binary-priced resolved positions).
- Caveat: 49% coverage is LOW; the other 51% of positions are not captured by this baseline.

**H-B: Timing / hold-to-resolution patience (secondary)**
- Held-to-resolution WR: **89.8%** (79 win / 9 loss, n=88, +$45,025).
- Pre-resolution exit WR: 75.6% (59 win / 19 loss, n=78, +$15,877).
- H-held analyst branch: supported (p~0, CI [62, 204]).
- H3 (exit-price vs. entry-price correlation): supported (rho=0.21, p=0.048 raw).
- Interpretation: patience is rewarded. Pre-resolution exits still profit but capture less.

**H-C: Category concentration (geopolitics + crypto public-sale commitment markets)**
- geopolitics: n=27, PnL=$22,701 (+12.2% share)
- politics: n=34, PnL=$19,486 (+10.5% share)
- finance: n=25, PnL=$3,233 (+1.7% share)
- crypto: n=23, PnL=$3,478 (+1.9% share)
- Biggest individual wins: MegaETH commitment (+$12,212), ZachXBT views (+$8,754), Israel-Hezbollah ceasefire (+$4,389), Aztec ETH commitment (+$10,055), Hurupay commitment (+$13,511).
- TAG_LOOKUP_UNAVAILABLE for this run — category labels are approximate REST-inferred, not leaf-tag-confirmed.

### Layer 4 — Outcome attribution tension (sizing anomaly)

The protocol labels `primary_edge_type: size_conviction` but the sizing-outcome surface contradicts this:

| Entry-size quartile | mean_entry | WR | mean_ROI |
|---|---|---|---|
| Q1 (smallest) | $35 | 84.6% | **+6.3%** |
| Q2 | $379 | 79.5% | **+8.0%** |
| Q3 | $1,683 | 79.5% | −1.9% |
| Q4 (largest) | $39,933 | 97.5% | **−0.5%** |

Global Spearman rho = **−0.19** (p=0.023, n=157). Category-weighted rho = −0.13 (honest headline).

**The actual edge is in small-to-mid entries (Q1–Q2), not marquee bets.** Q4 wins almost every time but earns near-zero ROI — big bets appear to be near-certainty scraping (the 34% ≥$0.95 band). The `size_conviction` label is misleading for replication purposes.

Late conviction ratio = 3.67× ($4.48M added after initial entry) — heavy pyramid behavior on winners.

---

## 3. Copy-latency profile (why this wallet qualified for derivation)

Source: protocol report copy-latency surface (`data_state: ok`, 74 eligible positions).

| Lag | n | Mean drift | Entry ROI | Copier ROI | Decay | Edge retained |
|---|---|---|---|---|---|---|
| +5 min | 74 | 1.53% | 33.8% | 27.7% | 6.2% | **81.7%** |
| +15 min | 74 | 2.29% | 33.8% | 22.2% | 11.6% | **65.6%** |
| +60 min | 74 | 4.37% | 33.8% | 14.5% | 19.3% | 42.9% |

At +15 minutes a copier retains ~66% of the original edge. At +60 minutes it is below half. **The execution window is narrow: ≤15 min is the operative target; ≥60 min is likely unprofitable after costs.**

Proxy execution cost estimate: 60.8 bps weighted (liquidity surface, `proxy_only` fidelity — treat as rough floor).

---

## 4. Risk and capital profile

Source: capital & risk surface (`data_state: proxy_only`).

| Metric | Value | Notes |
|---|---|---|
| Peak concurrent deployed | $463,734 | Capital base — peak USDC at once |
| Cumulative deployed | $6,239,095 | Over ~6 months |
| Recycle factor | 13.5× | Active capital recycling |
| Max drawdown | $25,694 (5.5% of peak) | Low relative to scale |
| Return on peak deployed | 13.1% | Over 180.8 days |
| DW ROI | +0.77% [−0.06%, +1.69%] CI | Dollar-weighted — the conservative headline |
| EW ROI | +2.04% | Equal-weighted per position |
| DW/EW divergence | 2.7× | Big positions underperformed average position |
| PnL concentration | moderate (gini=0.76, top5=41%) | Not a single-market story |

DW/EW divergence flag: the largest positions are disproportionately near-certainty buys (low ROI), dragging the dollar-weighted return below the per-position average. Any copy strategy sized proportionally to the wallet's notional will over-weight exactly these low-ROI trades.

---

## 5. Survivorship context (mandatory — do not skip)

Source: `survivorship_smallfish.md` + `finding/survivorship-cohort-smallfish-2026-05-27`.

**Cohort:** `hybrid_trader_v1`, n=3,074 wallets (Becker, same fingerprint definition).

| Cohort bucket | n | % |
|---|---|---|
| large winner (≥$50K) | 341 | 11.10% |
| small winner ($5K–$50K) | 796 | 25.90% |
| survivor ($0–$5K) | 537 | 17.47% |
| loser (−$10K–$0) | 751 | 24.43% |
| **busted (<−$10K)** | **648** | **21.08%** |

Cohort mean PnL: +$15,966. Median: +$779. 54.5% positive. Aggregate ROI: +2.2%.

SmallFish placement: **$86,750 → rank #236 of 3,074 (top 7.64%)**.

**Strategy-level read:** The hybrid_trader archetype is high-variance net-positive. The strategy has real aggregate EV (+2.2%) but the busted tail (21%) is as large as the large-winner tail (11%). This is not a structurally safe strategy to copy blindly — it requires enough independent trials to track the distribution rather than sampling into the bust tail.

**Do not extrapolate from SmallFish's PnL alone.** She is a top-8% outcome within a class where 21% go bust. Her result is consistent with skill AND consistent with lucky draws from a high-variance distribution. The distinction is not resolvable from 170 closed positions.

---

## 6. Tag × PnL breakdown

**STATUS: PARTIAL — TAG_LOOKUP_UNAVAILABLE for this run.**

The category labels below are REST-inferred approximations, not leaf-tag-confirmed. They are directionally useful but not suitable as precise filter conditions.

| Category (approx) | n | PnL | PnL share |
|---|---|---|---|
| politics | 34 | +$19,486 | +10.5% |
| geopolitics | 27 | +$22,701 | +12.2% |
| finance | 25 | +$3,233 | +1.7% |
| crypto | 23 | +$3,478 | +1.9% |
| other | 20 | +$16,502 | +8.9% |

Notable sub-themes (inferred from market titles, not tag lookup):
- Crypto public-sale commitment markets (Hurupay, MegaETH, Gensyn, Aztec, Ranger) — mixed results, some large wins AND large losses in this cluster.
- Geopolitical binary events (Iran strikes, Israel-Hezbollah ceasefire, Iranian regime fall).
- US political markets (ZachXBT views, other).
- UFC main-card outcomes (2 positions, 100% WR, +$4,210 — small sample).

**HUMAN: A re-run with market_tags table populated is required to get confirmed leaf-tag-level PnL. Until then, treat the above as indicative only and do not hard-code tag filters from this table.**

---

## 7. Proposed strategy type

**Nearest existing plugin template:** `convergence_copy_v1`

Rationale: the copyable edge here is not term-structure (no yield curve), not political-NO-value (not systematically fading YES), not a pure insider-copy. It is closest to convergence-copy: observe wallet entry, copy within a latency window, hold to resolution. The twist is underdog-side filtering.

**Blocker flag:** `convergence_copy_v1` as currently implemented does not have an underdog-filter parameter (side-selection filter by entry price band). Either:
- (a) extend `convergence_copy_v1` with a `max_entry_price` filter gate (blocks near-certainty copies), or
- (b) propose a new type `underdog_copy_v1`.

Option (a) is lower friction. Option (b) is more honest about the distinct mechanism.

**HUMAN: Choose (a) or (b) before implementation is scoped.**

---

## 8. Entry conditions (operator-supplied)

**HUMAN: fill entry trigger**

Template placeholders (do not implement as-is):

```
trigger: wallet 0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf opens a new position
filter_1: entry_price <= [HUMAN: threshold — suggested ≤0.65 based on Q1/Q2 quartile boundary, but verify]
filter_2: category in [HUMAN: confirm tag list once market_tags is populated — suggested geopolitics, politics, crypto commitment markets]
filter_3: [HUMAN: minimum market volume threshold to ensure copyability — proxy cost is 60.8 bps; set floor accordingly]
latency_gate: position detected within [HUMAN: specify max_seconds — data supports ≤900s (~15 min) to retain ~66% edge]
```

Open question: the wallet also takes near-certainty positions ($0.95+). Should those be silently filtered or trigger a separate lower-size copy rule? The data says low ROI on big entries but near-100% WR — a human judgment call on whether to include at a fraction of notional.

---

## 9. Exit conditions (operator-supplied)

**HUMAN: fill exit rule**

Template placeholders:

```
primary_exit: hold_to_resolution
  rationale: wallet WR held=89.8% vs. exit=75.6%; patience is rewarded per H2/H3
pre_resolution_exit_rule: [HUMAN: specify — e.g. exit if price drifts >X% against entry within Y days, or never exit early]
stop_loss: [HUMAN: specify — max drawdown was 5.5% of peak at wallet level, but per-position stop is a separate call]
```

Note on late conviction: the wallet adds 3.67× its initial entry notional after open. A copy strategy cannot replicate pyramiding in real-time without a monitoring loop. Either ignore pyramiding (enter once, hold flat) or define a secondary add rule separately.

---

## 10. Risk caps (proposed — operator must confirm)

These are proposed upper bounds. All numbers derived from wallet data; do not exceed without additional evidence.

| Parameter | Proposed cap | Rationale |
|---|---|---|
| Max copy size per position | ≤ wallet's Q2 median entry = ~$379 | Larger entries showed lower ROI; don't size to wallet's Q4 |
| Max concurrent open positions | [HUMAN: specify] | Wallet ran up to ~26 concurrent; copier capacity will differ |
| Max market exposure (single market) | [HUMAN: specify] | DW ROI concentration risk — top-5 positions = 41% of PnL |
| Max entry price (underdog filter) | ≤$0.65 (suggested) | Q1/Q2 boundary; captures the positive-ROI quartiles |
| Max latency at entry | 900 seconds | Beyond +15 min: edge_retained drops to 65.6% |
| Category exposure | [HUMAN: specify per-category cap] | Tag lookup incomplete; revisit once market_tags is available |

**Do not copy positions sized at the wallet's Q4 average ($39,933).** These are the near-certainty scraping bets. They win often but return −0.5% ROI and drag DW return below EW.

---

## 11. Validation plan

Per agent rules: paper-only, N ≥ 100 trades before any real money.

1. **Paper phase 1 (observation):** Log every wallet entry in real-time via the copy-latency surface. Do not paper-trade. Measure: what fraction of entries fall within the proposed entry filter (price ≤$0.65, latency ≤15 min)?
2. **Paper phase 2 (simulated copy):** Apply entry/exit rules in paper mode. Track per-position ROI, WR, and realized latency. Compare copier ROI to the wallet's own entry ROI as ground truth.
3. **Gate to live:** Only after N ≥ 100 paper trades with statistically credible positive ROI (CI lower bound > 0) and observed latency consistently ≤ 15 min.
4. **Independence check:** The protocol report flagged `fraction_in_3plus_event_clusters = 33%` — many positions are in correlated event clusters (e.g. multiple MegaETH commitment markets). Paper results must be evaluated for event-cluster correlation, not just raw N.

---

## 12. Known replication risks

In rough priority order.

**R1 — Survivorship / archetype variance (highest risk)**
21% of `hybrid_trader_v1` cohort wallets went bust (<−$10K). SmallFish is top 7.64%. Forward performance for a copy strategy depends on whether the wallet continues to perform at tail-level or reverts toward cohort mean. There is no way to distinguish skill from luck at n=170. See `survivorship_smallfish.md` for full cohort data.

**R2 — Tag-grain unknown (execution blocker)**
`TAG_LOOKUP_UNAVAILABLE` in the protocol run. Category PnL breakdown is approximate. Until market_tags is populated and a re-run confirms leaf tags, any category-filter entry condition is a guess. This is an active data gap, not a design choice.

**R3 — Latency window is narrow**
Edge retained at +5 min: 81.7%. At +60 min: 42.9%. The copy infrastructure must reliably detect entries within 5–15 minutes. Any monitoring lag, downtime, or alert delay collapses the edge. Proxy execution cost (60.8 bps) is additive on top of drift.

**R4 — DW/EW divergence means size-proportional copying underperforms**
If copy size is proportional to wallet notional, the copier will over-weight Q4 big bets (−0.5% ROI) and under-weight Q1 small bets (+6.3% ROI). A flat-size or capped-size entry rule is strictly better than notional-proportional. See sizing anomaly in § 2 Layer 4.

**R5 — Late conviction not replicable**
The wallet's late_conviction_ratio is 3.67×: $4.48M added after initial entries. A real-time copier cannot replicate this pyramid behavior without a continuous position-monitoring loop. If the initial entries are the alpha (not the add-ons), this risk is small. If the pyramid is the alpha, a one-shot copy will underperform.

**R6 — Event cluster concentration**
33% of positions cluster in 3+ same-event groups (e.g. multiple MegaETH tranches). These positions are not independent. A paper N=100 may correspond to far fewer independent event bets. Statistical claims about edge must account for this.

**R7 — Archetype misclassification (moderate risk)**
The Becker fingerprint bins SmallFish as `hybrid_trader_v1` based on price-band percentages. If her buying behavior shifts (e.g. she stops taking underdog positions), the archetype definition no longer applies and the entry filter becomes invalid. Monitor buy-price distribution quarterly.

**R8 — Capacity**
Wallet's median position is $52 (Becker) to ~$379 (Q2 protocol). The strategy is small-cap by design. If copy size approaches market-moving scale in thin geopolitics/commitment markets, self-adversarial slippage becomes a real cost. Commitment markets (MegaETH, Gensyn, Aztec) can be illiquid outside of announcement windows.

---

## 13. Open questions (operator must resolve before paper)

1. **Entry price ceiling:** The data supports ≤$0.65 as a filter (Q1/Q2 boundary), but this is inferred from quartile boundaries, not a calibrated threshold. Should this be tuned on a hold-out set first?

2. **Near-certainty copy rule:** Q4 entries ($0.95+) have 97.5% WR but −0.5% ROI. Include at reduced size, exclude entirely, or treat as a separate lower-priority rule?

3. **Strategy type decision:** `convergence_copy_v1` extension (add max_entry_price param) vs. new `underdog_copy_v1` plugin. See § 7 blocker flag.

4. **Forward edge under survivorship correction:** Cohort variance is high (11% large winners + 21% busted). How many paper trades does the operator need to see before having reasonable confidence the wallet is operating in the winner tail vs. regressing to cohort mean? See `survivorship_smallfish.md` and `docs/superpowers/specs/2026-05-27-survivorship-surface-design.md` for caveat language.

5. **Tag confirmation re-run:** When will market_tags be populated? Until then, the category filter (§ 8 filter_2) cannot be finalized. Is the spec blocked on this, or should paper phase 1 run without a category filter?

6. **Pyramiding:** Ignore the wallet's 3.67× late-add behavior (enter once, hold flat) or build a secondary add rule? The data does not tell us whether adds are the alpha or just capital rotation.

7. **Event-cluster correlation correction:** Paper validation methodology needs a plan for reporting results that accounts for correlated same-event positions. Raw N=100 may overstate statistical confidence. Define the cluster-corrected effective N target.

8. **UFC / sports-outcome positions:** 2 positions, 100% WR, +$4,210. Too small to include in entry filters, but worth watching in paper phase 1 to see if this is a recurring pattern.

---

*This scaffold was machine-generated from wallet protocol report v2 + survivorship study (2026-05-27). All numbers trace directly to their cited sources. No numbers were transformed or inferred beyond the source fields. Entry conditions, exit rules, sizing, and the strategy-type decision are unresolved and labeled accordingly. This document is NOT a trade recommendation.*
