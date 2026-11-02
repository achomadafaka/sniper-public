# Helius Sender SWQoS-only A/B on the Solana slot lane (spec)

Status: BUILT on lambda/sender-ab, 2026-10-01 (Lambda). Default off
(`MEMECOIN_SLOT_LANE_SENDER_AB`); the operator turns it on, and Alpha records the activation time.
Read: Gamma's pre-registration, `docs/audits/2026-10-01-sender-swqos-ab-preregistration.md`
(99679222a). Research: `docs/audits/2026-10-01-solana-delivery-and-pre-block-options.md`.

## 0. Doctrine

The operator said YES on 2026-10-01, via Alpha, to this A/B.
- This reverses the 07-25 no-paid-submission rule
  (`docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md`) for this test
  only.
- Everything else in the 07-25 spec stands:
  - `MEMECOIN_SOLANA_SUBMIT_MODE=sender`, `jito` and `fast_fanout` still fail startup;
  - exits, partial exits, approvals, the J7 lane, the on-chain lane and dip retries never send
    through Sender.
- The only way Sender enters the process is the A/B knob below, on the slot lane's entries.

## 1. What changes

**Arms** (pre-registration rule `sha256-attempt-odd-v1`):
- The arm is B if `int(sha256(attempt_id).hexdigest()[:8], 16)` is odd, else A.
- `attempt_id` is the slot-lane attempt's trace id.
- The arm is fixed before the transaction is built, because the tip instruction depends on it.
- Population:
  - `pump_curve` and `pump_amm` slot-lane attempts only;
  - the CPMM and LaunchLab canary venues stay arm-less on today's path;
  - recorded with `reason=venue_not_in_ab`.

**Arm A:** today's path, byte for byte. No tip.

**Arm B:** the same build, plus ONE `SystemProgram.transfer` of 5,000 lamports from our wallet to a
Helius tip account, appended last. The priority fee per CU is unchanged.
- The tip account is one of Helius's 10, chosen by `sha256(attempt_id)` byte 4, so a trace can
  recompute it.
- The signed bytes go to today's fan-out (custody RPC, chain-owner public endpoints, TPU sidecar)
  AND to `http://fra-sender.helius-rpc.com/fast?swqos_only=true`, in the same transport call.
- Sender is the LAST provider in the fan-out. Every other provider, the TPU sidecar included, is
  dispatched to its worker before Sender is, so Sender never runs before or blocks the TPU dispatch.

**The Sender provider sends only tipped bytes.**
- `SenderProvider.submit` decodes the signed payload. It POSTs only when the payload carries exactly
  one transfer to a Helius tip account of 1 to 10,000 lamports.
- Every other payload is `NOT_SENT`, which is neutral in the aggregate:
  - every arm-A slot-lane send;
  - every exit;
  - every J7 or on-chain lane entry.
- The provider never trusts a flag. The bytes it would send are the evidence.

**Knob and startup guard** (`sniper/settings/memecoin_submission.py`):
- `MEMECOIN_SLOT_LANE_SENDER_AB=1` adds the Sender provider to the Solana transport.
- It is valid only with `auto_free` or `pooled_rpc`; with `rpc` (no shared transport) startup fails.
- `_SOLANA_MODES` is unchanged, so the paid modes still fail startup.
- The lane tips only when the transport really holds the Sender provider (`sender_ab_ready` in the
  executor dict). A knob that is on while the provider is missing means no tips and no arms.

**Tip guard, before signing** (slot lane only, only while the A/B is ready):
- `slot_execute_fn` reads the tip transfers in the compiled message.
- Signing is refused, and the reservation released (`sender_ab_tip_guard`), when the tip seen differs
  from the tip the arm asked for. That covers any tip on arm A, a second tip, and any tip above
  10,000 lamports.
- Nothing is signed or sent on a refusal.

