# Desk CPMM Tape Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Delta implements in the shared checkout; Zeta reviews stable commits. No subagents or worktrees.

**Goal:** Capture confirmed Raydium CPMM swaps and expose an honest standalone, pool-bound research reader.

**Architecture:** A new, default-off process owns a dedicated confirmed gRPC subscription, bounded queue,
isolated read-RPC pool-binding budget, versioned SQLite tape and heartbeat. Pure transaction decoding binds
program logs to real instruction accounts and token owners. The reader never substitutes display data or
changes admission. Lambda owns dump-group retirement edits; Beta owns desk-reader wiring, so those are
subsequent coordinated integration steps rather than concurrent edits here.

**Tech Stack:** Existing Python, SQLite, solders, grpc/protobuf; no new dependency.

**Spec:** `docs/audits/2026-10-27-desk-graduated-venue-tape-scope.md`, unparked by Alpha's current operator order.

## Global Constraints

- New files only until Alpha/Lambda agree integration ownership. No funded imports, calls, signing, admission, marks, rails or live-service changes.
- All token/reserve/fee atomic values persist as decimal TEXT. Slot/order identities are separate from receipt clocks.
- Host receipt and corrected UTC receipt/offset are separate; missing correction is unknown, not zero.
- Explicit research gRPC/read-RPC credentials only; never silently reuse funded clients or provider fallback.
- Unknown pool/owner/order/fees/reserves are typed partials or gaps. Later metadata cannot retroactively authorize an earlier row.
- Retention initially stops at the disk cap; it deletes no tape. Activation/budget/p50/p90 proof is a later operator gate.
- Unsupported quotes, token extensions, pool switching, historic reconstruction, exact execution and funded use remain excluded.

## Review Focus

- Invocation order and router lookalikes: authenticate the executing program and pair each event with its instruction location.
- Pool/mint/vault/decimal/owner mismatch: retain partial evidence and fail closed for priced/owner counts.
- Queue overflow, reconnect and DB/disk failures: no fresh complete watermark without a committed fence and recorded gaps.
- Duplicate/restart/midnight and late rows: global event identity, immutable bindings/rows, explicit late gaps.
- Reader failure/absence/ambiguous pools/as-of: typed unreadable/incomplete, never a quiet empty success.

## Ownership and interfaces

- Create `sniper/services/memecoin_desk_cpmm.py`: pure event/pool/transaction decoder; no execution imports.
- Create `sniper/services/memecoin_desk_cpmm_capture.py`: private store, bounded capture queue, proto adapter and research-only binding budget.
- Create `sniper/services/memecoin_desk_cpmm_tape.py`: bounded, read-only, pool-bound as-of reader and coverage proof.
- Create `scripts/memecoin_desk_cpmm_worker.py` and `scripts/memecoin_desk_cpmm_watchdog.sh`: explicit opt-in process/identity only.
- Create `tests/test_desk_cpmm.py`, `tests/test_desk_cpmm_capture.py`, `tests/test_desk_cpmm_tape.py`, `tests/test_desk_cpmm_watchdog.py`, and frozen CPMM transaction fixtures.
- Update only this plan and the accepted scope note with measured test evidence and implementation limits.
- Later, after Lambda's commit: agree dump-group source/worker/cache integration. Beta owns structure/attention/RISK integration; no admission wiring.

## Task 1: Pure, authenticated transaction facts

Interfaces: `parse_event(raw)`, `pool_binding(pool, accounts, known_at, context_slot)`,
`decode_transaction(frame, bindings) -> {rows, gaps, unresolved_pools, status}`.
Frame includes confirmed commitment, successful metadata, complete logs, account keys, required signer count,
outer/inner instructions and pre/post token-balance ownership. Each row retains signature, actual instruction
location, log ordinal, pool, side, raw event bytes, atomics and evidence status.

- [x] Write failing tests against frozen existing CPMM event/account bytes plus explicit full transaction envelopes: direct/routed buy and sell, multiple events, lookalike router, failed/truncated frames, wrong binding, unknown owner/index/decimals and malformed values.
- [x] Run `python3 -m pytest -n 2 tests/test_desk_cpmm.py -q`; confirm RED before production code.
- [x] Implement the smallest pure decoder. Reuse the existing event format as a pinned format reference without importing execution builders. A payer alone never establishes the economic owner.
- [x] Validate GREEN; commit owned paths through `scripts/crew_commit.sh` and send Zeta evidence.

Pricing contract: the authenticated event carries **effective pre-swap** reserves; these differ from raw vault totals.
Expose that pre-state explicitly. A post-state/price remains unknown unless independently proved; do not pretend
the full trade fee leaves LP reserves or fabricate protocol/fund splits from a stale config. Observed event fee
legs/amounts remain available as raw facts. Later integration must choose the explicit price basis, not relabel it.
Reference implementation: [Raydium swap instruction](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/instructions/swap_base_input.rs).

## Task 2: Isolated capture, durable identity and typed gaps

