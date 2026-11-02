# Spec — Market-cohort calibration study (pre-registered, rev 2)

> **⛔ PRIMARY-T RESULT: KILL / not measurable (2026-05-30).** Stopped at the extraction
> checkpoint — active-fidelity coverage 4.4% at T=created_at+24h + price-band collapse under
> one-leg-per-market. No battery run, no promoted cohorts. See
> `docs/research/2026-05-30-market-cohort-calibration-primary-T-KILL.md`. Near-resolution T is a
> separate hypothesis class (deferred), NOT an amendment to this spec.

**Date:** 2026-05-30 · **Status:** pre-registration, **rev 2 (methodology-review patched)** — design locked before any result is trusted.
**Methodology review:** `research-methodologist` 2026-05-30 returned **needs-patch** (3 Critical + 4 Important + 2 Minor). This rev applies all fixes; see §Revision history. Build may proceed on this rev.
**Context:** `docs/research/2026-05-30-individual-wallet-copy-edge-not-supported.md` redirected effort off individual-wallet copying. This studies whether the edge lives in the **market**, not the wallet.

## Goal

Test whether systematic, exploitable mispricing exists in any **market cohort** defined by *observable, pre-trade* features — independent of any wallet. Measure: **calibration edge = realized win-rate − price-at-T**, point-in-time, multiple-testing corrected.

## Hypotheses (pre-registered)

- **H0 (efficient):** within every cohort, realized win-rate == mean price-at-T; net-of-cost calibration edge = 0.
- **H1 (exploitable cohort):** ≥1 cohort has a calibration edge that clears **ALL** of: cost-adjusted threshold, FDR-significance across the family, OOS time-split confirmation, and the market-level placebo.

We expect H0 to mostly hold. A surviving cohort is the prize; a clean null is a publishable kill.

## Unit + label  *(Critical-1 fix: ONE leg per market)*

- **Unit:** **one leg per binary market** — include the leg whose `price_at_T ≤ 0.5` (the underdog / lower-probability side), ties broken by taking the YES token. **The same market never appears twice in any single cohort's calibration test.** This eliminates the within-market anti-correlation that would otherwise inflate n, deflate variance, and collapse pooled calibration to a mechanical zero.
- **Price-at-T:** the chosen leg's mid at T (point-in-time, leakage-clean) via `pit_market_context.price_at`. **Inclusion requires `price_fidelity == 'active'`** (≥5 trades in ±1h of T); `sparse`/`pre_trade` legs are excluded (Important-4).
- **Label:** did that leg resolve to win (per-token `winning_index`), strictly post-T.

## Observation point T  *(Critical-3 fix: leakage guards)*

- **Primary T = market_created_at + 24h.** Pre-register this single primary; secondaries are descriptive only.
- **Hard exclusions for the primary family:**
  - markets where `end_date − created_at < 48h` (T risks being post/near-resolution);
  - markets where `end_date ≤ T + 24h` (need ≥24h of life remaining at T — blocks near-resolution contamination);
  - legs with `price_fidelity != 'active'` at T.
- **Secondary T (descriptive, NOT in the test family):** `end_date − 24h`, `end_date − 72h`. The `ttr_band <24h` stratum is valid ONLY under a secondary T, never the primary.

## Cohort definition (the strata)

`category × market_type × price_band × ttr_band × volume_tier`.
- price_band: [0,0.1,0.2,0.35,0.5,0.65,0.8,0.9,0.97,1.0] (note: with one-leg-per-market the underdog leg sits ≤0.5, so high bands populate from markets whose YES≤0.5 i.e. NO is the favorite — report band populations).
- ttr_band: [<24h (secondary-T only), 1–7d, 7–30d, 30–90d, >90d].
- volume_tier: terciles of per-market volume. **Tercile thresholds are pre-computed on the full resolved universe BEFORE any cohort filtering and fixed as constants in the run script** (Minor-2). Not recomputed per cohort.
- Drop cohorts below `min_n = 200` legs.

## Data  *(Minor-1 fix + cross-check)*

- **Universe:** Becker archive resolved markets + outcome_prices + block→epoch price index, via `scripts/build_cse_pit_index.py` (full build). Becker's Feb-2026 freeze is fine — we want *resolved* markets (resolution is the label).
- **Filter `negativeRisk = true` markets at the universe build** before constructing the price index; report the excluded count (Minor-1).
- **Cross-check** a recent slice via `build_cse_live_context.py` to confirm the block→epoch ~minutes error doesn't distort price-at-T at the 24h point (immaterial for `active`-fidelity legs by construction; the spot-check confirms it).

## Method (reuse CSE primitives — do NOT reinvent)

