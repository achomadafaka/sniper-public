"""Task 5 — Python TPU sidecar boundary + owned process lifecycle.

The provider delivers already-signed Solana bytes to a keyless local Rust sidecar over a private Unix
socket (length-prefixed JSON, <= 2 MiB). Money-safe mapping: nothing written -> NOT_SENT; any partial
write / lost reply / post-write exception -> AMBIGUOUS; a matching accepted identity -> ACCEPTED; a
returned identity that differs -> AMBIGUOUS (never accepted); the sidecar's own explicit rejection ->
EXPLICIT_REJECTED. The owned process passes RPC/WS URLs only through the child ENVIRONMENT (never
argv), restarts on crash without killing the runner, closes idempotently, and never logs secrets.

All offline: the provider uses an injected ``connect_fn`` (a scripted FakeConn) and one real Unix
socket smoke; the process uses an injected ``spawn_fn`` that starts an in-thread fake sidecar.
"""
import base64
import json
import os
import socket
import struct
import threading

import pytest

from sniper.execution import solana_tpu_sidecar as S
from sniper.execution.submission_transport import AttemptState, build_request


@pytest.fixture(autouse=True)
def _fixed_request_id(monkeypatch):
    class FixedUuid:
        hex = "r"

    monkeypatch.setattr(S.uuid, "uuid4", lambda: FixedUuid())


def _req(identity="SIG_TPU", payload_b64=None):
    payload = payload_b64 or base64.b64encode(b"signed-wire-bytes").decode()
    return build_request(chain_id="solana", action="entry", tx_identity=identity,
                         signed_payload=payload, last_valid_height=222)


class FakeConn:
    """Scripted socket-like object. ``resp`` is the framed bytes recv() will yield; ``send_limit``
    caps how many bytes send() accepts before raising OSError; ``fail_send`` raises on first send."""

    def __init__(self, resp=b"", *, send_limit=None, fail_send=False):
        self._resp = bytearray(resp)
        self._send_limit = send_limit
        self._fail_send = fail_send
        self.sent = bytearray()
        self.closed = False
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def send(self, data):
        if self._fail_send:
            raise OSError("peer reset before any byte")
        if self._send_limit is not None:
            room = self._send_limit - len(self.sent)
            if room <= 0:
                raise OSError("peer closed mid-write")
            data = data[:room]
        self.sent += data
        return len(data)

    def recv(self, n):
        if not self._resp:
            return b""
        chunk = bytes(self._resp[:n])
        del self._resp[:n]
        return chunk

    def close(self):
        self.closed = True


def _resp_frame(**fields):
    return S.encode_frame(fields)


def _conn_factory(conn):
    def connect_fn(path, timeout):
        return conn
    return connect_fn


# --------------------------------------------------------------------------- provider: NOT_SENT
def test_absent_socket_is_not_sent():
    def connect_fn(path, timeout):
        raise FileNotFoundError("no such socket")
    prov = S.TpuSidecarProvider("/nope/tpu.sock", connect_fn=connect_fn)
    a = prov.submit(_req())
    assert a.state is AttemptState.NOT_SENT and a.reason_code == "sidecar_unavailable"


def test_health_gated_is_not_sent_without_touching_socket():
    from sniper.execution.submission_health import HealthRegistry
    health = HealthRegistry(["solana:tpu"])
    health.record_wrong_chain("solana:tpu", "bad")           # exclude
    touched = []

    def connect_fn(path, timeout):
        touched.append(1)
        raise AssertionError("must not connect when health-gated")
    prov = S.TpuSidecarProvider("/x.sock", health=health, connect_fn=connect_fn)
    a = prov.submit(_req())
    assert a.state is AttemptState.NOT_SENT and not touched


def test_frame_too_large_is_not_sent_before_connect():
    touched = []

    def connect_fn(path, timeout):
        touched.append(1)
        return FakeConn()
    huge = base64.b64encode(b"x" * (S.MAX_FRAME_BYTES + 10)).decode()
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=connect_fn)
    a = prov.submit(_req(payload_b64=huge))
    assert a.state is AttemptState.NOT_SENT and a.reason_code == "frame_too_large"
    assert not touched                                       # never connected -> zero bytes crossed


def test_zero_byte_write_failure_is_not_sent():
    conn = FakeConn(fail_send=True)
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req())
    assert a.state is AttemptState.NOT_SENT               # provably nothing crossed the boundary
    assert len(conn.sent) == 0


# --------------------------------------------------------------------------- provider: AMBIGUOUS
def test_partial_write_is_ambiguous():
    conn = FakeConn(send_limit=4)                          # accepts a few bytes then the peer closes
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req())
    assert a.state is AttemptState.AMBIGUOUS              # some bytes may be on the wire
    assert len(conn.sent) > 0


def test_lost_reply_after_full_write_is_ambiguous():
    conn = FakeConn(resp=b"")                              # full write, but the sidecar reply is lost
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req())
    assert a.state is AttemptState.AMBIGUOUS
    assert a.reason_code == "reply_lost"


def test_returned_identity_mismatch_is_ambiguous_never_accepted():
    conn = FakeConn(_resp_frame(request_id="r", state="accepted", tx_identity="DIFFERENT",
                                leader_count=3, reason_code=None))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req(identity="SIG_TPU"))
    assert a.state is AttemptState.AMBIGUOUS and a.reason_code == "identity_mismatch"


