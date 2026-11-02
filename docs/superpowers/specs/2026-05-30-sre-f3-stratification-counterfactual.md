# SRE-F3/F4 — Stratification, counterfactual & engagement features

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Purpose:** turn the entity layers (F1) + point-in-time universe (F2) into the **labelled matrix** every detection test runs on: one row per (market considered at a decision time), columns = features, label = picked(1)/skipped(0), partitioned into logical groups.

## Stratification engine (form the groups)
A group is a stack of axes (master §2). Generate groups by:
- **single-axis** (every value of each axis),
- **pairwise & triple stacks** (category×outcome_role, category×price_band×ttr_bucket, event_family×favorite_rank, creation_cohort×category, …),
- **data-driven** (auto-bin continuous axes by her pick density so groups are balanced),
- **her-defined** (the grains she actually concentrates in, from F1 campaigns).

Keep a group only if it has ≥ `MIN_PICKED` and ≥ `MIN_SKIPPED` members (else no contrast). Emit `group_support` and a `group_homogeneity` score (how tight the group is on non-axis features) — tighter groups give sharper tests.

Combinatorial guard: rank groups by (support × her-concentration × homogeneity); cap at top-K per axis depth so the test battery doesn't explode. Log what was dropped (no silent truncation).

## Counterfactual assembly (per engagement)
For each engagement at decision time T and group G, pull skipped members from F2 at scopes C0→C7 (master §3). Default to the **tightest scope with adequate support** (C7 nearest-k if possible, else C4 within-event, else C3). Record which scope was used per row.

## The matrix (`sre_decision_matrix`)
One row per (decision_T, candidate_market, group_id, cf_scope), columns:
- **label:** `picked ∈ {0,1}`, `picked_engagement_id` (if 1).
- **intrinsic:** category, market_type, outcome_role, favorite_rank, event_family, template, entry_price_band.
- **price-state @T:** price, extremity |p−0.5|, price_staleness, distance_from_market_min/max.
- **price-path @T:** drift_1h/6h/24h, velocity, accel, dip/spike/reversal/trend flag, pre-vol.
- **time @T:** market_age, time_to_resolution, frac_life_elapsed, latency_from_creation (for picks), entry_hour, entry_dow.
- **crowd @T:** vol_window, n_trades_window, n_wallets_in.
- **(picks only) execution:** n_fills, n_clips, ladder_shape, accumulation_span (kept but flagged execution-not-selection).
- **(picks only) sizing:** entry_size, total_size, size_vs_own_median.
- **(post-hoc label) outcome:** resolved, roi (used ONLY by the edge test T2, never as a selection feature).

## Additional feature families to compute (imagination — beyond the obvious)
- **relative-price features:** her entry price vs the group's median price at T (is she the cheap one?); vs the event's favorite price; vs the same market's 24h-VWAP.
- **rank features:** price-rank and volume-rank of the picked market *within its group* at T.
- **freshness/contrarian:** was the market trending toward or away from her side when she entered (fade vs follow).
- **scarcity:** how many group members existed at T (selective when many available, forced when few).
- **repeat/loyalty:** has she traded this event_family/league/template before (specialist vs explorer).
- **co-decision:** how many other markets she entered in the same session/basket (focused vs spray).
- **resolution-proximity:** entries bunched just before resolution (information) vs early (structural).

## Outputs
`sre_decision_matrix` (the labelled table) + `sre_group_index` (group → support/homogeneity/cf_scope) — consumed by F4 detection battery and F5 rule learner.

## Acceptance
1. For an engagement, the skip-set at C2 ⊇ C3 ⊇ C4 (monotone tightening) and each carries its fidelity tier.
2. A pure within-event row (C4) lists the event's other outcomes she didn't take.
3. Dropped-group log present whenever the top-K cap binds.
