"""A deploy cuts every release-pinned worker over, the Solana cohort stream included (Alpha, 2026-10-09).

The deploy of e1beb1c23 left the cohort worker on 7fd644c1. Its watchdog, like the sol tape one,
refuses to cut a worker over ("the deploy's job"), and deploy_memecoin_runtime.sh only knew the tape
worker, so the worker ran on old code until it was SIGTERMed by hand. Both workers now go through one
`stop_stale_worker`: stop by PIDFILE only (never by pattern), only a process running the named script
from ANOTHER release, after the entry gate's CLEAR and the rollback trap; the watchdog relaunches it
from `current`.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "deploy_memecoin_runtime.sh").read_text()
COHORT = 'stop_stale_worker "sol cohort worker" "$STATE_ROOT/data/memecoin_sol_cohort.pid" ' \
         '"scripts/memecoin_sol_cohort_stream.py"'
TAPE = 'stop_stale_worker "sol tape worker" "$STATE_ROOT/data/memecoin_sol_tape.pid" ' \
       '"scripts/memecoin_sol_tape_worker.py"'


def test_deploy_validation_obeys_the_funded_host_worker_budget():
    assert '"$PY" -m pytest -n 2 "${MEMECOIN_TESTS[@]}" -q' in SCRIPT


def test_both_workers_are_cut_over_after_the_clear_and_the_rollback_trap():
    clear = SCRIPT.index('clear_deploy_entry_gate || fail "could not restore entry admission"')
    trap_off = SCRIPT.index("trap - EXIT", clear)
    assert trap_off < SCRIPT.index(TAPE) and trap_off < SCRIPT.index(COHORT)


def _function():
    """worker_running and stop_stale_worker, as the script defines them."""
    start = SCRIPT.index("worker_running() {")
    stop = SCRIPT.index("stop_stale_worker() {", start)
    return SCRIPT[start:SCRIPT.index("\n}\n", stop) + 3]


def _worker(tmp_path, release):
    script = tmp_path / release / "scripts" / "memecoin_sol_cohort_stream.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import time\ntime.sleep(60)\n")
    # DETACHED, as the watchdog launches it (setsid nohup ... &): a child of pytest would linger as a
    # zombie after the kill, and `kill -0` would still find it.
    pid = int(subprocess.run(["bash", "-c", 'setsid nohup "%s" "%s" >/dev/null 2>&1 </dev/null & echo $!'
                              % (sys.executable, script)], capture_output=True, text=True,
                             check=True).stdout.strip())
    time.sleep(0.3)
    pidfile = tmp_path / "cohort.pid"
    pidfile.write_text(str(pid))
    return _Proc(pid), pidfile


class _Proc:
    def __init__(self, pid):
        self.pid = pid

    def alive(self):
        """Running, not merely unreaped: a zombie answers kill -0 until its reaper collects it."""
        try:
            stat = Path("/proc/%d/stat" % self.pid).read_text()
        except OSError:
            return False
        return stat.rsplit(") ", 1)[1].split()[0] != "Z"

    def kill(self):
        try:
            os.kill(self.pid, 9)
        except ProcessLookupError:
            pass


def _run(tmp_path, pidfile, release):
    body = (_function() + '\nRELEASE="%s"\nSHA=newsha\nstop_stale_worker "sol cohort worker" "%s" '
            '"scripts/memecoin_sol_cohort_stream.py"\n' % (tmp_path / release, pidfile))
    return subprocess.run(["bash", "-euo", "pipefail", "-c", body], capture_output=True, text=True)


def test_a_worker_on_an_old_release_is_stopped_for_the_watchdog_to_relaunch(tmp_path):
    proc, pidfile = _worker(tmp_path, "oldrel")
    try:
        out = _run(tmp_path, pidfile, "newrel")
        assert out.returncode == 0, out.stderr
        assert "sol cohort worker pid %d stopped; watchdog will relaunch it on newsha" % proc.pid \
            in out.stdout
        assert not proc.alive()
    finally:
        proc.kill()


def test_a_worker_already_on_the_release_is_left_running(tmp_path):
    proc, pidfile = _worker(tmp_path, "newrel")
    try:
        out = _run(tmp_path, pidfile, "newrel")
        assert out.returncode == 0 and "already on newsha; not restarting" in out.stdout
        assert proc.alive()
    finally:
        proc.kill()


def test_no_pidfile_or_a_dead_pid_is_a_quiet_no_op(tmp_path):
    out = _run(tmp_path, tmp_path / "missing.pid", "newrel")
    assert out.returncode == 0 and out.stdout == "" and out.stderr == ""


def test_a_killed_worker_waiting_to_be_reaped_counts_as_stopped(tmp_path):
    """Gamma's full run: under load the detached worker's reaper (the user's systemd) lagged, the
    killed worker sat as a zombie, and kill -0 kept answering for the whole wait: "did not stop".
    Here its parent never reaps it, so it stays a zombie for the whole call."""
    script = tmp_path / "oldrel" / "scripts" / "memecoin_sol_cohort_stream.py"
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(60)\n")
    parent = subprocess.Popen(
        [sys.executable, "-c", "import subprocess, sys, time\n"
         "child = subprocess.Popen([sys.executable, sys.argv[1]])\n"
         "print(child.pid, flush=True)\ntime.sleep(60)\n", str(script)],
        stdout=subprocess.PIPE, text=True)
    try:
        pid = int(parent.stdout.readline())
        time.sleep(0.3)
        pidfile = tmp_path / "cohort.pid"
        pidfile.write_text(str(pid))
        started = time.monotonic()
        out = _run(tmp_path, pidfile, "newrel")
        assert out.returncode == 0, out.stderr
        assert "sol cohort worker pid %d stopped; watchdog will relaunch it on newsha" % pid in out.stdout
        assert "did not stop" not in out.stderr and time.monotonic() - started < 5.0
        assert not _Proc(pid).alive()
    finally:
        parent.kill()
        parent.wait()