def test_response_request_id_mismatch_is_ambiguous_even_for_rejection():
    conn = FakeConn(_resp_frame(request_id="stale", state="explicit_rejected",
                                tx_identity="SIG_TPU", leader_count=0,
                                reason_code="hash_mismatch"))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req())
    assert a.state is AttemptState.AMBIGUOUS
    assert a.reason_code == "request_id_mismatch"


def test_unknown_state_is_ambiguous():
    conn = FakeConn(_resp_frame(request_id="r", state="weird", tx_identity="SIG_TPU",
                                leader_count=0, reason_code=None))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    assert prov.submit(_req()).state is AttemptState.AMBIGUOUS


# --------------------------------------------------------------------------- provider: ACCEPTED / REJECT / NOT_SENT
def test_accepted_matching_identity():
    conn = FakeConn(_resp_frame(request_id="r", state="accepted", tx_identity="SIG_TPU",
                                leader_count=5, reason_code=None))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req(identity="SIG_TPU"))
    assert a.state is AttemptState.ACCEPTED
    assert a.returned_identity == "SIG_TPU"
    assert a.response_code == "5"                          # leader count surfaced (URL-free)
    # the request framed exactly the signed bytes + identity + validity, nothing more
    length = struct.unpack(">I", bytes(conn.sent[:4]))[0]
    body = json.loads(bytes(conn.sent[4:4 + length]).decode())
    assert body["tx_identity"] == "SIG_TPU"
    assert body["last_valid_block_height"] == 222
    assert set(body) == {"request_id", "tx_identity", "signed_payload_base64",
                         "signed_payload_sha256", "last_valid_block_height", "min_context_slot"}


def test_sidecar_explicit_rejection_maps_through():
    conn = FakeConn(_resp_frame(request_id="r", state="explicit_rejected", tx_identity="SIG_TPU",
                                leader_count=0, reason_code="hash_mismatch"))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    a = prov.submit(_req())
    assert a.state is AttemptState.EXPLICIT_REJECTED and a.reason_code == "hash_mismatch"


def test_sidecar_not_sent_maps_through():
    conn = FakeConn(_resp_frame(request_id="r", state="not_sent", tx_identity="SIG_TPU",
                                leader_count=0, reason_code="no_leader"))
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    assert prov.submit(_req()).state is AttemptState.NOT_SENT


def test_oversized_response_frame_is_ambiguous_not_a_crash():
    # a length prefix beyond the 2 MiB bound must be rejected without allocating it
    conn = FakeConn(struct.pack(">I", S.MAX_FRAME_BYTES + 1) + b"{}")
    prov = S.TpuSidecarProvider("/x.sock", connect_fn=_conn_factory(conn))
    assert prov.submit(_req()).state is AttemptState.AMBIGUOUS


# --------------------------------------------------------------------------- real Unix-socket smoke
def _serve_once(srv, response_obj):
    srv.settimeout(2.0)
    try:
        conn, _ = srv.accept()
        with conn:
            hdr = conn.recv(4)
            n = struct.unpack(">I", hdr)[0]
            body = b""
            while len(body) < n:
                body += conn.recv(n - len(body))
            req = json.loads(body.decode())
            resp = dict(response_obj)
            resp["request_id"] = req["request_id"]
            resp.setdefault("tx_identity", req["tx_identity"])
            conn.sendall(S.encode_frame(resp))
    except Exception:
        pass
    finally:
        srv.close()


def test_real_unix_socket_accept_roundtrip(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")
    # bind + listen in the MAIN thread so the socket is ready before the provider connects
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    t = threading.Thread(target=_serve_once,
                         args=(srv, {"state": "accepted", "leader_count": 4}), daemon=True)
    t.start()
    try:
        prov = S.TpuSidecarProvider(sock_path, io_timeout_s=2.0)
        a = prov.submit(_req(identity="SIG_TPU"))
        assert a.state is AttemptState.ACCEPTED and a.returned_identity == "SIG_TPU"
    finally:
        t.join(timeout=2.0)


# --------------------------------------------------------------------------- owned process lifecycle
class FakeSidecarServer:
    """An in-thread fake sidecar: binds the socket, answers health frames, and echoes a scripted
    submit verdict. Stands in for the Rust binary so the process lifecycle is testable offline."""

    def __init__(self, sock_path, *, leader_count=6, health_delay_s=0.0):
        self.sock_path = sock_path
        self.leader_count = leader_count
        self.health_delay_s = float(health_delay_s)
        self.health_requests = 0
        self.health_slots = []
        self._stop = threading.Event()
        self._thread = None
        self.started = False

    def start_env(self, env):
        # a spawn_fn stand-in: assert URLs arrive via ENV, never argv, then serve.
        assert env.get("SNIPER_TPU_RPC_URL")
        self.started = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.sock_path)
        except OSError:
            return
        srv.listen(8)
        srv.settimeout(0.25)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                try:
                    hdr = conn.recv(4)
                    if len(hdr) < 4:
                        continue
                    n = struct.unpack(">I", hdr)[0]
                    body = b""
                    while len(body) < n:
                        body += conn.recv(n - len(body))
                    req = json.loads(body.decode())
                    if req.get("health"):
                        self.health_requests += 1
                        self.health_slots.append(req.get("min_context_slot"))
                        if self.health_delay_s:
                            self._stop.wait(self.health_delay_s)
                        resp = {"request_id": req["request_id"], "ready": self.leader_count > 0,
                                "leader_count": self.leader_count}
                    else:
                        resp = {"request_id": req["request_id"], "state": "accepted",
                                "tx_identity": req["tx_identity"],
                                "leader_count": self.leader_count, "reason_code": None}
                    conn.sendall(S.encode_frame(resp))
                except Exception:
                    pass
        srv.close()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def terminate(self):
        self._stop.set()

    def poll(self):
        return None if self.is_alive() else 0

    def kill(self):
        self._stop.set()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)
        return 0


