# Polymarket Crypto5m Maker Regime State Evaluator Design

**Date:** 2026-07-05
**Status:** Phase 1 implemented; later evaluation/shadow phases remain staged
**Supersedes:** `docs/superpowers/specs/2026-07-04-polymarket-crypto5m-maker-regime-state-evaluator-design.md`
**Scope:** paper-only state, episode, profile-decision, and evaluator layer for the active crypto5m maker.

## Decision

Build a stateful evaluator around the current maker regime router.

Current engine state:

- `maker-regime-v1` is already the active paper campaign.
- `maker_regime.py` classifies the current tick and emits policy-like quote overrides.
- The active router can switch regimes each poll, but it is still mostly stateless.

Phase 1 does not change live paper quote behavior beyond attaching regime/profile attribution. Profile decisions run in `log_only` beside the active `maker-regime-v1` campaign.

The core architecture is:

```text
raw tick state
-> market/window episode
-> side/token profile decision
-> quote/fill attribution
-> episode outcome evaluation
```

The key model is:

```text
market-level regime episodes
+
side-level profile decisions
+
episode-level evaluation
```

not:

```text
per-token regime state machine
```

## Problem

The current router answers:

```text
What state is this tick?
```

The missing layer must answer:

```text
Is this state stable enough to trust?
What state did we come from?
Which quote profile did each side use?
Did that profile work in this kind of episode?
Did a quote created in one regime get filled after a worse transition?
```

Without episode memory, raw tick labels can misattribute losses. Example:

```text
quote created during mid_chop
market jumps 4 seconds later
quote fills during jump
```

That should be measured as quote-created context and fill-time context, not blamed blindly on one tick label.

## Non-Goals

- No live orders.
- No private keys.
- No production DB mutation.
- No service restart from this spec.
- No current quote-behavior change in Phase 1.
- No self-learning active switcher.
- No contextual bandit.
- No automatic disable or promote action.
- No backfill requirement for old v4, old shadow, or pre-evaluator rows.
- No pooling old campaign rows into `maker-regime-v1`.

## Core Architecture

Flow:

```text
maker_regime.py
  -> raw candidate regime from known-at-time tick state

maker_regime_state.py
  -> market/window-level stable episode
  -> transition record
  -> coarse moment key
  -> raw feature payload

maker_profile.py
  -> quote_tight
  -> quote_base
  -> quote_wide
  -> reduce_only
  -> pause
  -> exit_fast

maker_regime_eval.py
  -> quote/fill markouts
  -> suppressed quote labels
  -> per-context/profile stats
  -> human-review recommendations
```

Regime episode and profile decision are different objects:

```text
episode:
  condition_id = BTC 5m market
  regime = late_window
  moment = odds_high + volatility_jump + spread_wide

side decisions:
  Up token = reduce_only
  Down token = pause
```

## Module

Create:

```text
sniper/algos/polymarket_crypto5m/maker_regime_state.py
```

Keep pure compute isolated. Persistence should be a thin runner-owned layer.

Inputs:

- raw regime output from `maker_regime.py`;
- market snapshot;
- current inventory;
- current quote state;
- current fill state;
- current time/window state.

Outputs:

- `RegimeEpisodeState`;
- `ProfileDecision`;
- `TransitionEvent`;
- coarse `moment_key`;
- raw `moment_features_json`.

## Market-Level Episode State

Track per market/window:

```text
episode_id
condition_id
window_start_ts
window_end_ts
current_regime
previous_regime
candidate_regime
candidate_since_ts
entered_ts
last_seen_ts
dwell_seconds
transition_reason
transition_confidence
regime_confidence
window_phase
odds_bucket
volatility_state
book_state
spread_state
direction_state
inventory_state
```

Required events:

```text
episode_start
episode_update
episode_end
transition
window_close
restart_resume
```

The state machine is market/window-level. It is not per token.

## Side-Level Profile Decisions

Track profile decisions separately per side/token:

