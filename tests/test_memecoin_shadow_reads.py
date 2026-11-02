"""Measurement work cannot block entry or build an unlimited backlog."""
import threading
import queue
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_real_runner as runner
from sniper.services import memecoin_evm_tape_marks as marks
from sniper.services import memecoin_volume_writer as writer


def test_chase_shadow_returns_while_tape_read_is_blocked(monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        release.wait(2)
        return {"move_pct": 0.0, "why": "ok"}
    monkeypatch.setattr(writer, "_gate_source_tx", lambda *args: "0x" + "a" * 64)
    monkeypatch.setattr(marks, "source_move", slow)
    # Own pool: the process-wide momentum pool is shared with every other test in this xdist
    # worker, and a held or queued task there starved this read (deploy fc24af7f3 refused).
    from sniper.services.memecoin_shadow_reads import ShadowReads
    pool = ShadowReads(workers=1, maxsize=4)
    monkeypatch.setattr(runner, "_momentum_read_pool", lambda: pool)
    recorded = []
    trace = SimpleNamespace(mark_substage=lambda name, **fields: recorded.append((name, fields)))
    ev = SimpleNamespace(mint="0x" + "b" * 40, fomo_event_id="origin", their_price=1.0)
    worker = threading.Thread(target=lambda: (runner._shadow_tape_move(trace, ev, "4663", {"price": 1.2}), done.set()))
    worker.start()
    try:
        assert entered.wait(1)
        assert done.wait(0.5), "entry waited for the shadow dependency"   # the read holds for 2 s
        name, payload = recorded[0]
        assert name == "chase_tape_move_shadow" and payload["why"] == "read_not_ready"
        assert payload["move_pct"] is None
        release.set()
        trace.tape_move_shadow["future"].result(timeout=1)
        runner._tape_move_late_best_effort(trace)
        name, payload = recorded[-1]
        assert name == "chase_tape_move_shadow_late" and payload["move_pct"] == 0.0
        assert payload["display_move_pct"] == pytest.approx(20.0)
        assert payload["measured_at_s"] >= payload["decision_at_s"]
    finally:
        release.set()
        worker.join(2)


def test_shadow_queue_refuses_extra_work_without_waiting():
    from sniper.services.memecoin_shadow_reads import ShadowReads
    pool = ShadowReads(workers=1, maxsize=1)
    entered, release = threading.Event(), threading.Event()
    def held():
        entered.set()
        release.wait(2)
    first = pool.submit(held)
    try:
        assert entered.wait(1)
        second = pool.submit(lambda: 2)
        with pytest.raises(queue.Full, match="shadow_queue_full"):
            pool.submit(lambda: 3)
    finally:
        release.set()
        first.result(timeout=1)
    assert second.result(timeout=1) == 2


def test_shadow_pool_preserves_read_exception():
    from sniper.services.memecoin_shadow_reads import ShadowReads
    pool = ShadowReads(workers=1, maxsize=1)
    def failed():
        raise ValueError("partial answer")
    with pytest.raises(ValueError, match="partial answer"):
        pool.submit(failed).result(timeout=1)
    assert pool.submit(lambda: 0).result(timeout=1) == 0


@pytest.mark.parametrize('kw', [{'workers': 0}, {'maxsize': 0}, {'max_age_s': 0},
                                {'max_age_s': float('nan')}])
def test_shadow_capacity_cannot_be_disabled(kw):
    from sniper.services.memecoin_shadow_reads import ShadowReads
    with pytest.raises(ValueError):
        ShadowReads(**kw)


def test_expired_queued_read_never_calls_the_dependency():
    from sniper.services.memecoin_shadow_reads import ShadowReads
    pool = ShadowReads(workers=1, maxsize=1, max_age_s=0.1)
    entered, release = threading.Event(), threading.Event()
    def held():
        entered.set()
        release.wait(2)
    first = pool.submit(held)
    calls = []
    try:
        assert entered.wait(1)
        old = pool.submit(lambda: calls.append(True))
        time.sleep(0.2)
    finally:
        release.set()
        first.result(timeout=1)
    with pytest.raises(TimeoutError, match='shadow_queue_expired'):
        old.result(timeout=1)
    assert not calls


def test_momentum_overflow_is_typed_and_never_waited_for(monkeypatch):
    class FullPool:
        def submit(self, *args):
            raise queue.Full('shadow_queue_full')
    monkeypatch.setattr(runner, '_momentum_read_pool', lambda: FullPool())
    holder = runner._start_momentum_read('solana', 'mint')
    assert runner._momentum_own_peek(holder) == {'reason': 'queue_full', 'wait_would_ms': 0.0}
    assert holder['peeked'] == 'not_started'


@pytest.mark.parametrize('late', [False, True])
@pytest.mark.parametrize('error,reason', [(TimeoutError('shadow_queue_expired'), 'queue_expired'),
                                        (ValueError('broken read'), 'read_failed')])
def test_momentum_failed_future_retains_terminal_unknown(late, error, reason):
    from concurrent.futures import Future
    future = Future()
    holder = {'future': future}
    marks = []
    if late:
        assert runner._momentum_own_peek(holder)['reason'] == 'read_not_ready'
    future.set_exception(error)
    if late:
        trace = SimpleNamespace(momentum_own=holder,
                                mark_substage=lambda name, **fields: marks.append((name, fields)))
        runner._momentum_own_late_best_effort(trace)
        name, fields = marks[-1]
        assert name == 'momentum_own_late'
        assert fields['phase'] == 'unknown' and fields['points'] == 0
    else:
        fields = runner._momentum_own_peek(holder)
    assert fields['reason'] == reason


def test_error_terminal_preserves_ready_late_evidence_before_finish(monkeypatch):
    from concurrent.futures import Future
    tape, momentum = Future(), Future()
    tape.set_result({'move_pct': 0.0, 'why': 'ok'})
    momentum.set_exception(TimeoutError('shadow_queue_expired'))
    seen = []
    trace = SimpleNamespace(
        attempt_id='attempt',
        tape_move_shadow={'future': tape, 'peeked': True, 'decision_at_s': 99.0,
                          'display_move_pct': 0.0},
        momentum_own={'future': momentum, 'peeked': 'not_ready',
                      'classified_mono': time.monotonic()},
        mark_substage=lambda name, **fields: seen.append((name, fields)),
        finish=lambda status, reason, **fields: seen.append((status, fields)) or True)
    monkeypatch.setattr(runner, '_record_entry_shadows_best_effort', lambda *args: None)
    monkeypatch.setattr(runner, '_notify_entry_projection', lambda *args: None)
    monkeypatch.setattr(runner, '_record_radar_best_effort', lambda *args: None)
    assert runner._finish_trace_error_best_effort(trace, ValueError('unavailable'))
    assert [name for name, _ in seen] == ['momentum_own_late', 'chase_tape_move_shadow_late', 'error']
    assert seen[0][1]['reason'] == 'queue_expired' and seen[1][1]['move_pct'] == 0.0


def _receipt_trace(monkeypatch, *, ready=True, observation=None):
    from concurrent.futures import Future
    from sniper.services import memecoin_evm_post_trade as pt
    tx, owner, mint = '0x' + 'a9' * 32, '0x' + 'cd' * 20, '0x' + 'ef' * 20
    future, calls, marks = Future(), [], []
    fact = {'network_id': '56', 'source_tx': tx, 'source_block_number': 126956870}
    if ready:
        future.set_result({'reason': 'no_venue_leg', 'source_receipt_observation':
                           fact if observation is None else observation})
    monkeypatch.setattr(pt, 'enabled', lambda chain: chain == '56')
    monkeypatch.setattr(pt, 'read_host', lambda chain: 'https://read.invalid')
    monkeypatch.setattr(pt, 'submit_receipt_reference',
                        lambda *args: calls.append(args) or future)
    event = SimpleNamespace(fomo_event_id=f'swap:raybot:{tx}:{owner}:{mint}', mint=mint)
    trace = SimpleNamespace(attempt_id='attempt',
                            mark_substage=lambda name, **fields: marks.append((name, fields)),
                            finish=lambda *args, **kwargs: True)
    runner._start_evm_post_trade_read(event, '56', trace=trace)
    return event, trace, future, calls, marks


def test_receipt_observation_reuses_the_read_and_never_waits(monkeypatch):
    event, trace, future, calls, marks = _receipt_trace(monkeypatch, ready=False)
    runner._start_evm_post_trade_read(event, '56', trace=trace)
    runner._evm_receipt_timing_late_best_effort(trace)
    assert len(calls) == 1 and marks == [] and not future.done()
    future.set_result({'reason': 'no_venue_leg', 'source_receipt_observation': {
        'network_id': '56', 'source_tx': '0x' + 'a9' * 32, 'source_block_number': 126956870}})
    runner._evm_receipt_timing_late_best_effort(trace)
    assert marks == [('evm_source_receipt_observation', {'observation': {
        'network_id': '56', 'source_tx': '0x' + 'a9' * 32, 'source_block_number': 126956870}})]


@pytest.mark.parametrize('bad', [
    {'network_id': '1', 'source_tx': '0x' + 'a9' * 32, 'source_block_number': 126956870},
    {'network_id': '56', 'source_tx': '0x' + 'bb' * 32, 'source_block_number': 126956870},
    {'network_id': '56', 'source_tx': '0x' + 'a9' * 32, 'source_block_number': 0},
])
def test_receipt_observation_never_crosses_attempt_identity(monkeypatch, bad):
    _, trace, _, _, marks = _receipt_trace(monkeypatch, observation=bad)
    runner._evm_receipt_timing_late_best_effort(trace)
    assert marks == []


@pytest.mark.parametrize('error', [False, True])
def test_receipt_observation_is_recorded_before_terminal_finish(monkeypatch, error):
    _, trace, _, _, marks = _receipt_trace(monkeypatch)
    trace.finish = lambda *args, **kwargs: marks.append(('terminal', {})) or True
    monkeypatch.setattr(runner, '_record_entry_shadows_best_effort', lambda *args: None)
    monkeypatch.setattr(runner, '_notify_entry_projection', lambda *args: None)
    monkeypatch.setattr(runner, '_record_radar_best_effort', lambda *args: None)
    if error:
        assert runner._finish_trace_error_best_effort(trace, ValueError('unavailable'))
    else:
        assert runner._finish_trace_for_result(trace, {'status': 'filled'})
    assert [name for name, _ in marks] == ['evm_source_receipt_observation', 'terminal']
