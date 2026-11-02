"""C4(b) on 56 (Alpha, 2026-10-01): read the just-delivered tx and receipt over the push provider's
WEBSOCKET, MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_<chain> (default empty = today, byte for byte).

Measured 2026-10-01 23:3xZ: bsc-rpc.publicnode.com over HTTP refuses EVERY eth_getTransactionReceipt
with 403 "Archive requests require a personal token", under curl's and a browser's User-Agent alike
(the HTTP knob was reverted for it, and it cost the sell shadow its reads). The same host over wss
served head-block receipts 3 of 3 in 18-48 ms, read as each newHeads arrived.

Rule-5 map (2026-10-01, before the edit):
1. Money path: upstream of the 56 on-chain lane's decision. It changes when a detection row lands,
   never what it holds; the read host stays the fallback for every miss.
2. Speed: watcher process only, never the funded thread. One kept connection; the tx and receipt
   reads take turns on it (a lock bounded by the read timeout). A dead socket costs one timeout
   (1.5 s), then the host cools and reads go straight to the read host, as today.
3. Concurrency: per-process object, lock-guarded; no module state. The sell shadow never uses it:
   sell_read_url reads only the HTTP knob, so sells keep today's host.
4. Absent/zero/unknown: null is "null" (the existing retry ladder), a JSON-RPC error is a miss,
   a timeout or transport failure CLOSES the connection, so a late reply is never matched to the
   next request, and cools the host.
5. Dependencies: publicnode is also the 56 push socket and a member of the funded 56 read pool, on
   the same IP. About 2 calls per non-duplicate transfer (~370 a day at 184 transfers); one more
   connection per 56 watcher process.
6. Units: none new.
7. Sample: 56 watched-wallet transfers; outcome lines name the host and its milliseconds.
8. Rollback: delete the knob line and restart the 56 watcher.
"""
import json

import pytest

import scripts.research.onchain_evm_wallet_watch as w

TX = "0x" + "11" * 32
RECEIPT = {"logs": [], "status": "0x1", "blockNumber": "0x10"}
TXBODY = {"from": "0x" + "ab" * 20}


@pytest.fixture(autouse=True)
def _chain():
    w._configure("56")
    yield
    w._configure("4663")


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Ws:
    """A scripted connection: each send queues the scripted frames for that request. A frame is a
    dict (sent back with the request's id unless it carries its own), an Exception (raised by
    recv), or "late" (a stale reply with an older id)."""

    def __init__(self, *frames):
        self.frames, self.sent, self.closed, self.pending = list(frames), [], False, []

    def send(self, text):
        body = json.loads(text)
        self.sent.append(body)
        frame = self.frames.pop(0) if self.frames else {"result": None}
        if frame == "late":
            self.pending.append(json.dumps({"jsonrpc": "2.0", "id": body["id"] - 1, "result": "stale"}))
            frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            self.pending.append(frame)
        else:
            self.pending.append(json.dumps(dict({"jsonrpc": "2.0", "id": body["id"]}, **frame)))

    def recv(self, timeout=None):
        item = self.pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True

    def ping(self):
        import threading
        self.pings = getattr(self, "pings", 0) + 1
        pong = threading.Event()
        if getattr(self, "answers_pings", True):
            pong.set()
        return pong


class _Connect:
    def __init__(self, *conns):
        self.conns, self.calls = list(conns), 0

    def __call__(self, url, timeout):
        self.calls += 1
        conn = self.conns.pop(0)
        if isinstance(conn, Exception):
            raise conn
        return conn


def _host(connect, clock=None, slept=None):
    return w.WsTxReadHost("wss://bsc-rpc.publicnode.com", cooldown_s=60, timeout_s=1.5,
                          read_cooldown_s=10, null_retry_s=0.1, connect_fn=connect,
                          clock=clock or _Clock(),
                          sleep_fn=(slept.append if slept is not None else lambda s: None))


def test_an_empty_knob_is_todays_path(monkeypatch):
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_56", raising=False)
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", raising=False)
    assert w.tx_read_host_from_env() is None


def test_the_ws_knob_builds_a_ws_host_and_wins_over_http(monkeypatch):
    monkeypatch.setattr(w.WsTxReadHost, "start_keepalive", lambda self: None)   # no network in tests
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_56", "wss://bsc-rpc.publicnode.com")
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", "https://bsc-rpc.publicnode.com")
    host = w.tx_read_host_from_env()
    assert isinstance(host, w.WsTxReadPair) and host.url == "wss://bsc-rpc.publicnode.com"
    tx, receipt = host.hosts["tx"], host.hosts["receipt"]
    assert isinstance(tx, w.WsTxReadHost) and isinstance(receipt, w.WsTxReadHost) and tx is not receipt
    assert tx.url == receipt.url == "wss://bsc-rpc.publicnode.com"


