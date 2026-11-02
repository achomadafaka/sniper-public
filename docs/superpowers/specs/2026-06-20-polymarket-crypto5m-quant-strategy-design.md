<!-- schema:external=crypto5m_quant_decisions -->
<!-- schema:external=crypto5m_quant_campaigns -->
<!-- schema:external=crypto5m_quant_matrix -->
# Phase 2A — Quant Fair-Value, Dynamic Timing, and Paper Risk Engine (design)

**Date:** 2026-06-20
**Status:** design locked for the first cut; shadow/replay-only; no forward cohort activated.
**Instrument:** Polymarket BTC Up/Down 5-minute binary (resolves Up when Chainlink end ≥ start).

## Principle

Model **residual mispricing relative to the market**, not BTC direction from scratch. The market's own
Up/Down midpoint is a strong, already-informative prior. The system estimates a calibrated
`P(Up wins | all info now)` and acts only when that probability differs from the **executable** Polymarket
price by more than the sum of ask spread, taker fee, slippage, latency, model uncertainty, and
data-quality risk. Three separate questions: **probability**, **timing**, **sizing**.

## 0. Empirical clue that motivates this (with uncertainty)

From the frozen forward campaign `btc5m-forward-paper-v0` (this is a *clue*, not a result — n is tiny):

| Rule | n (resolved windows) | WR | 95% CI (Wilson) | PnL F1 |
|------|----------------------|-----|-----------------|--------|
| C0 random control | 12 | 0.50 | 0.25–0.75 | −$47 |
| **C1 market-favorite** | **12** | **0.92** | **0.65–0.99** | **+$22** |
| R1 momentum | 10 | 0.50 | 0.24–0.76 | −$30 |
| R2 book imbalance | 12 | 0.50 | 0.25–0.75 | −$32 |
| R3 R1∧R2 agreement | 4 | 0.50 | 0.15–0.85 | −$10 |

C1 (buy the higher-midpoint side) is strongest; the directional rules sit at coin-flip. Read: at the
4-minute checkpoint the **market price already contains the directional information**, and naive BTC-direction
signals add nothing. **Caveat:** n=12, CI is wide, and buying the late-window favorite is the known
`finding-favorite-scrape-is-negative-ev` pattern at scale — favorites pay little and one flip erases the
edge. C1 is therefore the *benchmark to beat*, not a validated edge. The quant target is residual value
**beyond** C1.

## A. What already exists and is REUSED (no duplication)

Verified in `sniper/algos/polymarket_crypto5m/`:

| Need (directive) | Already implemented | Reuse as |
|---|---|---|
| Q0 market prior | `probability_model.MarketMidpointBaseline` | the prior anchor |
| Q1 structural diffusion | `probability_model.ThresholdDiffusionV0` | structural baseline |
| Decision edge engine | `decision.evaluate` (edge = q − fee-inclusive cost, fail-closed gates) | shadow decisioning |
| FOK ask-ladder execution | `taker_sim.estimate_taker_buy` | executable cost |
| Features A–F (geometry, chainlink path, binance, basis, book/exec, market) | `features.build_feature_snapshot`, `feature_registry` | model inputs |
| Dataset (1 row per `condition_id×checkpoint`, label = Polymarket outcome) | `dataset_export` | training matrix |
| Walk-forward + negative controls + label shuffle | `market_efficiency.walk_forward_comparison`, `negative_controls`, `shuffle_labels_within_blocks` | OOS validation |
| Leakage audit (known-at-time) | `readiness_audit.leakage_checks` | leakage gate |
| Shadow replay over captured DB | `replay.replay`, `sim_persistence` | shadow runner base |
| Boundary-rule calibration | `calibration.two_stage` | locks structural P2B |
| Capture spine (13 tables: markets, books, chainlink ticks, P2B candidates, binance obs, settlements…) | `persistence`, `capture`, `chainlink_rtds`, `run_polymarket_crypto5m_capture.py` | data source |

**Implication:** Phase 2A is mostly *new decision-science on top of an existing spine*, plus a new
shadow cohort and multi-source fusion — not a rebuild.

## B. What is genuinely NEW in Phase 2A

