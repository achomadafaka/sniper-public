# Memecoin V6 correctness and fast-runtime remediation — design

**Date:** 2026-07-26
**Status:** approved for implementation planning; not approved for activation
**Scope:** V6 live paper only; Solana and Robinhood chain 4663; default OFF
**Extends:** `2026-07-26-memecoin-v6-live-paper-engine-design.md`
**Supersedes:** the current sweep-count semantics, global missing-data gate, unbounded policy state,
and fixed-cadence runtime described or implied by the foundation/runtime plans

## Decision

Keep one V6 engine and remediate it in three ordered deliveries:

1. make the pure transition cadence-invariant, bounded, recoverable, and explicit about action
   intent versus fill availability;
2. move every post-fill terminal and staged decision into V6 itself;
3. replace the five-second full-book sweep as the primary path with a subsecond event-driven data
   lane, retaining a quiet heartbeat and bounded catch-up path.

The order is mandatory. Running the current transition faster changes its decisions: repeated calls
on unchanged evidence increment negative-flow confirmation, append duplicate volatility samples,
and can consume multiple tranches. Faster scheduling is unsafe until evidence identity controls
state advancement.

V6 remains the sole candidate lineage. This work adds no V5 arm, replay gate, caller-owned exit
policy, funded authority, or automatic activation.

## Evidence for remediation

Read-only probes against the implementation at `1a64b540` established:

- a warm normal `step()` takes roughly 0.04–0.2 ms; direct observation construction over 5,000 tape
  rows takes roughly 24 ms per position;
- the first volatility-percentile decision imports NumPy lazily and took roughly 127 ms;
- identical market evidence presented with three different observation timestamps can produce
  `DERISK`, `DERISK`, then `EXIT_FULL`;
- `vol_hist` grows once per call, including repeated evidence, and is duplicated in persisted
  before/filled/unfilled state documents;
- recovery permanently prevents later break episodes;
- a missing executable quote makes `route_degraded=True` but the global executable-price gate
  returns `HOLD`, so route-loss confirmation is unreachable;
- missing `chain_vol_p90` blocks unrelated rules, including transaction-grounded LP removal;
- each timestamp can create a new action token and a new exact-quote attempt on unchanged evidence;
- the current worker reads at five-second cadence, the EVM tape defaults to three seconds, and the
  paced Solana pull tape defaults to 300 seconds.

The bottleneck is therefore orchestration and semantics, not arithmetic in the warm engine.

## Alternatives considered

### Chosen: ordered V6 remediation

Fix evidence semantics first, complete the policy second, then add the event-driven runtime. Each
delivery has a focused test gate and can be reviewed independently. Nothing is activated between
deliveries.

### Rejected: reduce the sweep interval only

This would make the engine react to loop frequency rather than market events, multiply quote calls,
and accelerate state/database growth.

### Rejected: replace engine, capture, persistence, and UI in one rewrite

This hides whether failures come from policy semantics or transport. It also overlaps the already
merged Plan 3 operator surface and makes rollback evidence weak.

## One public engine

`sniper.services.memecoin_v6_engine` remains the only public post-fill decision interface:

```python
step(state: V6PositionState, observation: V6Observation) -> V6Decision
```

`memecoin_v6_policy.py` may contain private rule helpers, but it is not another selectable engine.
The live-paper runtime must not import or dispatch to the historical replay seed. The runtime policy
identity advances to `v6-position-engine-v1`; `policy_hash` covers every constant, rule precedence,
evidence contract, and state schema.

There is no policy selector. An existing open position with another policy hash is never silently
migrated. Startup refuses it and requires explicit operator censoring. This is expected to be inert
because V6 has not been activated.

The engine owns all normal post-fill outcomes. The worker supplies facts and applies returned paper
actions; it cannot run a TP, stop, trail, mirror, timecap, catastrophe, LP, unsellable, or rescue
rule of its own.

## Semantic evidence identity

Wall-clock observation time is audit metadata, not evidence of a new market event.

Each observation carries three independent identities:

- `market_evidence_id`: chain, pool/vault, capture version, ordered tape cursor, and latest decoded
  event identity. Solana uses signature plus instruction/log index. EVM uses block hash, transaction
  hash, and log index. A coalesced batch includes the first and last cursor plus event count.
