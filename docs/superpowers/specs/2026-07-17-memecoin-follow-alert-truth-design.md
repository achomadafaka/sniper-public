# Memecoin Follow-Alert Truth Design

**Status:** Approved 2026-07-17

## Problem

Fresh `follow_activity` buys currently send Telegram before invoking the funded real-entry hook.
The network send therefore delays the decision, and the message can only show the raw follow
payload: blank ticker, zero market cap, and a wallet-level `REAL-ELIGIBLE` badge. It cannot show
whether an entry was attempted, refused, reserved, submitted, or filled.

The 2026-07-17 `youwish`/`Ge87Ets...pump` incident proves the failure:

- the alert showed `?`, `mcap ~$0`, and `REAL-ELIGIBLE`;
- Telegram completed before the real decision started, adding 5.036 seconds;
- real attempt `11a13062d19d4360846db6661e5f4d08` then finished in 29 ms;
- the attempt was correctly refused as `skipped_chase`: the coin was 5.2x above first-seen price;
- chain-aware local history already had ticker `Jimothy` and non-zero market-cap evidence.

## Goals

- Start every enabled follow-lane funded decision before notification work.
- Remove Telegram network I/O from the follow entry path.
- Make one follow alert state the exact funded outcome and deterministic reason.
- Replace raw blank ticker/zero market cap when chain-aware local evidence exists.
- Preserve exact `(network_id, mint)` identity and `origin_event_id` attribution.
- Preserve paper fanout, reservation, safety, duplicate-submit, kill-switch, and accounting behavior.

## Non-goals

- No entry-gate, sizing, TP, SL, exit, wallet-arming, or adaptive-policy changes.
- No new price, quote, metadata, Telegram, or other network request.
- No pooling of `global_feed` and `follow_activity` alert-latency instrumentation.
- No attempt to guess an EVM chain or ticker from address shape.

## Design

### Ordering

For each accepted follow buy:

1. persist the source event;
2. fan out the existing paper arms;
3. invoke the real hook exactly once when the follow real lane is enabled;
4. read the completed attempt by exact `origin_event_id`;
5. build one enriched alert;
6. enqueue the alert on a bounded daemon sender queue.

Real-hook exceptions remain isolated from paper processing. They produce an explicit
`FAILED / hook_exception` alert state rather than erasing the attempt result. A disabled real lane
produces `NOT ATTEMPTED / follow real lane disabled`.

### Exact attempt attribution

Add a chain-neutral indexed lookup on `memecoin_entry_attempts(origin_event_id)`. The follow alert
reads only the exact event's attempt. It never falls back to latest wallet, mint, or chain attempt.

The alert context carries:

- `entry_status` and `entry_reason` from the completed trace when present;
- `attempt_id`;
- `pending_id`, `tx_sig`, and `position_id` when reached;
- a stage label derived from persisted facts: `pre-reservation`, `reserved`, `submitted`, or
  `position persisted`.

If the hook returns but no trace exists, the message says `NOT ATTRIBUTED / attempt trace missing`.
It does not invent an attempt ID or stage.

### Local metadata enrichment

The alert uses the event's own non-empty ticker and positive market cap first. Missing values may
be filled only from local chain-aware database evidence for the same `(network_id, mint)`:

- ticker: latest prior non-empty event ticker in the same local observed-clock ordering;
- market cap: existing `resolve_mint_mcap(..., network_id=...)` non-zero median.

Unknown values remain `?`; zero is never rendered as a real market cap. No resolver may omit
`network_id`, and ambiguous legacy EVM events remain unresolved.

### Message semantics

`REAL-ELIGIBLE` is replaced with `ARMED`; arming states only that the wallet may enter the funded
decision path. The entry line owns the result:

```text
🟠 Jimothy — youwish bought
size ~$1.3k | mcap ~$913k | chain solana
fomo: 2026-07-17T06:39:58.602Z (feed lag 38.5s)
wallet: ARMED ⚡ · honest αb -$26/10c · gate ✗
entry: REFUSED · skipped_chase · pre-reservation
reason: coin ran 5.2x above first-seen price (cap 2.0x)
attempt: 11a13062d19d4360846db6661e5f4d08
mint: Ge87EtsjwRQbHaqQmKRno69RFTwh9bfSsm99XNxTpump
```

Outcome color is deterministic: green for confirmed fill/position, orange for refused or failed,
yellow for pending/unknown/not attempted. Global-feed messages built before a decision also use
`ARMED`, never claim entry eligibility, and show no fabricated outcome.

### Non-blocking notification

Production follow alerts use a bounded in-process queue with one daemon worker. Enqueue is
non-blocking. The worker calls the existing `send_telegram`; this moves the existing request rather
than adding one. An injected `alert_fn` remains synchronous as a test seam only.

If the queue is full, the runner logs a deterministic drop warning. Notification failure never
changes the entry, reservation, trace, paper position, or accounting result.

## Tests

- A fresh armed follow alert invokes the real hook before the alert callback.
- A refusal alert contains exact status, reason, attempt ID, and `pre-reservation`.
- A filled attempt renders submitted/persisted facts without guessing missing stages.
- A disabled real lane renders `NOT ATTEMPTED`.
- Blank ticker and zero market cap are enriched only from same-chain local evidence.
- Same EVM address on another chain cannot enrich the alert.
- Production dispatch enqueues without calling Telegram on the entry thread.
- Queue saturation is fail-open for trading and logs the drop.
- Existing follow copy, real-hook, holder-evidence, Telegram, schema, and concurrency tests remain
  green.

## Acceptance

- No Telegram network call precedes or blocks the follow real hook.
- Every alerted armed follow buy states one explicit entry outcome or `NOT ATTEMPTED`.
- Exact attempt ID and terminal reason appear whenever the immutable trace exists.
- Alert identity and enrichment remain chain-aware.
- No additional hot-path network request and no funded-execution semantics change.
