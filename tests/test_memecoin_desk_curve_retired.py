import json

from sniper.services import memecoin_desk_capture as C

NOW = 1793000000.0


def beat(tmp_path, **kw):
    (tmp_path / "universe.heartbeat").write_text(json.dumps(kw))


def test_curve_retired_needs_a_fresh_heartbeat_and_unknown_is_none(tmp_path):
    assert C.curve_retired(tmp_path, NOW) is None                               # no heartbeat
    beat(tmp_path, ts=NOW - 2)
    assert C.curve_retired(tmp_path, NOW) is None                               # old worker: field absent
    beat(tmp_path, ts=NOW - 2, curve_capture=False)
    assert C.curve_retired(tmp_path, NOW) is True
    beat(tmp_path, ts=NOW - 2, curve_capture=True)
    assert C.curve_retired(tmp_path, NOW) is False
    for ts in (NOW - 21, NOW + 5, "x", None):                                   # stale, future, malformed
        beat(tmp_path, ts=ts, curve_capture=False)
        assert C.curve_retired(tmp_path, NOW) is None
    beat(tmp_path, ts=NOW - 2, curve_capture=0)                                 # not a bool
    assert C.curve_retired(tmp_path, NOW) is None
    (tmp_path / "universe.heartbeat").write_text("{bad")
    assert C.curve_retired(tmp_path, NOW) is None
