"""Offline standing-v2 integration witnesses; exact18e74e1de archive only."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,sys
import pytest
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_desk_v23_zeta_witnesses import fixture
from test_desk_standing_v2 import V2,HOLD,ASOF,LEVEL,ticket_in,vet,vol_structure,T_RESP,iso
from test_memecoin_desk_pipeline import decide
from test_desk_watch_v22_pipeline import fire
from test_memecoin_desk_contract import evidence,verdict
from sniper.services import memecoin_desk as D
from sniper.persistence import db_memecoin_desk as store


def run_actual_refilter(tmp_path,db,mp):
    from test_desk_chase_limit import CHASE,chased,parent_vp
    root,_=chased(db,pol={**CHASE,'evidence_wait_s':60})
    t=root+':limit'
    level=parent_vp(db,root)['ref_price_usd']
    D.record_fire(db[0],t,fire(price_sol=level/180.0*.99),now_iso=iso(T_RESP+31))
    h=store.get_ticket(db[0],t)
    spec=importlib.util.spec_from_file_location('zeta_standing_assembler',Path.cwd()/'scripts/memecoin_desk_assembler.py')
    W=importlib.util.module_from_spec(spec);spec.loader.exec_module(W)
    now=T_RESP+33
    mp.setenv('MEMECOIN_DESK_ASSEMBLER_ENABLED','1');mp.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    mp.setattr(W,'state_root',lambda:str(tmp_path));mp.setattr(W,'load_env_files',lambda *a:None)
    mp.setattr(W,'time',SimpleNamespace(time=lambda:now,monotonic=lambda:100.0,sleep=lambda *_:None))
    from test_memecoin_desk_safety import TRADE
    probe,_=W.S.probe_from_tape(TRADE,head_slot=1000,head_known_at=now,heartbeat_age_s=0,
        gap_last_slots=[],graduated=False,mayhem=False,sol_usd=180.0,evaluated_at=now)
    assert probe['status']=='pass','positive control: these exact reserves and proven flag must price'
    checks=evidence()['checks']
    mp.setattr(W,'check_ticket',lambda *a,**kw:(checks,{'mayhem':False}))
    mp.setattr(W,'tape_state',lambda *a: (TRADE,1000,now,[],0,180.0))
    path=db[0].execute('PRAGMA database_list').fetchone()[2]
    assert W.main(['--desk-db',path,'--cohort-id',h['cohort_id']],env_root=tmp_path,rpc=SimpleNamespace(calls=0),from_start=True)==0
    return t,W,now


def test_actual_assembler_preserves_fresh_mayhem_flag(tmp_path):
    with fixture() as (db,mp):
        t,W,now=run_actual_refilter(tmp_path,db,mp)
        row=next(s for s in store.get_ticket(db[0],t)['stages'] if s['stage']=='refilter')
        assert row['verdict']=='PASS',row['reason']


def test_refilter_rpc_spend_is_reserved_for_restart(tmp_path):
    with fixture() as (db,mp):
        t,W,now=run_actual_refilter(tmp_path,db,mp)
        spent,window=W.counters(tmp_path/'data/desk/assembler',now)
        assert spent==W.SAFETY_CALLS and len(window)==1,('recovered_spend',spent,'window',window)



def test_chase_end_row_is_written_for_miss():
    from test_desk_chase_limit import chased
    required={'end_reason','life_s','mode','cohort_key','arm','limit_arm','refused_quote','verdict_price','chase_gap_pct'}
    with fixture() as (db,mp):
        root,_=chased(db);t=root+':limit'
        from test_memecoin_desk_pipeline import LATER
        from datetime import datetime,timedelta
        later=(datetime.fromisoformat(LATER)+timedelta(seconds=7201)).isoformat()
        assert D.expire_watches(db[0],now_iso=later)==[t]
        outputs=[s.get('output') or {} for s in store.get_ticket(db[0],t)['stages']]
        assert any(required<=set(r) for r in outputs),outputs[-1]


def test_chase_kill_preserves_refusal_and_no_child():
    from test_desk_chase_limit import chased
    from sniper.settings import memecoin as cfg
    with fixture() as (db,mp):
        mp.setattr(cfg,'MEMECOIN_DESK_CHASE_LIMIT',False)
        root,_=chased(db)
        assert store.get_ticket(db[0],root+':limit') is None
        assert store.get_ticket(db[0],root)['state']=='refused'


def test_chase_never_fires_above_usd_or_without_price_fx():
    from test_desk_chase_limit import chased,parent_vp
    from test_desk_standing_v2 import StandingBook
    from test_desk_limit_arm_watch import LATER,t_of,fire as lfire,iso as liso
    with fixture() as (db,mp):
        root,_=chased(db);t=root+':limit';level=parent_vp(db,root)['ref_price_usd']
        at=t_of(LATER,10);b=StandingBook();b.load(db[0].execute(StandingBook.SQL).fetchall(),at)
        mint=store.get_ticket(db[0],t)['mint']
        for px,fx in ((level/180*1.01,180.0),(level/180,None),(None,180.0)):
            assert b.on_trade(mint=mint,price_sol=px,block_time=at,received_at=at+.1,gap_since_load=False,sol_usd=fx)==[]
        with pytest.raises(ValueError,match='fire_price_usd'):
            D.record_fire(db[0],t,lfire(level/180*1.01,at),now_iso=liso(at+1))
        assert b.on_trade(mint=mint,price_sol=level/162*.99,block_time=at+2,received_at=at+2.1,gap_since_load=False,sol_usd=162.0)
        b2=StandingBook();b2.load(db[0].execute(StandingBook.SQL).fetchall(),at+2)
        assert b2.on_trade(mint=mint,price_sol=level/162*.99,block_time=at+2,received_at=at+2.1,gap_since_load=False,sol_usd=162.0)


def test_unwritten_chase_cancel_can_retry():
    from test_desk_chase_limit import chased
    from test_desk_standing_v2 import StandingBook,PUMP_SUPPLY_TOKENS
    from test_desk_limit_arm_watch import LATER,t_of
    with fixture() as (db,mp):
        root,_=chased(db);t=root+':limit';at=t_of(LATER,10)
        b=StandingBook();b.load(db[0].execute(StandingBook.SQL).fetchall(),at)
        mint=store.get_ticket(db[0],t)['mint'];px=100001/PUMP_SUPPLY_TOKENS/180
        b.on_trade(mint=mint,price_sol=px,block_time=at,received_at=at+.1,gap_since_load=False,sol_usd=180.0)
        assert b.take_cancels()
        b.on_trade(mint=mint,price_sol=px,block_time=at+1,received_at=at+1.1,gap_since_load=False,sol_usd=180.0)
        assert b.take_cancels(),'cancel marked spent before journal write'
