# Memecoin Learning Loop Design

**Date:** 2026-07-17
**Status:** Approved by operator directive
**Authority:** Measurement, read-only recommendations, shadow signals, and paper-policy evaluation. No automatic funded gate, wallet-arming, sizing, TP, SL, mirror, or strategy-selection change.

## Goal

Close the loop between signals observed, entries attempted or missed, execution quality, wallet quality, and exit-policy outcomes. The system must explain what happened, grade it from forward evidence, and expose the next decision without weakening funded safety.

## Non-negotiable contracts

- Coin identity is `(network_id, mint)`. Robinhood is network `4663`, never generic EVM.
- `global_feed` and `follow_activity` remain separate in every report.
- Claimed source time remains labeled untrusted. Stage time uses the reconciled local monotonic trace.
- No new funded-entry network request. Blind capture may use the existing single GeckoTerminal batch more efficiently, but may not increase requests per pass.
- No automatic wallet arming, funded entry gate, dynamic funded policy, or protected-hold promotion.
- Safety checks, exact-amount approvals, atomic reservations, one-position-per-coin, kill switch, and duplicate-submit protection remain authoritative.
- Missing data stays `NULL`/unknown. It is never rendered or scored as zero.
- Research and operator projections are read-only. Shadow-signal writes run after capture, outside the funded entry path.

## 1. Robinhood outcome capture

The current blind worker requests only 8 addresses although the existing GeckoTerminal client and endpoint support 30 addresses in one request. The worker will use all 30 slots without increasing request count.

Blind cohort ordering becomes deadline-aware:

1. active real positions;
2. coins with pending horizons from signals that were real-approved at observation time;
3. other pending signal horizons, earliest deadline first;
4. candidate-only cohorts.

One captured quote remains reusable across distinct event outcomes for the same `(network_id, mint)`. Within a single outcome, one quote may still satisfy at most one horizon. Coverage reports distinguish eligible-signal coverage from all-firehose coverage.

## 2. Radar decision facts

The immutable attempt rating remains untouched. A refusal that never reached a quote must not be rewritten as if it did.

The read-only radar projection adds current observable facts from existing tables:

- latest executable-observation price and age;
- quote quality and source;
- liquidity, market cap, and volume when known;
- route state: `blocked`, `observed`, or `unknown`;
- exact source wallet and chain-specific paper evidence;
- explicit attribution completeness.

The UI uses a flight-recorder strip: signal, our decision, latest quote, and forward outcome. Attempt truth and later observation truth are visually distinct.

## 3. Deterministic missed-entry grader

The grader is read-only and returns one primary row per `(network_id, mint, episode_id)`. It:

- selects the first terminal real decision in the episode;
- excludes episodes that later received a confirmed real fill;
- joins the exact upstream event to `memecoin_signal_outcomes_v2`;
- reports coverage before reporting performance;
- computes MFE, final observed return, hit-20, hit-50, and a labeled `diagnostic_60_60_v1` first-touch simulation on a $50 clip;
- aggregates avoided loss and forgone gain by terminal reason, chain, and source lane;
- never weights repeated refusals as separate opportunities.

Rows without usable forward marks remain `coverage_missing`; they cannot influence avoided/forgone totals.

## 4. EVM execution substages

`EntryAttemptTrace` gains monotonic substage events stored inside the existing `trace_json`. Substage marks do not change the existing top-level stage durations.

Measured substages include:

- token-safety completion;
- initial quote completion;
- balance-read completion;
- approval completion;
- post-approval quote completion;
- pre-transaction completion;
- swap signing;
- broadcast completion;
- confirmation completion;
- balance-delta completion;
- persistence completion.

A read-only latency report returns count, p50, and p95 by chain, terminal status, and substage. No optimization is promoted from fewer than five samples; thin rows are labeled.

## 5. Shadow self-signals

`memecoin_shadow_signals` is an immutable, paper-only ledger with at most one row per episode. It is evaluated after quote capture and never called from `real_entry_hook`.

Version 1 uses only facts already observed locally:

- first episode buy and exact network;
- point-in-time `was_real_approved`;
- claimed source lag under 60 seconds, labeled untrusted;
- quote observed within 90 seconds;
- market-cap band (`<1M` preferred, `1-5M` watch, `>=30M` reject);
- episode run-up below the existing chase cap;
- known liquidity when available; unknown liquidity is a warning, not a fabricated failure;
- chain-specific wallet evidence as a feature;
- consensus at signal time as a confidence fact, never a delayed trigger.

Outputs are `candidate`, `watch`, or `reject`. They are shadow labels, not funded commands. Each row stores canonical facts JSON and a hash.

## 6. Wallet arming recommender

The recommender is read-only. It compares exact-event matched paper closes for `control`, `real_shadow`, and `scalp_c2` by wallet and chain.

It reports:

- matched sample count;
- PnL, win rate, median PnL, and realized-series max drawdown per arm;
- PnL after dropping the two best winners;
- event overlap across arms;
- `promote`, `watch`, or `avoid` with deterministic reason codes.

`promote` requires mature, positive `control` and `real_shadow` evidence, positive drop-top-two results, and sufficient exact-event overlap. C2 is supporting evidence, not wallet discovery. The endpoint never calls `set_wallet_real_approved`.

## 7. Exit-policy tournament and dynamic readiness

The tournament is read-only and pairs closed positions by exact `origin_event_id`. Legacy mint/time matching is reported separately and excluded from the primary verdict.

It compares `control`, `real_shadow`, `scalp_c2`, and `peak_lock_e`, with `protected_hold_shadow` shown as an evidence counter rather than invented PnL. Results are segmented by chain, market-cap band, entry-lag band, and consensus band.

Each segment reports:

- paired closes and exact-attribution rate;
- PnL, win rate, median, worst result, and drop-top-two PnL per arm;
- pairwise delta versus control;
- winner-swallow count;
- readiness: `insufficient`, `shadow_candidate`, or `rejected`.

`shadow_candidate` requires at least 30 paired closes, positive delta versus control, positive drop-top-two delta, and no material winner-swallow failure. It authorizes only a new paper/shadow candidate. Funded dynamic deployment requires a separate approved spec.

## Operator surface

The existing Memecoin Radar and Performance workspaces gain read-only sections for:

- capture coverage by chain and eligibility;
- missed-entry reasons and counterfactuals;
- EVM substage latency;
- recent shadow self-signals;
- wallet recommendations;
- exit-policy tournament readiness.

The visual language follows the existing operator-cockpit tokens. The signature element is the flight-recorder strip: one horizontal evidence sequence per coin, with observed facts, decisions, and later outcomes separated by clock and authority.

## Acceptance

1. A 30-address 4663 batch still makes at most one GT request per pass.
2. Eligible pending horizons outrank candidate-only cohorts.
3. Cross-event quote reuse works without allowing same-outcome horizon reuse.
4. Radar later-observation facts never mutate immutable attempt ratings.
5. Missed-entry totals are episode-deduped and exclude later-filled episodes.
6. Substage timestamps are monotonic and do not perturb top-level stage durations.
7. Shadow self-signals are one-per-episode, exact-chain, immutable, and off the funded path.
8. Wallet recommendations are chain-aware, exact-event paired, and read-only.
9. Tournament readiness uses paired forward closes and drop-top-two checks.
10. No new automatic funded behavior or hot-path network call.
