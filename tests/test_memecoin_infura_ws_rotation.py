"""Infura socket failover uses one active subscription and never exposes credentials."""
import json

from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg


FIRST = "wss://mainnet.infura.io/ws/v3/first-secret"
SECOND = "wss://mainnet.infura.io/ws/v3/second-secret"


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def __iter__(self):
        return self.messages

    def recv(self, timeout=None):
        raw = next(self.messages, None)
        if raw is None:
            raise TimeoutError
        return raw


def run(connect, *, stop, clock, state=None, chain_id=None):
    state = state or P.PushState()
    return P.run_push_stream(
        FIRST, ws_urls=(FIRST, SECOND), chain_id=chain_id,
        on_transfer=lambda _log: None, on_initialize=lambda _log: None,
        addresses_fn=lambda: (), pool_manager=None, init_topic=None,
        state=state, connect_fn=connect, sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        now_fn=lambda: clock[0], should_stop=stop)


def head():
    return [json.dumps({"id": 1, "result": "heads"}), json.dumps({
        "method": "eth_subscription", "params": {"subscription": "heads", "result": {
            "number": "0x10", "timestamp": "0x01"}}})]


def test_daily_refusal_uses_backup_without_minute_wait():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_kwargs):
        opened.append(url)
        messages = [json.dumps({"id": 1, "error": {"code": -33000,
            "message": "You have reached your daily credit limit"}})] if url == FIRST else head()
        return Socket(messages)

    run(connect, stop=lambda: state.heads > 0, clock=clock, state=state)
    assert opened == [FIRST, SECOND]
    assert clock[0] == 1.0
    assert state.ws_pool["active_index"] == 1
    assert state.ws_pool["rotations"] == 1
    assert state.ws_pool["heads"] == [0, 1]
    assert "secret" not in json.dumps(state.ws_pool)
    assert state.reconnects == 1  # existing watcher gap recovery sees the switch


def test_both_daily_exhausted_are_not_retried_before_reset():
    opened, clock = [], [1.0]

    def connect(url, **_kwargs):
        opened.append(url)
        return Socket([json.dumps({"id": 1, "error": {"code": -33000,
            "message": "daily credit limit reached"}})])

    run(connect, stop=lambda: clock[0] >= 90, clock=clock)
    assert opened == [FIRST, SECOND]
    assert 90 <= clock[0] < 120


def test_wrong_chain_socket_never_subscribes_and_uses_backup():
    opened, sockets, clock, state = [], [], [1.0], P.PushState()

    def connect(url, **_kwargs):
        opened.append(url)
        socket = Socket([json.dumps({"id": 0, "result": "0x38" if url == FIRST else "0x1"}),
                         *head()])
        sockets.append(socket)
        return socket

    run(connect, stop=lambda: state.heads > 0, clock=clock, state=state, chain_id="1")
    assert opened == [FIRST, SECOND]
    assert [item["method"] for item in sockets[0].sent] == ["eth_chainId"]
    assert sockets[1].sent[1]["method"] == "eth_subscribe"


def test_ethereum_ws_pool_keeps_explicit_primary_and_only_infura_backups(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_WS_URL_1", SECOND)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (
        "https://mainnet.infura.io/v3/first-secret", "https://mainnet.infura.io/v3/second-secret",
        "https://rpc.mevblocker.io", "https://ethereum-rpc.publicnode.com")})
    assert cfg.memecoin_evm_ws_pool("1") == (SECOND, FIRST)


def test_other_chains_keep_single_socket(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_WS_URL_56", "wss://existing.example")
    assert cfg.memecoin_evm_ws_pool("56") == ("wss://existing.example",)


def test_both_transport_failures_have_bounded_retries():
    opened, clock = [], [1.0]

    def connect(url, **_kwargs):
        opened.append(url)
        raise TimeoutError

    run(connect, stop=lambda: clock[0] >= 90, clock=clock)
    assert opened[:2] == [FIRST, SECOND]
    assert len(opened) <= 8


def test_healthy_socket_stays_on_one_key():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_kwargs):
        opened.append(url)
        messages = head()
        return Socket([*messages, messages[1], messages[1]])

    run(connect, stop=lambda: state.heads == 3, clock=clock, state=state)
    assert opened == [FIRST]
    assert state.ws_pool["rotations"] == 0
    assert state.ws_pool["heads"] == [3, 0]


def test_daily_refusal_during_chain_probe_rotates_too():
    opened, clock, state = [], [1.0], P.PushState()

    def connect(url, **_kwargs):
        opened.append(url)
        proof = {"id": 0, "error": {"code": -33000, "message": "daily credit limit reached"}} \
            if url == FIRST else {"id": 0, "result": "0x1"}
        return Socket([json.dumps(proof), *head()])

    run(connect, stop=lambda: state.heads > 0, clock=clock, state=state, chain_id="1")
    assert opened == [FIRST, SECOND]
    assert clock[0] == 1.0
    assert state.ws_pool["refusals"] == [1, 0]