- `quote_evidence_id`: provider, route binding, exact input amount, output amount, provider quote id
  or response hash, and provider-observed time.
- `source_evidence_id`: the exact source-wallet holding event or snapshot identity.

`observation_id` hashes the position id and those semantic identities. It does not include worker
wake time. The observation retains corrected `observed_at`, receive time, and processing timestamps
for latency measurement, but those timestamps cannot advance market confirmation.

The state records the last applied identity for each lane. Rules declare which lane can advance
them:

- flow streaks, volatility history, break episodes, and LP state advance only on a new
  `market_evidence_id`;
- executable high-water and quote status advance only on a new `quote_evidence_id`;
- wallet-exit state advances only on a new `source_evidence_id`;
- held time is derived from immutable fill time and corrected current time. A heartbeat may cross a
  time threshold but cannot count as a market confirmation.

Re-presenting identical semantic evidence returns the same action intent and leaves policy state
unchanged. A different timestamp alone is a no-op.

## Bounded position state

`V6PositionState` becomes a bounded document with:

- remaining atomic quantity and immutable entry economics;
- last applied market, quote, and source evidence identities;
- at most 64 distinct causal realized-volatility samples;
- one current `BreakEpisode` and the global count of filled partial tranches;
- executable high-water;
- structured route-absence state;
- one active action intent and its last fill outcome.

The volatility window appends only on a new market identity and drops the oldest sample above 64.
The p90 calculation uses the standard library over this bounded window; NumPy is not imported on the
decision path. Twelve prior samples remain the minimum for own-history p90. Before that, the
point-in-time chain p90 is used when present; if neither exists, only the volatility score factor
abstains.

Append-only decisions store the observation id, action, reasons, state-before hash, state-after
hash, and compact transition delta. They do not duplicate two complete state documents for every
`HOLD`. The current full bounded state remains on the position. An actionable decision persists the
filled and unfilled successor data needed for crash recovery under its action intent.

The following limits are acceptance contracts, not tuning flags:

- `len(volatility_samples) <= 64` after any number of transitions;
- serialized current engine state <= 16 KiB;
- one active action intent per position;
- no append-only decision for a semantic no-op heartbeat.

## Break episodes and tranche accounting

Recovery ends only the current break episode. It does not make the position immune to later market
breaks.

A `BreakEpisode` carries `episode_id`, nominated break level, first market evidence id, negative-flow
streak, and `active | recovered | confirmed` status. A new distinct nominated break after recovery
opens a new episode. Repeating the same market evidence cannot open, confirm, or recover an episode.

Partial budget is position-global and fill-based:

- first low-tier break requests 25% of remaining inventory;
- first medium-tier break requests 50%;
- high tier requests a full exit;
- a distinct confirmed break or confirmation condition requests 50% of remaining inventory;
- at most two partial fills may occur before later medium/high break evidence becomes terminal;
- an unfilled action does not consume a tranche.

These are the existing V6 staged sizes. The remediation changes evidence correctness, not those
sizes.

## V6-native decision rules

Rules evaluate independently and return `triggered`, `not_triggered`, or
`insufficient_for_this_rule`. Missing evidence disables only the affected rule. It cannot globally
force `HOLD` when another rule has enough trusted evidence.

Actions are resolved by precedence: any `EXIT_FULL` beats any `DERISK`; otherwise the largest
triggered de-risk fraction wins. The decision records all triggered and insufficient rule codes so
the operator can see both the action and blind spots.

### Transaction-grounded LP loss

Return `EXIT_FULL` when `depth_pull_flag=True` comes from a complete tape, a supported capture
version, trusted depth provenance, and a distinct decoded event identity. The decoder's existing
chain-specific semantics remain authoritative: one-sided CLMM fee/liquidity activity cannot
fabricate a pull. No quote or chain-volatility percentile is required to form the intent.

### Executable catastrophe

Return `EXIT_FULL` when a fresh exact whole-bag quote values one unit at or below 20% of modeled
entry unit price, an 80% executable loss. The entry and current quote must use the same quote asset
and exact remaining-bag economics. A nominal chart price cannot trigger this rule.

