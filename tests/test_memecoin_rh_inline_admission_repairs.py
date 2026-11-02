"""Production seams; fixture HTTP only, no provider or live state."""
import threading
import pytest
from test_memecoin_rh_inline_rotation import _lane_with_two_hosts,_call


@pytest.fixture(autouse=True)
def no_external_alert_io(monkeypatch):
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts,'send_alert',lambda *a,**k:None)


@pytest.mark.parametrize("answer",[{"result":"0x1237","error":{"code":-32000,"message":"partial"}}, {"result":"0x1237","error":None}, None, [], {"result":"garbage"}])
def test_error_bearing_identity_cannot_admit_or_send_token_reads(monkeypatch,tmp_path,answer):
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    try:
        s=built['inline_paced']
        for h in s.hosts:
            h.identity_probe=lambda:(200,answer)
        s.admit_pass();sent.clear()
        for h in s.hosts:
            _call(h.safety_fn,'0x'+'22'*20);_call(h.facts_fn,'0x'+'22'*20)
        assert [h.admission for h in s.hosts]==['pending','pending']
        assert sent==[]
    finally:built['live']._safety_pool.shutdown(wait=True)


def test_rejoin_revokes_admission_before_quota_unblocks(monkeypatch,tmp_path):
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    cleared=threading.Event();release=threading.Event();thread=None
    try:
        s=built['inline_paced'];s.admit_pass();h=s.hosts[0];p=h.paced
        p._quota=True;p._quota_probe_at=0.
        p.quota_probe=lambda:(200,{'result':'0x123'})
        original=p.quota_probe_result
        def result(*args,**kwargs):
            original(*args,**kwargs)
            cleared.set();assert release.wait(3)
        monkeypatch.setattr(p,'quota_probe_result',result)
        thread=threading.Thread(target=s.quota_recover);thread.start()
        assert cleared.wait(3)
        sent.clear()
        _call(h.safety_fn,'0x'+'22'*20);_call(h.facts_fn,'0x'+'22'*20)
        assert h.admission=='pending'and sent==[]
    finally:
        release.set()
        if thread is not None:thread.join(4);assert not thread.is_alive()
        built['live']._safety_pool.shutdown(wait=True)


def test_identity_recheck_error_has_bounded_grace_and_expiry_alert(monkeypatch,tmp_path):
    import test_memecoin_rh_inline_rotation as F
    s,hosts,sent,alerts,logs,clock,_=F.build()
    h=hosts[0]
    h.identity_probe=lambda:(200,{'error':{'code':-32000}})
    clock.t+=601;s.admit_pass()
    assert h.admission=='admitted'and s.pick()is h
    clock.t+=1200;s.admit_pass();F.settle()
    assert h.admission=='pending'and s.pick()is hosts[1]
    assert sum('EXPIRED'in text for text in alerts)==1
    s.admit_pass();F.settle()
    assert sum('EXPIRED'in text for text in alerts)==1
    h.identity_probe=lambda:(200,{'result':'0x1237'})
    s.admit_pass()
    assert h.admission=='admitted'and s.pick()is h



@pytest.mark.parametrize('state',['pending','refused'])
def test_every_real_direct_and_set_seam_blocks_unadmitted_host(monkeypatch,tmp_path,state):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_RH_FAST_INLINE_WARM',True)
    monkeypatch.setattr(cfg,'MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S',0.)
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    try:
        s=built['inline_paced']
        for h in s.hosts:
            h.admission=state
            for fn in (h.safety_fn,h.facts_fn,h.warm_safety_fn,h.warm_facts_fn):
                assert fn is not None
                _call(fn,'0x'+'22'*20)
        for fn in (s.safety,s.facts,s.warm_safety,s.warm_facts):_call(fn,'0x'+'22'*20)
        assert sent==[]
    finally:built['live']._safety_pool.shutdown(wait=True)


