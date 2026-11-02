"""The Base 8453 lane's runner-heartbeat dict (Alpha 2026-10-14): measurement only, counters in
memory under a lock, read only by the heartbeat writer. Never-moved counters read 0, an unknown
age reads None, and a lane that does not run leaves no key at all."""
import json
import os

import pytest

from sniper.services import memecoin_base_fast_lane as base
from sniper.services import memecoin_rh_fast_lane as rh
from sniper.services import memecoin_runner as runner
from test_memecoin_base_fast_lane import HOST, _ctx, datagram


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    built = base.build_base_lane({"8453": _ctx()}, log=lambda line: None, mode="runner_shadow",
                                 root=tmp_path, url=HOST, ro_connect=lambda: None,
                                 dex_base=lambda *a: None)
    base._ACTIVE.update(lane=built, mode="runner_shadow")
    yield built
    base._ACTIVE.clear()


def test_a_snapshot_failure_counts_one_error_and_leaves_the_success_age_unknown(lane):
    def down(method, params):
        raise TimeoutError("lane host timed out")
    lane["snapshot"].host = down
    assert lane["snapshot"].poll_once() is None
    got = base.status(now=1000.0)["snapshot"]
    assert got == {"reads": 0, "errors": 1, "last_success_age_s": None}
    answers = {"eth_getBalance": "0x10", "eth_call": "0x0", "eth_getTransactionCount": "0x3",
               "eth_gasPrice": "0x5"}
    lane["snapshot"].host = lambda method, params: answers[method]
    lane["snapshot"].clock = lambda: 990.0
    lane["snapshot"].poll_once()
    assert base.status(now=1000.0)["snapshot"] == {"reads": 1, "errors": 1, "last_success_age_s": 10.0}


def test_a_lane_that_never_moved_reads_zero_everywhere(lane):
    got = base.status(now=1000.0)
    assert got["mode"] == "runner_shadow"
    assert got["datagrams"] == {"received": 0, "dropped": {}}
    assert got["signals_judged"] == 0 and got["verdicts"] == {}
    assert got["paced"] == {"calls": 0, "http_429": 0, "transport_errors": 0}
    assert got["inline_paced"] == {"calls": 0, "http_429": 0, "transport_errors": 0}
    json.dumps(got)                                        # the heartbeat file takes it as it is


def test_received_dropped_by_reason_judged_and_verdict_classes(lane, monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_bsc_fast_lane.watcher_pid", lambda beat, now: 7)
    monkeypatch.setattr(rh, "write_record", lambda *a, **k: None)
    dispatch = lane["dispatch"]
    monkeypatch.setattr(dispatch, "_on_signal",
                        lambda payload, recv_epoch: {"reason": "hook:rejected:mcap $1 over ceiling"})
    uid = os.getuid()
    dispatch.on_datagram(datagram(), 7, uid, 1000.0)
    dispatch.on_datagram(datagram(), 7, uid, 1000.1)             # the same buy again
    dispatch.on_datagram(datagram(chain="4663"), 7, uid, 1000.2)
    dispatch.on_datagram(datagram(tx_hash="0x" + "99" * 32), 8, uid, 1000.3)
    got = base.status(now=1000.0)
    assert got["datagrams"] == {"received": 4, "dropped": {"duplicate": 1, "malformed": 1,
                                                           "not_the_watcher": 1}}
    assert got["signals_judged"] == 1 and got["verdicts"] == {"hook:rejected": 1}


@pytest.mark.parametrize("reason,cls", [("hook:rejected:mcap over", "hook:rejected"), ("built", "built"),
    ("float_short+built", "float_short+built"), ("source_initiation_unknown:x", "source_initiation_unknown"),
    (None, "none")])
def test_verdicts_count_by_a_bounded_class(reason, cls):
    assert base.verdict_class(reason) == cls


def test_the_heartbeat_has_the_key_only_while_the_lane_runs(tmp_path):
    path = tmp_path / "hb"
    base._ACTIVE.clear()
    assert base.status() is None
    runner.write_heartbeat({}, True, path=str(path), base_fast_lane=base.status())
    assert "base_fast_lane" not in json.loads(path.read_text())
    runner.write_heartbeat({}, True, path=str(path),
                           base_fast_lane={"mode": "runner_shadow", "signals_judged": 0})
    assert json.loads(path.read_text())["base_fast_lane"] == {"mode": "runner_shadow", "signals_judged": 0}


def test_the_live_lane_s_verdicts_are_counted_too(lane, monkeypatch):
    """Gamma LOW 1: the live path (held under LANE_HOLDS) counts its judged signal like the shadow."""
    monkeypatch.setattr("sniper.services.memecoin_bsc_fast_lane.watcher_pid", lambda beat, now: 7)
    held = rh.LaneHolds(bound_s=1.0, clock=lambda: 1.0, wake_fn=lambda: None)
    monkeypatch.setattr(rh, "LANE_HOLDS", held)
    dispatch = lane["dispatch"]
    dispatch.live = object()                               # live: the hold branch
    monkeypatch.setattr(dispatch, "_on_signal", lambda payload, recv_epoch: {"reason": "hook:filled:"})
    dispatch.on_datagram(datagram(), 7, os.getuid(), 1000.0)
    got = base.status(now=1000.0)
    assert got["signals_judged"] == 1 and got["verdicts"] == {"hook:filled": 1}


def test_a_stopped_lane_leaves_the_heartbeat_with_no_key(lane):
    """Gamma LOW 2: never frozen counters and a growing snapshot age after a stop."""
    assert base.status() is not None
    assert base.stop_base_lane(timeout_s=0.1) == []
    assert base.status() is None
