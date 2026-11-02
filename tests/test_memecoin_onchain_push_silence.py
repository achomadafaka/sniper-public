"""A socket that goes fully SILENT must still trip the stale rule (2026-09-29, 56 watcher).

`run_push_stream` read `for raw in ws:` and checked its 15 s stale rule only after a message. On
2026-09-29 the 56 watcher's socket stopped delivering at ~02:49:56Z without closing: nothing arrived,
so nothing was checked, the heartbeat (written on a head) froze, reconnects stayed 0, and the lane
was blind ~2.8 min until the server closed it. A receive tick now runs the same rule with no
message, so a silent socket reconnects within stale + 1 s, and the reconnect count the watcher's gap
backfill keys on moves.

The poll fallback rides the same change: its beat says `mode: poll` with the last error, and a poll
that fails on every call backs off instead of re-asking a provider that answered `limit exceeded`.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_memecoin_onchain_push_lanes import ARMED, MANAGER, _sub_reply, _push  # noqa: E402
from sniper.execution import evm_v4_quote as V  # noqa: E402
from sniper.services import memecoin_onchain_push as P  # noqa: E402
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _SilentSocket:
    """A real-client-shaped socket: `recv(timeout)`. After its script it delivers NOTHING, and each
    empty receive lets `timeout` seconds pass, as a quiet socket does."""

    def __init__(self, messages, clock, sent):
        self.messages, self.clock, self.sent = list(messages), clock, sent

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        self.clock.t += float(timeout)
        raise TimeoutError()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _run(stale_s=15.0):
    clock, sent, opened = _Clock(), [], []

    def connect_fn(_url, **_kw):
        opened.append(clock.t)
        head = _push("0xheads", {"number": hex(len(opened))})
        return _SilentSocket([_sub_reply(1, "0xheads"), head], clock, sent)
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=clock,
                      stale_after_s=stale_s, should_stop=lambda: len(opened) >= 2 or clock.t >= 100)
    return state, opened


def test_a_silent_socket_reconnects_within_stale_plus_one_second():
    state, opened = _run(stale_s=15.0)
    assert len(opened) == 2, "the silent socket was replaced"
    assert opened[1] <= 15.0 + 1.0, opened
    assert state.reconnects == 1, "counted, so the watcher's gap backfill fires on the next head"


def test_a_socket_that_keeps_delivering_heads_is_never_replaced():
    clock, sent, opened = _Clock(), [], []

    class _Busy(_SilentSocket):
        def recv(self, timeout=None):
            if self.messages:
                return self.messages.pop(0)
            self.clock.t += 1.0
            return _push("0xheads", {"number": hex(int(self.clock.t))})

    def connect_fn(_url, **_kw):
        opened.append(clock.t)
        return _Busy([_sub_reply(1, "0xheads")], clock, sent)
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=clock,
                      stale_after_s=15.0,
                      should_stop=lambda: clock.t >= 60.0 or len(opened) >= 3)   # bounded either way
    assert opened == [0.0]


# --- the poll fallback's beat and backoff ------------------------------------------------------------

def _poll(monkeypatch, tmp_path, *, fail):
    import sqlite3
    import scripts.research.onchain_evm_wallet_watch as w
    beats, sleeps = [], []
    conn = sqlite3.connect(":memory:")
    # The fakes answer THIS thread only (2026-09-29): `w.time` is the process-wide time module, and a
    # finite iterator read by another thread in the xdist worker ran out under a deploy run.
    clock = iter([0.0, 0.0] + [float(i) for i in range(1, 400)])
    patch_local(monkeypatch, "monotonic", lambda: next(clock))
    patch_local(monkeypatch, "sleep", sleeps.append)
    monkeypatch.setattr(w, "get_connection", lambda path: conn)
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda c: None)
    monkeypatch.setattr(w, "_verified_addresses", lambda c: {"0xabc": ("u", "h")})
    monkeypatch.setattr(w, "_require_addresses", lambda c: {"0xabc": ("u", "h")})
    monkeypatch.setattr(w, "_write_beat", beats.append)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://rpc.test")

    def rpc(url, method, params):
        if method == "eth_blockNumber":
            return {"result": "0x64"}
        if method == "eth_getLogs":
            if fail:
                return {"error": {"code": -32005, "message": "limit exceeded"}}
            return {"result": []}
        raise AssertionError(method)
    monkeypatch.setattr(w, "_rpc", rpc)
    w.run_poll(duration_s=40.0, period_s=1.0, price=object())
    return beats, sleeps


def test_a_failing_poll_says_so_in_the_beat_and_backs_off(monkeypatch, tmp_path):
    beats, sleeps = _poll(monkeypatch, tmp_path, fail=True)
    failing = [b for b in beats if b.get("error") == "get_logs"]
    assert failing and all(b.get("mode") == "poll" for b in failing), beats[:3]
    assert "limit exceeded" in str(failing[-1].get("last_poll_error"))
    assert failing[-1].get("poll_error_streak", 0) >= 3
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0], sleeps[:6]
    assert max(sleeps) <= 30.0


def test_a_healthy_poll_keeps_its_cadence_and_says_poll(monkeypatch, tmp_path):
    beats, sleeps = _poll(monkeypatch, tmp_path, fail=False)
    assert beats and all(b.get("mode") == "poll" for b in beats)
    assert all(b.get("poll_error_streak", 0) == 0 for b in beats)
    assert set(sleeps) <= {1.0}, sleeps[:6]


def test_the_backoff_survives_a_very_long_outage():
    import scripts.research.onchain_evm_wallet_watch as w
    assert w._poll_backoff(1.0, 1) == 1.0 and w._poll_backoff(1.0, 6) == 30.0
    assert w._poll_backoff(0.25, 5000) == 30.0, "no OverflowError after thousands of failures"


# --- no reconnect storm after a silence reconnect (Gamma, 2026-09-29) --------------------------------
# new_connection() keeps the previous socket's last_head_at, so the stale rule judged a NEW socket by
# the OLD socket's last head: any new socket that went one 1 s tick without a head was replaced at
# once (Gamma's probe: 11 reconnects in 20 s; about a coin flip per attempt on 8453's 2 s blocks).

import pytest  # noqa: E402

_WAIT = object()


class _Clk:
    t = 0.0

    def __call__(self):
        return self.t


class _Scripted:
    """Gamma's storm probe (2026-09-29). WAIT = one empty receive (the timeout passes). After the
    script: a head every `gap` s (a healthy 2 s chain), or nothing at all when dead."""

    def __init__(self, script, clock, dead=False, gap=2.0):
        self.script, self.clock, self.dead, self.gap, self.n = list(script), clock, dead, gap, 0

    def send(self, raw):
        pass

    def recv(self, timeout=None):
        if self.script:
            m = self.script.pop(0)
            if m is _WAIT:
                self.clock.t += float(timeout)
                raise TimeoutError()
            return m
        if self.dead:
            self.clock.t += float(timeout)
            raise TimeoutError()
        self.clock.t += self.gap
        self.n += 1
        return _push("0xheads", {"number": hex(1000 + self.n)})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _storm(second_script):
    clock, opened = _Clk(), []

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        if len(opened) == 1:       # healthy, one head, then silent: the reason for the reconnect
            return _Scripted([_sub_reply(1, "0xheads"), _push("0xheads", {"number": "0x1"})],
                             clock, dead=True)
        return _Scripted(list(second_script), clock)
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, should_stop=lambda: clock.t >= 60 or len(opened) >= 12)
    return state, opened


@pytest.mark.parametrize("empty_ticks", [0, 1, 2])
def test_a_new_socket_is_not_judged_by_the_dead_sockets_last_head(empty_ticks):
    """After a silence reconnect the previous head is >15 s old by construction. A fresh socket
    whose first head needs 1-2 empty 1 s ticks (8453's 2 s blocks) must not be replaced for it."""
    state, opened = _storm([_sub_reply(1, "0xheads")] + [_WAIT] * empty_ticks)
    assert len(opened) == 2, opened          # the dead socket replaced once, and never again
    assert state.reconnects == 1


def test_a_new_sockets_first_log_is_not_judged_by_the_dead_sockets_last_head():
    """The same storm through the post-message check: the new socket's first message is a transfer
    log, not a head."""
    log = {"address": "0x" + "ab" * 20, "topics": [P.TRANSFER_TOPIC, "0x" + "00" * 32,
                                                    P.address_topic(ARMED)], "data": "0x1"}
    state, opened = _storm([_sub_reply(1, "0xheads"), _sub_reply(2, "0xtransfers"),
                            _push("0xtransfers", log)])
    assert len(opened) == 2, opened
    assert state.reconnects == 1
