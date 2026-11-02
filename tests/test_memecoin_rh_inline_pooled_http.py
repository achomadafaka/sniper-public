"""RH inline reads keep one owned HTTP pool and their existing admission/health fences."""
import json
import time

import httpx
import pytest

from sniper.execution import fast_http, submission_backends
from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
from sniper.services import memecoin_rh_fast_lane as lane

URL = "https://inline.example.test/key"
MINT = "0x" + "12" * 20


@pytest.fixture
def build(monkeypatch, tmp_path):
    from sniper.execution import evm_wallet
    from sniper.settings import memecoin as cfg

    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://research.example.test/key")
    monkeypatch.delenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(evm_wallet, "_http", lambda *a, **k: pytest.fail("legacy inline transport"))
    owned, registered, requests = [], [], []
    client_type = fast_http.PooledHttpClient

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        value = "0x12" if body["params"][0]["data"] == "0x313ce567" else "0xf4240"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": value})

    def factory(**kwargs):
        client = client_type(transport=httpx.MockTransport(handler), **kwargs)
        owned.append(client)
        return client

    monkeypatch.setattr(fast_http, "PooledHttpClient", factory)
    monkeypatch.setattr(submission_backends, "register_client", lambda client: registered.append(client) or client)
    contexts = {"4663": {"address": "0x" + "34" * 20,
                          "v4_direct_quote_fn": lambda *a, **k: None}}

    def make(url=URL):
        monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", url)
        built = lane.build_lane(contexts, mode="runner_shadow", log=lambda *_: None,
                                root=tmp_path, ro_connect=lambda: pytest.fail("no ledger reads"))
        return built, owned, registered, requests

    yield make
    for client in owned:
        client.close()


def test_production_build_reuses_one_owned_pool_for_inline_reads(build, monkeypatch):
    captured = {}
    original = lane.lane_inline_http

    def capture(url, paced, base=None, gate=None):
        captured["http"] = original(url, paced, base=base, gate=gate)
        return captured["http"]

    monkeypatch.setattr(lane, "lane_inline_http", capture)
    built, owned, registered, requests = build()
    assert len(owned) == 1 and registered == owned
    assert built["inline_url"] == URL and requests == []  # construction sends nothing
    for h in built["inline_paced"].hosts:
        h.admission = "admitted"                           # the background identity pass is not running here
    for _ in range(2):
        answer = captured["http"]("POST", URL, body=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": MINT, "data": "0x313ce567"}, "latest"]}))
        assert answer[1]["result"] == "0x12"
    assert len(owned) == 1 and len(requests) == 2
    assert built["inline_paced"].counts["calls"] == 2
    owned[0].close()
    with pytest.raises(RuntimeError, match="closed"):
        captured["http"]("POST", URL, body="{}")


@pytest.mark.parametrize("url", ["", "http://plain.example.test/key", "https://research.example.test/key/"])
def test_absent_or_refused_inline_host_never_constructs_a_client(build, url):
    built, owned, registered, requests = build(url)
    assert built["inline_url"] == "" and built["inline_paced"] is None
    assert owned == registered == requests == []


def test_pool_receives_original_request_and_bounded_deadline():
    requests = []
    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(
                200, json={"result": "0x1"}, headers={"date": "Wed, 14 Oct 2026 00:00:00 GMT"})))
    paced = lane.PacedHttp(min_gap_s=0.0)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    body = '{"jsonrpc":"2.0","id":1,"method":"eth_getCode","params":[]}'
    try:
        with lane.inline_deadline(time.monotonic() + 0.25):
            result = http("POST", URL, headers={"x-test": "same"}, body=body, timeout=4.0)
        assert result == (200, {"result": "0x1"}, "Wed, 14 Oct 2026 00:00:00 GMT")
        request, = requests
        assert request.method == "POST" and str(request.url) == URL
        assert request.headers["x-test"] == "same" and request.content == body.encode()
        assert all(0 < limit <= 0.25 for limit in request.extensions["timeout"].values())
        assert paced.counts["calls"] == 1
    finally:
        client.close()


