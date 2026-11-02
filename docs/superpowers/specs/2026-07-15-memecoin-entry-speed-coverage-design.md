# Memecoin Entry Speed and Coverage Design

Status: implementation-approved by the 2026-07-15 one-shot task
Branch: `feat/memecoin-entry-speed-coverage`
Runtime baseline: pinned release `0f2bc6b5d2eb4778f48f3e3f577d3173bca0f6ae`
Source baseline: `7f1f2aa8497bc909f9711341ff0572ba802733d3`

## Objective

Reduce source-observation-to-submission latency and explain every funded entry outcome without weakening identity, reservation, safety, kill-switch, clock, submission, or accounting contracts.

This project does not change paper verdicts, exits, sizing, adaptive policy, wallet approval, safety thresholds, slippage thresholds, or retry/resubmit behavior.

## Verified baseline

Evidence is from the read-only live `sniper_tracker.db`, current code, and the pinned runner. The retained funded sample has 79 reservations and 25 confirmed fills.

`origin/main` advanced while the design was being implemented. The feature branch was rebuilt from `7f1f2aa8`, preserving four relevant landed changes: `b0c80554` adds explicit network provenance to quote/alert/execution instruments; `3d4b4315` prevents a transient EVM slot-scan RPC failure from poison-caching a token as unsellable; `8f5d0735` threads the measured mcap-tiered Solana impact cap; and `7f1f2aa8` makes wallet arming global while moving unsupported chains behind an explicit venue-context refusal. The pinned funded runtime remains `0f2bc6b5`; no before/after live claim attributes those source-only fixes to the running process.

| Chain | Reservations | Confirmed | Failed | Submitted | Stage sample | p50 total | p95 total | p95 safety | p95 execution |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Solana | 23 | 11 | 12 | 12 | 22 | 2,157.9 ms | 8,982.9 ms | 511.7 ms | 8,465.2 ms |
| Base `8453` | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a |
| BSC `56` | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a |
| Ethereum `1` | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a |
| Robinhood `4663` | 56 | 14 | 42 | 14 | 14 | 10,677.3 ms | 11,070.6 ms | 2,591.1 ms | 8,595.7 ms |

Robinhood failures are 42 token-safety refusals. Solana failures include seven impact-cap refusals, one token-safety refusal, and one submitted attempt whose executor reported `unknown`. Legacy persistence cannot deterministically classify every pre-reservation refusal by chain and signal source.

Global-feed observation-to-reservation p50 is 127 ms on `4663` and 156 ms on Solana for nearest-event legacy matches. This is not exact attribution and is not used to justify path reordering.

Attribution coverage is the blocking defect:

- 0/25 funded positions have `origin_event_id`.
- 1/25 funded positions have source-price/total-drift attribution.
- 0/25 funded fills have the required full stage trace.
- `memecoin_exec_latency` records only completed executor calls. It omits approval, mcap, staleness, chase, reservation, safety refusal, quote/sign/submit/confirm boundaries, and persistence completion.

## Proven bottlenecks

Ranked by `p95 latency x affected accepted reservations` where a stage sample exists:

1. Robinhood execution: `8,595.7 ms x 14 = 120.3 entry-seconds` in the completed sample. `erc20_balance()` performs immutable `decimals()` reads on every pre/post balance read; the quote-token decimals are already fixed in `CHAIN_QUOTE`.
2. Robinhood safety: `2,591.1 ms x 14 = 36.3 entry-seconds`. Current main already fixes the measured transient-RPC poison cache from `3d4b4315`; this branch preserves and audits that behavior. A separate process-global executable-quote cache can still reuse a stale quote across attempts. Quote caching is forbidden; only within-attempt reuse is valid.
3. Solana execution has a long tail (`p95 8,465.2 ms`) driven by confirmation/unknown behavior. Confirmation and no-resubmit semantics are unchanged in this project.
4. Unresolved EVM routing resolves before approval and then resolves again inside `real_entry()` because the resolved identity is not propagated. The retained data does not contain an exact unresolved accepted-attempt sample, so this is a correctness/coverage fix, not a claimed latency number.

