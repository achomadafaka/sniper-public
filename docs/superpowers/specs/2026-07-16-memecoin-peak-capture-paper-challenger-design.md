# Memecoin Peak-Capture Paper Challenger Design

**Date:** 2026-07-16
**Status:** Approved for paper implementation
**Authority:** Additive paper arm only. Never funded.

## Purpose

Test one narrow hypothesis: coins that reach a meaningful gain and then stop making highs should harvest profit before a full round trip.

The existing `trail_d` arm waits for a percentage drawdown from peak. The challenger isolates a different trigger: peak activation followed by a fixed count of no-new-high sweeps.

## Policy

Arm name: `peak_lock_e`.

- Shared entry fill with all other paper arms.
- Same exact `(network_id, mint)` identity.
- Same mirror-on-sell behavior as `trail_d`.
- Activation gain defaults to the already-running `trail_d` activation: 30%.
- Stall count defaults to the already-running momentum rollover count: two sweeps.
- Wide catastrophe stop and timecap default to the existing `trail_d` values.
- After activation, exit when the price remains above entry and no new high occurs for the configured stall count.
- A new high resets the stall counter.
- Precedence: catastrophe stop, peak-stall harvest, timecap.

These are a frozen forward-paper registration, not a parameter search. The arm adds no quote request because all arms share the same sweep price map and entry quote.

## Boundaries

- No real-runner setting or funded exit function reads `peak_lock_e`.
- No mutation of `control`, `momentum_a`, `alpha_b`, `scalp_c`, `scalp_c2`, `trail_d`, or `real_shadow` behavior.
- No stop removal.
- No adaptive policy selector or paper verdict mutation.
- No backtest-based promotion.

## Evaluation

Evaluate only matched episodes where the comparator arms also entered. Report by chain and signal source:

- close count
- net PnL and return
- hit rate
- maximum executable return
- realized giveback from peak
- exit reason
- quote coverage and route failures
- drop-the-two-best-winners sensitivity

Do not promote until the branch-close evidence gate in the entry-radar spec is met and a separate funded-behavior spec is approved.

## Acceptance tests

- `peak_lock_e` opens with the identical shared fill and no extra quote call.
- It does not exit before activation.
- A new high resets no-new-high sweeps.
- It exits after activation plus the registered stall count while still profitable.
- Catastrophe stop wins over stall.
- Mirror sell closes it.
- It is present in default paper arms and absent from every real-policy selector.
