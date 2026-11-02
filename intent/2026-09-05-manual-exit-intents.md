# Intent: Every operator sell runs in the funded runner, never in the web process

**Author:** Pablo (operator) with Alpha (Claude), drafted from the 2026-09-05 data-source audit
**Status:** approved — implemented the same day

## Problem
The dashboard's Close and Sell-a-fraction buttons executed on-chain sales inside the web server
process. That process captures its settings at import and is not restarted by funded deploys, so a
manual sell could run on a stale exit policy or a stale RPC-pool state. Manual buys were moved to a
runner-drained intent queue on 2026-08-26 for exactly this reason (position 44706); sells were not.

## Proposed outcome
A manual Close or partial Sell writes an intent row (position, action, fraction, who asked) and the
funded runner claims it within one pass, executes it with its own venue exit executors, and books
the outcome. The dashboard polls the intent and shows the result. The web process can no longer sell.

## Affected users and systems
Operator dashboard (`static/app.js`), web routes (`sniper/routes/api_memecoin.py`), web context
(`sniper/web/server.py`), runner loop (`scripts/memecoin_runner_loop.py`), ledger
(`memecoin_manual_exit_intents`).

## Constraints
- Exits are never gated by the kill switch; the queue must not be either.
- The runner loop never sleeps for an operator request: the old 2/5/10/30 s retry ladder becomes
  re-queues across passes, and a result carrying a signature is never retried.
- Paper positions keep their existing modeled close; only real positions cross as intents.
- Bounded drain (≤2 per pass) so a backlog cannot starve the rail sweep.

## Open questions
- Should the dashboard offer "cancel" on a queued intent before the runner claims it? (not built)
- Telemetry: intent-to-claim latency is recorded; alerting on it is not.
