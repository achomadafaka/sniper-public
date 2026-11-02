"""Ethereum's watcher must survive a skipped slot without manufacturing heartbeat delivery."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import scripts.research.onchain_evm_wallet_watch as W
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg


def drive(monkeypatch, tmp_path, *, chain="1", gap=None, global_stale=15.):
    """Real watcher wiring and push loop; only database, socket and clock boundaries are offline."""
    clock = SimpleNamespace(now=0.)
    opened, beats, states = [], [], []
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(W, "CHAIN", chain)
    monkeypatch.setattr(W, "get_connection", lambda *_: conn)
    monkeypatch.setattr(W, "_roster_connection", lambda: conn)
    monkeypatch.setattr(W.m, "ensure_memecoin_tables", lambda *_: None)
    monkeypatch.setattr(W, "_verified_addresses", lambda *_: {})
    monkeypatch.setattr(W, "_restart_anchor", lambda: (None, "offline"))
    monkeypatch.setattr(W, "HEARTBEAT", tmp_path / "watcher.heartbeat")
    monkeypatch.setattr(W, "_wall_clock", lambda: clock.now)
    monkeypatch.setattr(W, "_write_beat", lambda beat: beats.append((clock.now, beat)))
    monkeypatch.setattr(W, "_rpc", lambda *_a, **_k: pytest.fail("unexpected provider read"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_STALE_S", global_stale)
    monkeypatch.setattr(cfg, "memecoin_evm_ws_pool", lambda *_: ())
    monkeypatch.setattr(cfg, "memecoin_onchain_evm_j7_xcheck_chains", lambda: frozenset())

    class Socket:
        def __init__(self):
            self.replies, self.heads, self.next_head = [], 0, clock.now

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def send(self, raw):
            request = json.loads(raw)
            assert request["method"] == "eth_subscribe"
            self.replies.append(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                           "result": request["params"][0]}))

        def recv(self, timeout):
            if self.replies:
                return self.replies.pop(0)
            if clock.now >= self.next_head:
                self.heads += 1
                self.next_head = float("inf") if gap is None else clock.now + gap
                return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription", "params": {
                    "subscription": "newHeads", "result": {
                        "number": hex(self.heads), "timestamp": hex(1000 + int(clock.now))}}})
            clock.now += timeout
            raise TimeoutError()

    def connect(*_a, **_k):
        opened.append(clock.now)
        return Socket()

    real_stream = P.run_push_stream

    def stream(url, **callbacks):
        state = callbacks["state"]
        states.append(state)
        callbacks.update(connect_fn=connect, now_fn=lambda: clock.now,
                         sleep_fn=lambda *_: None,
                         should_stop=lambda: len(opened) >= 2 or state.heads >= 3 or clock.now >= 90.)
        return real_stream(url, **callbacks)

    monkeypatch.setattr(P, "run_push_stream", stream)
    W.run_push("wss://offline.invalid", observe_only=True, price=object(),
               read_transport=SimpleNamespace(tx_host=lambda: None))
    return states[0], opened, beats


@pytest.mark.parametrize("gap", [12., 24., 36.])
def test_ethereum_normal_and_skipped_slots_keep_one_socket_and_only_head_beats(monkeypatch, tmp_path, gap):
    state, opened, beats = drive(monkeypatch, tmp_path, gap=gap)
    assert opened == [0.] and state.reconnects == 0
    assert state.heads == 3 and [at for at, _ in beats] == [0., gap, 2 * gap]
    assert [beat["heads"] for _, beat in beats] == [1, 2, 3]


def test_ethereum_real_silence_still_reconnects_after_forty_five_seconds(monkeypatch, tmp_path):
    state, opened, beats = drive(monkeypatch, tmp_path)
    assert opened == [0., 46.]
    assert state.reconnect_causes == {"socket_silent": 1}
    assert [at for at, _ in beats] == [0.]  # Quiet receive ticks and ACKs cannot refresh delivery.


@pytest.mark.parametrize("chain", ["56", "8453", "4663", "5042", "999"])
def test_other_chains_keep_the_global_fifteen_second_silence_guard(monkeypatch, tmp_path, chain):
    state, opened, beats = drive(monkeypatch, tmp_path, chain=chain)
    assert opened == [0., 16.] and state.reconnect_causes == {"socket_silent": 1}
    assert [at for at, _ in beats] == [0.]


def test_other_chains_still_use_an_explicit_global_silence_setting(monkeypatch, tmp_path):
    state, opened, _ = drive(monkeypatch, tmp_path, chain="4663", global_stale=20.)
    assert opened == [0., 21.] and state.reconnect_causes == {"socket_silent": 1}


def test_ethereum_preserves_an_explicit_longer_global_silence_window(monkeypatch, tmp_path):
    state, opened, beats = drive(monkeypatch, tmp_path, global_stale=60.)
    assert opened == [0., 61.] and state.reconnect_causes == {"socket_silent": 1}
    assert [at for at, _ in beats] == [0.]