def _spawn_fn_factory(server):
    def spawn_fn(*, binary_path, socket_path, env):
        assert "http" not in " ".join([binary_path, socket_path])   # no URL in the argv-ish inputs
        return server.start_env(env)
    return spawn_fn


def test_default_spawn_appends_combined_output_to_private_state_log(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    binary = tmp_path / "fake-sidecar"
    binary.write_text("#!/bin/sh\necho sidecar-out\necho sidecar-err >&2\n")
    binary.chmod(0o555)

    proc = S._default_spawn(
        binary_path=str(binary), socket_path=str(data_dir / "tpu.sock"), env={})
    assert proc.wait(timeout=2.0) == 0

    log_path = tmp_path / "logs" / "memecoin_tpu_sidecar.log"
    assert log_path.read_text().splitlines() == ["sidecar-out", "sidecar-err"]
    assert os.stat(log_path).st_mode & 0o777 == 0o600


def test_process_starts_passes_urls_via_env_and_reports_health(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path)
    proc = S.TpuSidecarProcess(binary_path="/opt/tpu-bin", socket_path=sock_path,
                               rpc_url="https://secret-custody/rpc", ws_url="wss://secret-custody/ws",
                               spawn_fn=_spawn_fn_factory(server), start_timeout_s=3.0)
    try:
        assert proc.start() is True
        assert server.started is True
        h = proc.health()
        assert h["ready"] is True and h["leader_count"] == 6
        # a provider built from the process talks to the same socket
        a = proc.provider().submit(_req(identity="SIG_TPU"))
        assert a.state is AttemptState.ACCEPTED
    finally:
        proc.close()


def test_default_health_timeout_exceeds_sidecar_request_deadline(tmp_path):
    """A valid slow health reply must not cause an RPC-only restart window.

    The Rust sidecar bounds a whole request at 1.5s. The parent timeout must be longer than that
    inner deadline; production custody health calls have exceeded the old 1.0s parent timeout.
    """
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path, health_delay_s=1.2)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server), start_timeout_s=2.5)
    try:
        assert proc.start() is True
        assert proc.sanitized_status()["restart_count"] == 0
    finally:
        proc.close()


def test_sidecar_child_environment_contains_no_funded_process_secrets(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path)
    captured = {}
    monkeypatch.setenv("MEMECOIN_SOLANA_PRIVATE_KEY", "funded-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")

    def spawn_fn(*, binary_path, socket_path, env):
        captured.update(env)
        return server.start_env(env)

    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", ws_url="wss://custody/ws", spawn_fn=spawn_fn)
    try:
        assert proc.start() is True
        assert set(captured) == {
            "SNIPER_TPU_RPC_URL", "SNIPER_TPU_WS_URL", "SNIPER_TPU_SOCKET_PATH",
            "SNIPER_TPU_PARENT_PID"}
        assert captured["SNIPER_TPU_PARENT_PID"] == str(os.getpid())
        assert "funded-secret" not in repr(captured)
        assert "telegram-secret" not in repr(captured)
    finally:
        proc.close()


def test_process_status_and_logs_are_secret_free(tmp_path, capsys):
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path)
    proc = S.TpuSidecarProcess(binary_path="/opt/tpu-bin", socket_path=sock_path,
                               rpc_url="https://secret-custody/APIKEY",
                               spawn_fn=_spawn_fn_factory(server))
    try:
        proc.start()
        proc.health()
        status = proc.sanitized_status()
        blob = repr(status) + capsys.readouterr().out
        assert "secret-custody" not in blob and "APIKEY" not in blob and "https" not in blob
        assert status["provider"] == "solana:tpu"
        assert status["running"] is True
    finally:
        proc.close()


def test_process_restart_after_crash_increments_count(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)

    def spawn_fn(*, binary_path, socket_path, env):
        return next(it).start_env(env)
    proc = S.TpuSidecarProcess(binary_path="/opt/tpu-bin", socket_path=sock_path,
                               rpc_url="https://custody/rpc", spawn_fn=spawn_fn, start_timeout_s=3.0)
    try:
        assert proc.start() is True
        servers[0].terminate()                            # simulate a crash
        servers[0].wait(timeout=2.0)
        assert proc.restart() is True
        assert proc.sanitized_status()["restart_count"] == 1
        assert proc.health()["ready"] is True             # the restarted sidecar answers
    finally:
        proc.close()


