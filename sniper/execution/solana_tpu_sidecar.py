"""Python boundary + owned lifecycle for the keyless Solana TPU sidecar.

``TpuSidecarProvider`` delivers already-signed Solana wire bytes to a local Rust sidecar over a
PRIVATE Unix socket (length-prefixed JSON, big-endian 32-bit length, <= 2 MiB). It never signs,
quotes, refreshes a blockhash, or reads a key — it hands the exact ``request.signed_payload`` to the
sidecar, which independently re-verifies the payload hash + first signature and delivers it over QUIC.

Money-safe mapping (design §6/§8):
- health-gated / frame-too-large / connect failure -> NOT_SENT (provably zero bytes crossed);
- a write that fails with ZERO bytes written -> NOT_SENT; a partial/failed write after any byte, a
  lost reply, an oversized/garbled response frame, or any post-write exception -> AMBIGUOUS;
- a returned ``accepted`` whose identity equals the local identity -> ACCEPTED (leader count surfaced
  in ``response_code``); a differing identity -> AMBIGUOUS (never accepted);
- the sidecar's own ``explicit_rejected`` / ``not_sent`` map straight through.

``TpuSidecarProcess`` owns the child: it passes the custody RPC/WS URLs only through the child
ENVIRONMENT (never argv), waits a bounded time for a no-send health reply, restarts on crash without
killing the runner, closes idempotently, and never logs a URL, payload, key, or raw error.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time as _time
import uuid

from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, _entry_request_slot_refusal,
)

MAX_FRAME_BYTES = 2 * 1024 * 1024          # design §8.2: maximum 2 MiB framed message
_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- framing helpers
def encode_frame(obj) -> bytes:
    """Big-endian 32-bit length prefix + UTF-8 JSON body."""
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _observed_head_slot():
    """Fresh independent processed head; one cached snapshot, no RPC or entry lock."""
    signal = sys.modules.get("sniper.services.memecoin_slot_signal")
    head = getattr(signal, "HEAD", None)
    if not isinstance(head, dict):
        return None
    head = head.copy()
    slot, mono = head.get("slot"), head.get("mono")
    if (isinstance(slot, int) and not isinstance(slot, bool) and slot > 0
            and isinstance(mono, (int, float)) and 0 <= _time.monotonic() - mono <= 1.0):
        return slot
    return None


def _recv_exact(recv, n):
    buf = b""
    while len(buf) < n:
        chunk = recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_message(recv, max_bytes=MAX_FRAME_BYTES):
    """Read one framed JSON message via a ``recv(n)->bytes`` callable. Returns the parsed dict, or
    ``None`` on EOF, a zero/over-``max_bytes`` length, a short read, or a decode/transport error. An
    over-bound length is refused WITHOUT allocating it."""
    try:
        header = _recv_exact(recv, 4)
        if header is None:
            return None
        length = struct.unpack(">I", header)[0]
        if length == 0 or length > max_bytes:
            return None
        body = _recv_exact(recv, length)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))
    except (OSError, ValueError, struct.error):
        return None


def _send_all(conn, data):
    """Manual send loop returning the total bytes written; raises OSError on failure (the caller
    inspects the returned count to distinguish zero-write NOT_SENT from partial-write AMBIGUOUS)."""
    written = 0
    while written < len(data):
        n = conn.send(data[written:])
        if not n:
            raise OSError("socket closed mid-write")
        written += n
    return written


def _default_connect(socket_path, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(socket_path)
    return s


# --------------------------------------------------------------------------- provider
class TpuSidecarProvider:
    provider_id = "solana:tpu"

    def __init__(self, socket_path, *, health=None, connect_fn=None, connect_timeout_s=0.5,
                 io_timeout_s=1.0, clock=None, request_id_fn=None):
        self._socket_path = socket_path
        self._health = health
        self._connect_fn = connect_fn or _default_connect
        self._connect_timeout = float(connect_timeout_s)
        self._io_timeout = float(io_timeout_s)
        self._clock = clock or _time.monotonic
        self._request_id_fn = request_id_fn or (lambda: uuid.uuid4().hex)

    def _attempt(self, state, identity, latency, reason, *, response_code=None, provider_timing=None):
        return SubmissionAttempt(self.provider_id, state, identity, latency, response_code, reason, provider_timing=provider_timing)

    def submit(self, request):
        if self._health is not None and not self._health.should_attempt(self.provider_id):
            return self._attempt(AttemptState.NOT_SENT, None, 0.0, "health_gated")
        started = self._clock()

        def lat():
            return (self._clock() - started) * 1000.0

        maximum = (request.entry_max_slot
                   if request.chain_id == "solana" and request.action == "entry" else None)
        if maximum is not None and (type(maximum) is not int or not 0 < maximum < 2**64):
            return self._attempt(AttemptState.NOT_SENT, None, lat(), "entry_slot_deadline_invalid")
        refused = _entry_request_slot_refusal(request, clock=self._clock)
        if refused:
            return self._attempt(AttemptState.NOT_SENT, None, lat(), refused)
        request_id = self._request_id_fn()
        frame = encode_frame({
            "request_id": request_id,
            "tx_identity": request.tx_identity,
            "signed_payload_base64": request.signed_payload,
            "signed_payload_sha256": request.signed_payload_sha256,
            "last_valid_block_height": request.last_valid_height,
            "min_context_slot": _observed_head_slot(),
            **({"entry_max_slot": maximum} if maximum is not None else {}),
        })
        if len(frame) > MAX_FRAME_BYTES:
            return self._attempt(AttemptState.NOT_SENT, None, lat(), "frame_too_large")
        try:
            conn = self._connect_fn(self._socket_path, self._connect_timeout)
        except OSError:
            return self._attempt(AttemptState.NOT_SENT, None, lat(), "sidecar_unavailable")
        try:
            try:
                conn.settimeout(self._io_timeout)
            except Exception:
                pass
            # Manual send loop so the bytes-written count is visible here: a failure with ZERO bytes
            # written is provably NOT_SENT; a failure after any byte is AMBIGUOUS (bytes may be on the
            # wire). This distinction is load-bearing for money safety.
            written = 0
            failed = False
            try:
                while written < len(frame):
                    # Connecting or a partial write can outlive the outer transport's check.
                    refused = _entry_request_slot_refusal(request, clock=self._clock)
                    if refused:
                        return self._attempt(AttemptState.AMBIGUOUS if written else AttemptState.NOT_SENT,
                                             None, lat(), refused)
                    n = conn.send(frame[written:])
                    if not n:
                        raise OSError("socket closed mid-write")
                    written += n
            except OSError:
                failed = True
            if failed:
                if written == 0:
                    return self._attempt(AttemptState.NOT_SENT, None, lat(), "write_failed")
                return self._attempt(AttemptState.AMBIGUOUS, None, lat(), "partial_write")
            resp = read_message(conn.recv, MAX_FRAME_BYTES)
            if not isinstance(resp, dict):
                return self._attempt(AttemptState.AMBIGUOUS, None, lat(), "reply_lost")
            if str(resp.get("request_id") or "") != str(request_id):
                return self._attempt(
                    AttemptState.AMBIGUOUS, None, lat(), "request_id_mismatch")
            return self._classify(request, resp, lat())
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _classify(self, request, resp, latency):
        state = str(resp.get("state") or "")
        ident = resp.get("tx_identity")
        reason = resp.get("reason_code")
        leader = resp.get("leader_count")
        timing = None
        if (ident is not None and str(ident) == str(request.tx_identity)
                and isinstance(resp.get("guard"), dict) and "request_min_context_slot" in resp):
            timing = {"tpu_guard": resp["guard"],
                      "request_min_context_slot": resp["request_min_context_slot"]}
        if state == "accepted":
            if ident is None or str(ident) != str(request.tx_identity):
                return self._attempt(AttemptState.AMBIGUOUS, ident, latency, "identity_mismatch")
            return self._attempt(AttemptState.ACCEPTED, str(ident), latency, reason,
                                 response_code=(None if leader is None else str(leader)), provider_timing=timing)
        if state == "explicit_rejected":
            return self._attempt(AttemptState.EXPLICIT_REJECTED, None, latency, reason or "rejected", provider_timing=timing)
        if state == "not_sent":
            return self._attempt(AttemptState.NOT_SENT, None, latency, reason or "sidecar_not_sent", provider_timing=timing)
        return self._attempt(AttemptState.AMBIGUOUS, None, latency, reason or "sidecar_ambiguous", provider_timing=timing)


# --------------------------------------------------------------------------- owned process
def _default_spawn(*, binary_path, socket_path, env):
    import subprocess
    socket_dir = os.path.dirname(os.path.abspath(socket_path))
    state_root = os.path.dirname(socket_dir) if os.path.basename(socket_dir) == "data" else socket_dir
    log_dir = os.path.join(state_root, "logs")
    log_path = os.path.join(log_dir, "memecoin_tpu_sidecar.log")
    os.makedirs(log_dir, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(log_path, 0o600)
    log_file = os.fdopen(fd, "ab", buffering=0)
    try:
        return subprocess.Popen(
            [binary_path], env=env, stdout=log_file, stderr=subprocess.STDOUT)
    finally:
        log_file.close()


class TpuSidecarProcess:
    provider_id = "solana:tpu"

    def __init__(self, *, binary_path, socket_path, rpc_url, ws_url=None, spawn_fn=None,
                 # Must exceed the Rust sidecar's 1.5s whole-request deadline. A shorter outer
                 # timeout mistakes a valid slow custody-RPC health reply for a wedged sidecar.
                 connect_fn=None, start_timeout_s=5.0, health_timeout_s=2.0, poll_interval_s=0.05,
                 monitor_interval_s=1.0, health_failure_threshold=3,
                 restart_backoff_s=(1.0, 2.0, 4.0, 8.0, 30.0),
                 clock=None):
        self._binary_path = binary_path
        self._socket_path = socket_path
        self._rpc_url = rpc_url
        self._ws_url = ws_url
        self._spawn_fn = spawn_fn or _default_spawn
        self._connect_fn = connect_fn or _default_connect
        self._start_timeout = float(start_timeout_s)
        self._health_timeout = float(health_timeout_s)
        self._poll_interval = float(poll_interval_s)
        self._monitor_interval = float(monitor_interval_s)
        self._health_failure_threshold = max(1, int(health_failure_threshold))
        self._restart_backoff = tuple(float(v) for v in restart_backoff_s) or (1.0,)
        self._clock = clock or _time.monotonic
        self._proc = None
        self._restart_count = 0
        self._last_leader_count = None
        self._last_leader_slot = None
        self._last_health = None
        self._ready = False
        self._health_responsive = False
        self._last_reason = "not_started"
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread = None

    # --- spawn / start / restart ---------------------------------------------------------------
    def _spawn(self):
        # Deliberately do NOT inherit the funded runner environment. The sidecar is keyless by
        # process boundary, not merely by convention: it receives only its two read endpoints and
        # private socket path, never wallet keys, bot tokens, DB credentials, or unrelated config.
        env = {"SNIPER_TPU_RPC_URL": self._rpc_url,
               "SNIPER_TPU_SOCKET_PATH": self._socket_path,
               "SNIPER_TPU_PARENT_PID": str(os.getpid())}
        if self._ws_url:
            env["SNIPER_TPU_WS_URL"] = self._ws_url
        return self._spawn_fn(binary_path=self._binary_path, socket_path=self._socket_path, env=env)

    def _remove_unhealthy_stale_socket_unlocked(self):
        if not self._socket_path or not os.path.lexists(self._socket_path):
            return
        if self._health_unlocked().get("ready"):
            return
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

    def _start_unlocked(self):
        self._remove_unhealthy_stale_socket_unlocked()
        try:
            self._proc = self._spawn()
        except Exception:
            self._proc = None
            self._ready = False
            self._last_reason = "spawn_failed"
            return False
        deadline = self._clock() + self._start_timeout
        while self._clock() < deadline:
            try:
                if self._proc.poll() is not None:
                    break                              # child exited before becoming healthy
            except Exception:
                pass
            h = self._health_unlocked()
            if h.get("ready"):
                return True
            _time.sleep(self._poll_interval)
        # Rust owns stale-head/leader repair; preserve its backoff while it still answers.
        if not (self._health_responsive and self._is_alive(self._proc)):
            self._terminate_proc()
        self._ready = False
        if self._last_reason == "ready":
            self._last_reason = "initial_health_unavailable"
        return False

    def start(self) -> bool:
        with self._lock:
            if self._proc is not None and self._is_alive(self._proc):
                return self._ready
            self._monitor_stop.clear()
            ok = self._start_unlocked()
            self._start_monitor_unlocked()
            return ok

    def _note_restart(self, reason: str) -> None:
        """Count a restart and say so, in ONE place so the two can never disagree.

        WHY THIS EXISTS (2026-09-23). The sidecar restarted 11 times in 57 minutes and nothing
        recorded WHEN or WHY: this module emitted no log line of any kind, so the only witness was
        `restart_count` in the heartbeat, which is a running total with no timestamps. Working out
        whether the restarts clustered at a DNS failure or ran all night took a live sampler and an
        inference, and it could not be answered for the hour that had already passed.

        The increment lives here WITH the line rather than beside it, so a future edit cannot leave
        the heartbeat counter and the log disagreeing about how many restarts there were.
        """
        self._restart_count += 1
        _LOG.warning("memecoin: solana tpu sidecar restart #%d (%s)",
                     self._restart_count, reason)

    def restart(self) -> bool:
        with self._lock:
            self._terminate_proc()
            self._note_restart("requested")
            ok = self._start_unlocked()
            self._start_monitor_unlocked()
            return ok

    def _start_monitor_unlocked(self):
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="memecoin-tpu-monitor", daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self):
        backoff_i = 0
        unhealthy_streak = 0
        while not self._monitor_stop.wait(self._monitor_interval):
            with self._lock:
                alive = self._proc is not None and self._is_alive(self._proc)
                healthy = alive and bool(self._health_unlocked().get("ready"))
                responsive = alive and self._health_responsive
            if healthy:
                backoff_i = 0
                unhealthy_streak = 0
                continue
            if alive:
                if responsive:
                    unhealthy_streak = 0
                    continue
                unhealthy_streak += 1
                if unhealthy_streak < self._health_failure_threshold:
                    continue
            delay = self._restart_backoff[min(backoff_i, len(self._restart_backoff) - 1)]
            if self._monitor_stop.wait(delay):
                return
            with self._lock:
                if self._monitor_stop.is_set():
                    return
                alive = self._proc is not None and self._is_alive(self._proc)
                healthy = alive and bool(self._health_unlocked().get("ready"))
                if alive and self._health_responsive:
                    if healthy:
                        backoff_i = 0
                    unhealthy_streak = 0
                    continue
                self._terminate_proc()
                self._note_restart("crashed" if not alive else "unhealthy")
                ok = self._start_unlocked()
            backoff_i = 0 if ok else backoff_i + 1
            unhealthy_streak = 0

    # --- health ---------------------------------------------------------------------------------
    def health(self) -> dict:
        with self._lock:
            return self._health_unlocked()

    def _health_unlocked(self) -> dict:
        self._health_responsive = False
        request_id = uuid.uuid4().hex
        obj = {"request_id": request_id, "health": True,
               "min_context_slot": _observed_head_slot()}
        try:
            conn = self._connect_fn(self._socket_path, self._health_timeout)
        except OSError:
            self._ready = False
            self._last_reason = "socket_unavailable"
            return {"ready": False, "leader_count": None}
        resp = None
        try:
            try:
                conn.settimeout(self._health_timeout)
            except Exception:
                pass
            _send_all(conn, encode_frame(obj))
            resp = read_message(conn.recv, MAX_FRAME_BYTES)
        except OSError:
            resp = None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        leader_count = resp.get("leader_count") if isinstance(resp, dict) else None
        self._health_responsive = (
            isinstance(resp, dict) and resp.get("request_id") == request_id
            and type(resp.get("ready")) is bool
            and type(leader_count) is int and leader_count >= 0)
        if self._health_responsive and resp["ready"] and leader_count > 0:
            self._last_leader_count = leader_count
            self._last_leader_slot = resp.get("leader_slot")
            self._last_health = self._clock()
            self._ready = True
            self._last_reason = "ready"
            return {"ready": True, "leader_count": leader_count}
        self._ready = False
        self._last_reason = "health_unready"
        return {"ready": False, "leader_count": None}

    # --- provider / status / close --------------------------------------------------------------
    def provider(self, health=None) -> TpuSidecarProvider:
        return TpuSidecarProvider(self._socket_path, health=health, connect_fn=self._connect_fn,
                                  io_timeout_s=self._health_timeout, clock=self._clock)

    def _is_alive(self, proc) -> bool:
        try:
            return proc.poll() is None
        except Exception:
            return proc is not None

    def sanitized_status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._is_alive(self._proc)
            last = self._last_health
            age = None if last is None else round(self._clock() - last, 3)
            return {"provider": self.provider_id, "running": bool(running),
                    "ready": bool(self._ready), "reason": self._last_reason,
                    "restart_count": self._restart_count, "leader_count": self._last_leader_count,
                    "leader_slot": self._last_leader_slot,
                    "last_health_age_s": age}

    def _terminate_proc(self):
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # best-effort private-socket cleanup so a restart can rebind the same path
        try:
            if self._socket_path and os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

    def close(self) -> None:
        self._monitor_stop.set()
        with self._lock:
            self._terminate_proc()
            thread = self._monitor_thread
            self._monitor_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)


def _verify_binary_hash(path, expected_sha256):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == str(expected_sha256).lower()
    except OSError:
        return False


def _immutable_executable(path):
    """The runner may execute only a regular, non-symlink, non-writable sidecar artifact."""
    try:
        if os.path.islink(path):
            return False
        info = os.stat(path, follow_symlinks=False)
        mode = info.st_mode
        return (stat.S_ISREG(mode) and not (mode & 0o222) and bool(mode & 0o111))
    except OSError:
        return False


@dataclass(frozen=True)
class TpuSidecarResolution:
    process: object | None
    reason: str


def _resolution(process, reason):
    if reason != "ready":
        _LOG.warning("memecoin TPU sidecar degraded reason=%s", reason)
    return TpuSidecarResolution(process=process, reason=reason)


def resolve_tpu_sidecar(*, rpc_url, ws_url=None, env=None, manifest_path=None, socket_path=None,
                        clock=None, process_factory=None):
    """Best-effort start of the manifest-bound keyless TPU sidecar for auto_free.

    Returns ``TpuSidecarResolution(process, reason)``. A valid manifest-bound process is retained even
    when its initial health check fails because its monitor keeps retrying; invalid deployment
    evidence returns ``process=None``. NEVER raises and never logs raw exception text or endpoints."""
    source_env = os.environ if env is None else env
    if manifest_path is None:
        manifest_path = source_env.get("SNIPER_MEMECOIN_DEPLOY_MANIFEST")
    if manifest_path is None:
        try:
            from sniper.util.state_paths import state_path
            manifest_path = str(state_path("data", "memecoin_deploy.json", env=env))
        except Exception:
            return _resolution(None, "manifest_path_unresolvable")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return _resolution(None, "manifest_unreadable")
    sidecar = manifest.get("sidecar") if isinstance(manifest, dict) else None
    if not isinstance(sidecar, dict):
        return _resolution(None, "sidecar_manifest_missing")
    binary = sidecar.get("path")
    expected = sidecar.get("sha256")
    if not binary or not expected or not _immutable_executable(binary):
        return _resolution(None, "binary_unusable")
    if not _verify_binary_hash(binary, expected):
        return _resolution(None, "binary_hash_mismatch")
    if socket_path is None:
        socket_path = source_env.get("SNIPER_TPU_SOCKET_PATH_OVERRIDE")
    if socket_path is None:
        try:
            from sniper.util.state_paths import state_path
            socket_path = str(state_path("data", "memecoin-tpu-sidecar.sock", env=env))
        except Exception:
            return _resolution(None, "socket_path_unresolvable")
    factory = process_factory or TpuSidecarProcess
    try:
        proc = factory(binary_path=binary, socket_path=socket_path, rpc_url=rpc_url,
                       ws_url=ws_url, clock=clock)
    except Exception:
        return _resolution(None, "process_construct_failed")
    try:
        if proc.start():
            return _resolution(proc, "ready")
    except Exception:
        try:
            proc.close()
        except Exception:
            pass
        return _resolution(None, "initial_start_exception")
    return _resolution(proc, "initial_health_unavailable")
