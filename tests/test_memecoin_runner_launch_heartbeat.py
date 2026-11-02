"""The first exit sweep after a launch reaches the heartbeat at once, not one poll later.

The runner loop writes its heartbeat BEFORE the exit sweep in each cycle, so the first sweep's
stamp (bracket_ts) reached the file only with the next cycle's heartbeat, one 5 s poll later. The
deploy holds the entry kill switch until `runner_launch_health` sees bracket_ts >= launch, so
every deploy kept entries shut ~5 s after the new runner had already proved its sweep. On
2026-09-27 00:47Z that cost untaxxable's aqhpFfoG buy, refused kill_switch 0.1 s before the CLEAR.
"""
import json
import time

from sniper.services.memecoin_runner import (
    publish_sweep, read_heartbeat, runner_launch_health, write_heartbeat)

STARTED = "2026-09-27T00:47:19.710758+00:00"


def _cycle_one(path, now):
    # What cycle 1 writes: poll beacon fresh, auth ok, no sweep yet.
    write_heartbeat({"events": 3, "opened": 0}, auth_ok=True, path=path, ts=now,
                    bracket_ts=None, started_at=STARTED, tpu_status={"ready": True})


def test_the_first_sweep_is_visible_to_the_deploy_check_right_after_it_runs(tmp_path):
    path = str(tmp_path / "memecoin_runner.heartbeat")
    now = time.time()
    launched_at = now - 10.0
    _cycle_one(path, now - 6.0)
    assert runner_launch_health(read_heartbeat(path), launched_at, now)["reason"] == \
        "launch_sweep_missing"
    assert publish_sweep(now - 0.5, path=path, started_at=STARTED) is True
    beat = read_heartbeat(path)
    assert runner_launch_health(beat, launched_at, now)["reason"] == "ok"
    assert beat["bracket_ts"] == now - 0.5 and beat["ts"] >= now - 0.5
    # Everything else is the heartbeat this cycle already wrote.
    assert (beat["events"], beat["auth_ok"], beat["tpu"], beat["started_at"]) == (
        3, True, {"ready": True}, STARTED)


def test_a_heartbeat_left_by_the_previous_runner_is_never_republished(tmp_path):
    path = str(tmp_path / "memecoin_runner.heartbeat")
    old = {"ts": 1.0, "auth_ok": True, "started_at": "2026-09-27T00:10:23+00:00"}
    with open(path, "w") as fh:
        json.dump(old, fh)
    assert publish_sweep(time.time(), path=path, started_at=STARTED) is False
    assert read_heartbeat(path) == old


def test_no_heartbeat_or_no_sweep_stamp_writes_nothing(tmp_path):
    path = str(tmp_path / "memecoin_runner.heartbeat")
    assert publish_sweep(time.time(), path=path, started_at=STARTED) is False
    assert read_heartbeat(path) is None
    _cycle_one(path, 100.0)
    assert publish_sweep(None, path=path, started_at=STARTED) is False
    assert "bracket_ts" not in read_heartbeat(path)


def test_the_first_cycle_line_names_every_phase_and_when_the_sweep_was_published():
    """Cycle 1 always printed `phase{}` in the periodic report, so how much of a deploy's
    stand-down went to cycle 1's feed pass versus its first exit sweep was unmeasured."""
    import scripts.memecoin_runner_loop as loop
    line = loop.first_cycle_line({"copy": 3.2106, "maint.bracket": 1.5, "cycle": 5.4}, 4.75)
    assert line == ("[memecoin] first cycle: copy=3.211 cycle=5.400 maint.bracket=1.500 "
                    "published=4.750")
    assert loop.first_cycle_line({"cycle": 1.0}, None).endswith("published=no")
