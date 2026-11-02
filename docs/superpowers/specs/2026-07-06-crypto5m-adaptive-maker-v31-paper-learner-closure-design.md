# Crypto5m Adaptive Maker V3.1 Paper Learner Closure

## Purpose

V3 collected paper rewards and selected quote-action arms from stats, but two learner loops were inert:

- `crypto5m_maker_v3_fair_blend_stats` was read by `choose_fair_value()` but never written.
- `crypto5m_maker_v3_action_arm_stats.disabled` and `paper_preferred` existed, and the selector respected them, but no V3 evaluator ever set them.

V3.1 closes those loops without touching real orders, private keys, production DBs, V2 history, or V3 history.

## Campaign

New challenger:

```text
maker-adaptive-paper-v3.1
```

Control:

```text
maker-adaptive-paper-v2
```

V3.1 uses the existing paper-only maker runner flag:

```text
--maker-adaptive-v3
```

with V3.1 versions:

```text
learner_version = v3.1
selector_version = adaptive-maker-brain-v3.1
reward_version = maker-reward-v3.1
```

## Root Cause Audit

Fair stats were empty because V3 had only the read side:

```text
choose_fair_value() -> SELECT crypto5m_maker_v3_fair_blend_stats
```

There was no settlement callback that evaluated resolved policy actions and updated fair stats.

Disable/promote was inert because V3 settlement stopped after:

```text
insert reward row
increment action_arm_stats
```

It never evaluated the updated stat cell. V2 already had this pattern via `_maybe_disable()` / `_maybe_promote()`; V3 lacked the equivalent.

## Behavior

On settlement, V3.1 now:

1. Inserts fill reward rows as before.
2. Updates action-arm stats.
3. Evaluates the action arm for paper-only `disabled_active` or `paper_preferred`.
4. Evaluates fair-blend forecast quality for all candidate weights:

```text
market_only      w=0.00
blend_25         w=0.25
blend_50         w=0.50
blend_75         w=0.75
structural_only  w=1.00
```

5. Writes the selected context weight back to `crypto5m_maker_v3_fair_blend_stats`.

Fair-blend evaluation is forecast-only for unchosen weights. Maker reward remains attached only to actually used fills.

## Selector Precedence

```text
1. safety_override
2. exact-context disabled filter
3. paper_preferred
4. exploration
5. learned_ucb / parent_fallback
6. hardcoded_fallback
```

Safety still wins over every learned choice.

## Disable / Promote Gates

Catastrophic disable:

```text
n_fills >= 10
mean_reward <= -0.50
negative adverse / repeated hard-cuts / negative CI
not one-window dominated
```

Normal disable:

```text
n_fills >= 30
mean_reward < 0
CI high < 0
not one-window dominated
```

Paper preferred:

```text
n_fills >= 30
CI low > 0
beats base_small when base_small has enough sample
not one-window dominated
```

These states apply only inside the V3.1 paper campaign.

## Visibility

The adaptive API surfaces:

- current selected arm;
- selector source;
- disabled/preferred action-arm counts;
- fair-blend context stats;
- selector source distribution;
- V3.1 vs V2 paired-window summary.

## Scope Guards

- No live orders.
- No private keys.
- No production DB.
- No mutation of V2/V3 history.
- No service restart required by the code change.
- No hand-tuned mid-chop or odds-specific rule.
