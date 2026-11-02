"""A manual-exit pass that stops making progress is unhealthy (Codex CORE-02, 2026-10-07).

ManualExitWorker.healthy() accepted a held pass lock whatever its age: a drain hung for 3,600 s read
healthy=True, busy=True, so the main loop's fallback (health-gated) never even looked. A pass now
records its start on the monotonic clock; a busy pass is healthy only inside
MEMECOIN_MANUAL_EXIT_BUSY_BUDGET_S (default 300 s: 2 intents per pass, each a serialized exit that
may wait the wallet lock and a ~90 s confirm). Past it the worker reports stalled. The lock still
excludes a second drain, so a fallback never overlaps the stuck pass. A negative age is never
healthy.
"""
import threading
from types import SimpleNamespace

from sniper.services import memecoin_manual_exit_intents as xi


def _worker(drain, clock):
    w = xi.ManualExitWorker(connect=lambda: None, observe=lambda: None, drain_fn=drain,
                            clock=lambda: clock[0])
    w.thread = SimpleNamespace(is_alive=lambda: True)
    w.last_pass = clock[0]
    return w


def _blocked_pass(worker, gate):
    t = threading.Thread(target=worker._pass, args=(None,), daemon=True)
    t.start()
    for _ in range(200):
        if worker.pass_lock.locked() and worker.pass_started is not None:
            break
        threading.Event().wait(0.005)
    return t


def test_a_short_active_pass_is_healthy():
    clock, gate = [100.0], threading.Event()
    w = _worker(lambda c: gate.wait(2), clock)
    t = _blocked_pass(w, gate)
    clock[0] = 130.0
    s = w.status()
    assert s["healthy"] and s["busy"] and not s["stalled"] and s["busy_age_s"] == 30.0
    gate.set(); t.join(1)


def test_a_pass_past_its_budget_is_stalled_and_status_never_waits(monkeypatch):
    """THE regression: a held lock read healthy forever."""
    monkeypatch.setenv("MEMECOIN_MANUAL_EXIT_BUSY_BUDGET_S", "300")
    clock, gate = [100.0], threading.Event()
    w = _worker(lambda c: gate.wait(2), clock)
    t = _blocked_pass(w, gate)
    clock[0] = 100.0 + 3600
    s = w.status()
    assert not s["healthy"] and s["stalled"] and s["busy"] and s["busy_age_s"] == 3600.0
    gate.set(); t.join(1)


def test_a_fallback_never_overlaps_a_stalled_pass():
    clock, gate, calls = [100.0], threading.Event(), []

    def drain(c):
        calls.append(c)
        gate.wait(2)
    w = _worker(drain, clock)
    t = _blocked_pass(w, gate)
    clock[0] = 100.0 + 3600
    assert w.fallback("main") == [] and len(calls) == 1
    gate.set(); t.join(1)


def test_a_completed_pass_clears_the_stall():
    clock, gate = [100.0], threading.Event()
    w = _worker(lambda c: gate.wait(2), clock)
    t = _blocked_pass(w, gate)
    clock[0] = 100.0 + 3600
    assert w.status()["stalled"]
    gate.set(); t.join(1)
    w.last_pass = clock[0]
    s = w.status()
    assert s["healthy"] and not s["stalled"] and s["busy_age_s"] is None


def test_a_negative_age_is_never_healthy():
    clock = [100.0]
    w = _worker(lambda c: None, clock)
    w.last_pass = 500.0                      # a clock that went backwards
    assert not w.healthy()
    w.last_pass, w.pass_started = 100.0, 400.0
    w.pass_lock.acquire()
    try:
        assert not w.healthy() and w.status()["stalled"]
    finally:
        w.pass_lock.release()
        w.pass_started = None


def test_a_typed_error_still_makes_it_unhealthy():
    clock = [100.0]
    w = _worker(lambda c: None, clock)
    w.error = "OSError"
    assert not w.healthy() and w.status()["error"] == "OSError"