def test_process_automatically_restarts_after_crash(tmp_path):
    import time

    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)

    def spawn_fn(*, binary_path, socket_path, env):
        return next(it).start_env(env)

    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=spawn_fn, start_timeout_s=2.0,
        monitor_interval_s=0.01, restart_backoff_s=(0.01, 0.02))
    try:
        assert proc.start() is True
        servers[0].terminate()
        servers[0].wait(timeout=1.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and proc.sanitized_status()["restart_count"] < 1:
            time.sleep(0.01)
        assert proc.sanitized_status()["restart_count"] == 1
        assert proc.health()["ready"] is True
    finally:
        proc.close()


def test_monitor_restarts_live_process_when_socket_health_fails(tmp_path):
    import time

    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)

    def spawn_fn(*, binary_path, socket_path, env):
        return next(it).start_env(env)

    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=spawn_fn, start_timeout_s=1.0,
        health_timeout_s=0.05, monitor_interval_s=0.01, restart_backoff_s=(0.01,))
    try:
        assert proc.start() is True
        servers[0].health_delay_s = 1.0                # PID stays alive; socket stops responding
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and proc.sanitized_status()["restart_count"] < 1:
            time.sleep(0.01)
        assert proc.sanitized_status()["restart_count"] == 1
        assert proc.health() == {"ready": True, "leader_count": 6}
    finally:
        proc.close()


def test_monitor_does_not_restart_live_process_after_two_transient_health_misses(tmp_path):
    import time

    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server),
        start_timeout_s=1.0, health_timeout_s=0.05, monitor_interval_s=0.01,
        restart_backoff_s=(0.01,),
    )
    try:
        assert proc.start() is True
        baseline = server.health_requests
        server.leader_count = 0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.health_requests < baseline + 2:
            time.sleep(0.005)
        assert server.health_requests >= baseline + 2
        server.leader_count = 6
        time.sleep(0.1)
        assert proc.sanitized_status()["restart_count"] == 0
        assert proc.health() == {"ready": True, "leader_count": 6}
    finally:
        proc.close()


def test_monitor_leaves_responsive_unready_child_to_its_own_repair(tmp_path):
    import time

    server = FakeSidecarServer(str(tmp_path / "tpu.sock"))
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=server.sock_path,
        rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server),
        start_timeout_s=0.2, health_timeout_s=0.02, poll_interval_s=0.005,
        monitor_interval_s=0.01, restart_backoff_s=(0.01,))
    try:
        assert proc.start()
        baseline = server.health_requests
        server.leader_count = 0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.health_requests < baseline + 8:
            time.sleep(0.005)
        assert server.health_requests >= baseline + 8
        assert proc.sanitized_status()["restart_count"] == 0
        assert proc.health()["ready"] is False
        assert server.poll() is None
        server.leader_count = 6
        assert proc.health()["ready"] is True
        assert proc.sanitized_status()["restart_count"] == 0
    finally:
        proc.close()


def test_cold_start_preserves_responsive_unready_child(tmp_path):
    server = FakeSidecarServer(str(tmp_path / "tpu.sock"), leader_count=0)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=server.sock_path,
        rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server),
        start_timeout_s=0.04, health_timeout_s=0.01, poll_interval_s=0.005,
        monitor_interval_s=1.0)
    try:
        assert proc.start() is False
        assert proc.sanitized_status()["running"] is True
        assert proc.health()["ready"] is False
        server.leader_count = 6
        assert proc.health()["ready"] is True
        assert proc.sanitized_status()["restart_count"] == 0
    finally:
        proc.close()


@pytest.mark.parametrize("fields", [
    {"request_id": "wrong", "ready": False, "leader_count": 0},
    {"request_id": "r", "ready": "false", "leader_count": 2},
    {"request_id": "r", "ready": False, "leader_count": None},
    {"request_id": "r", "ready": False, "leader_count": -1},
])
def test_malformed_health_is_not_a_responsive_child(fields):
    proc = S.TpuSidecarProcess(
        binary_path="/unused", socket_path="/unused.sock", rpc_url="https://unused",
        connect_fn=lambda *_: FakeConn(_resp_frame(**fields)))
    assert proc.health()["ready"] is False
    assert proc._health_responsive is False


def test_cold_start_failure_keeps_monitor_and_recovers(tmp_path):
    import time

    sock_path = str(tmp_path / "tpu.sock")
    healthy = FakeSidecarServer(sock_path)

    class DeadServer:
        def poll(self): return 1
        def terminate(self): pass
        def wait(self, timeout=None): return 1
        def kill(self): pass

    attempts = iter((DeadServer(), healthy))

    def spawn_fn(*, binary_path, socket_path, env):
        child = next(attempts)
        return child.start_env(env) if hasattr(child, "start_env") else child

    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=spawn_fn, start_timeout_s=0.05,
        health_timeout_s=0.01, poll_interval_s=0.005, monitor_interval_s=0.01,
        restart_backoff_s=(0.01,))
    try:
        assert proc.start() is False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not proc.sanitized_status().get("ready"):
            time.sleep(0.01)
        status = proc.sanitized_status()
        assert status["ready"] is True
        assert status["restart_count"] == 1
    finally:
        proc.close()


