import importlib.util
import json
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def campaign_api(monkeypatch):
    import sys
    import types
    module=types.ModuleType('sniper.office.wallet_campaign')
    module.materialize_campaigns=lambda *args,**kwargs:dict(state='known',changed=0,items=[])
    monkeypatch.setitem(sys.modules,'sniper.office.wallet_campaign',module)
    module.links=types.ModuleType('sniper.office.wallet_link_backfill')
    module.links.expand_existing_links=lambda *args,**kwargs:dict(state='partial',added=0)
    monkeypatch.setitem(sys.modules,'sniper.office.wallet_link_backfill',module.links)
    module.groups=types.ModuleType('sniper.office.wallet_group_leads')
    module.groups.scan_group_leads=lambda *args,**kwargs:dict(state='partial',items=[],changed=0)
    monkeypatch.setitem(sys.modules,'sniper.office.wallet_group_leads',module.groups)
    return module


def test_enabled_collector_materializes_local_family_even_when_rpc_is_capped(tmp_path, monkeypatch, capsys,campaign_api):
    from sniper.office import wallet_watch_collect as C, wallet_family as F
    root = tmp_path; office = root / 'data/office'; (office / 'v2').mkdir(parents=True)
    (office / 'wallet_network.db').touch(); (office / 'v2/chain.db').touch()
    calls = []
    def links(*args,**kwargs):calls.append('links');return dict(state='partial',added=2)
    def groups(*args,**kwargs):calls.append('groups');return dict(state='partial',items=[],changed=3)
    campaign_api.links.expand_existing_links=links;campaign_api.groups.scan_group_leads=groups
    from contextlib import contextmanager
    from sniper.office import chain_data as D
    lifetime=[]
    class FakeChainData(D.ChainData):
        @contextmanager
        def stream_store_lifetime(self):
            lifetime.append('enter')
            try:yield
            finally:lifetime.append('exit')
    monkeypatch.setattr(D,'ChainData',FakeChainData)
    class FakeCollector:
        def __init__(self, *args): pass
        def consume_flows(self, **kwargs):
            assert kwargs == {'limit': 499}
            assert lifetime==['enter']
            calls.append('flows'); return dict(state='unknown', reason='flow_tape_unknown', rows=0)
        def consume_setups(self):
            calls.append('setups'); return dict(state='known', added=0)
        def tick(self, **kwargs):
            calls.append('rpc'); return dict(state='unknown', reason='credit_cap', pending=0, wallets=[])
    def materialize(*args, **kwargs):
        calls.append('family')
        return dict(state='known', reason=None, items=[dict(early=True)])
    monkeypatch.setattr(C, 'Collector', FakeCollector)
    monkeypatch.setattr(F, 'materialize_candidates', materialize)
    spec = importlib.util.spec_from_file_location('watch_cli_family', Path(__file__).resolve().parents[2] / 'scripts/office_wallet_watch_collect.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    assert cli.main(['--enabled', '--state-root', str(root), '--research-env-file', str(root / 'nonexistent.env'), '--ticks', '1']) == 0
    assert calls == ['flows', 'setups', 'links','groups','family', 'rpc']
    assert lifetime==['enter','exit']
    saved = json.loads((office / 'wallet_watch_collect.heartbeat').read_text())
    assert saved['poll']['reason'] == 'credit_cap'
    assert saved['families']['candidates'] == 1 and saved['families']['early'] == 1
    assert saved['links']['added']==2 and saved['groups']['changed']==3
    assert 'nonexistent.env' not in capsys.readouterr().out


@pytest.mark.parametrize('flow_state',['partial','unknown'])
def test_local_writers_defer_during_enrichment_and_resume_after_failure(tmp_path,monkeypatch,campaign_api,flow_state):
    import threading
    from sniper.office import wallet_watch_collect as C,wallet_family as F
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    active=threading.Event();turns=[];deferred=[]
    class FakeCollector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):
            assert not active.is_set()
            turns.append(1)
            return dict(state=flow_state,rows=0,cursor=len(turns))
        def consume_setups(self):assert not active.is_set();return dict(state='known',added=0)
        def tick(self,**kw):return dict(state='known',wallets=[])
    def scan(*a,**kw):
        active.set()
        threading.Event().wait(.04)
        active.clear()
        raise RuntimeError('private-provider-detail')
    campaign_api.groups.scan_group_leads=scan
    monkeypatch.setattr(C,'Collector',FakeCollector)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a,**kw:dict(state='known',items=[]))
    spec=importlib.util.spec_from_file_location('fair_research_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    from sniper.util import atomic
    original=atomic.atomic_write_json
    def capture(path,status):
        if status.get('local_scheduler',{}).get('state')=='deferred':deferred.append(dict(status))
        original(path,status)
    monkeypatch.setattr(atomic,'atomic_write_json',capture)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--ticks','3','--interval','.005'])==0
    assert len(turns)==3 and deferred
    assert all(s['flows']['cursor']==1 and s['flows']['state']==flow_state and 'raw_frontier' not in s['flows'] and s['local_result_at']<=s['ts'] for s in deferred)
    saved=json.loads((office/'wallet_watch_collect.heartbeat').read_text())
    assert saved['groups']['reason']=='enrichment_failed'
    assert 'private-provider-detail' not in json.dumps(saved)


def test_enrichment_has_fair_turns_even_with_sustained_raw_backlog(tmp_path,monkeypatch,campaign_api):
    from concurrent.futures import Future
    from sniper.office import wallet_watch_collect as C,wallet_family as F
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    turns=[];scans=[]
    class InlinePool:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def submit(self,fn):
            result=Future();result.set_result(fn());return result
    class FakeCollector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):turns.append(1);return dict(state='partial',cursor=len(turns),raw_frontier=1000,caught_up=False)
        def consume_setups(self):return dict(state='known',added=0)
        def tick(self,**kw):return dict(state='known',wallets=[])
    def scan(*a,**kw):scans.append(len(turns));return dict(state='partial',inserted=1,has_more=True)
    campaign_api.groups.scan_group_leads=scan
    monkeypatch.setattr(C,'Collector',FakeCollector)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a,**kw:dict(state='known',items=[]))
    spec=importlib.util.spec_from_file_location('fair_backlog_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    monkeypatch.setattr(cli,'ThreadPoolExecutor',InlinePool)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--ticks','22','--interval','.001'])==0
    assert scans==[1,11,21]


