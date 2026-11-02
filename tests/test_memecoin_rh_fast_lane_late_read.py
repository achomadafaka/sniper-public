"""A custody read that completes after its caller gave up is used by the next poll (Codex EVM-02).

LiveLane._bounded_read promised "an answer that lands late is used by the next poll", but a
finished job was replaced by a new request before its key was compared, so the late receipt was
dropped and asked again (provider_read_count=2 in the audit's reproduction). If the second read
stalled or failed, a canonical receipt already in hand was lost and the attempt stayed unknown.
Now a finished same-key job with a value is consumed (once); a finished foreign or empty job is
replaced; one live worker at most; the caller's deadline is unchanged.
"""
import threading
import time

from sniper.services.memecoin_rh_fast_lane import LiveLane

RECEIPT = {"status": "0x1", "transactionHash": "0xabc"}


def _lane(read):
    lane = LiveLane.__new__(LiveLane)
    lane._read_lock = threading.Lock()
    lane._read_job = lane._read_thread = None
    lane.rpc_read = read
    return lane


def _late_first(release, calls):
    def read(method, params):
        calls.append((method, tuple(params)))
        if len(calls) == 1:
            time.sleep(0.03)
            return dict(RECEIPT)
        release.wait(1)
        return None
    return read


def test_a_same_key_late_completion_is_returned_with_one_provider_call():
    """THE regression (audit reproduction)."""
    release, calls = threading.Event(), []
    lane = _lane(_late_first(release, calls))
    assert lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005) is None
    lane._read_thread.join(0.2)
    got = lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005)
    release.set()
    assert got == RECEIPT and len(calls) == 1
    assert lane._read_job is None, "consumed exactly once"


def test_a_different_key_never_receives_the_prior_result():
    release, calls = threading.Event(), []
    lane = _lane(_late_first(release, calls))
    lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005)
    lane._read_thread.join(0.2)
    got = lane._bounded_read("eth_getTransactionReceipt", ["0xdef"], time.monotonic() + 0.01)
    release.set()
    assert got is None and calls[-1] == ("eth_getTransactionReceipt", ("0xdef",))


def test_a_pending_foreign_request_returns_promptly():
    release, calls = threading.Event(), []

    def hung(method, params):
        calls.append(1)
        release.wait(1)
    lane = _lane(hung)
    lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005)
    started = time.monotonic()
    assert lane._bounded_read("eth_getTransactionReceipt", ["0xdef"], time.monotonic() + 0.5) is None
    assert time.monotonic() - started < 0.05 and len(calls) == 1
    release.set()


def test_parallel_callers_keep_one_live_worker():
    release, calls = threading.Event(), []

    def slow(method, params):
        calls.append(1)
        release.wait(1)
        return dict(RECEIPT)
    lane = _lane(slow)
    out = []
    threads = [threading.Thread(target=lambda: out.append(lane._bounded_read(
        "eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.3))) for _ in range(6)]
    [t.start() for t in threads]
    time.sleep(0.05)
    release.set()
    [t.join() for t in threads]
    assert len(calls) == 1 and RECEIPT in out


def test_a_timed_out_worker_is_daemon_and_joined_not_multiplied():
    release, calls = threading.Event(), []

    def hung(method, params):
        calls.append(1)
        release.wait(1)
        return dict(RECEIPT)
    lane = _lane(hung)
    for _ in range(4):
        assert lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005) is None
    assert lane._read_thread.daemon and len(calls) == 1
    release.set()


def test_an_empty_late_answer_is_not_consumed_as_a_hit():
    calls = []

    def read(method, params):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.03)
            return None                    # a failed read
        return dict(RECEIPT)
    lane = _lane(read)
    lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.005)
    lane._read_thread.join(0.2)
    got = lane._bounded_read("eth_getTransactionReceipt", ["0xabc"], time.monotonic() + 0.2)
    assert got == RECEIPT and len(calls) == 2


def test_the_receipt_poll_still_ends_at_its_deadline_with_a_hung_rpc():
    release = threading.Event()
    lane = _lane(lambda m, p: release.wait(2))
    lane.receipt_poll_s, lane.sleep = 0.01, time.sleep
    started = time.monotonic()
    assert lane._receipt("0xabc", 0.15) is None
    assert time.monotonic() - started < 0.4
    release.set()
