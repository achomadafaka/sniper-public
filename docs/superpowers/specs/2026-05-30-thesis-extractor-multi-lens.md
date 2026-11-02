# Spec — Multi-lens thesis extractor (the "where did the edge come from" analyzer)

**Date:** 2026-05-30 · **Status:** pre-registration **rev 2 (methodology-review patched)** — review returned needs-patch (2 Critical + 5 Important); all applied here (see §Pre-registered card protocols + §Revision history). Review: Supabase `007339cd`.
**Supersedes intent of:** the selection-only CSE as the *whole* analyzer (it becomes one lens of many).
**Context:** the selection-edge hunt returned nulls (`finding/individual-wallet-copy-edge-not-supported` / the rerun `6681f0a2`). Those nulls answered ONE question — "did the wallet pick sides that beat the price paid?" A winning wallet can have edge somewhere else entirely.

## Doctrine (the reframe)

> **A winning wallet is not the edge. It is a *detector of where market machinery broke*.**
> The analyzer must not ask "can we copy this wallet?" It must ask:
> **"What market failure did this wallet repeatedly exploit — stale liquidity, slow attention, an internal mispricing, a resolution quirk, a regime, a niche?"**

The output is a **thesis profile**: a ranked set of cards, one per profit-source, each answering the same four questions:
**Where is the edge? · What evidence? · What controls (placebo/OOS/confound)? · What would prove it forward?**

## Architecture (cards are cheap hypotheses; forward-paper is the only truth)

Adding ~15 lenses × niche/time/size slices × thousands of wallets is a multiple-testing **bomb** — at α=0.05 you would "find gold" in thousands of wallets by chance. We do NOT promote on in-sample stats. Instead:

- **Cards = ranked HYPOTHESIS generator** — in-sample, many, each scored (effect · placebo · OOS · n), **FDR-*ranked*, not FDR-*gated***.
- **Forward-paper router = the assay** — limited slots; ranked candidates compete; out-of-sample resolution is survivorship-immune and is the family-wide control by construction.
- **Gold board = forward survivors ONLY.** Nothing is "real" until it survives paper.

This keeps the extractor honest: in-sample cards are leads; paper is truth.

**Paper admission rule (CRITICAL-1 — pre-registered, immutable).** "Paper is the family-wide
control" only holds if admission is a rule, not operator choice (operator-picking top-ranked cards
for paper is itself a test). So: **every card instance with (a) FDR q ≤ 0.20, (b) placebo pass, and
(c) n ≥ its card-type minimum is admitted to a paper slot in rank order, with NO operator re-ranking
or by-name exclusion after scores are observed.** The operator may cap total slots but not curate.
The admission log is auditable. The forward-paper router MUST NOT be built until this rule is wired.

**Cross-card correlation audit (IMPORTANT-5 — before any paper routing).** `price_capture`,
`timing`, and `discovery` are all driven by post-entry drift and will co-fire in momentum markets —
one factor masquerading as orthogonal edges. Before admitting a wallet's card set, compute pairwise
card-score correlation across a bootstrapped wallet sample; cards with r > 0.7 form a cluster, and
**at most one representative per cluster is admitted to paper.**

## Card catalog (15 thesis classes)

Data tiers: **HAVE** (current data), **PARTIAL** (computable proxy only; true version needs new data), **DEFER** (needs a new feed).

