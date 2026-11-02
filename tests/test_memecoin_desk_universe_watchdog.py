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
import pytest
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



def _cmdline(pid):
    try:return Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
    except OSError:return []


@pytest.mark.parametrize('arguments',[[],['--wallet-flow']])
@pytest.mark.parametrize('unready_reads',[0,2])
def test_watchdog_starts_one_worker_and_stops_a_superseded_one(tmp_path,arguments,unready_reads,monkeypatch):
    name = "u_worker_%s.py" % uuid.uuid4().hex[:12]
    repo, old, state = tmp_path / "release", tmp_path / "old", tmp_path / "state"
    for d in (repo / "scripts", old / "scripts", state):
        d.mkdir(parents=True)
    script = (ROOT / "scripts" / "memecoin_desk_universe_watchdog.sh").read_text()
    assert script.count("memecoin_desk_universe_worker.py") == 1        # the one WORKER line the copy renames
    watchdog = repo / "scripts" / "watchdog.sh"
    watchdog.write_text(script.replace("memecoin_desk_universe_worker.py", name))
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
        assert _wait(lambda: _cmdline(superseded.pid)==[os.fsencode(sys.executable),os.fsencode(old/'scripts'/name)])
        assert _wait(lambda: _cmdline(not_python.pid)==[os.fsencode(editor),os.fsencode(old/'scripts'/name)])
        subprocess.run([str(watchdog),*arguments], env=env, check=True, timeout=20)
        pid = int((state / "data" / "desk" / "universe.pid").read_text())
        procs.append(pid)
        real_read=Path.read_bytes;remaining=[unready_reads]
        def read_bytes(path):
            if path==Path(f'/proc/{pid}/cmdline') and remaining[0]:
                remaining[0]-=1;return b''  # The forked shell child has not completed exec yet.
            return real_read(path)
        monkeypatch.setattr(Path,'read_bytes',read_bytes)
        assert _wait(lambda: superseded.poll() is not None)              # the old release's worker: TERM
        assert not_python.poll() is None                                  # argv[0] not python: untouched
        assert _alive(pid)
        expected=[os.fsencode(sys.executable),os.fsencode(repo/'scripts'/name),*[a.encode() for a in arguments]]
        assert _wait(lambda: _cmdline(pid)==expected)  # Popen's fork/exec child can briefly have empty argv.
        subprocess.run([str(watchdog),*arguments], env=env, check=True, timeout=20)  # next cron minute: no second copy
        assert int((state / "data" / "desk" / "universe.pid").read_text()) == pid
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
    watchdog.write_text((ROOT / "scripts" / "memecoin_desk_universe_watchdog.sh").read_text()
                        .replace("memecoin_desk_universe_worker.py", name))
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
        assert not (state / "data" / "desk" / "universe.pid").exists()    # nothing started this minute
    finally:
        stubborn.kill()
        pid_file = state / "data" / "desk" / "universe.pid"
        if pid_file.exists():                                              # a red run started the release copy
            try:
                os.kill(int(pid_file.read_text()), 9)
            except OSError:
                pass