```text
profile_decision_id
episode_id
token_id
outcome
side
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

The side decision can differ inside the same episode because inventory and exposure differ by token.

Example:

```text
market episode = inventory_saturated
Up token profile = quote_wide
Down token profile = reduce_only
```

## Transition Rules

Use priority plus hysteresis.

Emergency states switch immediately:

```text
hard_invalid
late_window
book_quarantined
settlement_conflict
inventory_saturated
```

Opportunity states require debounce:

```text
mid_chop
normal
trend_confirmed
jump
```

Suggested defaults:

```text
transition_debounce_ticks = 3
transition_debounce_ms = 3000
min_regime_dwell_ms = 5000
```

Safety regimes ignore minimum dwell. A hard-risk state must be able to pull quotes immediately.

## Regime Precedence

Use first-match precedence for candidate generation:

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

Rules:

- `hard_invalid` beats everything.
- `late_window` beats `jump` and `trend_confirmed`.
- `inventory_saturated` beats opportunity states.
- `jump` can interrupt `mid_chop`.
- `mid_chop` must be stable before becoming active.
- `normal` is fallback.

## Moment Definition

A moment is the learnable context bucket. It must be coarse.

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

Store raw values separately:

```text
moment_features_json = {
  fair,
  market_bid,
  market_ask,
  seconds_remaining,
  spread_price,
  spread_ticks,
  spread_bps_display,
  vol_30s,
  jump_score,
  direction_score,
  inventory_shares,
  gross_inventory,
  net_directional_inventory,
  complete_sets,
  book_age_ms,
  depth_at_quote,
  last_fill_age_ms,
  source_snapshot_hash
}
```

The key is for stable scoring. Raw features are for later research.

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

Thresholds belong in config and must be versioned.

## Policy Profiles

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

Each profile returns overrides against the existing base maker policy:

```text
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

Profile intent:

- `quote_tight`: only for healthy, stable, low-risk contexts.
- `quote_base`: default maker behavior.
- `quote_wide`: smaller and wider for uncertainty or pickoff risk.
- `reduce_only`: no inventory-increasing quote.
- `pause`: no quotes.
- `exit_fast`: exit-only, short TTL, may hard-cut if allowed.

## Profile Modes

Support:

```text
log_only
shadow
active
```

Phase 1 default:

```text
log_only
```

Mode meanings:

- `log_only`: record profile that would have been selected; no quote impact.
- `shadow`: simulate side decisions and fills where feasible; no live paper quote impact.
- `active`: affects paper quotes; not part of this evaluator Phase 1.

The current `maker-regime-v1` active router stays as-is. The evaluator logs beside it.

## Quote And Fill Attribution

Every quote/fill/skip after enablement must carry episode and profile context.

Quote rows:

```text
quote_id
episode_id
regime_id
moment_schema_version
moment_key
profile_id
profile_mode
profile_decision_id
policy_hash
quote_created_ts
quote_created_regime_id
quote_created_episode_id
```

Fill rows:

```text
quote_id
quote_episode_id
fill_episode_id
quote_regime_id
fill_regime_id
quote_profile_id
fill_profile_id
fill_ts
```

Skips or suppressed opportunities:

```text
episode_id
regime_id
moment_key
profile_id
profile_mode
suppressed_reason
counterfactual_quality
```

This distinction is mandatory:

```text
quote_created_regime_id != fill_regime_id
```

That difference is how we detect stale quote pickoffs after transitions.

## Pending Quote Transition Actions

When the regime transitions, record intended action for outstanding quotes:

```text
keep
cancel
reduce
exit_only
hard_pull
```

Even if the paper engine only models quote replacement at poll cadence, the intended action must be logged.

Reason:

```text
transition says cancel, quote later fills
```

is direct evidence of transition/poll latency risk.

## Episode Evaluation

Score maker-friendliness, not prediction accuracy.

Per episode, moment, and profile:

```text
quote_count
fill_count
fill_rate
spread_captured_bps
adverse_selection_3s_bps
adverse_selection_10s_bps
adverse_selection_30s_bps
future_executable_unwind_3s
future_executable_unwind_10s
future_executable_unwind_30s
hard_cut_count
inventory_carry_time_ms
max_inventory_abs
realized_window_pnl
MFE
MAE
quote_missed_opportunity
suppressed_quote_counterfactual
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
fee_adjusted_pnl
realized_resolution_pnl
```

Economic labels outrank diagnostic labels. Do not promote a profile based only on microprice-marked edge.

## Suppressed Quote Counterfactual

When a profile says:

```text
pause
reduce_only
exit_fast
```

record suppressed quote opportunities.

Later classify:

```text
would_quote_have_been_fillable
would_fill_have_been_adverse
would_fill_have_been_good
unknown
```

This label is not realized PnL. Keep it separate from actual fills.

## Persistence

Add dedicated logical tables or equivalent existing-table extensions:

```text
crypto5m_maker_regime_episodes
crypto5m_maker_regime_transitions
crypto5m_maker_moments
crypto5m_maker_profile_decisions
crypto5m_maker_quote_evaluations
crypto5m_maker_context_profile_stats
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
transition_confidence
regime_confidence
moment_schema_version
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

Profile-decision fields:

```text
profile_decision_id
episode_id
token_id
outcome
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

Rules:

- Episodes are append-only except closing the open episode.
- Transitions are append-only.
- Profile decisions are append-only.
- Moment keys are schema-versioned.
- JSON payloads use deterministic key ordering before hashing.
- New evaluator rows start from enablement forward. No old-row backfill.
- Campaign boundaries stay strict.

## Learning Layer

Start deterministic. No contextual bandit.

Rolling stats by:

```text
moment_key
profile_id
asset
campaign
```