| Card | "What failure did it exploit?" | Core signal / lens | Data | Dominant trap → guard |
|---|---|---|---|---|
| `selection_card` | picked sides that beat the price | calibration edge vs price-at-T (the shipped CSE) | HAVE | survivorship → matched controls + placebo + OOS (built) |
| `price_capture_card` *(stale-liquidity)* | bought stale asks before the book moved | entry vs prevailing-trade-mid; favorable drift 5m/30m/1h | **PARTIAL** (no historic book → no spread/best-ask; drift only) | drift-conflation with selection → isolate cohorts where calibration is flat but drift is +; placebo on drift |
| `timing_card` *(dead-zone)* | traded when books were stale (night/weekend) | entry-hour × dead-hour repricing; next-session drift | HAVE (hour, drift) / PARTIAL (spread) | multiple-testing over buckets → FDR-rank; placebo |
| `exit_card` | sold/held better than hold-to-resolution | mark entry/peak/exit/resolution; **actual realized − hold-to-resolution PnL** = exit skill | HAVE | look-ahead on "peak" (descriptive only) → exit attribution uses only realized vs hold |
| `sizing_card` | sized the right trades, not won more often | **dollar-weighted calibration − equal-weighted**; size vs realized edge | HAVE | size-vs-outcome survivorship → require dollar-wtd > equal-wtd AND a real underlying edge first |
| `discovery_card` *(first-attention)* | entered before attention/volume arrived | creation→entry latency; first-N-trader status; subsequent volume/price move toward wallet | HAVE | reverse-causation (wallet moved price) → require later *other-trader* volume + drift, not own |
| `specialist_card` | edge only in a narrow niche | per-`eventSlug` / fine-taxonomy calibration breakdown | HAVE (taxonomy) | **multiple-testing across niches** (the big one) → FDR across niches + `min_n`; rank, never auto-promote |
| `basket_card` | legs weak, the position SET is smart | event-level net PnL +, leg-level calibration flat; hedge / neg-risk structure | HAVE (event grouping + PnL) | basket selection survivorship → event-level placebo |
| `relationship_pricing_card` | exploited inconsistency between related markets | A/B conditional markets disagree; bought the cheap side of a logical relationship | **PARTIAL** (need related-market linkage; within-event sibling coherence is the feasible start) | spurious "relationships" → restrict to same-event / explicit conditional links |
| `resolution_mechanics_card` | understood settlement quirks | trades near ambiguous/disputed rules; repeat rules-complex domains | **PARTIAL** (need UMA dispute / resolution-source data) | label scarcity → start descriptive (flag dispute-adjacent wins) |
| `liquidity_provider_card` | earned spread, not predicted outcomes | maker/taker inference; passive fills; low post-fill adverse selection; inventory turnover | **PARTIAL** (maker/taker from on-chain OrderFilled; adverse-sel via drift) | maker/taker mislabel → use on-chain side, not orderbook inference |
| `crowd_fade_card` | faded retail overreaction | enters against recent price/volume surge; mean-reverts; strongest after bursts | HAVE (price/vol surge) / DEFER (true flow) | regime-specific noise → FDR + restrict to noisy categories |
| `regime_card` | only works in a regime | edge in playoffs/elections/high-vol/thin-weekend; none outside | HAVE (calendar/vol/liquidity splits) | post-hoc regime carving → pre-register regime definitions |
| `consensus_candidate_card` *(cross-wallet)* | no single wallet, but agreement predicts | independent wallets converge on a side; price moves after | HAVE (needs multi-wallet pipeline) | correlated survivors → require independence + post-convergence drift |
| `information_reaction_gap_card` | reacted to public info faster | entry shortly after an external event, before reprice | **DEFER** (needs news/odds/event feed) | can't fake from wallet data → emit only as a GAP flag until the feed exists |
| `operational_coverage_card` | wins by monitoring more markets | fast fresh-market entry; low-latency; dead-hour coverage; no deep rule | HAVE | overlaps discovery/dead-zone → report as a meta-signal |

## New data unlocks (what each feed buys)

