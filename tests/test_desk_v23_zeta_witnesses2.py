"""Additional offline Zeta review witnesses for e49d1b0f9; run from Sniper-beta-v23."""
from contextlib import nullcontext
from datetime import datetime,timedelta
from pathlib import Path
import json,sys
import pytest
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_desk_v23_zeta_witnesses import fixture
from test_desk_v23_pipeline import ticket_in,vet,GATE,ASOF,PLAN,F,BASIS,vol_structure,verdict
from test_memecoin_desk_pipeline import decide,api,RESPONSE
from test_desk_watch_v22_pipeline import T_RESP,iso,fire
from sniper.persistence import db_memecoin_desk as store
from sniper.services.memecoin_desk_universe import StandingBook,Field,PUMP_SUPPLY_TOKENS


def parent(db,mp,cand=None):
    t=ticket_in(db,mp,pol={**GATE,'judge_p_admit':1.0},cand=cand)
    asof={**ASOF,'candidate':cand} if cand is not None else ASOF
    out=decide(db,t,{**verdict(k=t),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,5))
    assert out['reason']=='px_hold_noise_watch'
    return t


def priced_context():
    from test_memecoin_desk_contract import candidate
    c=candidate()
    basis=c['snapshot']['fields']['mcap_usd']['value']/PUMP_SUPPLY_TOKENS/180.0
    asof={**ASOF,'candidate':c,'meta':{**ASOF['meta'],'rank':{'price_now':basis}}}
    return c,asof,basis

def test_original_usd_gate_ceiling_survives_fx_rise():
    with fixture() as (db,mp):
        c,asof,basis=priced_context()
        t=ticket_in(db,mp,pol={**GATE,'verdict_tolerance_pct':0.0},cand=c)
        decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,30))
        row=api().record_fire(db[0],t,fire(price_sol=basis*1.5,sol_usd=198.0,block_time=T_RESP+7,
            received_at=T_RESP+7.1,sol_at=T_RESP+7),now_iso=iso(T_RESP+8))
        ref=row['output']['verdict_price']['ref_price_usd']
        assert ref<=basis*ASOF['meta']['sol_usd'], ('original_usd',basis*180.0,'accepted_usd',ref)


def test_above_gate_ceiling_does_not_consume_remaining_watch():
    with fixture() as (db,mp):
        c,asof,basis=priced_context()
        t=ticket_in(db,mp,pol={**GATE,'verdict_tolerance_pct':0.0},cand=c)
        decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,30))
        b=StandingBook(); b.load(db[0].execute(StandingBook.SQL).fetchall(),T_RESP)
        mint=store.get_ticket(db[0],t)['mint'];rich=[]
        for dt,multiple in ((1,.90),(2,1.50),(7,1.50)):
            rich=b.on_trade(mint=mint,price_sol=basis*multiple,block_time=T_RESP+dt,
                received_at=T_RESP+dt+.1,gap_since_load=False,net_flow_usd=1.0,sol_usd=180.0)
        if rich:
            b.commit(t)  # Production commits once the fire record is durable.
        valid=[]
        for dt in (13,18):
            valid=b.on_trade(mint=mint,price_sol=basis*.98,block_time=T_RESP+dt,
                received_at=T_RESP+dt+.1,gap_since_load=False,net_flow_usd=1.0,sol_usd=180.0)
        assert not rich and valid, ('above_ceiling_fire',bool(rich),'later_valid_fire',bool(valid))


def test_noise_child_recovery_finishes_committed_child_after_crash():
    with fixture() as (db,mp):
        t=parent(db,mp);real=api().evaluate_candidate
        def crash(*a,**kw):
            raise RuntimeError('injected crash after committed child before hard filter')
        mp.setattr(api(),'evaluate_candidate',crash)
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+31))
        child=store.get_ticket(db[0],t+':noise')
        assert child is not None and child['state']=='open'
        mp.setattr(api(),'evaluate_candidate',real)
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+32))
        api().expire_stranded_children(db[0],now_iso=iso(T_RESP+700))
        child=store.get_ticket(db[0],t+':noise')
        assert any(s['stage']=='hard_filter' for s in child['stages']) or child['state']=='expired', (
            'unfinished child',child['state'],[s['stage'] for s in child['stages']])