def test_start_removes_unhealthy_stale_socket_before_spawn(tmp_path):
    sock_path = tmp_path / "tpu.sock"
    sock_path.write_text("stale")
    server = FakeSidecarServer(str(sock_path))

    def spawn_fn(*, binary_path, socket_path, env):
        assert not os.path.exists(socket_path)
        return server.start_env(env)

    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=str(sock_path),
        rpc_url="https://custody/rpc", spawn_fn=spawn_fn, start_timeout_s=1.0)
    try:
        assert proc.start() is True
    finally:
        proc.close()


def test_zero_leader_health_is_not_ready(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path, leader_count=0)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path,
        rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server),
        start_timeout_s=0.2, health_timeout_s=0.05)
    try:
        assert proc.start() is False
        assert proc.health()["ready"] is False
    finally:
        proc.close()


def test_close_is_idempotent_and_bounded(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")
    server = FakeSidecarServer(sock_path)
    proc = S.TpuSidecarProcess(binary_path="/opt/tpu-bin", socket_path=sock_path,
                               rpc_url="https://custody/rpc", spawn_fn=_spawn_fn_factory(server))
    proc.start()
    proc.close()
    proc.close()                                          # second close must not raise
    assert proc.sanitized_status()["running"] is False


# --------------------------------------------------------------------------- resolve_tpu_sidecar
def _write_manifest(tmp_path, sidecar):
    import json as _json
    p = tmp_path / "memecoin_deploy.json"
    p.write_text(_json.dumps({"sha": "a" * 40, "sidecar": sidecar}))
    return str(p)


def test_resolve_returns_reason_without_manifest(tmp_path, caplog):
    missing = str(tmp_path / "nope.json")
    with caplog.at_level("WARNING"):
        result = S.resolve_tpu_sidecar(rpc_url="https://rpc", manifest_path=missing,
                                       socket_path=str(tmp_path / "s.sock"))
    assert result.process is None
    assert result.reason == "manifest_unreadable"
    assert "manifest_unreadable" in caplog.text


def test_resolve_returns_none_without_sidecar_object(tmp_path):
    import json as _json
    p = tmp_path / "m.json"
    p.write_text(_json.dumps({"sha": "a" * 40}))     # no sidecar object
    result = S.resolve_tpu_sidecar(rpc_url="https://rpc", manifest_path=str(p),
                                   socket_path=str(tmp_path / "s.sock"))
    assert result.process is None and result.reason == "sidecar_manifest_missing"


def test_resolve_returns_none_on_hash_mismatch(tmp_path):
    binary = tmp_path / "bin"
    binary.write_bytes(b"REAL-BINARY")
    binary.chmod(0o555)
    manifest = _write_manifest(tmp_path, {"path": str(binary), "sha256": "0" * 64})  # wrong hash

    def factory(**kw):
        raise AssertionError("must not launch a binary whose hash does not match the manifest")
    result = S.resolve_tpu_sidecar(rpc_url="https://rpc", manifest_path=manifest,
                                   socket_path=str(tmp_path / "s.sock"),
                                   process_factory=factory)
    assert result.process is None and result.reason == "binary_hash_mismatch"


def test_resolve_rejects_writable_or_symlink_binary_before_spawn(tmp_path):
    import hashlib as _h

    binary = tmp_path / "bin"
    binary.write_bytes(b"REAL-BINARY")
    binary.chmod(0o755)
    good = _h.sha256(b"REAL-BINARY").hexdigest()
    manifest = _write_manifest(tmp_path, {"path": str(binary), "sha256": good})
    spawned = []

    def factory(**kw):
        spawned.append(kw)
        raise AssertionError("mutable binary must not launch")

    result = S.resolve_tpu_sidecar(
        rpc_url="https://rpc", manifest_path=manifest, socket_path=str(tmp_path / "s.sock"),
        process_factory=factory)
    assert result.process is None and result.reason == "binary_unusable"
    binary.chmod(0o555)
    link = tmp_path / "bin-link"
    link.symlink_to(binary)
    manifest = _write_manifest(tmp_path, {"path": str(link), "sha256": good})
    result = S.resolve_tpu_sidecar(
        rpc_url="https://rpc", manifest_path=manifest, socket_path=str(tmp_path / "s.sock"),
        process_factory=factory)
    assert result.process is None and result.reason == "binary_unusable"
    assert spawned == []


def test_resolve_starts_exact_match_binary(tmp_path):
    import hashlib as _h
    binary = tmp_path / "bin"
    binary.write_bytes(b"REAL-BINARY")
    binary.chmod(0o555)
    good = _h.sha256(b"REAL-BINARY").hexdigest()
    manifest = _write_manifest(tmp_path, {"path": str(binary), "sha256": good})
    started = {}

    class FakeProc:
        def __init__(self, **kw):
            started["binary_path"] = kw["binary_path"]

        def start(self):
            return True

        def close(self):
            pass
    result = S.resolve_tpu_sidecar(rpc_url="https://rpc", manifest_path=manifest,
                                   socket_path=str(tmp_path / "s.sock"),
                                   process_factory=FakeProc)
    assert isinstance(result.process, FakeProc)
    assert result.reason == "ready"
    assert started["binary_path"] == str(binary)


def test_resolve_uses_candidate_manifest_and_verifier_socket_from_env(tmp_path):
    import hashlib as _h

    binary = tmp_path / "bin"
    binary.write_bytes(b"REAL-BINARY")
    binary.chmod(0o555)
    manifest = _write_manifest(
        tmp_path, {"path": str(binary), "sha256": _h.sha256(b"REAL-BINARY").hexdigest()})
    socket_path = str(tmp_path / "candidate-verify.sock")
    seen = {}

    class FakeProc:
        def __init__(self, **kw):
            seen.update(kw)

        def start(self):
            return True

        def close(self):
            pass

    result = S.resolve_tpu_sidecar(
        rpc_url="https://rpc",
        env={"SNIPER_MEMECOIN_DEPLOY_MANIFEST": manifest,
             "SNIPER_TPU_SOCKET_PATH_OVERRIDE": socket_path},
        process_factory=FakeProc)
    assert isinstance(result.process, FakeProc)
    assert seen["socket_path"] == socket_path


def test_resolve_uses_default_manifest_and_socket_under_state_root(tmp_path):
    import hashlib as _h
    import json as _json

    data = tmp_path / "data"
    data.mkdir()
    binary = tmp_path / "bin"
    binary.write_bytes(b"REAL-BINARY")
    binary.chmod(0o555)
    (data / "memecoin_deploy.json").write_text(_json.dumps({
        "sha": "a" * 40,
        "sidecar": {
            "path": str(binary),
            "sha256": _h.sha256(b"REAL-BINARY").hexdigest(),
        },
    }))
    seen = {}

    class FakeProc:
        def __init__(self, **kw):
            seen.update(kw)

        def start(self):
            return True

        def close(self):
            pass

    result = S.resolve_tpu_sidecar(
        rpc_url="https://rpc", env={"SNIPER_STATE_ROOT": str(tmp_path)},
        process_factory=FakeProc)
    assert isinstance(result.process, FakeProc)
    assert seen["socket_path"] == str(data / "memecoin-tpu-sidecar.sock")


def test_resolve_keeps_supervised_process_when_initial_start_fails(tmp_path):
    import hashlib as _h
    binary = tmp_path / "bin"
    binary.write_bytes(b"X")
    binary.chmod(0o555)
    manifest = _write_manifest(tmp_path, {"path": str(binary), "sha256": _h.sha256(b"X").hexdigest()})

    class FakeProc:
        def __init__(self, **kw):
            pass

        def start(self):
            return False

        def close(self):
            pass
    result = S.resolve_tpu_sidecar(rpc_url="https://rpc", manifest_path=manifest,
                                   socket_path=str(tmp_path / "s.sock"),
                                   process_factory=FakeProc)
    assert isinstance(result.process, FakeProc)
    assert result.reason == "initial_health_unavailable"


def test_start_returns_false_when_sidecar_never_becomes_healthy(tmp_path):
    sock_path = str(tmp_path / "tpu.sock")

    class DeadServer:
        def start_env(self, env):
            return self                                   # never binds a socket -> never healthy

        def is_alive(self):
            return False

        def poll(self):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 1
    proc = S.TpuSidecarProcess(binary_path="/opt/tpu-bin", socket_path=sock_path,
                               rpc_url="https://custody/rpc",
                               spawn_fn=lambda **kw: DeadServer().start_env(kw["env"]),
                               start_timeout_s=0.4, health_timeout_s=0.1)
    assert proc.start() is False
    proc.close()


# --- the restart must say so, on the path restarts actually take ---------------------------------
#
# 2026-09-23: the sidecar restarted 11 times in 57 minutes and NOTHING recorded when or why -- this
# module emitted no log line at all, so the only witness was a running total in the heartbeat.
# Deciding whether the restarts clustered at a DNS failure or ran all night needed a live sampler
# and an inference, and the hour that had already passed stayed unanswerable.
#
# These drive `_monitor_loop`, which is where restarts REALLY happen. `restart()` has no production
# caller -- instrumenting only that would have emitted nothing and read as "no restarts", which is
# worse than no line at all because it looks like evidence.

import logging as _logging

TPU_LOGGER = "sniper.execution.solana_tpu_sidecar"


def _restart_lines(caplog):
    return [r.getMessage() for r in caplog.records if "sidecar restart #" in r.getMessage()]


def test_a_crash_restart_is_logged_with_its_reason_and_count(tmp_path, caplog):
    """THE PROPERTY. The monitor's own restart path, not the unused public method."""
    import time

    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path, rpc_url="https://custody/rpc",
        spawn_fn=lambda *, binary_path, socket_path, env: next(it).start_env(env),
        start_timeout_s=2.0, monitor_interval_s=0.01, restart_backoff_s=(0.01, 0.02))
    try:
        with caplog.at_level(_logging.WARNING, logger=TPU_LOGGER):
            assert proc.start() is True
            servers[0].terminate()
            servers[0].wait(timeout=1.0)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and proc.sanitized_status()["restart_count"] < 1:
                time.sleep(0.01)
            lines = _restart_lines(caplog)
        assert len(lines) == 1, lines
        assert "restart #1" in lines[0] and "crashed" in lines[0], lines[0]
        assert proc.sanitized_status()["restart_count"] == 1, "the count and the line agree"
    finally:
        proc.close()


