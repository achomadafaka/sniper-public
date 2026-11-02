# Maker Regime State + Moment Evaluator v1

**Date:** 2026-07-04
**Status:** design approved, not implemented
**Scope:** paper-only state/evaluation layer for the Polymarket crypto 5m maker
**Primary principle:** we are not trying to classify regimes. We are trying to learn which quote profile is maker-friendly in which moment.

---

## Decision

The next build is not another static maker-knob router.

The core object is:

```text
context moment + quote profile -> maker outcome
```

not:

```text
regime -> policy
```

Current `maker_regime.py` may still produce a raw tick-level candidate. This spec adds the missing memory and evaluation layer around it:

- stable regime episodes,
- transition and dwell tracking,
- coarse learnable moment keys,
- explicit quote profiles,
- profile decisions in `log_only` or `shadow`,
- post-fill and suppressed-quote evaluation labels,
- per-context/profile recommendations.

No active self-learning switcher is allowed in v1. No live orders. No replacement of the current maker from this spec alone.

## Problem

The current regime router classifies the current tick, but it has no memory and no outcome attribution.

It cannot answer:

- how long we have been in this regime;
- whether this is a stable state or one noisy tick;
- what state we transitioned from;
- whether fills in this state were favorable;
- whether pulling quotes avoided bad fills;
- which quote profile would have been better;
- whether one bad context/profile pair is poisoning the whole maker engine.

The system needs to stop asking only:

```text
What regime is this?
```

and start answering:

```text
In this moment, which maker behavior actually works?
```

## Non-Goals

- Do not build a live trading system.
- Do not add private keys, authenticated order endpoints, or live-order code.
- Do not mutate a production DB.
- Do not replace the current maker campaign.
- Do not merge evaluation results into the current campaign without a new campaign namespace.
- Do not auto-disable or auto-promote a quote profile.
- Do not build a contextual bandit or self-learning active switcher.
- Do not rank profiles from weak shadow-quality labels.
- Do not let raw continuous values explode the context key.

## Architecture

The data flow is:

```text
maker_regime.py
  -> raw regime candidate

maker_regime_state.py
  -> debounced/stable regime episode
  -> transition record
  -> context key / moment key

maker_profile.py
  -> quote_tight / quote_base / quote_wide / reduce_only / pause / exit_fast

maker_regime_eval.py
  -> post-fill markouts
  -> per-context/profile stats
  -> disable/promote recommendations
```

`maker_regime.py` remains pure tick classification. It should not own dwell, transitions, learning, or promotion decisions.

`maker_regime_state.py` owns episode state. It converts raw tick candidates into stable episodes and moment keys.

`maker_profile.py` owns profile definitions. Profiles modify the existing maker base policy through bounded multipliers and side enablement.

`maker_regime_eval.py` owns labels, context/profile stats, and human-review recommendations.

## Regime State Machine

Create:

```text
sniper/algos/polymarket_crypto5m/maker_regime_state.py
```

Keep pure compute isolated where possible. Persistence is a thin caller-owned layer.

Inputs:

- raw router output from `maker_regime.py`;
- current market snapshot;
- inventory state;
- quote/fill state;
- time/window state.

Track:

```text
current_regime
previous_regime
entered_ts
last_seen_ts
dwell_seconds
transition_reason
transition_confidence
regime_confidence
window_phase
inventory_state
odds_bucket
volatility_state
book_state
spread_state
direction_state
```

Required state events:

```text
episode_start
episode_update
episode_end
transition
window_close
restart_resume
```

Hard-risk states may interrupt immediately:

```text
hard_invalid
late_window
book_quarantined
settlement_conflict
```

Normal opportunity states require debounce/hysteresis:

```text
candidate must persist for N ticks or M milliseconds
```

Config fields:

```text
min_regime_dwell_ms
transition_debounce_ms
emergency_transition_reasons
```

State must survive process restart enough to resume the current open episode without double-counting dwell or transition records.

## Stateful Precedence

Use first-match precedence for raw candidate priority:

```text
hard_invalid
late_window
extreme_odds
inventory_saturated
jump
trend_confirmed
mid_chop
normal
```

This order decides the episode label, not whether the engine is allowed to add risk. Inventory, spread, book, direction, and odds still remain in the moment key and profile decision.

Examples:

- `hard_invalid` beats everything and pulls all quotes.
- `late_window` beats `jump`.
- `inventory_saturated` beats `trend_confirmed`.
- `mid_chop` must persist before it becomes the stable episode.
- A lower-priority opportunity regime cannot flip the state on a single tick.

## Moment Definition

A moment is not just `regime_id`.

It is a coarse feature bucket designed to be learnable:

```text
moment_schema_version = 1

moment_key = {
  window_phase,
  odds_bucket,
  volatility_state,
  inventory_state,
  spread_state,
  direction_state,
  book_health,
}
```

Do not include raw continuous values directly in the key.

Store full raw detail beside it:

```text
moment_features_json = {
  fair,
  market_bid,
  market_ask,
  seconds_remaining,
  vol_30s,
  spread_price,
  spread_bps_display,
  spread_ticks,
  inventory_shares,
  gross_inventory,
  net_directional_inventory,
  complete_sets,
  jump_score,
  direction_score,
  book_age_ms,
  depth_at_quote,
  last_fill_age_ms,
  source_snapshot_hash
}
```

The key is for stable scoring. The raw features are for future research.

## Moment Buckets

`window_phase`:

```text
early_window
mid_window
late_window
final_seconds
```

`odds_bucket`:

```text
extreme_low
low
mid_chop
high
extreme_high
```

`volatility_state`:

```text
calm
elevated
jump
unknown
```

`inventory_state`:

```text
flat
long_light
long_saturated
short_light
short_saturated
```

`spread_state`:

```text
healthy
wide
untradeable
unknown
```

`direction_state`:

```text
unconfirmed
confirmed_up
confirmed_down
conflicted
```

`book_health`:

```text
healthy
stale
quarantined
missing
```

Bucket boundaries belong in config and must be logged with the policy/version hash. Suggested defaults can start conservative, but the spec requires the bucket names and versioning, not exact final thresholds.

## Quote Profiles

Separate detection from action.

Profiles:

```text
quote_tight
quote_base
quote_wide
reduce_only
pause
exit_fast
```

Each profile defines bounded overrides:

```text
profile_id
margin_multiplier
margin_add
clip_multiplier
max_shares_multiplier
min_edge
min_seconds_quote
quote_ttl_ms
bid_enabled
ask_enabled
exit_only
hard_cut
reason
warnings_json
```

For binary-token quote controls, authoritative units are token-price units and ticks, not bps. Bps-style fields may exist only as derived display/diagnostic values.

The existing maker policy remains the base. Profiles modify it. The state machine does not directly set arbitrary quote prices, clips, or caps.

Profile intent:

- `quote_tight`: smallest safe edge, normal inventory, only for healthy/chop contexts.
- `quote_base`: current base maker behavior.
- `quote_wide`: wider quotes and smaller clip for uncertainty, volatility, or pickoff risk.
- `reduce_only`: no inventory-increasing side; quote only to reduce existing exposure.
- `pause`: no quotes; still record suppressed quote counterfactuals.
- `exit_fast`: exit-only, shorter TTL, may hard-cut if executable and policy allows it.

## Profile Decision Modes

Support:

```text
log_only
shadow
active
```

v1 default:

```text
log_only
```

Mode rules:

- `log_only`: record regime, moment, and profile that would be selected. No quote impact.
- `shadow`: compute quote decisions and counterfactual fills under alternative profiles. No impact on the live paper maker.
- `active`: use the selected profile for paper quotes. Out of scope for this spec unless separately approved.

`shadow` starts only after episode tables and profile-decision persistence work.

## Evaluation Goal

Score maker-friendliness, not prediction accuracy.

Per episode, moment, and profile, compute:

```text
quote_count
fill_count
fill_rate
spread_captured_bps
adverse_selection_3s_bps
adverse_selection_10s_bps
adverse_selection_30s_bps
executable_unwind_markout_3s
executable_unwind_markout_10s
executable_unwind_markout_30s
hard_cut_count
inventory_carry_time_ms
inventory_max_abs
realized_window_pnl
MFE
MAE
quote_missed_opportunity
pull_avoided_bad_fill
pickoff_loss_count
```

Diagnostic labels:

```text
future_mid_markout
future_microprice_markout
future_fair_markout
```

Economic labels:

```text
future_executable_unwind_value
realized_resolution_pnl
fee_adjusted_pnl
```

Economic labels outrank diagnostic labels. Do not promote a profile based only on microprice-marked edge.

## Suppressed Quote Counterfactual

When a profile says `pause` or `reduce_only`, record suppressed quote opportunities.

For a suppressed quote, later label:

```text
would_quote_have_been_fillable
would_fill_have_been_adverse
would_fill_have_been_good
unknown
```

Call this:

```text
suppressed_quote_counterfactual
```

This is not proof of PnL. It is a counterfactual label with quality limits. It must not be pooled with actual realized fills without a separate field.

## Persistence Contract

Add dedicated logical tables:

```text
crypto5m_maker_regime_episodes
crypto5m_maker_regime_transitions
crypto5m_maker_moments
crypto5m_maker_profile_decisions
crypto5m_maker_quote_evaluations
crypto5m_maker_context_profile_stats
```

Existing quote/fill/skip tables must record:

```text
episode_id
moment_key
moment_schema_version
regime_id
profile_id
profile_mode
policy_id
profile_decision_id
```

Episode fields:

```text
episode_id
campaign_id
condition_id
window_start_ts
window_end_ts
regime_id
previous_regime_id
started_ts
ended_ts
dwell_ms
transition_reason
regime_confidence
moment_key
moment_features_json
inventory_state
odds_bucket
volatility_state
book_state
spread_state
direction_state
source_snapshot_hash
```

Profile decision fields:

```text
profile_decision_id
episode_id
decision_ts
profile_id
profile_mode
base_policy_id
profile_policy_hash
bid_enabled
ask_enabled
exit_only
hard_cut
margin_used
clip_used
max_shares_used
min_edge_used
quote_ttl_ms
reason
warnings_json
```

Persistence rules:

- Episodes are append-only except closing the current open episode.
- Transitions are append-only.
- Profile decisions are append-only.
- Quote/fill/skip rows must never be missing episode/profile identifiers after the feature is enabled.
- Moment keys must be schema-versioned.
- Raw feature payloads must be JSON with deterministic key ordering before hashing.
- A campaign boundary must keep old v4 and any future evaluator campaign separate.

## Learning Layer

Start deterministic.

No contextual bandit. No automatic switching.

Maintain per-context/profile rolling stats:

```text
n_episodes
n_quotes
n_fills
mean_spread_capture
mean_adverse_selection_10s
mean_executable_markout_10s
mean_realized_pnl
confidence_interval
last_updated_ts
status
```

Statuses:

```text
insufficient_sample
candidate_good
candidate_bad
disabled_recommended
promote_recommended
needs_review
```

Minimum sample gates:

```text
min_episodes = 30
min_fills = 50
min_windows = 20
```

Use conservative confidence intervals or bootstrap by market window. Do not let one window with many fills dominate.

Aggregate and report at three grains:

```text
market window
episode
fill
```

Uncertainty must be clustered by window for campaign-level conclusions.

## Disable and Promote Recommendations

The evaluator may recommend:

```text
disable profile in context
promote profile in context
keep collecting
needs human review
```

It must not automatically change active policy.

Example disable rule:

```text
if n_fills >= 50
and adverse_selection_10s_bps materially negative
and fee_adjusted_pnl lower confidence bound < 0
then disabled_recommended
```

Example promote rule:

```text
if n_fills >= 50
and profile beats current shadow baseline
and adverse selection acceptable
and result not concentrated in one window
then promote_recommended
```

One bad context/profile pair should be disableable without killing the whole maker engine.

## Shadow Comparison

The main use of this system is to compare profiles.

For each moment, evaluate where feasible:

```text
current_v4_policy
quote_tight
quote_base
quote_wide
reduce_only
pause
exit_fast
```

Maker fills are path-dependent. Shadow results are approximations unless the shadow book and actual book path are equivalent.

Every shadow evaluation must record:

```text
shadow_quality:
  exact
  approximate
  not_evaluable
```

Do not rank profiles when `shadow_quality` is weak.

## API and UI

Do not build a major UI.

Add only compact inspection fields to the existing crypto5m maker surface after persistence exists:

```text
current_regime
dwell_time
moment_key
selected_profile
profile_mode
last_transition
per_regime_fill_pnl_stats
per_context_profile_status
```

The first API goal is auditability, not a dashboard.

## Tests

### State Machine

- starts first episode;
- extends same regime;
- debounces noisy candidate flip;
- emergency `hard_invalid` interrupts immediately;
- `late_window` interrupts `jump`;
- window close ends episode;
- restart resume restores current episode;
- dwell time is correct;
- transition reason is recorded.

### Moment Key

- stable coarse bucket construction;
- raw features are not embedded in key;
- schema version is included;
- odds bucket boundaries;
- inventory state boundaries;
- spread/book/direction state mapping.

### Profiles

- profile object complete;
- profile hash deterministic;
- `quote_tight` modifies base policy correctly;
- `quote_wide` modifies base policy correctly;
- `reduce_only` disables inventory-increasing side;
- `pause` disables both sides;
- `exit_fast` is exit-only.

### Logging

- quote row includes `episode_id`, `regime_id`, `profile_id`;
- fill row includes `episode_id`, `regime_id`, `profile_id`;
- skip row includes `episode_id`, `regime_id`, `profile_id`;
- profile decisions are append-only;
- no missing context key.

### Evaluation

- fill rate;
- spread capture;
- adverse selection markout;
- executable unwind markout;
- inventory carry time;
- suppressed quote counterfactual;
- per-context/profile stats;
- window-clustered aggregation;
- disable recommendation;
- promote recommendation.

### Scope Guards

- no live orders;
- no private keys;
- no production DB;
- no active profile switching;
- old campaign untouched.

## Implementation Phases

### Phase 1: state machine and episode tables

- write `maker_regime_state.py`;
- add episode and transition persistence;
- add current regime/moment/profile logging;
- no policy change.

### Phase 2: post-fill and quote evaluation labels

- adverse selection;
- spread capture;
- executable unwind;
- MFE/MAE;
- pull avoided bad fill;
- quote missed opportunity.

### Phase 3: per-context score report

- context/profile stats;
- minimum sample gates;
- disable/promote recommendations.

### Phase 4: shadow profile selector

- profile decision layer;
- shadow side-by-side;
- per-profile comparison.

### Phase 5: active cutover only after evidence

- separate campaign;
- explicit approval;
- no auto-learning.

## Open Questions

No blocking design questions.

Implementation must still choose and test initial bucket thresholds for:

- window phase boundaries;
- odds bucket boundaries;
- spread health;
- inventory saturation;
- volatility/jump thresholds;
- direction confidence thresholds.

Those thresholds should be config-backed and versioned. They should not be treated as learned truths in v1.

## Acceptance Bar

This spec is complete when it produces:

- a state machine that turns raw tick regimes into stable episodes;
- a coarse moment key plus raw feature payload;
- explicit profile decisions in `log_only` first;
- persistence that can join quotes/fills/skips back to episode, moment, and profile;
- evaluation labels that separate actual realized outcomes from counterfactual suppressed quotes;
- per-context/profile recommendations that require sample gates and human review.

The correct first build is:

```text
state machine + episode tables + evaluation labels
```

not:

```text
self-learning active switcher
```
