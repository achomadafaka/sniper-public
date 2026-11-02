import ast

import pytest

from _source_guard import code_of

from sniper.services.memecoin_fastlane import (
    FastlaneState,
    ensure_fastlane_thread,
    run_fastlane_exit_sweep,
    run_fastlane_worker_cycle,
    run_slow_exit_sweep,
)


class _GuardLock:
    """Models `threading.Lock` closely enough for both sweeps.

    It grew `acquire`/`release` on 2026-09-09: the slow sweep stopped using `with` and now acquires
    with a TIMEOUT, so a stub that only spoke the context-manager protocol no longer modelled the
    object under test. `acquire(timeout=...)` returns False when already held, exactly as the real
    lock does, which is what lets a test assert the contended path without a second thread.
    """

    def __init__(self, held=False):
        self.held = held

    def acquire(self, timeout=-1):
        if self.held:
            return False
        self.held = True
        return True

    def release(self):
        self.held = False

    def __enter__(self):
        assert self.acquire(), "lock already held"

    def __exit__(self, *_args):
        self.release()


def test_worker_cycle_recovers_after_setup_failure():
    now = [10.0]
    state = FastlaneState(clock=lambda: now[0])
    setup_calls = []

    def setup():
        setup_calls.append(True)
        if len(setup_calls) == 1:
            raise RuntimeError("secret provider text")
        return {"connection": "ready"}

    resource, ok = run_fastlane_worker_cycle(
        None, setup_fn=setup, pass_fn=lambda _resource: None, state=state)
    assert resource is None and ok is False
    failed = state.snapshot(stale_after_s=5.0)
    assert failed["status"] == "failed"
    assert failed["failure"] == "RuntimeError"
    assert "secret" not in repr(failed)

    now[0] = 11.0
    resource, ok = run_fastlane_worker_cycle(
        resource, setup_fn=setup, pass_fn=lambda _resource: None, state=state)
    assert resource == {"connection": "ready"} and ok is True
    assert state.snapshot(stale_after_s=5.0)["status"] == "healthy"


def test_slow_sweep_runs_only_when_fastlane_is_not_healthy():
    now = [100.0]
    state = FastlaneState(clock=lambda: now[0])
    lock = _GuardLock()
    calls = []

    state.mark_success()
    assert run_slow_exit_sweep(
        state=state, exit_lock=lock, sweep_fn=lambda: calls.append("slow"),
        fastlane_enabled=True, fastlane_decides=True, stale_after_s=5.0,
    ) is False

    now[0] = 106.0
    assert run_slow_exit_sweep(
        state=state, exit_lock=lock,
        sweep_fn=lambda: calls.append("slow") if lock.held else None,
        fastlane_enabled=True, fastlane_decides=True, stale_after_s=5.0,
    ) is True
    assert calls == ["slow"]
    assert state.snapshot(stale_after_s=5.0)["fallback_active"] is True


def test_fast_and_slow_sweeps_use_the_same_lock():
    state = FastlaneState(clock=lambda: 100.0)
    lock = _GuardLock()
    calls = []

    run_fastlane_exit_sweep(
        state=state, exit_lock=lock,
        sweep_fn=lambda: calls.append("fast") if lock.held else None,
    )
    assert calls == ["fast"]

    assert run_slow_exit_sweep(
        state=state, exit_lock=lock,
        sweep_fn=lambda: calls.append("slow") if lock.held else None,
        fastlane_enabled=False, fastlane_decides=False, stale_after_s=5.0,
    ) is True
    assert calls == ["fast", "slow"]


def test_supervisor_restarts_a_stopped_worker():
    state = FastlaneState(clock=lambda: 100.0)
    made = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.alive = False
            made.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    first = ensure_fastlane_thread(
        None, target=lambda: None, state=state, thread_factory=FakeThread)
    assert first.is_alive()
    assert ensure_fastlane_thread(
        first, target=lambda: None, state=state, thread_factory=FakeThread) is first

    first.alive = False
    second = ensure_fastlane_thread(
        first, target=lambda: None, state=state, thread_factory=FakeThread)
    assert second is not first and second.is_alive()
    snapshot = state.snapshot(stale_after_s=5.0)
    assert snapshot["thread_alive"] is True
    assert snapshot["restart_count"] == 1


# --- the slow sweep must never park on the fast lane's lock (2026-09-09) -------------------------
#
# `run_slow_exit_sweep` used `with exit_lock:` — an unbounded acquire, called from the MAIN LOOP
# between the previous bracket_ts and finish_bracket_cycle. `run_fastlane_exit_sweep` holds the SAME
# lock for a whole real exit (quote, sign, submit, confirm, slippage ladder 3 -> 10 -> 25%). So a
# fast-lane exit froze bracket_ts, and at 300s `watch_sweep.py` called it `exit_sweep_stale` and the
# watchdog SIGTERMed the runner — while the fast lane was mid-sell. 58 kills between 2026-08-22 and
# 2026-09-09, seven on 09-04 alone, and not one exception in the log, because waiting on a lock is
# not an error.


def test_a_held_lock_does_not_block_the_slow_sweep():
    """THE REGRESSION. Held lock -> returns immediately, does not run, does not raise."""
    state = FastlaneState(clock=lambda: 100.0)
    lock = _GuardLock(held=True)
    calls = []
    assert run_slow_exit_sweep(
        state=state, exit_lock=lock,
        sweep_fn=lambda: calls.append("slow"),
        fastlane_enabled=False, fastlane_decides=False, stale_after_s=5.0,
        lock_timeout_s=0.01,
    ) is False
    assert calls == [], "the sweep must not run without the lock"
    assert lock.held is True, "someone else's lock must be left exactly as found"


