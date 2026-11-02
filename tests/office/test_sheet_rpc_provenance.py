"""Fallback provenance survives known and unknown results; source code binds cache identity."""
from types import SimpleNamespace
import pytest

from office.test_pool_state_rpc import run
from office.test_sheet_tool import env  # noqa: F401
from sniper.office.tools import sheet as SH
from sniper.office import desk_near_pass as N


def test_failed_second_refresh_keeps_exact_read_time_slot_and_code(monkeypatch):
    got=dict(base_reserve=1,quote_reserve=1,virtual_quote=0,slot=9,read_at=1002.5)
    out,_=run(monkeypatch,['tape_gap_since_cut','tape_gap_since_cut'],got)
    meta=out['source_provenance']
    assert meta['rpc_fallback']['read_at']==1002.5 and meta['rpc_fallback']['slot']==9
    assert meta['rpc_fallback']['initial_reason']=='tape_gap_since_cut'
    assert meta['rpc_fallback']['unknown_reason'] is None
    assert meta['code_version']==N.SHEET_CODE_VERSION and len(meta['code_sha256'])==64
    assert 'pinned_release_sha' in meta
    assert meta['pinned_release_sha']==N.PINNED_RELEASE_SHA


def test_rpc_unknown_reason_and_absent_state_stay_explicit(monkeypatch):
    out,_=run(monkeypatch,['latest_swap_not_buy'],{'unknown':'rpc_cap'})
    rpc=out['source_provenance']['rpc_fallback']
    assert rpc['attempted'] is True and rpc['read_at'] is None and rpc['slot'] is None
    assert rpc['unknown_reason']=='rpc_cap'


def test_successful_fallback_passes_provenance_to_persisting_sheet(monkeypatch):
    from office.test_pool_state_rpc import ticket
    from sniper.services.memecoin_desk_asof import StaleSnapshot
    seen={}
    monkeypatch.setattr(SH,'sheet',lambda cand,**kw:seen.update(kw) or {'state':'known'})
    store=SimpleNamespace(get_ticket=lambda *a:ticket(),get_cohort=lambda *a:{'policy':{'hard_filter':{}}},
                          get_intake_source_receipt=lambda *a,**k:{})
    def refresh(candidate,**kw):
        if kw.get('rpc_state') is None:raise StaleSnapshot('tape_gap_since_cut')
        return candidate,{}
    reader=SimpleNamespace(read=lambda *a:dict(slot=9,read_at=1002.5))
    out=N.near_pass_sheet(None,'T',store_conn=None,desk_dir='/d',now=1000.,
        clock_sample={'offset_s':.25},buyer_ledger=[],refresh=refresh,store=store,
        pool_state_reader=reader,clock=lambda:1003.)
    assert out['state']=='known' and seen['source_provenance']==out['source_provenance']
    assert out['source_provenance']['rpc_fallback']['read_at_corrected']==1002.25


def test_sheet_cache_and_persisted_receipt_bind_source_code_provenance(env):
    conn,_=env
    from office.test_sheet_tool import candidate,hard,CALL_NOW
    args=dict(desk_dir='/d',policy={'rail_bounds':{},'hard_filter':{},'v':1},
        clock_sample={'offset_s':.25,'sampled_at':CALL_NOW-10},hard_filter_row=hard(),now=CALL_NOW,
        store_conn=conn,buyer_ledger=[],strategy_epoch='e1')
    a={'code_version':'one','code_sha256':'a'*64,'rpc_fallback':{'attempted':False}}
    b=dict(a,code_version='two',code_sha256='b'*64)
    one=SH.sheet(candidate(),source_provenance=a,**args)
    two=SH.sheet(candidate(),source_provenance=b,**args)
    assert one['source_provenance']==one['request']['source_provenance']==a
    assert one['cache_key']!=two['cache_key'] and one['row_hash']!=two['row_hash']
    import json
    saved=json.loads(conn.execute('SELECT payload_json FROM receipts WHERE row_hash=?',(one['row_hash'],)).fetchone()[0])
    assert saved['source_provenance']==a


def test_malformed_rpc_reply_remains_a_typed_unknown_with_provenance(monkeypatch):
    out,_=run(monkeypatch,['tape_gap_since_cut'],None)
    assert out['state']=='unknown'
    assert out['source_provenance']['rpc_fallback']['unknown_reason']=='rpc_state_unusable'


