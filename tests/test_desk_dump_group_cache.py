import importlib
import json


def test_cache_client_exists():
    assert importlib.util.find_spec('sniper.services.memecoin_desk_dump_group_cache') is not None


def test_cache_identity_freshness_partial_and_kill_knob(tmp_path):
    from sniper.services import memecoin_desk_dump_group_cache as C
    from sniper.services import memecoin_desk_dump_group as D
    path=tmp_path/'cache.json'
    cache=C.SnapshotCache(path)
    assert cache.lookup(mint='m',pool='p',now=100)['status']=='unknown'
    score=dict(positive_proven=True,target_complete=True,exclusions_valid=True,status='kill',reason='dump_group_buyers',buyer_share=.5,buy_sol_share=.8,
               matched_buyers=4,buyers=8,mint='m',pool='p',venue='curve')
    C.publish_cache(path,{'p':score},coverage={'curve':'partial','pumpswap':'partial'},cutoff=99,published_at=100)
    cache.reload()
    got=cache.lookup(mint='m',pool='p',now=100)
    assert got['status']=='kill' and got['coverage']['curve']['complete'] is False
    assert cache.lookup(mint='wrong',pool='p',now=100)['status']=='unknown'
    assert cache.lookup(mint='m',pool='p',now=106)['status']=='unknown'
    assert C.SnapshotCache(path,enabled=False).lookup(mint='m',pool='p',now=100)['reason']=='disabled'
    # Below threshold + incomplete history stays unknown, never clean.
    score.update(status='pass',reason='below_threshold',buyer_share=0,buy_sol_share=0)
    C.publish_cache(path,{'p':score},coverage={'curve':'partial','pumpswap':'partial'},cutoff=99,published_at=100)
    cache.reload()
    assert cache.lookup(mint='m',pool='p',now=100)['status']=='unknown'
    path.write_text('{')
    cache.reload()
    assert cache.lookup(mint='m',pool='p',now=100)['status']=='unknown'


def test_lookup_never_opens_files_or_starts_network(tmp_path,monkeypatch):
    from sniper.services import memecoin_desk_dump_group_cache as C
    path=tmp_path/'cache.json';cache=C.SnapshotCache(path)
    def refuse(*a,**k):raise AssertionError('I/O in entry lookup')
    monkeypatch.setattr(type(path),'open',refuse)
    for _ in range(100):assert cache.lookup(mint='m',pool='p',now=100)['status']=='unknown'


def test_assembler_consumes_only_supplied_memory_cache():
    from test_memecoin_desk_assembler import A,emission,SAFE
    class Cache:
        def lookup(self,**kw):
            return dict(status='kill',reason='dump_group_buyers',**kw)
    c=emission(100)
    got=A.assemble(c,safety=SAFE,cluster=None,probe={'status':'pass'},now=100,dump_cache=Cache())
    assert got['checks']['dump_group']['reason']=='dump_group_buyers'
    assert got['checks']['dump_group']['pool']==c['pool']


def test_actual_published_fact_can_block_contract(tmp_path):
    from test_memecoin_desk_contract import candidate,evidence,policy,AT
    from sniper.services import memecoin_desk_contract as D
    from sniper.services import memecoin_desk_dump_group_cache as C
    from datetime import datetime
    now=datetime.fromisoformat(AT).timestamp()
    c=candidate();e=evidence(c)
    path=tmp_path/'cache.json'
    score=dict(positive_proven=True,target_complete=True,exclusions_valid=True,status='kill',reason='dump_group_buyers',buyer_share=.5,buy_sol_share=.8,
               matched_buyers=4,buyers=8,mint=c['mint'],pool=c['pool'],venue='curve')
    C.publish_cache(path,{c['pool']:score},coverage={'curve':{'complete':False,'holes':[[1,2]]}},cutoff=now,published_at=now)
    cache=C.SnapshotCache(path);cache.reload()
    e['checks']['dump_group']=cache.lookup(mint=c['mint'],pool=c['pool'],now=now)
    assert D.hard_filter(c,evidence=e,policy=policy(),evaluated_at=AT)['reason']=='dump_group_buyers'


def test_positive_share_without_predicate_or_target_proof_is_unknown(tmp_path):
    from sniper.services import memecoin_desk_dump_group_cache as C
    p=tmp_path/'cache.json'
    score=dict(mint='m',pool='p',status='kill',reason='dump_group_buyers',buyer_share=.5,buy_sol_share=.8)
    C.publish_cache(p,{'p':score},coverage={},cutoff=100,published_at=100)
    c=C.SnapshotCache(p);c.reload()
    assert c.lookup(mint='m',pool='p',now=100)['status']=='unknown'
