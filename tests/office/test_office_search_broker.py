import sqlite3
"""Offline witnesses for shared search state; never contact an upstream."""
import json
import multiprocessing as mp
import time

from sniper.office import search_broker as S


def broker(tmp_path, **kw):
    policy=tmp_path/'policy.json'
    if not policy.exists():policy.write_text('{"daily_budget":30}')
    return S.Broker(tmp_path/'search.db',policy,**kw)


def test_cache_exact_empty_age_and_failures(tmp_path):
    now=[100.]
    b=broker(tmp_path,clock=lambda:now[0]);calls=[]
    def fetch():calls.append(1);return dict(state='known',results=[])
    first=b.search('AbC',fetch);now[0]+=3
    hit=b.search('AbC',fetch)
    assert first['cached'] is False and hit['cached'] is True and hit['age_s']==3
    assert hit['results']==[] and len(calls)==1
    b.search('abc',fetch);assert len(calls)==2
    bad=b.search('failed',lambda:dict(state='unknown',reason='timeout'))
    assert bad['reason']=='timeout' and 'results' not in bad


def test_shared_budget_pacing_rollover_and_unknown(tmp_path):
    now=[86400.]
    a=broker(tmp_path,clock=lambda:now[0]);b=broker(tmp_path,clock=lambda:now[0])
    assert a.admit('API.TAVILY.COM.') is None
    assert b.admit('api.tavily.com')=='search_paced'
    for n in range(29):
        now[0]+=6;assert (a if n%2 else b).admit('api.tavily.com') is None
    now[0]+=6;assert b.admit('bing.com')=='budget_exhausted'
    now[0]=172800.;assert b.admit('bing.com') is None
    now[0]=86400.;assert a.admit('duckduckgo.com')=='search_clock_unknown'
    (tmp_path/'policy.json').write_text('{}')
    assert a.admit('bing.com')=='search_config_unknown'


def _search_worker(root, start, queue):
    from pathlib import Path
    b=broker(Path(root));start.wait()
    def fetch():
        with open(Path(root)/'calls','a') as f:f.write('fetch\n')
        time.sleep(.2)
        return dict(state='known',results=[])
    queue.put(b.search('same',fetch))


def test_singleflight_five_processes(tmp_path):
    broker(tmp_path)
    ctx=mp.get_context('spawn');start=ctx.Event();queue=ctx.Queue()
    workers=[ctx.Process(target=_search_worker,args=(str(tmp_path),start,queue)) for _ in range(5)]
    for p in workers:p.start()
    start.set();answers=[queue.get(timeout=15) for _ in workers]
    for p in workers:p.join(10);assert p.exitcode==0
    assert (tmp_path/'calls').read_text().splitlines()==['fetch']
    assert all(a['state']=='known' for a in answers)


def test_proxy_guard_is_shared_and_funded_deny_wins(tmp_path):
    from sniper.office.egress_proxy import Proxy
    a=Proxy('unused',peer_uid=0,deny={'bing.com'},ca=object(),search_broker=broker(tmp_path))
    b=Proxy('unused2',peer_uid=0,deny=set(),ca=object(),search_broker=broker(tmp_path))
    assert a.decide('bing.com')=='denied_funded_lane'
    assert a.decide('api.tavily.com',count=False) is None
    assert a.decide('api.tavily.com') is None
    assert b.decide('api.tavily.com')=='search_paced'
    assert b.decide('notbing.com') is None
    unconfigured=Proxy('unused3',peer_uid=0,deny=set(),ca=object())
    assert unconfigured.decide('www.bing.com')=='search_config_unknown'


def test_all_fetcher_hosts_are_guarded():
    from sniper.office.seat_search import ENGINES
    from urllib.parse import urlsplit
    assert all(S.provider(urlsplit(url).hostname) for _,url,_,_ in ENGINES)


def test_retention_and_crashed_lease_fencing(tmp_path):
    b=broker(tmp_path,clock=lambda:1000000.)
    with b._conn() as c:
        c.execute("INSERT INTO cache VALUES('old','dead',0,NULL)")
        for i in range(1002):c.execute('INSERT INTO cache VALUES(?,?,?,?)',(str(i),'x',999999.,'{"state":"known","results":[]}'))
    b.prune()
    with b._conn() as c:
        assert c.execute('SELECT count(*) FROM cache').fetchone()[0]==1000
        assert c.execute("SELECT 1 FROM cache WHERE key='old'").fetchone() is None
    import hashlib
    key=hashlib.sha256(b'keyless-v1\0recover').hexdigest()
    with b._conn() as c:c.execute('INSERT INTO cache VALUES(?,?,?,NULL)',(key,'crashed',999000.))
    assert b.search('recover',lambda:dict(state='known',results=[]))['state']=='known'


def test_failure_types_and_challenge_never_become_empty(monkeypatch):
    import urllib.error
    from sniper.office import seat_search as F
    for code,reason in [(429,'rate_limited'),(403,'http_403')]:
        def fail(*a):raise urllib.error.HTTPError('https://x',code,'no',{},None)
        monkeypatch.setattr(F,'_fetch',fail)
        assert set(F.search('q')['failed'].values())=={reason}
    monkeypatch.setattr(F,'_fetch',lambda *a:'<form id="challenge-form">captcha</form>')
    assert F.search('q')['failed']['duckduckgo']=='challenge'
    def refused(*a):raise urllib.error.HTTPError('https://x',403,'no',{'X-Office-Refusal':'budget_exhausted'},None)
    monkeypatch.setattr(F,'_fetch',refused)
    out=F.search('q');assert out['reason']=='budget_exhausted' and 'results' not in out


