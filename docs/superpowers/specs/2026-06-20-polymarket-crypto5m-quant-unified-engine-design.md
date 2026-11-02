# Phase 2B — Unified Quant Engine (btc5m-quant-shadow-v2) design

**Date:** 2026-06-20 · **Status:** design locked, shadow/replay-only, no orders.
**Goal:** fold every tool we have into ONE quant cohort that records, per window, the full multi-signal
panel + a calibrated ensemble + the executable comparison, and benchmarks it against C0 (random) and C1
(favorite). Maximize the toolset to *test* for an edge — not to claim one.

## Principle (unchanged)
Beating Polymarket on this instrument requires a calibrated probability that differs from the EXECUTABLE
price by more than fees+slippage, OR a timing/microstructure edge. The historical test already showed the
structural *direction* is priced (84.6% acc but the market knows it). So the only candidate edges are:
lead-lag vs external venues, order-book microstructure, volatility/jump mispricing, and residual-vs-market.
v2 instruments all of them concurrently so we can evaluate which (if any) survives — paired against C1.

## What v2 combines (every tool, one panel per window)

| Tool | Source | Module |
|---|---|---|
| Market prior `q_market` | Polymarket Up midpoint | features F / book |
| Structural (realized vol) `q_struct_realized` | Binance within-source move (variant-b, VALIDATED) | quant_probability / signals |
| Structural (implied vol) `q_struct_implied` | Deribit DVOL → per-sec σ | quant_sources + signals |
| Monte-Carlo jump/regime `q_mc` | Binance path + jumps | quant_probability Q2 |
| Residual `q_residual` | logit(q_market)+features (trained later) | quant_probability Q3 |
| Multi-source latent + disagreement | Binance+Coinbase+Kraken+Deribit | quant_fusion + quant_sources |
| Cross-source basis / lead-lag proxy | fused external vs market lean | quant_signals |
| Microstructure: OFI, microprice, imbalance, spread | both token books | quant_matrix / signals |
| Vol regime: realized vs implied (DVOL) | Binance path vs DVOL | quant_signals |
| Calibrated ensemble + uncertainty | log-opinion pool of the q_* | quant_ensemble (NEW) |
| Conservative `q_trade` (shrink-to-prior) | ensemble − k·uncertainty toward 0.5 | quant_ensemble (FIXES v1 overconfidence) |
| Executable cost + net edge per fee | FOK ladder, F0/F1/F2 | quant_matrix |
| Timing T0/T1 | edge threshold/persistence | quant_timing |
| Sizing S0/S1/S2 | capped fractional Kelly | quant_sizing |
| Benchmarks C0 (random), C1 (favorite) | recorded per window for pairing | quant_signals |
| Regime/selection tags | vol/disagreement/time-of-day | quant_signals |

## New modules (this phase)
1. `quant_sources` — pure parsers for Coinbase/Kraken/Deribit ticker+index+DVOL; DVOL→per-second σ.
2. `quant_signals.build_signal_panel(...)` — pure; assembles the full per-window panel above incl. C0/C1.
3. `quant_ensemble` — pure; log-opinion-pool ensemble, predictive uncertainty, and an
   **uncertainty-shrunk conservative probability** (shrinks toward 0.5, never to 0/1 — fixes the v1 bug
   where agreement manufactured fake favorite "edges").

## Runner: `run_polymarket_crypto5m_quant_shadow.py --version v2`
Per window at 240s(+latency): fetch both books + Binance 1s path + Coinbase/Kraken/Deribit + DVOL →
`build_signal_panel` → `quant_ensemble` → `quant_matrix` (q_trade = conservative ensemble) → record matrix
(now carrying the full panel + c0/c1 sides) + shadow decision → settle on the real outcome. Records to
`data/crypto5m/btc5m_quant_shadow_v2.sqlite`, campaign `btc5m-quant-shadow-v2`. Frozen on config hash;
restart-safe. NO orders. v1 superseded (data kept).

## C0/C1 are benchmarks, not the system
The old C0–R3 forward rules are kept ONLY as controls: C0 = random floor, C1 = favorite baseline. v2 must
beat C1 on paired windows after fees, or it has no edge. They are recorded inside the v2 matrix
(`c0_side`, `c1_side`) so the report computes paired hit-rates without a schema change.

## Evaluation (per the standing protocol)
Unit = one window. As n grows: hit-rate and net edge (F0/F1/F2) for the ensemble, paired vs C1; the
RESIDUAL SPLIT (agree vs differ from favorite); calibration (Brier/ECE); negative controls (random side,
shuffled labels, lagged source); by-regime breakdown. No edge claim until n is large, controls fail, and
multiple-testing passes. Promotion to a real forward cohort is a separate, later, operator-gated step.

## Honesty / constraints
Shadow/replay-only; no orders, keys, prod DB, or frozen-forward-campaign mutation. Fee unresolved → every
economic number under F0/F1/F2. Adding a source/signal is only "kept" if it improves OOS in ablation once
data exists; until then everything is recorded but nothing is claimed.