1. `quant_fusion` — multi-source latent BTC state (robust weighted median composite + disagreement + uncertainty).
2. `quant_probability` — **Q2** jump/regime Monte Carlo terminal probability; **Q3** residual model anchored on `logit(q_market)`.
3. `quant_matrix` — the per-decision price-comparison matrix (§9 of the directive) assembled from existing features.
4. `quant_timing` — **T0** threshold-cross, **T1** edge-persistence (pure policies; T2 DP / T3 event-driven deferred).
5. `quant_sizing` — **S0** fixed, **S1** frozen edge-buckets, **S2** fractional-Kelly with hard caps (paper-only).
6. `quant_persistence` — new cohort family `btc5m-quant-shadow-v1` (separate tables; never touches forward/sim tables).
7. `quant_registry` — experiment registry + nested walk-forward orchestration over the existing evaluator.
8. New source adapters (Coinbase/Kraken/Deribit) — feasibility proven (all reachable, ~190–320ms), built only after they beat ablation.

## C. Binding blockers (must be reported, not hidden)

1. **No captured data.** `data/crypto5m_capture.sqlite` does not exist; the only resolved windows are the
   12 from the forward campaign. The directive's validation protocol needs hundreds–thousands of
   independent windows. **The #1 gate is running continuous capture.**
2. **Boundary rule unlocked.** `price_to_beat_resolver.LOCKED_BOUNDARY_RULE = None`. Structural
   `P(S_end ≥ PriceToBeat)` is **uncomputable mid-window** until `calibration.two_stage` locks the rule on
   ≥100 windows of RTDS-vs-oracle match-rate. Q1/Q2 stay diagnostic until then. Q0+Q3 (residual on the
   market prior + microstructure) do **not** need P2B and can be developed first.
3. **Single predictive source today.** Only Binance + Polymarket books are wired. Coinbase/Kraken/Deribit
   adapters do not exist yet (feasibility proven). Fusion runs in degraded (single-source) mode until added.
4. **Fee unresolved** (`finding-polymarket-crypto5m-fee-conflict`): every economic number reported under
   F0 gross / F1 documented-0.07 / F2 canonical, never collapsed.

## D. Component designs

### D1. Source fusion → latent state (`quant_fusion`)
Pure. Input: list of source observations `{source, price, source_age_ms, stale, gap, dispersion_weight?}`.
Output: `latent_btc_price`, `latent_price_uncertainty`, `source_disagreement` (robust spread),
`source_contribution_weights`, `state_quality ∈ {ok, degraded, single_source, unusable}`. Weight =
freshness × inverse-recent-tracking-error × inverse-spread; stale/gapped sources drop to weight 0. High
disagreement ⇒ higher uncertainty ⇒ smaller/zero size downstream. **Never** substitutes Binance for
Chainlink as settlement truth.

### D2. Structural probability (`quant_probability` Q2)
Vectorized Monte Carlo for `P(S_end ≥ PriceToBeat)`: continuous log-return component + time-varying σ +
optional jump component (intensity λ, jump-size dist), seeded in research mode. Output `q_structural_up`,
`simulation_standard_error`, distribution quantiles, `model_assumptions`. Must beat Q1 OOS to be retained;
complexity is not a credential.

### D3. Residual model (`quant_probability` Q3)
`logit(q_final) = logit(q_market) + residual(features)`. `q_market` = predeclared Polymarket midpoint
baseline. Start interpretable: regularized logistic / GAM on the A–F feature families (this generalizes the
existing `market_efficiency` `logit(midpoint)+features` logistic into a named, persisted residual model).
The residual answers: *does external/microstructure info add anything beyond the price?*

### D4. Ensemble + uncertainty
Calibrated stack over {market prior, Q1/Q2 structural, Q3 residual, (later) sequence}. Weights fit only on
a designated calibration interval. Decision consumes an **uncertainty-adjusted** probability
(`q_trade = q_up_lower`, a predeclared posterior-risk lower bound), not the mean.

### D5. Price-comparison matrix (`quant_matrix`)
Per valid decision time, assemble the §9 record: all `q_*`, both tokens' best ask + executable
$5/$10/$20 + fee-inclusive breakeven + microprice + OFI, all source prices + latent, and derived
`forecast_edge_vs_midpoint`, `net_edge_vs_executable_ask`, `market_residual`, cross-source basis. The only
actionable paper edge = uncertainty-adjusted prob − fee-inclusive executable cost − safety margin.

### D6. Dynamic timing (`quant_timing`)
Fixed checkpoints remain controls. T0: enter when uncertainty-adjusted net edge ≥ threshold and gates pass.
T1: require the edge to persist ≥ N updates/seconds (kills one-tick noise). T2 (DP value-of-wait) and T3
(event-driven re-evaluation) deferred behind shadow flags until data exists.

