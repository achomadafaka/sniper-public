"""Canonical derived mappings feed the same bounded off-path mint cache."""
import sqlite3
import pytest
from test_desk_ps_metadata_worker import W,MD,WSOL,Rpc,mint_result,setup


def test_canonical_curve_mapping_fills_causal_cache_before_trigger_plus_10(tmp_path):
    clock,now=setup(tmp_path,[('P','M',WSOL,'mapped_curve_derived',1001.)],t0=1000.)
    clock[0]=1006.;rpc=Rpc([(mint_result(),None)])
    result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)
    assert result['read']==1 and len(rpc.calls)==1
    conn=sqlite3.connect(tmp_path/'m.db')
    row=conn.execute('SELECT decimals,metadata_known_at,slot,source FROM mints WHERE mint=?',('M',)).fetchone()
    assert row==(6,1006.,500,MD.SOURCE) and row[1]<=1011.  # trigger at1001 +10
    assert MD.read_metadata(conn,'M',as_of=1005.) is None
    assert MD.read_metadata(conn,'M',as_of=1011.)['metadata_known_at']==1006.
    conn.close()
    assert W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)['read']==0
    assert len(rpc.calls)==1


@pytest.mark.parametrize('status',['unmapped','conflict','mapped_intake','mapped_curve_derived_unverified'])
def test_other_mapping_labels_do_not_gain_metadata_authority(tmp_path,status):
    clock,now=setup(tmp_path,[('P','M',WSOL,status,1001.)],t0=1000.);clock[0]=1006.
    rpc=Rpc([])
    assert W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)['read']==0
    assert rpc.calls==[]


def test_derived_old_mapping_is_not_broadly_backfilled_and_rate_limit_stays_fail_closed(tmp_path):
    clock,now=setup(tmp_path,[('OLD','OLD',WSOL,'mapped_curve_derived',999.),('NEW','NEW',WSOL,'mapped_curve_derived',1001.)],t0=1000.);clock[0]=1006.
    rpc=Rpc([(None,'rate_limited')]);result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)
    assert result['stopped']=='rate_limited' and rpc.calls[0][1][0]=='NEW'
    conn=sqlite3.connect(tmp_path/'m.db');assert conn.execute('SELECT count(*) FROM mints').fetchone()[0]==0;conn.close()
    assert W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now)['stopped']=='cool_down'


def arm_with_first_reach(path,*,at=1005.,mint='HOT',proof=True):
    from sniper.office import large_buyer_arm as A
    conn=A.open_db(path)
    conn.execute("INSERT INTO triggers(pool,wallet,mint,signature,trigger_at,detected_at,state,source_slot,source_tx_index,source_ordinal,source_signature,detect_lag_s,detection_state) VALUES('HP','w',?,'s',?,?,'pending',10,1,0,'s',0.1,'known')",(mint,at,at+.1))
    conn.execute("INSERT INTO reaches(pool,at,wallet,crowd_state) VALUES('HP',?,'w',?)",(at,'known' if proof else 'unknown'))
    conn.commit();conn.close()


def test_fresh_first_reach_prioritizes_only_its_canonical_mint_under_unchanged_budget(tmp_path,monkeypatch):
    arm=tmp_path/'arm.db';arm_with_first_reach(arm,at=1005.)
    priority=W.first_reach_priority(arm,now=1006.)
    assert priority['state']=='known' and priority['mints']==['HOT']
    clock,now=setup(tmp_path,[('COLD','COLD',WSOL,'mapped',1001.),('HP','HOT',WSOL,'mapped_curve_derived',999.),('OLD','OLD',WSOL,'mapped',998.)],t0=1000.);clock[0]=1006.
    monkeypatch.setattr(W,'PER_MIN',1)
    rpc=Rpc([(mint_result(),None)])
    result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now,priority_mints=priority['mints'])
    assert result['read']==1 and result['skipped_budget']==1 and rpc.calls[0][1][0]=='HOT'
    conn=sqlite3.connect(tmp_path/'m.db');assert conn.execute('SELECT mint FROM mints').fetchall()==[('HOT',)];conn.close()
    assert W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now,priority_mints=priority['mints'])['skipped_budget']==1


