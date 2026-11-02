"""Execute the pinned watchdog with harmless workers and fake /proc records."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/memecoin_desk_intake_assembler_watchdog.sh'
NAME = 'memecoin_desk_intake_assembler.py'


@pytest.fixture
def h(tmp_path):
    state = tmp_path / 'state'
    release = state / '.runtime/memecoin/releases' / ('a' * 40)
    scripts = release / 'scripts'
    scripts.mkdir(parents=True)
    proc = tmp_path / 'proc'
    proc.mkdir()
    assert SOURCE.is_file(), 'intake assembler watchdog missing'
    watchdog = scripts / SOURCE.name
    shutil.copyfile(SOURCE, watchdog)
    (scripts / 'lib').mkdir()
    shutil.copyfile(ROOT / 'scripts/lib/desk_proc_match.sh', scripts / 'lib/desk_proc_match.sh')
    worker = scripts / NAME
    worker.write_text('import json,os,sys\nfrom pathlib import Path\n'
                      'Path(os.environ["SNIPER_STATE_ROOT"],"launched.json").write_text(json.dumps(sys.argv))\n')
    env = {**os.environ, 'SNIPER_STATE_ROOT': str(state), 'SNIPER_PYTHON': sys.executable,
           'DESK_INTAKE_ASSEMBLER_WATCHDOG_PROC_ROOT': str(proc)}
    def process(pid=101, *, path=worker, root=state, interpreter=sys.executable, readable=True):
        p = proc / str(pid)
        p.mkdir()
        (p / 'cmdline').write_bytes(os.fsencode(interpreter) + b'\0' + os.fsencode(path) + b'\0--loop\0')
        if readable:
            (p / 'environ').write_bytes(os.fsencode('SNIPER_STATE_ROOT=' + str(root)) + b'\0')
        return p
    def invoke():
        return subprocess.run(['/bin/bash', str(watchdog)], env=env, text=True, capture_output=True, timeout=15)
    def launched():
        until = time.monotonic() + 2
        while time.monotonic() < until and not (state / 'launched.json').exists():
            time.sleep(.01)
        return (state / 'launched.json').is_file()
    return dict(state=state, release=release, scripts=scripts, proc=proc, env=env, worker=worker,
                watchdog=watchdog, process=process, invoke=invoke, launched=launched)


def test_launch_is_exact_pinned_worker_state_and_loop(h):
    r = h['invoke']()
    assert r.returncode == 0, r.stderr
    assert h['launched']()
    assert json.loads((h['state'] / 'launched.json').read_text()) == [
        str(h['worker']), '--desk-db', str(h['state'] / 'data/memecoin_desk.db'), '--loop']


def test_existing_exact_process_prevents_second_launch(h):
    h['process']()
    assert h['invoke']().returncode == 0
    assert not (h['state'] / 'launched.json').exists()


@pytest.mark.parametrize('change', ['other_state', 'different_script', 'not_python', 'unknown_state'])
def test_unrelated_process_or_untrusted_pid_file_is_never_signalled(h, change):
    kwargs = {'other_state': {'root': h['state'].parent / 'other'},
              'different_script': {'path': h['scripts'] / 'other.py'},
              'not_python': {'interpreter': '/bin/grep'},
              'unknown_state': {'readable': False}}[change]
    p = h['process'](**kwargs)
    saved = (p / 'cmdline').read_bytes()
    data = h['state'] / 'data/desk/assembler_intake'
    data.mkdir(parents=True)
    (data / 'assembler.pid').write_text('101\n')
    r = h['invoke']()
    assert r.returncode == 0, r.stderr
    assert h['launched']()
    assert (p / 'cmdline').read_bytes() == saved


def test_superseded_worker_must_exit_before_replacement(h):
    old = h['state'] / '.runtime/memecoin/releases' / ('b' * 40) / 'scripts' / NAME
    h['process'](path=old)
    # The shared helper refuses to signal fake /proc pids. Refusal must prevent overlap.
    r = h['invoke']()
    assert r.returncode != 0 or 'no replacement' in r.stderr
    assert not (h['state'] / 'launched.json').exists()


def test_mutable_checkout_and_state_inside_release_are_refused(h, tmp_path):
    checkout = tmp_path / 'mutable/scripts'
    checkout.mkdir(parents=True)
    shutil.copyfile(SOURCE, checkout / SOURCE.name)
    result = subprocess.run(['/bin/bash', str(checkout / SOURCE.name)], env=h['env'], capture_output=True, text=True)
    assert result.returncode != 0 and not (h['state'] / 'launched.json').exists()
    h['env']['SNIPER_STATE_ROOT'] = str(h['release'] / 'data')
    assert h['invoke']().returncode != 0


def test_watchdog_flock_is_not_bypassed(h):
    data = h['state'] / 'data/desk/assembler_intake'
    data.mkdir(parents=True)
    with (data / 'watchdog.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert h['invoke']().returncode == 0
        assert not (h['state'] / 'launched.json').exists()


def test_replacement_starts_only_after_fake_old_worker_has_stopped(h):
    old = h['state'] / '.runtime/memecoin/releases' / ('b' * 40) / 'scripts' / NAME
    p = h['process'](path=old)
    helper = h['scripts'] / 'lib/desk_proc_match.sh'
    with helper.open('a') as stream:
        stream.write('\ndesk_proc_term() { mv -- "$1/$2/cmdline" "$1/$2/stopped"; }\n')
    result = h['invoke']()
    assert result.returncode == 0, result.stderr
    assert (p / 'stopped').exists() and not (p / 'cmdline').exists()
    assert h['launched']()


def test_pidfd_replacement_gracefully_stops_only_its_own_fixture_child(h):
    old = h['state'] / '.runtime/memecoin/releases' / ('b' * 40) / 'scripts' / NAME
    old.parent.mkdir(parents=True)
    ready = h['state'] / 'old-ready'
    stopped = h['state'] / 'old-stopped'
    old.write_text('import os,signal,time\nfrom pathlib import Path\n'
                   'root=Path(os.environ["SNIPER_STATE_ROOT"])\n'
                   'def stop(*args):\n    (root/"old-stopped").write_text("TERM")\n    raise SystemExit(0)\n'
                   'signal.signal(signal.SIGTERM,stop)\n(root/"old-ready").write_text("ready")\n'
                   'while True: time.sleep(.1)\n')
    child = subprocess.Popen([sys.executable, str(old)], env=h['env'])
    try:
        until = time.monotonic() + 2
        while time.monotonic() < until and not ready.exists():
            time.sleep(.01)
        assert ready.exists()
        h['env']['DESK_INTAKE_ASSEMBLER_WATCHDOG_PROC_ROOT'] = '/proc'
        result = h['invoke']()
        assert result.returncode == 0, result.stderr
        assert child.wait(timeout=3) == 0 and stopped.read_text() == 'TERM'
        assert h['launched']()
    finally:
        if child.poll() is None:
            child.terminate()  # only this test's Popen child; never a service or a discovered PID
            child.wait(timeout=3)
