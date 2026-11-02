# V6 Admission and Quote-Alert Remediation Design

Status: approved for implementation. V6 remains live-paper only. Funded entry, funded exit policy,
and Coin Radar are unchanged.

## Problem

V6 currently cursors mutable `memecoin_events.action='opened'`. An event can become eligible after
the cursor passes it, so valid live fills disappear permanently. The fast runtime also permits only
one entry quote globally; one slow Solana capture blocks every later signal. A failed quote is a
terminal signal outcome with no retry evidence.

Funded exact-exit infrastructure alerts fire once per position per sweep. A provider outage can
therefore send an unbounded stream of alerts whose wording looks like token unsellability even though
the unsellable counter is deliberately unchanged.

## Admission source

- The source of truth is an append-only paper fill in `memecoin_positions`, not mutable event action.
- Eligible rows have `execution_mode='paper'`, a nonempty `origin_event_id`, a supported normalized
  network, and an unpaused, non-excluded, real-approved source wallet.
- Event metadata is joined by `origin_event_id`; the immutable paper position supplies its entry
  timestamp and price. Missing required identity is terminal and visible, never silently skipped.
- The cursor is `memecoin_positions.id`. Intake scans bounded raw-position pages and advances across
  accepted, rejected, and duplicate rows, so an ineligible tail cannot be rescanned forever.
  Multiple paper strategy arms for one origin are deduplicated by `(origin_event_id, policy_id)`;
  the lowest source-position id owns the V6 candidate.
- Migration never opens stale historical candidates. Event ids and position ids are different cursor
  domains, so migration records one bounded `cursor_domain_migration` marker with count `unknown`
  instead of scanning the full production ledger or inventing per-row attribution; the new cursor
  starts at the current source-position tail.

## Durable admission state

`v6_paper_admission_queue` is mutable operational state. `v6_paper_entry_quote_attempts` is immutable
evidence. The existing `v6_paper_signal_attempts` remains the one terminal summary per origin.

Queue states are `queued`, `inflight`, `retry_wait`, `filled`, and `terminal_unfilled`.

- Intake and cursor advancement commit independently of provider calls.
- A claim uses `BEGIN IMMEDIATE`, a unique claim token, and a lease deadline.
- Open positions plus live claims may not exceed `MEMECOIN_V6_PAPER_MAX_OPEN`.
- Up to the free-cap count may quote concurrently. Oldest eligible candidate wins; one network cannot
  head-of-line block the other.
- `QuoteUnavailable` and provider exceptions retry at 2s and 5s, at most three attempts, while the
  source fill is at most 90s old. `IdentityInvalid` and `QuoteStale` are terminal immediately.
- Every attempt stores start/end timestamps, normalized outcome, detail code, and provider when known.
- A stale lease is recovered only when no filled entry or terminal summary exists. Late results with
  an obsolete claim token cannot fill or mutate the replacement claim.
- A process timeout cannot cancel a running Python thread. Provider operations therefore keep their
  own bounded HTTP/RPC timeouts and the Solana capture scan has a bounded transaction budget. Logical
  leases reject late results; executor size remains capped.
- Capacity is backpressure, not a terminal signal outcome. Queued candidates wait until a slot opens
  unless their 90s freshness window expires.

## Runtime invariants

- Entry polling runs on the one-second heartbeat and immediately after any entry result.
- A stalled entry cannot stop position observation, exit quoting, another entry, or the other chain.
- Filled-entry identity remains deterministic and one-fill-per-origin.
- A quote success commits terminal summary, entry, queue completion, and claim validation in one owner
  sequence. A crash between these idempotent steps is reconciled on restart without a second fill.
- V6 remains the sole post-fill decision authority for its paper positions. No hard rail, funded signer,
  approval, broadcast, reservation, or funded-authority setting is introduced.

## Operator evidence

The V6 report exposes admission counts for eligible, queued, inflight, retrying, filled, terminal
unfilled, and historical gaps plus normalized failure reasons and oldest queue age. This evidence is
read-only and separate from funded positions.

## Quote-infrastructure alerts

- Safety counters remain per funded position and retain existing TOKEN-versus-INFRA classification.
- Telegram is provider-episode based: alert once at episode start, again when a provider reaches
  streak 5 and 20, then once on recovery. No alert is sent on every intermediate sweep.
- One provider alert aggregates all affected positions in that sweep. It includes provider, failure
  class, affected count, maximum streak, and explicitly says token unsellable counters are unchanged.
- Recovery is emitted only for positions whose persisted infra streak was nonzero and whose exact
  quote succeeds. Mixed success/failure in one provider group does not claim full provider recovery.
- Restarts do not repeat episode-start alerts because persisted per-position streaks resume above one.
- Token-specific failures never enter the provider alert episode.

## Edge cases and rejection rules

- Null/blank origin, unsupported chain, malformed mint, naive timestamp, nonpositive source fill, or
  missing event/wallet provenance becomes terminal evidence with a bounded detail code.
- A duplicate origin with conflicting chain or mint is an immutable conflict and does not quote.
- Clock skew cannot extend freshness: age is clamped at zero for future timestamps and capped by the
  corrected local clock for expiry.
- A claim whose result arrives after expiry or replacement is recorded `obsolete_claim`; it cannot
  consume capacity or create an entry.
- Shutdown stops accepting claims, drains completed owner-loop results, and leaves unfinished claims
  lease-recoverable.
- Database migration is explicit from schema v5 to v6 and validates every new column and index.

## Activation and rollback

Activation requires focused tests, schema validation, no funded imports in V6 paper modules, exact
release deployment, V6 paper worker restart, and live proof that the new source cursor advances.
Rollback stops the V6 paper worker and returns to the prior release; queue evidence remains readable.
Funded V6 authority must stay empty throughout.

The paper watchdog separates code and state roots. `SNIPER_V6_CODE_ROOT` selects the exact checkout;
`SNIPER_STATE_ROOT` owns `.env`, DBs, locks, PID, and logs. A reboot must not fall back to whichever
branch happens to occupy the mutable repository root.