@pytest.mark.parametrize('at,proof',[(940.,True),(1007.,True),(1005.,False)])
def test_old_future_or_unknown_reach_does_not_gain_lookup_priority(tmp_path,at,proof):
    arm=tmp_path/'arm.db';arm_with_first_reach(arm,at=at,proof=proof)
    assert W.first_reach_priority(arm,now=1006.)['mints']==[]


def test_missing_or_unreadable_priority_source_is_unknown_not_empty_proof(tmp_path,monkeypatch):
    result=W.first_reach_priority(tmp_path/'missing.db',now=1006.)
    assert result['state']=='unknown' and result['reason']=='arm_missing'
    arm=tmp_path/'arm.db';arm_with_first_reach(arm)
    def failed(*args,**kwargs):raise sqlite3.OperationalError('interrupted')
    monkeypatch.setattr(W,'ro',failed)
    result=W.first_reach_priority(arm,now=1006.)
    assert result['state']=='unknown' and result['mints']==[]


def test_priority_does_not_convert_an_unmapped_pool_into_lookup_authority(tmp_path):
    clock,now=setup(tmp_path,[('HP','HOT',WSOL,'unmapped',1001.)],t0=1000.);clock[0]=1006.
    rpc=Rpc([])
    result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now,priority_mints=['HOT'])
    assert result['read']==0 and rpc.calls==[]


@pytest.mark.parametrize('priority',[[],['MISSING']])
def test_pilot_priority_only_never_spends_general_budget_without_a_target(tmp_path,priority):
    clock,now=setup(tmp_path,[('COLD','COLD',WSOL,'mapped_curve_derived',1001.)],t0=1000.);clock[0]=1006.
    rpc=Rpc([])
    result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now,priority_mints=priority,priority_only=True)
    assert result['read']==0 and rpc.calls==[]


def test_pilot_priority_only_reads_only_target_mint_and_never_pool_or_general(tmp_path):
    clock,now=setup(tmp_path,[('COLD','COLD',WSOL,'mapped',1001.),('HOT','HOT',WSOL,'mapped_curve_derived',1002.)],t0=1000.);clock[0]=1006.
    rpc=Rpc([(mint_result(),None)])
    result=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now,priority_mints=['HOT'],priority_only=True,pool_account=True)
    assert result['read']==1 and result['pool_read']==0 and len(rpc.calls)==1
    assert rpc.calls[0][1][0]=='HOT'


@pytest.mark.parametrize('env,expected',[({},False),({'MEMECOIN_LARGE_BUYER_ENABLED':'1','MEMECOIN_DESK_ENABLED':'0'},True),({'MEMECOIN_LARGE_BUYER_ENABLED':'0','MEMECOIN_DESK_ENABLED':'0'},False),({'MEMECOIN_LARGE_BUYER_ENABLED':'1','MEMECOIN_DESK_ENABLED':'1'},False)])
def test_priority_only_mode_uses_existing_pilot_and_desk_flags(env,expected):
    assert W.priority_only_mode(env) is expected


def test_legacy_priority_schema_is_typed_unknown_without_crashing(tmp_path):
    arm=tmp_path/'legacy.db';conn=sqlite3.connect(arm)
    conn.execute('CREATE TABLE triggers(pool,wallet,mint,signature,trigger_at,detected_at)')
    conn.execute("INSERT INTO triggers VALUES('P','w','M','s',1005,1005.1)");conn.commit();conn.close()
    result=W.first_reach_priority(arm,now=1006.)
    assert result['state']=='unknown' and result['mints']==[]


def test_ordinary_mode_has_no_priority_path_and_does_not_open_arm(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('ordinary mode must not read arm')
    monkeypatch.setattr(W,'ro',forbidden)
    assert W.first_reach_priority(now=1006.)['state']=='not_requested'