def test_completed_noise_parent_does_not_fill_recovery_page():
    from test_memecoin_desk_contract import candidate
    with fixture() as (db,mp):
        a=parent(db,mp)
        assert api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+31),limit=1)==[a+':noise']
        c={**candidate(),'ticket_id':'desk:U:other-noise','mint':candidate()['mint'][:-1]+'Z'}
        b=parent(db,mp,c)
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+32),limit=1)
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+33),limit=1)
        assert store.get_ticket(db[0],b+':noise') is not None, 'completed A still occupies the page; B cannot resume'


def test_drift_rejudge_with_young_noise_does_not_get_impossible_watch():
    from test_desk_v23_drift_requeue import refuse,LATER
    from test_memecoin_desk_contract import candidate,evidence,policy
    from sniper.settings import memecoin as cfg
    with fixture() as (db,mp):
        p={**GATE,'drift_requeue':'drift-requeue-v1','judge_p_admit':1.0}
        mp.setattr(cfg,'MEMECOIN_DESK_ENTRY_GATE',False)
        t=ticket_in(db,mp,pol=p)
        decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=ASOF,structure=vol_structure(4.0,30))
        req=store.list_outbox(db[0])[0]
        api().dispatch_outbox(*db,now_iso=RESPONSE)
        refuse(db,req['intent_id'])
        api().reconcile_intents(*db,now_iso=LATER)
        child=t+':drift';h=store.get_ticket(db[0],child)
        assert h is not None and any(s['stage']=='hard_filter' for s in h['stages'])
        mp.setattr(cfg,'MEMECOIN_DESK_ENTRY_GATE',True)
        out=decide(db,child,{**verdict(k=child),'p':PLAN,'f':F},asof={'candidate':h['candidate'],'meta':ASOF['meta']},
            structure=vol_structure(4.0,5),call_started_at=LATER,response_at=LATER,now_iso=LATER)
        # Beta: Zeta allowed 'preserve lineage OR explicitly exempt'; exempt chosen: a claimed child never promises a WATCH
        assert out['action']=='SKIP' and out['reason']=='cond_unavailable'
        later=(datetime.fromisoformat(LATER)+timedelta(seconds=31)).isoformat()
        api().rejudge_noise_watches(db[0],now_iso=later)
        assert store.get_ticket(db[0],child+':noise') is None

def test_preinsert_crash_resumes_control():
    with fixture() as (db,mp):
        t=parent(db,mp);real=store.record_candidate
        mp.setattr(store,'record_candidate',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('before insert')))
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+31))
        assert store.get_ticket(db[0],t+':noise') is None
        mp.setattr(store,'record_candidate',real)
        assert api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+32))==[t+':noise']
        assert any(s['stage']=='hard_filter' for s in store.get_ticket(db[0],t+':noise')['stages'])


def test_second_unknown_noise_is_terminal_control():
    from test_memecoin_desk_judge import model_returning
    from sniper.services import memecoin_desk_judge as J
    with fixture() as (db,mp):
        t=parent(db,mp);calls=[]
        fake=model_returning(lambda sheet: json.dumps({**verdict(v='W',e=['px_hold','<=',-8,1,60],k=sheet['opportunity_id']),
             'p':PLAN,'f':F}),calls=calls)
        def run(dt):
            return J.run_once(db[0],lambda:nullcontext(db[1]),call_model=fake,system_prompt='offline',
                worker='zeta-test',now_fn=lambda:datetime.fromisoformat(RESPONSE)+timedelta(seconds=dt),calls_per_hour=40,
                asof_fn=lambda c,at:(c,ASOF['meta']),structure_fn=lambda *a:vol_structure(4.0,5))
        out=run(31)
        assert len(calls)==1 and out[0]['reason']=='cond_unavailable'
        assert store.get_ticket(db[0],t+':noise')['state']=='skipped'
        assert run(90)==[] and len(calls)==1
