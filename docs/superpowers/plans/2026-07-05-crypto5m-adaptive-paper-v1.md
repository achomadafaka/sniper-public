# Polymarket Crypto5m Adaptive Paper v1

## Goal

Launch a separate paper-only campaign, `maker-adaptive-paper-v1`, that actively chooses quote profiles by coarse market context and learns from its own paper outcomes.

## Scope

- No real orders.
- No private keys.
- No production DB.
- No mutation of prior campaigns.
- Existing `maker-regime-v1` remains the control.

## Vertical Slice

1. Add pure adaptive selector/profile module.
2. Persist adaptive campaign, stats, disables, and rewards.
3. Add runner flag `--maker-adaptive-paper`.
4. Let adaptive mode force the regime router active and apply selected profiles to paper quotes.
5. Update settled-window reward stats from the fill ledger.
6. Expose selected profile, context, source, and sample count in `maker_state`.
7. Start the new paper campaign side-by-side.

## V1 Learning Rule

Use contextual UCB over:

```text
moment_key + profile_id
```

Safety overrides dominate:

```text
hard_invalid -> pause
late_window -> reduce_only or pause
inventory_saturated -> reduce_only
book unhealthy -> pause
```

Reward v1:

```text
settled_fill_pnl - hard_cut_penalty
```

Markout/adverse-selection components are logged as nullable placeholders in v1 and can become reward terms in a new reward version.

## Verification

Focused tests:

```bash
python3 -m pytest tests/test_crypto5m_maker_adaptive.py tests/test_crypto5m_maker_runner.py -q
```
