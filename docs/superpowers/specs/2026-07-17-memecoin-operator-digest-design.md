# Memecoin Operator Digest Design

**Status:** approved for implementation by the operator on 2026-07-17.

## Goal

Surface funded-runner health and failures without requiring log inspection and without adding notification, analytics, or network work to the entry loop.

## Contract

- One compact Telegram digest after each configured period.
- The digest reads SQLite only. It makes no pricing, RPC, Fomo, browser, LLM, or dashboard request.
- Collection runs in a dedicated daemon thread with its own SQLite connection.
- Delivery uses `sniper.services.memecoin_telegram.enqueue_telegram()`; the digest worker never calls Telegram HTTP directly.
- The first digest waits one full period after process start. Restarts therefore cannot create an immediate notification storm.
- Repository default is disabled. The funded host may enable it explicitly with `MEMECOIN_TG_DIGEST_ENABLED=1`.
- All observation windows use corrected real-UTC half-open intervals `(start, end]`. Entry windows prefer the immutable attempt completion time and fall back to a separate corrected `opened_recorded_at` position-commit stamp when best-effort trace finalization is absent. Exit bookkeeping stamps corrected `closed_recorded_at`, leaving the strategy's `opened_at` and `closed_at` unchanged. Slow transactions committed after a watermark therefore remain observable. Future-clock rows are excluded and a shared boundary cannot be counted twice. Successful passes carry their exact end forward as the next start; failed collection or queue submission retries the unadvanced window.
- Missing tables, query failures, queue saturation, and Telegram failures are fail-soft and cannot stop the funded loop or exit sweep.
- The digest never changes entries, exits, sizing, reservations, wallet approvals, safety verdicts, or accounting state.

## Contents

The digest reports real entries and exits, realized PnL versus the daily breaker, refusal and terminal-failure counts, open real positions by chain, pending reservations and stale-reservation warnings, observed feed freshness, mark coverage for held chains, and funded ladder sample counts.

Severity is deterministic: green for no warning, yellow for actionable operational warnings, and red for a tripped daily breaker or realized loss beyond 60% of its configured limit.

## Runtime wiring

`scripts/memecoin_runner_loop.py` starts the worker only when enabled and the period is positive. The worker sleeps before its first pass, opens its own connection inside the worker thread, then runs on fixed monotonic deadlines. It calls the pure DB collector/formatter and submits the resulting text to the bounded asynchronous Telegram dispatcher. Collection and delivery failures retain the previous corrected-UTC window start so the next successful pass has no observation gap.

The 2026-09-11 operator rework routes source and result notices through the shared dispatcher.
The digest still uses its own DB connection and schedule, separate from the funded loop and exit sweep.
See `2026-09-11-memecoin-operator-rework-design.md` for durable source and result delivery.

## Verification

- Unit tests cover corrected windows, future-row exclusion, severity, stale reservations, feed/mark health, breaker state, bounded formatting, queue refusal, and fail-soft collection.
- A runner wiring test proves the worker is disabled by default and that enabled startup creates a background thread rather than performing a synchronous send.
- The full memecoin suite, schema validator, compileall, `git diff --check`, pinned deploy verification, and sustained heartbeat check are required before activation.
