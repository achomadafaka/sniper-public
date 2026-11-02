"""A long hold of the funded exit lock leaves a timestamped stack of its holder in runner.log.

2026-09-27 12:57:17-12:57:56Z: the fast lane held _funded_exit_lock for about 39 s on position
66015 while the funded Solana pool failed every endpoint. The slow sweep logged only that the lane
"holds the exit lock", no thread dump existed, and the call it waited on had to be inferred. The
lock now remembers its holder, and a watcher logs that holder's stack once per episode past a
threshold, one stamped line per stack line (a bare logger line is dropped by a timestamp-window
read of runner.log).
"""
import re
import threading
import time

import pytest

from sniper.services import memecoin_fastlane as F

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def _stamped_log():
    lines = []

    def log(msg):
        from datetime import datetime, timezone
        lines.append("%s %s" % (datetime.now(timezone.utc).isoformat(), msg))
    return lines, log


# --- the lock keeps Lock semantics ----------------------------------------------------------------

def test_the_watched_lock_acquires_releases_and_times_out_like_a_lock():
    lock = F.WatchedLock()
    assert lock.acquire() is True and lock.locked()
    assert lock.holder()[0] == threading.get_ident()
    other = []
    t = threading.Thread(target=lambda: other.append(lock.acquire(timeout=0.05)))
    t.start()
    t.join()
    assert other == [False]                      # a bounded acquire still fails while held
    lock.release()
    assert not lock.locked() and lock.holder() is None


def test_a_with_block_releases_even_when_its_body_raises():
    lock = F.WatchedLock()
    with pytest.raises(RuntimeError):
        with lock:
            assert lock.holder() is not None
            raise RuntimeError("boom")
    assert not lock.locked() and lock.holder() is None


def test_each_acquisition_is_a_new_episode():
    lock = F.WatchedLock()
    with lock:
        first = lock.holder()[3]
    with lock:
        assert lock.holder()[3] == first + 1


# --- the watcher --------------------------------------------------------------------------------

def _hold(lock, seconds, started):
    def exit_sweep_that_waits_on_the_network():
        with lock:
            started.set()
            time.sleep(seconds)
    t = threading.Thread(target=exit_sweep_that_waits_on_the_network, name="fastlane-test")
    t.start()
    return t


def test_a_hold_past_the_threshold_logs_the_holders_stack_once_every_line_stamped():
    lock, started, state = F.WatchedLock(), threading.Event(), {}
    lines, log = _stamped_log()
    t = _hold(lock, 0.6, started)
    started.wait(1)
    time.sleep(0.3)
    F.check_exit_lock(lock, hold_s=0.2, log=log, state=state)
    F.check_exit_lock(lock, hold_s=0.2, log=log, state=state)       # same episode: no second dump
    t.join()
    assert lines, "no dump for a 0.3 s hold past a 0.2 s threshold"
    assert sum("exit lock held" in line for line in lines) == 1, lines   # once per episode
    assert all(STAMP.match(line) for line in lines), lines
    assert "exit lock held" in lines[0] and "fastlane-test" in lines[0]
    assert any("exit_sweep_that_waits_on_the_network" in line for line in lines), lines
    n = len(lines)
    F.check_exit_lock(lock, hold_s=0.2, log=log, state=state)
    assert len(lines) == n                      # released: nothing more


def test_a_short_hold_logs_nothing():
    lock, started = F.WatchedLock(), threading.Event()
    lines, log = _stamped_log()
    t = _hold(lock, 0.3, started)
    started.wait(1)
    F.check_exit_lock(lock, hold_s=5.0, log=log, state={})
    t.join()
    assert lines == []


def test_a_new_episode_dumps_again():
    lock = F.WatchedLock()
    lines, log = _stamped_log()
    state = {}
    for _ in range(2):
        started = threading.Event()
        t = _hold(lock, 0.4, started)
        started.wait(1)
        time.sleep(0.2)
        F.check_exit_lock(lock, hold_s=0.1, log=log, state=state)
        t.join()
    assert sum("exit lock held" in line for line in lines) == 2


def test_the_runner_builds_the_exit_lock_as_a_watched_lock():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "_funded_exit_lock = WatchedLock()" in src
    assert "target=watch_exit_lock" in src and "MEMECOIN_EXIT_LOCK_DUMP_S" in src
