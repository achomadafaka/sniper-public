# Memecoin Strategy Engine v0 - Design

**Date:** 2026-07-11
**Status:** Approved for implementation by operator directive
**Scope:** Immutable paper candidates, deterministic live/replay evaluation, controls, statistical reads, and gated exit search

## Goal

Turn entry predicates and exit policies into immutable data, evaluate them identically live and offline, and learn whether a signal has net forward value without giving generated candidates a path to real execution.

## Safety Boundary

Candidate code lives under `sniper/research/memecoin_strategy/` and may import persistence, pure pricing models, and clock helpers. It may not import `sniper.execution`, `memecoin_real_leg`, or `memecoin_real_runner`. Candidate positions use separate tables. Database constraints require `execution_mode='paper'` and `timecap_s > 0`.

## Registry

`memecoin_strategy_candidates` stores:

- candidate id, name, status, spawn index, generation, and parent id;
- canonical predicate, exit, sizing, cost, control, and gate JSON;
- SHA-256 parameter hash, born time, discovery cutoff, and capture-version floor;
- alpha allocation, read gate, kill gate, and immutable notes.

SQLite triggers reject updates to immutable columns. A changed parameter creates a new candidate. At most five rows may be in `candidate_active`; enrollment is transactional and ranked candidates wait when no slot is free.

## Predicate DSL

v0 is typed, allowlisted, and AND-only. Operators are `lt`, `lte`, `gt`, `gte`, `eq`, `in`, and `between`. Fields are entry-time facts only: network, market cap, size, source cohort flags, distinct buyer count, window seconds, thesis stance/score, source lag, token age, liquidity, and event type. Unknown values fail the predicate unless the predicate explicitly tests a nullable enum value.

Temporal features are built by the same pure event processor in live and replay modes. The live adapter supplies prior observations from v2 tables; replay supplies the same ordered records from memory. Golden tests require identical match IDs and feature payloads.

## Ledgers

- `memecoin_candidate_signals`: every match and rejection with source observation ids, feature JSON, reason, episode key, and parameter hash.
- `memecoin_candidate_positions`: paper-only entry, open/close state, costs, and origin signal.
- `memecoin_candidate_fills`: modeled fills with observation id, source, cost, and timestamp.
- `memecoin_candidate_reads`: immutable kill/read/promotion evidence with manifest and hash.

Episode key is `(candidate_id, network_id, normalized_mint, UTC day)`. At most one entry per key. Backlog observations older than the configured freshness limit can be replayed but cannot open a live paper position.

## Seed Candidates

### `cluster_momentum_v0`

- Trigger on the second distinct `was_curated=1` buyer of the same network/mint within 1,800 seconds.
- Market cap must be present and below $5,000,000.
- Trigger observation age must be at most 120 seconds live.
- One episode per coin per UTC day.

### `thesis_momentum_v0`

- Trigger on receipt of a curated trader thesis classified as winning stance with score at least 20.
- Market cap must be present and below $5,000,000.
- Thesis receipt age must be at most 120 seconds live and must precede entry.
- One episode per coin per UTC day.

Both use fixed paper notional $100, TP 60%, stop 50% after four qualifying observations, 24-hour hard time cap, no mirror dependency, and modeled entry/exit costs. These values validate machinery; they are not asserted optimal.

## Controls

At signal time, choose a control only from earlier-observed eligible events in the same network, market-cap band, and 30-minute clock window that did not satisfy the predicate and do not share the signal mint. Selection is deterministic by smallest absolute time difference then stable observation id. If none exists, record `control_missing`; never choose a future event or silently drop the candidate signal from the denominator.

## Lifecycle

- Automatic: ranked specification to `candidate_active` when a slot exists.
- Automatic kill: net cumulative PnL at or below -$150, or at least 25 closes with non-positive net EV, or fewer than 10 distinct coins after 30 days.
- Fixed read: at least 40 closes, 20 distinct coins, 10 distinct UTC days, and adequate paired-control power.
- Alpha allocation: `base_alpha / (spawn_index * (spawn_index + 1))` over all candidates ever spawned.
- Statistical read: per-coin candidate-minus-control net-return deltas, positive mean and median, adjusted lower confidence bound above zero, and one-sided paired sign-flip p-value at allocated alpha.
- Human gate: candidate to first-class paper.
- Fresh evidence: first-class paper starts a new forward cohort after approval. Discovery/candidate evidence cannot promote it to real.
- Human gate: first-class paper to real, outside v0.

## Offline Exit Search

The search engine consumes ordered v2 quote observations, not six horizon marks. It simulates first-touch ordering for TP, stop confirmation count, and time cap, subtracting modeled costs. Search is locked until all gates pass:

- at least 21 distinct days;
- at least 200 complete 24-hour paths;
- at least 100 distinct coins;
- at least 90% expected-observation coverage including budget gaps.

The grid is TP 20-200 by 20, stop 20-80 by 10, stop confirmations 1/2/4, and time caps 1h/6h/12h/24h. Training selects once; a later chronological forward window scores unchanged parameters. Thin strata shrink toward the global policy. The engine may auto-enroll a surviving generated candidate only into the capped paper candidate pool. While gates fail, it returns `accruing` with exact deficits and creates nothing.

## Tests

- DSL type/operator/unknown-field rejection and canonical hash stability.
- Immutable trigger and five-slot transactional cap.
- Live/replay match parity including event ties.
- Cluster distinct-wallet rule, same-chain rule, stale event, and daily episode dedup.
- Thesis chronology, score, cohort, and stale event.
- Past-only matched control and explicit missing control.
- Paper-only import/DB constraints and positive time cap.
- Exit ordering, costs, missing observations, and stop confirmations.
- Data-gate deficits, chronological split, shrinkage, no-spawn while accruing, and capped auto-enrollment.
- Kill/read gates, alpha allocation, and no evidence reuse across promotion stages.