def test_the_sell_shadow_never_reads_over_the_ws_knob(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_56", "wss://bsc-rpc.publicnode.com")
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", raising=False)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_56", "https://watcher.example")
    monkeypatch.setattr(w, "evm_rpc_for_chain", lambda chain: "https://custody.example")
    assert w.sell_read_url() == "https://watcher.example"     # today's sell host, unchanged


def test_one_kept_connection_answers_tx_and_receipt():
    conn = _Ws({"result": dict(TXBODY)}, {"result": dict(RECEIPT)})
    connect = _Connect(conn)
    host = _host(connect)
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)
    assert host.read("eth_getTransactionReceipt", [TX]) == (dict(RECEIPT), None)
    assert connect.calls == 1
    assert [b["method"] for b in conn.sent] == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]
    assert conn.sent[0]["id"] != conn.sent[1]["id"]


def test_null_twice_and_rpc_error_are_misses_that_keep_the_connection():
    conn = _Ws({"result": None}, {"result": None}, {"error": {"code": -32602, "message": "nope"}},
               {"result": dict(TXBODY)})
    connect = _Connect(conn)
    slept = []
    host = _host(connect, slept=slept)
    assert host.read("eth_getTransactionReceipt", [TX]) == (None, "null")
    assert slept == [0.1] and len(conn.sent) == 2              # one retry, then the read host
    assert host.read("eth_getTransactionReceipt", [TX]) == (None, "rpc_error")
    assert len(conn.sent) == 3                                  # an error is not retried
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)
    assert connect.calls == 1 and not conn.closed


def test_a_null_head_receipt_is_asked_once_more_on_the_socket():
    """Gamma LOW: the pushing node usually has the receipt a moment later; the read host is
    null-first too."""
    slept = []
    host = _host(_Connect(_Ws({"result": None}, {"result": dict(RECEIPT)})), slept=slept)
    assert host.read("eth_getTransactionReceipt", [TX]) == (dict(RECEIPT), None)
    assert slept == [0.1]


def test_a_stale_reply_is_skipped_never_taken_as_the_answer():
    host = _host(_Connect(_Ws("late", {"result": dict(RECEIPT)})))
    assert host.read("eth_getTransactionReceipt", [TX]) == (dict(RECEIPT), None)


def test_a_timeout_closes_the_connection_and_cools_then_reconnects():
    clock = _Clock()
    first = _Ws(TimeoutError("slow"))
    second = _Ws({"result": dict(TXBODY)})
    connect = _Connect(first, second)
    host = _host(connect, clock)
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "transport")
    assert first.closed                       # a late reply can never answer the next request
    clock.t += 5
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "cooling")
    assert connect.calls == 1                 # not reconnected while cooling
    clock.t += 6                              # a failed READ cools 10 s, not 60 (Gamma LOW)
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)
    assert connect.calls == 2


def test_a_failed_connect_is_a_miss_and_cools():
    clock = _Clock()
    connect = _Connect(OSError("refused"))
    host = _host(connect, clock)
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "transport")
    clock.t += 30                             # a failed CONNECT cools the full 60 s
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "cooling")
    assert connect.calls == 1


def test_a_miss_falls_back_to_the_read_host_through_host_first():
    host = _host(_Connect(OSError("refused")))
    asked = []

    def base(method, params):
        asked.append(method)
        return dict(RECEIPT)
    outcome = {"hosts": {}, "host_ms": {}}
    read = w._host_first(base, host, outcome)
    assert read("eth_getTransactionReceipt", [TX]) == dict(RECEIPT)
    assert asked == ["eth_getTransactionReceipt"] and outcome["hosts"]["receipt"] == "read_host"
    assert outcome["tx_read_miss"] == {"receipt": "transport"}


def test_close_ends_the_connection_and_a_later_read_reconnects():
    """websockets' sync client keeps a NON-daemon receive thread when opened from the main thread:
    an open connection held the process at exit (measured 2026-10-01). run_push and run_poll close
    the host in their finally blocks."""
    first, second = _Ws({"result": dict(TXBODY)}), _Ws({"result": dict(TXBODY)})
    connect = _Connect(first, second)
    host = _host(connect)
    host.read("eth_getTransactionByHash", [TX])
    host.close()
    assert first.closed
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None) and connect.calls == 2


