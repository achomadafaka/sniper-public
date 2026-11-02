# Crypto5m V3.3 - Post-Gate Maker Design

**Date:** 2026-07-06
**Status:** DRAFT, gated. Do not implement until the v3.2 tournament gate reads.
**Scope:** paper-only crypto5m maker. No live orders, private keys, production DB writes, or mutation of old campaign history.

## Trigger

V3.3 is not a new live tweak. It is the next paper campaign design after the
registered v3.2 tournament reaches its forward n>=200 paired windows.

Inputs required before build:

- frozen-f vs learning-f gate read
- learning-r vs control gate read
- period attribution report over the registered forward sample
- vol-floor shadow ladder over pre-anchor historical data only

No manual peeking changes this spec. If those inputs are not available, V3.3
does not launch.

## Current Diagnosis To Verify At The Gate

The leading hypothesis is:

```text
honest vol floor is useful
online learner is likely a tax
```

Claude's learner review gives the failure mode to test:

1. **Censored reward:** missed quotes are not penalized, so wide/tiny arms look
   good per fill while participation collapses.
2. **Wrong fair objective:** fair-blend learner optimizes settlement Brier, even
   though this line already rejected level-Brier as the maker edge.
3. **Pseudo-replication:** fill-level samples share a window outcome but are
   treated as independent.
4. **Weak hierarchy trust:** a sparse child context can block fallback to a
   stronger parent context.
5. **No decay / inert risk penalty:** old regimes persist too long, and the UCB
   adverse-selection penalty is too small to matter.

Treat these as design constraints, not as permission to patch the running
tournament.

## Branching Rule

### Branch A - frozen-f >= learning-f

If frozen-f matches or beats learning-f on paired forward windows, V3.3 is a
fixed-policy campaign, not a learner.

Design:

- keep the winning vol-floor configuration from the gate and ladder
- disable online fair-blend learning
- disable online arm learning
- use the frozen prior action policy as baseline
- allow only pre-registered fixed participation rules if the forward period
  attribution proves them

This is the default if the learner does not justify itself.

### Branch B - learning-f > frozen-f

If learning-f beats frozen-f on paired forward windows with a positive CI, V3.3
may rebuild the learner.

Required fixes:

- window-level paired reward
- maker-reward objective for fair selection, not Brier
- sufficient-sample hierarchy
- recency decay
- per-share and per-window normalization
- no learned deviation without evidence

### Branch C - inconclusive

If the gate is noisy or inconsistent, do not launch V3.3. Keep collecting or run
the simpler fixed-policy line only.

## Vol-Floor Shadow Ladder

Purpose: decide whether 2e-5 is a plateau or a lucky spike before V3.3 inherits
it.

Sample rule:

- historical/pre-anchor only
- cutoff is engine time, exclusive
- tournament forward windows are excluded
- no campaign decision is made from this ladder alone

Registered command:

```bash
python3 scripts/crypto5m_vol_floor_shadow.py \
  --before-window-start-ts 1783355700 \
  --floors 1e-5,1.5e-5,2e-5,3e-5,5e-5,1e-4 \
  --blend-weights 0,0.25,0.5,0.75,1 \
  --campaign maker-regime-v1
```

Read:

- Brier by floor is diagnostic only
- realized/share and claimed-minus-realized edge matter more for maker design
- market-mid blend rows are diagnostics, not permission to re-enable Brier
  optimization

Structural limit (2026-07-06 run, 322 pre-anchor windows): the ladder CANNOT
decide the floor economically. realized/share is constant across counterfactual
floors by construction (historical fills, not replayed quotes), and the column
that does vary — claimed fill-edge — RISES with the floor (0.031 @1e-5 →
0.055 @1e-4), which is exactly the fake-edge mechanism already proven in calm
windows (inflated floor → fair hugs 0.5 → everything looks cheap). Likewise the
Brier-optimal 5e-5 ≈ p50 vol is the rejected level-accuracy metric rewarding
flattening. So the ladder's only decision-grade output is the bind-rate profile
(2e-5 binds 10% overall — the lever is concentrated in calm windows, consistent
with the calm-concentrated edge). The floor CHOICE needs the proper
market-mid/floor replay or forward campaign evidence — never this ladder alone.

