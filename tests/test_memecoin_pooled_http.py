"""Task 1 — persistent pooled HTTP transport tests.

Proves the reusable httpx-backed client: one long-lived client per instance, no automatic
POST retry, redacted transport errors (never a URL/api-key), bounded/idempotent shutdown, and
connection warming that marks providers healthy/unhealthy and stops promptly. Offline via
httpx.MockTransport and a loopback HTTP server — no live endpoint.
"""
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from sniper.execution import fast_http


def _mock_client(handler, **kw):
    return fast_http.PooledHttpClient(transport=httpx.MockTransport(handler), **kw)


def test_request_returns_status_json_date():
    def handler(request):
        return httpx.Response(200, json={"ok": True}, headers={"date": "Wed, 01"})
    c = _mock_client(handler)
    try:
        status, body, date = c.request("POST", "https://x/rpc", body=json.dumps({"a": 1}))
        assert status == 200
        assert body == {"ok": True}
        assert date == "Wed, 01"
    finally:
        c.close()


def test_request_with_headers_returns_all_response_headers():
    def handler(request):
        return httpx.Response(
            200, json={"ok": True},
            headers={"date": "Wed, 01", "ratelimit-remaining": "11999"},
        )

    c = _mock_client(handler)
    try:
        status, body, headers = c.request_with_headers("GET", "https://x/quote")
        assert status == 200
        assert body == {"ok": True}
        assert headers["ratelimit-remaining"] == "11999"
    finally:
        c.close()


def test_json_body_defaults_to_application_json_content_type():
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"result": "0x1237"})

    c = _mock_client(handler)
    try:
        c.request("POST", "https://x/rpc", body=json.dumps({"method": "eth_chainId"}))
        assert seen["content_type"] == "application/json"
    finally:
        c.close()


def test_client_reused_across_requests():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})
    c = _mock_client(handler)
    try:
        underlying = c._client
        for _ in range(3):
            c.request("GET", "https://x/y")
        assert c._client is underlying          # one long-lived client, reused
        assert calls["n"] == 3
    finally:
        c.close()


