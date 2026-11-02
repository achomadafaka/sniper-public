"""JUDGE watchdog: MEMECOIN_DESK_JUDGE_PLAYBOOK (env, else state-root .env) picks the playbook; a missing file starts nothing."""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDER = ("import sys, time\nopen(sys.argv[sys.argv.index('--playbook') + 1] + '.seen', 'w').close()\n"
            "while True:\n    time.sleep(1)\n")


def _run(tmp_path, dotenv):
    name = "judge_seat_%s.py" % uuid.uuid4().hex[:12]
    repo, state = tmp_path / "release", tmp_path / "state"
    (repo / "scripts").mkdir(parents=True)
    state.mkdir()
    script = (ROOT / "scripts" / "memecoin_desk_judge_watchdog.sh").read_text()
    assert script.count("memecoin_desk_judge_worker.py") == 1
    wd = repo / "scripts" / "watchdog.sh"
    wd.write_text(script.replace("memecoin_desk_judge_worker.py", name))
    wd.chmod(0o755)
    subprocess.run(["cp", "-r", str(ROOT / "scripts" / "lib"), str(repo / "scripts" / "lib")], check=True)
    (repo / "scripts" / name).write_text(RECORDER)
    for rel in ("docs/superpowers/specs/2026-10-21-desk-judge-playbook.md", "pb/v19.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x")
    (state / ".env").write_text(dotenv)
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_DESK_JUDGE_PLAYBOOK"}
    env.update({"SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": sys.executable})
    subprocess.run([str(wd)], env=env, check=True, timeout=20)
    pid_file = state / "data" / "desk" / "judge_worker.pid"
    return repo, pid_file


def _seen(path):
    end = time.time() + 5
    while time.time() < end and not path.exists():
        time.sleep(0.05)
    return path.exists()


def _stop(pid_file):
    if pid_file.exists():
        os.kill(int(pid_file.read_text()), 9)


def test_dotenv_knob_picks_the_playbook(tmp_path):
    repo, pid = _run(tmp_path, "MEMECOIN_DESK_JUDGE_PLAYBOOK=pb/v19.md\n")
    try:
        assert _seen(repo / "pb" / "v19.md.seen")
    finally:
        _stop(pid)


def test_unset_knob_keeps_the_v18_file(tmp_path):
    repo, pid = _run(tmp_path, "OTHER=1\n")
    try:
        assert _seen(repo / "docs/superpowers/specs/2026-10-21-desk-judge-playbook.md.seen")
    finally:
        _stop(pid)


def test_missing_playbook_starts_nothing(tmp_path):
    _, pid = _run(tmp_path, "MEMECOIN_DESK_JUDGE_PLAYBOOK=pb/nope.md\n")
    assert not pid.exists()
