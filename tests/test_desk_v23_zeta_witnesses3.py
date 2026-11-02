"""Offline final-boundary checks at 1e0c21c4e; run from Sniper-beta-v23."""
from contextlib import nullcontext
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import pytest
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_desk_v23_zeta_witnesses import fixture
from test_desk_v23_zeta_witnesses2 import priced_context,parent
from test_desk_v23_pipeline import ticket_in,vet,GATE,ASOF,PLAN,F,BASIS,vol_structure,verdict
from test_memecoin_desk_pipeline import decide,api,RESPONSE
from test_desk_watch_v22_pipeline import T_RESP,iso,fire
from sniper.persistence import db_memecoin_desk as store


def test_gate_usd_ceiling_matches_actual_judge_sheet_after_causal_fx_move():
    from test_memecoin_desk_asof import Tape
    from test_memecoin_desk_contract import NOW,SIG,candidate,evidence,policy
    from sniper.services import memecoin_desk_asof as A
    from sniper.services.memecoin_desk_universe import Field,PUMP_SUPPLY_TOKENS
    with TemporaryDirectory(prefix='zeta-asof-fx-') as td,fixture() as (db,mp):
        tape=Tape(Path(td),NOW+60)
        tape.swap(SIG,0,7,NOW,40_000_000_000,100_000_000_000_000,real_token=50_000_000_000_000)
        tape.swap('fresh-swap',0,8,NOW+50,40_000_000_000,100_000_000_000_000,real_token=50_000_000_000_000)
        tape.sw.execute('ALTER TABLE swaps ADD COLUMN real_quote INTEGER')
        tape.sw.execute('UPDATE swaps SET real_quote=?',(10_000_000_000,));tape.sw.commit()
        tape.beat(sol=110.0,sol_age=1.0)
        c=candidate(mcap_usd=Field(40000.0,'pumpfun_grpc:curve_reserves',NOW,7,'complete'),
                    liq_usd=Field(1000.0,'pumpfun_grpc:real_quote_reserves',NOW,7,'complete'))
        seen,meta=A.refresh(c,desk_dir=tape.dir,now=tape.now,rank_inputs=True)
        assert seen['snapshot']['fields']['mcap_usd']['value']==40000.0
        assert meta['rank']['price_now']*meta['sol_usd']*PUMP_SUPPLY_TOKENS==pytest.approx(44000.0)
        t=ticket_in(db,mp,pol={**GATE,'standing_orders':{**GATE['standing_orders'],'graduation_guard_pct':97.0},'verdict_tolerance_pct':0.0},cand=c)
        start=iso(NOW+60);resp=iso(NOW+61)
        decide(db,t,{**verdict(),'p':PLAN,'f':F},asof={'candidate':seen,'meta':meta},structure=vol_structure(4.0,30),
               call_started_at=start,response_at=resp,now_iso=resp)
        trig=vet(db,t)['inputs']['trigger']
        sheet_ref=api().priced_verdict(seen,clip_usd=25,basis='asof')['ref_price_usd']
        assert trig['ceiling_price_usd']<=sheet_ref, ('sheet_ref',sheet_ref,'gate_ceiling',trig['ceiling_price_usd'])

@pytest.mark.parametrize('fx',[None,0,-1,float('inf'),float('nan')])
def test_unknown_or_invalid_fx_never_freezes_ceiling_control(fx):
    with fixture() as (db,mp):
        c,asof,basis=priced_context();asof={**asof,'meta':{**asof['meta'],'sol_usd':fx}}
        t=ticket_in(db,mp,pol=GATE,cand=c)
        if type(fx) is float and not __import__('math').isfinite(fx):
            with pytest.raises(ValueError):
                decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,30))
            assert not any(s['stage']=='vet' for s in store.get_ticket(db[0],t)['stages'])
        else:
            out=decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,30))
            assert out['action'] in ('WATCH','SKIP') and 'trigger' not in vet(db,t)['inputs']


def test_expired_noise_child_cannot_resurrect_control():
    with fixture() as (db,mp):
        t=parent(db,mp);real=api().evaluate_candidate
        mp.setattr(api(),'evaluate_candidate',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('after insert')))
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+31))
        mp.setattr(api(),'evaluate_candidate',real)
        api().expire_stranded_children(db[0],now_iso=iso(T_RESP+700))
        assert store.get_ticket(db[0],t+':noise')['state']=='expired'
        api().rejudge_noise_watches(db[0],now_iso=iso(T_RESP+701))
        assert store.get_ticket(db[0],t+':noise')['state']=='expired'
        assert store.list_awaiting_judge(db[0])==[]

@pytest.mark.parametrize('current,complete',[(None,'missing'),(95.0,'partial')])
def test_unknown_asof_curve_does_not_inherit_old_refusal(current,complete):
    from test_memecoin_desk_contract import candidate,NOW
    from sniper.services.memecoin_desk_universe import Field
    with fixture() as (db,mp):
        old=candidate(curve_pct=Field(95.0,'pumpfun_grpc:real_token_reserves',NOW,7,'complete'))
        seen=candidate(curve_pct=Field(current,'pumpfun_grpc:real_token_reserves',NOW,7,complete))
        t=ticket_in(db,mp,pol=GATE,cand=old)
        out=decide(db,t,{**verdict(v='W',e=['px_hold','<=',-8,1,60]),'p':PLAN,'f':F},
                   asof={**ASOF,'candidate':seen},structure=vol_structure(4.0,30))
        assert out['action']=='WAIT', ('current_curve',current,'completeness',complete,'actual',out['action'],out['reason'])

from test_memecoin_desk_provision_cohort import env,crawler,P,ARGS,_out  # noqa:F401

def test_final_provisioner_flags_freeze_and_refuse_bounds_control(env,capsys):
    args=ARGS+['--standing-orders','--v23','--px-hold-max-wait-s','600','--graduation-guard-pct','97',
               '--desk-db',str(env/'desk.db'),'--ledger-db',str(env/'ledger.db')]
    assert P.main(args)==0
    standing=_out(capsys)[0]['policy']['standing_orders']
    assert standing['px_hold_max_wait_s']==600 and standing['graduation_guard_pct']==97.0
    for opt,bad in [('--px-hold-max-wait-s','601'),('--graduation-guard-pct','101')]:
        with pytest.raises((ValueError,SystemExit)):
            P.main(ARGS+['--standing-orders',opt,bad,'--desk-db',str(env/'desk.db'),'--ledger-db',str(env/'ledger.db')])
