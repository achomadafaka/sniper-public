# Office final-step actions and distinct-recipient messages

Alpha authorized this runtime proposal after read-only diagnosis of Head turns at 17:30 and 18:00 UTC. Zeta reviews; Gamma records the activation behavior boundary. No activation is part of implementation.

## Problem and smallest change

The Head returned valid JSON with `done=true`, three `msg.send` requests to different seats, and one `task.update`. AgentStep rejected the whole step as `done_with_requests`; none of those writes or its completion memo ran. Merely removing that check would still reject repeated mutation kinds, and Supervisor action identity currently collides for all messages in a turn.

Interpret a write-only `done=true` batch as: dispatch the validated writes in supplied order, then record the existing completion memo and end. A read-only final batch remains permitted. Reject a mixed read/write final batch as `mixed_final_requests` before any broker read or write executes; count every submitted request as refused. Allow multiple `msg.send` requests only for distinct recipients; keep one request per other mutation kind. Track host-generated mutation identities across steps, bounded by the existing six mutation kinds (`WRITE_TOOLS` cardinality), so recipient expansion does not grant an unbounded write budget. The completion memo remains separate from that budget, as today.

Use recipient-specific message action keys. For an existing legacy message action row, reuse its old key for that recipient so pending/completed historical sends retain their exact identities. Other mutation keys remain unchanged. Payload changes under an existing recipient key must still refuse.

## Edge cases and limits

- This reaches Office messages/tasks only, never funded entry, exits, sizing or signing. No new funded-path work, global settings or shared connections.
- Validate every requested mutation before returning any actions; duplicate recipients, duplicate non-message mutation kinds, malformed host progress or over-budget writes refuse the complete step before dispatch.
- Missing/invalid recipients and denied capabilities remain existing typed refusals. No capability expansion, prompt edit or tool-schema change; Head's denied near-pass/health reads remain denied.
- Preserve write order, actor/generation, immutable cut, policy/prompt pins, lease/deadline checks and existing per-action transactions. Mixed final batches are refused before effects; no write-prefix guarantee is claimed for existing nonfinal mixed batches, whose broker reads run eagerly and whose writes remain queued.
- Partial failure is not atomic rollback: earlier committed actions stay committed with receipts; stop before subsequent actions and completion memo. Existing pending-action reconciliation/lookup handles ambiguous local effects. Never claim all messages delivered or acknowledged from a model memo.
- A crash/reentry must reuse action keys, never duplicate a completed send, and never silently replace an old payload. Legacy and recipient-specific identities must not both execute for the same historical send.
- Persist bounded host mutation progress across model steps; model output cannot set that metadata. Repeated recipient requests in later steps are refused; retries of an already-persisted pending step use supervisor idempotence instead.
- `done=false` retains its existing eager-read/queued-write behavior and continues to another bounded model step; `done=true` with no requests keeps existing behavior. Read requests remain broker reads and do not consume write slots.
- No automatic retry of a failed/partial batch, no recall of prior messages, no retry of a different payload, no retry/reseed/service or live DB action in this change.

## Proof

RED/GREEN: recorded Head-shaped done+three-recipient-send+task-update becomes accepted; duplicate recipient and seventh mutation refuse; host progress persists across steps; native tool rejection and unchanged schema remain. Supervisor tests prove distinct keys, legacy pending/completed compatibility, payload conflict, prefix committed on injected failure, no completion memo after failure, and idempotent reentry. Compare exact canonical tool hash and all four prompt bytes against e93. Run focused tests with two workers only.

## Measurement condition (Gamma via Alpha)

Persist cumulative per-turn `request_admission` in the host turn value: typed state, requested count, refused count, and truncation boolean/count. Cap, duplicate and pre-dispatch validation refusals reject the entire submitted batch and count every submitted request as refused; never silently truncate it. Successful admission counts are not delivery/acknowledgement counts. An unparsed/partial provider result keeps counts and truncation unknown (`null`), never zero or absent. Add regression checks for cap, duplicate, validation, cumulative steps and unknown output.

Zeta mixed-order regression: both read-first and write-first final mixed batches must refuse with zero reads/actions and known all-refused admission counts. Read-only finals execute their reads and then append the existing completion memo, with known zero refusals. The deadline-after-read regression uses an existing nonfinal mixed batch; interruption retains unknown admission/truncation. This introduces no two-phase final-batch semantics.
