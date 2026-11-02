from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from sniper.office.models import Envelope
from sniper.office.scheduler import Scheduler


def result(provider='claude', outcome='success'):
    return Envelope(requested_provider=provider,requested_model='m',outcome=outcome)


def test_provider_rate_limit_does_not_block_other_provider():
    s=Scheduler({'claude':1,'codex':1},2,backoff_s=10)
    assert s.submit('claude',lambda:result(outcome='rate_limited')).outcome=='rate_limited'
    r=s.submit('claude',lambda:pytest.fail('backoff dispatched'),timeout_s=.015)
    assert r.outcome=='rate_limited' and r.throttled_s>0
    assert s.submit('codex',lambda:result('codex')).outcome=='success'


def test_auth_pause_requires_manual_resume():
    s=Scheduler({'claude':1,'codex':1},2)
    s.submit('codex',lambda:result('codex','auth_revoked'))
    assert s.submit('codex',lambda:pytest.fail('auth dispatched')).outcome=='auth_revoked'
    s.resume('codex')
    assert s.submit('codex',lambda:result('codex')).outcome=='success'
    s.pause('claude','d_judge_guard')
    assert s.submit('claude',lambda:pytest.fail('paused dispatched')).error_reason=='d_judge_guard'


def test_host_children_and_provider_limits_bound_concurrent_calls():
    s=Scheduler({'claude':2,'codex':2},1)
    entered=threading.Event();release=threading.Event()
    def first():
        entered.set();assert release.wait(2);return result()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(s.submit,'claude',first)
        assert entered.wait(1)
        blocked=s.submit('codex',lambda:pytest.fail('host limit exceeded'),timeout_s=.015)
        assert blocked.outcome=='timeout' and blocked.waiting_s>0 and blocked.throttled_s==0
        release.set();assert future.result().outcome=='success'
    assert s.submit('codex',lambda:result('codex')).outcome=='success'


def test_different_accounts_keep_independent_backoff():
    s=Scheduler({'claude':1},2)
    s.submit('claude',lambda:result(outcome='auth_revoked'),account_id='a')
    assert s.submit('claude',lambda:result(),account_id='b').outcome=='success'


def test_external_guard_read_failure_refuses_without_call():
    def broken():raise OSError('missing watchdog')
    s=Scheduler({'claude':1},1,external_guard=broken)
    r=s.submit('claude',lambda:pytest.fail('guard bypass'))
    assert r.outcome=='refused' and r.error_reason=='guard_unknown'


def test_guard_is_rechecked_after_waiting():
    paused=threading.Event();entered=threading.Event();release=threading.Event()
    s=Scheduler({'claude':1,'codex':1},1,external_guard=lambda:'guard_paused' if paused.is_set() else None)
    def work():entered.set();assert release.wait(2);return result()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(s.submit,'claude',work);assert entered.wait(1)
        b=pool.submit(s.submit,'codex',lambda:pytest.fail('guard not rechecked'),timeout_s=.5)
        paused.set();release.set()
        assert a.result().outcome=='success'
        assert b.result().error_reason=='guard_paused'


def test_bad_result_and_exception_release_capacity():
    s=Scheduler({'claude':1},1)
    assert s.submit('claude',lambda:None).outcome=='invalid'
    def fail():raise RuntimeError('secret')
    assert s.submit('claude',fail).outcome=='invalid'
    assert s.submit('claude',lambda:result()).outcome=='success'


def test_bounded_pending_refuses_extra_waiters():
    entered=threading.Event();release=threading.Event()
    s=Scheduler({'claude':1},1,max_pending=0)
    def first():entered.set();assert release.wait(2);return result()
    with ThreadPoolExecutor(max_workers=1) as p:
        a=p.submit(s.submit,'claude',first);assert entered.wait(1)
        r=s.submit('claude',lambda:pytest.fail('overflow dispatch'))
        assert r.outcome=='refused' and r.error_reason=='queue_full'
        release.set();a.result()


def test_replay_yields_to_queued_live_work():
    import time
    s=Scheduler({'claude':1,'codex':1},1)
    entered=threading.Event();release=threading.Event();live_done=threading.Event()
    def busy():entered.set();assert release.wait(2);return result()
    def live():live_done.set();return result('codex')
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(s.submit,'claude',busy)
        assert entered.wait(1)
        queued=pool.submit(s.submit,'codex',live,timeout_s=1)
        end=time.monotonic()+1
        while not s.live_backlogged() and time.monotonic()<end:time.sleep(.005)
        replay=s.submit('claude',lambda:pytest.fail('replay stole live capacity'),priority='replay',timeout_s=0)
        assert replay.error_reason=='live_backlog'
        release.set();assert queued.result(2).outcome=='success';assert first.result(2).outcome=='success'
    assert live_done.is_set() and not s.live_backlogged()


def test_replay_admits_only_valid_priority_and_idle_capacity():
    s=Scheduler({'claude':1},1)
    assert s.submit('claude',lambda:result(),priority='replay').outcome=='success'
    assert s.submit('claude',lambda:pytest.fail('unknown priority'),priority='urgent').outcome=='refused'