def test_default_pool_reuses_a_quiet_connection_beyond_the_five_second_warm_cycle():
    """Exercise the real HTTP pool: a fresh client object alone does not prove socket reuse."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import time

    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            peers.append(self.client_address)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = fast_http.PooledHttpClient()
    try:
        url = "http://127.0.0.1:%d/" % server.server_address[1]
        assert client.request("GET", url)[:2] == (200, {"ok": True})
        time.sleep(5.1)
        assert client.request("GET", url)[:2] == (200, {"ok": True})
        assert peers[0] == peers[1]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1.0)


def test_managed_rpc_pool_expiry_also_covers_read_rotation():
    ordinary = fast_http.warm_keepalive_expiry(5, 1.5, serial_calls=3)
    pool = fast_http.warm_keepalive_expiry(5, 1.5, serial_calls=7)
    assert pool > ordinary and pool >= 2 * 5 + 7 * 4 * 3


def test_post_not_retried_on_server_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "x"})
    c = _mock_client(handler)
    try:
        status, _body, _d = c.request("POST", "https://x", body="{}")
        assert status == 500
        assert calls["n"] == 1                   # NO automatic retry of a POST
    finally:
        c.close()


def test_transport_error_is_redacted_and_not_retried():
    secret = "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom to mainnet.helius-rpc.com api-key=SUPERSECRET",
                                 request=request)
    c = _mock_client(handler)
    try:
        with pytest.raises(fast_http.FastHttpError) as ei:
            c.request("POST", secret, body="{}")
        msg = str(ei.value)
        assert "SUPERSECRET" not in msg
        assert "helius-rpc.com" not in msg
        assert "api-key" not in msg
        assert calls["n"] == 1                   # transport failure is not retried
    finally:
        c.close()


def test_close_is_idempotent():
    c = _mock_client(lambda r: httpx.Response(200, json={}))
    c.close()
    c.close()                                    # second close must not raise


def test_default_client_has_bounded_timeout_and_no_module_singleton():
    assert not any(isinstance(v, httpx.Client) for v in vars(fast_http).values())
    c = fast_http.PooledHttpClient(read_timeout_s=0.5)
    try:
        assert c._client.timeout.read == 0.5     # bounded read timeout applied
    finally:
        c.close()


# --- connection warming --------------------------------------------------------------------------

def test_warm_once_marks_healthy_and_unhealthy():
    def good():
        return None

    def bad():
        raise RuntimeError("down")
    health = fast_http.warm_once([("sender", good), ("jito", bad)])
    assert health["sender"] is True
    assert health["jito"] is False


def test_warm_loop_bounded_by_stop_event():
    ticks = {"n": 0}

    def warm():
        ticks["n"] += 1
    stop = threading.Event()
    slept = {"n": 0}

    def fake_sleep(_s):
        slept["n"] += 1
        if slept["n"] >= 3:
            stop.set()
    fast_http.run_warm_loop([("p", warm)], interval_s=5.0, stop_event=stop, sleep=fake_sleep)
    assert ticks["n"] == 3
    assert stop.is_set()


def test_start_warming_thread_is_daemon_and_stops_quickly():
    stop = threading.Event()
    t = fast_http.start_warming([("p", lambda: None)], interval_s=0.01, stop_event=stop)
    try:
        assert t.daemon is True
    finally:
        stop.set()
        t.join(timeout=2.0)
    assert not t.is_alive()                       # warming never blocks shutdown


def test_slow_provider_cannot_delay_healthy_provider_warming():
    stop = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    healthy_repeated = threading.Event()
    workers = []
    calls = []
    health = {}

    def slow():
        workers.append(threading.current_thread())
        entered.set()
        release.wait(2.0)

    def healthy():
        calls.append("healthy")
        if len(calls) == 3:
            healthy_repeated.set()

    thread = fast_http.start_warming(
        [("slow", slow), ("healthy", healthy)],
        interval_s=0.02, stop_event=stop, health=health,
    )
    try:
        assert entered.wait(1.0)
        assert healthy_repeated.wait(1.0)
        assert health.get("healthy") is True
        assert "slow" not in health              # an in-flight probe is still unknown
        assert len(workers) == 1                 # no queued or overlapping slow probes
    finally:
        stop.set()
        release.set()
        thread.join(timeout=1.0)
        for worker in workers:
            worker.join(timeout=1.0)


def test_warming_stop_is_prompt_and_discards_late_health():
    stop = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    workers = []
    health = {}

    def slow():
        workers.append(threading.current_thread())
        entered.set()
        release.wait(2.0)

    thread = fast_http.start_warming(
        [("slow", slow)], interval_s=0.02, stop_event=stop, health=health,
    )
    try:
        assert entered.wait(1.0)
        stop.set()
        thread.join(timeout=0.2)
        assert not thread.is_alive()
    finally:
        stop.set()
        release.set()
        thread.join(timeout=1.0)
        for worker in workers:
            worker.join(timeout=1.0)
    assert health == {}


@pytest.mark.parametrize(("probe_s", "wait_s"), [(3.0, 2.0), (8.0, 0.0)])
def test_warming_cadence_counts_probe_time_without_catchup_bursts(monkeypatch, probe_s, wait_s):
    stop = threading.Event()
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(fast_http, "_time", SimpleNamespace(monotonic=lambda: now[0]))

    def warm():
        now[0] += probe_s

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 3:
            stop.set()

    fast_http.run_warm_loop([("p", warm)], interval_s=5.0, stop_event=stop, sleep=sleep)

    assert sleeps == [wait_s, wait_s, wait_s]


def test_warming_freezes_provider_list_before_starting():
    stop = threading.Event()
    calls = []
    providers = [("p", lambda: calls.append("p"))]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            providers.append(("late", lambda: calls.append("late")))
        if len(sleeps) == 3:
            stop.set()

    fast_http.run_warm_loop(providers, interval_s=5.0, stop_event=stop, sleep=sleep)

    assert calls == ["p", "p", "p"]


@pytest.mark.parametrize("interval_s", [0, -1, float("nan"), float("inf"), threading.TIMEOUT_MAX * 2])
def test_warming_rejects_invalid_cadence_before_spawning(interval_s):
    stop = threading.Event()
    thread = None
    try:
        with pytest.raises(ValueError, match="interval"):
            thread = fast_http.start_warming([], interval_s=interval_s, stop_event=stop)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)


def test_warming_rejects_noncallable_probe_before_spawning():
    stop = threading.Event()
    thread = None
    try:
        with pytest.raises(ValueError, match="callable"):
            thread = fast_http.start_warming([("p", None)], stop_event=stop)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