def test_an_unhealthy_restart_is_logged_as_unhealthy_not_crashed(tmp_path, caplog):
    """An alive child with an unresponsive socket needs parent repair, distinct from a crash."""
    import time

    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path, rpc_url="https://custody/rpc",
        spawn_fn=lambda *, binary_path, socket_path, env: next(it).start_env(env),
        start_timeout_s=1.0, health_timeout_s=0.05, monitor_interval_s=0.01,
        restart_backoff_s=(0.01,))
    try:
        with caplog.at_level(_logging.WARNING, logger=TPU_LOGGER):
            assert proc.start() is True
            servers[0].health_delay_s = 1.0            # PID alive, health reply times out
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and proc.sanitized_status()["restart_count"] < 1:
                time.sleep(0.01)
            lines = _restart_lines(caplog)
        assert lines and "unhealthy" in lines[0], lines
        assert "crashed" not in lines[0], lines[0]
    finally:
        proc.close()


def test_the_public_restart_method_also_logs(tmp_path, caplog):
    """It has no production caller today, but a future one must not go silent."""
    sock_path = str(tmp_path / "tpu.sock")
    servers = [FakeSidecarServer(sock_path), FakeSidecarServer(sock_path)]
    it = iter(servers)
    proc = S.TpuSidecarProcess(
        binary_path="/opt/tpu-bin", socket_path=sock_path, rpc_url="https://custody/rpc",
        spawn_fn=lambda *, binary_path, socket_path, env: next(it).start_env(env),
        start_timeout_s=3.0)
    try:
        with caplog.at_level(_logging.WARNING, logger=TPU_LOGGER):
            assert proc.start() is True
            servers[0].terminate()
            servers[0].wait(timeout=2.0)
            assert proc.restart() is True
            lines = _restart_lines(caplog)
        assert lines and "requested" in lines[0], lines
        assert "restart #1" in lines[0], lines[0]
    finally:
        proc.close()


