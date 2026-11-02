"""U producer watchdog: one worker per release path, a superseded path stopped, non-interpreters ignored.

The watchdog scans /proc by basename. Run as written it would TERM the live U worker on this host, so
the test runs a copy whose worker filename is unique to the test run; the logic is otherwise the file's.
"""
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLEEPER = "import time\nwhile True:\n    time.sleep(1)\n"


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    with open("/proc/%d/stat" % pid) as fh:
        return fh.read().split()[2] != "Z"


def test_watchdog_starts_one_worker_and_stops_a_superseded_one(tmp_path):
    name = "u_worker_%s.py" % uuid.uuid4().hex[:12]
    repo, old, state = tmp_path / "release", tmp_path / "old", tmp_path / "state"
    for d in (repo / "scripts", old / "scripts", state):
        d.mkdir(parents=True)
    script = (ROOT / "scripts" / "memecoin_desk_risk_watcher_watchdog.sh").read_text()
    assert script.count("memecoin_desk_risk_watcher.py") == 1        # the one WORKER line the copy renames
    watchdog = repo / "scripts" / "watchdog.sh"
    watchdog.write_text(script.replace("memecoin_desk_risk_watcher.py", name))
    watchdog.chmod(0o755)
    shutil.copytree(ROOT / "scripts" / "lib", repo / "scripts" / "lib")
    for d in (repo, old):
        (d / "scripts" / name).write_text(SLEEPER)
    env = {**os.environ, "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": sys.executable}
    procs = []
    try:
        superseded = subprocess.Popen([sys.executable, str(old / "scripts" / name)], env=env)
        editor = tmp_path / "vim"                                           # argv[0] "vim": not an interpreter
        editor.symlink_to(sys.executable)
        not_python = subprocess.Popen([str(editor), str(old / "scripts" / name)])
        procs += [superseded, not_python]
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)
        pid = int((state / "data" / "desk" / "risk_watcher.pid").read_text())
        procs.append(pid)
        assert _wait(lambda: superseded.poll() is not None)              # the old release's worker: TERM
        assert not_python.poll() is None                                  # argv[0] not python: untouched
        assert _alive(pid)
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)  # next cron minute: no second copy
        assert int((state / "data" / "desk" / "risk_watcher.pid").read_text()) == pid
    finally:
        for p in procs:
            try:
                (p.kill() if hasattr(p, "kill") else os.kill(p, 9))
            except OSError:
                pass


def test_a_superseded_worker_still_exiting_means_no_start_this_minute(tmp_path):
    # Alpha/Gamma on bd01e49a4: never two U gRPC streams at once beside the funded runner's
    name = "u_worker_%s.py" % uuid.uuid4().hex[:12]
    repo, old, state = tmp_path / "release", tmp_path / "old", tmp_path / "state"
    for d in (repo / "scripts", old / "scripts", state):
        d.mkdir(parents=True)
    watchdog = repo / "scripts" / "watchdog.sh"
    watchdog.write_text((ROOT / "scripts" / "memecoin_desk_risk_watcher_watchdog.sh").read_text()
                        .replace("memecoin_desk_risk_watcher.py", name))
    watchdog.chmod(0o755)
    shutil.copytree(ROOT / "scripts" / "lib", repo / "scripts" / "lib")
    (repo / "scripts" / name).write_text(SLEEPER)
    (old / "scripts" / name).write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                        "time.sleep(15)\n")
    env = {**os.environ, "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": sys.executable}
    stubborn = subprocess.Popen([sys.executable, str(old / "scripts" / name)], env=env)
    try:
        time.sleep(0.3)                                                    # its TERM handler is installed
        t0 = time.time()
        subprocess.run([str(watchdog)], env=env, check=True, timeout=30)
        assert 9 <= time.time() - t0 <= 15                                 # waited the 10 s, then gave up
        assert stubborn.poll() is None
        assert not (state / "data" / "desk" / "risk_watcher.pid").exists()    # nothing started this minute
    finally:
        stubborn.kill()
        pid_file = state / "data" / "desk" / "risk_watcher.pid"
        if pid_file.exists():                                              # a red run started the release copy
            try:
                os.kill(int(pid_file.read_text()), 9)
            except OSError:
                pass