def test_continuous_local_detection_does_not_wait_for_rpc(tmp_path, monkeypatch):
    import threading
    from sniper.office import wallet_watch_collect as C, wallet_family as F
    root = tmp_path; office = root / 'data/office'; (office / 'v2').mkdir(parents=True)
    (office / 'wallet_network.db').touch(); (office / 'v2/chain.db').touch()
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    local = []
    class FakeCollector:
        def __init__(self, *args): pass
        def consume_flows(self, **kwargs):
            assert kwargs == {'limit': 499}
            return dict(state='unknown', reason='flow_tape_unknown', rows=0)
        def consume_setups(self): return dict(state='known', added=0)
        def tick(self, **kwargs):
            started.set(); release.wait(.5); returned.set()
            return dict(state='known', reason=None, wallets=[])
    def materialize(*args, **kwargs):
        local.append(1)
        if len(local) == 2:
            assert started.wait(1)
            assert not returned.is_set(), 'slow RPC blocked local family detection'
            release.set()
        return dict(state='known', items=[])
    monkeypatch.setattr(C, 'Collector', FakeCollector)
    monkeypatch.setattr(F, 'materialize_candidates', materialize)
    spec = importlib.util.spec_from_file_location('watch_cli_async', Path(__file__).resolve().parents[2] / 'scripts/office_wallet_watch_collect.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    try:
        assert cli.main(['--enabled', '--state-root', str(root), '--research-env-file', str(root / 'absent.env'), '--ticks', '2']) == 0
    finally:
        release.set()
    assert len(local) == 2


def test_rpc_error_recovers_without_overlap_or_leaking_provider_text(tmp_path, monkeypatch, capsys):
    import threading
    from sniper.office import wallet_watch_collect as C, wallet_family as F
    office = tmp_path / 'data/office'; (office / 'v2').mkdir(parents=True)
    (office / 'wallet_network.db').touch(); (office / 'v2/chain.db').touch()
    first_done, second_done = threading.Event(), threading.Event()
    guard = threading.Lock(); state = dict(calls=0, active=0, peak=0, local=0)
    class FakeCollector:
        def __init__(self, *args): pass
        def consume_flows(self, **kwargs):
            assert kwargs == {'limit': 499}
            return dict(state='unknown', reason='flow_tape_unknown', rows=0)
        def consume_setups(self): return dict(state='known', added=0)
        def tick(self, **kwargs):
            with guard:
                state['calls'] += 1; n = state['calls']; state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            try:
                if n == 1: raise RuntimeError('secret-provider-url')
                return dict(state='known', reason=None, wallets=[])
            finally:
                with guard: state['active'] -= 1
                (first_done if n == 1 else second_done).set()
    def materialize(*args, **kwargs):
        state['local'] += 1
        if state['local'] in (2, 3):
            assert (first_done if state['local'] == 2 else second_done).wait(2)
            threading.Event().wait(.02)
        return dict(state='known', items=[])
    monkeypatch.setattr(C, 'Collector', FakeCollector)
    monkeypatch.setattr(F, 'materialize_candidates', materialize)
    spec = importlib.util.spec_from_file_location('watch_cli_recovery', Path(__file__).resolve().parents[2] / 'scripts/office_wallet_watch_collect.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    assert cli.main(['--enabled', '--state-root', str(tmp_path), '--research-env-file', str(tmp_path / 'absent.env'), '--ticks', '3']) == 0
    assert state['peak'] == 1 and state['local'] == 3 and state['calls'] >= 2
    output = capsys.readouterr().out
    assert 'poll_failed' in output and 'secret-provider-url' not in output
    assert json.loads((office / 'wallet_watch_collect.heartbeat').read_text())['poll']['state'] == 'known'


def test_local_loop_repairs_a_committed_watch_with_missing_export(tmp_path, monkeypatch):
    from sniper.office import wallet_watch_collect as C, wallet_family as F
    from sniper.office import wallet_watch as W
    from sniper.office.wallet_network import WalletNetwork
    office = tmp_path / 'data/office'; (office / 'v2').mkdir(parents=True)
    (office / 'wallet_network.db').touch(); (office / 'v2/chain.db').touch()
    desk = tmp_path / 'data/desk'; (desk / 'pumpswap').mkdir(parents=True)
    net = WalletNetwork(office / 'wallet_network.db')
    with net._conn() as c:
        c.executescript(W.SCHEMA)
        c.execute('INSERT INTO watch_list VALUES(?,?,?,?,?,?,NULL)', ('1' * 40, None, 'committed before crash', 'host', 'test', 100.))
    class FakeCollector:
        def __init__(self, *args): pass
        def consume_flows(self, **kwargs):
            assert kwargs == {'limit': 499}
            return dict(state='unknown', reason='flow_tape_unknown', rows=0)
        def consume_setups(self): return dict(state='known', added=0)
        def tick(self, **kwargs): return dict(state='known', wallets=[])
    monkeypatch.setattr(C, 'Collector', FakeCollector)
    monkeypatch.setattr(F, 'materialize_candidates', lambda *args: dict(state='known', items=[]))
    spec = importlib.util.spec_from_file_location('watch_cli_repair', Path(__file__).resolve().parents[2] / 'scripts/office_wallet_watch_collect.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    assert cli.main(['--enabled', '--state-root', str(tmp_path), '--research-env-file', str(tmp_path / 'absent.env')]) == 0
    assert json.loads(W.watch_file(desk).read_text())['wallets'] == ['1' * 40]


def test_preparation_materializer_runs_without_any_buy_candidate(tmp_path,monkeypatch,campaign_api):
    from sniper.office import wallet_watch_collect as C,wallet_family as F
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    calls=[]
    class FakeCollector:
        def __init__(self,*args):pass
        def consume_flows(self,**kwargs):calls.append('flows');return dict(state='partial',rows=0)
        def consume_setups(self):calls.append('setups');return dict(state='known',rows=0)
        def tick(self,**kwargs):calls.append('rpc');return dict(state='unknown',reason='credit_cap',wallets=[])
    def preparation(*args,**kwargs):calls.append('preparation');return dict(state='known',changed=1,items=[dict(status='mint_unresolved')])
    def family(*args,**kwargs):calls.append('family');return dict(state='known',items=[])
    monkeypatch.setattr(C,'Collector',FakeCollector);monkeypatch.setattr(F,'materialize_candidates',family);campaign_api.materialize_campaigns=preparation
    spec=importlib.util.spec_from_file_location('campaign_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env')])==0
    assert calls==['flows','setups','preparation','family','rpc']
    saved=json.loads((office/'wallet_watch_collect.heartbeat').read_text())
    assert saved['campaigns']['changed']==1 and saved['families']['candidates']==0


def test_background_enrichment_starts_after_local_writes(tmp_path,monkeypatch,campaign_api):
    from sniper.office import wallet_watch_collect as C,wallet_family as F,wallet_watch as W
    import threading
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    exported=threading.Event();finished=threading.Event();observed=[]
    class FakeCollector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):return dict(state='partial',rows=0)
        def consume_setups(self):return dict(state='known',added=0)
        def tick(self,**kw):return dict(state='known',wallets=[])
    def sync(*a):
        exported.set();threading.Event().wait(.03);finished.set()
    def links(*a,**kw):
        assert a[0].busy_timeout==.1
        assert exported.wait(1)
        observed.append(finished.is_set())
        return dict(state='partial',added=0)
    monkeypatch.setattr(C,'Collector',FakeCollector)
    monkeypatch.setattr(W,'_sync',sync)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a:dict(state='known',items=[]))
    campaign_api.links.expand_existing_links=links
    spec=importlib.util.spec_from_file_location('enrichment_order_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--ticks','2','--interval','.01'])==0
    assert observed and all(observed)