def test_off_host_and_expired_reads_are_refused_before_pool_dispatch():
    requests = []
    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200, json={})))
    paced = lane.PacedHttp(min_gap_s=0.0)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    try:
        with pytest.raises(RuntimeError, match="off the lane"):
            http("POST", URL + "/", body="{}")
        with lane.inline_deadline(time.monotonic() - 1):
            with pytest.raises(EvmSafetyDeadlineExceeded):
                http("POST", URL, body="{}")
        assert requests == [] and paced.counts["calls"] == 0
    finally:
        client.close()


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_http_429_is_a_response_and_pauses_until_recovery():
    clock, requests = Clock(), []
    responses = [429, 200]
    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(
            responses.pop(0), json={"result": "0x1"})))
    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0, clock=clock)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    try:
        paced.transport(False)
        assert http("POST", URL, body="{}")[0] == 429
        assert paced.paused() and not paced.dead() and paced._run == 0
        with pytest.raises(lane.ResearchPaused):
            http("POST", URL, body="{}")
        clock.now += 121
        assert http("POST", URL, body="{}")[0] == 200
        assert len(requests) == 2
        assert paced.counts == {"calls": 2, "http_429": 1, "transport_errors": 1, "dead_marked": 0, "quota_403": 0}
    finally:
        client.close()


def test_pooled_transport_failures_mark_host_dead_then_recover(caplog):
    clock, requests = Clock(), []

    def handle(request):
        requests.append(request)
        if len(requests) <= 3:
            raise httpx.ConnectError("private endpoint details", request=request)
        return httpx.Response(200, json={"result": "0x1"})

    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(handle))
    paced = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    try:
        for _ in range(3):
            with pytest.raises(fast_http.FastHttpError):
                http("POST", URL, body="{}")
        assert paced.dead() and paced.counts["transport_errors"] == 3
        with pytest.raises(lane.InlineHostDead):
            http("POST", URL, body="{}")
        assert len(requests) == 3 and paced.counts["dead_marked"] == 1
        assert URL not in caplog.text and "private endpoint details" not in caplog.text
        clock.now += 601
        assert http("POST", URL, body="{}")[0] == 200 and paced._run == 0
        assert len(requests) == 4
    finally:
        client.close()


def test_pooled_timeout_cut_by_inline_deadline_never_marks_host_dead():
    requests = []

    def handle(request):
        requests.append(request)
        time.sleep(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("deadline elapsed", request=request)

    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(handle))
    paced = lane.PacedHttp(min_gap_s=0.0)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    try:
        for _ in range(3):
            with lane.inline_deadline(time.monotonic() + 0.005):
                with pytest.raises(EvmSafetyDeadlineExceeded):
                    http("POST", URL, body="{}")
        assert len(requests) == 3 and paced.counts["calls"] == 3
        assert paced.counts["transport_errors"] == 0 and not paced.dead()
    finally:
        client.close()


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="provider unavailable"),
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json={"error": {"code": -32603, "message": "rejected"}}),
    httpx.Response(200, json={}),
])
def test_provider_failure_or_malformed_reply_cannot_pass_safety(response):
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    from sniper.execution.evm_wallet import eth_get_code

    requests = []
    client = fast_http.PooledHttpClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or response))
    paced = lane.PacedHttp(min_gap_s=0.0)
    http = lane.lane_inline_http(URL, paced, base=client.request)
    try:
        result = token_safety_ok_evm(
            MINT, code_fn=lambda mint: eth_get_code(mint, URL, http=http),
            sell_route_fn=lambda mint: pytest.fail("no route authority after failed code read"),
            sell_sim_fn=lambda mint: pytest.fail("no simulation authority after failed code read"))
        assert result == (False, "code read failed (fail-closed)")
        assert len(requests) == 1 and paced.counts["transport_errors"] == 0
    finally:
        client.close()