def test_run_push_closes_the_host_on_the_way_out(monkeypatch, tmp_path):
    from sniper.services import memecoin_onchain_push as P
    closed = []

    class _Host:
        def read(self, method, params):
            return None, "null"

        def close(self):
            closed.append(True)

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {})
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: _Host())
    monkeypatch.setattr(P, "run_push_stream", lambda ws_url, **kw: kw.get("state"))
    price = w.NativePrice(lambda: 600.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert closed == [True]


# --- KEEPALIVE (2026-10-02): publicnode closes an idle kept socket ---------------------------------
# Live 23:49-00:14Z on pid 583094: 2 of 4 transfers found the socket closed after 2 and 19 idle
# minutes (ConnectionClosedError) and fell back to the read host. websockets 12's sync client sends
# no keepalive pings. A daemon thread now pings every keepalive_s and reconnects on ITS thread, so a
# read finds a warm socket and never pays a connect. Rule-5: no money path (the 56 watcher's reads,
# upstream of the decision); reads never wait on the keepalive (a held lock skips the tick; the
# connect and the pong wait run outside the lock; a socket that loses a race with a read's own
# connect is closed); cooling is respected so a dead host is not hammered; close() stops it; ~4 pings
# a minute plus a reconnect when publicnode drops the socket; rollback: revert.

def test_keepalive_opens_the_socket_off_the_read_path():
    conn = _Ws({"result": dict(TXBODY)})
    connect = _Connect(conn)
    host = _host(connect)
    assert host.keepalive_once() == "connected" and connect.calls == 1
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)
    assert connect.calls == 1                         # the read found the warm socket


def test_keepalive_pings_a_live_socket_and_keeps_it():
    conn = _Ws()
    connect = _Connect(conn)
    host = _host(connect)
    host.keepalive_once()
    assert host.keepalive_once() == "ok" and conn.pings == 1 and not conn.closed and connect.calls == 1


def test_a_missing_pong_drops_the_socket_and_the_same_tick_reconnects():
    dead, fresh = _Ws(), _Ws({"result": dict(TXBODY)})
    dead.answers_pings = False
    connect = _Connect(dead, fresh)
    host = _host(connect)
    host.keepalive_once()
    assert host.keepalive_once() == "dropped+connected" and dead.closed and connect.calls == 2
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None) and connect.calls == 2


def test_keepalive_never_waits_for_a_read():
    host = _host(_Connect(_Ws()))
    with host._lock:
        assert host.keepalive_once() == "busy"


def test_keepalive_respects_cooling_and_stops_after_close():
    clock = _Clock()
    connect = _Connect(OSError("refused"), _Ws())
    host = _host(connect, clock)
    assert host.keepalive_once() == "failed"           # a failed connect cools the host
    assert host.keepalive_once() == "cooling" and connect.calls == 1
    clock.t += 61
    host.close()
    assert host.keepalive_once() == "closed" and connect.calls == 1


def test_the_factory_starts_the_keepalive(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_56", "wss://bsc-rpc.publicnode.com")
    started = []
    monkeypatch.setattr(w.WsTxReadHost, "start_keepalive", lambda self: started.append(self))
    host = w.tx_read_host_from_env()
    assert started == [host.hosts["tx"], host.hosts["receipt"]]


def test_the_keepalive_thread_is_a_daemon_and_close_ends_it():
    host = w.WsTxReadHost("wss://x", cooldown_s=60, timeout_s=1.5, connect_fn=_Connect(_Ws()), keepalive_s=0.01)
    thread = host.start_keepalive()
    assert thread.daemon and thread.name == "evm-tx-read-ws-keepalive"
    host.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_a_socket_the_server_closed_while_idle_is_warm_again_at_the_next_read():
    """The live failure: publicnode closed the idle socket, and the next read paid a dead send and the
    read host. Now the keepalive finds the close first (the ping raises), reopens on its own thread,
    and the read connects nothing."""
    class _ClosedByServer(_Ws):
        def ping(self):
            raise ConnectionError("received 1000 (OK); then sent 1000 (OK)")

    closed, fresh = _ClosedByServer(), _Ws({"result": dict(RECEIPT)})
    connect = _Connect(closed, fresh)
    host = _host(connect)
    assert host.keepalive_once() == "connected"
    assert host.keepalive_once() == "dropped+connected" and closed.closed
    calls_before_read = connect.calls
    assert host.read("eth_getTransactionReceipt", [TX]) == (dict(RECEIPT), None)
    assert connect.calls == calls_before_read and fresh.sent[-1]["method"] == "eth_getTransactionReceipt"


def test_a_keepalive_socket_that_loses_a_race_with_a_read_is_closed():
    mine, theirs = _Ws(), _Ws({"result": dict(TXBODY)})
    host = _host(_Connect(theirs))

    def connect_while_a_read_connects(url, timeout):
        host.read("eth_getTransactionByHash", [TX])      # the read opens `theirs` meanwhile
        return mine
    host._connect = lambda url, timeout: (setattr(host, "_connect", _Connect(theirs)),
                                          connect_while_a_read_connects(url, timeout))[1]
    assert host.keepalive_once() == "raced" and mine.closed and host._ws is theirs


def test_a_socket_is_replaced_before_publicnode_s_60_s_close():
    """Measured 2026-10-02: publicnode dropped the kept socket about 60 s after it opened, pinged or
    not. A socket at max_age_s (40) is replaced on the keepalive's thread, never by a read."""
    clock = _Clock()
    first, second = _Ws(), _Ws({"result": dict(TXBODY)})
    connect = _Connect(first, second)
    host = _host(connect, clock)
    assert host.keepalive_once() == "connected"
    clock.t += 39
    assert host.keepalive_once() == "ok" and connect.calls == 1
    clock.t += 1
    assert host.keepalive_once() == "rotated" and first.closed and connect.calls == 2
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None) and connect.calls == 2


