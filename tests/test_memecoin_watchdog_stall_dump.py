"""A stale restart preserves the blocked call before the process stops."""
import subprocess
from pathlib import Path


def test_stale_restart_requests_thread_dump_before_termination():
    source = (Path(__file__).resolve().parents[1] / 'scripts/memecoin_watchdog.sh').read_text()
    start = source.index('  if [ "$REASON" = "stale" ]')
    end = source.index('  elif [ "$REASON" = "auth_dead" ]', start)
    branch = source[start:end] + '\n  fi\n'
    script = '''
REASON=sweep_stale
PINNED_PIDS=123
log() { :; }
alert() { :; }
escalate_failure() { :; }
sleep() { :; }
kill() { [ "$1" = "-0" ] && return 1; printf '%s\\n' "$*"; }
''' + branch
    run = subprocess.run(['bash', '-c', script], text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ['-USR2 123', '123']
