"""Funded workers share warm sockets without sharing the warmer's request accounting."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.execution import pump_curve_buy as P, raydium_launchlab_buy as LL
from sniper.execution import solana_real_seams as seams
from test_slot_lane_exotic_round_trip import _warm
from test_slot_lane_pump_nonsol import SNAP, SPCX, _slot_buy


@pytest.fixture
def quote_server(monkeypatch):
    requests, reverse_status = [], [200]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            reverse = query["inputMint"][0] != P.USDC_MINT
            status = reverse_status[0] if reverse else 200
            self.answer(status, dict(inputMint=query["inputMint"][0],
                outputMint=query["outputMint"][0], inAmount=query["amount"][0],
                outAmount="10000000" if reverse else "40000",
                otherAmountThreshold="9900000" if reverse else "39400",
                swapMode="ExactIn", slippageBps=P.DEFAULT_LEG_SLIPPAGE_BPS,
                routePlan=[{"swapInfo": {"ammKey": SPCX["pool"], "label": "fixture"}}]))

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.answer(200, {"swapInstruction": {"programId": P.SYSTEM_PROGRAM, "data": "",
                "accounts": [{"pubkey": self.server.user, "isSigner": True, "isWritable": True}]}})

        def answer(self, status, payload):
            requests.append((self.command, self.client_address, self.headers.get("x-api-key")))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    monkeypatch.setattr(seams, "_jup_hosts", lambda *a, **k: (base,))
    monkeypatch.setattr(seams, "JUP_KEYED_BASES", (base,))
    monkeypatch.setattr(seams, "_jup_headers", lambda env=None: {"x-api-key": "fixture-key"})
    monkeypatch.setattr(seams, "_note_auth_alert", lambda *a, **k: None)
    monkeypatch.setattr(seams, "_note_host_auth_failure", lambda *a, **k: None)
    monkeypatch.setattr(seams, "funded_429_age_s", lambda: None)
    try:
        yield server, requests, reverse_status
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.parametrize("with_warmer", [True, False])
@pytest.mark.parametrize("status", [200, 401, 429])
def test_live_worker_reuses_quote_connection_without_spending_warm_budget(
        monkeypatch, quote_server, with_warmer, status):
    server, requests, reverse_status = quote_server
    warm, builder, clock, _, facts = _warm(monkeypatch)
    server.user = builder.user
    warm._http = None
    warm._entries.clear()
    monkeypatch.setattr(LL, "warm_legs_on", lambda: True)
    monkeypatch.setattr(LL, "exotic_round_trip_on", lambda: True)
    builder.read_rpc = lambda *a: pytest.fail("fresh quote facts must avoid a new chain read")
    if with_warmer:
        warm.seed("pump_amm", SPCX["quote_mint"], 10_000_000, builder=builder, facts=facts)
        warm.refresh_once()
        assert [r[0] for r in requests] == ["GET", "POST"]
        # A live worker is independent of the speculative warmer's exhausted start budget.
        warm._http_starts.extend([clock.t, clock.t])
        accounting = (warm.stats["http_calls"], list(warm._http_starts),
                      list(warm._actual_statuses), copy.deepcopy(warm._probe))
    requeued = []
    worker = LL.OnDemandLegWorker(builders={"pump_amm": builder},
        admission_fn=lambda *a: None, snapshot_fn=lambda: SNAP, clip_fn=lambda: 10.,
        offer_fn=lambda *a: None, live_fn=lambda venue: True, requeue_fn=requeued.append,
        warm=warm if with_warmer else None, clock=SimpleNamespace(monotonic=clock.monotonic), start=False)
    worker._quote_seen[SPCX["quote_mint"]] = (clock.t, (6, 0, 0))
    reverse_status[0] = status
    buy = _slot_buy(SPCX, recv_monotonic=clock.t)
    try:
        result = worker.process(buy)
        assert [r[0] for r in requests] == ["GET", "POST", "GET"]
        assert {r[2] for r in requests} == {"fixture-key"}
        assert len({r[1] for r in requests}) == 1, "warm and live requests opened different TCP connections"
        if with_warmer:
            assert accounting == (warm.stats["http_calls"], list(warm._http_starts),
                                  list(warm._actual_statuses), warm._probe)
        if status == 200:
            assert result["status"] == "requeued"
            resumed = requeued[-1]
            assert resumed.signature == buy.signature and resumed.recv_monotonic == buy.recv_monotonic
            leg = resumed.ondemand["leg"]
            assert leg["reverse"]["fetched_at"] == leg["fetched_at"]
            assert leg["reverse"]["min_usdc_atomic"] == 9_900_000
            if with_warmer:
                worker.process(_slot_buy(SPCX, signature="complete-warm", recv_monotonic=clock.t))
                assert len(requests) == 3
        else:
            assert result["reason"] == builder.REASON_PREFIX + "_leg_rate_limited"
            assert not requeued
            assert worker.process(buy)["reason"] == builder.REASON_PREFIX + "_leg_backoff"
            assert len(requests) == 3
    finally:
        if warm._http is not None:
            warm._http.close()
        client = getattr(worker, "_http", None)
        if client is not None:
            client.close()


def test_shared_client_initialization_is_single_and_releases_lock_before_io(monkeypatch):
    from sniper.execution import fast_http
    constructed, errors, replies = [], [], []
    started, release = threading.Event(), threading.Event()
    callers, requests = threading.Barrier(4), threading.Barrier(3)

    class Client:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            started.set()
            assert release.wait(2)

        def request(self, *args, **kwargs):
            requests.wait(2)
            return 200, {"ok": True}, None

    monkeypatch.setattr(fast_http, "PooledHttpClient", Client)
    warm = LL.WarmExoticLegs()
    assert callable(getattr(warm, "request_http", None)), "the warmer needs an accounting-free shared transport"

    def call():
        try:
            callers.wait(2)
            replies.append(warm.request_http("GET", "fixture"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        callers.wait(2)
        assert started.wait(2)
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert not errors and len(replies) == 3
    assert constructed == [{"max_connections": 4, "max_keepalive": 4}]
    assert warm.stats["http_calls"] == 0 and list(warm._http_starts) == []
    assert warm._actual_statuses == [] and warm._probe is None
