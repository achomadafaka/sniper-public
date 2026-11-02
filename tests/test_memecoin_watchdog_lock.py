"""The watchdog must serialize itself without handing its lock to the runner it launches.

Two required behaviours, both from live incidents:

  1. 2026-08-25: cron and a deploy invoked the watchdog 2s apart, both read "not running", both
     launched, both failed the runner's internal lock, and the deploy rolled back.
  2. finding-watchdog-lock-inherited-by-worker-2026-08-12: a bare `exec 9>lock` leaks the
     descriptor into the launched worker, which then holds the WATCHDOG's lock for its whole life.
     Every later invocation loses the race and the watchdog is permanently inert while looking
     healthy.

The lock preamble is EXTRACTED VERBATIM from scripts/memecoin_watchdog.sh between its
BEGIN/END markers, so these tests cannot drift away from the shipped code the way a re-typed copy
would. Running the real watchdog is not an option here: it launches the funded runner.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import time

import pytest

WATCHDOG = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_watchdog.sh"


def _lock_preamble():
    text = WATCHDOG.read_text()
    start = text.index("# BEGIN watchdog-lock")
    end = text.index("# END watchdog-lock")
    return text[start:end]


def _harness(tmp_path, body):
    """A script carrying the REAL preamble plus a test body."""
    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -u\n"
        'SCRIPT="$(readlink -f "$0")"\n'
        + _lock_preamble()
        + "\n" + body + "\n")
    script.chmod(0o755)
    return script


def _env(tmp_path):
    env = dict(os.environ)
    env["MEMECOIN_WATCHDOG_LOCK"] = str(tmp_path / "watchdog.lock")
    # The lock loser now appends a line to the watchdog log; a test must never write the real one.
    env["MEMECOIN_WATCHDOG_LOG"] = str(tmp_path / "watchdog.log")
    env.pop("MEMECOIN_WATCHDOG_LOCK_HELD", None)
    return env


def test_the_preamble_is_present_and_uses_every_required_flag():
    pre = _lock_preamble()
    # -E 75 so the preamble can tell a lost race from the run's own result; the loser still
    # exits 0 to cron (asserted below by running it).
    assert "flock -n -E 75 -o" in pre, "needs non-blocking, distinct-conflict, close-before-exec"
    assert "MEMECOIN_WATCHDOG_LOCK_HELD" in pre, "needs a re-exec guard or it recurses forever"
    assert "memecoin_watchdog.lock" in pre
    # It must not USE either execution lock. Comments may name them; code may not.
    code = "\n".join(l for l in pre.splitlines() if not l.lstrip().startswith("#"))
    assert "MEMECOIN_RUN_LOCK" not in code, "must not serialize on the runner's own run lock"
    assert "sol-" not in code, "must not touch the wallet execution lock"


@pytest.mark.skipif(not subprocess.run(["which", "flock"], capture_output=True).stdout,
                    reason="flock not installed")
def test_a_second_invocation_exits_quietly_instead_of_queueing(tmp_path):
    """The loser must exit 0. Cron stacking a minute's worth of blocked watchdogs is its own
    outage, and a nonzero exit reads as a failure it did not cause."""
    marker = tmp_path / "ran"
    slow = _harness(tmp_path, f'echo run >> "{marker}"\nsleep 3\n')

    first = subprocess.Popen([str(slow)], env=_env(tmp_path),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.7)                       # first is inside its sleep, holding the lock
    second = subprocess.run([str(slow)], env=_env(tmp_path), capture_output=True, timeout=20)

    assert second.returncode == 0, "a conflicted invocation must not look like a failure"
    assert second.stdout == b"" and second.stderr == b"", "the loser must be silent"
    first.wait(timeout=20)
    assert marker.read_text().count("run") == 1, "exactly one invocation may proceed"


@pytest.mark.skipif(not subprocess.run(["which", "flock"], capture_output=True).stdout,
                    reason="flock not installed")
def test_the_launched_child_does_not_inherit_the_lock(tmp_path):
    """THE 2026-08-12 TRAP. After the watchdog exits, its long-lived child must not still be
    holding the lock — otherwise every later invocation loses forever and the watchdog is inert
    while every health surface says it is fine."""
    lock = tmp_path / "watchdog.lock"
    child_pid = tmp_path / "child.pid"
    launcher = _harness(tmp_path, (
        f'setsid nohup sleep 30 >/dev/null 2>&1 &\n'
        f'echo $! > "{child_pid}"\n'))

    subprocess.run([str(launcher)], env=_env(tmp_path), capture_output=True, timeout=20)
    pid = int(child_pid.read_text().strip())
    try:
        # The watchdog has exited; the child is still alive. A fresh invocation must WIN the lock.
        marker = tmp_path / "second_ran"
        prober = _harness(tmp_path, f'echo ok > "{marker}"\n')
        result = subprocess.run([str(prober)], env=_env(tmp_path), capture_output=True, timeout=20)
        assert result.returncode == 0
        assert marker.exists(), (
            "the surviving child is holding the watchdog lock — this is the inherited-fd trap, "
            "and the watchdog is now permanently inert")

        # Direct check too: nothing should hold the descriptor.
        holders = subprocess.run(["fuser", str(lock)], capture_output=True, timeout=10)
        assert str(pid) not in holders.stdout.decode().split(), "child still holds the lock fd"
    finally:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(not subprocess.run(["which", "flock"], capture_output=True).stdout,
                    reason="flock not installed")
def test_the_lock_is_released_when_the_watchdog_exits(tmp_path):
    fast = _harness(tmp_path, 'true\n')
    for _ in range(3):
        assert subprocess.run([str(fast)], env=_env(tmp_path), timeout=20).returncode == 0


def test_the_guard_prevents_infinite_re_exec(tmp_path):
    """With the guard already set the preamble must fall through, not exec flock again."""
    marker = tmp_path / "ran"
    script = _harness(tmp_path, f'echo run >> "{marker}"\n')
    env = _env(tmp_path)
    env["MEMECOIN_WATCHDOG_LOCK_HELD"] = "1"
    assert subprocess.run([str(script)], env=env, timeout=20).returncode == 0
    assert marker.read_text().count("run") == 1


@pytest.mark.skipif(not subprocess.run(["which", "flock"], capture_output=True).stdout,
                    reason="flock not installed")
def test_a_lost_race_leaves_one_line_at_most_every_five_minutes(tmp_path):
    """2026-09-24: a 4-minute release verification held the lock through a network outage and every
    cron minute in between exited with no trace, which read as a watchdog that logged nothing for
    36 minutes. The loser now says so -- in the watchdog log, never on stdout, never on the lock --
    and at most once per 5 minutes so a long run adds one line, not one per minute."""
    marker = tmp_path / "ran"
    slow = _harness(tmp_path, f'echo run >> "{marker}"\nsleep 3\n')
    log = tmp_path / "watchdog.log"

    first = subprocess.Popen([str(slow)], env=_env(tmp_path),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.7)
    losers = [subprocess.run([str(slow)], env=_env(tmp_path), capture_output=True, timeout=20)
              for _ in range(3)]
    first.wait(timeout=20)

    for loser in losers:
        assert loser.returncode == 0 and loser.stdout == b"" and loser.stderr == b""
    assert marker.read_text().count("run") == 1, "exactly one invocation may proceed"
    lines = log.read_text().splitlines() if log.exists() else []
    assert len(lines) == 1, lines
    assert "watchdog run skipped: a previous run still holds the lock" in lines[0]
