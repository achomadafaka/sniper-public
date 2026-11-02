# Memecoin Telegram Chain Truth Remediation

**Date:** 2026-08-05
**Status:** approved for implementation

## Problem

Telegram buy alerts currently overstate funded authority and paper evidence:

- `_prepare_alert()` treats approval on any funded chain as `ARMED` on every signal chain.
- It displays pooled `alpha_b` evidence even though funded authority and evidence gates are chain scoped.
- `alpha_b_copy_stats()` and `alpha_b_copy_stats_all()` exclude Fomo-priced fills but still count
  censored or otherwise unmeasurable closes.
- The asynchronous Telegram dispatcher has no durable record of the rendered content or send result.
- A front-run alert does not say plainly that the source buy arrived before our funded entry decision.

These are truth and observability defects. They do not justify changing strategy, routing, sizing,
capital controls, or funded authority.

## Decisions

### Exact-chain authority

For each alert, resolve the signal network through the shared chain registry. A wallet is `ARMED`
only when that exact canonical chain is present in `wallet_armed_chains()`.

- Funded and armed on the signal chain: `ARMED`.
- Funded but not armed on the signal chain: `watch-only`.
- Unsupported/unfunded signal chain: `unfunded chain - watch-only`.

The message may list configured chains for diagnosis, but approval on another chain never confers
authority on the signal chain.

### Chain-scoped measurable evidence

On funded chains, `_prepare_alert()` calls `alpha_b_copy_stats(..., chain=<resolved chain>)`.
Unsupported chains do not show a promotion verdict; they render `paper gate: not applicable`.

The shared `alpha_b_copy_stats()` and `alpha_b_copy_stats_all()` queries use
`measurable_outcome_sql()`. This makes every consumer exclude Fomo provenance, missing fill
provenance, non-executable/noquote closes, and unquotable closes without a usable mark. No historical
PnL is rewritten. The change only stops unmeasurable rows from being used as evidence.

### Alert timing language

Front-run alerts say `source buy received - our entry decision pending` until an attributed entry
result is attached. The alert remains an observation, not a funded fill claim.

### Delivery audit

The dispatcher writes one append-only audit row from its worker thread for each dequeued message.
The funded caller remains non-blocking and does not open or write a DB connection.

Each row stores:

- message kind and optional source identifier;
- queued, send-started, and completed corrected-UTC timestamps;
- primary-chat result and elapsed milliseconds;
- redacted rendered text;
- SHA-256 of the exact unredacted rendered text;
- a bounded error class when the sender raises.

The redactor replaces the value of every `tap:` line. Signed one-tap URLs are never persisted.
The exact hash allows a supplied Telegram export to be verified without storing a reusable token.

Audit insertion/update failures are logged and fail soft. They never block, retry, or alter a funded
entry/exit. Queue saturation remains visible as a warning; it cannot become a synchronous DB write on
the funded path.

Front-run sends keep their existing `memecoin_alert_latency` record. The generic delivery table covers
the operational dispatcher path; the two tables serve different purposes and are not merged.

## Compatibility

`enqueue_telegram(text)` remains valid. Optional keyword metadata adds kind/source identity without
forcing all callers to change at once. `send_telegram()` keeps its `(ok, elapsed_ms)` primary-chat
contract; follower delivery remains best effort and is not represented as primary success.

## Verification

Regression tests must prove:

1. Approval on Solana does not render a Robinhood signal as armed.
2. Funded-chain alerts use exact-chain evidence and gate status.
3. Unfunded-chain alerts render authority and gate as not applicable.
4. Censored/noquote, orphan-close, unquotable-without-mark, and Fomo rows are excluded from both
   single-wallet and batched `alpha_b` statistics.
5. Measurable executable closes remain counted.
6. Front-run alerts state that the entry decision is pending.
7. Delivery audit redacts tap URLs, preserves the exact message hash, and records success/failure.
8. Audit failures and queue saturation do not raise into the caller.

Focused tests run first. Existing memecoin alert, authority, arm-matrix, and persistence suites run
afterward. No live provider call, production DB mutation, restart, or deployment is part of local
verification.

## Rollout boundary

The fix is not live until merged and deployed through `scripts/deploy_memecoin_runtime.sh` from the
pinned merged SHA. Live verification then requires the release manifest, actual funded process,
fresh heartbeat, one organic alert on each funded chain available in the observation window, and a
matching delivery-audit row. Historical Telegram messages are not backfilled.