def test_job_only_turn_preserves_last_actual_regular_snapshot_and_time(tmp_path,monkeypatch):
    from concurrent.futures import Future
    from sniper.office import wallet_watch_collect as C,wallet_family as F,wallet_receipt_jobs as J
    from sniper.util import atomic
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True);(office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    saved=[];turns=[];write=atomic.atomic_write_json
    def capture(path,status):saved.append(dict(status));write(path,status)
    class Pool:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def submit(self,fn):f=Future();f.set_result(fn());return f
    class Collector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):return dict(state='partial',rows=0)
        def consume_setups(self):return dict(state='known',added=0)
        def tick(self,**kw):raise AssertionError('job scheduler must own the existing remote poll budget')
    def poll(chain,collector,path,*,max_calls):
        turns.append(1)
        return (dict(state='known',wallets=[dict(address='actual-snapshot')]) if len(turns)==1 else None,
                dict(state='partial',reason='receipt_job_processed',pending=True,calls_attempted=1))
    monkeypatch.setattr(C,'Collector',Collector);monkeypatch.setattr(J,'poll',poll)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a,**kw:dict(state='known',items=[]));monkeypatch.setattr(atomic,'atomic_write_json',capture)
    spec=importlib.util.spec_from_file_location('receipt_jobs_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli);monkeypatch.setattr(cli,'ThreadPoolExecutor',Pool)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--ticks','3','--interval','.001'])==0
    assert saved[1]['poll']['wallets']==saved[2]['poll']['wallets']==[dict(address='actual-snapshot')]
    assert saved[1]['poll_result_at']==saved[2]['poll_result_at']
    assert saved[2]['receipt_job_asof']>saved[1]['receipt_job_asof']