## Design

### 1. Canonical coin identity

The canonical identity is `(network_id, mint)`.

- `4663` always means Robinhood. It is never collapsed into generic EVM.
- A concrete event `network_id` is authoritative and travels unchanged.
- Address shape distinguishes only Solana-family from EVM-family.
- An unresolved EVM event first checks indexed, unambiguous event history. Conflicting history is terminal `ambiguous_network`; it is never sent to an address resolver. Missing history may use the existing external resolver fallback once.
- Reservations and open-position uniqueness use normalized `(network_id, mint)` keys. Legacy base58 rows with no network normalize to `solana`; legacy `0x` rows with no network normalize to `evm-unknown`.
- The global/feed and follow-activity threads share the same database uniqueness guard. No in-memory dedup is trusted.

### 2. Exact origin propagation

For each new funded buy attempt, carry these fields through event, reservation, executor, position, fill, latency, and trace rows:

- `origin_event_id`
- `attempt_id`
- `signal_source`
- `network_id`
- `mint`
- exact event `source_price`
- exact event `source_market_cap`
- observed receipt timestamp and clock domain

New rows never look up source price by trader/chain. `resolve_mint_their_price()` remains only a labeled legacy fallback for old/manual reservations that have no exact event price.

### 3. Immutable attempt trace

`memecoin_entry_attempts` has one row per funded buy handed to the real hook. A shell row is inserted before routing so a process failure still leaves evidence. Stage values are collected from one `time.perf_counter()` domain. Absolute stage timestamps are derived from one reconciled real-UTC decision-start anchor plus monotonic elapsed time; claimed upstream time is stored separately and never used as a local stage clock.

Stages:

1. observed receipt
2. decision start
3. chain resolution complete
4. market-data lookup complete
5. reservation complete
6. safety checks complete
7. quote complete
8. signing complete
9. transaction submitted
10. chain confirmation/inclusion complete
11. balance-delta read complete
12. position/fill persistence complete

The row stores absolute timestamps and per-stage durations. A stage not reached remains `NULL`. Every normal return finalizes one `terminal_status` and `terminal_reason`. The finalizer updates only a row with `terminal_status IS NULL`; finalized traces cannot be changed.

`global_feed` and `follow_activity` remain separate dimensions. Claimed source lag is reported but not mixed with local observation-to-stage latency.

### 4. Quote and drift attribution

For buys, positive drift means we paid more:

- source drift = `(pre-trade executable quote - source price) / source price * 100`
- execution slippage = `(actual fill - pre-trade executable quote) / pre-trade executable quote * 100`
- total copy drift = `(actual fill - source price) / source price * 100`

The trace/fill stores quote price, quote observation timestamp, quote age at confirmation, and a deterministic hash of chain, mint, sell amount, buy amount, route target, and quote timestamp. Missing source/quote/fill prices leave the corresponding drift `NULL`; no zero is synthesized.

Solana derives executable quote price from Jupiter `inAmount`/`outAmount` plus decimals already returned by the first token-balance response. EVM derives it from 0x `sellAmount`/`buyAmount`, the chain's fixed quote-token decimals, and a bounded immutable token-decimals cache. No extra network request is added for attribution.

### 5. Measured request removal

The EVM context owns a bounded LRU cache for immutable ERC-20 decimals. The quote token uses `CHAIN_QUOTE[chain]["decimals"]` without RPC. A memecoin's decimals are read once per process/chain and then reused by safety, quote attribution, and balance conversion.

Executable quotes, balances, safety verdicts, kill-switch state, capital, gas prices, nonces, and prices are never cached. The EVM safety route quote is reused only inside one `token_safety_fn(mint)` call and discarded at return.

The storage-layout cache remains distinct from executable quotes. Preserve `3d4b4315`: only a clean completed scan may negative-cache an undiscoverable layout; transient transport failure fails closed for that attempt and is never cached.