**Trace, per send** (substage `sender_ab`, then `sender_ab_send` after the transport returns):
- `sender_ab`: arm, rule, attempt hash, tip lamports, tip account. Marked before the build.
- `sender_ab_send`:
  - `sender_http_status` and `sender_ms` (the provider's own answer, held per identity because
    the transport may return on another provider's accept first);
  - `sender_state`;
  - `sender_payload_sha_match`: the sha256 Sender sent against the sha256 we signed, which is
    the bytes the TPU path got;
  - `sender_error_run`: consecutive arm-B sends Sender did not accept.
- Sender's attempt also lands in `memecoin_submission_attempts` with every other provider's.
- Decision to send stays in `slot_send_call.elapsed_ms`, transport time in `slot_transport_*`.

**Accounting.**
- A native (SOL-funded) fill's receipt excludes the tip from token cost, like the network fee and
  the new ATA rent (`receipt_sol_entry_fill(tip_lamports=...)`).
  - The pre-sign guard has proved the message carries exactly that tip.
  - A curve buy spends its exact lamports. A tip left in would read as spend over the reservation,
    and the fill would be booked as a `receipt_unreadable` anomaly.
- A USDC-funded fill's SOL surplus credit comes out 5,000 lamports smaller. That fill's cost
  includes the tip, about $0.0007.
- The tip cost is Gamma's measure, read from the trace (`tip_lamports`) and the chain.

## 2. Byte identity within an arm

- One attempt has one signed payload.
  - The arm is fixed before the build, and the lane never rebuilds after signing.
  - The transport fans those exact bytes to every provider, Sender included.
  - The TPU re-sender repeats them under a sha256 check (`SlotResender`, unchanged).
- Arm-B bytes may reach the control providers too. That is the same signature, so it cannot fill
  twice.
- Untipped bytes never reach Sender: Sender would refuse them, and the provider never sends them.
- **Packet fallback.** If the tipped build is over the 1,232-byte packet, the lane builds the control
  transaction instead, BEFORE anything is signed. The trace then records
  `assigned=B, sent=A, reason=tip_over_packet`.
  - Measured since 2026-09-26: the largest signed slot-lane payload is 1,103 bytes (110 of 110 at
    or under 1,182), against about 50 bytes added by the tip. So the fallback is not expected to
    fire.
- An ambiguous result is never re-sent on another path with other bytes. Sender's answer goes
  through the existing money-safe aggregate:
  - Sender's accept is an accept;
  - its timeout or transport loss is `AMBIGUOUS`, so the result is `submitted_unknown` unless another
    provider accepted;
  - its rejection never outweighs another provider's accept.

## 3. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.**
   - On arm B, the signed bytes gain one 5,000-lamport transfer, paid only when the transaction
     lands successfully. A failed transaction reverts it.
   - Delivery gains one provider.
   - Sizing, gates, the price cap, and exits do not change.
2. **Speed.** Measured numbers are in §5.
   - Before the send, arm B adds the tip instruction to the build, one hash for the arm, and the
     pre-sign tip guard.
   - The Sender HTTP call runs on its own transport worker, dispatched after the TPU's. The
     caller returns on the first accept, as today.
   - A DNS stall or a hang on the Sender host holds only Sender's worker and its lease. The next
     arm-B send is then `NOT_SENT provider_busy`.
   - The Sender lease holds one call at a time (Gamma LOW 2). While an arm-B POST is in flight, or
     for the microseconds an untipped send takes to be refused, another send's Sender attempt is
     `NOT_SENT provider_busy`. That is harmless, and it is recorded in
     `sender_ab_send.sender_state`; Gamma reads the delivered share from it.
3. **Concurrency.**
   - No module attribute changes at runtime.
   - The Sender provider's outcome map is guarded by its own lock and capped at 256 identities.
   - The arm is a pure function of `attempt_id`.
   - The knob is read once, at transport build and at lane start.
4. **Absent versus zero versus unknown.**
   - With the knob off or the provider missing, there is no `sender_ab` substage: no arm.
   - Arm A's tip is 0, and the guard refuses any tip there.
   - A Sender call still in flight when the trace is written reads `sender_state=pending`, never a
     status.
   - An unreadable payload is `NOT_SENT unreadable` at Sender.
5. **Dependencies.**
   - **Sender timeout, transport loss or 5xx:** that attempt is `AMBIGUOUS`; another provider's
     accept still wins.
   - **Sender rejection (for example "tip too low"):** that attempt is `EXPLICIT_REJECTED`; it
     never outweighs an accept.
   - **Rate limit (50 tx/s):** two orders of magnitude above our rate.
   - **Wrong echo (identity mismatch):** `AMBIGUOUS`, by the transport's identity check.
   - **One case is more conservative than arm A.** If custody positively rejects while Sender
     answered ambiguously, arm B is `submitted_unknown` where arm A would release at once. Recovery
     then releases it through the proof reads.
6. **Units.**
   - The tip is 5,000 lamports, that is 0.000005 SOL, the documented SWQoS-only minimum.
   - The priority fee stays in micro-lamports per CU.
   - Receipt lamports are compared in integers.
7. **The sample.** The arm comes from our own attempt id, so it is independent of the market. The
   population, censoring and read are Gamma's (pre-registration).
8. **Rollback.**
   - Set the knob to 0 and restart: no Sender provider, no tips, no `sender_ab` substages.
   - ORDER (Gamma LOW 1): turn `MEMECOIN_SLOT_LANE_SENDER_AB` off BEFORE any rollback of
     `MEMECOIN_SOLANA_SUBMIT_MODE` to `rpc`. The knob with `rpc` refuses startup, so a mode
     rollback with the knob still on leaves the runner unable to start.
   - Half-applied (knob on, provider missing): the lane does not tip.
   - Open positions are unaffected. The tip lives only in the entry transaction.

## 4. Checks left behind

On production paths (`SlotLane.handle`, `dispatch_build`, the real `SlotLaneCurveBuilder`, the
real `slot_execute_fn`, `make_solana_send_fn`, `SubmissionTransport`, `SenderProvider`):
- An arm-B send gives Sender and the TPU-path provider the same bytes, with one 5,000-lamport tip. An
  arm-A send has no tip, and Sender never sees it.
- The recorded arm equals the recomputed one, and Sender is the last provider dispatched.
- The guard refuses before signing: a tip on arm A, a tip above 10,000 lamports, or a tipped build
  without an assigned tip.
- Sender refuses untipped payloads; `MEMECOIN_SOLANA_SUBMIT_MODE=sender` still fails startup;
  the knob with `rpc` fails startup.
- Knob off: there is no `sender_ab` substage, and the bytes equal today's.

## 5. Latency (measured, 2026-10-01, this host)

Method:
- The real `SlotLaneCurveBuilder` on the 4020 fixture, 800 builds per arm, interleaved.
- The transport is a real `SubmissionTransport` with an instantly accepting TPU stub, and with the
  real `SenderProvider` over a 20 ms fake HTTP.
- Script: `bench_sender.py` in the branch's review notes (scratch, not committed).

| step | arm A | arm B |
|---|---|---|
| build, p50 / p90 | 193.2 / 201.1 us | 197.6 / 202.5 us |
| arm assignment | 0.8 / 0.9 us | 0.8 / 0.9 us |
| pre-sign tip guard (knob on only) | 4.2 / 4.3 us | 5.2 / 5.3 us |
| transport submit to return, knob off | 234.3 / 335.6 us | n/a |
| transport submit to return, knob on | 231.6 / 346.6 us | 175.2 / 280.6 us |
| signed size | 788 bytes | 837 bytes (+49) |

- Arm B adds about 10 us p50 before the send (build +4.4, arm 0.8, guard 5.2).
- Transport return time is the same within noise, knob on or off, because the caller returns on
  the first accept and Sender runs on its own worker.
- Gamma's stop line is 10 ms. Live, decision to send is read per arm from `slot_send_call` and
  `slot_transport_*`.
