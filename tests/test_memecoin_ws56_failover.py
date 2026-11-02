"""56 push-lane host failover (2026-10-17, Alpha; MEMECOIN_EVM_WS_URLS_56).

publicnode refused the 56 watcher's subscription all day ("-32603: Internal error, transport", 37
"socket dark; polling" lines 04Z..13Z), so 56 detection ran on its ~32 req/min poll and made 0
attempts (53 the day before). With a host list, two refusals (or dark sockets) on a host inside the
window move the subscription to the next host; after a healthy interval there it returns to the
primary. One socket at a time; the poll fallback stays the last resort."""
import json

import pytest

from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

A = "wss://bsc-rpc.publicnode.com"
B = "wss://bnb-mainnet.g.alchemy.com/v2/secret-key"
REFUSED = json.dumps({"id": 1, "error": {"code": -32603, "message": "Internal error, transport"}})


class Socket:
    open_now = 0

    def __init__(self, messages):
        self.messages, self.sent = iter(messages), []

    def __enter__(self):
        Socket.open_now += 1
        assert Socket.open_now == 1, "two sockets open at once"
        return self

    def __exit__(self, *_a):
        Socket.open_now -= 1

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        raw = next(self.messages, None)
        if raw is None:
            raise TimeoutError
        return raw


def _heads(n, start=0x10):
    out = [json.dumps({"id": 1, "result": "heads"})]
    for i in range(n):
        out.append(json.dumps({"method": "eth_subscription", "params": {"subscription": "heads",
                               "result": {"number": hex(start + i), "timestamp": "0x01"}}}))
    return out


def _run(connect, *, stop, clock, state, **kw):
    return P.run_push_stream(
        A, ws_urls=(A, B), on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: (), pool_manager=None, init_topic=None, state=state,
        connect_fn=connect, sleep_fn=lambda s: clock.__setitem__(0, clock[0] + s),
        now_fn=lambda: clock[0], should_stop=stop, **kw)


def test_two_refusals_on_the_primary_inside_the_window_move_the_subscription_to_the_next_host():
    opened, clock, state, polls = [], [1.0], P.PushState(), []

    def connect(url, **_k):
        opened.append(url)
        return Socket([REFUSED] if url == A else _heads(1))
    _run(connect, stop=lambda: state.heads > 0, clock=clock, state=state,
         failover_after=2, failover_window_s=600.0, on_fallback=lambda: polls.append(clock[0]))
    assert opened == [A, A, B], "one refusal waits on the primary (today), the second fails over"
    assert polls, "the poll fallback still covers the wait on the primary"
    assert state.ws_pool["active_index"] == 1 and state.ws_pool["rotations"] == 1
    assert state.ws_pool["push_host"] == "bnb-mainnet.g.alchemy.com"
    assert "secret" not in json.dumps(state.ws_pool)


def test_refusals_further_apart_than_the_window_stay_on_the_primary():
    opened, clock, state = [], [1.0], P.PushState()
    script = {"n": 0}

    def connect(url, **_k):
        opened.append(url)
        script["n"] += 1
        if url == A and script["n"] == 1:
            return Socket([REFUSED])
        if url == A and script["n"] == 2:
            clock[0] += 700.0                       # the next refusal is outside the window
            return Socket([REFUSED])
        return Socket(_heads(1))
    _run(connect, stop=lambda: state.heads > 0, clock=clock, state=state,
         failover_after=2, failover_window_s=600.0)
    assert opened == [A, A, A]


def test_the_lane_returns_to_the_primary_after_a_healthy_interval_on_the_backup():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_k):
        opened.append(url)
        if url == A and len(opened) <= 2:
            return Socket([REFUSED])
        if url == B:
            def ticking():
                yield from _heads(1)
                for i in range(40):                 # 40 heads, 30 s apart on the fake clock
                    clock[0] += 30.0
                    yield _heads(1, start=0x20 + i)[1]
            return Socket(ticking())
        return Socket(_heads(1, start=0x100))
    _run(connect, stop=lambda: len(opened) >= 4 and state.heads >= 2, clock=clock, state=state,
         failover_after=2, failover_window_s=600.0, primary_return_s=600.0)
    assert opened[:4] == [A, A, B, A], opened
    assert state.reconnect_causes.get("primary_return") == 1
    assert state.ws_pool["push_host"] == "bsc-rpc.publicnode.com"


def test_one_host_keeps_todays_single_socket_behaviour():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_k):
        opened.append(url)
        return Socket([REFUSED] if len(opened) == 1 else _heads(1))
    P.run_push_stream(A, on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: (), pool_manager=None, init_topic=None, state=state,
                      connect_fn=connect, sleep_fn=lambda s: clock.__setitem__(0, clock[0] + s),
                      now_fn=lambda: clock[0], should_stop=lambda: state.heads > 0,
                      failover_after=2, primary_return_s=600.0)
    assert opened == [A, A] and state.ws_pool is None


# --- the host list ----------------------------------------------------------------------------------

@pytest.fixture
def pool56(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_WS_URL_56", A)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"56": (
        "https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com",
        "https://bnb-mainnet.g.alchemy.com/v2/secret-key")})


def test_the_56_host_list_names_publicnode_then_the_funded_pools_alchemy_socket(monkeypatch, pool56):
    monkeypatch.delenv("MEMECOIN_EVM_WS_URLS_56", raising=False)
    assert cfg.memecoin_evm_ws_pool("56") == (A,), "absent: today's single host"
    monkeypatch.setenv("MEMECOIN_EVM_WS_URLS_56", "publicnode,alchemy")
    assert cfg.memecoin_evm_ws_pool("56") == (A, B)
    monkeypatch.setenv("MEMECOIN_EVM_WS_URLS_56", "alchemy")
    assert cfg.memecoin_evm_ws_pool("56") == (A, B), "the configured primary always leads"


@pytest.mark.parametrize("raw", ["publicnode,wss://bsc-mainnet.nodereal.io/ws/v1/k", "publicnode,bogus",
                                 "publicnode,ws://plain.example"])
def test_research_hosts_unknown_names_and_plain_ws_are_never_push_hosts(monkeypatch, pool56, raw):
    monkeypatch.setenv("MEMECOIN_EVM_WS_URLS_56", raw)
    assert cfg.memecoin_evm_ws_pool("56") == (A,)


def test_the_watcher_failover_settings(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_WS_FAILOVER_AFTER", raising=False)
    assert cfg.memecoin_evm_ws_failover("56") == {"failover_after": 2, "failover_window_s": 600.0,
                                                  "primary_return_s": 900.0}
    assert cfg.memecoin_evm_ws_failover("1") == {"failover_after": 1, "failover_window_s": 600.0,
                                                 "primary_return_s": None}, "chain 1 keeps its rotation"
