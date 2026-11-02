"""The Solana watcher names its own death (2026-09-29).

A SIGTERM used to end it with no line: on 2026-09-28/29 it died silently again and again, and the
killer (a test running the whole watchdog with a tmp STATE_ROOT) had to be found from timestamps.
Now a signal is logged with its sender before the process exits 128+n, and a return or an exception
out of run() is logged too. The watcher runs here in a child interpreter with run() replaced, so no
socket, key or ledger is touched.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts/research/onchain_wallet_watch.py"

CHILD = """
import asyncio, importlib.util as u, os, sys
s = u.spec_from_file_location('w', %(src)r); w = u.module_from_spec(s); s.loader.exec_module(w)
assert len(os.listdir('/proc/self/task')) == 1, 'a thread started before the mask'
async def fake_run(duration_s=None, override_addrs=None):
    print('ready', flush=True)
    mode = %(mode)r
    if mode == 'raise':
        raise RuntimeError('boom')
    if mode == 'broken_stdout':
        class Broken:
            def write(self, *a):
                raise BrokenPipeError(32, 'Broken pipe')
            flush = write
        sys.stdout = Broken()
    if mode in ('hold', 'broken_stdout'):
        await asyncio.sleep(60)
w.run = fake_run
sys.argv = ['onchain_wallet_watch.py', '--duration', '86400']
w.main()
"""


def _child(tmp_path, mode):
    env = {"PATH": "/usr/bin:/bin", "SNIPER_STATE_ROOT": str(tmp_path),
           "DB_PATH": str(tmp_path / "ledger.db"), "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-c", CHILD % {"src": str(SRC), "mode": mode}],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.mark.parametrize("sig, code", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_is_logged_with_its_sender_then_exits_as_the_default_would(tmp_path, sig, code):
    child = _child(tmp_path, "hold")
    try:
        assert child.stdout.readline().strip() == "ready"
        # The sender stays alive for a second, so its /proc entry is there to be read; the
        # trailing `true` stops bash exec-ing `sleep` over its own command line.
        sender = subprocess.Popen(["bash", "-c", "# exit-line-sender-marker\n"
                                   f"kill -{sig.name[3:]} {child.pid}; sleep 1; true"])
        t0 = time.monotonic()
        rc = child.wait(timeout=10)
        elapsed = time.monotonic() - t0
        out = child.stdout.read()
        sender.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    assert rc == code, out
    line = next(l for l in out.splitlines() if " exit: " in l)
    assert f"exit: {sig.name} from {sender.pid} [" in line, line
    assert "exit-line-sender-marker" in line and f" < {os.getpid()} [" in line, line
    assert line.endswith(f"uid {os.getuid()}"), line
    assert elapsed < 1.0, f"the exit took {elapsed:.3f}s; a deploy waits on it"


def test_a_log_step_that_raises_still_exits(tmp_path):
    """Gamma H1: with TERM blocked everywhere, a print that raised used to kill the waiter thread
    and leave the watcher unkillable by TERM (a deploy's stop failed silently)."""
    child = _child(tmp_path, "broken_stdout")
    try:
        assert child.stdout.readline().strip() == "ready"
        os.kill(child.pid, signal.SIGTERM)
        rc = child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert rc == 143


def test_a_return_from_run_is_logged(tmp_path):
    child = _child(tmp_path, "return")
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0, out
    assert "exit: run returned (--duration 86400.0)" in out, out


def test_an_exception_out_of_run_is_logged_before_the_traceback(tmp_path):
    child = _child(tmp_path, "raise")
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 1, out
    assert out.index("exit: RuntimeError: boom") < out.index("Traceback"), out


def test_the_sender_chain_reads_the_kernel_and_a_gone_process():
    import importlib.util as u
    s = u.spec_from_file_location("oww_exit", SRC)
    w = u.module_from_spec(s)
    s.loader.exec_module(w)
    assert w._sender_chain(0) == "the kernel"
    assert w._sender_chain(2 ** 22 + 7) == f"{2 ** 22 + 7} [gone]"