def test_contention_is_reported_so_it_is_not_invisible():
    """It used to be silent, which is why a frozen bracket clock read as a dead sweep."""
    state = FastlaneState(clock=lambda: 100.0)
    seen = []
    run_slow_exit_sweep(
        state=state, exit_lock=_GuardLock(held=True), sweep_fn=lambda: None,
        fastlane_enabled=False, fastlane_decides=False, stale_after_s=5.0,
        lock_timeout_s=0.01, on_contended=lambda: seen.append(True),
    )
    assert seen == [True]


def test_the_lock_is_released_even_when_the_sweep_raises():
    """`with` guaranteed this. An explicit acquire/release must not lose it."""
    state = FastlaneState(clock=lambda: 100.0)
    lock = _GuardLock()

    def boom():
        raise RuntimeError("sweep exploded")

    try:
        run_slow_exit_sweep(
            state=state, exit_lock=lock, sweep_fn=boom,
            fastlane_enabled=False, fastlane_decides=False, stale_after_s=5.0)
    except RuntimeError:
        pass
    assert lock.held is False, "a raising sweep must not strand the exit lock"


def test_an_uncontended_sweep_is_unchanged():
    state = FastlaneState(clock=lambda: 100.0)
    lock = _GuardLock()
    calls = []
    assert run_slow_exit_sweep(
        state=state, exit_lock=lock, sweep_fn=lambda: calls.append("slow"),
        fastlane_enabled=False, fastlane_decides=False, stale_after_s=5.0) is True
    assert calls == ["slow"] and lock.held is False


def test_the_runner_passes_a_contention_reporter():
    """A silent skip would reintroduce the invisibility, not the block."""
    calls = [node for node in ast.walk(ast.parse(code_of("scripts/memecoin_runner_loop.py")))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "run_slow_exit_sweep"]
    assert calls and all(any(kw.arg == "on_contended" for kw in call.keywords)
                         for call in calls)


def test_the_deadline_sleep_applies_only_to_a_small_book_and_never_to_an_unknown_one():
    """The lane slept a full tick AFTER variable work, so its period was work + tick.

    Measured on `memecoin_route_series` (seam `exit_decision`, 14 d to 2026-09-24, ordered by id):
    consecutive quotes of one position are p50 1.61 s apart against a 1.0 s tick. Sleeping to
    `pass start + tick` removes that ~0.6 s from every stop.

    It is scoped by book size because the cost lands on one Jupiter key (1 RPS / 60 per minute):
    exit quotes taken with one position open sat in a minute that lost a quote to rate limiting
    0.8% of the time, with two open 25.8%, with three open 75.2%. So a book above `max_book` keeps
    the old floor, and so does a book we could not count.
    """
    from sniper.services.memecoin_fastlane import fastlane_wait_s

    # knob off (the code default): today's behaviour at every book size
    for book in (None, 0, 1, 2, 9):
        assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=book,
                               max_book=0) == 1.0

    # knob at 1, one position open: the remainder of the tick, not a fresh tick
    assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=1,
                           max_book=1) == pytest.approx(0.4)
    # a pass slower than the tick starts the next one immediately, never a negative wait
    assert fastlane_wait_s(1.0, started_at=100.0, now=101.9, open_book=1, max_book=1) == 0.0

    # two open with max_book=1: the old floor, whatever the elapsed time
    assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=2, max_book=1) == 1.0
    # raising the knob (a bigger key budget) extends it to that book size
    assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=2,
                           max_book=3) == pytest.approx(0.4)

    # an unknown book (a failed cycle reports None) and an empty book keep the floor
    assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=None, max_book=1) == 1.0
    assert fastlane_wait_s(1.0, started_at=100.0, now=100.6, open_book=0, max_book=1) == 1.0


def test_the_fast_lane_reports_the_open_book_it_already_counted(tmp_path):
    """The pacing needs the book size; asking the ledger again would put a query on the period."""
    from sniper.services import memecoin_fastlane as fl

    seen = {}
    monkey = [{"id": 1, "network_id": "solana", "mint": "m1"},
              {"id": 2, "network_id": "solana", "mint": "m2"}]
    real = fl.open_real_positions
    fl.open_real_positions = lambda conn, limit=None: list(monkey)
    try:
        fl.refresh_open_positions(None, {}, mid_batch_fn=lambda *a, **k: {},
                                  stats_out=seen)
    finally:
        fl.open_real_positions = real
    assert seen["open_positions"] == 2


@pytest.mark.parametrize("chains,expected_wait", [
    (["solana", "56"], 0.4),
    (["56", "4663"], 0.4),
    (["solana", "solana", "56"], 1.0),
    ([], 1.0),
])
def test_evm_positions_do_not_spend_the_solana_deadline_budget(monkeypatch, chains, expected_wait):
    from sniper.services import memecoin_fastlane as fl
    positions = [{"id": i, "network_id": chain, "mint": str(i)}
                 for i, chain in enumerate(chains)]
    monkeypatch.setattr(fl, "open_real_positions", lambda *a, **k: positions)
    stats = {}
    fl.refresh_open_positions(None, {}, context_fn=lambda *a: {},
                              mid_batch_fn=lambda *a, **k: {}, stats_out=stats)
    assert stats["open_positions"] == len(chains)
    wait = fl.fastlane_wait_s(1, started_at=100, now=100.6,
                             open_book=stats.get("deadline_positions", stats["open_positions"]),
                             max_book=1)
    assert wait == pytest.approx(expected_wait)
