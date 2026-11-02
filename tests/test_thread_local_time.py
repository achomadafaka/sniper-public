"""`patch_local` fakes the process-wide time module for the patching thread only (2026-09-29)."""
import threading
import time

from thread_local_time import patch_local


def test_the_fake_answers_this_thread_and_the_real_clock_answers_others(monkeypatch):
    clock = iter([1.0, 2.0])                     # finite: a foreign read would exhaust it
    patch_local(monkeypatch, "monotonic", lambda: next(clock))
    seen = []
    t = threading.Thread(target=lambda: seen.extend(time.monotonic() for _ in range(1000)))
    t.start()
    t.join(5.0)
    assert time.monotonic() == 1.0 and time.monotonic() == 2.0, "the test's own reads are untouched"
    assert len(seen) == 1000 and all(v > 2.0 for v in seen), "other threads read the real clock"


def test_a_foreign_sleep_is_not_recorded(monkeypatch):
    sleeps = []
    patch_local(monkeypatch, "sleep", sleeps.append)
    t = threading.Thread(target=lambda: time.sleep(0.01))
    t.start()
    t.join(5.0)
    time.sleep(7.0)
    assert sleeps == [7.0]