Per cohort: `detection.calibration_edge(rows)` (win-rate vs price-at-T) under this guard battery:
1. **FDR family (Important-1):** `benjamini_hochberg` over **cohorts passing `min_n` at the PRIMARY T ONLY**. Secondary-T results are descriptive and CANNOT drive a promotion. Use **BH** by default; if any cohort pair shows **negative** pairwise test dependence (e.g. complementary price bands sharing markets), apply **BY** (dependence-robust) as a sensitivity check.
2. **Min-sample:** `min_n = 200` legs/cohort.
3. **Cost floor (Important-2):** band-specific (see §Costs).
4. **OOS time-split:** fit/observe on the **earlier 70% by `created_at`**, confirm on the later 30%. The split point is the `created_at` percentile **pre-computed and frozen before any cohort statistic is computed**. **OOS-confirmed** = OOS-half point estimate ≥ 0 AND one-tailed p ≤ 0.10 in the OOS half independently. A large-n near-zero OOS result is a KILL.
5. **Placebo (Critical-2 — MARKET-level):** permute at the **market** level — for each market in the cohort, flip a fair coin and, if flipped, swap which leg wins (i.e. flip the resolution outcome of the whole market), preserving "exactly one leg per market wins." Do NOT shuffle individual leg labels independently. Seed pre-registered. The real edge must beat this null (`placebo_pass`).
6. **Promotion gate:** `promotion_decision(n, effect, q, oos_confirmed)` AND `placebo_passed` — a cohort is a candidate ONLY if it clears every gate.

## Costs (mandatory, band-specific — Important-2)

Per-band pre-registered spread estimate `s(band)` and floor `net_edge ≥ max(0.03, 2·s(band))`:
- [0–0.10]: s=0.05 · [0.10–0.35]: 0.03 · [0.35–0.65]: 0.02 · [0.65–0.80]: 0.02 · [0.80–0.90]: 0.02 · [0.90–0.97]: 0.03 · **[0.97–1.0]: EXCLUDED** (upside `1−price` < spread by construction).
- **Report ROI-per-dollar = edge / price_at_T alongside the pp-edge** for every cohort so economic magnitude is visible (a 3pp edge at 0.05 ≠ at 0.50).

## Traps + guards

- **Favorite/longshot tautology:** calibration (win-rate vs price) controls for "favorites win more" by construction; one-leg-per-market + per-band reporting prevents a single band masquerading as a global edge.
- **Two-leg dependence:** eliminated by one-leg-per-market (Critical-1) + market-level placebo (Critical-2).
- **Leakage:** the §Observation-point exclusions (Critical-3).
- **Sparse price:** `active`-fidelity inclusion criterion; **drop any cohort where >30% of candidate legs are excluded** by the fidelity/leakage filters (Important-4).
- **Market-selection bias:** we only see listed markets; do not extrapolate to unlisted. Stated, not corrected.
- **Underpowering is a valid kill (review trust note):** after the one-leg + leakage + fidelity filters the universe shrinks materially; many strata may fall below `min_n`. A credible null from underpowering is still a kill — but the deliverable must report n per cohort so "no edge" is distinguished from "not enough data," and the latter is NOT spun as evidence of efficiency.

## Pre-registered kill / promote criteria  *(Important-3 — no degrees of freedom)*

- **PROMOTE** a cohort iff it clears **ALL FOUR**: FDR q ≤ 0.05 (primary-T family) **AND** OOS-confirmed (as defined) **AND** market-level placebo passed **AND** band-specific net cost floor. → forward-paper candidate (NOT a live deploy).
- **KILL** otherwise. **3-of-4 is a KILL.** No post-hoc weakening of the cost floor, `min_n`, or price-band boundaries. **Exploring finer/other cohort definitions after seeing primary results is forbidden** regardless of motivation. If zero cohorts survive, write the kill finding immediately — no re-runs with loosened parameters.

## Deliverable

- Ranked cohort table: gross edge, **net edge, ROI-per-dollar**, n, q, OOS-confirmed, placebo-passed, promotable, **excluded-fraction**.
- One-line verdict: KILL or the promoted cohort(s).
- A brain finding either way (a clean kill is a result).

## Build sequence

1. ✅ `research-methodologist` design review (done 2026-05-30; this rev applies the fixes).
2. Full Becker `MarketContext` build (resolved universe, `negativeRisk` filtered, price-at-T index); pre-compute + freeze volume terciles + the OOS split percentile.
3. Cohort builder: one-leg-per-market → leakage/fidelity filters → stratify → per-cohort `calibration_edge` + n + ROI.
4. Guard battery across the primary-T family: BH (BY sensitivity) + OOS split + market-level placebo + band cost floor + promotion.
5. Rank, apply kill criteria, write the finding.
6. IF promoted: forward-paper the cohort (separate, pre-registered).

## Revision history

- **rev 2 (2026-05-30):** applied all methodology-review findings — Critical-1 one-leg-per-market, Critical-2 market-level placebo, Critical-3 T-leakage exclusions; Important-1 FDR family = primary-T only + BH/BY, Important-2 band-specific cost floor + ROI, Important-3 tightened kill criteria (3-of-4 = kill, fixed OOS threshold + split), Important-4 `active`-fidelity inclusion + 30% drop rule; Minor-1 `negativeRisk` filter, Minor-2 frozen volume terciles; + underpowering-is-a-valid-kill note.
- rev 1 (2026-05-30): initial pre-registration.
