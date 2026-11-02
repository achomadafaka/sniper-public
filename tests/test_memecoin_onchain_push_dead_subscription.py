"""A dead topic-only subscription behind a live socket (spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md).

After the 05:30:42Z socket-error reconnect the 4663 socket's transfers and sells subscriptions were acked
and delivered nothing for 22+ minutes while heads and Initialize flowed; 3 J7 buys by an acked address were
never detected. Two defects in the push stream: (D1) a socket-error reconnect never counted as a
reconnect, so the watcher's gap backfill (keyed on the count) never ran after one; and (D2) nothing could
tell the stream to drop a socket that is alive but deaf. `force_reconnect` is the watcher's lever.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_memecoin_onchain_push_lanes import ARMED, MANAGER, _push, _sub_reply  # noqa: E402
from sniper.execution import evm_v4_quote as V  # noqa: E402
from sniper.services import memecoin_onchain_push as P  # noqa: E402


class _Clk:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Sock:
    """`recv(timeout)`: the script, then heads every `gap` s, or an error when `error` is set."""

    def __init__(self, script, clock, *, gap=1.0, error=False):
        self.script, self.clock, self.gap, self.error, self.n = list(script), clock, gap, error, 0

    def send(self, raw):
        pass

    def recv(self, timeout=None):
        if self.script:
            return self.script.pop(0)
        if self.error:
            raise ConnectionResetError("socket error")
        self.clock.t += self.gap
        self.n += 1
        return _push("0xheads", {"number": hex(100 + self.n)})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stream(connect_fn, clock, **kw):
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, **kw)
    return state


def test_a_socket_error_reconnect_counts_as_a_reconnect():
    """D1: the watcher's gap backfill fires on a changed count; a socket error left it at 0."""
    clock, opened = _Clk(), []

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        head = _push("0xheads", {"number": "0x1"})
        return _Sock([_sub_reply(1, "0xheads"), head], clock, error=len(opened) == 1)
    state = _stream(connect_fn, clock, should_stop=lambda: len(opened) >= 2 and clock.t >= 5)
    assert len(opened) == 2 and state.reconnects == 1


def test_a_refused_subscription_is_not_counted_as_a_reconnect():
    """A provider that says no has its own backoff and poll; it is not a gap on a live lane."""
    clock, opened = _Clk(), []
    refusal = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "limit"}})

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        return _Sock([refusal] if len(opened) == 1 else [_sub_reply(1, "0xheads")], clock)
    state = _stream(connect_fn, clock, should_stop=lambda: len(opened) >= 2 and clock.t >= 5)
    assert state.limit_backoffs == 1 and state.reconnects == 0


def test_force_reconnect_drops_a_live_socket_once():
    clock, opened, asked = _Clk(), [], []

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        return _Sock([_sub_reply(1, "0xheads")], clock)

    def force():
        asked.append(clock.t)
        return 5.0 <= clock.t and len(opened) == 1
    state = _stream(connect_fn, clock, force_reconnect=force,
                    should_stop=lambda: clock.t >= 12)
    assert len(opened) == 2 and 5.0 <= opened[1] <= 7.0, opened
    assert state.reconnects == 1
    assert len(asked) >= 10, "consulted on every message, not once"


def test_without_a_force_lever_a_healthy_socket_stays():
    clock, opened = _Clk(), []

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        return _Sock([_sub_reply(1, "0xheads")], clock)
    state = _stream(connect_fn, clock, force_reconnect=lambda: False, should_stop=lambda: clock.t >= 12)
    assert opened == [0.0] and state.reconnects == 0


def test_force_reconnect_is_consulted_on_a_quiet_socket_too():
    """A deaf socket may deliver nothing at all between heads: the receive tick asks as well."""
    clock, opened = _Clk(), []

    class _Quiet(_Sock):
        def recv(self, timeout=None):
            if self.script:
                return self.script.pop(0)
            self.clock.t += float(timeout)
            if self.clock.t % 4 < 1:
                return _push("0xheads", {"number": hex(int(self.clock.t))})
            raise TimeoutError()

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        return _Quiet([_sub_reply(1, "0xheads")], clock)
    _stream(connect_fn, clock, force_reconnect=lambda: 6.0 <= clock.t < 6.5 and len(opened) == 1,
            should_stop=lambda: clock.t >= 12)
    assert len(opened) == 2


# --- the armed set is re-read on heads too, at most once per roster_check_s (Alpha, 2026-09-29) -------
# The check ran only after a delivered non-head message, so a newly armed wallet stayed unsubscribed
# until something else was delivered: ~2.5 min on average on 56.

def _roster_stream(clock, roster, reads, **kw):
    def addresses_fn():
        reads.append(clock.t)
        return list(roster)
    opened = []

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        return _Sock([_sub_reply(1, "0xheads"), _sub_reply(2, "0xtr")], clock, gap=0.5)
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=addresses_fn, pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, **kw)
    return state, opened


def test_a_roster_change_resubscribes_within_the_check_interval_without_any_delivery():
    clock, roster, reads = _Clk(), [ARMED], []
    NEW = "0x" + "12" * 20

    def stop():
        if clock.t >= 3.0 and NEW not in roster:
            roster.append(NEW)                   # armed at t=3, no transfer delivered at all
        return clock.t >= 20
    state, opened = _roster_stream(clock, roster, reads, roster_check_s=5.0, should_stop=stop)
    assert len(opened) == 2 and 3.0 <= opened[1] <= 3.0 + 5.0 + 2.0, opened
    assert state.reconnects == 1 and state.last_reconnect_cause == "armed_set"
    assert state.reconnect_causes == {"armed_set": 1}, "every counted reconnect is counted by cause"


def test_the_roster_is_read_at_most_once_per_check_interval_on_heads():
    clock, roster, reads = _Clk(), [ARMED], []
    _roster_stream(clock, roster, reads, roster_check_s=5.0, should_stop=lambda: clock.t >= 30)
    # heads every 0.5 s for 30 s: one read at connect, then one per 5 s
    assert 6 <= len(reads) <= 8, reads
