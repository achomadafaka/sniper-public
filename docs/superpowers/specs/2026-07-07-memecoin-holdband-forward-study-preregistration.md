# Memecoin Hold-Band Forward Study — Pre-Registration

**Date:** 2026-07-07
**Status:** Pre-registered (locked before any read). Do NOT peek before the gates below are met.
**Motivated by:** `finding-memecoin-pool-arm-mapping-refuted` — discovery pool does NOT map to arms; hold is a per-trader property. This study tests whether a copied trader's **hold band** predicts our forward **copy-PnL**, with discovery pool as a covariate expected to be null.

---

## Hypotheses (locked)

- **H1 (primary):** forward realized copy-PnL differs by the copied trader's hold band, **measured in Arm A (momentum_a) ONLY**. Arm A's exit is our own bracket/rollover machine — independent of the source trader's hold behavior — so hold band there is a clean **selection** covariate. A-priori direction: *fast* traders (<1h) produce higher copy-PnL than *holders* (>1d). Reported two-sided.
  - **Confound guard (locked):** `control`/`alpha_b` mirror-exit on the trader's sell, so in those arms hold band mechanically **is** our trade duration, not a selection signal. Their band splits are **secondary/context only** and never used to accept/reject H1.
- **H2 (covariate / expected null):** discovery pool does NOT predict copy-PnL once hold band is accounted for — consistent with the refuted pool→arm mapping. To control the familywise error, H2 tests **one pre-committed pair — `leaderboard` vs `large_buy` within the `fast` band** (the only comparison with a plausible mechanism); any wider source×band scan is exploratory, Holm-corrected, and cannot by itself reopen the pool question.

## Measurement hardening (locked, per methodology review)

- **Frozen covariate:** hold band is snapshotted at position-open (`memecoin_positions.hold_band`), NOT joined live — re-running the analyzer must not reclassify closed positions. Live analytics is a fallback only for pre-freeze legacy rows.
- **Trader-clustered CI:** the bootstrap resamples by **trader**, not position (positions within a trader are correlated — repeated buys on the same pumps). A positions-independent CI would overstate power. `n_traders` is reported per cell and gated (below).
- **Censoring diagnostic:** the read-out reports `by_band_open` (open positions per band). Holders close later, so their closed sample is survivorship-biased toward whatever resolves early; the operator must read the closed/open split before interpreting. State the survivorship direction explicitly in the write-up.

## Unit & outcome

- **Unit:** one CLOSED memecoin paper position (`memecoin_positions.is_open=0`).
- **Outcome:** `realized_pnl` (already slippage-modeled on exit).
- **Factors:** (a) trader **hold band** from `memecoin_trader_analytics.median_hold_s`: **fast** < 3600 s, **med** 3600–86400 s, **holder** > 86400 s, **unknown** (no screen); (b) **discovery_source** (`memecoin_wallets.discovery_source`); (c) **arm** (`memecoin_positions.strategy`).

## Metric & test

- Per cell: `n`, mean `realized_pnl`, median, win-rate, and a simple bootstrap 95% CI on the mean (positions treated as independent; note clustering by mint/trader is a known limitation — report n_distinct_traders per cell).
- Primary comparison: mean copy-PnL **fast vs holder within Arm A**; secondary within Arm B and control.
- Covariate check (H2): mean copy-PnL across discovery_source, and within each hold band.

## Gates (do not read before ALL met)

1. **≥ 30 closed positions per hold band** (fast / med / holder) overall, AND
2. **≥ 20 closed AND ≥ 5 distinct traders per (band × Arm-A) cell** (fast|momentum_a, holder|momentum_a) — the trader floor guards the clustering (20 positions from 2 traders is not 20 independent observations), AND
3. **calendar gate:** not before **2026-07-21** (2 weeks of forward accrual) — whichever is later.

Reading before the gates is a protocol violation (the n=19 rushed-verdict lesson). The read-out harness may be RUN anytime to monitor n toward the gates, but **verdicts are only formed once gates pass**.

## Decision rule (pre-committed)

- **H1 accepted** iff, within Arm A, fast-band and holder-band mean copy-PnL have **non-overlapping trader-clustered 95% CIs** (executed by `decision()`, not eyeballed) → route selection toward the winning band for the momentum arm.
- **H2 rejected** (pool matters) iff the pre-committed `leaderboard`-vs-`large_buy`-within-`fast` CIs are non-overlapping → reopen pool as a selection axis. Wider source×band differences are exploratory only.
- Null results are a valid, recorded outcome (fast≈holder → hold band is not a useful selection axis either; the edge is elsewhere — likely the post-buy-pump signal from build A).

## Read-out

`sniper/analysis/memecoin_holdband.hold_band_readout(conn)` returns the by-band / by-source / by-(band×arm) aggregations + n counts + gate status. It computes numbers only; interpretation waits for the gates. Runs off already-recorded data (arms, analytics hold, `discovery_source` provenance wired 2026-07-07) — no new capture needed.