### D7. Sizing (`quant_sizing`)
S0 fixed $10. S1 frozen edge-buckets (small/med/large → fixed stakes). S2 fractional Kelly on the binary
payoff with fee-inclusive entry cost, computed from a **conservative** (lower-bound/shrunk) probability,
fraction ∈ {0.10, 0.25}, with hard caps: max fraction/market, max total open exposure, max
consecutive-loss exposure. Stale/disagreeing/uncertain ⇒ size → 0. Paper-only; never real-money advice.

### D8. Selection / regime engine
Known-at-time regime gate (vol/liquidity/spread/consensus/threshold-distance/time-of-day/macro-flag/
book-integrity) → `eligible | ineligible`. Likely where the real edge lives (select abnormal windows, not
forecast every ordinary one). Small predeclared candidate set, nested walk-forward selection — no
retrospective regime shopping.

### D9. Cohort persistence (`quant_persistence`) — new family `btc5m-quant-shadow-v1`
Separate tables (`crypto5m_quant_campaigns`, `crypto5m_quant_decisions`, `crypto5m_quant_matrix`,
`crypto5m_quant_settlements`, `crypto5m_quant_eval_runs`). Mirrors the frozen-campaign + idempotent
content-key discipline of `forward_persistence`/`sim_persistence`. Each model/policy/sizing change → new
version + future activation timestamp + separate results. No historical backfill presented as forward.

## E. Hard constraints (verbatim from directive)

No live orders, no private keys, no authenticated trading endpoints, no production DB, no service restart,
no mutation of `btc5m-forward-paper-v0`, no rule added to the existing campaign after start, no in-sample
edge claim, no promotion on gross PnL alone, no random train/test split, no Binance-derived labels, no
hidden checkpoint optimization, no full/uncapped Kelly, no black-box promoted without ablation + calibration.

## F. Validation protocol & promotion bar

Unit of independence = **one market window** (not a book event, checkpoint, or tick). Nested walk-forward,
day/time-blocked splits, separate model-selection vs calibration vs untouched-final-test intervals. Ablation
ladder: market-prior-only → +structural → +chainlink path → +binance → +cross-source basis → +book →
+sequence; a family stays only if it improves untouched-temporal performance. Metrics: Brier, log loss,
calibration intercept/slope, ECE, net PnL (per fee scenario), ROI, drawdown, trade/no-trade rate,
fillability, edge-bucket/timing/sizing breakdowns — all paired against C1 on identical windows. False-discovery
control: experiment registry, predeclared families, multiple-testing correction, negative controls (shuffled
labels, random side, random timing, lagged irrelevant source, leakage-blocked future feature, random regime),
day-block bootstrap, time-uniform forward monitoring. **A candidate becomes a forward cohort only when** it
beats the market-prior forecast OOS, adds value beyond C1 on paired windows, is net-positive after
documented/canonical fees, survives latency + FOK, calibrates acceptably, has stable uncertainty, is not
concentrated in one day/regime/checkpoint, negative controls fail as expected, and multiple-testing passes.
Paper-only throughout.

## G. Phased build

- **Phase 2A.0 (this cut, shadow/replay-only):** spec + new pure decision-science modules (fusion, Q2, Q3,
  matrix, T0/T1, S0/S1/S2) with unit tests + worked examples on synthetic state. No data needed.
- **Phase 2A.1 (data gate):** run continuous capture; accumulate windows; lock boundary rule via
  `calibration.two_stage`; build Coinbase/Kraken/Deribit adapters if ablation supports them.
- **Phase 2A.2 (model gate):** fit Q3/ensemble on captured data via the existing walk-forward + negative
  controls; ablation ladder; calibration.
- **Phase 2A.3 (cohort gate):** only if the promotion bar passes, register `btc5m-quant-shadow-v1` with a
  future activation timestamp and run it forward as a *separate* cohort.

## H. First-cut deliverables (Phase 2A.0)

`quant_fusion.py`, `quant_probability.py` (Q2+Q3), `quant_matrix.py`, `quant_timing.py`, `quant_sizing.py`
+ unit tests; worked examples (probability decomposition, comparison matrix, timing, sizing); this spec.
Cohort persistence, registry, shadow runner, and adapters are specified here and built in 2A.1+ behind the
data gate. Nothing in this cut places orders, touches the frozen campaign, or claims edge.
