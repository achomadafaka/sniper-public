"""Post-start evidence reader: fake OS observations, real temporary RO snapshots."""
import importlib
import json
from pathlib import Path
from datetime import datetime,timezone
import pytest
from .test_eval_report import world, T, E, candidate, publish
from sniper.office import store as S,ledger as L
from sniper.office.supervisor import _put

@pytest.fixture(autouse=True)
def _synthetic_epoch_has_a_kill_rule(monkeypatch):
    # These stores use the synthetic epoch E1; the ledger enforces its kill rule only for listed epochs.
    from sniper.office import ledger as _L
    monkeypatch.setattr(_L,'EVAL_EPOCHS',_L.EVAL_EPOCHS+('E1',))



@pytest.fixture
def running(world,monkeypatch):
    H=importlib.import_module('scripts.office_eval_health')
    release=world.root/('a'*40);(release/'scripts').mkdir(parents=True)
    script=release/'scripts/office_supervisor.py';script.write_text('# offline fixture')
    current=world.root/'current';current.symlink_to(release,target_is_directory=True)
    argv=['python3',str(script),'--store',str(world.office)]
    identity={'pid':42,'birth_ticks':100,'uid':1000,'exe':'/usr/bin/python3','argv':argv}
    monkeypatch.setattr(H,'process_identity',lambda pid:dict(identity))
    monkeypatch.setattr(H,'release_identity',lambda path:{'sha':path.name,'tracked_clean':True},raising=False)
    monkeypatch.setattr(H,'utc_lstart',lambda pid:datetime.fromtimestamp(T,timezone.utc).isoformat())
    for i,(role,family) in enumerate((('Head','claude'),('Scout','claude'),('Judge','claude'),('Judge','gpt'),('Postmortem','claude'))):
        _put(world.o,'agents',('head','scout','judge_claude','judge_gpt','postmortem')[i],{'role':role,'family':family,'state':'active','generation':1})
    L.evaluation_safety(world.l,epoch_id=E,now=T+1)
    rid,_=candidate(world,0);did=publish(world,rid)
    S.append(world.o,'receipts',{'kind':'judge.settle','cache_key':'judge.settle:'+did,'decision_id':did,
        'epoch_id':E,'known_at':T+30,'result':{'state':'refused','reason':'max_concurrent'}})
    world.o.commit()
    kw=dict(office_path=world.office,ledger_path=world.ledger,pid=42,expected_release=release,current_link=current,
        expected_argv=argv,expected_exe=identity['exe'],expected_uid=1000,now=T+100,receipt_boundary=T+15,epoch_id=E)
    return H,world,kw,identity


def test_poststart_health_checks_process_release_epoch_roster_receipts_and_readonly_safety(running):
    H,w,kw,identity=running
    before=['\n'.join(c.iterdump()) for c in (w.o,w.l)]
    out=H.inspect(**kw)
    assert out['state']=='known' and out['process']['utc_lstart']==datetime.fromtimestamp(T,timezone.utc).isoformat()
    assert out['release']['expected']==str(kw['expected_release']) and out['agents']['active_count']==5
    assert out['receipts']['first_judge']['known_at']==T+20 and out['receipts']['first_settle']['known_at']==T+30
    assert out['safety']['state']!='unknown' and out['loaded_modules_proven'] is False
    assert before==['\n'.join(c.iterdump()) for c in (w.o,w.l)]
    assert 'equity_usd' not in json.dumps(out)


def test_reused_pid_cannot_combine_database_and_process_evidence(running,monkeypatch):
    H,w,kw,identity=running;seen=[]
    def changed(pid):
        seen.append(1);return dict(identity,birth_ticks=100+len(seen))
    monkeypatch.setattr(H,'process_identity',changed)
    assert H.inspect(**kw)['state']=='unknown'


def test_python_c_script_as_data_is_not_supervisor(running):
    H,w,kw,identity=running
    identity['argv']=['python3','-c','pass',str(kw['expected_release']/'scripts/office_supervisor.py')]
    kw['expected_argv']=identity['argv']
    assert H.inspect(**kw)['state']=='unknown'


def test_wrong_release_roster_epoch_old_receipts_and_missing_safety_remain_unknown(running):
    H,w,kw,_=running
    out=H.inspect(**dict(kw,receipt_boundary=T+50))
    assert out['receipts']['first_judge']['state']=='unknown' and out['receipts']['first_settle']['state']=='unknown'
    _put(w.o,'agents','4',{'role':'Postmortem','family':'claude','state':'inactive','generation':1});w.o.commit()
    assert H.inspect(**kw)['state']=='unknown'
    w.o.execute('UPDATE office_epoch_control SET payload_json=?',(S.canonical_json({'active_epoch':'foreign'}).decode(),));w.o.commit()
    assert H.inspect(**kw)['epoch']['state']=='unknown'
    w.l.execute('DROP TABLE epoch_entry_safety')
    assert H.inspect(**kw)['safety']['state']=='unknown'
    kw['current_link'].unlink();kw['current_link'].symlink_to(w.root/'foreign')
    assert H.inspect(**kw)['state']=='unknown'


def test_mutable_current_entrypoint_is_labeled_and_does_not_prove_loaded_modules(running):
    H,w,kw,identity=running
    identity['argv'][1]=str(kw['current_link']/'scripts/office_supervisor.py')
    out=H.inspect(**kw)
    assert out['state']=='known' and out['loaded_modules_proven'] is False
    assert out['release']['entrypoint_basis']=='current_symlink'


def test_missing_database_returns_unknown_instead_of_raising(running):
    H,w,kw,_=running
    assert H.inspect(**dict(kw,office_path=w.root/'absent.db'))['state']=='unknown'


def test_release_git_identity_and_tracked_dirt_must_match(running,monkeypatch):
    H,w,kw,_=running
    monkeypatch.setattr(H,'release_identity',lambda path:{'sha':'b'*40,'tracked_clean':True})
    assert H.inspect(**kw)['state']=='unknown'
    monkeypatch.setattr(H,'release_identity',lambda path:{'sha':path.name,'tracked_clean':False})
    assert H.inspect(**kw)['state']=='unknown'


def test_old_generation_and_non_judge_actor_do_not_prove_poststart_judgement(running):
    from sniper.office import judge_decisions as J
    from types import SimpleNamespace
    H,w,kw,_=running
    rid,_=candidate(w,1)
    ctx=SimpleNamespace(epoch_id=E,actor_id='head',generation=1,decision_id='wrong-role',cut={'knowledge_cut':T+15})
    J.record(w.o,context=ctx,slot='judge_claude',args={'decision':'WAIT','candidate':rid,'order_id':None,'public_reason':'wrong actor'},
        now=T+16,equity_snapshot_id='s',lesson_ids=[])
    w.o.commit()
    assert H.inspect(**kw)['receipts']['first_judge']['known_at']==T+20
    for aid in ('judge_claude','judge_gpt'):
        p=json.loads(w.o.execute('SELECT payload_json FROM agents WHERE id=?',(aid,)).fetchone()[0]);p['generation']=2
        _put(w.o,'agents',aid,p)
    w.o.commit()
    out=H.inspect(**kw)
    assert out['state']=='unknown' and out['receipts']['first_judge']['state']=='unknown'
