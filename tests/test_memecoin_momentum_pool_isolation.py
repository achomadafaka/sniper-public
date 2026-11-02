"""A test that leaves the shared momentum pool busy must not starve a later test (Gamma task
2765ab12; deploy fc24af7f3 refused 2026-10-15 by two victims of exactly this). The two tests run in
file order in one process: the first leaves both of the pool's workers blocked for 3 s, the second
needs one read back within the 1 s the gate's shadow readers allow. tests/conftest.py's
_no_inherited_busy_momentum_pool hands the second test a fresh pool; without it the second test
times out. Under xdist the pair may land on different workers, where the second passes trivially."""
import threading

from sniper.services import memecoin_real_runner as runner


def test_a_producer_leaves_both_momentum_pool_workers_blocked():
    pool = runner._momentum_read_pool()
    entered, release = threading.Semaphore(0), threading.Event()

    def blocked():
        entered.release()
        release.wait(3.0)

    timer = threading.Timer(3.0, release.set)
    timer.daemon = True                        # frees the workers after this test, never in it
    timer.start()
    pool.submit(blocked)
    pool.submit(blocked)
    assert entered.acquire(timeout=1) and entered.acquire(timeout=1)
    assert pool.queue.unfinished_tasks == 2    # both workers still busy when this test ends


def test_a_later_test_gets_its_read_within_the_gate_wait():
    future = runner._momentum_read_pool().submit(lambda: "read")
    assert future.result(timeout=1) == "read"