Interfaces: `Store(root, ...)`, `Store.bind(...)`, `Store.record(...)`, `Store.gap(...)`,
`Store.fence(...)`, `Capture.offer(raw, received_at)`, and a proto-to-frame adapter.
Single process/DB writer; network lookup runs outside the receive callback. A binding is learned with a bounded
dedicated `getMultipleAccounts(pool, input_mint, output_mint)` read and stored with source/context/receipt proof.
No historical frame is upgraded using that later binding. A partial first swap may queue lookup; future swaps
alone can use the resulting prior binding.

- [ ] Write failing tests for atomic TEXT beyond signed SQLite integer range, duplicate/restart/midnight identity, immutable binding, typed global/per-pool gaps, late events, budget persistence, provider errors, disk/DB failure and bounded queue overflow.
- [ ] Confirm RED: `python3 -m pytest -n 2 tests/test_desk_cpmm_capture.py -q`.
- [ ] Implement one versioned `cpmm/tape.db` and `cpmm/heartbeat.json`; no prune. Persist read-RPC reservations before calls, with minimum spacing, daily cap and bounded failure backoff. HTTP failures never read as empty metadata.
- [ ] Advance committed-through only after a successful confirmed-stream fence, all earlier queued facts/gaps and DB commit. A connection flag or heartbeat tick alone grants no coverage.
- [ ] Implement default-off CLI/worker and pinned watchdog with own process basename/lock; no cron installation.
- [ ] GREEN and focused adjacent validation; commit/review stable SHA.

## Task 3: Standalone reader and coordinated handoff

Interfaces: `read_window(desk_dir, mint, lo, hi, *, pool=None, now=None)` returns typed state/reason,
rows, gaps, pool and receipt-domain coverage. Missing/unreadable tape, row cap, unknown mapping or multiple pools
cannot become an empty healthy window. Queries are indexed, bounded and close mode=ro connections promptly.

- [ ] Write failing tests for absent vs valid empty, cold/stale/incomplete coverage, explicit pool mismatch, reserve/owner unknowns, no future rows/metadata, gap intersection, as-of ordering, continuity and restart replay.
- [ ] RED: `python3 -m pytest -n 2 tests/test_desk_cpmm_tape.py tests/test_desk_cpmm_watchdog.py -q`.
- [ ] Implement the bounded reader; keep raw/pre-state price basis explicit and post price nullable. No automatic pool selection when ambiguous.
- [ ] Run all new focused tests at `-n 2`, schema checks on owned files, no-funded-import guard and no-network fixture checks. Record synthetic processing p50/p90 separately from unmeasured live capture latency.
- [ ] Commit with crew wrapper; request Zeta's independent review. Send exact API/fixture/schema and gap/price-basis contract to Lambda/Beta for their separately owned integration.

## Deferred integration and activation

This plan does not retire or mutate existing capture workers, readers or crons. Alpha's curve-retirement order is
handled by the existing owners; new capture remains opt-in. No live request, deployment or environment write is
implied by offline completion. Integration with Lambda/Beta and organic capture/budget proof remain tracked work,
not fixture-derived claims.

Task 1 evidence: module-absent RED, then 16 GREEN; 8 additional identity/index/nonfinite-clock tests RED then all 24 GREEN (1.25 s, -n 2). Fixture event/pool bytes and account order are frozen census facts; transaction envelope and mint metadata are synthetic. Zeta review pending.

## Review ledger — 2026-10-27

- Decoder `220606b3b` plus `186d6749d`: Zeta PASS after two RED→GREEN trust-boundary fixes (explicit successful metadata; unknown instruction layout gets a global gap).
- Capture/reader baseline `1104175b5`; `9d170ea76` fixes funded pool/credential overlap and scopes transaction-wide discarded facts globally, each witnessed RED→GREEN.
- Follow-up: provider failure count and retry deadline now persist in private `rpc_backoff`; a restart witness was RED before the change.
- Ruling: **metadata-only fences do not establish complete capture**. The worker records processing checkpoints with `complete=0`; the reader reports `capture_fence_unproven`. The `complete=True` store dependency is injected only by unit fixtures. No production caller enables it. Complete organic windows require a separately reviewed, counted source fence or a pinned provider delivery guarantee. This intentionally blocks coverage readiness rather than inventing a denominator.
- Source checked: https://github.com/rpcpool/yellowstone-grpc/blob/master/README.md describes reconstructed full blocks versus metadata, without the needed filtered-transaction-before-metadata guarantee.
- Offline evidence: 62 new focused tests GREEN at `-n 2` (2.26 s), including a fake-gRPC receiver/processor/storage path; schema, compile and shell syntax checks pass. Synthetic decoder n=500: p50 0.024867 ms, p90 0.027587 ms. Live capture/provider cost/latency remain unmeasured.
- The worker and watchdog are default off; no cron, environment, funded service or provider request changed. Zeta full follow-up review remains pending. Beta owns later desk-reader wiring; Lambda owns later dump-group integration. This plan is not complete while the source-fence readiness item remains open.
