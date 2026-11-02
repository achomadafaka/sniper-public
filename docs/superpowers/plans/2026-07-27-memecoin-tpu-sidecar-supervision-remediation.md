# Memecoin TPU Sidecar Supervision Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a valid pinned TPU sidecar observable and self-healing across socket loss, process
wedges, and cold-start failure without weakening funded submission safety.

**Architecture:** A process-lifetime supervisor probes the existing health frame and restarts through
bounded backoff. The provider stays in the immutable transport even while down because its pre-write
failure is typed `NOT_SENT`; the runner heartbeat projects sanitized supervisor state.

**Tech Stack:** Python 3.13, Unix domain sockets, subprocess, pytest, Rust/Cargo, Bash deployment.

## Global Constraints

- Never log endpoints, keys, payloads, signatures, raw provider errors, or child environment values.
- Never turn a zero-byte sidecar failure into `AMBIGUOUS`, or a partial write into `NOT_SENT`.
- Preserve custody/public RPC fallback and same-signed-bytes fanout.
- Do not submit a funded or mainnet smoke transaction.
- Deploy only an exact SHA reachable from `origin/main` after zero-reservation and reconciliation gates.

---

### Task 1: Process supervision and durable logs

**Files:**
- Modify: `sniper/execution/solana_tpu_sidecar.py`
- Test: `tests/test_memecoin_solana_tpu_sidecar.py`

**Interfaces:**
- Produces: `TpuSidecarResolution(process, reason)` and enhanced
  `TpuSidecarProcess.sanitized_status() -> dict`.

- [ ] Add failing tests proving default spawn appends combined output to a `0600` state-root log.
- [ ] Add failing tests proving a live PID with failed health is restarted.
- [ ] Add a failing test proving failed initial start leaves a monitor that later recovers.
- [ ] Add a failing test proving an unhealthy stale socket path is removed before spawn.
- [ ] Add failing tests for each resolver reason class and warning emission.
- [ ] Implement the minimal log, health-monitor, cold-start, stale-socket, and resolution changes.
- [ ] Run `python3 -m pytest tests/test_memecoin_solana_tpu_sidecar.py -q`.
- [ ] Commit the process-lifecycle change with its tests.

### Task 2: Transport retention and funded heartbeat

**Files:**
- Modify: `sniper/execution/submission_backends.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_runner.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Test: `tests/test_memecoin_submission_backends.py`
- Test: `tests/test_memecoin_real_context.py`
- Test: `tests/test_memecoin_sweep_liveness.py`

**Interfaces:**
- Consumes: `TpuSidecarResolution.process` and `TpuSidecarProcess.sanitized_status()`.
- Produces: Solana context `tpu_status_fn()` and heartbeat `tpu` object.

- [ ] Add a failing test proving an initially unhealthy supervised provider remains registered and
  returns `NOT_SENT` without affecting RPC providers.
- [ ] Add a failing context test proving resolver reason and live supervisor state are sanitized.
- [ ] Add a failing heartbeat test proving the `tpu` field is persisted atomically.
- [ ] Implement provider retention, context status projection, and both heartbeat call sites.
- [ ] Run the focused backend, context, heartbeat, and sidecar tests.
- [ ] Commit the transport/heartbeat change with its tests.

### Task 3: Verification, integration, and deployment

**Files:**
- Validate the complete branch; no new production interface is introduced here.

- [ ] Run all focused shared-submission, funded-runner, EVM safety, alert, and UI tests.
- [ ] Run `python3 -m pytest tests/test_memecoin_*.py -q`.
- [ ] Run schema validation and Python compile checks.
- [ ] Run Rust format, lint, tests, release build, and sidecar self-test.
- [ ] Run the exact RIALTOES read-only funded safety simulation.
- [ ] Complete the Knowledge Sync Gate and commit the explicit knowledge delta.
- [ ] Push the reviewed linear SHA to `origin/main`.
- [ ] Confirm zero reservations, deploy the pinned funded runtime, and verify the manifest/binary.
- [ ] Atomically switch the web release and restart the systemd-owned web service.
- [ ] Verify one runner, fresh auth/bracket/TPU heartbeat, sidecar log output, authenticated API/UI,
  exact pins, and zero reservations.
