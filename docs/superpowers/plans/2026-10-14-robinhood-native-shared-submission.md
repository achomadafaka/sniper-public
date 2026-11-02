# Robinhood native shared submission implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Execution is authorized end to end by the operator; implement and deploy the reviewed repair without another method-selection pause.

**Goal:** Remove the native Robinhood lane's single-provider send dependency using the existing funded same-bytes transport.

**Architecture:** Expose the already-built chain-4663 submission transport on its context. After the native lane's existing durable signed binding, offer the exact bytes concurrently through that transport and record its provider outcomes asynchronously. Retain legacy/injected sender behavior when no transport exists. Warm the submission-only sequencer on the transport's own pooled client.

**Tech Stack:** Python, existing SubmissionTransport/EvmRpcProvider, pooled HTTPX, SQLite signed binding and asynchronous submission audit.

**Spec:** `docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md`; this amends the sequencer-only send described in `docs/superpowers/specs/2026-10-01-memecoin-4663-fast-lane-design.md`.

## Global constraints

- Chain 4663 only; no nonce, gas, sizing, floor, source/payment proof or confirmation policy change.
- Persist exact signed identity before any provider I/O; derive and compare identity from bytes.
- Timeout, wrong hash, missing reply and all-not-sent remain ambiguous; never release a signed owner on unknown delivery.
- Keep native receipt recovery tag compatible; provider audit identifies actual transport.
- Existing bounded workers/provider leases; no second automatic custody offer after shared fanout.
- No new synchronous RPC/lock/retry on entry; audit queues after transport return, never writes inline on disk-backed ledger.
- Preserve all Europe Solana roles, fee cap, exits, operator kill switch and PAUSED timer.
- Focused pytest only, `-n 2`; no database backup, artificial funded probe or historical retry.

## Review focus

1. Slow provider and a simultaneous keepalive must not hold up an independently accepting provider.
2. Signed bytes/hash/bound owner mismatch must contact no provider and retain existing binding.
3. All providers unavailable, timeout or wrong returned hash must remain unresolved.
4. Late provider completion must retain its original request/owner without borrowing SQLite handles.
5. Tests/injected senders, BSC subclass and explicit no-transport rollback retain their behavior.

## Task 1: Native adapter and durable evidence

**Files:** Modify `sniper/services/memecoin_evm_leg.py` and `sniper/services/memecoin_rh_fast_lane.py`; create `tests/test_memecoin_rh_shared_submission.py`.

**Interface:** Context key `submission_transport` contains the existing transport or None. Consume `build_request`, `guarded_submit`, `_record_fn(async_write=True)` and the existing native signed binding. Return the existing native `accepted/rejected/ambiguous` tuple; record accepted provider and attempt states in the trace. No provider receives a ledger handle.

- [ ] Write behavioral regressions with the existing native-lane fixture and bounded fake providers. Hold sequencer completion with an Event; custody acceptance must return while the sequencer remains blocked. Verify every provider received identical bytes and the reservation was bound before its call. Verify mismatch/all-not-sent/late outcomes and zero redundant reoffer.
- [ ] Root runs the new file with `nice -n 15 ionice -c 3 python3 -m pytest -n 2 tests/test_memecoin_rh_shared_submission.py -q`; record expected RED.
- [ ] Expose the existing transport and adapt only native RH submission after binding. Reuse the existing asynchronous audit callback and typed aggregate; preserve the native recovery backend tag.

```python
request = build_request(chain_id="4663", action="entry", tx_identity=identity,
                        signed_payload=raw, owner_kind="pending", owner_id=str(pending_id))
result = guarded_submit(transport, request, record_fn=record)
```

- [ ] Root reruns the new file plus `tests/test_memecoin_rh_fast_live.py`, `tests/test_exec_intent_fence.py` and `tests/test_memecoin_submission_transport.py`. Confirm no actual network or funded keys were used.

## Task 2: Warm the actual sequencer transport connection

**Files:** Modify `sniper/execution/submission_backends.py` and `tests/test_memecoin_submission_backends.py`.

- [ ] Add a regression proving chain-4663 auto_free's submission-only official endpoint is included in sign-free warming on the same pooled HTTP seam; pooled_rpc/other chains retain their provider policy.
- [ ] Root runs the focused new test and records RED.
- [ ] Add the submission-only endpoint to existing warm_specs using `_warm_call(http, endpoint.url, "eth_chainId")`. A refused read warms the socket but never grants identity/health authority. Disable the unused legacy native keepalive when the shared transport owns sends.
- [ ] Root reruns the backend file and native adapter gates. No new timeout, retry, provider URL or setting.

## Task 3: Review and pinned activation

- [ ] Independent reviewer examines the full changed call graph, ambiguity/late results, injected rollback and BSC inheritance.
- [ ] Root records fixture scheduling p50/p90 and exact incident evidence. Synthetic timing is not an organic landing improvement.
- [ ] Commit logical changes, run schema/connection/knowledge gates, push, create/attach PR and merge the exact reviewed head. Inspect any GitHub failure; do not represent unstarted checks as passing.
- [ ] Deploy the merged SHA through the normal pinned script with focused manifest, required funded core checks, reconciliation/reservation/flat-book/rollback gates and `--defer-backup`.
- [ ] Verify actual runner/web pins, process paths/cgroups, heartbeat, new native transport readiness, unchanged policy/env hashes and PAUSED monitor. Do not generate a trade for proof.

## Incident boundaries

IGNITION a0768f0e: receipt-to-decision 8.098 ms, inline safety 178.592 ms, dispatch 208.873 ms, sequencer ACK 441.307 ms, observed receipt 860.744 ms, acquired tokens at source+5 blocks. Cache verdict was 71.60 seconds old (60-second TTL); a paced refresh had started about eight seconds before ingress and was unfinished. This change does not relax that TTL or remove cold safety. Signing/setup/read portions of the old 232.419 ms send interval are unidentifiable. Same-block Robinhood fills from a sealed-block feed and earlier organic inclusion remain unproved.
