"""A stale restart waits for the runner to exit, up to a bound, before calling it a hang.

2026-09-24 21:19Z: the runner's SIGTERM handler landed 17.5 s after the kill, because its main
thread was blocked in network I/O during a Wi-Fi outage (0.2-1.0 s otherwise). The watchdog checked
after 2 s, logged "did not stop" -- the only time that line was ever written -- and exited instead
of relaunching. The branch is extracted from the shipped script, as the stall-dump test does.
"""
import subprocess
from pathlib import Path


def _branch():
    source = (Path(__file__).resolve().parents[1] / "scripts/memecoin_watchdog.sh").read_text()
    start = source.index('  if [ "$REASON" = "stale" ]')
    end = source.index('  elif [ "$REASON" = "auth_dead" ]', start)
    return source[start:end] + "\n  fi\n  echo REACHED_LAUNCH\n"


def _run(tmp_path, exits_after, slow_s=0):
    """`kill -0` reports the runner alive for `exits_after` polls, then gone. sleep is a no-op."""
    count = tmp_path / "polls"
    count.write_text("0")
    script = f'''
REASON=sweep_stale
PINNED_PIDS=123
log() {{ printf 'LOG %s\\n' "$*"; }}
alert() {{ command sleep {slow_s}; printf 'ALERT %s\\n' "$*" >> "{tmp_path / 'alerts'}"; }}
escalate_failure() {{ printf 'ESCALATE %s\\n' "$*"; }}
sleep() {{ :; }}
kill() {{
  if [ "$1" = "-0" ]; then
    n=$(( $(cat "{count}") + 1 )); echo "$n" > "{count}"
    [ "$n" -le {exits_after} ] && return 0 || return 1
  fi
  return 0
}}
t0=$(date +%s%N)
''' + _branch() + f'''
echo "ELAPSED_MS $(( ($(date +%s%N) - t0) / 1000000 ))"
wait; cat "{tmp_path / 'alerts'}" 2>/dev/null; true
'''
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


def test_a_runner_that_exits_within_the_bound_is_restarted_not_called_a_hang(tmp_path):
    run = _run(tmp_path, exits_after=17)           # the 21:19Z case
    assert "did not stop" not in run.stdout, run.stdout
    assert "LOG runner stopped 17s after stale restart request" in run.stdout
    assert "ALERT memecoin runner restarted by watchdog (sweep_stale)" in run.stdout
    assert "REACHED_LAUNCH" in run.stdout and run.returncode == 0


def test_a_runner_that_never_exits_is_reported_after_the_bound_and_not_killed(tmp_path):
    run = _run(tmp_path, exits_after=10_000)
    assert "LOG runner did not stop within 30s after stale restart request" in run.stdout
    assert "ESCALATE runner did not stop within 30s" in run.stdout
    assert "REACHED_LAUNCH" not in run.stdout and run.returncode == 1
    # Code only: the branch's own comment says "No SIGKILL", and a guard that reads its prose
    # fails on the sentence that documents the rule.
    code = "\n".join(line for line in _branch().splitlines() if not line.lstrip().startswith("#"))
    assert not any(tok in code for tok in ("kill -9", "kill -KILL", "kill -SIGKILL", "-s KILL")), \
        "no SIGKILL on a funded runner"


def test_a_prompt_exit_does_not_wait_or_log(tmp_path):
    run = _run(tmp_path, exits_after=0)
    assert "did not stop" not in run.stdout and "runner stopped" not in run.stdout
    assert "REACHED_LAUNCH" in run.stdout


def test_a_slow_telegram_never_delays_the_restart(tmp_path):
    """Gamma LOW / Alpha 2026-09-29: the "runner restarted" page is sent in the background, so a
    10 s send leaves the path to the relaunch unchanged."""
    run = _run(tmp_path, exits_after=2, slow_s=10)
    assert "REACHED_LAUNCH" in run.stdout
    elapsed = int(run.stdout.split("ELAPSED_MS ")[1].split()[0])
    assert elapsed < 2000, run.stdout
    assert "ALERT memecoin runner restarted by watchdog (sweep_stale)" in run.stdout