def test_proxy_actual_http_request_refused_before_dial(tmp_path,monkeypatch):
    import pytest
    from sniper.office import egress_proxy as P,netguard
    policy=tmp_path/'policy.json';policy.write_text('{"daily_budget":0}')
    p=P.Proxy('unused',peer_uid=0,deny=set(),ca=object(),search_broker=broker(tmp_path))
    class Client:
        def recv(self,n):return b'POST /search HTTP/1.1\r\nHost: api.tavily.com\r\nContent-Length: 0\r\n\r\n'
    monkeypatch.setattr(netguard,'connect',lambda *a:pytest.fail('upstream dial bypassed budget'))
    with pytest.raises(netguard.Denied,match='budget_exhausted'):p._request(Client(),'api.tavily.com',None)


def _admit_worker(root,start,queue):
    from pathlib import Path
    start.wait();queue.put(broker(Path(root)).admit('api.tavily.com'))


def test_budget_one_atomic_across_five_processes(tmp_path):
    (tmp_path/'policy.json').write_text('{"daily_budget":1}')
    ctx=mp.get_context('spawn');start=ctx.Event();queue=ctx.Queue()
    workers=[ctx.Process(target=_admit_worker,args=(str(tmp_path),start,queue)) for _ in range(5)]
    for p in workers:p.start()
    start.set();answers=[queue.get(timeout=15) for _ in workers]
    for p in workers:p.join(10);assert p.exitcode==0
    assert answers.count(None)==1 and answers.count('budget_exhausted')==4


def test_expired_owner_cannot_overwrite_new_result(tmp_path):
    now=[100.];a=broker(tmp_path,clock=lambda:now[0]);b=broker(tmp_path,clock=lambda:now[0])
    def old_fetch():
        now[0]+=121
        assert b.search('q',lambda:dict(state='known',results=[],engine='new'))['state']=='known'
        return dict(state='known',results=[],engine='old')
    assert a.search('q',old_fetch)['reason']=='search_lease_lost'
    assert b.search('q',lambda:None)['engine']=='new'


def test_missing_policy_and_store_failure_do_not_fetch(tmp_path):
    calls=[];b=S.Broker(tmp_path/'search.db',tmp_path/'missing')
    assert b.search('q',lambda:calls.append(1))['reason']=='search_config_unknown'
    (tmp_path/'policy.json').write_text('{"daily_budget":30}')
    (tmp_path/'broken').write_text('not sqlite')
    b=S.Broker(tmp_path/'broken',tmp_path/'policy.json')
    assert b.search('q',lambda:calls.append(1))['reason']=='search_store_unknown' and not calls


def test_corrupt_budget_never_grants_a_fresh_allowance(tmp_path):
    b=broker(tmp_path)
    with b._conn() as c:c.execute('INSERT INTO budget VALUES(1,0,-30,0)')
    assert b.admit('api.tavily.com')=='search_store_unknown'


def test_hourly_retention_calls_the_same_shared_cache(tmp_path,monkeypatch):
    from sniper.office import seat_retention as R
    cfg=dict(office_db=str(tmp_path/'office.db'),search_policy=str(tmp_path/'policy.json'),proxy_log_dir=str(tmp_path),state_dir=str(tmp_path))
    b=S.from_config(cfg)
    with b._conn() as c:c.execute("INSERT INTO cache VALUES('expired','dead',0,NULL)")
    monkeypatch.setattr(R,'prune_events',lambda *a,**k:0)
    monkeypatch.setattr(R,'prune_logs',lambda *a,**k:0)
    monkeypatch.setattr(R,'prune_sessions',lambda *a,**k:True)
    monkeypatch.setattr(R.Q,'prune',lambda *a,**k:0)
    assert 'search_pruned=True' in R.run(cfg,sqlite3.connect(':memory:'),None,now=1000000.)
    with b._conn() as c:assert c.execute('SELECT count(*) FROM cache').fetchone()[0]==0


def test_all_role_configs_use_one_global_search_policy_and_store(tmp_path):
    from pathlib import Path
    configs=[json.loads(Path('config/office/seat_%s_v2.json'%role).read_text()) for role in ('head','judge','scout')]
    policy='/home/bigpapi/Desktop/Sniper/config/office/search.json'
    assert all(cfg.get('search_policy')==policy for cfg in configs)
    assert len({str(S.from_config(cfg).path) for cfg in configs})==1
    assert json.loads(Path('config/office/search.json').read_text())['daily_budget']==500
    # Exercise the same configuration seam against private fixture paths only.
    fixture_policy=tmp_path/'policy.json';fixture_policy.write_text('{"daily_budget":1}')
    brokers=[S.from_config(dict(cfg,office_db=str(tmp_path/'office.db'),search_policy=str(fixture_policy))) for cfg in configs]
    assert brokers[0].admit('api.tavily.com') is None
    assert all(b.admit('bing.com')=='budget_exhausted' for b in brokers[1:])


def test_a_challenge_or_429_stops_every_query_and_a_partial_failure_does_not(tmp_path):
    from sniper.office.search_broker import Broker
    policy=tmp_path/'p.json';policy.write_text('{"daily_budget":500}')
    clock=[1000.];b=Broker(tmp_path/'s.db',policy,clock=lambda:clock[0])
    assert b.search('fine',lambda:dict(state='known',results=[],failed=dict(duckduckgo='challenge')))['state']=='known'
    assert b.search('a',lambda:dict(state='unknown',reason='result_markup_absent'))['reason']=='result_markup_absent'
    assert b.search('b',lambda:dict(state='unknown',reason='timeout',failed=dict(ddg='challenge',bing='timeout')))['reason']=='timeout'
    assert b.search('c',lambda:1/0)['reason']=='search_stopped' and b.admit('bing.com')=='search_stopped'
    clock[0]+=901
    assert b.search('c',lambda:dict(state='known',results=[]))['state']=='known'