def test_cli_reuses_successful_flow_export_instead_of_second_writer(tmp_path,monkeypatch,campaign_api):
    from sniper.office import wallet_watch_collect as C,wallet_watch as W,wallet_family as F
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    class FakeCollector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):
            assert kw=={'limit':499}
            return dict(state='partial',rows=499,raw_caught_up=False,watch_export=dict(state='known',asof=123.))
        def consume_setups(self):return dict(state='known',added=0)
        def tick(self,**kw):return dict(state='unknown',reason='paced')
    monkeypatch.setattr(C,'Collector',FakeCollector)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a,**kw:dict(state='known',items=[]))
    def unexpected(*a):raise AssertionError('duplicate export writer')
    monkeypatch.setattr(W,'_sync',unexpected)
    spec=importlib.util.spec_from_file_location('reuse_export_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--ticks','2'])==0
    saved=json.loads((office/'wallet_watch_collect.heartbeat').read_text())
    assert saved['watch_export']==dict(state='known',asof=123.)
    assert set(saved['local_stage_s'])>={'flows','setups','export','campaigns','families'}


def test_metadata_only_turn_preserves_regular_and_receipt_snapshot_times(tmp_path,monkeypatch,campaign_api):
    from sniper.office import wallet_watch_collect as C,wallet_metadata_jobs as M,wallet_family as F
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    (office/'wallet_network.db').touch();(office/'v2/chain.db').touch()
    class FakeCollector:
        def __init__(self,*a):pass
        def consume_flows(self,**kw):return dict(state='partial',rows=0)
        def consume_setups(self):return dict(state='known',added=0)
    monkeypatch.setattr(C,'Collector',FakeCollector)
    monkeypatch.setattr(F,'materialize_candidates',lambda *a,**kw:dict(state='known',items=[]))
    calls=[]
    def poll(*a,**kw):
        calls.append(kw)
        assert kw['slots']==9
        return None,None,dict(state='partial',reason='metadata_slot_recovered',slot=1,calls_attempted=1)
    monkeypatch.setattr(M,'poll',poll)
    spec=importlib.util.spec_from_file_location('metadata_cli_snapshot',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file',str(tmp_path/'absent.env'),'--metadata-repair-slots','9','--ticks','1'])==0
    saved=json.loads((office/'wallet_watch_collect.heartbeat').read_text())
    assert len(calls)==1
    assert saved['poll_result_at'] is None and saved['receipt_job_asof'] is None
    assert saved['poll']['reason']=='not_polled' and saved['receipt_jobs']['reason']=='not_polled'
    assert saved['metadata_recovery_asof'] is not None and saved['metadata_recovery']['slot']==1
