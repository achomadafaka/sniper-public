# Hyperliquid Evidence Closure Design

**Date:** 2026-07-29
**Authority:** observational paper and research only; no keys, orders, funded imports, or funded-state changes
**Status:** approved design

## Objective

Close five evidence-integrity gaps without changing the frozen copy or alt-short entry/exit policy:

1. recover exact provider casing for the available historical alt-short universe while keeping the
   missing delisted tail censored;
2. measure source-event latency prospectively instead of treating polling detection time as source
   time;
3. give every Hyperliquid PnL surface one funding sign and interval contract;
4. prevent repeated nominations from one 72-hour trough episode while retaining the repeats as raw
   observations;
5. remove the client-side 15-level capacity ceiling and make provider-depth censoring explicit.

These changes improve measurement. They cannot promote a strategy, authorize funded execution, or
retroactively manufacture evidence that was never captured.

## Non-goals

- No requester-pays archive access or third-party reconstruction of delisted history.
- No backfilled historical source timestamps.
- No change to the 30% run-up, 72-hour lookback, funding percentile, exits, clip, concurrency, or
  arm evidence milestones.
- No additional polling sweep across the 225 legacy copy targets.
- No claim that Hyperliquid's returned L2 snapshot is the complete exchange book.
- No destructive column rename or rewrite of sealed reports.

## 1. Historical provider identity and censored tail

The corrected cache uses the exact case-sensitive names from `metaAndAssetCtxs.universe.name`.
Existing uppercase cache files may be reused only when the current provider symbol is byte-identical
to the filename identity and the file checksum is recorded. The seven affected lowercase-`k`
contracts are fetched under their provider-exact names into a new cache version. Old cache and report
versions remain immutable.

The cache seal contains:

- provider-exact universe and its SHA-256;
- study interval;
- per-symbol candle/funding path, SHA-256, first/last timestamp, row count, and coverage status;
- source endpoint and retrieval timestamp;
- `delisted_tail_status="censored_archive_declined"`;
- the invalidated report SHAs that this cache does not overwrite.

Analysis may emit live-universe sensitivity output from this cache. Its top-level evidence status is
`partial_censored`; it must not emit `valid`, `edge`, `pass`, or promotion language while the 37-name
delisted tail is unavailable. Symbols with incomplete in-window candles or funding remain censored,
not zero-filled.

## 2. Prospective source-event latency

### Scope

Only the immutable nine-wallet copy arm is instrumented. Hyperliquid permits ten unique users across
user-specific WebSocket subscriptions; legacy targets remain explicitly `not_instrumented`. The
copy runner owns one background source-fill subscriber and one bounded in-memory queue. The main
runner remains the sole SQLite writer.

The subscriber requests `userFills` for each arm wallet. Initial snapshots establish a baseline;
they are persisted but never presented as newly observed live events. Reconnects use bounded
`userFillsByTime` recovery from the last persisted source timestamp. REST recovery is exceptional,
passes through the shared IP token bucket, and never becomes another periodic scan.

### Persistence

Add append-only `hl_copy_source_events`, uniquely keyed by `(wallet, tid)`:

- provider fields: wallet, `tid`, coin, side, direction, price, size, start position, order ID,
  transaction hash, and `source_event_ts_ms=fill.time`;
- observation fields: `received_ts_ms`, corrected clock domain, snapshot flag, recovery flag,
  connection epoch, and persisted timestamp;
- match fields: `copy_id`, `match_status`, match reason, and matched timestamp.

Add nullable timing fields to `hl_copy_book`:

- `source_event_ts_ms`;
- `source_received_ts_ms`;
- `decision_ts_ms`;
- `book_request_ts_ms` and `book_received_ts_ms`;
- `mirror_open_ts_ms`;
- `source_latency_status` and `source_event_tid`.

Entry matching is causal and fail-closed. A fill must be newer than the wallet baseline and match
wallet, provider-exact coin, opening direction, and the observed position delta. Multiple plausible
fills produce `ambiguous`; no plausible fill produces `unmatched`. Only `matched` rows enter latency
aggregates. Existing copy rows remain `historical_not_captured` with all source fields `NULL`.

Reported components are:

```
source_to_receive = source_received - source_event
receive_to_decision = decision - source_received
decision_to_book = book_received - book_request
book_to_persist = mirror_open - book_received
source_to_mirror = mirror_open - source_event
```

Negative values, mixed clock domains, snapshots, ambiguous matches, or recovery events are excluded
from headline latency and retained for audit.

### Failure and performance behavior

- Subscriber failure does not block copy reconciliation or paper entries.
- Queue size is bounded; overflow is counted and marks the affected interval incomplete.
- Reconnect uses exponential backoff with jitter.
- The runner performs no more recurring REST calls than before this change.
- Heartbeat exposes connection state, instrumented wallet count, queue depth, last event age,
  reconnect count, recovery count, unmatched count, and overflow count.

## 3. Canonical funding accounting

The canonical field is `funding_pnl_bps`: positive improves strategy PnL; negative reduces it.
Every net equation becomes:

```
net_bps = gross_bps + funding_pnl_bps - explicit_cost_bps
```

