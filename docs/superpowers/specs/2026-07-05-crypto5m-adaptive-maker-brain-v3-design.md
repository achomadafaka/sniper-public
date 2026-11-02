# Crypto5m Adaptive Maker Brain V3 Design

**Date:** 2026-07-05
**Status:** Approved for paper-only implementation
**Scope:** new `maker-adaptive-paper-v3` campaign. V1/V2 history stays immutable.

## Decision

Build `Adaptive Maker Brain v3` as a paper-only maker decision system.

It learns:

```text
moment + inventory + fair uncertainty + quote action -> maker outcome
```

and separately tracks which fair-value estimate is trustworthy in the current moment.

## Hard Boundary

- No live orders.
- No private keys.
- No production DB.
- No mutation of V1/V2 campaign history.
- No real-money execution.
- No hidden online self-modification outside the V3 paper campaign.

Inside `maker-adaptive-paper-v3`, the engine may adapt paper quote decisions.

## Campaign

```text
campaign_id = maker-adaptive-paper-v3
learner_version = v3
selector_version = adaptive-maker-brain-v3
reward_version = maker-reward-v3
```

Control:

```text
maker-regime-v1
maker-adaptive-paper-v2
```

## Architecture

Flow:

```text
market tick
-> fair-value learner
-> regime / moment state
-> tail-risk governor
-> quote-action selector
-> paper quote/fill/skip
-> markout / settlement labels
-> online paper learner update
-> next decision
```

V3 is one system with shared state, not four unrelated engines.

Shared identifiers:

```text
moment_key
episode_id
fair_model_id
risk_state
quote_action_arm
reward_version
selector_version
campaign_id
```

## Fair-Value Learner

The learner chooses what probability to quote around.

Use logit blending:

```text
logit(q_used) =
    w_context * logit(q_structural)
  + (1 - w_context) * logit(q_market_mid)
  + residual_context
```

Clamp:

```text
q_used in [0.01, 0.99]
```

Context for weight lookup:

```text
seconds_bucket
odds_bucket
volatility_state
jump_state
source_health
spread_state
structural_vs_market_disagreement_bucket
book_health
```

Outputs:

```text
q_structural
q_market_mid
q_used
blend_weight
residual_adjustment
fair_confidence
fair_source
```

Labels:

```text
Brier score
log loss
calibration error
quote reward when used
adverse selection after fill
```

Forecast quality and maker reward are related but not identical. Store both.

## Moment / Regime State

Reuse the existing stateful episode machinery.

Moment key remains coarse:

```text
window_phase|odds_bucket|volatility_state|inventory_state|spread_state|direction_state|book_health
```

Raw continuous values stay in JSON.

## Tail-Risk Governor

Purpose:

```text
decide when not to quote, even if the fair/action learner likes the spot
```

Inputs:

```text
jump_state
transition_state
quote_created_regime
fill_regime
book_health
spread widening
volatility spike
inventory carry
fair uncertainty
source disagreement
time left
extreme odds
```

Key pattern:

```text
quote created in mid_chop, filled after jump
```

Outputs:

```text
risk_state = normal | caution | reduce_only | pause | hard_stop
tail_risk_score
transition_risk_score
reason
```

Hard overrides:

```text
hard_invalid -> pause
book_quarantined -> pause
late_window + inventory -> reduce_only / exit_fast
inventory_saturated -> reduce_only
extreme tail risk -> pause
```

## Quote-Action Arms

V3 starts with discrete arms, not a huge continuous grid:

```text
pause
reduce_only
exit_fast
tight_small_fast
base_small
wide_small
wide_tiny
normal_small
```

Each arm defines:

```text
margin_add_bps
margin_multiplier
clip_usdc
clip_shares_cap
max_inventory_multiplier
quote_ttl_ms
min_edge_bps
bid_enabled
ask_enabled
exit_only
hard_cut
```

Use USDC clip and share cap because binary token risk differs across 10c and 90c odds.

Selector:

```text
moment_key + fair_confidence + risk_state + inventory_state -> quote_action_arm
```

V3 uses contextual UCB with hierarchical fallback. Thompson sampling can be added later under a new selector version.

## Hierarchical Fallback

Lookup order:

```text
full moment_key
drop direction_state
drop inventory_state
drop volatility_state
regime_id only
global profile prior
```

## Exploration

Exploration is allowed in paper:

```text
epsilon = 0.10
```

No exploration in hard-risk states:

```text
hard_invalid
book_quarantined
late_window
inventory_saturated
settlement_conflict
```

Every exploration logs:

```text
exploration_flag
exploration_reason
selector_state_before
```

## Reward

Reward optimizes maker quality, not fill rate.

Version:

```text
maker-reward-v3
```

Formula:

```text
reward =
    fee_adjusted_pnl
  + spread_captured_bps
  - adverse_selection_penalty
  - inventory_carry_penalty
  - hard_cut_penalty
  - concentration_penalty
```

Initial implementation uses the V2 settlement and markout reward path plus V3 columns. If reward weights change, create a new `reward_version`.

## Disables / Promotions

Automatic paper-only disable:

```text
n_fills >= 30
mean_reward < 0
adverse_selection_10s materially negative
lower confidence bound < 0
not one-window concentrated
```

Paper preferred:

```text
n_fills >= 30
beats baseline
adverse selection acceptable
not one-window concentrated
```

Statuses:

```text
insufficient_sample
exploring
paper_preferred
disabled_active
cooldown_retest
needs_review
```

## Ablations

V3 writes shadow ablation rows:

```text
A0: V2 baseline replay
A1: fair learner only
A2: quote-action learner only
A3: tail-risk governor only
A4: full V3
```

Only A4 controls V3 paper quotes. A0-A3 are diagnostic rows.

## Persistence

Logical tables:

```text
crypto5m_maker_v3_campaigns
crypto5m_maker_v3_fair_blend_stats
crypto5m_maker_v3_action_arm_stats
crypto5m_maker_v3_tail_risk_stats
crypto5m_maker_v3_policy_actions
crypto5m_maker_v3_rewards
crypto5m_maker_v3_ablations
```

Every quote/fill/skip logs enough fields to reconstruct the choice:

```text
campaign_id
learner_version
selector_version
reward_version
fair_model_id
fair_blend_weight
fair_confidence
risk_state
tail_risk_score
quote_action_arm
profile_id
context_key
parent_context_used
episode_id
exploration_flag
policy_action
disabled_profile_flag
ablation_group
```

## UI / Report

Add read-only V3 visibility:

```text
fair structural / market / used / weight
context key
risk state
tail risk score
selected action arm
selector source
ablation status
```

No browser-side policy mutation.

## Launch

After tests pass, launch:

```text
maker-adaptive-paper-v3
```

Run V2 and V3 side by side.

## Evaluation

Milestones:

```text
30 windows: sanity only
100 windows: early disable/promotion review
200 windows: first V3 vs V2 comparison
500 windows: stronger review
```

Do not claim production edge.
