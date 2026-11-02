# HL Alt-Short Regime Replication Design

**Date:** 2026-07-26
**Status:** Approved for paper-only implementation
**Scope:** Standalone HL exhaustion study, forward-paper arm, and reporting only

## Problem

The sealed v2 study and current forward gate can promote after enough trades and days from one continuous bearish tape. That repeats the study's main limitation instead of testing it. The historical W2 result is one bearish episode, so `paper_promote` overstates the machine conclusion even though the memo discloses the limitation.

The historical study also chooses its exit clock from hourly OHLC threshold touches while the forward arm observes sampled marks. The v2 causal return uses next-open entry and exit prices, but its exit clock can still be earlier than a sampled observer could reproduce.

## Decision

Keep the v2 files and hash immutable. Add a v3 interpretation and forward gate with regime-episode replication. A single bearish episode is always `paper_continue`, regardless of trade or day count.

Do not require profitable non-bearish trades. The strategy is defined to stand down outside bearish regimes. The missing proof is replication across distinct bearish episodes.

## Regime observations and episodes

The scanner persists one causal regime observation for every processed closed hour, including hours with no run-up crossing. Identity is `(arm_id, decision_ts_ms)`; replay with different content is an error. Pre-arm observations are rejected.

An observation contains the full causal regime payload already computed by `causal_basket_regime`.

Episode assignment is deterministic:

- The first valid bearish hour starts episode 1.
- Consecutive bearish hours remain in that episode.
- A new episode requires at least 24 consecutive valid non-bearish hours (`neutral` or `bullish`) before the next bearish hour.
- `unknown` is not evidence of separation and resets the separator streak.
- A bearish trade stores the current `episode_id` in its immutable `regime_json`.

The 24-hour separator is an operational independence proxy, not proof of statistical independence. Reports must say so.

## Promotion contract

All existing provenance, timing, coverage, day-cluster, cost, residual-CI, concentration, portfolio, and exit-rule gates remain.

Promotion additionally requires:

- at least 2 distinct bearish episodes;
- at least 5 valid closed trades in every counted episode;
- positive mean matched-basket residual in every episode; and
- positive leave-one-episode-out residual mean.

Before episode/sample completeness, the result is `paper_continue`. After completeness, a failed episode residual check is `paper_reject`.

The forward report exposes episode starts, closes, residual means, and leave-one-episode-out results. Raw regime hours are retained so the episode calculation is auditable.

## Historical v3 interpretation

The W2 evidence remains numerically unchanged. The v3 report records one qualifying bearish episode and therefore returns `paper_continue`. It leads with matched-basket residual, not the capital-unmatched passive comparison.

Sensitivity is explicit: the pre-registered 30%/72h cell is positive, while nearby cells are non-monotone and one 20%/72h cell is near zero. This narrows the claim to a candidate threshold, not a general exhaustion law.

## Exit-observation bridge

Add a cache-only diagnostic that replays the frozen events with hourly-close observations:

- entry remains next tradeable open after the decision close;
- stop/take-profit are evaluated on hourly closes only;
- the 8-hour cap uses the observed close;
- the matched leave-one-out basket exits on the same sampled clock;
- costs and funding stay identical.

This is a conservative coarse bridge between OHLC-clock backtest and sampled forward execution. It is not a replay of the live scanner's roughly 30-second mark stream and cannot validate live execution by itself.

## Provenance and reporting corrections

- Study cleanliness is scoped to the pinned study code paths, matching the runtime pin logic. Unrelated tracked service writes do not invalidate a clean recompute.
- A window with zero eligible coins is `unknown`, never `neutral`.
- The passive full-period short is descriptive only because its exposure profile differs from the intermittent fixed-slot strategy. It cannot support a `2.33x with less drawdown` headline.

## Safety

No SDK, order, key, funded runner, production service, or real-leg behavior changes. The arm remains paper-only. Any future funded promotion remains an operator action outside this design.
