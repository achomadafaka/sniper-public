"""One command stops the live 4663 lane for Gamma's pilot hard stops (Alpha 2026-10-02): the stop file
first (honoured within a second, no restart, because the deploy refuses while the reservation that
fired H3 is open), then .env runner_shadow, then the runner restart on its live release. The deploy
is a fake here: nothing is restarted and no process is signalled."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_rh_fast_lane as lane

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_fast_lane_stop.sh"
SHA = "a" * 40


def _root(tmp_path, env_text, deploy_exit):
    (tmp_path / ".runtime" / "memecoin" / "releases" / SHA).mkdir(parents=True)
    os.symlink(tmp_path / ".runtime" / "memecoin" / "releases" / SHA,
               tmp_path / ".runtime" / "memecoin" / "current")
    (tmp_path / ".env").write_text(env_text)
    calls = tmp_path / "deploy_calls"
    fake = tmp_path / "fake_deploy.sh"
    fake.write_text("#!/bin/sh\necho \"$@\" >> %s\nexit %d\n" % (calls, deploy_exit))
    fake.chmod(0o755)
    env = dict(os.environ, SNIPER_STATE_ROOT=str(tmp_path), MEMECOIN_FAST_LANE_STOP_DEPLOY=str(fake))
    return env, calls


@pytest.mark.parametrize("env_text", ["A=1\nMEMECOIN_RH_FAST_LANE=live\nB=2\n", "A=1\n"])
def test_the_stop_writes_the_file_sets_runner_shadow_and_restarts_the_live_release(tmp_path, env_text):
    env, calls = _root(tmp_path, env_text, 0)
    out = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "data" / "memecoin_rh_fast_lane.stop").exists()
    lines = (tmp_path / ".env").read_text().splitlines()
    assert "MEMECOIN_RH_FAST_LANE=runner_shadow" in lines and "MEMECOIN_RH_FAST_LANE=live" not in lines
    assert "A=1" in lines
    assert calls.read_text().split() == [SHA, "--restart", "--defer-backup"]


def test_a_refused_restart_still_leaves_the_lane_stopped_and_says_what_to_run(tmp_path):
    env, calls = _root(tmp_path, "MEMECOIN_RH_FAST_LANE=live\n", 1)
    out = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert out.returncode == 2
    assert (tmp_path / "data" / "memecoin_rh_fast_lane.stop").exists()
    assert "restart REFUSED" in out.stdout and "--restart --defer-backup" in out.stdout


def test_the_stop_file_is_the_one_the_lane_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    assert lane.stop_file_path() == str(tmp_path / "data" / "memecoin_rh_fast_lane.stop")


def test_the_watch_stops_and_resumes_the_lane_with_the_file_and_logs_each_change_once(tmp_path):
    class Live:
        stopped = False
    live, lines, path = Live(), [], tmp_path / "memecoin_rh_fast_lane.stop"
    assert lane.apply_stop_file(live, str(path), lines.append) is False and lines == []
    path.write_text("x")
    lane.apply_stop_file(live, str(path), lines.append)
    lane.apply_stop_file(live, str(path), lines.append)
    assert live.stopped is True and len(lines) == 1 and "STOPPED" in lines[0]
    path.unlink()
    lane.apply_stop_file(live, str(path), lines.append)
    assert live.stopped is False and "running" in lines[-1] and len(lines) == 2


def test_start_lane_runs_the_stop_watch_only_when_live():
    import inspect
    src = inspect.getsource(lane.start_lane)
    assert 'threading.Thread(target=stop_watch, name="rh-fast-stop"' in src
    assert src.index("stop_watch, name") > src.index('if lane["live"] is not None:')



# ---- Gamma MEDIUM on 1ba4102eb: run from a release copy, the stop must reach the runner's root ----

def _release_copy(tmp_path):
    """The script as the release tree carries it: <root>/.runtime/memecoin/releases/<sha>/scripts."""
    import shutil
    scripts = tmp_path / ".runtime" / "memecoin" / "releases" / SHA / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    return scripts / SCRIPT.name


@pytest.mark.parametrize("via_current", [True, False], ids=["current_link", "release_path"])
def test_run_from_the_release_copy_it_stops_the_runners_own_root(tmp_path, via_current):
    env, calls = _root(tmp_path, "MEMECOIN_RH_FAST_LANE=live\n", 0)
    script = _release_copy(tmp_path)
    if via_current:
        script = tmp_path / ".runtime" / "memecoin" / "current" / "scripts" / SCRIPT.name
    env.pop("SNIPER_STATE_ROOT")
    out = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "data" / "memecoin_rh_fast_lane.stop").exists()
    assert "MEMECOIN_RH_FAST_LANE=runner_shadow" in (tmp_path / ".env").read_text()
    assert calls.read_text().split() == [SHA, "--restart", "--defer-backup"]


@pytest.mark.parametrize("missing", [".env", "current"])
def test_outside_the_runners_state_root_it_refuses_before_writing_anything(tmp_path, missing):
    env, calls = _root(tmp_path, "MEMECOIN_RH_FAST_LANE=live\n", 0)
    if missing == ".env":
        (tmp_path / ".env").unlink()
    else:
        (tmp_path / ".runtime" / "memecoin" / "current").unlink()
    out = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert out.returncode == 3 and "REFUSED" in out.stderr
    assert not (tmp_path / "data" / "memecoin_rh_fast_lane.stop").exists()
    assert not calls.exists()
    assert (missing == ".env") == (not (tmp_path / ".env").exists())   # no .env was created



@pytest.mark.parametrize("routes", [("v4_native",), ("v3_shared",)])
def test_a_stop_file_present_at_start_stops_the_lane_before_it_listens(tmp_path, monkeypatch, routes):
    import threading
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_rh_fast_lane.stop").write_text("x")

    class Live:
        stopped = False
        sender = None
    live = Live()
    live.routes = frozenset(routes)
    live.shared_connect = lambda: None  # threads are recorded, never run in this fixture
    built = {"live": live, "dispatch": None, "cache": None, "safety_fn": None, "paced": None,
             "db_path": str(tmp_path / "l.db"), "address_name": "\0x", "url": "https://r", "dex": None}
    monkeypatch.setattr(lane, "build_lane", lambda contexts, **kw: built)
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "live")
    # This process's lane registry stays clean: the threads below are never started (Gamma, c7f4626c3).
    monkeypatch.setattr(lane, "_LANE_RUNS", [], raising=False)
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(live.stopped))
    lane.start_lane({}, log=lambda *a: None, root=tmp_path)
    assert started and all(started)                  # stopped before any thread started



# ---- Alpha 2026-10-02: `--help` once ran the whole stop; arguments are read before any effect ----

@pytest.mark.parametrize("args, code", [(["-h"], 0), (["--help"], 0), (["--dry-run"], 64),
                                        (["now"], 64), (["--help", "x"], 0)])
def test_help_and_unknown_arguments_change_nothing(tmp_path, args, code):
    env, calls = _root(tmp_path, "MEMECOIN_RH_FAST_LANE=live\n", 0)
    out = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    assert out.returncode == code, out.stdout + out.stderr
    assert "usage:" in out.stdout + out.stderr
    assert ("REFUSED" in out.stderr) is (code == 64)
    assert not (tmp_path / "data" / "memecoin_rh_fast_lane.stop").exists()
    assert (tmp_path / ".env").read_text() == "MEMECOIN_RH_FAST_LANE=live\n"
    assert not calls.exists()
