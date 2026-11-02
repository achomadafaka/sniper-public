"""GPT judge shadow seat watchdog: one seat per release path, a superseded path stopped, and the knob is a kill switch.

The watchdog scans /proc by basename; the test runs a copy whose worker filename is unique to the test run."""
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


def _setup(tmp_path):
    name = "gpt_seat_%s.py" % uuid.uuid4().hex[:12]
    repo, old, state = tmp_path / "release", tmp_path / "old", tmp_path / "state"
    for d in (repo / "scripts", old / "scripts", state):
        d.mkdir(parents=True)
    script = (ROOT / "scripts" / "memecoin_desk_gpt_judge_watchdog.sh").read_text()
    assert script.count("memecoin_desk_gpt_judge_shadow.py") == 1          # the one WORKER line the copy renames
    watchdog = repo / "scripts" / "watchdog.sh"
    watchdog.write_text(script.replace("memecoin_desk_gpt_judge_shadow.py", name))
    watchdog.chmod(0o755)
    shutil.copytree(ROOT / "scripts" / "lib", repo / "scripts" / "lib")
    for d in (repo, old):
        (d / "scripts" / name).write_text(SLEEPER)
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_DESK_GPT_JUDGE_ENABLED"}
    env.update({"SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": sys.executable})
    return name, repo, old, state, watchdog, env


def test_starts_one_seat_and_stops_a_superseded_one_when_enabled(tmp_path):
    name, repo, old, state, watchdog, env = _setup(tmp_path)
    env["MEMECOIN_DESK_GPT_JUDGE_ENABLED"] = "1"
    procs = []
    try:
        superseded = subprocess.Popen([sys.executable, str(old / "scripts" / name)], env=env)
        procs.append(superseded)
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)
        pid = int((state / "data" / "desk" / "gpt_judge_shadow.pid").read_text())
        procs.append(pid)
        assert _wait(lambda: superseded.poll() is not None) and _alive(pid)
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)         # next minute: no second copy
        assert int((state / "data" / "desk" / "gpt_judge_shadow.pid").read_text()) == pid
    finally:
        for p in procs:
            try:
                (p.kill() if hasattr(p, "kill") else os.kill(p, 9))
            except OSError:
                pass


def test_the_knob_is_a_kill_switch_env_or_dotenv(tmp_path):
    name, repo, old, state, watchdog, env = _setup(tmp_path)
    procs = []
    try:
        # unset everywhere: nothing starts
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)
        assert not (state / "data" / "desk" / "gpt_judge_shadow.pid").exists()
        # .env says 1 (cron env unset): starts
        (state / ".env").write_text("OTHER=1\nMEMECOIN_DESK_GPT_JUDGE_ENABLED=1\n")
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)
        pid = int((state / "data" / "desk" / "gpt_judge_shadow.pid").read_text())
        procs.append(pid)
        assert _alive(pid)
        # a stale copy from an older release is also stopped when the knob goes off
        superseded = subprocess.Popen([sys.executable, str(old / "scripts" / name)], env=env)
        procs.append(superseded)
        (state / ".env").write_text("MEMECOIN_DESK_GPT_JUDGE_ENABLED=0\n")
        subprocess.run([str(watchdog)], env=env, check=True, timeout=20)
        assert _wait(lambda: not _alive(pid)) and _wait(lambda: superseded.poll() is not None)
    finally:
        for p in procs:
            try:
                (p.kill() if hasattr(p, "kill") else os.kill(p, 9))
            except OSError:
                pass
