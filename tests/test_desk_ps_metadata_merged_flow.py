"""Offline metadata receipt -> raw wire -> emission -> decision snapshot -> identical sheet/queue cap.

The synthetic cohort bypasses admission only to exercise the unwired development seams. No model,
provider, courier or executor is invoked; production PS admission remains unregistered.
"""
import copy
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk, memecoin_desk_asof as A
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_ps_metadata as MD
from sniper.services import memecoin_desk_soft_facts as SF
from test_codex_pumpswap_merged_flow import W, NOW, MINT, POOL, frame, create_line, buy_line, emissions, script
from test_memecoin_desk_judge import model_returning, enter
from test_memecoin_desk_pipeline import databases, POLICY  # noqa: F401

MW=script('merged_ps_metadata_worker','scripts/memecoin_desk_ps_metadata_worker.py')


def build(base,monkeypatch,*,newer_v2=False):
    for key,value in (('CAPTURE','1'),('EMISSIONS','1'),('BAND_LO_USD','30000'),('BAND_HI_USD','100000')):
        monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_'+key,value)
    clock=[NOW];monkeypatch.setattr(W.time,'time',lambda:clock[0])
    monkeypatch.setattr(W.clock,'offset_s',lambda:1.)
    w=W.Worker(W.knobs({}),base=base,sol_price_fn=lambda:None,alert_fn=lambda *_:None)
    w.sol_usd,w.sol_at=150.,NOW
    def play(n,line,*,slot=None,index=1):
        clock[0]=NOW+n
        u=pb.SubscribeUpdate.FromString(frame(int(round(n*10))+1,line))
        u.transaction.slot=slot if slot is not None else 10+int(n)
        u.transaction.transaction.index=index
        w.process(u.SerializeToString(),received_at=clock[0]-.01)
        w.commit_files()
    play(0,create_line())
    calls=[]
    def rpc(method,params,gate):
        if not gate():return None,'budget_exhausted'
        calls.append((method,params))
        assert params[0]==MINT
        return {'context':{'slot':11},'value':{
            'owner':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
            'data':{'program':'spl-token','parsed':{'type':'mint','info':{
                'decimals':6,'supply':str(10**15),'mintAuthority':None,'freezeAuthority':None}}}}},None
    meta=base/'pumpswap'/'metadata.db';tape=base/'pumpswap'/'tape.db'
    assert MW.run_once(tape,meta,rpc,now_fn=lambda:clock[0])['read']==0
    play(1,buy_line(20_000))
    assert emissions(base)==[]
    clock[0]=NOW+1.1
    assert MW.run_once(tape,meta,rpc,now_fn=lambda:clock[0])['read']==1
    assert len(calls)==1
    play(2,buy_line(20_000));play(3,buy_line(40_000))
    c=emissions(base)[0]
    if newer_v2:play(3.2,buy_line(40_000,ix='buy_v2'),slot=13,index=2)
    # A later real block advances the source cut past slot 13's receipt; an idle commit cannot.
    for slot,received in ((13,NOW+3.3),(14,NOW+3.5)):
        clock[0]=received
        block=pb.SubscribeUpdate();block.block_meta.slot=slot;block.block_meta.block_time.timestamp=int(NOW+3)
        w.process(block.SerializeToString(),received_at=received)
    clock[0]=NOW+3.6
    w.commit_files();w.heartbeat(True)
    return w,c,calls


def test_metadata_to_frozen_sheet_and_outbox_uses_one_price(tmp_path,monkeypatch,databases):
    base=tmp_path/'capture';w,c,calls=build(base,monkeypatch)
    frozen=copy.deepcopy(c)
    refreshed,meta=A.refresh(c,desk_dir=base,now=NOW+4)
    state=refreshed['snapshot']['fields']['pool_state']['value']
    assert c==frozen and c['snapshot']['hash']!=refreshed['snapshot']['hash']
    assert state['cut_slot']==14 and state['reserve_known_at']==state['virtual_quote_known_at']==NOW+2.99
    assert state['metadata_known_at']==NOW+1.1
    assert refreshed['snapshot']['fields']['pool_state']['known_at']==NOW+4
    assert state['sol_usd']==150 and state['sol_age_s']==pytest.approx(4)
    cohort=desk.provision_cohort(*databases,producer='U',policy={**POLICY,'venue':'pumpswap',
        'judge_p_admit':1.,'select_version':J.SELECT_VERSION},prompt_version='offline-ps-fixture',
        created_at=datetime.fromtimestamp(NOW,timezone.utc).isoformat())
    at=datetime.fromtimestamp(NOW+3,timezone.utc).isoformat()
    ticket=desk.ingest_candidate(databases[0],c,cohort_id=cohort,now_iso=at)['ticket_id']
    D.record_stage(databases[0],ticket_id=ticket,stage='hard_filter',seat='code',verdict='PASS',
        reason='offline_fixture_only',inputs={'evidence':{'checks':{'holders':{
            'status':'kill','reason':'top10_high','top10_pct':95,'creator_pct':25,
            'creator_basis':'top20','exclusions_verified':True}}}},output={},ts=at)
    clock=SimpleNamespace(t=datetime.fromtimestamp(NOW+4,timezone.utc));sheets=[]
    result=J.run_once(databases[0],lambda:nullcontext(databases[1]),
        call_model=model_returning(enter,calls=sheets,clock=clock),system_prompt='offline fixture',
        worker='offline',now_fn=lambda:clock.t,calls_per_hour=100,
        asof_fn=lambda candidate,now:A.refresh(candidate,desk_dir=base,now=now),
        soft_fn=lambda ticket:SF.for_ticket(ticket,desk_dir=base,now=NOW+4))
    assert result[0]['action']=='ENTER' and len(sheets)==len(calls)==1
    vet=next(s for s in D.get_ticket(databases[0],ticket)['stages'] if s['stage']=='vet')
    persisted=vet['inputs']['asof']['meta']
    assert persisted['receipt_cut']==persisted['committed_through']==NOW+3.5
    assert persisted['receipt_lag_s']==.5
    cap=D.list_outbox(databases[0])[0]['payload']['desk_metadata']['verdict_price']
    assert sheets[0]['verdict_price']==cap==desk.priced_verdict(vet['inputs']['asof']['candidate'],clip_usd=25,basis='asof')
    assert sheets[0]['pool_state']==state
    assert sheets[0]==vet['inputs']['sheet']
    assert sheets[0]['evidence']['cluster']['reason']=='not_applicable'
    assert sheets[0]['evidence']['cluster']['check']=='unknown'
    assert sheets[0]['evidence']['holders']['check']=='flagged'
    assert sheets[0]['evidence']['holders']['top10_pct']==95
    reasons=w.files.conn('pricing_ps',NOW+4).execute('SELECT reason,COUNT(*) FROM pricing_ps GROUP BY reason').fetchall()
    assert reasons==[('emitted',1),('metadata_not_yet_known',1),('outside_band',1)]
    assert D.get_ticket(databases[0],ticket)['snapshot_hash']==frozen['snapshot']['hash']
    # No courier dispatch: no real/manual intent or executor action follows the synthetic outbox.