@pytest.mark.parametrize('age,expected', [(0.1, 453640587), (2.0, None), (-0.1, None)])
def test_submit_uses_fresh_independent_slot_without_read_rpc(monkeypatch, age, expected):
    from sniper.services import memecoin_slot_signal
    monkeypatch.setattr(memecoin_slot_signal, 'HEAD', {'slot': 453640587, 'mono': 100 - age})
    monkeypatch.setattr(S._time, 'monotonic', lambda: 100)
    conn = FakeConn(_resp_frame(request_id='r', state='accepted', tx_identity='SIG_TPU'))
    prov = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn))
    assert prov.submit(_req()).state is AttemptState.ACCEPTED
    frame = json.loads(conn.sent[4:])
    assert frame.get('min_context_slot') == expected


def test_partial_slot_signal_without_head_is_unknown(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(S.sys.modules, "sniper.services.memecoin_slot_signal", SimpleNamespace())

    assert S._observed_head_slot() is None


@pytest.mark.parametrize("head", [None, [], "initializing", 42])
def test_non_dict_slot_signal_head_is_unknown(monkeypatch, head):
    from types import SimpleNamespace

    monkeypatch.setitem(
        S.sys.modules, "sniper.services.memecoin_slot_signal", SimpleNamespace(HEAD=head))

    assert S._observed_head_slot() is None


def test_monitor_survives_partial_slot_signal_and_later_observes_fresh_head(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    signal = SimpleNamespace(HEAD={})
    monkeypatch.setitem(S.sys.modules, "sniper.services.memecoin_slot_signal", signal)
    server = FakeSidecarServer(str(tmp_path / "tpu.sock"))
    proc = S.TpuSidecarProcess(
        binary_path="/unused", socket_path=server.sock_path, rpc_url="https://unused",
        spawn_fn=_spawn_fn_factory(server), start_timeout_s=1.0, monitor_interval_s=0.01)
    start_monitor = proc._start_monitor_unlocked

    def start_while_signal_is_initializing():
        del signal.HEAD
        start_monitor()

    monkeypatch.setattr(proc, "_start_monitor_unlocked", start_while_signal_is_initializing)
    try:
        assert proc.start() is True
        baseline = server.health_requests
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.health_requests < baseline + 2:
            time.sleep(0.01)
        assert server.health_requests >= baseline + 2
        assert proc._monitor_thread.is_alive()
        assert all(slot is None for slot in server.health_slots)

        signal.HEAD = {"slot": 453640587, "mono": time.monotonic()}
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and 453640587 not in server.health_slots:
            time.sleep(0.01)
        assert 453640587 in server.health_slots
        assert proc._monitor_thread.is_alive()
        assert proc.sanitized_status()["restart_count"] == 0
    finally:
        proc.close()


def test_health_sends_independent_slot_for_background_repair(monkeypatch):
    from sniper.services import memecoin_slot_signal
    monkeypatch.setattr(memecoin_slot_signal, 'HEAD', {'slot': 453640587, 'mono': 99.9})
    monkeypatch.setattr(S._time, 'monotonic', lambda: 100)
    conn = FakeConn(_resp_frame(request_id='r', ready=True, leader_count=2, leader_slot=453640587))
    proc = S.TpuSidecarProcess(binary_path='/unused', socket_path='/x.sock', rpc_url='https://unused',
                              connect_fn=_conn_factory(conn))
    assert proc.health()['ready']
    assert json.loads(conn.sent[4:])['min_context_slot'] == 453640587


@pytest.mark.parametrize("minimum", [None, 0, 2**64 - 1])
def test_stale_slot_keeps_state_and_carries_guard(minimum):
    guard = dict(version=1, estimated_slot=455637874, minimum_slot=455637877,
                 age_since_advance_ms=10.0, clock_frozen=False,
                 behind_minimum=True, available_leader_count=2)
    conn = FakeConn(_resp_frame(request_id='r', state='not_sent', tx_identity='SIG_TPU',
        reason_code='stale_slot', leader_count=0, guard=guard, request_min_context_slot=minimum))
    result = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn)).submit(_req())
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == 'stale_slot'
    assert result.provider_timing == {'tpu_guard': guard, 'request_min_context_slot': minimum}
    assert conn.closed


