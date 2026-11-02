# Memecoin Exit Hardening Round 4 Design

**Date:** 2026-07-24
**Status:** Approved for implementation
**Scope:** Correct four remaining activation blockers without enabling, deploying, or changing the frozen exit-engine-v3 decision rules.

## Constraints

- Round-3 crash, continuous-completeness, and one-row evidence-chain regressions remain tests and must stay green.
- Add a crash-after-broadcast regression proving the position remains submitted-unknown, cannot be released as stale, cannot be reclaimed, and cannot be sold twice.
- Candidate decision, state reference, proposed fraction, exact-confirmation result, and execution outcome remain one queryable row per position/tick.
- Work in an isolated worktree. Stage explicit paths only. Do not touch flags, services, live APIs/RPC, or production databases.
- Use focused tests only; do not run the full suite.

## 1. Solana Tape Completeness

`sol_sig_cursor.frontier_before` remains both the resume marker and the engine-visible incompleteness marker, but its value gains an explicit mode:

- `enum:<signature>` means enumeration stopped above the cursor. Resume with RPC `before=<signature>` and decode nothing until the walk reaches the cursor.
- `decode:<signature>` means enumeration reached the cursor but known signatures remain undecoded. Re-enumerate without `before`, decode the oldest cursor-contiguous prefix, and retain decode mode while work remains.

Existing unprefixed frontier values are read as enumeration markers for migration compatibility. The frontier clears only after an unrestricted walk reaches the cursor and every returned signature is decoded. An RPC or transaction failure holds both cursor and frontier.

This preserves bounded pagination without the round-3 semantic overload where an exclusive `before` marker could hide the marker signature and newer head signatures for one pass.

## 2. De-risk Claim and Submission State Machine

Add `derisk_claim_id` to the funded position row. The executor owns a de-risk attempt through an opaque claim token:

1. `claim_position_derisk` atomically moves idle to pending and returns the token.
2. Quote/approval/signing run while pending.
3. Once the signed transaction identity is known, a claim-qualified CAS persists `derisked_at + derisk_sig` before broadcast. This is `submitted_unknown`; `derisk_confirmed` remains false.
4. Only then may `send_fn` run.
5. Confirmed execution atomically books the fill and settles the matching claim/signature. Explicit failure or explicit non-broadcast resets only that matching submission to pending so the same owner may try another rung. Ambiguous send/confirm failure retains submitted-unknown for recovery.

Every release, reset, fill, and recovery clear is qualified by claim token and, after signing, transaction identity. Stale recovery releases only unsigned pending claims whose exact token and timestamp still match. A delayed old executor cannot clear or submit over a newer claim.

## 3. Live Authority Flag

`MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` is read directly from `$SNIPER_STATE_ROOT/.env` on every reservation decision and exit sweep. The reader does not mutate `os.environ`.

The state-root file is authoritative for this funded kill lever. Missing key, blank value, malformed/unreadable file, or invalid state root returns the empty set and therefore fails OFF. Tests edit a temporary state-root `.env` between calls and prove the next call observes the change without `load_dotenv`.

## 4. Deferred One-row Decision Evidence

Engine evaluation creates a complete in-memory audit record containing:

- position/tick natural key;
- engine and feature versions;
- exact `state_id`;
- candidate action, proposed fraction, reason codes, and no-action reason;
- envelope/legacy action at that instant and divergence;
- source block timestamp;
- exact-confirmation availability/result;
- final execution outcome.

No exit-state write occurs inside a position's decision, confirmation, or execution path. Records are flushed only after every position in the funded sweep has completed its funded hook. A lock can delay or fail telemetry after submission, never delay any position's submission. The database stores one row per natural key; deferred flush changes timing, not row shape.

RIDE/DEFER records receive explicit no-action outcomes. Refused actions record the executable rejection code. Submitted actions record the executor status. The existing round-3 `state_id + outcome` regression remains and is extended to assert the proposed fraction and confirmation fields.

## 5. Verification

Each change follows red-green TDD with a focused regression:

- 100 signatures / budget 40: incompleteness remains true until all 100 signatures are fetched.
- Enumeration-bound and new-head cases: no cursor jump and no transient complete state.
- Crash after broadcast: submitted-unknown survives stale recovery and a second executor cannot broadcast.
- Duplicate stale recoverers and delayed executors cannot clear a newer claim.
- Editing the state-root `.env` from `solana` to blank demotes the next call without environment reload.
- A locked exit-state DB cannot delay any funded executor call; after an unlocked flush, one complete decision row exists.

Run the focused memecoin engine, Solana tape, executor, recovery, materializer, persistence, schema-reference, and diff-hygiene checks. Do not run the full suite.