- **Live order-book capture (going forward):** the *true* `price_capture` + `liquidity_provider` cards (real spread, best-ask, maker/taker). Historic book is gone — capture forward.
- **News / odds / sports-line feed (event-timestamped):** `information_reaction_gap` and the strong form of `crowd_fade`.
- **On-chain `OrderFilled` maker/taker:** firms up `liquidity_provider` side inference (Codex's microstructure caveat: orderbook-inferred direction ≈ chance).

## Build prioritization

- **Wave 1 — build now (feasible + highest non-obvious upside):** `discovery_card` (first-attention), `price_capture_card` (stale-liquidity, drift form), `exit_card`, `sizing_card`, `timing_card`, `specialist_card`. *(Codex's top picks: stale-liquidity + first-attention + relationship-pricing; the first two are fully actionable now, relationship-pricing starts as within-event coherence.)*
- **Wave 2 — V2 (feasible):** `basket_card`, `regime_card`, `relationship_pricing_card`, `crowd_fade_card` (price form), `operational_coverage_card`, `consensus_candidate_card` (needs the multi-wallet pipeline).
- **Wave 3 — needs new feeds:** `information_reaction_gap_card` (news), true `liquidity_provider`/`price_capture` (live book), `resolution_mechanics_card` (dispute data).
- **Cross-cutting:** `rule_learner` upgrade to **2–4 condition conjunctive rules** (overfitting is the trap → hard OOS + complexity penalty); the **forward-paper router** (the gate that makes any card "real").

## Per-card output contract

Every card emits: `{card_id, edge_locus, effect, evidence{n, by-slice}, controls{placebo, oos, confound}, data_tier, forward_test_design, score}`. The wallet's thesis profile = the ranked cards + one honest line: *"where, if anywhere, this wallet has a forward-testable edge."*

## Pre-registered card protocols (rev 2 — methodology-review patched)

Each Wave-1 card's exact statistic + null + guard (these supersede the table's prose where they differ):

- **`exit_card` (IMPORTANT-3).** Statistic = actual realized PnL − simulated hold-to-resolution PnL on the same closed positions, **stratified by entry-price band** (the 5 calibration bands — absorbs entry quality, the confounder). Require ≥15 early-exit positions per stratum. **Null:** shuffle exit timestamps across positions of the same entry-price band, recompute the realized-vs-hold gap. Only the within-stratum residual is exit skill; the unconditional version is `DESCRIPTIVE ONLY`. *(SAFE to build.)*
- **`sizing_card` (IMPORTANT-2).** Require ALL of: (1) dollar-weighted calibration > equal-weighted; (2) the underlying calibration edge passes its own test; (3) **within-wallet size permutation p < 0.10** — shuffle her actual sizes across her actual resolved outcomes (entry prices fixed), ≥500 shuffles, recompute dollar-weighted calibration. Also filter out high-certainty entries (≥0.80) so "big on near-certainties" isn't scored as skill. Without (3) → `EXPLORATORY`. *(SAFE to build with the permutation null.)*
- **`price_capture_card` (IMPORTANT-1 — PATCH before build).** Drift only on markets where ≥1 **independent (non-wallet)** trade moved same-direction within 30m; **exclude** markets where her trade was the sole price-setter in ±30m. **Null:** shuffle her entry timestamps within the market's fixed price path; if shuffled entries show the same drift, the market was trending regardless of her. Flat calibration is NOT sufficient to infer non-selection. Require n ≥ 20 over ≥3 categories. Drift-proxy only (no book) → `price_capture_proxy_card`, labeled PROXY.
- **`discovery_card` (IMPORTANT-4 + MINOR-1 — PATCH before build).** Exclude markets where her trade ≥20% of ±1h volume (self-referential drift); require ≥3 **independent** wallets trading her direction within 2h (attention arrival); **null:** shuffle her entry timestamps (outcomes fixed), recompute fraction where first-N-trader AND drifted her way, p < 0.10. Directional-other-volume → data tier **PARTIAL** until confirmed from the Becker fill schema.
- **`specialist_card` (CRITICAL-2 — PATCH before build).** Pre-register **ONE** niche axis per run ∈ {eventSlug, category, template, sport} — no cross-axis shopping. FDR family = all niches under that axis for the wallet. **min_n ≥ 20**; exclude niches with < 3 resolved markets before FDR. Control for niches disproportionately populated by retail sellers (the prior 7–16pp sell-side artifact).
- **`timing_card` (MINOR — data tier).** Next-session drift is **PARTIAL** for markets near the Becker freeze (post-freeze price path unavailable); scope to markets with ≥1 day of post-entry price within data.
- **`basket_card` (two-leg dead-end).** Resolve at the **EVENT level (net PnL of the full position set)**, never leg-by-leg in one statistical test — grouping correlated/hedged legs in a single test inflates confidence (the cohort-study two-leg failure).
- **`conjunctive_rules` (MINOR-2).** OOS = the **last chronological 20%** of the wallet's history, split **pre-registered before any rule search**; max conjunction pre-registered; complexity penalty = each added condition **doubles** the required OOS n; minimum support floor.

**Build safety (from review):** `selection` (shipped), `sizing` (+permutation), `exit` (+entry-band stratification) → **build now**. `price_capture`, `discovery`, `specialist`, `timing` → spec now patched above; build after. **Forward-paper router → blocked until the admission rule is wired** (it now is, above).

## Discipline

- Pre-register each card's measure + guard + forward-test BEFORE coding it (this spec is that registration; amend per card).
- `research-methodologist` reviews this spec before Wave 1 build (the pattern that caught 3 Criticals on the cohort study).
- Checkpoint counts before any batch run (the pattern that killed the cohort study cheaply).
- Do NOT chase omniscience — finish Wave 1's orthogonal lenses, wire the forward router, then run volume on a non-survivorship wallet feed.

## Build sequence

1. `research-methodologist` review of THIS spec (per-card traps, especially specialist multiple-testing + drift-conflation + sizing survivorship).
2. Wave 1 cards as `detection.py`-style pure functions over the existing decision matrix + her fills/positions/price-series; each with placebo + OOS + score.
3. Thesis-profile assembler (runner emits the ranked card set).
4. Forward-paper router (promote ranked candidates into paper slots).
5. Batch runner + non-survivorship candidate feed (the conveyor) — separate spec.

## Revision history

- **rev 4 (2026-05-30):** Codex producer review — 2 Critical + 2 Important fixed.
  **sizing_card (C1):** now enforces spec req (2) — the underlying calibration edge must pass
  its OWN test (Poisson-binomial null on win-rate vs implied prob) before `supported`; sizing
  skill on a negative/zero base edge → `exploratory`, never supported.
  **discovery_card (C2):** `self_volume_frac` is now REQUIRED in `_has_attention` — missing it
  → BLOCKED, not defaulted to 0.0 (the old default silently bypassed the self-volume exclusion).
  **exit_card (I3+I4):** early exits taken ONLY from an explicit `is_early_exit` attribution flag
  (from real SELL evidence), never inferred from realized≠hold; and the sign-flip null is relabelled
  **PROXY** — the pre-registered within-band exit-timestamp shuffle needs per-position exit-time
  price paths we don't have, so the card is `descriptive_only` with `placebo_passed=None` and is
  NOT promotable by admission (which requires placebo_passed is True) until a real exit-time feed
  exists. 30/30 thesis-card tests green.
- **rev 3 (2026-05-30):** Wave-1b build (`timing_card`, `discovery_card`) + verified data tiers
  against the real feed. **`timing_card` = built, data tier PARTIAL but real** — next-session drift is
  sampled from the per-market `price_index` (`[unix_ts, price]` series in the CSE event context);
  truncates near the Feb Becker freeze (those positions arrive `next_session_price=None` and are
  dropped). **`discovery_card` = built but data tier BLOCKED on current data** — the Becker trades
  parquet (`build_cse_pit_index.py:10`) carries `maker/taker asset_id + amount + block_number` and
  has **no per-fill timestamp and no trader identity**, so `independent_followers_2h` and
  `self_volume_frac` cannot be derived. The producer runs the real statistic when a future fill feed
  supplies attention fields, but self-reports `data_tier=BLOCKED` + `underpowered` rather than
  fabricating a first-attention signal. The literal entry-timestamp-shuffle null also needs per-market
  trade sequences (absent), so the first-N-label permutation is the documented position-level analog.
  Cards return ranked-hypothesis objects only; no verdict mutation, no paper routing. 27/27 thesis-card
  tests green.
- **rev 2 (2026-05-30):** applied `research-methodologist` review (Supabase `007339cd`, needs-patch).
  CRITICAL-1 pre-registered paper-admission rule (q≤0.20 + placebo + n, rank-order, no operator
  curation; router blocked until wired); CRITICAL-2 specialist single pre-registered niche axis +
  min_n≥20 + ≥3-resolved-markets; IMPORTANT-1..5 per-card nulls/guards (price_capture independent-
  trade + timestamp-shuffle, sizing within-wallet permutation, exit entry-band stratification,
  discovery self-volume exclusion + independent-wallet attention, cross-card correlation audit);
  basket event-level only (two-leg dead-end); conjunctive OOS protocol; data-tier corrections
  (discovery directional-volume, timing freeze-boundary → PARTIAL). Build-safe now: selection,
  sizing, exit.
- rev 1 (2026-05-30): initial design (my architecture + Codex's 15-class catalog).