@pytest.mark.parametrize("identity", [None, "WRONG"])
@pytest.mark.parametrize("state", ["accepted", "not_sent", "ambiguous", "explicit_rejected"])
def test_guard_metadata_requires_transaction_identity(identity, state):
    conn = FakeConn(_resp_frame(request_id="r", state=state, tx_identity=identity,
                               guard={"version": 1}, request_min_context_slot=None))
    result = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn)).submit(_req())
    assert result.provider_timing is None
    if state == "accepted":
        assert result.state is AttemptState.AMBIGUOUS


@pytest.mark.parametrize("peer_guard", [None, [], "malformed"])
def test_old_or_malformed_guard_keeps_transport_state(peer_guard):
    conn = FakeConn(_resp_frame(request_id="r", state="not_sent", tx_identity="SIG_TPU",
                               reason_code="stale_slot", guard=peer_guard))
    result = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn)).submit(_req())
    assert result.state is AttemptState.NOT_SENT
    assert result.provider_timing is None


def test_guard_missing_request_minimum_stays_unknown():
    guard = dict(version=1, estimated_slot=455637874, minimum_slot=455637877,
                 age_since_advance_ms=10.0, clock_frozen=False,
                 behind_minimum=True, available_leader_count=2)
    conn = FakeConn(_resp_frame(request_id="r", state="not_sent", tx_identity="SIG_TPU",
                               reason_code="stale_slot", guard=guard))
    result = S.TpuSidecarProvider('/x.sock', connect_fn=_conn_factory(conn)).submit(_req())
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == "stale_slot"
    assert result.provider_timing is None


def _bounded_req(head, *, maximum=101, action="entry"):
    from dataclasses import replace
    return replace(_req(), action=action, entry_max_slot=maximum,
                   entry_head_fn=lambda: (head[0], head[1]))


@pytest.mark.parametrize("head", [(102, 10.0), (101, 9.4), (None, 10.0)])
def test_tpu_expired_or_unknown_entry_never_connects(head):
    def connect(*_):
        pytest.fail("refused entry must not reach a socket")
    result = S.TpuSidecarProvider('/x', connect_fn=connect, clock=lambda: 10.0).submit(
        _bounded_req(head))
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code in ('entry_slot_expired', 'entry_slot_head_stale', 'entry_slot_head_unknown')


def test_tpu_entry_rechecks_deadline_after_connect():
    head = [101, 10.0]
    conn = FakeConn(_resp_frame(request_id='r', state='accepted', tx_identity='SIG_TPU'))
    def connect(*_):
        head[0] = 102
        return conn
    result = S.TpuSidecarProvider('/x', connect_fn=connect, clock=lambda: 10.0).submit(
        _bounded_req(head))
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == 'entry_slot_expired'
    assert not conn.sent and conn.closed


def test_tpu_partial_entry_frame_becomes_ambiguous_when_deadline_expires():
    head = [101, 10.0]
    class AdvancingConn(FakeConn):
        def send(self, data):
            size = super().send(data[:5])
            head[0] = 102
            return size
    conn = AdvancingConn(_resp_frame(request_id='r', state='accepted', tx_identity='SIG_TPU'))
    result = S.TpuSidecarProvider('/x', connect_fn=_conn_factory(conn), clock=lambda: 10.0).submit(
        _bounded_req(head))
    assert result.state is AttemptState.AMBIGUOUS
    assert result.reason_code == 'entry_slot_expired'
    assert len(conn.sent) == 5 and conn.closed


@pytest.mark.parametrize("action,head,expected_cap", [('entry', 101, 101), ('exit', 102, None)])
def test_tpu_carries_original_entry_cap_without_changing_other_actions(action, head, expected_cap):
    conn = FakeConn(_resp_frame(request_id='r', state='accepted', tx_identity='SIG_TPU'))
    request = _bounded_req([head, 10.0], action=action)
    result = S.TpuSidecarProvider('/x', connect_fn=_conn_factory(conn), clock=lambda: 10.0).submit(request)
    assert result.state is AttemptState.ACCEPTED
    assert json.loads(conn.sent[4:]).get('entry_max_slot') == expected_cap


@pytest.mark.parametrize('maximum', [0, -1, True, '101', 2**64])
def test_tpu_invalid_slot_cap_is_not_sent(maximum):
    conn = FakeConn()
    result = S.TpuSidecarProvider('/x', connect_fn=_conn_factory(conn), clock=lambda: 10.0).submit(
        _bounded_req([101, 10.0], maximum=maximum))
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == 'entry_slot_deadline_invalid'
    assert not conn.sent
