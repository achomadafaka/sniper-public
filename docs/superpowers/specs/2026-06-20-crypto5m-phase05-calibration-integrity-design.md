# Crypto-5m Phase 0.5 — Continuous Calibration + Capture Integrity — Design

**Date:** 2026-06-20
**Status:** building (autonomous; GPT Pro directive after Phase-0 approval)
**Branch:** `feat/crypto5m-phase05-calibration-integrity` (stacked off `main`; PR, not direct merge)
**Builds on:** Phase 0 (`sniper/algos/polymarket_crypto5m/`, merged `9fcce43..a6eb758`).
**Scope:** make the capture spine capable of **unattended, trustworthy accumulation** of the boundary-calibration dataset, and close integrity gaps. **Still capture-only** — no taker simulator, no probability model, no paper decisions, no live orders/keys.

## Why (GPT Pro verdict)

Phase 0 is approved for PR review but is **not operationally complete**: a single-window shell loop can't accumulate the ≥100 resolved windows the boundary-rule lock needs, snapshot-only RTDS is a degraded condition (not the design), unresolved rows need automatic backfill, and several integrity guards are missing. Phase 0.5 fixes that. **No Phase-1 work** until Chainlink boundary reconstruction + continuous rollover + post-resolution reconciliation + book-state integrity are proven on a **held-out** window set.

## Components

### 1. Continuous market lifecycle manager (`lifecycle.py` + CLI `--continuous`)
A pure `WindowLifecycle` state machine over the set of in-flight windows, states:
`discovered → capturing → awaiting_resolution → reconciled` (terminal) or `→ expired` (timeout).
Handles current + next + recently-closed windows simultaneously; rediscovers on an interval; dynamic subscribe/unsubscribe of Up/Down token ids; retains a closed window's subscription until settlement/reconciliation; no per-boundary restart gap. CLI: `--continuous`, `--max-windows`, `--discovery-interval-seconds`, `--settlement-timeout-seconds`. The state machine is pure/tested; the long-running service wraps it.

### 2. RTDS health + fallback (`chainlink_rtds.py` additions)
Record `rtds_delivery_mode ∈ {live_updates, snapshot_reconstruction, authenticated_chainlink_report, unavailable}` per capture interval; a staleness watchdog (`max_source_age`); `rtds_reconnect_count`, `rtds_stale_interval_ms`, `last_chainlink_source_ts`, `last_chainlink_receive_ts`. If live updates stay absent, reconnect/resubscribe shortly after each window boundary so the snapshot backfill contains the boundary ticks. **Snapshot-only delivery is never called "live coverage."** Subscription variants (`type="*"` vs `type="update"`, filtered vs unfiltered) are probed and the observed delivery mode recorded — not assumed.

### 3. Automatic post-resolution reconciliation (`reconciliation.py` + worker)
A pure `needs_reconcile(market_row)` + `apply_resolution(...)` and a worker that revisits unresolved captured markets until it obtains winning token, winning outcome, UMA status, post-hoc Gamma `priceToBeat`, and resolution ts. Idempotent resume. A reconciliation status state machine: `pending → resolved | timed_out | conflict`.

### 4. Two-stage boundary calibration (`calibration.py`)
- **Calibration set** = first 100 usable resolved windows; **holdout** = next 100, untouched.
- Select exactly ONE rule (`last_at_or_before` / `first_at_or_after` / `nearest`) + a tolerance using **only** the calibration set; freeze both.
- Evaluate once on the holdout; lock only if the holdout gate passes.
- Report raw Decimal abs error + match rates at **$0.0001 / $0.001 / $0.01** (tolerance chosen *before* seeing holdout).
- `price_to_beat_resolver.LOCKED_BOUNDARY_RULE` stays `None` until a holdout pass.
- Quarantine every mismatch.

### 5. Known-at-time discipline (schema + guard)
Persist `chainlink_source_ts`, `chainlink_receive_ts`, `price_to_beat_known_ts`, `known_latency_ms` on the price-to-beat lane. A (future) paper decision may not use the reference before `price_to_beat_known_ts` — a `decision_not_before(known_ts)` guard prevents lookahead.

### 6. Book integrity reconciliation (`book_integrity.py` + `crypto5m_book_reconciliation`)
Periodically fetch a fresh REST full book and compare it against the locally reconstructed `BookState`; persist `book_state_match`, `book_divergence_levels`, `book_divergence_duration_ms`, `rest_reconciliation_ts`, `quarantined_interval`. Any unexplained divergence marks the interval unusable for (future) simulated execution. Reporting says **"zero detected gaps,"** never "zero gaps" (the market channel has no documented monotonic sequence number; absence of detected gaps ≠ provable completeness).

### 7. Market identity safety (`identity.py` + persistence change + resolver)
Treat `condition_id`, `market_id`, `up_token_id`, `down_token_id`, `window_start_ts`, `window_end_ts`, `series_id` as **immutable after first confirmation**. `upsert_market` updates only **operational** fields (`active`, `closed`, `accepting_orders`, `enable_order_book`, fee metadata, `price_to_beat`, `raw_payload`, `last_updated_ts`); on an identity-field change it emits `MARKET_IDENTITY_CHANGED` to `crypto5m_identity_changes` and does **not** overwrite. (This narrows the Phase-0 round-1 "update all fields on conflict" fix, which over-corrected.) The resolver rejects multi-market Gamma events unless exactly one market is selected deterministically (by condition id / outcomes / slug), never blind `markets[0]`.

### 8. Clock integrity (`clock.py` + `crypto5m_clock`)
Explicit `clock_source ∈ {system_ntp, polymarket_http_date, binance_server, manual_offset}` + `clock_offset_ms` + `clock_last_verified_ts`. Binance server time is a **fallback**, not hidden canonical truth. A startup check warns/fails on excessive host skew (the +7h env skew is an ops bug to surface, not paper over).

### 9. Storage health (reporting + stress test)
DB-size + event-rate reporting (~41 events/s observed → millions/day), WAL + indexes on hot query columns, a retention/compaction *design* (no raw deletion before boundary+fill validation), and a **100k-event persistence/replay stress test**: no thread failures, idempotency, bounded memory, restart recovery, deterministic book reconstruction.

## Persistence additions
`crypto5m_identity_changes`, `crypto5m_book_reconciliation`, `crypto5m_clock`; new columns: `rtds_delivery_mode` + watchdog fields on `crypto5m_chainlink_ticks`/a per-window summary; known-at-time columns on `crypto5m_price_to_beat`. All idempotent, server-time, version-lineaged, in the dedicated DB.

## Acceptance gate before Phase 1
Continuous rollover without shell looping; ≥200 resolved windows auto-accumulated; calibration/holdout separated; book-state divergence measured; unresolved markets auto-reconcile; Chainlink delivery mode + latency explicit; no paper-decision code; no live-order/key surface. (The 200-window accrual is a **runtime** gate — the code lands now; the data accrues over real time.)

## Out of scope
Taker/maker simulator, probability model, paper decisions, fee-rate interpretation (carried to Phase 1: do not interpret `takerBaseFee=1000` as the rate without proving `fee = shares·feeRate·price·(1-price)`).
