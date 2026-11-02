"""A single-flight watchdog must not hand its own lock to the worker it starts.

Measured on 2026-08-12: `data/memecoin_sol_tape.watchdog.lock` was held by the WORKER (pid 12539),
not by any watchdog. `exec 9>lock` opens the descriptor in the watchdog shell, and a background
`setsid nohup ... &` child inherits it, so the flock survives for the worker's whole life. Every
later cron tick then failed `flock -n 9` and exited 0 before running a single check. The supervisor
looked healthy and supervised nothing: the tape worker sat on release a30faaea across three deploys
and no restart could ever be triggered while it stayed alive.

Two invariants are protected here, and they are one change, not two:

1. The launch closes the lock descriptor (`9>&-`).
2. Discovery is release-INDEPENDENT while the launch stays release-PINNED. Fixing (1) alone would
   EXPOSE the double-start hazard, because a discovery match built from the release path stops
   recognizing the running worker the moment a deploy repoints `current`.
"""
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# (watchdog, worker script name) — every cron-supervised single-flight worker in the memecoin family.
WATCHDOGS = [
    ("scripts/memecoin_sol_tape_watchdog.sh", "memecoin_sol_tape_worker.py"),
    ("scripts/memecoin_exit_state_watchdog.sh", "memecoin_exit_state_worker.py"),
    ("scripts/memecoin_opportunity_watchdog.sh", "memecoin_opportunity_worker.py"),
    ("scripts/memecoin_v6_paper_watchdog.sh", "memecoin_v6_paper_worker.py"),
    ("scripts/memecoin_radar_watchdog.sh", "memecoin_discovery_worker.py"),
]


def _launch_lines(script):
    # Join backslash continuations first: a launch split across lines puts the redirections on the
    # LAST line, so a naive per-line scan reads the first half and calls a correct script broken.
    joined = re.sub(r"\\\n\s*", " ", script)
    return [line for line in joined.splitlines()
            if "setsid" in line or ("nohup" in line and "&" in line)]


@pytest.mark.parametrize("path,worker", WATCHDOGS)
def test_the_launched_worker_does_not_inherit_the_single_flight_lock(path, worker):
    script = Path(path).read_text()
    fd = re.search(r"exec (\d+)>", script)
    assert fd, f"{path}: expected an `exec N>` single-flight lock"
    launches = _launch_lines(script)
    assert launches, f"{path}: expected a background launch"
    closer = f"{fd.group(1)}>&-"
    for line in launches:
        assert closer in line, (
            f"{path}: launch does not close the lock descriptor ({closer}); the worker would hold "
            f"the watchdog's flock for life and silently retire every later cron tick.\n  {line.strip()}")


@pytest.mark.parametrize("path,worker", WATCHDOGS)
def test_discovery_is_release_independent_so_a_deploy_cannot_double_start(path, worker):
    script = Path(path).read_text()
    match = re.search(r'^MATCH="([^"]*)"', script, re.MULTILINE)
    assert match, f"{path}: expected a MATCH used for process discovery"
    value = match.group(1)
    assert "$" not in value, (
        f"{path}: MATCH={value!r} interpolates a release root. After a deploy repoints `current` it "
        f"stops matching the running worker, and the next cron tick starts a SECOND one.")
    assert value == f"scripts/{worker}", f"{path}: MATCH should be scripts/{worker}, got {value!r}"


@pytest.mark.parametrize("path,worker", WATCHDOGS)
def test_the_launch_itself_stays_release_pinned(path, worker):
    """Release-independent DISCOVERY must not leak into the launch — that would run the repo copy."""
    script = Path(path).read_text()
    launch = re.search(r'^(?:LAUNCH=\(|WORKER=|LAUNCH=")(.*)$', script, re.MULTILINE)
    assert launch, f"{path}: expected a pinned LAUNCH/WORKER definition"
    pinned = any(
        root in launch.group(1) for root in ("$CODE_ROOT", "$RUNTIME_ROOT", "$ROOT"))
    # exit_state deliberately launches from the state root by relative path; it has no pinned release.
    if "exit_state" not in path:
        assert pinned, f"{path}: launch must name the pinned release root, got {launch.group(1)!r}"


def test_a_child_started_with_the_closer_really_releases_the_lock(tmp_path):
    """Behavioural check on the actual shell mechanics, not just the text of the scripts."""
    lock = tmp_path / "wd.lock"
    script = tmp_path / "wd.sh"

    def run(closer):
        # The child is spawned with `&` and the parent exits WITHOUT waiting, so probing straight
        # after the parent returns races the child's fork/exec: with no closer the child may not
        # have inherited fd 9 yet (probe reads FREE), and with `9>&-` it may not have closed it yet
        # (probe reads HELD). Both directions flake, and under load they do — this test failed a
        # funded deploy on 2026-08-13 while passing locally and under 8-way parallelism.
        # Synchronise on the child instead: it announces itself only after its fd table is final,
        # because the redirection is applied by the shell BEFORE the command runs.
        ready = tmp_path / f"ready{closer!r}".replace("/", "_").replace("'", "")
        ready.unlink(missing_ok=True)
        script.write_text(textwrap.dedent(f"""
            exec 9>"{lock}" || exit 3
            flock -n 9 || exit 4
            setsid nohup bash -c 'touch "$0"; exec sleep 30' "{ready}" \\
                </dev/null >/dev/null 2>&1 {closer} &
            echo $!
        """))
        started = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        child = started.stdout.strip()
        try:
            deadline = time.monotonic() + 10.0
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "child never started; the probe below would be meaningless"
            # The watchdog shell has exited (subprocess.run waited for it) and the child's fd table
            # is settled, so exactly one of them can hold the lock and the answer is deterministic.
            probe = subprocess.run(
                ["bash", "-c", f'exec 9>"{lock}"; flock -n 9 && echo FREE || echo HELD'],
                capture_output=True, text=True)
            return probe.stdout.strip()
        finally:
            if child.isdigit():
                subprocess.run(["kill", child], capture_output=True)

    assert run("") == "HELD", "inheriting the descriptor should hold the lock (the defect)"
    assert run("9>&-") == "FREE", "closing the descriptor must release the lock for the next tick"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
