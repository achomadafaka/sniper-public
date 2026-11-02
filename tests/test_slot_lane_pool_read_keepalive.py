"""PumpSwap pool-miss learns read on a kept-alive custody connection (2026-10-07, operator order
via Alpha: "rejected pool_learn_budget").

A pool miss is learned by two dependent reads (the Pool account, then its mints) inside
MISS_LEARN_BUDGET_S (250 ms). Each read went through urllib, a fresh TCP + TLS handshake per call:
69 of 73 misses since 2026-09-29 learned in time (read p50 89 ms, p90 125, max 219) and two did not
(ResellCalendar 18:17Z and OmakaseOnly CBUCKS 19:59Z on 2026-10-06, learn_ms 250.5 and 250.6; the
reads' own times were never logged). The PumpSwap pool cache now reads through one keep-alive
client, the PumpSwap learner keeps that connection open while it idles, and a read that lands after
every waiter's budget logs its time.

A local HTTP/1.1 node stands in for the custody RPC and counts the TCP connections it accepts.
"""
import http.server
from contextlib import contextmanager, nullcontext
import json
import threading
import time

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg
from test_slot_lane_handoff import FX, _Clock, _PoolRpc, _buy

USER = "11111111111111111111111111111111"


class _StopLearner(Exception):
    pass


@contextmanager
def _running_learner(learner):
    sentinel, workers = object(), []
    original_learn = learner.learn_once
    original_warm = getattr(learner.pools, "keep_warm", None)
    patch = pytest.MonkeyPatch()

    def learn(pool):
        if pool is sentinel:
            raise _StopLearner
        return original_learn(pool)

    def keep_warm():
        workers.append(threading.current_thread())
        return original_warm()

    def run():
        try:
            learner.run()
        except _StopLearner:
            pass

    patch.setattr(learner, "learn_once", learn)
    if original_warm is not None:
        patch.setattr(learner.pools, "keep_warm", keep_warm)
    worker = threading.Thread(target=run, name="test-miss-learner", daemon=True)
    workers.append(worker)
    worker.start()
    try:
        yield workers
    finally:
        learner._jobs.put(sentinel)
        deadline = time.monotonic() + 3.0
        worker.join(max(0.0, deadline - time.monotonic()))
        # A started ping may not have reached the instance hook yet.
        for thread in threading.enumerate():
            if getattr(thread, "_target", None) is keep_warm and thread not in workers:
                workers.append(thread)
        for thread in workers[1:]:
            thread.join(max(0.0, deadline - time.monotonic()))
        assert not any(thread.is_alive() for thread in workers), "learner teardown leaked a thread"
        patch.undo()


@pytest.mark.parametrize("body_error", [False, True])
@pytest.mark.parametrize("has_ping", [False, True])
def test_learner_context_joins_its_worker_and_active_ping(body_error, has_ping):
    ping_started, release_ping = threading.Event(), threading.Event()

    class Pools:
        pass

    pools = Pools()
    if has_ping:
        def keep_warm():
            ping_started.set()
            release_ping.wait(2.0)
        pools.keep_warm = keep_warm

    learner = lane.MissLearner(pools=pools, put=lambda b: None,
                               handoff_fn=lambda b, r: None, log=lambda line: None,
                               keep_warm_s=0.01)
    original_run = learner.run
    original_learn = learner.learn_once
    original_warm = getattr(pools, "keep_warm", None)

    def run():
        try:
            original_run()
        finally:
            release_ping.set()

    learner.run = run
    with pytest.raises(RuntimeError, match="body failed") if body_error else nullcontext():
        with _running_learner(learner) as workers:
            if has_ping:
                assert ping_started.wait(1.0)
                assert len(workers) == 2 and workers[1].is_alive()
            else:
                assert len(workers) == 1 and workers[0].is_alive()
            if body_error:
                raise RuntimeError("body failed")
    assert not any(worker.is_alive() for worker in workers)
    assert learner.learn_once == original_learn
    assert getattr(pools, "keep_warm", None) == original_warm


class _Node(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.connections += 1

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.methods.append(req["method"])
        status, body = self.server.answer(req)
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def _fixture_answer(req):
    if req["method"] == "getHealth":
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "ok"}
    return 200, {"jsonrpc": "2.0", "id": 1, "result": _PoolRpc()(req["method"], req["params"])}