### Persistent route loss

Quote outcomes become structured:

- `healthy`: an exact executable route exists;
- `route_absent`: providers/RPC responded successfully but no supported executable route exists;
- `provider_unavailable`: timeout, rate limit, transport, or provider failure;
- `stale`: a route response exists but exceeds its freshness contract.

Only `route_absent` accumulates route-loss evidence. Four distinct route checks spanning at least
60 seconds return `EXIT_FULL`. Provider failures and stale responses remain visible but cannot be
misclassified as an unsellable token. A later healthy exact quote resets route-absence state.

The engine may form this exit intent without an executable quote. The paper executor records it as
unfilled and keeps trying under the same action intent; it never fabricates proceeds or closes the
position.

### Time expiry

Return `EXIT_FULL` when corrected held time reaches 43,200 seconds. This is a V6 decision, not a
worker rail. The rule needs only immutable fill time and corrected current time. If no exit quote is
available, the action remains open and unfilled.

### Staged break manager

The current V6 break score remains:

- early: held time below 10,800 seconds;
- frenzy: arrival rate at least 0.10 decoded events/second;
- crowded: decoded buy share at least one third;
- volatility shock: realized volatility at or above causal own-history p90, or chain p90 before 12
  own samples.

Score at least two is high tier, one is medium, and zero is low. A frozen wallet prior may move one
tier up or down. If that prior is unavailable, the base tier remains and the decision records
`wallet_prior_unavailable`; it does not block unrelated rules.

Confirmation requires a distinct second nominated break, negative decoded flow on two distinct
market evidence identities, a persistent route-loss trigger, or an exact source-wallet full exit.
The current broad `any sell/closed after open` inference is removed: a partial sell is not a full
exit, and each source event must have a stable identity.

Recovery requires price to reclaim the active episode break level on new market evidence before
confirmation. It marks that episode recovered. A later distinct break can create another episode
and use any remaining global partial budget.

### Deliberate non-rules

There is no fixed take-profit or caller-supplied trailing stop. Executable high-water and drawdown
are consumed as causal context, reported, and retained for later V6 analysis, but this remediation
does not invent an untested profit cap. A later winner-management change must be a new V6 policy
hash and prospective live-paper cohort, not an operator overlay.

## Action intent versus execution

`V6Decision` separates policy from paper execution:

- `action`: `HOLD | DERISK | EXIT_FULL`;
- `fraction_ppm` of then-current remaining inventory;
- stable `action_intent_token` based on position, policy, semantic trigger evidence, action, and
  fraction;
- rule results, reason codes, and deterministic thought;
- bounded successors for filled and unfilled outcomes.

An active intent does not stop observation. New market/source evidence continues through V6 while
the paper executor retries. There is never more than one fillable intent:

- equivalent or weaker decisions reuse the active token;
- a stronger decision atomically supersedes the prior intent before any new quote attempt;
- `EXIT_FULL` cannot be superseded by a partial action;
- a fill atomically claims the current intent, appends one modeled fill, updates inventory and
  tranche budget, and completes the intent.

Unfilled retries use the same token and exact current quantity. Retry intervals are 0, 1, 2, 5, 10,
then 30 seconds, capped at one attempt per 30 seconds thereafter. A new semantic observation does
not reset this backoff unless it proves a healthy route or produces a stronger action. This avoids
subsecond quote storms without delaying receipt of new market facts.

## Event-driven runtime

The five-second loop remains only a recovery supervisor. Normal work is driven by position dirtiness
from normalized events.

### Read-only chain feeds

Add V6-only read adapters with no funded execution imports:

- Solana: subscribe to the resolved pool/vault logs, normalize signature plus instruction/log
  identity, and reuse pure transaction decoders;
- 4663: subscribe to pool logs and new heads when the provider supports it; otherwise poll
  `eth_getLogs` from the persisted cursor every 500 ms;
- both: on reconnect, run bounded cursor catch-up before declaring the lane current. A gap is
  persisted and the affected rule families abstain until recovered.