def test_a_socket_a_read_opened_ages_out_too():
    clock = _Clock()
    first, second = _Ws({"result": dict(TXBODY)}), _Ws()
    host = _host(_Connect(first, second), clock)
    host.read("eth_getTransactionByHash", [TX])           # the read opened it at t=100
    clock.t += 40
    assert host.keepalive_once() == "rotated" and first.closed and host._ws is second


# --- the tx and the receipt on their own sockets (2026-10-14) -----------------------------------
# One socket served them in turn: over 401 reads on 56 (10-13/14) the receipt trailed the tx by
# p50 36 ms and the pair took p50 63 / p90 119 ms. The comment on WsTxReadHost named the fix.

class _Rendezvous:
    """Sockets that answer only once BOTH the tx and the receipt request have been sent. One socket
    serving one request at a time sends the second only after the first answer, so it times out."""

    def __init__(self):
        import threading
        self.sent, self.both, self.sockets = {}, threading.Event(), []

    def __call__(self, url, timeout):
        outer = self

        class Sock:
            def __init__(self):
                self.pending, self.closed = [], False

            def send(self, text):
                body = json.loads(text)
                outer.sent[body["method"]] = body
                self.pending.append(body)
                if len(outer.sent) == 2:
                    outer.both.set()

            def recv(self, timeout=None):
                if not outer.both.wait(timeout):
                    raise TimeoutError("answered only after both requests")
                body = self.pending.pop(0)
                result = dict(TXBODY) if body["method"] == "eth_getTransactionByHash" else dict(RECEIPT)
                return json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})

            def close(self):
                self.closed = True

        sock = Sock()
        self.sockets.append(sock)
        return sock


def test_the_tx_and_receipt_are_read_at_the_same_time_on_their_own_sockets(monkeypatch):
    monkeypatch.setattr(w.WsTxReadHost, "start_keepalive", lambda self: None)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_56", "wss://bsc-rpc.publicnode.com")
    server = _Rendezvous()
    monkeypatch.setattr(w, "_ws_connect", server)
    host = w.tx_read_host_from_env()
    outcome = {"hosts": {}, "host_ms": {}}
    read = w._host_first(lambda method, params: None, host, outcome)     # a miss reads nothing
    assert w._read_pair(read, TX) == (dict(TXBODY), dict(RECEIPT))
    assert outcome["hosts"] == {"tx": "tx_read", "receipt": "tx_read"}
    assert "tx_read_miss" not in outcome and len(server.sockets) == 2


def test_a_failed_receipt_socket_cools_only_the_receipt_reads():
    tx_conn = _Connect(_Ws({"result": dict(TXBODY)}), _Ws({"result": dict(TXBODY)}))
    pair = w.WsTxReadPair(_host(tx_conn), _host(_Connect(OSError("refused"))))
    assert pair.read("eth_getTransactionReceipt", [TX]) == (None, "transport")
    assert pair.read("eth_getTransactionReceipt", [TX]) == (None, "cooling")
    assert pair.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)


def test_close_ends_both_sockets():
    first, second = _Ws({"result": dict(TXBODY)}), _Ws({"result": dict(RECEIPT)})
    pair = w.WsTxReadPair(_host(_Connect(first)), _host(_Connect(second)))
    pair.read("eth_getTransactionByHash", [TX])
    pair.read("eth_getTransactionReceipt", [TX])
    pair.close()
    assert first.closed and second.closed
