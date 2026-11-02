"""Bounded Base observation work; no provider calls or production ledger access."""
import threading

from sniper.services import memecoin_base_preconf as P


def test_decode_admission_deduplicates_and_bounds_before_work():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def decode(log, arrival):
        calls.append((log, arrival))
        entered.set()
        assert release.wait(2)
        return True
    queue = P.DecodeWork(decode, workers=1, cap=2, max_age_s=2, now_fn=lambda: 0)
    try:
        first = queue.submit(('8453', 'tx1', 'mint', 'wallet'), 'one', 'socket-arrival-one')
        assert entered.wait(2)
        second = queue.submit(('8453', 'tx2', 'mint', 'wallet'), 'two', 'socket-arrival-two')
        duplicate = queue.submit(('8453', 'tx2', 'mint', 'wallet'), 'duplicate', 'later-clock')
        refused = queue.submit(('8453', 'tx3', 'mint', 'wallet'), 'full', 'three')
        assert duplicate is second
        assert refused.result(timeout=1) is None
        assert queue.snapshot()['full'] == 1 and queue.snapshot()['duplicate'] == 1
        assert queue.snapshot()['queued'] == 1
        release.set()
        assert first.result(timeout=2) is True and second.result(timeout=2) is True
        assert calls == [('one', 'socket-arrival-one'), ('two', 'socket-arrival-two')]
    finally:
        release.set()
        queue.close(wait=True)


def test_expired_queued_decode_never_starts_or_rebases_arrival():
    entered, release = threading.Event(), threading.Event()
    now, calls = [0.0], []
    def decode(log, arrival):
        calls.append((log, arrival))
        entered.set()
        assert release.wait(2)
        return True
    queue = P.DecodeWork(decode, workers=1, cap=2, max_age_s=2, now_fn=lambda: now[0])
    try:
        first = queue.submit('one', 'one', 'original-time')
        assert entered.wait(2)
        second = queue.submit('two', 'expired', 'second-time')
        now[0] = 3
        release.set()
        assert first.result(timeout=2) is True
        assert second.result(timeout=2) is None
        assert calls == [('one', 'original-time')]
        assert queue.snapshot()['expired'] == 1
    finally:
        release.set()
        queue.close(wait=True)


def test_canonical_resolution_coalesces_burst_separately_from_decode_overload():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    heads = []
    def resolve(head):
        heads.append(head)
        if head == 101:
            entered.set()
            assert release.wait(2)
        if head == 110:
            finished.set()
    decode_entered, decode_release = threading.Event(), threading.Event()
    def slow_decode(*args):
        decode_entered.set()
        assert decode_release.wait(2)
        return True
    decoder = P.DecodeWork(slow_decode, workers=1, cap=1)
    decode = decoder.submit('busy', 'log', 'arrival')
    assert decode_entered.wait(2)
    assert decoder.submit('full', 'log2', 'arrival2').result(timeout=1) is None
    resolver = P.HeadWork(resolve)
    try:
        resolver.submit(101)
        assert entered.wait(2)
        for head in range(102, 111):
            resolver.submit(head)
        resolver.submit(105)
        assert resolver.snapshot()['pending_head'] == 110
        release.set()
        assert finished.wait(2)
        assert heads == [101, 110] and not decode.done()
        assert resolver.snapshot()['coalesced'] >= 8
    finally:
        release.set()
        decode_release.set()
        resolver.close(wait=True)
        decoder.close(wait=True)


def test_stopped_decode_rejects_new_work_without_running_callback():
    calls = []
    queue = P.DecodeWork(lambda *args: calls.append(args), workers=1, cap=2)
    queue.close(wait=True)
    assert queue.submit('key', 'log', 'arrival').result(timeout=1) is None
    assert not calls and queue.snapshot()['stopped'] == 1


def test_cancelled_observation_does_not_kill_decoder_worker():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def decode(log, arrival):
        calls.append(log)
        if log == 'first':
            entered.set()
            assert release.wait(2)
        return True
    queue = P.DecodeWork(decode, workers=1, cap=3)
    try:
        first = queue.submit('first', 'first', 'arrival')
        assert entered.wait(2)
        cancelled = queue.submit('cancelled', 'cancelled', 'arrival')
        assert cancelled.cancel()
        last = queue.submit('last', 'last', 'arrival')
        release.set()
        first.result(timeout=2)
        assert last.result(timeout=2) is True
        assert calls == ['first', 'last']
        assert queue.snapshot()['cancelled'] == 1
    finally:
        release.set()
        queue.close(wait=True)