## Fixed-Policy V3.3 If Branch A Wins

Campaign name:

```text
maker-v33-fixed-paper
```

Properties:

- paper-only
- separate DB/campaign namespace
- no learner state writes
- no fair-blend stats writes
- no exploration
- same settlement and ledger code as tournament campaigns
- paired vs the surviving control/frozen baseline

Allowed fixed levers after gate:

- vol floor selected by gate + ladder
- fixed action prior
- optional regime/flow participation rule only if period attribution proves it

The existing flow-scaled participation draft stays a gated sub-lever:

```text
docs/superpowers/specs/2026-07-06-crypto5m-v33-flow-scaled-participation-draft.md
```

It must pass its own exogenous print-flow gate before inclusion.

## Learner V3.3 If Branch B Wins

Campaign name:

```text
maker-v33-learner-paper
```

### Reward

Replace per-fill reward with one observation per window:

```text
reward(context, arm, window) =
    campaign_window_pnl
  - paired_baseline_window_pnl
```

Baseline candidates:

- frozen-f if the learner branch is being tested
- control/regime-v1 if testing absolute campaign value

Rules:

- one reward per window/context/arm, not per fill
- missed participation is punished when the baseline earns in the same window
- fill-level markouts remain labels, not independent training samples
- normalize by both window and shares where arm sizing differs

### Fair Value

Delete Brier-driven fair selection.

Allowed fair modes:

- frozen structural fair with selected vol floor
- maker-reward-labeled fair selector, if and only if reward labels are
  window-level and paired

Forbidden:

- choosing fair blend by settlement Brier
- moving q_used toward market mid because market mid wins Brier

### Context Hierarchy

Trust a context level only when it has enough independent windows.

Lookup:

```text
full moment_key
drop direction_state
drop inventory_state
drop volatility_state
regime_id only
global prior
fixed fallback
```

Sufficiency gate:

```text
min_windows >= 30
```

If a child has 2 fills and the parent has 50 windows, use the parent.

### Decay

Use exponential decay by window age.

Starting half-life:

```text
1 to 2 days
```

Decay applies to reward stats, disables, and promotions. It does not rewrite old
rows; it only affects current scoring.

### Disable / Promote

Disable and promote only on window-level paired reward.

Disable:

```text
n_windows >= 30
mean_paired_reward < 0
lower_ci < 0
not one-window concentrated
```

Promote:

```text
n_windows >= 30
beats fixed fallback or frozen baseline
lower_ci > 0
not one-window concentrated
```

No fill-count gate can trigger a learner action by itself.

## Period Attribution Required At Gate

Run:

```bash
python3 scripts/crypto5m_period_attribution_report.py --forward-anchor 1783355700
```

Required cuts:

- jump share buckets
- posted and filled shares
- selected arms
- risk state mix
- odds bucket
- winner/loser inventory
- claimed vs realized edge
- print count/notional where retention allows

Use this to decide whether any fixed participation rule is allowed. Do not use
it to add more online learner arms.

## Launch Rules

V3.3 gets a new campaign and new DB namespace.

Do not:

- patch v3.2f/v3.2r histories
- change tournament entrants
- restart the current engines as part of this spec
- merge V3.3 results into old campaigns
- claim production edge

First read:

```text
n >= 200 forward paired windows
```

Decision metric:

```text
paired window PnL delta vs baseline, with CI
```

## Tests Required Before Any V3.3 Launch

Fixed branch:

- frozen policy writes no learner stats
- no exploration flags
- campaign config records vol floor and fixed mode
- paired accounting uses same-window windows only

Learner branch:

- missed participation produces negative reward when baseline earns
- one reward per window/context/arm
- fill count alone cannot disable or promote
- sparse child context falls back to sufficient parent
- Brier cannot select fair blend
- decay changes current scoring without rewriting history
- disabled arms are scoped by campaign/context/reward version

Shared:

- no live orders/private-key imports
- no production DB writes
- old campaigns untouched
- kill guard covers the new challenger
