"""The inline host rotation state must reach the RUNNER heartbeat file (data/memecoin_runner.heartbeat), not only the lane's own sqlite beat: a new status field needs the runner's whitelist."""
import json

from sniper.services import memecoin_rh_fast_lane as lane
from sniper.services import memecoin_runner as runner

import test_memecoin_rh_inline_rotation as R


def _beat(tmp_path, **kw):
    path = tmp_path / "memecoin_runner.heartbeat"
    assert runner.write_heartbeat({"events": 0, "opened": 0}, True, path=str(path), **kw) is not False
    return json.loads(path.read_text())


def test_the_status_is_none_when_no_inline_host_set_exists(monkeypatch):
    monkeypatch.setitem(lane._STATUS, "inline", None)
    assert lane.status() is None


def test_the_real_heartbeat_writer_publishes_inline_hosts_with_the_active_index_and_states(monkeypatch, tmp_path):
    s, hosts, sent, alerts, logs, clock, ans = R.build(answers={R.URLS[0]: [R.QUOTA], R.URLS[1]: []})
    s.safety("0xm")
    s.safety("0xm")                                                           # host 1 spent, rotated to host 2
    R.settle()
    monkeypatch.setitem(lane._STATUS, "inline", s)
    beat = _beat(tmp_path, rh_fast_lane=lane.status())
    got = beat["rh_fast_lane"]["inline_hosts"]
    assert got["n"] == 2 and got["active"] == 2 and got["rotations"] == {"rotated": 1}
    assert [h["state"] for h in got["hosts"]] == ["quota spent", "healthy"] and "SECRETKEY" not in json.dumps(beat)


def test_no_inline_host_leaves_the_key_out_and_the_writer_survives_a_malformed_status(monkeypatch, tmp_path):
    assert "rh_fast_lane" not in _beat(tmp_path)
    beat = _beat(tmp_path, rh_fast_lane={"inline_hosts": {"n": "x", "active": object(), "hosts": [{"label": "host 1 of 1 (?)", "state": object(), "url": "https://secret/KEY"}, 5],
                                                            "rotations": {"rotated": 2, "bad": "z"}, "extra": "https://secret/KEY"}})
    got = beat["rh_fast_lane"]["inline_hosts"]
    assert got["n"] is None and got["active"] is None and got["hosts"][0] == {"label": "host 1 of 1 (?)", "state": None}
    assert got["rotations"] == {"rotated": 2} and "secret" not in json.dumps(beat)                  # only whitelisted, typed fields leave
def test_the_runner_loop_passes_the_lane_status_to_every_heartbeat_write():
    import re
    from pathlib import Path
    src = (Path(runner.__file__).resolve().parents[2] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert src.count("rh_fast_lane=_rh_fast_lane_status()") == src.count("base_fast_lane=_base_fast_lane_status()") >= 2
    assert re.search(r"def _rh_fast_lane_status\(\):", src)
