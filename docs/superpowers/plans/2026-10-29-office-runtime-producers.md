# Office runtime observation producers

> **For agentic workers:** Use superpowers:executing-plans in the isolated worktree. No live actions.

Spec: `docs/superpowers/specs/2026-10-29-office-live-floor-ui.md` at `0137c593b`.
Alpha approved the producer design and retention on 2026-10-29. Runtime changes do not change prompts or open a new epoch.

## Boundaries and edge cases

- No funded imports, provider calls, new model authority, admission changes, or production DB operations.
- Model dispatch only captures whitelisted metadata in a bounded in-memory buffer. Flush after each turn and on shutdown. A crash before flush is an observation gap, never evidence of zero calls. Overflow and failed persistence are visible producer-health failures.
- Unknown usage/cost/provider identity stays NULL. Preserve observed zero. Latency is measured monotonic transport time, not corrected-clock subtraction. Never persist prompt/auth/result text.
- Call identity is the existing deterministic request ID; duplicate identity cannot overwrite the first observation. Conflicting attribution refuses.
- Existing memo creation has no recipient: no inferred edge. Alpha additionally permits optional typed receipt references on `memo.commit`; unknown/private/future/mismatched references refuse before mutation. No prompt changes are applied.
- Materialize handoffs from committed immutable Office receipts and ledger decision receipts only, in bounded indexed tails. Decision orders retain NULL recipient when no actor is recorded. Aborted or nonexistent references do not create edges.
- Raw tables are insert-only except explicit retention deletes. They are separate from hash-chain core records. Schema migration is additive at Office store initialization, never in a GET or model dispatch.
- Retain 30 days. Prune at most 200 rows per table/pass, off dispatch; protect every OPEN epoch window and every row beyond Lambda's monotonic consumed-seq watermark. Unknown epoch state or absent watermark blocks pruning. Record retained-from/pruned counts. Do not delete core receipts or other chain rows.
- Lambda owns `agent_daily`/`equity_snapshots`; `mark_consumed` joins its transaction and never commits it. Sequence IDs use AUTOINCREMENT so retention cannot recycle consumption identities.
- Rollback is source rollback; additive observation tables remain intact. A prior runtime ignores them. No deployment/restart is part of this branch.

## Implementation

- [x] RED tests for typed schema/indexes, idempotence, unknown/zero/failed-call metadata and privacy; optional refs and real causal edges; retention fences and no implicit commits.
- [x] Add `runtime_observations.py` for schema, call buffer, committed receipt tails, health, and bounded retention. Small `store.open_store` schema hook.
- [x] Extend `models.Envelope` with measured latency and explicitly sourced cost; keep existing provider dispatch untouched. `runtime_steps.AgentStep` optionally captures actual dispatched results, including failures.
- [x] Wire the runtime buffer and maintenance; expose producer health in supervisor heartbeat. Flush after turns and during close, outside model dispatch.
- [x] Add optional typed refs to host memo action validation (model schema exposure follows the separately approved DM/task tool contract). Write immutable host receipts for memo/proposal creation. Broker validates typed receipt refs before storing successful handoff references.
- [x] GREEN focused tests, then existing Office model/runtime/broker/store/supervisor/probe regressions with `-n 2`. Measure capture overhead separately from provider and full-fill latency.
- [x] Explicit owned-path commit via `scripts/crew_commit.sh`; exact SHA to Zeta and Alpha. No push/merge/deploy by this task.

## Files

Owned: new `sniper/office/runtime_observations.py`, `store.py`, `models.py`, `runtime_steps.py`, `runtime.py`, `broker.py`, `scripts/office_supervisor.py`, focused tests and this plan. Do not change Lambda's reporting module or Epsilon's reader/UI.

Additional model call columns: `seq`, `latency_s` (seconds), `cache_creation_tokens` (separate from read-cache tokens). Existing spec columns remain direct SQL columns.
Typed response refs: `{kind: memo|decision|proposal, receipt_id: <immutable Office receipt hash>}`. Handoffs refer to actual committed receipt/record IDs, not prose, token proximity or guessed agent roles.

Verification: 185 focused producer/model/runtime/broker/store/supervisor tests plus 36 probe regressions PASS before final lifecycle witness; capture-only 1000 samples p50 2.76us/p90 3.57us, not provider/full-fill latency proof.
