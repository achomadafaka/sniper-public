# SwissMiss paper-copy validation — pre-registered study design

**Spec:** v1 design (this doc). Design only — no trading, no live deployment.
**Subject wallet:** `0xdbade4c82fb72780a0db9a38f821d8671aba9c95` (SwissMiss)
**Status:** pre-registered. Criteria below are the contract; changing them after data is seen is p-hacking.
**Priors:** skeptical. Her out-of-sample time-split replay FAILED (R9.5 "underpowered"), her current geo-NO regime is bleeding (−$1.5M named), her bread-and-butter band (sub-$0.55 underdogs) is structurally −9.76% carry, and her dossier verdict is "Do not copy." We test her *anyway* because if a skeptically-priored wallet clears the bar, that's strong signal — and if it doesn't, we've cheaply confirmed the survivorship read.

---

## Why this study exists

Five structural/price-band edges have now died under honest (market-level) weighting: longshot-buy (−9.76%), favorite-band (−4.1% market-level), $0.99-scrape (efficient), mid-band (efficient), structural-NO (Batch 1 MC-killed). The only un-exhausted hypothesis is **wallet-level selection skill** — that a specific wallet picks winners better than the band average. SwissMiss is the test case.

The question is narrow and binary: **is her edge skill (persists forward, capturable by a copier) or survivorship (luck, won't persist)?** Copying her ≠ running her style — a copier inherits *her selections*, so the cohort base rate (72% of her archetype loses) does not directly apply. What applies is whether HER forward selections beat baseline net of copy-latency decay.

## Hypothesis (pre-registered)

**H1:** A paper book that copies SwissMiss's *new* entries — filtered to the mechanism-justified subset (below) — produces positive net PnL over the forward sample, AND beats both baselines:
- B1: always-favorite on the same markets (the −9.76%/efficient-market null)
- B2: her own archetype cohort base rate

net of modeled fees and the measured copy-latency decay.

**Baseline aggregation (pre-registered, per methodology review Issue 2):** B1 and B2 are computed at **market level** — one observation per market, unweighted, then averaged across markets — NOT trade/volume-weighted. This is the exact discipline whose absence killed Batch 1 (favorites looked +2.8% volume-weighted but −4.1% market-level). The copied book itself is evaluated per-copy (trade-level is correct for a single-wallet copy book), but the *nulls* it must beat are market-level.

**B2 scope (per review Issue 4):** B2 is the **full archetype cohort base rate** (from the survivorship catalog — easy to compute, weaker null). The stronger null — forward regression rate of *top-1%-historical-outlier* wallets — is not yet computable (needs forward cohort data), so **C4 is designated the primary skill-vs-survivorship gate**, not B2.

**Null (likely true given priors):** the copied book does not beat B1/B2 after decay — markets are efficient and her edge is survivorship or un-capturable.

## Copy filter (pre-registered — mechanism, not outcome)

Copy a SwissMiss BUY only if ALL hold:
1. **Category** ∈ {sports, games, esports} — her proven regime (cfb, nba, ufc, mlb, lol, dota, etc.)
2. **Entry price ≤ $0.55** — her documented underdog-selection band (+6.7pp historical)
3. **NOT geopolitics/macro-NO** — explicitly excluded (new, unproven, bleeding)
4. **Conviction threshold met** — her cumulative fills in the market cross a $-threshold within a time window (see "Entry timing" — the threshold is a measured parameter, not a guessed one)

Rationale for each cut is a mechanism (favorite-longshot-bias exploitation in a domain she's traded for 765 days), not a hindsight outcome. Three coarse cuts, no fine parameter sweep. The forward holdout validates against overfit.

## Entry timing — ONE locked decision policy + a descriptive curve

She builds positions via many micro-fills over hours; a $500 copier has no market-impact reason to replicate that and takes a single fill. The open question is *when* in her build to copy.

**Decision policy (single, pre-registered — used for all kill criteria):**
> Copy a qualifying market when her **cumulative fills cross $25,000 within 6 hours** of her first fill in that market. One fill, at the copier's prevailing price at threshold-crossing.

Justification (mechanism, locked before any forward data): $25K is ~3× her median per-fill (~$8.5K) and signals a multi-fill conviction position, not a toe-dip; the 6-hour window excludes slow drift-ins where the thesis/price has already matured. This is ONE policy, not a grid — there is no entry-policy selection step, so no multiple-comparisons / overfit vector.

**Descriptive-only curve (NOT a selection input):** for transparency we *also* report edge_retained under first-fill and a couple of alternative thresholds, purely to characterize her accumulation dynamics (the user's question: why she builds, where the edge sits in the build). These numbers are **reported, never used to pick the decision policy** — the decision policy is locked above. Per the methodology review (Issue 1), selecting the policy from a forward sweep would be p-hacking; we explicitly do not do that.

If the locked $25K/6h policy proves to never trigger (her sports conviction fills may be smaller than geo), that is itself a finding — re-pre-register a single revised threshold from her *historical* cadence BEFORE observing forward outcomes, never after.

## Position sizing (pre-registered)

- Per-copy notional fixed at a small flat $ (paper: model $20–$50 to mirror the eventual $500 live rig), NOT proportional to her notional. Mirroring her share count or notional is wrong — she trades at $103M-capital scale.
- Max concurrent open copies capped (pre-register, e.g. 15) so the paper book reflects a realistic small-account constraint.
- Hold to resolution (her exit is REDEEM; 1.2% sell ratio). No active exit logic — copying her hold discipline.

## Kill criteria (all evaluated on the OUT-OF-SAMPLE / forward sample)

1. **C1 — net positive:** cumulative net paper PnL after modeled fees + decay > 0. If ≤ 0 → KILL.
2. **C2 — beats baselines:** copied-book edge > max(B1 always-favorite, B2 cohort base rate) by a pre-registered margin. If not → KILL (no edge over the null).
3. **C3 — significance:** shuffle test (permute which copied markets won, preserve marginal WR) that observed PnL exceeds chance. **Threshold is power-dependent (see Sample size below):** p < 0.05 if N ≥ 80 resolved copies; if N < 80 this is a *screening* gate at p < 0.10 and confirmation is explicitly deferred to a follow-up at N ≥ 120. Pre-register which regime applies before trade 1 resolves.
4. **C4 — skill-not-survivorship (PRIMARY survivorship gate):** her forward win-rate in the copied band ≥ her historical, within CI. If forward WR regresses materially below historical → she was a survivor → KILL. This, not B2, is the load-bearing skill-vs-luck test (per review Issue 4).
5. **C5 — capturable (cost-model accuracy, not edge-vs-slippage):** the $500 rig measures actual fill cost (slippage + gas + latency). C5 PASSES if mean actual fill cost is **within 2× the cost assumption baked into the paper model at design time**. It does NOT compare live slippage to a noisy N=15 paper edge (per review Issue 5 — that would be noise-vs-noise). The rig's job is to confirm the paper simulator's cost input is not materially wrong; if actual cost > 2× modeled, the paper edge is built on a bad cost assumption → re-run paper with corrected costs before any deployment.

**Sample size + power (pre-registered, per review Issue 3):** compute expected N at setup from her *historical* qualifying-sports-fill cadence × the calendar window. Detecting her ~+6.7pp edge over the structural headwind needs N ≥ ~80 for ~0.05 power; her realistic cadence may yield only 30–60 resolved copies in 6 months. Therefore: pre-register a **12-month window** (not 6) to push N toward 80+, OR if 12-month N still projects < 80, declare the study **screening-grade** (α=0.10, C3 demoted) up front and disclose that a positive result is a green-light to a *longer confirmation run*, not a deployment trigger. Lock the window + N projection + α BEFORE trade 1. No early peeking, no early stop.

## Decision matrix

| C1 | C2 | C3 | C4 | C5 | Action |
|---|---|---|---|---|---|
| PASS | PASS | PASS | PASS | PASS | Edge validated forward + capturable → THEN consider a tiny live pilot (separate operator decision) |
| PASS | PASS | PASS | PASS | FAIL | Real edge but eaten by live costs → not deployable at this size; document |
| any FAIL | | | | | KILL — write up, close the SwissMiss copy hypothesis, move to next survivorship-ranked wallet |

## The $500 instrumentation rig (separate from the paper test)

NOT a profit attempt. Its only job is to measure what paper can't, feeding C5:
- intended_price vs actual_fill_price (slippage)
- signal_time → fill_time (latency; for copy, source-trade → our fill)
- gas_cost as % of position
- fill_rate (did the order fill at the intended price at all)

~10–20 deliberately tiny live positions ($20–30). The rig's output is a **measured cost-per-fill** (slippage + gas + latency), compared against the paper model's design-time cost assumption (C5: pass if actual ≤ 2× modeled). It does NOT gate on live-cost-vs-paper-edge at N=15 — at that N the paper edge is noise, so that comparison would be noise-vs-noise (review Issue 5). Pre-register a hard safety stop on cumulative loss (e.g. halt at −$150 of the $500) independent of the edge question. Results recalibrate the paper simulator's cost model — valuable regardless of which strategy ultimately deploys.

## What this study does NOT do

- Does not deploy real money for return-seeking (the $500 is measurement only)
- Does not claim SwissMiss is copyable until C1–C5 all pass forward
- Does not replicate her position-building (a small account doesn't need to)
- Does not slice on individual markets or fine parameters (overfit guard)

## Open inputs to resolve at setup (all locked BEFORE trade 1 resolves)

1. Real-time data source for her *new* fills (Polymarket Data API activity poll vs subgraph). Source-feed latency is itself part of the C5 cost measurement.
2. N projection + calendar window + α — pinned from her historical qualifying-fill cadence (per C3 power requirement). Document with a timestamp.
3. Baseline B2 — the `underdog_accumulator_v1` cohort from the survivorship catalog (her archetype), computed market-level.
4. **Verify the +6.7pp reference (per review Issue 6):** confirm whether her documented +6.7pp underdog edge is excess over (a) a 50% WR null, (b) the −9.76% structural carry null, or (c) a volume-weighted market average. C2's "beat baseline by margin" threshold must be set against the correct reference — this changes whether the bar is +6.7pp or ~+16pp net of headwind. Resolve before C2 margins are locked.
5. Paper-model cost assumption (the design-time number C5 checks against 2×) — set explicitly at setup so C5 has a fixed target.

## Cross-references

- Subject dossier: `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md` (verdict: DISSECT / "Do not copy")
- Survivorship context: `finding/survivorship-cohort-swissmiss-2026-05-27` (her archetype cohort: 72% lose)
- Calibration basis: tonight's per-band carry analysis (longshot −9.76%, favorite −4.1% market-level) — the structural backdrop her selection must beat
- Copy-latency decay model: `copy_latency_surface` (35% edge loss by 15 min)
- Methodology kin: `studies/batch1_killed.md` (pre-registration + MC + market-level weighting discipline)
- Cohort surface (now wired live, PR #108): `cohort_survivorship_surface`
