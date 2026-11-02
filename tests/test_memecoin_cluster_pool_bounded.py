"""A deadline must actually STOP the traces it abandons.

Nothing outside a running trace can end it: `future.cancel()` only drops futures that never
started, and `ThreadPoolExecutor.shutdown(wait=False)` deliberately does not wait. So every
deadline used to return while its traces kept reading up to 20 paged RPC calls with network
timeouts, each holding a connection and spending the research quota.

The leak is ACROSS calls, not within one — `decide_cluster` returns on the first deadline, so a
single call never builds a second pool with live threads. Measured against the version before the
fix: five back-to-back deadlined calls at workers=3 left 15 traces running and 16 threads alive.
Only cooperative cancellation changes that, which is why `trace_first_funder` takes `should_stop`.
"""
from __future__ import annotations

import threading
import time

from sniper.services import memecoin_cluster_admission as ca


def _deadlined_calls(trace_fn, calls=3, workers=3):
    for _ in range(calls):
        t = [0.0]

        def clock():
            t[0] += 0.05          # every read advances, so the deadline lapses inside the batch
            return t[0]

        ca.decide_cluster([(f"o{i}", 100 - i) for i in range(24)], {},
                          trace_fn=trace_fn, workers=workers, deadline_s=0.2, clock=clock)


def test_a_deadline_stops_traces_that_accept_should_stop():
    live = 0
    lock = threading.Lock()

    def stoppable(owner, should_stop=None):
        """Outlives the observation window on purpose. A trace that finishes by itself inside the
        wait would make this test pass with or without the fix — verified: an earlier version of
        this fixture slept 20x0.01s and did exactly that."""
        nonlocal live
        with lock:
            live += 1
        try:
            for _page in range(40):               # 40 x 0.05s = 2.0s if never stopped
                if should_stop is not None and should_stop():
                    return None, "aborted"
                time.sleep(0.05)
            return None, "unresolved"
        finally:
            with lock:
                live -= 1

    _deadlined_calls(stoppable)
    time.sleep(0.5)                               # well inside the 2.0s an unstopped trace needs
    with lock:
        assert live == 0, (
            f"{live} traces still running after three deadlined calls. Measured against the "
            f"version before the fix with this same fixture: 9 still running at +0.5s.")


def test_a_single_argument_trace_fn_still_works():
    """The cache worker and every test double pass a one-argument callable. It must not break."""
    seen = []

    def plain(owner):
        seen.append(owner)
        return None, "unresolved"

    t = [0.0]

    def clock():
        t[0] += 0.001
        return t[0]

    # enough owners to clear the min-owners gate, or nothing reaches the trace path at all
    owners = [(f"o{i}", 100 - i) for i in range(24)]
    ca.decide_cluster(owners, {}, trace_fn=plain, workers=3, deadline_s=30.0, clock=clock)
    assert set(seen) == {o for o, _ in owners}, "a one-argument trace_fn must still be called"
