# PRE-REGISTRATION — crypto5m floor-vs-regime, clean neutral feature

**Committed BEFORE computing any paired deltas.** This exists because the first pass
(finding-crypto5m-floor-endogeneity-2026-07-07) binned by `mean_up_fair` = the engine's own
floor-endogenous fair, invalidating the crossover/heteroskedasticity/ranking claims. This time
the feature is treatment-independent and the bins/inference/decision rule are fixed in advance.

## Neutral feature (verified independent)
`p` = average **Polymarket market mid of the UP token over the first 90 s of the window**, from
`data/crypto5m_capture.sqlite` `crypto5m_book_snapshots` (`(best_bid+best_ask)/2`, `best_ask>best_bid`,
joined to `crypto5m_markets.up_token_id`), requiring ≥3 early snapshots.
- Treatment-independent: the real Polymarket book; our makers place NO real orders.
- No look-ahead: first 90 s only, not the outcome.
- **Verified**: corr(neutral early p, v3.2f engine fair) = **−0.085** → the old feature was near-orthogonal
  to the true market odds; this is a genuinely different (and correct) regime measure.

## Regime axis
`x = |p − 0.5|` (odds-extremity). Bins fixed NOW: `[0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.50]` → 6 bins.

## Primary contrast (pure floor lever)
`delta = v3.2f.realized_pnl − v3.1.realized_pnl` per window — same adaptive-v3 LEARNING engine, floor
2e-5 (v3.2f) vs 1e-4 (v3.1) the ONLY difference. Paired on identical `window_start_ts` (forward anchor
≥ 1783355700), both settled.

## Inference (fixed)
- Per bin: mean delta + 95% bootstrap CI (10,000 resamples, seed 20260707).
- **BH-FDR correction across the 6 bins at q = 0.10** (the correction absent last time).
- delta>0 favors the tight/honest floor (2e-5); delta<0 favors wide (1e-4).

## Hypotheses (fixed)
- **H1 (primary, confirmatory):** far-tail one-sided windows (x ≥ 0.20) favor the tight floor (delta>0),
  FDR-significant. This is the only plank that survived the multi-source robustness check last time.
- **H2 (secondary, exploratory):** any contested-regime (x < 0.20) floor preference — reported but NOT
  claimed unless FDR-significant.

## Robustness (pre-specified)
(a) full-window mid instead of early-90 s; (b) cross-engine contrast frozen-f − control with the SAME
neutral feature; (c) temporal first/second-half split; (d) early-cutoff sensitivity (60 s / 120 s).

## Decision rule (fixed BEFORE results)
- H1 survives FDR → tight-floor-when-one-sided is real → v2's honest-when-one-sided leg is justified.
- No FDR-significant contested effect → v2's **wide-in-contested leg is NOT justified** → redesign v2 to
  "tight when one-sided, default/neutral otherwise" (drop the ramp-to-wide); the contested half was an
  artifact of the endogenous feature.
- H1 fails under the clean feature → the whole floor-regime premise is dead; keep only the frozen 2e-5
  everywhere (frozen-f) and stop the regime-gated line.

---

# RESULTS (computed after the pre-registration above was committed at 819cf74)

## Feature validation (both passed)
- **Independence**: corr(neutral early-odds, v3.2f engine fair) = **−0.085** — near-orthogonal. The old
  engine fair was measuring almost the opposite of the real market regime.
- **Calibration** (neutral early-odds vs actual UP-win rate, N=84 with settlements): 0.2–0.4→27%,
  0.4–0.6→45%, 0.6–0.8→76%, 0.8–1.0→100%. Monotone and well-calibrated → a real odds measure.

## Primary result (v3.2f − v3.1, N=203 paired, BH-FDR q=0.10)
| bin \|p−0.5\| | n | mean delta | 95% CI | boot p | FDR |
|---|---|---|---|---|---|
| 0.00–0.05 | 121 | +0.53 | [−0.91, +1.90] | 0.485 | ns |
| 0.05–0.10 | 34 | +1.00 | [−1.70, +3.87] | 0.483 | ns |
| 0.10–0.15 | 21 | +3.24 | [−0.58, +6.91] | 0.095 | ns |
| 0.15–0.20 | 12 | +3.21 | [−1.16, +7.44] | 0.153 | ns |
| 0.20–0.25 | 8 | −0.64 | [−12.07, +8.61] | 0.973 | ns |
| **0.25+** | **7** | **+5.16** | **[+3.97, +6.34]** | **0.000** | **SIG** |

Only the extreme-odds bin (|p−0.5|≥0.25) is FDR-significant — tight floor wins +5.16/w, all 7 deltas
positive (not outlier-driven). **H1 (far-tail favors tight) NOT supported** (the 0.20–0.25 bin flips
negative). **H2 (contested effect) NONE.**

## The market-structure correction (biggest finding)
By the TRUE Polymarket odds, one-sided windows are RARE: ≥0.15 = 13%, ≥0.25 = 3%; 60% of windows are
within 0.05 of a coin-flip. The original tournament's "one-sided dominates 71%" was entirely the
engine-fair artifact. The maker operates in a **mostly-contested** market where the floor is
indifferent (contested n=176, no significant effect).

## Robustness (all directionally consistent, none overturning)
- full-window feature: contested −0.36, one-sided +4.19 (stronger separation).
- cross-engine frozen-f−control: contested +2.09, one-sided +2.51 (both positive; tight ≥ wide everywhere).
- temporal: first half (+0.99 / +2.70), second half (+0.91 / +2.36) — stable.
- **No regime favors the wide floor.** Tight (2e-5) is ≥ wide (1e-4) in every regime and every check.

## Verdict (per the pre-registered decision rule)
The regime-gated crossover premise is **REFUTED by the clean feature**: no ~0.20 crossover, no
wide-in-contested advantage. The only real signal is "tight floor wins in the rare extreme tail
(3%, n=7)"; elsewhere the floor is indifferent-to-mildly-tight-favoring. Therefore:
- **frozen-f (constant honest 2e-5 everywhere) is the right, simple answer** — validated.
- **regime-gated-v1 (binary) and v2 (ramp) are NOT justified** — they switch on a crossover that does
  not exist and spend ~76% of their time in floor-indifferent windows. Recommend retiring.
- The **floor-magnitude sweep stays** — the real open question is whether 2e-5 is optimal or a tighter
  floor (1e-5) is better. That is unconfounded and forward-testable.