The adapters write only V6 observations/cursors/health into the dedicated V6 database. They do not
write the shared tape, source ledger, funded positions, reservations, or production policy. The
existing slow pull tapes remain independent verification/backfill sources, not the live decision
clock.

### Incremental aggregation

Each normalized event updates an in-memory per-pool accumulator and marks affected positions dirty.
The worker coalesces bursts for 50 ms, freezes one semantic observation, persists it, and calls the
pure engine. It does not rescan 5,000 tape rows per position on every wake.

A startup/reconnect catch-up may rebuild the accumulator from the bounded tape. Once current, only
new events are applied. Multiple positions on one pool share the pool accumulator but keep separate
position economics and engine state.

### Quotes and quiet positions

A fresh whole-bag executable quote is requested asynchronously when:

- a position opens;
- new market evidence marks it dirty;
- the last successful quote reaches five seconds old;
- an active action intent becomes retry-eligible.

Quotes are prewarmed and never block receipt or normalization of chain events. The exact
action-quantity quote remains mandatory before a modeled fill.

A one-second heartbeat checks feed health, quote expiry, action retry deadlines, and time-rule
boundaries. An unchanged heartbeat updates a compact health row; it does not append an observation,
decision, volatility sample, or action token.

### Concurrency

The worker supports the existing four-position experiment cap and does not serialize unrelated
positions behind one network quote. Per-position state mutation is single-writer; network reads and
quotes are bounded asynchronous tasks. A slow or failed quote for one position cannot block event
ingestion or decisions for another.

## Latency contract

Latency is split so provider delay is not mislabeled as engine compute:

- `provider_event_at -> local_received_at`;
- `local_received_at -> observation_persisted_at`;
- `observation_persisted_at -> decision_persisted_at`;
- `action_intent_at -> quote_response_at`;
- `quote_response_at -> modeled_fill_at`.

On a warm local test with fake providers and four simultaneous positions:

- event receipt to persisted observation p95 must be below 100 ms;
- persisted observation to persisted decision p99 must be below 25 ms;
- pure `step()` p99 must be below 5 ms across 10,000 transitions;
- identical market evidence at 10 Hz for one minute must create one semantic transition and no
  duplicate quote intent;
- the worker must continue processing other positions while one quote waits for its timeout.

Provider latency has no false hard SLO. It is reported separately by chain/provider, including
subscription gaps, reconnects, fallback-poll mode, and p50/p95/p99 receipt lag.

## Persistence and recovery

One transaction persists observation, decision, current bounded state, and action-intent changes.
Modeled fill application remains a second idempotent transaction because a network quote occurs in
between. On restart:

1. recover or supersede the single active action intent;
2. rebuild pool accumulators from persisted cursors and bounded tape;
3. catch up chain feeds;
4. resume event processing;
5. allow new entries only after both chain lanes required for those entries are current.

An unknown or incomplete chain cursor never advances past a gap. It marks affected rule evidence
insufficient; it does not trigger a fallback exit or fabricate a complete tape.

## Operator surface

The merged V6 Paper section remains the surface. Extend it with:

- market/quote/source evidence identities and whether the latest wake was a semantic transition;
- active or superseded action intent, retry count, next retry time, and last structured quote
  outcome;
- current break episode and global filled-tranche budget;
- per-rule triggered/not-triggered/insufficient results;
- receive, observation, decision, quote, and fill latency;
- feed mode, cursor, gap, reconnect, and accumulator freshness by chain.

Do not expose cadence controls as policy knobs. The UI still has no funded action buttons. Censoring
requires confirmation, a non-empty audit reason, POST-only mutation, CSRF protection, and the
existing V6-paper database boundary.

## Testing order

Implementation is RED-first and follows the delivery order.

### Engine correctness gate

- reproduce the three-call same-evidence `DERISK -> DERISK -> EXIT_FULL` failure, then prove one
  semantic transition at 10 Hz;
- prove confirmations require distinct market identities;
- prove 100,000 transitions keep the volatility window and serialized state bounded;
- prove recovery permits a later distinct break episode;
- prove LP loss can exit with quote and chain-p90 fields absent;
- prove route loss distinguishes route absence from provider/stale failure;
- prove an unfilled action neither consumes a tranche nor creates a new token;
- prove source partial sell is not source full exit;
- prove policy hash changes for any effective constant or rule order change.

