# Memecoin Fast v5 Design

**Date:** 2026-07-25
**Status:** implemented; activation and promotion remain gated
**Supersedes:** the assumption that v5 is a complete engine in
`docs/research/2026-07-25-exit-flex-v5-prereg.md`

## Goal

Build one pure v5 exit policy that can run on every Solana hot tick and on the 30-second fallback
without semantic drift. The existing funded envelope, exact-quote confirmation, one-shot claim,
signed-transaction identity, concurrency bound, and executor mappings remain unchanged.

## Current truth

- `HotPositionEngine` calls `propose_v3()` directly.
- `real_exit_sweep()` calls `propose_v3()` directly.
- `exit_flex_v5.shadow_comparison()` changes only the displayed partial fraction. It reports a
  per-band duration but does not use that duration to produce a decision.
- The hot lane is default OFF. v5 is not selectable as authority.

## Policy

`propose_v5(contract, state, cfg) -> CandidateDecision` is pure. It preserves v3 precedence and
reason mappings:

1. `CUT` rules are byte-identical to v3.
2. `TAKE_PROFIT` keeps the v3 trigger and replaces the fixed `0.5` fraction with the registered
   conviction fraction in `[0.25, 0.75]`. Strength is the parameter-free saturating ratio
   `x / (1 + x)`, where `x = realized_vol / trail_distance`; this avoids clamping every eligible
   structure action to the same 0.75 fraction.
3. `DERISK` keeps the v3 lower-high/vol-expansion trigger, uses the conviction fraction, and replaces
   the uniform 10,800-second edge-duration threshold with the registered median for `coin_band`.
4. `RIDE` and `DEFER_TO_LEGACY` keep v3 actions and reason codes when no v5 rule changes the result;
   no-action evidence names the engine that actually decided.

The proposer reuses one internal policy implementation shared with v3. v3 passes its fixed fraction
and 10,800-second threshold; v5 passes the registered fraction function and band threshold. Copying
the v3 rule tree into a second module is forbidden.

## Selection and data flow

`MEMECOIN_EXIT_ENGINE_VERSION` accepts only `v3` or `v5` and defaults to `v3`. A single selector
returns both the proposer and immutable engine version. The same selector is used by:

- the 30-second materializer and funded authoritative adapter;
- `HotPositionEngine` on every tick;
- the hot-book history warm-up;
- replay/backtest entry points that claim live-policy parity.

The selector applies only when a new funded engine-envelope contract is reserved. That contract
freezes `decision_engine_version`; existing engine-envelope contracts without the field parse as
v3. A process-level switch therefore cannot upgrade an open funded position in place.

The hot contract includes `coin_band`, `opened_at`, and the existing frozen policy fields. `held_s`
is refreshed from corrected time before each decision rather than frozen when the book is built.
Funded band names are translated to the measured v5 market-cap cohorts by interval containment:
`assumed_nano`/`nano`/`micro`/`small` → `<1M`, and funded `mid` → `1–5M`.
No episode, regime, wallet-graph, or other shadow context enters the proposer.

## Horizon windows

Hot features remain time-windowed, not block-count-windowed. Their 300-second volatility and
60-second flow/arrival windows are expressed from registered horizon units and elapsed event time,
so block-cadence drift cannot rescale the signal. The block conversion helper remains research-only
and is not introduced into the live hot path.

## Safety and activation

- Both `MEMECOIN_HOT_EXIT_ENABLED` and v5 authority remain default OFF by configuration: hot defaults
  off and engine selection defaults to v3.
- Selecting v5 does not bypass the chain allowlist, five-position limit, engine-envelope policy,
  state identity/freshness checks, exact preview, claims, or executor reason mapping.
- An explicit value other than `v3` or `v5` fails startup validation; an omitted value selects v3.
- The existing v5 promotion gate still applies: no operator activation until the preregistered
  `n >= 30` divergent-decision comparison passes. Building the selectable path is not promotion.
- No `.env` edit, deployment, restart, RPC call, or production DB mutation belongs to this change.

## Evidence and tests

Required regressions:

- v3 output remains identical across its frozen rule matrix.
- v5 CUT/RIDE/DEFER actions and reasons match v3 when no registered v5 change applies.
- v5 partial fractions are bounded and change both DERISK and TAKE_PROFIT decisions.
- per-band elapsed thresholds can DERISK nano/small positions before v3 while respecting the
  one-shot guard.
- hot history warm-up and new ticks use v5 when selected.
- hot and fallback paths produce the same decision for the same contract and state.
- invalid selection fails closed; default selection remains v3.
- materialized evidence rows name the engine that actually decided and retain one-row state/outcome
  linkage.

Verification is focused: changed unit/integration suites, crash-window regressions, schema reference
validation, `git diff --check`, and a deterministic same-tape replay comparison. No full suite.
