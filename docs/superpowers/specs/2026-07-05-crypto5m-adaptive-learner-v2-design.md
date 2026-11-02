# Crypto5m Adaptive Learner V2 Design

**Date:** 2026-07-05
**Status:** Approved design
**Scope:** next adaptive paper learner version after cockpit visibility exists.

## Decision

Build Learner V2 as a new paper campaign:

```text
maker-adaptive-paper-v2
```

Do not mutate `maker-adaptive-paper-v1`.

V2 improves the learner, reward function, profile disable/promote rules, and context fallback. It stays paper-only.

## Goal

Learn:

```text
context + quote profile -> maker outcome
```

The learner is not trying to predict Up/Down directly. It is deciding which paper quote posture works best in the current context:

```text
quote_tight
quote_base
quote_wide
normal_small
reduce_only
pause
exit_fast
```

## Non-Goals

- No real orders.
- No private keys.
- No production DB.
- No neural net.
- No self-modifying code.
- No policy mutation through UI.
- No retroactive profile relabeling.
- No future data in current decisions.
- No claim of production readiness from paper-only evidence.

## Campaign Versioning

Create a new campaign and reward version:

```text
campaign_id = maker-adaptive-paper-v2
selector_version = adaptive-ucb-v2
reward_version = maker-reward-v2
```

Reason:

- V1 reward is settlement-fill PnL minus hard-cut penalty.
- V2 adds markout/adverse-selection costs and stronger disable/promote rules.
- Mixing V1 and V2 rows would corrupt learning.

## Reward V2

Reward must optimize maker quality, not fill count.

Base formula:

```text
reward =
  settled_fill_pnl
  - adverse_selection_penalty
  - inventory_carry_penalty
  - hard_cut_penalty
  - concentration_penalty
```

Store all components separately:

```text
settled_fill_pnl
adverse_selection_3s_bps
adverse_selection_10s_bps
adverse_selection_30s_bps
future_executable_unwind_10s
inventory_carry_ms
hard_cut_penalty
concentration_penalty
reward
```

### Markout Source

Use known-after-fill quote snapshots from the same paper DB:

```text
maker_quotes
```

For each fill, find future quote/book snapshots at:

```text
3s
10s
30s
```

Compute markout in token price space.

If no future snapshot exists, mark:

```text
shadow_quality = not_evaluable
```

Do not impute missing markouts.

### Economic Priority

Promotion decisions require economic labels:

```text
settled_fill_pnl
future_executable_unwind_10s
fee_adjusted_pnl
```

Diagnostic labels such as mid/fair markout can explain behavior but cannot promote a profile by themselves.

## Context Hierarchy

Full context key:

```text
window_phase|odds_bucket|volatility_state|inventory_state|spread_state|direction_state|book_health
```

Fallback order:

```text
full context
drop direction_state
drop inventory_state
drop volatility_state
window_phase + odds_bucket + spread_state + book_health
regime only
global
```

The selector records:

```text
parent_context_used
fallback_depth
fallback_reason
```

V2 must not freeze because the exact context has thin sample.

## Exploration Budget

Exploration is allowed because this is paper.

Rules:

```text
epsilon_start = 0.10
epsilon_min = 0.02
min_profile_fills_per_context = 10
no exploration in safety states
```

Safety states:

```text
hard_invalid -> pause
book_quarantined -> pause
late_window -> reduce_only if inventory else pause
inventory_saturated -> reduce_only
```

Exploration applies only to non-safety contexts.

The selector records:

```text
selection_source
exploration_flag
exploration_budget_remaining
```

## Disable Rules

A context/profile can be disabled automatically in paper if:

```text
n_fills >= 30
mean_reward < 0
mean_adverse_selection_10s_bps < -50
lower_confidence_bound_reward < 0
not concentrated in a single window
```

Disabled rows record:

```text
disabled_at
disabled_reason
selector_version
reward_version
sample_counts
confidence_band
```

Disabled means:

```text
selector stops choosing that profile in that context
```

It does not delete history.

## Promote Rules

A context/profile can be marked:

```text
paper_preferred
```

if:

```text
n_fills >= 30
mean_reward > quote_base mean_reward
adverse_selection_10s_bps acceptable
result not dominated by one window
paired-window delta not worse than control where available
```

This is not production validation.

The status label must remain:

```text
paper_preferred
```

not:

```text
best
validated
profitable
```

## Concentration Check

One window cannot dominate a profile verdict.

Track:

```text
top_window_reward_share
top_window_fill_share
n_distinct_windows
```

If:

```text
top_window_reward_share > 0.50
```

then status is:

```text
needs_more_windows
```

even if raw reward is positive.

## Cooldown and Re-Enable

Disabled profiles can be revisited only in paper.

Rules:

```text
cooldown_windows = 50
retest_epsilon = 0.01
max_retest_fills = 5 per cooldown
```

Retest rows are tagged:

```text
selection_source = retest
```

If retest reward remains negative, extend cooldown.

## Persistence Additions

Extend or add adaptive tables:

```text
crypto5m_maker_profile_stats
crypto5m_maker_profile_disables
crypto5m_maker_adaptive_rewards
crypto5m_maker_profile_promotions
crypto5m_maker_profile_cooldowns
crypto5m_maker_context_fallbacks
```

Every quote/fill/decision row must include:

```text
campaign_id
episode_id
moment_key
adaptive_context_key
profile_id
selector_version
reward_version
selection_mode
exploration_flag
parent_context_used
fallback_depth
```

## Selector

Use contextual UCB V2:

```text
score =
  mean_reward
  + exploration_bonus
  - risk_penalty
  - concentration_penalty
```

Risk penalty includes:

```text
negative adverse_selection_10s
hard_cut rate
inventory carry
disabled/retest state
```

Do not optimize raw fill rate.

## UI/API Dependency

Learner V2 should be built after the Adaptive Paper Cockpit.

Reason:

- V2 needs visible reward components;
- disable/promote decisions need operator review;
- paired-control comparisons need to be visible before interpreting V2.

## Tests

Add tests for:

- reward v2 component calculation;
- missing markout produces `not_evaluable`;
- no future data used during current selection;
- context fallback depth is recorded;
- safety states block exploration;
- UCB score ranks by reward after sample gate;
- negative adverse selection lowers score;
- disable rule fires only after sample and concentration gates;
- promotion rule labels `paper_preferred`;
- disabled profile is skipped;
- cooldown permits limited retest;
- v1 rows are not pooled into v2;
- campaign IDs remain separate;
- no real-order/private-key imports.

## Launch

Launch only after cockpit implementation is live.

Command shape:

```bash
python3 -u scripts/run_polymarket_crypto5m_maker.py \
  --paper-db data/crypto5m/maker_adaptive_v2.sqlite \
  --pid data/crypto5m/maker_adaptive_v2.pid \
  --campaign maker-adaptive-paper-v2 \
  --continuous \
  --poll 1.0 \
  --maker-adaptive-paper \
  --adaptive-control-campaign maker-regime-v1 \
  --adaptive-selector-version adaptive-ucb-v2 \
  --adaptive-reward-version maker-reward-v2
```

Exact flags may differ after implementation, but the process must use a separate DB and campaign.

## Evaluation Milestones

```text
30 windows: sanity only
60 windows: early disable review
100 windows: first learner-quality review
200 windows: campaign comparison
500 windows: stronger profile/context review
```

## Open Questions

None blocking.

Parameter values are intentionally initial defaults. Changing reward weights after launch requires a new `reward_version` and campaign namespace.

