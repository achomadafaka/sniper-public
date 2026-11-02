# Hyperliquid Live Paper Proof Design

**Date:** 2026-07-27
**Authority:** observational paper only; no keys, orders, funded imports, or funded-state changes

## Objective

Forward-test the frozen discovery candidate without contaminating it with the older wallet-copy or
standalone-exhaustion lineages:

- nominate the first completed-hour crossing above a 30% trailing-72h run-up;
- skip when the causal trailing funding percentile is above `0.94993`;
- short at the first executable L2 fill in the next hour;
- close on first executable 15% target, 20% stop, or 24h cap;
- record `$40` operator-clip and `$5,000` capacity-reference fills from the same books.

The first milestone is fill-model falsification at 25 funding-complete closes. Edge evidence remains
incomplete until 200 funding-complete closes, with at least 20 closes in each of two distinct causal
trailing-72h alt-basket regime labels.

## Proven defects being corrected

1. The shared runtime checkout left `main`, so the v5 copy watchdog tried to launch absent/stale v3
   artifacts once per minute and failed `code_sha_mismatch`.
2. The first paper runner evaluated the current incomplete hourly candle and entered before the
   frozen nomination bar had closed.
3. It opened a dollar notional but closed a different dollar notional instead of the exact entry
   quantity. This invalidated depth-sensitive `$5,000` exit fills.
4. Its five-position cap stopped scanning the universe, creating provider-order selection bias and
   dropping eligible events from the proof population.
5. Missing close funding was silently charged as zero and still written as valid net PnL.
6. Clip size and concurrency changed execution behavior but were absent from immutable arm identity.
7. Cap `fill_delta_bps` was forced to zero because the modeled cap exit reused the executable fill.
8. The dedicated ledger was invisible to the Hyperliquid UI, which continued to headline the old
   standalone arm with zero decisions.

## Data and identity

The runner preserves provider symbol casing. Completed candles require `close_time_ms < now_ms`.
The decision timestamp is the nomination candle open; entry must occur during its immediately
following hour. Late or unavailable entry books remain immutable observations with a rejection
reason.

Arm identity hashes strategy, taxonomy, benchmark, regime definition, operator clip, reference
notional, fee model, entry timing, exit rules, and evidence mode. `code_sha` is runtime provenance,
not evidence identity. A semantic mismatch under an existing arm fails startup.

Every nomination is persisted before capacity classification. The event-level proof opens every
eligible paper observation; `portfolio_slot_eligible` records whether a five-slot funded-sized book
would have admitted it. Capacity never censors the primary evidence.

Every completed scan hour also persists an equal-weight trailing-72h alt-basket label using exact
provider symbols and only bars at or before the decision timestamp. The label is descriptive and
cannot veto an event. It prevents 200 closes from one market state being mistaken for cross-regime
proof.

## Execution accounting

Entry stores exact filled quantity for each notional. Exit walks asks for that exact quantity.
Entry/exit mid, top spread, exact quantity, and swept level count are persisted for both sizes.
Entry delay is derived from `entry_ts_ms - (decision_ts_ms + 1h)`.

Stop/target modeled exit is the threshold; cap modeled exit is the contemporaneous mid. Actual
gross PnL uses book fills. `fill_delta_bps = actual_gross_bps - modeled_gross_bps` therefore measures
overshoot plus execution impact instead of becoming zero by construction.

A close is never blocked on funding. If exact funding cannot be read, the position closes with
`funding_status='missing'` and net fields remain `NULL`. A later reconciliation pass may fill them
immutably. Missing funding never becomes zero.

## Runtime and UI

The dedicated database remains `data/hl_altshort_paper.db`; the runner is the sole writer. The Edge
Desk opens it read-only and exposes a new `edge_paper` block: heartbeat age, policy identity,
universe, nominations, accepted/skipped/rejected, open/closed/funding-incomplete counts, clip/ref
net summaries, fill delta, and 25/200-close milestones.

The status strip labels this block `EDGE PAPER TEST`. The old standalone-exhaustion collector is
paused and shown only as legacy evidence. Wallet copy stays active as a separate exit-discipline
control. Exactly one web server remains on port 5000.

## Failure behavior

- Incomplete or stale candle: no decision.
- Missing causal funding: persisted stood-down observation.
- Entry after the next-hour window: persisted rejected observation.
- Missing/short book: persisted rejected observation; no fabricated fill.
- Missing exit book: position remains open and stale state is visible.
- Missing funding on close: closed price evidence retained, net excluded until reconciliation.
- Heartbeat, DB, or schema mismatch: UI reports unavailable; it does not render zeros.

## Verification

Use focused tests only: paper runner unit/integration tests, provider-symbol tests, Edge Desk payload
tests, API route contract, static UI contract, schema validation, watchdog single-process behavior,
read-only authenticated endpoint probe, heartbeat freshness, DB row audit, funded heartbeat dry-run,
and single port-5000 ownership.