@pytest.mark.parametrize('rejoin',[False,True])
def test_inflight_identity_answer_cannot_cross_new_quota_generation(rejoin):
    import test_memecoin_rh_inline_rotation as F
    s,hosts,sent,alerts,logs,clock,_=F.build();h=hosts[0]
    entered=threading.Event();release=threading.Event()
    def probe():
        entered.set();assert release.wait(3)
        return 200,{'result':'0x1237'}
    h.identity_probe=probe;clock.t+=601
    t=threading.Thread(target=s.admit_pass);t.start()
    try:
        assert entered.wait(3)
        h.paced.note_answer(F.QUOTA[0],F.QUOTA[1])
        if rejoin:
            h.paced._quota_probe_at=clock.t
            h.paced.quota_probe=lambda:(200,{'result':'0x123'})
        s.quota_recover()
        assert h.admission=='pending'
        release.set();t.join(4);assert not t.is_alive()
        assert h.admission=='pending'
    finally:
        release.set();t.join(4);F.settle()
import pytest

from test_memecoin_rh_inline_rotation import _lane_with_two_hosts,_call

MINT='0x'+'22'*20

@pytest.mark.parametrize("reply", [(500, {}), (200, None), (200, {}), (200, {"result": "bad"}), None])
def test_failed_revalidation_revokes_actual_token_read_paths(monkeypatch,tmp_path,reply):
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    try:
        s=built['inline_paced'];t=[1000.];s.clock=lambda:t[0];s.admit_pass();sent.clear()
        t[0]+=1801.
        def probe():
            if reply is None:
                raise TimeoutError('identity unavailable')
            return reply
        for h in s.hosts:
            h.identity_probe = probe
        s.admit_pass()
        for h in s.hosts:
            _call(h.safety_fn,MINT);_call(h.facts_fn,MINT)
        assert sent==[]
        assert s.pick() is None
    finally:built['live']._safety_pool.shutdown(wait=True)

def test_identity_response_with_error_is_unknown(monkeypatch,tmp_path):
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    try:
        s=built['inline_paced']
        for h in s.hosts:h.identity_probe=lambda:(200,{'result':'0x1237','error':{'code':-32000}})
        s.admit_pass()
        for h in s.hosts:_call(h.facts_fn,MINT)
        assert sent==[] and s.pick() is None
    finally:built['live']._safety_pool.shutdown(wait=True)

def test_quota_clear_cannot_open_a_read_before_rejoin_admission(monkeypatch,tmp_path):
    built,sent=_lane_with_two_hosts(monkeypatch,tmp_path,{})
    try:
        s=built['inline_paced'];s.admit_pass();sent.clear();h=s.hosts[0]
        h.paced._quota=True
        def recovery():
            h.paced._quota=False
            _call(h.facts_fn,MINT)  # the other thread observes the cleared latch before this probe returns
        monkeypatch.setattr(h.paced,'quota_recover',recovery)
        s.quota_recover()
        assert sent==[] and h.admission=='pending'
    finally:built['live']._safety_pool.shutdown(wait=True)


def test_revalidation_blocks_reads_while_probe_is_pending(monkeypatch, tmp_path):
    built, sent = _lane_with_two_hosts(monkeypatch, tmp_path, {})
    try:
        s = built['inline_paced']; now = [1000.]; s.clock = lambda: now[0]
        s.admit_pass(); sent.clear(); now[0] += 1801.
        h = s.hosts[0]
        def probe():
            _call(h.facts_fn, MINT)
            return 200, {'result': '0x1237'}
        h.identity_probe = probe
        s.admit_pass()
        assert all(method == 'eth_chainId' for _, method in sent) and h.admission == 'admitted'
    finally:
        built['live']._safety_pool.shutdown(wait=True)


def test_pre_quota_identity_result_cannot_readmit_recovered_host(monkeypatch, tmp_path):
    built, sent = _lane_with_two_hosts(monkeypatch, tmp_path, {})
    try:
        s = built['inline_paced']; h = s.hosts[0]
        def recovery():
            h.paced._quota = False
        monkeypatch.setattr(h.paced, 'quota_recover', recovery)
        def probe():
            h.paced.note_answer(403, {'error': {'message': 'monthly quota exceeded'}})
            assert h.paced.quota_spent()
            s.quota_recover()
            return 200, {'result': '0x1237'}
        h.identity_probe = probe
        s.admit_pass(); sent.clear()
        _call(h.facts_fn, MINT)
        assert sent == [] and h.admission == 'pending'
    finally:
        built['live']._safety_pool.shutdown(wait=True)