Tests assert exact RPC/request counts before and after the cache seam. The funded branch is not declared latency-proven until a canary deployment records chain-specific post-change p50/p95 from real attempt traces.

### 6. Read-only reporting

`scripts/report_memecoin_entry_baseline.py` reads the new trace first and emits a per-chain/per-source funnel, stage p50/p95, drift coverage, and terminal reasons. For legacy rows it reports only fields that can be joined exactly; unavailable categories print `unknown`, never inferred zeroes.

## Error handling

- Explicit supported network but no context: terminal `unsupported_or_unarmed_network`; identity remains concrete.
- Missing EVM network with no unambiguous evidence: terminal `unresolved_network`.
- Conflicting event history: terminal `ambiguous_network`.
- Reservation refusal: terminal status `refused`, exact risk reason.
- Safety failure: terminal status `safety_failed`, exact reason, reservation released.
- Quote/sign/send/confirmation/balance/persistence outcomes retain their existing release-or-reserve behavior. Instrumentation cannot authorize release or retry.
- `unknown`/timeout keeps the reservation and records one terminal reason. It never resubmits.
- Trace write failure is logged and cannot alter funded decisions. Existing pending/position/fill ledgers remain authoritative money state.

## Validation

Required tests cover:

- explicit `4663` propagation without any resolver call;
- local unambiguous fallback and ambiguous refusal;
- `(network_id, mint)` reservation/open uniqueness under global-feed/follow-activity races;
- exact event source price beating the legacy latest-trader lookup;
- quote hash/age and all three buy drift formulas;
- missing stages remain `NULL` and every normal non-fill has one terminal reason;
- monotonic derived timestamps and nonnegative durations;
- quote/safety state is not cached across attempts;
- immutable decimals remove repeated RPC calls;
- no changes to reservation arithmetic, gates, sizing, exits, policies, or resubmit behavior.

## Deployment gate

Do not merge or deploy the funded runner from this task. After review approval, deploy as a pinned canary, collect at least 20 submitted attempts per active chain where flow permits, and compare p50/p95 by chain and signal source. Base, BSC, and Ethereum remain `insufficient_sample` until real attempts exist; no fixture result may be presented as funded latency proof.

## Implementation status — 2026-07-15

Implemented on `feat/memecoin-entry-speed-coverage`; not deployed. The branch adds exact-event
attempt traces, exact quote/fill drift, chain-aware duplicate guards, bounded immutable EVM
metadata caching, and the read-only reporter. Live before/after latency remains unproven until the
pinned canary gate above. The initial reporter left point-in-time curated eligibility unknown
instead of reconstructing it from mutable rosters or nearest events.

## Measurement correctness addendum — 2026-07-15

The follow-up branch `fix/memecoin-entry-measurement-correctness-current` corrects four telemetry
gaps without changing a funded decision:

- `memecoin_events.real_decision_at` reuses the hook's reconciled decision-start timestamp. Direct
  helper callers fall back to `real_utc_now_iso()`. Historical rows are not backfilled.
- A truly missing market cap remains fail-closed, but its new deterministic terminal reason is
  `mcap_unresolved`, distinct from `unresolved_network`.
- Reporter schema v2 reads point-in-time `was_curated` and `was_real_approved` from
  `memecoin_feed_observations_v2` and joins eligible signals to attempts only by exact
  `upstream_event_id = origin_event_id`. Follow activity remains separate and honestly reports its
  point-in-time eligibility as unavailable.
- Reporter schema v2 derives receipt-to-decision, receipt-to-submission, and receipt-to-terminal
  latency only from timezone-aware `observed_real_utc` timestamps with nonnegative deltas. Missing
  stages and invalid clock pairs are excluded with named reasons; no zero is synthesized.

Read-only live evidence and the Knowledge Sync audit are recorded in
`docs/audits/2026-07-15-memecoin-entry-measurement-correctness.md`. This branch is not deployed and
makes no funded before/after latency claim.
