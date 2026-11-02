"""A runner death must name itself in the log.

On 2026-08-23 the watchdog relaunched the funded runner 13 times. Every death was silent: the
runner had no SIGTERM handler and never enabled faulthandler, so neither an external signal nor a
native crash wrote a line, and the watchdog log could only say "pinned runner not running".
"""
import os
import signal
import subprocess
import sys

RUNNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "memecoin_runner_loop.py")


def test_sigterm_is_named_in_the_log_and_keeps_the_real_exit_status():
    prog = (
        "import sys, os, signal, runpy, importlib.util;"
        "spec = importlib.util.spec_from_file_location('r', %r);"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod);"
        "mod.register_death_logger();"
        "os.kill(os.getpid(), signal.SIGTERM)"
    ) % RUNNER
    proc = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=120)
    assert "received SIGTERM" in proc.stdout, proc.stdout[-2000:] + proc.stderr[-2000:]
    # default disposition restored -> the parent still sees a signal death, not a clean exit 0
    assert proc.returncode == -signal.SIGTERM, proc.returncode


def test_faulthandler_is_enabled_so_a_native_crash_leaves_a_traceback():
    prog = (
        "import sys, faulthandler, importlib.util;"
        "spec = importlib.util.spec_from_file_location('r', %r);"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod);"
        "mod.register_death_logger();"
        "print('enabled', faulthandler.is_enabled())"
    ) % RUNNER
    proc = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=120)
    assert "enabled True" in proc.stdout, proc.stdout[-2000:] + proc.stderr[-2000:]
