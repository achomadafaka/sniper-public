"""Labeller cron wrapper: runs the pinned labeller with the state root, and skips while a funded deploy
holds its lock, detected from /proc/locks without ever taking that lock."""
import fcntl
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "memecoin_desk_labels_cron.sh"


def _run(tmp_path):
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\necho "$SNIPER_STATE_ROOT $*" > "%s"\n' % (tmp_path / "ran"))
    fake.chmod(0o755)
    out = subprocess.run(["bash", str(WRAPPER)], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, SNIPER_STATE_ROOT=str(state), SNIPER_PYTHON=str(fake)))
    return state, out


def test_runs_the_pinned_labeller_once_with_the_state_root(tmp_path):
    state, out = _run(tmp_path)
    assert out.returncode == 0 and "labeller pass" in out.stdout
    root, script, flag = (tmp_path / "ran").read_text().split()
    assert root == str(state) and script == str(ROOT / "scripts" / "research" / "desk_u_ranker_labels.py")
    assert flag == "--once"


def test_skips_while_a_funded_deploy_holds_its_lock_and_never_takes_it(tmp_path):
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    with open(state / "data" / "memecoin_deploy.lock", "w") as deploy:      # the deploy's exclusive lock
        fcntl.flock(deploy, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, out = _run(tmp_path)
        assert "funded deploy in progress" in out.stdout and not (tmp_path / "ran").exists()
        with open(state / "data" / "memecoin_deploy.lock") as other:      # still exclusively ours
            try:
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
                raise AssertionError("the deploy lock was released or shared")
            except BlockingIOError:
                pass
    _, out = _run(tmp_path)                                               # released: the next pass runs
    assert "labeller pass" in out.stdout and (tmp_path / "ran").exists()
