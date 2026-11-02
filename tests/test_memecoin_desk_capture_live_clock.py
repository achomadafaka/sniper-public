import json

from sniper.services import memecoin_desk_capture as C

NOW = 1800000000.


def _beat(tmp_path, ts):
    (tmp_path / "universe.heartbeat").write_text(json.dumps(dict(ts=ts, connected=True, ps_committed_through=ts, ps_capture_from=NOW - 100, pumpswap_capture=False)))


def test_a_heartbeat_that_advanced_during_the_read_is_not_ahead(tmp_path):
    _beat(tmp_path, NOW + 1.5)
    frozen = []
    assert C.capture_proof(tmp_path, NOW, venues=("ps",), why=frozen) is None and frozen == ["heartbeat_ahead"]
    live = []
    assert C.capture_proof(tmp_path, NOW, venues=("ps",), live_now=lambda: NOW + 2, why=live) is None
    assert live == ["capture_off"]     # past the clock checks, on to the next branch


def test_a_heartbeat_ahead_of_a_fresh_sample_still_refuses(tmp_path):
    _beat(tmp_path, NOW + 5)
    why = []
    assert C.capture_proof(tmp_path, NOW, venues=("ps",), live_now=lambda: NOW + 2, why=why) is None and why == ["heartbeat_ahead"]


def test_a_clock_stepping_back_refuses_and_never_hides_a_future_heartbeat(tmp_path):
    _beat(tmp_path, NOW)
    why = []
    assert C.capture_proof(tmp_path, NOW, venues=("ps",), live_now=lambda: NOW - 1, why=why) is None and why == ["clock_regressed"]


def test_a_commit_mark_past_the_fresh_sample_still_refuses(tmp_path):
    (tmp_path / "universe.heartbeat").write_text(json.dumps(dict(ts=NOW, connected=True, ps_committed_through=NOW + 5, ps_capture_from=NOW - 100, pumpswap_capture=True)))
    why = []
    assert C.capture_proof(tmp_path, NOW, venues=("ps",), live_now=lambda: NOW + 2, why=why) is None and why == ["commit_mark_invalid"]

