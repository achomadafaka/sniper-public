"""Offline review checks. All network/database boundaries replaced by existing fake harness."""
import json
import sys
import threading
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import test_onchain_evm_watch_backfill as b
import test_onchain_evm_watch_dark_poll as d


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    monkeypatch.setattr(b.w, 'erc20_decimals', lambda *args, **kwargs: 18)
    monkeypatch.setattr(b.w, 'evm_rpc_for_chain', lambda chain: 'http://rpc.test')
    monkeypatch.setattr(b.cfg, 'memecoin_onchain_evm_j7_xcheck_chains', lambda: frozenset())
    monkeypatch.setattr(b.cfg, 'memecoin_onchain_evm_sell_shadow_chains', lambda: frozenset())
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError('Unexpected network in review check')
    monkeypatch.setattr(socket.socket, 'connect', no_network)


def test_backfill_receipt_null_keeps_unread_frontier(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    def null_decode(*args, outcome, **kwargs):
        outcome.update(reason='receipt_null', retries=4, hosts={}, host_ms={})
        return None
    monkeypatch.setattr(b.w, 'push_detection', null_decode)
    b._run(monkeypatch, env, [b._heads(1030, 5)], previous_block=1000, hits=[1012])
    assert env['inserts'] == []
    assert json.loads(env['hb'].read_text())['last_block'] < 1012


def test_dark_poll_preserves_failed_push_gap_frontier(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    clock = d.dark.__wrapped__(monkeypatch, env)
    env['head'] = 2060
    post = b._getlogs(env, [])
    def fail_gap(url, method, params):
        if method == 'eth_getLogs' and int(params[0]['fromBlock'], 16) <= 2050:
            raise RuntimeError('HTTP 429')
        return post(url, method, params)
    def capture_gap(state, kw):
        env['gap_frontier'] = json.loads(env['hb'].read_text())['last_block']
    d._run_dark(monkeypatch, env, clock,
                [b._heads(2000, 20), d._reconnect, b._heads(2050, 2), capture_gap, d._fallback(env)],
                post=fail_gap)
    assert env['gap_frontier'] == 2019
    assert env['beat_after_dark']['last_block'] == 2019


def test_failed_backfill_requeue_survives_concurrent_reconnect(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    b.w._configure('4663')
    monkeypatch.setattr(b.w, 'HEARTBEAT', env['hb'])
    env['hb'].write_text(json.dumps({'last_block':1000}))
    queued=[]
    monkeypatch.setattr(b.w, '_backfill_thread', queued.append)
    def fail(*args):
        raise RuntimeError('HTTP 429')
    monkeypatch.setattr(b.w, '_backfill_post', fail)
    # Trace supplies deterministic scheduling only. Worker runs the unchanged production
    # _run_backfill in a real thread after prior_pending was read by the reconnect thread.
    import inspect
    source, start = inspect.getsourcelines(b.w.run_push)
    pause_line = start + next(i for i, line in enumerate(source)
                             if line.strip() == 'prior_pending = gap.get("pending")') + 1
    def fake_stream(url, **kw):
        state=kw['state']
        b._heads(1005, 6)(state, kw)
        assert len(queued)==1
        def trace(frame, event, arg):
            if frame.f_code.co_name == '_gap_check' and event=='line' and frame.f_lineno==pause_line:
                worker=threading.Thread(target=queued.pop())
                worker.start()
                worker.join(2)
                assert not worker.is_alive()
                env['worker_finished'] = True
                sys.settrace(None)
            return trace
        state.reconnects += 1
        sys.settrace(trace)
        try:
            b._heads(1012, 1)(state, kw)
        finally:
            sys.settrace(None)
        return state
    monkeypatch.setattr(b.P, 'run_push_stream', fake_stream)
    price=b.w.NativePrice(lambda:2650.0, log_fn=lambda _:None)
    price.refresh()
    b.w.run_push('wss://test', price=price)
    assert env['worker_finished']
    # The start backfill re-reads the anchor block (P1(c), [prev, cur] inclusive), so the durable
    # frontier is prev - 1 while that read is unfinished.
    assert json.loads(env['hb'].read_text())['last_block']==999


def test_failed_backfill_retries_before_default_action_window_expires(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    clock = d.dark.__wrapped__(monkeypatch, env)
    b.w._configure('4663')
    monkeypatch.setattr(b.w, 'HEARTBEAT', env['hb'])
    env['hb'].write_text(json.dumps({'last_block':1000}))
    calls=[]
    def transient_failure(url, method, params):
        calls.append(params)
        if len(calls)==1:
            raise RuntimeError('HTTP 429')
        return []
    monkeypatch.setattr(b.w, '_backfill_post', transient_failure)
    monkeypatch.setattr(b.w, '_backfill_thread', lambda target: target())
    def fake_stream(url, **kw):
        state=kw['state']
        b._heads(1005, 2)(state, kw)
        for block in range(1007,1068):
            clock.t += 1
            b._heads(block,1)(state,kw)
        return state
    monkeypatch.setattr(b.P, 'run_push_stream', fake_stream)
    price=b.w.NativePrice(lambda:2650.0, log_fn=lambda _:None)
    price.refresh()
    b.w.run_push('wss://test', price=price)
    assert len(calls)>1, (calls, json.loads(env['hb'].read_text())['last_block'])


@pytest.mark.parametrize('reason,decode', [
    ('tx_null', None), ('receipt_null', None),
    ('no_buy', 'contract_code_unavailable'), ('no_buy', 'relay_price_unavailable'),
])
def test_dark_poll_holds_frontier_on_unknown_decode(monkeypatch, tmp_path, reason, decode):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    clock = d.dark.__wrapped__(monkeypatch, env)
    def unknown(*args, outcome, **kwargs):
        outcome.update(reason=reason, decode=decode, retries=0, hosts={}, host_ms={})
        return None
    monkeypatch.setattr(b.w, 'push_detection', unknown)
    d._run_dark(monkeypatch, env, clock, [b._heads(2000, 20), d._fallback(env)], hits=[2025])
    assert env['inserts'] == []
    assert env['beat_after_dark']['last_block'] == 2019


def test_persistent_failure_has_one_retry_then_named_expiry(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    clock = d.dark.__wrapped__(monkeypatch, env)
    b.w._configure('4663')
    monkeypatch.setattr(b.w, 'HEARTBEAT', env['hb'])
    env['hb'].write_text(json.dumps({'last_block':1000}))
    calls=[]
    def failed(url, method, params):
        calls.append(clock.t)
        raise RuntimeError('HTTP 429')
    monkeypatch.setattr(b.w, '_backfill_post', failed)
    monkeypatch.setattr(b.w, '_backfill_thread', lambda target: target())
    def fake_stream(url, **kw):
        state=kw['state']
        b._heads(1005, 2)(state, kw)
        for block in range(1007,1068):
            clock.t += 1
            b._heads(block,1)(state,kw)
            if block <= 1045:
                # prev - 1: the unread start range includes the anchor block itself (P1(c)).
                assert json.loads(env['hb'].read_text())['last_block'] == 999
        return state
    monkeypatch.setattr(b.P, 'run_push_stream', fake_stream)
    price=b.w.NativePrice(lambda:2650.0, log_fn=lambda _:None)
    price.refresh()
    b.w.run_push('wss://test', price=price)
    assert len(calls) == 2 and calls[1] - calls[0] >= 5
    assert any('expired unread' in line for line in env['lines'])
    assert json.loads(env['hb'].read_text())['last_block'] == 1067


def test_poll_recovery_retires_pending_gap_before_push_resumes(monkeypatch, tmp_path):
    env = b.env.__wrapped__(monkeypatch, tmp_path)
    clock = d.dark.__wrapped__(monkeypatch, env)
    env['head'] = 2060
    post = b._getlogs(env, [])
    dark = [False]
    def failing_push(url, method, params):
        if not dark[0] and method == 'eth_getLogs':
            raise RuntimeError('HTTP 429')
        return post(url, method, params)
    def fallback(state, kw):
        dark[0] = True
        kw['on_fallback']()
    d._run_dark(monkeypatch, env, clock,
               [b._heads(2000, 20), d._reconnect, b._heads(2050, 2), fallback,
                d._reconnect, b._heads(2080, 2)], post=failing_push)
    assert sum(lo <= 2040 <= hi for lo, hi in env['pages']) == 1
