# Office evaluated paper orders implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. Delta owns the order library, ledger, trader, probe and watcher; Beta owns runtime and epoch activation. No subagents.

**Goal:** Connect two committed blind Judge answers to one idempotent paper order, with typed display metadata and persistent entry safety stops.

**Architecture:** Re-verify the stored sheet and its causal receipt identity, freeze the decision bundle, reuse probe aggregation/Risk and Trader/ledger submission. Keep fill math and rail exits unchanged. The ledger enforces safety under its existing write transaction; the watcher maintains safety while agents are paused.

**Tech stack:** Python standard library, existing Office SQLite stores and paper fill model.

**Spec:** `docs/audits/2026-10-29-office-paper-eval-1-preregistration.md` through `9cfc43e79` (Amendments 4–8): any Judge EXIT; typed entry lower bounds, stale-leg demotion and frozen clock tolerance.

## Global constraints

- Paper only; no funded database, provider calls, environment changes, services or activation.
- Preserve max concurrency 3, daily loss USD 100, 15-second marks, `office-ps-paper-v1` fill model and existing rail exits.
- Epoch configuration freezes the USD 200 drawdown and 86400-second unknown/partial kill; no silent defaults.
- No efficacy statistics, live-money claims, grade access or closed per-coin outcome surfaces.
- Focused pytest uses `-n 2`; commits contain explicit owned paths through `scripts/crew_commit.sh`.

## Review focus and edge cases

- Forged/mismatched/future receipt or opposite book/epoch: unknown/refused, no order. Never trust a model-supplied sheet body.
- Partial/invalid Judge set or wrong served family: unknown, never SKIP. A split is counted separately; no ENTER unless both are known ENTER on the same receipt.
- Replay/concurrent callers/crash between stores: frozen bundle may survive without order; retry reuses the exact decision/order identity. A ledger receipt and order commit together.
- Missing ticker/decimals/price remains null; zero decimals is valid. Entry price is USD per whole token from actual modeled fill, not quote or model text. Costs remain separately recorded.
- Partial/unknown equity is never zero; stale marks, missing FX, backward time and corrupt safety config fail closed. Halt is durable across restart and never disables rail exits.
- Alpha's amendment makes clock regression and stale/unproven equity reversible entry pauses. Only drawdown, no-read timeout, manual/auditor halt or config drift latch irreversibly; drift maps to BROKEN, other latched kinds to FAILED. The initial peak is initial paper capital, not the first sample.
- Amendments 5–8 supersede the partial-equity pause: only fresh `real_quote_insufficient`, `dust_or_invalid_sell`, `no_swaps_before_cut`, `no_route` count at zero minus exit cost for both entry and halt bounds. Other leg errors pause, then demote after frozen `leg_stale_demote_s=1800` (configured, unmeasured); the halt basis retains their last proven priced mark. Gamma's review amendment uses labeled entry cost for a `demoted_never_priced` leg; that proxy is halt-only, never a mark or reported NET point. A fresh proven mark re-promotes. Peak uses known point equity/initial capital only. Explicit clock/FX/unproven-mark/binding-mismatch book faults never demote.
- The no-read timer now uses `last_equity_read_at` (initialized at epoch open, reset by known points or accepted bounds). `last_known_equity_at` remains full-point provenance. Stale/missing/unreadable data is never a reported point NET.
- `clock_skew_tolerance_s=0.125` absorbs bounded observation reordering with effective `max(now,last_at)`; larger regression pauses. Evidence is 1075 last-day supervisor offsets against ONE retained watcher sample: p99 absolute delta 0.0168421268 s, maximum 0.0919318199 s. This is an offset envelope, NOT paired dispatch jitter or a proven coverage bound; a miss pauses/retries.
- Beta owns trusted host callers for zero_tolerance/funded_boundary and epoch-state propagation. The library exposes no model halt tool. Pending orders are censored on a durable halt; open positions retain post_halt/epoch_halted_at and keep closing.
- Judge EXIT requires the host Trader context's exact book/epoch, uses the existing delayed ledger exit, and records exit cause separately from rails. Per Alpha's later ruling, Beta supplies the context after ANY committed Judge EXIT bound to that open order; requested_by is `judge`, never the model's actor text.
- Not handled: runtime aggregation scheduling, five-seat manifest/activation, real fills/landing, funded execution or evaluation statistics. Beta owns runtime and epoch status transitions.

## Task 1: admitted order library and display fields

- [x] Confirm exact context, Judge binding and frozen epoch-config names with Beta; send additive field names to Epsilon.
- [x] Write failing tests for real persisted receipt verification, unanimous/split/unknown decisions, context/epoch mismatch, replay/conflict, causal cutoff, metadata and actual-fill price.
- [x] Implement `sniper/office/eval_orders.py` using existing sheet/probe/Risk/Trader primitives; add nullable order columns and pass verified metadata through the existing atomic Trader transaction.
- [x] Run focused library, sheet, probe, Trader and ledger tests; commit explicit paths and request Gamma/Zeta review.

## Task 2: epoch safety and watcher integration

- [x] Write failing tests for exact drawdown threshold, 24-hour unknown boundary, restart, manual halt, concurrent submit, config mismatch, clock regression, stale/partial marks and unaffected exits.
- [x] Add persistent epoch entry-safety state, frozen-config validation and atomic submit checks to `ledger.py`. The existing paper watcher already calls shared `manage`, so no script wiring/timer is added; mark cadence and fill/exit math are preserved.
- [x] Run focused safety, watcher, ledger and order-library tests. Send exact tip/config/status fields to Beta, Gamma and Zeta; Alpha owns merge/deploy. Source reviews and actual activation remain separate gates.

## Host contracts

- `eval_orders.enter(store_conn, ledger_conn, policy, *, context, candidate, judgements, now, sol_usd, sol_at)`: validated host Trader context, both committed Judge rows bound to the receipt. Returns typed state/reason/order_id/bundle_hash/outcome. Action key is SHA256 of canonical JSON `[epoch_id, book_id, decision_id]`.
- `eval_orders.exit(ledger_conn, *, context, order_id, reason, now, asker)`: `asker={slot,decision_id,answers}`, answers maps both slots to committed decision or null. The host passes the actual view cut, never the commit time. Gamma owns the exit-asker audit fix; later answers are retained even if the first request closed the order.
- `ledger.halt_evaluation_entries(conn, *, epoch_id, reason, actor, now, kind='manual')`: trusted operator/auditor code only; kinds manual/zero_tolerance/funded_boundary. Beta owns callers and status propagation, no model halt tool is added.
- `ledger.evaluation_safety_state(conn, *, epoch_id)`: read-only, no DDL, missing schema/row is typed unknown. `halt_kind=config_drift` maps to BROKEN, other non-null halt kinds to FAILED. Nullable order `epoch_halted_at`/`post_halt` preserve open inventory's post-halt provenance.
- Reversible admission reasons: `epoch_clock_regressed`, `epoch_mark_stale`, `epoch_equity_unknown`, `fx_unknown`. Accepted `partial_lower_bound` is distinct from point equity. Persisted diagnostics include unpriced/demoted ids, demotion seconds, re-promotions and separate entry/halt equity bases.