def test_newer_same_slot_v2_never_falls_back_to_an_older_legacy_buy(tmp_path,monkeypatch):
    w,c,_=build(tmp_path,monkeypatch,newer_v2=True)
    with pytest.raises(A.StaleSnapshot,match='virtual_quote_unproven'):
        A.refresh(c,desk_dir=tmp_path,now=NOW+4)
    assert w.files.conn('pricing_ps',NOW+4).execute("SELECT COUNT(*) FROM pricing_ps WHERE reason='virtual_quote_unproven'").fetchone()==(1,)


def test_existing_local_metadata_does_not_get_rewritten_by_the_universe_reader(tmp_path,monkeypatch):
    w,c,_=build(tmp_path,monkeypatch)
    path=tmp_path/'pumpswap'/'metadata.db'
    conn=MD.connect(path)
    before=conn.execute('SELECT * FROM mints').fetchall();conn.close()
    binding={'mint':MINT,'quote_mint':c['snapshot']['fields']['pool_state']['value']['quote_mint']}
    row=w.pool_metadata(POOL,binding,NOW+4)
    assert row['known_at']==NOW+1.1 and row['supply_atomic']==str(10**15)
    conn=MD.connect(path)
    assert conn.execute('SELECT * FROM mints').fetchall()==before
    conn.close()


def test_unreadable_metadata_is_typed_and_never_a_successful_empty_read(tmp_path,monkeypatch):
    w,c,_=build(tmp_path,monkeypatch)
    conn=MD.connect(tmp_path/'pumpswap'/'metadata.db')
    conn.execute('DROP TABLE mints');conn.commit();conn.close()
    binding={'mint':MINT,'quote_mint':c['snapshot']['fields']['pool_state']['value']['quote_mint']}
    assert w.pool_metadata(POOL,binding,NOW+4) is None
    assert w.ps_metadata_reason=='metadata_unreadable' and w.stats['ps_metadata_errors']==1


def send(w,monkeypatch,*,number,slot,cap,received,processed):
    monkeypatch.setattr(W.time,'time',lambda:processed)
    w.sol_at=processed
    u=pb.SubscribeUpdate.FromString(frame(number,buy_line(cap)))
    u.transaction.slot=slot
    w.process(u.SerializeToString(),received_at=received);w.commit_files()


def test_stale_source_reserve_is_retained_with_its_pricing_reason(tmp_path,monkeypatch):
    w,c,_=build(tmp_path,monkeypatch)
    send(w,monkeypatch,number=50,slot=14,cap=20_000,received=NOW+4,processed=NOW+4)
    send(w,monkeypatch,number=51,slot=15,cap=40_000,received=NOW+5,processed=NOW+66)
    assert len(emissions(tmp_path))==1 and w.ps_book.last_reason=='reserve_stale'
    row=w.files.conn('pricing_ps',NOW+66).execute("SELECT received_at,processed_at,reason FROM pricing_ps WHERE reason='reserve_stale'").fetchall()
    assert row==[(NOW+5,NOW+66,'reserve_stale')]


@pytest.mark.parametrize('known_at',[NOW+1.1,NOW+6])
def test_metadata_after_source_slot_is_a_distinct_persisted_reason(tmp_path,monkeypatch,known_at):
    w,c,_=build(tmp_path,monkeypatch)
    send(w,monkeypatch,number=50,slot=14,cap=20_000,received=NOW+4,processed=NOW+4)
    binding={'mint':MINT,'quote_mint':c['snapshot']['fields']['pool_state']['value']['quote_mint']}
    metadata=w.pool_metadata(POOL,binding,NOW+5)
    metadata.update(slot=16,known_at=known_at)
    w.pool_metadata_fn=lambda _:metadata
    send(w,monkeypatch,number=51,slot=15,cap=40_000,received=NOW+5,processed=NOW+7)
    assert len(emissions(tmp_path))==1 and w.ps_book.last_reason=='metadata_after_reserve'
    assert w.files.conn('pricing_ps',NOW+7).execute("SELECT COUNT(*) FROM pricing_ps WHERE reason='metadata_after_reserve'").fetchone()==(1,)
