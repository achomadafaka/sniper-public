# Memecoin Entry Radar and Coin-State Evidence Design

**Date:** 2026-07-16
**Status:** Approved for implementation
**Authority:** Observation and funded-path telemetry only. It does not gate entries or suspend exits.

## Purpose

Give each attempted real entry an exact, replayable quality record and make holder-consensus evidence safe enough to evaluate before it is ever allowed to influence a funded stop.

The unit of identity is always `(network_id, mint)`. Robinhood is network `4663`. A bare EVM address is never enough to select Base, BSC, Ethereum, or Robinhood.

## Evidence behind the design

- The corpus has a sharp claimed-source-latency break near 60 seconds: sub-60-second entries were materially stronger than later entries. The source timestamp remains labeled claimed and untrusted.
- Entry market cap was positive below $30M and negative at or above $30M in the current study.
- SOLdiers shows that source-price drift alone misses a late coin-wave entry: later source wallets bought hundreds of percent above the first armed observation while each individual source gap looked small.
- Consensus improves coin-quality estimates but delayed consensus entry is harmful. Consensus is therefore a recorded modifier, never a trigger and never a green-rating upgrade.
- The global feed missed roughly 28.5% of classified sells from currently armed wallets in the inspected book. Positive holder evidence must expire and any sell without an explicit post-trade balance must invalidate it.
- `memecoin_token_episodes` had no automatic close boundary. A one-hour quiet gap matches the existing consensus window and creates deterministic, replayable episode boundaries without inventing a new funded rule.

## Non-goals

- No new signal source.
- No funded entry rejection or sizing change.
- No funded stop suspension.
- No change to token safety, reservation, capacity, kill-switch, or duplicate-submit contracts.
- No new network request.
- No Telegram, browser, dashboard, LLM, or notification work in the entry path.
- No pooling of `global_feed` and `follow_activity` results.

## Architecture

### 1. Episode facts

An episode is keyed by `(network_id, mint)` and contains exact event membership.

- The first eligible buy after a quiet period opens an episode.
- A new buy within 60 minutes of the prior episode signal joins the episode.
- A new buy after more than 60 quiet minutes closes the old episode and opens a new one.
- Reprocessing the same upstream event is idempotent and cannot increment signal counts.
- The episode stores the first source price and first executable observation when present. Missing prices remain `NULL`.

This supports:

- `episode_entry_sequence`
- `episode_source_runup_pct`
- `episode_executable_runup_pct`
- exact first-event attribution

No episode fact changes entry eligibility.

### 2. Immutable radar rating

Each real entry attempt gets at most one `memecoin_radar_ratings` row joined by exact `attempt_id`, `upstream_event_id`, `snapshot_id`, and `episode_id` where available.

The row contains:

- exact coin identity and signal source
- rating version
- `green`, `yellow`, or `red`
- deterministic reason codes
- canonical facts JSON and SHA-256 fact hash
- trusted local observation-to-decision latency
- claimed source age, labeled untrusted
- exact-event source price
- executable quote price, quote age, quote ID, and source drift where reached
- entry mcap and liquidity from the existing market-data response
- episode sequence and runup
- consensus count as a separate fact
- terminal attempt status and reason

Rating v1 has no score or learned weights:

- `red`: an existing deterministic eligibility or execution refusal already occurred, such as unresolved identity, stale entry, chase refusal, mcap refusal, safety failure, route/quote failure, or explicit rejection.
- `yellow`: the existing gates passed but attribution is incomplete, claimed age is at least 60 seconds, trusted local observation-to-decision latency is at least 60 seconds, entry mcap is at least $30M, or episode executable runup is at least the configured chase cap.
- `green`: existing gates passed and none of the v1 yellow facts apply.

`green` means “no observed v1 warning,” not “predicted winner.” `not_approved` remains yellow because it is an operator-arming fact, not evidence that the candidate is bad.

The writer is insert-once. A second write with different facts is an immutable-collision error; it never rewrites history.

### 3. Holder evidence ledger

`memecoin_holder_evidence` is keyed by `(network_id, mint, source_user_id)` and records:

- `positive`, `zero`, or `unknown`
- explicit remaining USD when present
- source, upstream event, observed timestamp, clock domain
- latest successful follow poll timestamp
- confirmation state
- invalidation timestamp and reason
- immutable evidence hash for the latest observation

Reconciliation rules:

- Explicit positive post-trade remaining USD -> `positive`.
- Explicit zero -> `zero`.
- Any sell with missing/partial/ambiguous remaining amount -> `unknown` immediately.
- Failed poll, malformed amount, unresolved exact network, or stale evidence -> not positive.
- A positive row is usable only while younger than the bounded evidence TTL.
- Follow-activity consumes the existing armed-wallet fast-lane response. It adds no request.

### 4. Protected-hold shadow

When the funded sweep is otherwise ready to execute `b_stop`, it evaluates fresh positive evidence from other armed wallets and writes a `protected_hold_shadow` audit row with `would_protect=true|false`.

The stop still executes exactly as before. TP, de-risk, timecap, unquotable, kill switch, and manual controls remain authoritative. This is count-only until paired forward evidence and sell-coverage reliability justify a separate funded proposal.

### 5. Coin-state vocabulary

The radar records a derived phase without giving it authority:

- `watching`: no funded reservation or open position from this attempt
- `owned_unconfirmed`: submitted/reserved but not confirmed
- `protected_hold_shadow`: confirmed funded position; holder evidence is observation-only
- `profit_lock`: reserved for a separate paper exit experiment
- `decaying`: reserved for later evidence-driven policy work
- `cooldown`: closed episode before the next eligible episode

Only the first three are emitted in v1. The others are vocabulary, not hidden behavior.

## Hot-path contract

- Reuse `event.network_id`; fallback resolution stays in the existing identity resolver.
- Reuse existing entry snapshot, attempt trace, market-data, quote, and fill facts.
- Local writes are best-effort after the funded attempt reaches a terminal result.
- Holder reconciliation is part of existing feed retention or existing follow polling.
- No network call is introduced.
- Rating or evidence-write failures cannot change the funded result.

## Acceptance tests

- Exact `(network_id, mint)` separation, including the same `0x` address on Base and `4663`.
- Duplicate upstream events do not increment episode sequence.
- A greater-than-60-minute quiet gap creates a new episode.
- Every finalized real attempt can create one immutable radar row.
- Missing attribution stays `NULL`/yellow; it is never zero-filled.
- Consensus never upgrades a yellow rating.
- A follow sell with no explicit remaining balance makes prior positive evidence unknown.
- Stale evidence is never returned as positive.
- A stop with fresh holder consensus writes `would_protect` but still calls the funded exit executor.
- Existing reservation, concurrency, identity, trace-clock, and duplicate-submit tests remain green.

## Graduation gate

No radar fact may gate funded entries and no protected hold may suppress funded stops until a new spec pre-registers:

- at least 30-50 paired forward closes per chain or an explicitly justified pooled-chain model
- source-lane-separated results
- complete quote/fill attribution coverage
- winner-swallow and drop-the-two-best-winners checks
- sell-evidence staleness and missing-sell stress tests
- exact fail-closed behavior for unknown holder state