@pytest.fixture
def node(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Node)
    srv.daemon_threads = True
    srv.connections, srv.methods, srv.answer = 0, [], _fixture_answer
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "http://127.0.0.1:%d/" % srv.server_address[1])
    yield srv
    srv.shutdown()
    srv.server_close()


def test_the_default_pumpswap_pool_cache_learns_on_one_kept_alive_connection(node):
    pools = ps.SlotLanePumpSwapBuilder(user=USER).pools
    pool = _buy(FX["tekkerrss"], _Clock()).pool
    assert pools.learn(pool) is True
    pools._pools.clear()
    assert pools.learn(pool) is True
    assert node.methods == ["getAccountInfo", "getMultipleAccounts"] * 2
    assert node.connections == 1, "each pool read opened its own connection"


@pytest.mark.parametrize("answer, expect", [
    ((429, {"jsonrpc": "2.0", "id": 1, "error": {"code": 429}}), RuntimeError),
    ((200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}), RuntimeError),
    ((200, {"jsonrpc": "2.0", "id": 1, "result": None}), None),
])
def test_a_refused_or_failed_read_raises_and_a_null_account_stays_null(node, answer, expect):
    node.answer = lambda req: answer
    rpc = ps.SlotLanePumpSwapBuilder(user=USER).pools.rpc
    if expect is None:
        assert rpc("getAccountInfo", ["x", {}]) is None
    else:
        with pytest.raises(expect):
            rpc("getAccountInfo", ["x", {}])


def test_the_pumpswap_learner_keeps_its_connection_open_while_idle_and_survives_a_failed_ping(node):
    pools = ps.SlotLanePumpSwapBuilder(user=USER).pools
    learner = lane.MissLearner(pools=pools, put=lambda b: None, handoff_fn=lambda b, r: None,
                               log=lambda line: None, keep_warm_s=0.05)
    node.answer = lambda req: (500, {}) if req["method"] == "getHealth" else _fixture_answer(req)
    with _running_learner(learner):
        deadline = time.monotonic() + 2.0
        while node.methods.count("getHealth") < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert node.methods.count("getHealth") >= 2, "a failed ping stopped the keep-warm"
        node.answer = _fixture_answer
        while node.methods.count("getHealth") < 4 and time.monotonic() < deadline + 2.0:
            time.sleep(0.01)
        assert node.methods.count("getHealth") >= 4
        learner.learn_once(_buy(FX["tekkerrss"], _Clock()).pool)
        assert [m for m in node.methods if m != "getHealth"] == ["getAccountInfo", "getMultipleAccounts"]


def test_an_injected_reader_without_a_ping_is_never_pinged():
    calls = []

    class Rpc(_PoolRpc):
        def __call__(self, method, params):
            calls.append(method)
            return super().__call__(method, params)
    learner = lane.MissLearner(pools=ps.PumpSwapPoolCache(rpc=Rpc()), put=lambda b: None,
                               handoff_fn=lambda b, r: None, log=lambda line: None,
                               keep_warm_s=0.01)
    with _running_learner(learner) as workers:
        time.sleep(0.1)
        assert calls == []


def test_a_hung_ping_never_delays_a_learn(node):
    """Alpha's check on a2c112410: the ping may stall on the node; the miss still reads at once."""
    pools = ps.SlotLanePumpSwapBuilder(user=USER).pools
    learner = lane.MissLearner(pools=pools, put=lambda b: None, handoff_fn=lambda b, r: None,
                               log=lambda line: None, keep_warm_s=0.02)

    release_ping = threading.Event()

    def answer(req):
        if req["method"] == "getHealth":
            release_ping.wait(2.0)
        return _fixture_answer(req)
    node.answer = answer
    with _running_learner(learner):
        try:
            deadline = time.monotonic() + 2.0
            while "getHealth" not in node.methods and time.monotonic() < deadline:
                time.sleep(0.005)
            assert "getHealth" in node.methods
            started = time.monotonic()
            learner._jobs.put(_buy(FX["tekkerrss"], _Clock()).pool)
            while "getMultipleAccounts" not in node.methods and time.monotonic() < started + 2.0:
                time.sleep(0.005)
            assert time.monotonic() - started < 0.25, "a hung ping held the learn"
        finally:
            release_ping.set()
