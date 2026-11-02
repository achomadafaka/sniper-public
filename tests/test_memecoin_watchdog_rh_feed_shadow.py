"""The 4663 feed shadow's watchdog: launch, replace, kill a wedge, leave a foreign root alone.

RUNS ONLY INSIDE ITS OWN PID NAMESPACE (`unshare -c -p -f --mount-proc`). The watchdog reads the real
process table and sends real signals; a tmp ROOT isolates files, never pids (2026-09-29: a whole-
watchdog test killed the live Solana watcher). In the namespace the only processes it can see are the
ones each case starts. The file name keeps it inside the covering-set exclusion `test_memecoin_watchdog_`.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCHDOG = REPO / "scripts" / "memecoin_rh_feed_shadow_watchdog.sh"
NS = ["unshare", "-c", "-p", "-f", "--mount-proc"]
REL = "scripts/memecoin_rh_feed_shadow_loop.py"

# Each case: start the given owned/foreign stand-ins, run the watchdog once, report who is alive.
CASE = r'''
set -u
T="$1"; shift
start() {   # start <state-root> <script path>: a process whose LAST argument is the script
  env SNIPER_STATE_ROOT="$1" bash "$T/sleeper.sh" "$2" >/dev/null 2>&1 &
  echo $!
}
OLD="$T/.runtime/memecoin/releases/aaaa1111/$REL_PATH"
CUR="$T/.runtime/memecoin/releases/bbbb2222/$REL_PATH"
PIDS=""
for spec in "$@"; do
  case "$spec" in
    old) PIDS="$PIDS old:$(start "$T" "$OLD")" ;;
    cur) PIDS="$PIDS cur:$(start "$T" "$CUR")" ;;
    foreign) PIDS="$PIDS foreign:$(start /elsewhere "$OLD")" ;;
  esac
done
sleep 0.3
bash "$WATCHDOG"
for p in $PIDS; do
  name="${p%%:*}"; pid="${p##*:}"
  st="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')"     # a killed child is a zombie until reaped
  case "$st" in ""|Z*) echo "dead $name" ;; *) echo "alive $name" ;; esac
done
'''


@pytest.fixture
def tree(tmp_path):
    probe = subprocess.run(NS + ["true"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("no unprivileged pid namespace: %s" % probe.stderr.strip()[:120])
    for rel in ("logs", "data", ".runtime/memecoin/releases/aaaa1111/scripts",
                ".runtime/memecoin/releases/bbbb2222/scripts", "scripts"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    for base in (tmp_path, tmp_path / ".runtime/memecoin/releases/aaaa1111",
                 tmp_path / ".runtime/memecoin/releases/bbbb2222"):
        (base / REL).write_text("# stand-in\n")
    os.symlink(tmp_path / ".runtime/memecoin/releases/bbbb2222", tmp_path / ".runtime/memecoin/current")
    (tmp_path / ".env").write_text("MEMECOIN_RH_FAST_LANE=shadow\n")
    (tmp_path / "sleeper.sh").write_text("sleep 30\n")
    stub = tmp_path / "python"           # records its argv and starts a heartbeat, then exits
    stub.write_text('#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s/launched.txt"\n'
                    'touch "%s/data/memecoin_rh_feed_shadow.heartbeat"\n' % (tmp_path, tmp_path))
    stub.chmod(0o755)
    return tmp_path


def run(tree, *specs, beat_age_s=None):
    beat = tree / "data" / "memecoin_rh_feed_shadow.heartbeat"
    if beat_age_s is not None:
        beat.touch()
        old = time.time() - beat_age_s
        os.utime(beat, (old, old))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMECOIN_", "SNIPER_"))} | {
        "SNIPER_STATE_ROOT": str(tree), "SNIPER_PYTHON": str(tree / "python"),
        "WATCHDOG": str(WATCHDOG), "REL_PATH": REL}
    out = subprocess.run(NS + ["bash", "-c", CASE, "case", str(tree), *specs], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    launched = (tree / "launched.txt").read_text().splitlines() if (tree / "launched.txt").exists() else []
    log = (tree / "logs" / "memecoin_rh_feed_shadow.log")
    return dict(line.split()[::-1] for line in out.stdout.splitlines() if line.startswith(("alive", "dead"))), \
        launched, log.read_text() if log.exists() else ""


def test_a_cold_start_launches_the_current_release(tree):
    _state, launched, log = run(tree)
    assert launched == ["-u %s/.runtime/memecoin/releases/bbbb2222/%s" % (tree, REL)]
    assert "started" in log


def test_live_also_runs_the_detector(tree):
    (tree / ".env").write_text("MEMECOIN_RH_FAST_LANE=live\n")
    _state, launched, log = run(tree)
    assert launched == ["-u %s/.runtime/memecoin/releases/bbbb2222/%s" % (tree, REL)]


def test_runner_shadow_also_runs_the_detector(tree):
    # M2 step 1: the in-runner lane hears only the detector, so runner_shadow needs it running.
    (tree / ".env").write_text("MEMECOIN_RH_FAST_LANE=runner_shadow\n")
    _state, launched, log = run(tree)
    assert launched == ["-u %s/.runtime/memecoin/releases/bbbb2222/%s" % (tree, REL)]
    assert "started" in log


def test_off_without_the_env_switch_and_a_running_shadow_is_stopped(tree):
    (tree / ".env").write_text("MEMECOIN_RH_FAST_LANE=off\n")
    state, launched, log = run(tree, "cur")
    assert launched == [] and state == {"cur": "dead"} and "stopping the running shadow" in log


def test_off_with_nothing_running_does_nothing(tree):
    (tree / ".env").write_text("MEMECOIN_RH_FAST_LANE=\n")
    state, launched, log = run(tree, "foreign")
    assert launched == [] and state == {"foreign": "alive"} and "stopping" not in log


def test_a_follower_on_an_old_release_is_replaced(tree):
    state, launched, log = run(tree, "old")
    assert state == {"old": "dead"} and "not bbbb2222" in log
    assert launched == ["-u %s/.runtime/memecoin/releases/bbbb2222/%s" % (tree, REL)]


def test_a_stale_heartbeat_kills_the_wedged_follower(tree):
    state, launched, log = run(tree, "cur", beat_age_s=600)
    assert state == {"cur": "dead"} and "stale" in log and len(launched) == 1


def test_a_healthy_follower_is_left_alone(tree):
    state, launched, _log = run(tree, "cur", beat_age_s=1)
    assert state == {"cur": "alive"} and launched == []


def test_a_foreign_state_root_is_never_signalled(tree):
    state, launched, _log = run(tree, "foreign")
    assert state == {"foreign": "alive"}
    assert len(launched) == 1                 # it is not ours, so ours is started beside it
