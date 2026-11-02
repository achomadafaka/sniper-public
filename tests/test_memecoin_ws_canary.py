"""A low-rate canary on the 56 push socket (2026-10-17, Alpha/Gamma): one busy PancakeSwap V2 pool's Sync.

At ~1.7 armed events an hour Gamma cannot tell a silent logs subscription from quiet wallets. A second
logs filter on the SAME socket, the pool's Sync, is counted only (never decoded, never handed to the
lane); with heads flowing and the canary silent past its window the socket is judged silent and the
lane moves to the next host (or reconnects on a single host). The armed filter is unchanged."""
import json

from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

A = "wss://bsc-rpc.publicnode.com"
B = "wss://bnb-mainnet.g.alchemy.com/v2/secret-key"
POOL = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"          # PancakeSwap V2 USDT/WBNB
SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
CANARY = {"address": POOL, "topics": [SYNC]}


class Socket:
    def __init__(self, messages):
        self.messages, self.sent = iter(messages), []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        pass

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        raw = next(self.messages, None)
        if raw is None:
            raise TimeoutError
        return raw


def _stream(clock, *, canary, heads=30, gap=1.0, canary_every=None):
    """Acks for heads(1), transfers(2), canary(3); then `heads` heads `gap` s apart, with a canary log
    every `canary_every` heads."""
    yield json.dumps({"id": 1, "result": "h"})
    yield json.dumps({"id": 2, "result": "t"})
    if canary:
        yield json.dumps({"id": 3, "result": "c"})
    for i in range(heads):
        clock[0] += gap
        yield json.dumps({"method": "eth_subscription", "params": {"subscription": "h", "result": {
            "number": hex(0x100 + i), "timestamp": "0x01"}}})
        if canary_every and i % canary_every == 0:
            yield json.dumps({"method": "eth_subscription", "params": {"subscription": "c", "result": {
                "address": POOL, "topics": [SYNC], "transactionHash": "0x" + "11" * 32}}})


def _run(connect, clock, state, *, stop, urls=(A, B)):
    transfers = []
    P.run_push_stream(
        A, ws_urls=urls, on_transfer=transfers.append, on_initialize=lambda _l: None,
        addresses_fn=lambda: ("0x" + "ab" * 20,), pool_manager=None, init_topic=None, state=state,
        connect_fn=connect, sleep_fn=lambda s: clock.__setitem__(0, clock[0] + s),
        now_fn=lambda: clock[0], should_stop=stop, failover_after=2, failover_window_s=600.0,
        canary_filter=CANARY, canary_silence_s=10.0)
    return transfers


def test_heads_flowing_with_the_canary_silent_past_the_window_rotates_to_the_next_host():
    opened, clock, state, sockets = [], [1.0], P.PushState(), []

    def connect(url, **_k):
        opened.append(url)
        sockets.append(Socket(_stream(clock, canary=True, canary_every=None if url == A else 2)))
        return sockets[-1]
    _run(connect, clock, state, stop=lambda: len(opened) >= 2 and state.heads >= 18)
    assert opened[:2] == [A, B], opened
    assert state.reconnect_causes.get("canary_silent") == 1
    subs = [m["params"] for m in sockets[0].sent if m.get("method") == "eth_subscribe"]
    assert subs[-1] == ["logs", CANARY], "the canary rides the same socket, after the armed filter"
    assert state.canary["per_host"]["bnb-mainnet.g.alchemy.com"]["logs"] > 0
    assert "secret" not in json.dumps(state.canary)


def test_a_flowing_canary_keeps_the_host_and_is_never_handed_to_the_lane():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_k):
        opened.append(url)
        return Socket(_stream(clock, canary=True, heads=40, canary_every=3))
    transfers = _run(connect, clock, state, stop=lambda: state.heads >= 40)
    assert opened == [A] and "canary_silent" not in state.reconnect_causes
    assert transfers == [], "canary logs are counted only"
    assert state.canary["per_host"]["bsc-rpc.publicnode.com"]["logs"] == 13   # the 40th head stops first
    assert state.logs == 0 and "canary" not in state.delivered, "the lane's log liveness ignores it"


def test_without_a_canary_the_stream_is_todays():
    opened, clock, state, sockets = [], [1.0], P.PushState(), []

    def connect(url, **_k):
        opened.append(url)
        sockets.append(Socket(_stream(clock, canary=False, heads=40)))
        return sockets[-1]
    P.run_push_stream(A, ws_urls=(A, B), on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: ("0x" + "ab" * 20,), pool_manager=None, init_topic=None,
                      state=state, connect_fn=connect, sleep_fn=lambda s: None, now_fn=lambda: clock[0],
                      should_stop=lambda: state.heads >= 40)
    assert opened == [A] and state.canary is None
    assert [m["params"][0] for m in sockets[0].sent if m.get("method") == "eth_subscribe"] == ["newHeads", "logs"]


def test_the_56_canary_settings(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_WS_CANARY_56", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_WS_CANARY_ADDRESS_56", raising=False)
    assert cfg.memecoin_evm_ws_canary("56") == {"canary_filter": CANARY, "canary_silence_s": 120.0}
    assert cfg.memecoin_evm_ws_canary("4663") == {}
    monkeypatch.setenv("MEMECOIN_EVM_WS_CANARY_ADDRESS_56", "0x" + "AB" * 20)
    assert cfg.memecoin_evm_ws_canary("56")["canary_filter"] == {"address": "0x" + "ab" * 20, "topics": [SYNC]}
    monkeypatch.setenv("MEMECOIN_EVM_WS_CANARY_ADDRESS_56", "not-an-address")
    assert cfg.memecoin_evm_ws_canary("56") == {}, "a malformed pool turns the canary off, never a wildcard"
    monkeypatch.delenv("MEMECOIN_EVM_WS_CANARY_ADDRESS_56")
    monkeypatch.setenv("MEMECOIN_EVM_WS_CANARY_56", "0")
    assert cfg.memecoin_evm_ws_canary("56") == {}, "kill knob"


def test_a_refused_canary_never_drops_the_socket_or_judges_it():
    opened, clock, state = [], [1.0], P.PushState()

    def stream():
        yield json.dumps({"id": 1, "result": "h"})
        yield json.dumps({"id": 2, "result": "t"})
        yield json.dumps({"id": 3, "error": {"code": -32602, "message": "invalid filter"}})
        for i in range(30):
            clock[0] += 1.0
            yield json.dumps({"method": "eth_subscription", "params": {"subscription": "h", "result": {
                "number": hex(0x100 + i), "timestamp": "0x01"}}})

    def connect(url, **_k):
        opened.append(url)
        return Socket(stream())
    _run(connect, clock, state, stop=lambda: state.heads >= 30)
    assert opened == [A] and state.limit_backoffs == 0 and "canary_silent" not in state.reconnect_causes