The canonical hold interval is `(entry_ts_ms, exit_ts_ms]`. Hyperliquid applies funding at the end
of each hourly interval, so the entry instant is excluded and the exit instant is included. A row at
the exact exit millisecond with unresolved event ordering produces `ambiguous_boundary` and keeps
net PnL unknown.

A shared pure helper returns value plus provenance:

- `complete`: all expected hourly prints present;
- `not_applicable`: no funding boundary occurred, canonical value `0.0`;
- `missing`: one or more expected prints absent;
- `ambiguous_boundary`: exact-boundary order cannot be established;
- `fetch_failed`: provider request failed.

`missing`, `ambiguous_boundary`, and `fetch_failed` return `funding_pnl_bps=NULL`.

Migration is additive:

- add `funding_pnl_bps` and `funding_provenance_status` to copy and paper position rows;
- new writes populate canonical and legacy columns atomically;
- copy legacy `funding_bps` remains cost-signed, so `funding_bps=-funding_pnl_bps`;
- alt-short legacy `funding_bps` remains contribution-signed, so
  `funding_bps=funding_pnl_bps`;
- readers prefer canonical and may fall back to legacy only with table-specific provenance;
- no legacy column is removed in this release;
- historical `NULL` never becomes zero.

Startup and tests assert the dual-write relation. Any mismatch is excluded from scorecards with
`funding_provenance_status="legacy_mismatch"`.

## 4. Repeat-nomination episode identity

The frozen threshold crossing remains unchanged. Each completed-hour calculation also identifies
the exact trough used as the minimum of the preceding 72 completed closes. Ties use the earliest
timestamp. The episode identity is:

```
sha256(arm_id | provider_coin | trough_ts_ms | trough_price)
```

The first upward 30% crossing for an episode is the only portfolio-eligible nomination. Later
crossings using the same trough are persisted with:

- the same `episode_id`;
- `episode_primary=0`;
- `portfolio_slot_eligible=0`;
- `entry_status='stood_down'`;
- `entry_reason='repeat_same_episode'`.

An episode resets only when the causal 72-hour trough identity changes, either because a new trough
forms or the old trough leaves the rolling window. No wall-clock cooldown, hidden hysteresis, or
operator reset exists. Open positions remain protected by the existing one-open-position rule.

Episode identity, reset rule, and primary/repeat classification become arm semantics. Existing rows
are not reclassified in place; the migration may annotate them in a separate provenance report.

## 5. Capacity and depth censoring

`compact_book` stores all provider-returned bid and ask levels up to a defensive 100-level cap. It
also stores:

- returned and stored level counts per side;
- returned and stored notional per side;
- `client_truncated`;
- `depth_scope="provider_returned_snapshot"`;
- capture timestamp when supplied.

The current provider response is not assumed to be full depth. Capacity results become structured:

- `measured`: the requested fill and cost threshold are resolved within stored levels;
- `lower_bound`: all stored qualifying levels are consumed while more provider depth may exist;
- `censored_depth`: the requested notional cannot be resolved from the returned snapshot;
- `unavailable`: no valid book.

`censored_depth` is not a capacity rejection. Scorecards may show confirmed executable sizes and
lower bounds but must not convert a censored result into zero capacity. Existing 15-level snapshots
remain tagged `legacy_client_truncated_15` and are never pooled as equivalent to new snapshots in
capacity summaries.

The 100-level guard bounds row size and CPU. Fill math stays linear in captured levels. No extra L2
request is added.

## 6. Runtime, UI, and compatibility

- All schema changes are additive and idempotent.
- The copy runner remains observational paper only and keeps the current 30-second reconciliation.
- The alt-short paper runner remains the sole writer of `hl_altshort_paper.db`.
- Edge Desk/API exposes provenance counts before latency, funding, episode, or capacity summaries.
- Old clients continue receiving legacy fields during the compatibility window.
- New fields are nullable so deployment can switch code before forward evidence exists.
- `hl_real` remains retired; no real runner is launched.

## 7. Verification and deployment gates

Implementation uses failing tests first. Required coverage:

1. exact provider casing, sealed cache identity, partial-censored report status, and immutable old
   artifacts;
2. source-fill baseline/reconnect/dedup/matching/ambiguity/clock-domain behavior and bounded queue;
3. canonical funding signs, `(entry, exit]` boundaries, missing-print censoring, dual-write
   compatibility, and scorecard exclusion;
4. BOME-shaped repeated crossings sharing one trough, trough-change reset, raw-repeat persistence,
   and unchanged primary crossing behavior;
5. 20-level capture, 100-level cap, legacy tagging, structured capacity status, and no false zero;
6. schema migration from production-shaped temporary databases;
7. copy, alt-short paper, scorecard, Edge Desk/API, rate-limit, watchdog, and schema suites;
8. `git diff --check`, compile checks, and database integrity checks.

Deploy only the merged clean SHA through the Hyperliquid and web pinned-runtime scripts. Verify both
runtime symlinks, manifest SHA, exact process counts, fresh heartbeats, authenticated Edge Desk/API,
source subscriber health, schema columns, DB integrity, rate-limit health, log errors, and zero
funded/`hl_real` processes. Forward latency may correctly have zero matched events immediately after
deployment; health is proven by an active subscription and baseline state, not fabricated samples.

Rollback restores the previous pinned release and process set. Additive columns and append-only
source events remain harmless and readable by the prior release.