def test_runtime_result_whitelist_retains_fallback_provenance(tmp_path,monkeypatch):
    import json,sqlite3,threading,time
    from sniper.office import runtime as RT
    from sniper.persistence import db_core
    owner=threading.get_ident();original=db_core.get_readonly_connection
    def local(*a,**kw):
        return sqlite3.connect(':memory:') if threading.get_ident()==owner else original(*a,**kw)
    monkeypatch.setattr(db_core,'get_readonly_connection',local)
    expected={'code_version':'witness','code_sha256':'a'*64,'rpc_fallback':{'attempted':True,'read_at':1000.,'slot':9}}
    monkeypatch.setattr(N,'near_pass_sheet',lambda *a,**kw:{'state':'unknown','reason':'gap','source_provenance':expected})
    clock=tmp_path/'clock.json';clock.write_text(json.dumps({'offset_s':0,'at':time.time()}))
    path=tmp_path/'desk.db'
    sqlite3.connect(path).close()
    tool=RT.near_pass_tools(None,path,tmp_path,clock)['desk.near_pass_sheet']
    assert tool.handler({'ticket_id':'ticket'},None).result['source_provenance']==expected


@pytest.mark.parametrize('read_offset,expected',[(.1,'unknown'),(-.45,'known')])
def test_rpc_timestamp_cannot_advance_the_host_cut_into_the_future(tmp_path,monkeypatch,read_offset,expected):
    import sqlite3
    from sniper.services import memecoin_desk_ps_asof as A
    from test_memecoin_desk_ps_evidence import fixture,NOW
    from test_desk_ps_asof_rpc_state import rpc_state
    desk,candidate=fixture(tmp_path)
    with sqlite3.connect(desk/'pumpswap/tape.db') as conn:
        conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-.8,'worker_start',0))
    ticket=dict(candidate=candidate,cohort_id='c',opportunity_id='o',stages=[dict(
        stage='hard_filter',verdict='PASS',reason='pass',ts=NOW-1,
        inputs={'evidence':{'market_cut':{'receipt_cut':NOW-1,'cut_slot':1}}})])
    store=SimpleNamespace(get_ticket=lambda *a:ticket,get_cohort=lambda *a:{'policy':{'hard_filter':{}}},
                          get_intake_source_receipt=lambda *a,**k:{})
    monkeypatch.setattr(SH,'sheet',lambda *a,**k:{'state':'known'})
    reader=SimpleNamespace(read=lambda *a:rpc_state(candidate,read_at=NOW+read_offset))
    out=N.near_pass_sheet(None,'T',store_conn=None,desk_dir=desk,now=NOW-.5,
        clock_sample={'offset_s':0},buyer_ledger=[],refresh=A.refresh,store=store,
        pool_state_reader=reader,clock=lambda:NOW-.4)
    assert out['state']==expected
    if expected=='unknown':assert 'rpc_state_unusable' in out['reason']


def test_oversized_rpc_timestamp_remains_typed_unknown(monkeypatch):
    out,_=run(monkeypatch,['tape_gap_since_cut'],dict(slot=9,read_at=10**400))
    assert out['state']=='unknown'
    rpc=out['source_provenance']['rpc_fallback']
    assert rpc['read_at'] is None and rpc['unknown_reason']=='rpc_state_unusable'


@pytest.mark.parametrize('at,offset',[(1002.5,10**400),(1e308,-1e308)],ids=['oversized-offset','overflowed-correction'])
def test_invalid_corrected_time_cannot_break_failure_provenance(monkeypatch,at,offset):
    import json
    from office.test_pool_state_rpc import ticket
    from sniper.services.memecoin_desk_asof import StaleSnapshot
    store=SimpleNamespace(get_ticket=lambda *a:ticket(),get_cohort=lambda *a:{'policy':{'hard_filter':{}}})
    def failed_refresh(*a,**kw):raise StaleSnapshot('tape_gap_since_cut')
    out=N.near_pass_sheet(None,'T',store_conn=None,desk_dir='/d',now=1000.,
        clock_sample={'offset_s':offset},buyer_ledger=[],refresh=failed_refresh,store=store,
        pool_state_reader=SimpleNamespace(read=lambda *a:dict(slot=9,read_at=at)),clock=lambda:1003.)
    assert out['state']=='unknown' and out['source_provenance']['rpc_fallback']['read_at_corrected'] is None
    json.dumps(out,allow_nan=False)