### Persistence gate

- crash after intent persistence, after quote, and after fill insertion; restart must apply at most
  one fill;
- stronger evidence supersedes one pending partial without making both fillable;
- state hashes and compact deltas replay to the current bounded state;
- database size remains bounded by event volume rather than sweep count.

### Fast-runtime gate

- fake Solana and 4663 streams test event identity, ordering, reconnect, cursor catch-up, and gap
  refusal;
- burst coalescing preserves all causal events while producing one current observation;
- four simultaneous positions advance independently;
- fallback polling stays at 500 ms and cannot duplicate events;
- latency acceptance tests use fake providers and a warmed engine, not internet timing;
- AST/import tests prove funded signing, approval, broadcast, reservation, runner, and funded DB
  modules are unreachable.

### Regression gate

Run all V6 foundation/runtime/operator tests, adjacent EVM quote tests, Solana tape tests, tick-stream
tests, funded-boundary import tests, schema validation, and `git diff --check`. Live RPC smoke,
worker activation, deployment, production DB writes, and external Supabase knowledge writes remain
separate operator-authorized gates.

## Delivery boundaries

The implementation plan must use three mergeable task groups:

1. **Engine kernel and persistence:** evidence identities, bounded state, episode semantics,
   per-rule evidence, action intents, and compact persistence.
2. **V6 policy completion:** LP loss, executable catastrophe, persistent route loss, time expiry,
   exact source exit, and precedence.
3. **Fast runtime and operator telemetry:** read-only event feeds, incremental aggregation, dirty
   queue, quote scheduling, health/latency, and Plan 3 surface additions.

Task group 2 cannot begin until group 1 tests pass. Task group 3 cannot become runnable until groups
1 and 2 pass. All groups remain default OFF and unactivated.

## Acceptance criteria

1. V6 is the only candidate engine and the only normal post-fill decision authority for its book.
2. Decision behavior depends on distinct semantic evidence, not sweep frequency or wall-clock wake
   count.
3. Engine state and per-decision persistence are bounded.
4. Recovery is episode-local; later distinct breaks remain actionable.
5. Missing evidence abstains per rule and cannot blind an independently supported terminal rule.
6. LP loss, executable catastrophe, persistent route loss, time expiry, and staged break behavior
   are evaluated inside V6.
7. Action intent survives quote failure without consuming inventory/tranche budget or creating a
   quote storm.
8. New Solana and 4663 activity reaches the local observation path in under one second when the
   provider feed or fallback poll delivers within that window.
9. Warm local event-to-decision latency meets the stated p95/p99 contracts with four simultaneous
   positions.
10. No V6 path can sign, approve, broadcast, reserve funds, mutate funded state, or activate itself.
11. No live call, worker start, deployment, production DB write, or funded promotion occurs without
   a later explicit operator instruction.

## Implementation evidence — 2026-07-26

Fake-provider acceptance, four simultaneous positions across Solana and chain 4663, 25 interleaved
events per position, warmed engine, and one deliberately blocked quote:

- receive to observation: p50 1.301 ms, p95 1.571 ms, p99 1.633 ms;
- observation to decision: p50 0.107 ms, p95 0.211 ms, p99 0.221 ms;
- pure `step()`: p50 0.069 ms, p95 0.120 ms, p99 0.128 ms.

Command: `V6_TEST_METRICS=1 python3 -m pytest -n0 tests/test_memecoin_v6_fast_runtime_integration.py::test_four_position_cross_chain_runtime_isolates_slow_quote_and_meets_local_targets -q -s`

These are local fake-provider measurements only. They prove owner-loop isolation and local compute
latency, not live provider latency or activation readiness.

## Knowledge Sync

This design changes V6 engine semantics and runtime architecture. The implementation PR must update
`AGENTS.md` and `CLAUDE.md`, add/supersede the relevant architecture memory after explicit external
write approval, and include the complete Knowledge Delta Report. Historical V5/replay documents may
remain as superseded evidence, but no runtime or operator surface may present them as selectable
engines or acceptance arms.
