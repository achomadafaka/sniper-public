"""Cross-process wake for the on-chain lane (operator 2026-09-27: remove the detection->execution
delay). Baseline, 468 Solana first attempts 09-25..09-27: detection commit -> attempt start p50
0.427 s, p90 0.860 s, the shape of the lane's 1 s poll. The watcher now sends one datagram after
it commits a detection; the lane selects on an abstract AF_UNIX socket and sweeps at once. The poll
stays for recovery; the datagram is only a hint and its payload is ignored."""
import socket
import threading
import time
import uuid

import pytest

from sniper.services import memecoin_onchain_wake as w
from sniper.util.state_paths import state_root


def _address():
    return "\0sniper-onchain-wake-test-" + uuid.uuid4().hex[:12]


def test_the_address_is_abstract_and_named_from_the_state_root():
    a = w.wake_address("/home/bigpapi/Desktop/Sniper")
    assert a.startswith("\0sniper-onchain-wake-") and len(a) == len("\0sniper-onchain-wake-") + 12
    assert a != w.wake_address("/tmp/another-checkout")
    # the watcher (no SNIPER_STATE_ROOT, run from the checkout) and the runner (SNIPER_STATE_ROOT set
    # to that checkout) must name the same socket
    root = str(state_root({}))
    assert w.wake_address(state_root({})) == w.wake_address(state_root({"SNIPER_STATE_ROOT": root}))


def test_a_send_with_nothing_listening_never_raises():
    assert w.send_wake(_address()) is False


def test_a_datagram_wakes_a_bound_lane_at_once_and_its_payload_is_ignored():
    addr = _address()
    lane = w.LaneWake(addr)
    assert lane.bind() is True
    try:
        for payload in (b"w", b"", b"x" * 900):          # any sender, any bytes: one sweep
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(payload, addr)
            t0 = time.monotonic()
            assert lane.wait(5.0) is True
            assert time.monotonic() - t0 < 0.05
            lane.clear()
        assert w.send_wake(addr) is True and lane.wait(5.0) is True
        lane.clear()
        t0 = time.monotonic()
        assert lane.wait(0.3) is False                    # nothing sent: the poll timeout
        assert 0.28 <= time.monotonic() - t0 < 0.6
        assert lane.stats["datagrams"] == 4 and lane.stats["bound"] is True
    finally:
        lane.close()


def test_clear_drains_every_queued_datagram():
    addr = _address()
    lane = w.LaneWake(addr)
    lane.bind()
    try:
        for _ in range(5):
            w.send_wake(addr)
        assert lane.wait(1.0) is True
        lane.clear()
        assert lane.wait(0.05) is False
    finally:
        lane.close()


def test_an_in_process_set_wakes_a_waiter_blocked_in_select():
    lane = w.LaneWake(_address())
    lane.bind()
    got = {}

    def waiter():
        t0 = time.monotonic()
        got["woke"] = lane.wait(5.0)
        got["s"] = time.monotonic() - t0
    try:
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        lane.set()
        t.join(2)
        assert got["woke"] is True and got["s"] < 0.2
    finally:
        lane.close()


def test_deploy_overlap_polls_then_binds_when_the_old_runner_goes():
    """EADDRINUSE while the old runner still holds the name: the new lane polls (no crash), keeps
    retrying the bind on every wait, and takes the name once the old process lets it go."""
    addr = _address()
    old = w.LaneWake(addr)
    assert old.bind() is True
    new = w.LaneWake(addr)
    try:
        assert new.bind() is False and new.stats["bound"] is False
        assert new.stats["bind_failures"] == 1 and "EADDRINUSE" in new.stats["last_error"]
        t0 = time.monotonic()
        assert new.wait(0.2) is False                     # poll-only, never raises
        assert time.monotonic() - t0 >= 0.18
        assert new.stats["bind_failures"] == 2            # retried on that wait
        old.close()
        assert new.wait(0.01) is False and new.stats["bound"] is True   # took the name
        w.send_wake(addr)
        assert new.wait(1.0) is True
    finally:
        old.close()
        new.close()


def test_back_to_back_wakes_keep_the_minimum_sweep_interval():
    addr = _address()
    lane = w.LaneWake(addr)
    lane.bind()
    try:
        w.send_wake(addr)
        assert lane.wait(1.0) is True
        t1 = time.monotonic()
        lane.clear()
        w.send_wake(addr)
        assert lane.wait(1.0) is True
        assert time.monotonic() - t1 >= w.MIN_SWEEP_INTERVAL_S - 0.002
    finally:
        lane.close()


def test_the_lane_waits_through_wait_for_next_sweep_and_the_rollback_is_an_event(monkeypatch):
    from sniper.services import memecoin_onchain_lane as ol
    from sniper.settings import memecoin as cfg
    addr = _address()
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_LANE_WAKE", True)
    lane = w.lane_wake(address=addr)
    try:
        assert isinstance(lane, w.LaneWake)
        w.send_wake(addr)
        t0 = time.monotonic()
        assert ol.wait_for_next_sweep(lane, 1.0) is True and time.monotonic() - t0 < 0.1
        assert ol.wait_for_next_sweep(lane, 0.25) is False     # drained: the poll again
    finally:
        lane.close()
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_LANE_WAKE", False)
    rollback = w.lane_wake(address=addr)
    assert isinstance(rollback, threading.Event)               # today's in-process event, no socket
    assert w.send_wake(addr) is False                          # nothing bound under rollback


def test_the_lane_heartbeat_carries_the_wake_counters(tmp_path):
    import json
    from sniper.services import memecoin_onchain_lane as ol
    path = tmp_path / "lane.json"
    ol.write_lane_heartbeat(cursor=7, now_iso="2026-09-27T18:00:00+00:00", path=path,
                            wake={"bound": True, "datagrams": 3, "bind_failures": 0,
                                  "last_error": None, "extra": "dropped"})
    got = json.loads(path.read_text())
    assert got == {"ts": "2026-09-27T18:00:00+00:00", "cursor": 7,
                   "wake": {"bound": True, "datagrams": 3, "bind_failures": 0, "last_error": None}}


# --- the watcher side -------------------------------------------------------------------------

def _watcher():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "research" / "onchain_wallet_watch.py"
    spec = importlib.util.spec_from_file_location("onchain_wallet_watch_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    rowcount = 1

    def __init__(self, order, fail_commit=False):
        self.order, self.fail_commit = order, fail_commit

    def execute(self, sql, params=()):
        self.order.append("insert")
        return self

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("database is locked")
        self.order.append("commit")


def test_the_watcher_wakes_the_lane_only_after_its_commit(monkeypatch):
    mod = _watcher()
    order = []
    monkeypatch.setattr(mod, "send_wake", lambda address: order.append("wake") or True)
    mod._store_detection(_Conn(order), ("row",))
    assert order == ["insert", "commit", "wake"]
    order.clear()
    with pytest.raises(RuntimeError):
        mod._store_detection(_Conn(order, fail_commit=True), ("row",))
    assert order == ["insert"]                             # no commit, no wake


def test_a_failing_wake_never_reaches_the_watcher_loop(monkeypatch):
    mod = _watcher()
    order = []

    def broken(address):
        raise OSError("socket table full")
    monkeypatch.setattr(mod, "send_wake", broken)
    mod._store_detection(_Conn(order), ("row",))
    assert order == ["insert", "commit"]
