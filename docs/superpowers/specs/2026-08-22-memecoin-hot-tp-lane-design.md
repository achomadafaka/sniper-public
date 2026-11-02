# Memecoin Hot TP Lane — Event-Driven Take-Profit (Self-Custody)

**Date:** 2026-08-22. **Status:** APPROVED DESIGN (operator: self-custody lane over a
Jupiter vault order). **Scope:** funded Solana exits only. Paper lanes untouched.

## Problem

The funded take-profit fires on the fastlane TICK (`MEMECOIN_FASTLANE_TICK_S` = 1 s,
measured 1.6 s median between marks). A swap that crosses the TP target waits for the next
tick before the sweep sees it. Measured exit execution is 1.33 s median after the decision.
On this venue the sellable window after a peak is often a few seconds, so one tick of
waiting is a material share of the window. The exact-quote warm-up already exists
(`exact_quote_demand` near_tp at TP − 10 points). What is missing is an event-driven
decision.

## Design — the hot lane is a timing change, not a new exit path

1. **Stored target.** `memecoin_positions.tp_target_price` (REAL, USD). The TP percentage
   is already immutable in the frozen funded contract. The price target is written the first
   time the sweep sees the position with a known `entry_price` and a frozen `tp_pct > 0`:
   `entry_price * (1 + tp_pct / 100)`. It is written once and never updated.
2. **Mark hook.** `SolanaSwapMarkCache` accepts an optional `on_mark(mark)` callback and
   calls it after it stores a swap or state mark.
3. **Trigger.** `HotTpTrigger` (new module `sniper/services/memecoin_hot_tp.py`) keeps a
   thread-safe map `mint -> (tp_target_usd, quote_usd)` that the fastlane pass refreshes on
   every tick from the open real positions. `on_mark` converts the mark to USD with the
   stored `quote_usd`; when the mark is at or above `near_fraction * tp_target_usd`, the
   trigger sets a wake event. Wakes are rate limited to one per `min_interval_s`.
4. **Wake.** The fastlane worker replaces `time.sleep(tick_s)` with `wake.wait(tick_s)`.
   A wake runs the normal `_pass` immediately: refresh marks, write marks, and the
   existing `real_exit_sweep` under `_funded_exit_lock` with the existing `demand_fn`,
   exact preview, rails, claim machine, exit intent, and executor. No exit logic changes.
5. **Observability.** The heartbeat reports `hot_tp: {wakes, passes, last_wake_ts}`. The
   fastlane pass logs `hot_tp pass` with the mint and the mark-to-target ratio.

## Invariants preserved

- The exact whole-balance quote still confirms before signing (the sweep's own path).
- Claim single-flight prevents a concurrent tick sweep from double-selling.
- The 20 s stop confirmation is untouched (replay 2026-08-22: keep).
- No pre-signed transaction is ever retained.
- Funded invariants from `2026-07-18-memecoin-funded-entry-contract-regime-design.md` and
  the free direct-submission spec are untouched.

## Settings (`sniper/settings/memecoin.py`, from `.env`)

- `MEMECOIN_HOT_TP_ENABLED` (default 1)
- `MEMECOIN_HOT_TP_NEAR_FRACTION` (default 0.97)
- `MEMECOIN_HOT_TP_MIN_INTERVAL_S` (default 0.25)

## Tests

- Trigger fires at/above near_fraction × target, not below; respects min interval;
  ignores mints without a target; converts with the stored quote_usd.
- Cache calls `on_mark` for swap and state marks and never for refused rows.
- `ensure_tp_target` writes once from entry_price and frozen tp_pct, never overwrites.
- Worker wake path: a set event runs a pass before the tick elapses.

## Deploy and read

Release-pinned deploy with `scripts/deploy_memecoin_runtime.sh <sha>` after push, with the
usual gates. Canary read: on organic TP closes after deploy, compare the mark-cross to
fill latency (first mark ≥ target → `exit_timing_json` fill) against the 20 closes before
deploy. No improvement claim before 20 post-deploy TP attempts.
