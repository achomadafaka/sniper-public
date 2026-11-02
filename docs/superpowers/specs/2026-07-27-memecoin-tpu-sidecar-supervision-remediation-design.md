# Memecoin TPU Sidecar Supervision Remediation Design

**Status:** Approved for implementation and funded deployment on 2026-07-27.

## Problem

The funded runner can retain a live Rust process whose Unix socket is missing, wedged, or no longer
serving health frames. The monitor checks only the child PID, child stdout/stderr are discarded,
resolver degradation is silent, initial start failure creates no supervisor, and the runner heartbeat
does not expose TPU state. RPC fallback preserves transaction safety, but the TPU optimization can be
lost permanently and invisibly.

## Design

**2026-10-09 amendment:** Parent recovery now distinguishes readiness from responsiveness.
A matching health frame with Boolean `ready` and a nonnegative integer `leader_count` keeps an
alive child running even when `ready=false`, including after the initial bounded health window.
Rust owns stale-head/leader repair and its rebuild backoff. The parent restarts crashes, socket
timeouts/loss and malformed or mismatched replies. Unready still gates delivery as `NOT_SENT`;
the stale-slot bound is unchanged. This corrects the older “health failure” wording below.

`TpuSidecarProcess` remains the sole owner of the child and socket. Its monitor probes the existing
health frame every interval. A dead PID or unhealthy socket enters the existing bounded restart
backoff. The monitor also starts after an unsuccessful initial launch so a valid manifest-bound
sidecar can recover without restarting the funded runner.

A valid but initially unhealthy sidecar remains registered with the shared submission transport.
While unavailable, its provider returns `NOT_SENT` before writing payload bytes; custody and public
RPC providers continue unchanged. This avoids dynamic mutation of a live transport and preserves
same-signed-bytes and ambiguity semantics.

Before each spawn, an existing socket is probed. An unconnectable or unhealthy stale path is removed;
a healthy path is not removed. The child writes combined stdout/stderr to
`logs/memecoin_tpu_sidecar.log`, opened append-only with mode `0600`. The child environment remains
keyless and contains only RPC, optional WS, socket, and the non-secret expected parent PID. The PID
binding preserves parent-death protection when the live parent is PID 1 inside a namespace.

Resolver outcomes use stable, credential-free reason codes. Every degraded outcome logs at warning
level. A valid artifact that fails initial health returns its supervised process plus
`initial_health_unavailable`; invalid or absent manifest/binary evidence returns no process with a
specific reason.

The Solana funded context exposes a sanitized TPU status function. Each
`data/memecoin_runner.heartbeat` write includes `tpu` with `configured`, `ready`, `running`,
`restart_count`, `leader_count`, `last_health_age_s`, and a stable reason. No URL, socket path, raw
child output, payload, key, or exception text enters the heartbeat.

## Failure behavior

- Socket/PID/health failure: restart with bounded backoff; RPC fanout continues.
- Cold-start failure with valid artifact: supervisor retries; provider emits `NOT_SENT` while down.
- Invalid manifest, mutable binary, or hash mismatch: no process/provider; warning plus heartbeat
  reason.
- Partial socket write or lost reply: existing `AMBIGUOUS` behavior is unchanged.
- Healthy sidecar: no restart; leader count and health age refresh continuously.

## Verification and deployment

The 2026-09-27 operator order supersedes the backup wording below: deployments defer backups
unless that deployment explicitly requests one; do not take a post-cutover backup by inference.
For the Python-only 2026-10-09 repair, run the scoped sidecar/transport/build regressions and the
current deploy validator; Rust source and dependencies are unchanged.

Tests must prove RED then GREEN for log redirection, socket-health restart, cold-start recovery,
stale-socket cleanup, typed resolver reasons, provider retention while initially down, and heartbeat
projection. Run focused free-direct/funded tests, all memecoin tests, Rust format/lint/test and binary
self-test, schema validation, compile checks, and `git diff --check`.

Deployment uses one reviewed SHA reachable from `origin/main`, zero pending reservations, and
`scripts/deploy_memecoin_runtime.sh <sha> --restart`. The explicit `--defer-backup` operator override
may skip a blocking predeploy copy without pruning existing backups; it must report
`backup=deferred`, and the consistent backup is taken after post-cutover health proof. The web release
switch remains atomic. Post-cutover proof requires one pinned funded runner, fresh authenticated and bracket
heartbeats, `tpu.ready=true`, a current sidecar log, exact manifest/binary hash verification,
authenticated API/UI success, and zero reservations. No mainnet test transaction is submitted.