For this engine, use:

```text
asset = btc
```

Do not split samples further by asset unless this engine expands beyond BTC.

Stats:

```text
n_windows
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

Sample gates:

```text
min_windows = 20
min_episodes = 30
min_fills = 50
```

Use window-clustered uncertainty so one busy window cannot dominate.

## Disable And Promote Recommendations

Evaluator can recommend:

```text
disable profile in context
promote profile in context
keep_collecting
needs_review
```

It must not apply changes automatically.

Example disable:

```text
n_fills >= 50
and adverse_selection_10s_bps materially negative
and fee_adjusted_pnl lower confidence bound < 0
-> disabled_recommended
```

Example promote:

```text
n_fills >= 50
and profile beats current baseline
and adverse_selection acceptable
and result not concentrated in one window
-> promote_recommended
```

## API And UI

Keep UI small.

Expose compact fields on the existing maker surface:

```text
current_episode_id
current_regime
dwell_seconds
moment_key
selected_profiles_by_token
profile_mode
last_transition
per_context_profile_status
```

Do not build a dashboard before episode tables and evaluation labels exist.

## Implementation Phases

### Phase 1: state machine and episode tables

- add `maker_regime_state.py`;
- add episode and transition persistence;
- create market/window episodes;
- create side/token profile decisions in `log_only`;
- attach episode/profile IDs to new quote/fill/skip rows;
- no quote-behavior change.

### Phase 2: quote and fill evaluation labels

- quote-created vs fill-time regime attribution;
- adverse selection;
- spread capture;
- executable unwind;
- MFE/MAE;
- suppressed quote counterfactual;
- missed opportunity.

### Phase 3: per-context score report

- context/profile stats;
- sample gates;
- window-clustered uncertainty;
- disable/promote recommendations.

### Phase 4: shadow profile selector

- profile side-by-side comparison;
- shadow-quality labels;
- no active cutover.

### Phase 5: active policy changes only after review

- separate campaign or explicit current-campaign amendment;
- human approval;
- no auto-learning.

## Tests

State machine:

- starts first episode;
- extends same regime;
- debounces noisy candidate flip;
- emergency `hard_invalid` interrupts immediately;
- `late_window` interrupts `jump`;
- `inventory_saturated` interrupts `trend_confirmed`;
- window close ends episode;
- restart/resume restores active episode;
- dwell time correct;
- transition reason recorded.

Moment key:

- stable coarse key construction;
- raw features not embedded in key;
- schema version included;
- odds bucket boundaries;
- inventory state boundaries;
- spread/book/direction state mapping.

Profiles:

- profile object complete;
- profile hash deterministic;
- `quote_tight` modifies base policy;
- `quote_wide` modifies base policy;
- `reduce_only` disables inventory-increasing side;
- `pause` disables both sides;
- `exit_fast` is exit-only.

Logging:

- quote row has `episode_id`, `regime_id`, `profile_id`;
- fill row has `quote_episode_id` and `fill_episode_id`;
- skip row has `episode_id`, `regime_id`, `profile_id`;
- profile decisions append-only;
- no missing moment key.

Evaluation:

- spread capture;
- adverse selection markouts;
- executable unwind markouts;
- MFE/MAE;
- inventory carry time;
- suppressed quote counterfactual;
- per-context/profile stats;
- window-clustered aggregation;
- disable recommendation;
- promote recommendation.

Scope guards:

- no live orders;
- no private keys;
- no production DB;
- no active profile switching from Phase 1;
- old campaigns untouched;
- no backfill required.

## Risks

Too much state can hide fast market changes.

Mitigation:

```text
safety regimes interrupt immediately
opportunity regimes debounce
minimum dwell does not apply to hard-risk states
```

Too granular a context key can make every bucket too sparse.

Mitigation:

```text
coarse moment_key
raw features stored separately
sample gates before recommendations
```

Quote-created and fill-time context can diverge.

Mitigation:

```text
store both quote_episode_id and fill_episode_id
evaluate stale quote fills explicitly
```

## Open Questions

No blocking design questions.

Implementation must choose exact threshold defaults for:

- window phase boundaries;
- odds buckets;
- spread health;
- inventory saturation;
- volatility/jump state;
- direction confidence;
- book freshness.

Defaults must be config-backed and versioned.

## Acceptance Bar

The first build passes when:

- raw tick regimes become durable market/window episodes;
- each episode has a coarse moment key and raw feature payload;
- side/token profile decisions are recorded in `log_only`;
- quotes/fills/skips can join back to episode, moment, and profile;
- quote-created context and fill-time context are both preserved;
- evaluation separates actual realized outcomes from counterfactual suppressed quotes;
- per-context/profile recommendations are human-review only.

The correct first build is:

```text
state machine + episode tables + log_only profile decisions
```

not:

```text
self-learning active switcher
```
