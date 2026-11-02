"""Codex adversarial witnesses for the desk JUDGE caller (FAIL on 6b58552db, kept as regressions).

Run from the snapshot:
  python3 -m pytest -n 2 /tmp/sniper-crew-coordination/test_judge_witness_6b58552db.py -q --tb=short
No production files, APIs, models, signers, or RPCs are used.
"""
import sys
import json
import sqlite3
import hashlib
import threading
from contextlib import nullcontext, contextmanager
from datetime import timedelta
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SNAPSHOT), str(SNAPSHOT / 'tests')]

import pytest
from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_pipeline import databases, POLICY, AT
from test_memecoin_desk_judge import Clock, T0, make_candidate, feed
from test_memecoin_desk_contract import candidate, evidence, policy


def ready(db, *, basis='subscription_flat', p=1.0, index=None, at=T0):
    cohort = desk.provision_cohort(*db, producer='U', policy={**POLICY,
        'judge_p_admit': p, 'model_billing_basis': basis},
        prompt_version='desk-judge-v6', created_at=AT)
    c = candidate() if index is None else make_candidate(index, at)
    tid = desk.ingest_candidate(db[0], c, cohort_id=cohort, now_iso=at.isoformat())['ticket_id']
    desk.evaluate_candidate(db[0], tid, evidence=evidence(c), policy=policy(), now_iso=at.isoformat())
    return cohort, tid


def answer(sheet, **changes):
    return {'text': json.dumps({'k': sheet['opportunity_id'], 'v':'E', 'e':None,
        'r':[60,20,15], 'c':1, 'enter_ttl_s':5, 'next_check_s':15}), 'is_error':False, 'session_id':'session-stable',
        'result_id':'result-stable', 'usage':{'input_tokens':4,'output_tokens':60},
        'notional_usd':0.0004, 'model_echo':J.MODEL, 'call_error':None,
        'billing_basis':'subscription_flat', **changes}


def run(db, changes=None, *, calls=None, clock=None, record_fn=None):
    calls = [] if calls is None else calls
    clock = Clock() if clock is None else clock
    def model(system, user, **_):
        calls.append(json.loads(user))
        return answer(calls[-1], **(changes or {}))
    out=J.run_once(db[0], lambda:nullcontext(db[1]), call_model=model,
        system_prompt='SYS', worker='w', now_fn=clock, calls_per_hour=100,
        record_fn=record_fn)
    print({'result':out,'outbox_count':len(store.list_outbox(db[0])),
        'costs':[dict(r) for r in db[1].execute('SELECT event_id,usd,provenance FROM memecoin_desk_cohort_costs')]})
    return out


@pytest.mark.parametrize('is_error',[False, True])
def test_missing_stable_result_identity_refuses_and_preserves_unknown(databases, is_error):
    cohort, tid = ready(databases)
    with pytest.raises(ValueError, match='model_cost_receipt_invalid'):
        run(databases, {'session_id':None,'result_id':None,'is_error':is_error})
    assert store.list_outbox(databases[0]) == []
    assert ledger.desk_cohort_accounting(databases[1],cohort)['unknown_costs'] == 1


def test_declared_flat_positive_bill_refuses_and_preserves_unknown(databases):
    cohort, tid = ready(databases)
    with pytest.raises(ValueError, match='model_cost_receipt_invalid'):
        run(databases, {'billing_basis':'subscription_flat','billed_usd':0.25,'charge_id':'charge-1'})
    assert not store.list_outbox(databases[0])
    assert ledger.desk_cohort_accounting(databases[1],cohort)['unknown_costs'] == 1


def test_actual_attributable_bill_survives_frozen_flat_policy(databases):
    cohort, tid = ready(databases)
    run(databases, {'billing_basis':'billed_receipt','billed_usd':0.25,'charge_id':'charge-actual'})
    assert ledger.desk_cohort_accounting(databases[1],cohort)['known_cost_usd'] == 0.25


def test_undeclared_billing_basis_is_unknown_never_flat_zero(databases):
    """Gamma FAIL on dec3a9439: the caller defaulted a missing basis to subscription_flat."""
    cohort, tid = ready(databases)
    with pytest.raises(ValueError, match='model_cost_receipt_invalid'):
        run(databases, {'billing_basis': None})
    assert store.list_outbox(databases[0]) == []
    acct = ledger.desk_cohort_accounting(databases[1], cohort)
    assert acct['unknown_costs'] == 1 and acct['known_cost_usd'] == 0


def test_same_sdk_result_identity_cannot_authorize_two_tickets(databases):
    cohort, first = ready(databases, index=0)
    run(databases)
    _, second = ready(databases, index=1)
    with pytest.raises(ValueError):
        run(databases)  # independent text key, replayed session_id/result_id
    assert len(store.list_outbox(databases[0])) == 1


def test_sdk_is_error_true_overrides_success_subtype(databases):
    ready(databases)
    out = run(databases, {'is_error':True, 'subtype':'success'})
    assert out[0]['action'] == 'SKIP'
    assert out[0]['reason'] == 'judge_model_result_error'
    assert not store.list_outbox(databases[0])


def test_out_of_bounds_rails_create_no_intent(databases):
    _, tid = ready(databases)
    raw=json.dumps({'k':tid,'v':'E','e':None,'r':[151,20,15],'c':1,'enter_ttl_s':5,'next_check_s':15})
    out=run(databases,{'text':raw})
    assert out[0]['reason'] == 'rails_out_of_bounds'
    assert not store.list_outbox(databases[0])
    assert not databases[1].execute('SELECT 1 FROM memecoin_manual_intents').fetchall()


def test_crash_after_sdk_result_before_save_is_lost_without_reroll(databases, monkeypatch):
    _,tid=ready(databases)
    calls=[]
    save=store.save_judge_response
    def crash(*a,**k):
        raise RuntimeError('process died between SDK Result and saved response')
    monkeypatch.setattr(store,'save_judge_response',crash)
    with pytest.raises(RuntimeError):
        run(databases,calls=calls)
    monkeypatch.setattr(store,'save_judge_response',save)
    clock=Clock();clock.t=T0+timedelta(seconds=60)
    J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)
    assert run(databases,calls=calls,clock=clock) == []
    assert len(calls)==1
    vet=next(s for s in store.get_ticket(databases[0],tid)['stages'] if s['stage']=='vet')
    assert vet['reason']=='judge_lost'
    assert not store.list_outbox(databases[0])


def test_lost_recovery_after_ledger_half_commit_keeps_original_response_identity(databases, monkeypatch):
    _,tid=ready(databases)
    store.claim_judge_call(databases[0],ticket_id=tid,call_id='crashed',worker='dead',claimed_at=AT)
    clock=Clock();clock.t=T0+timedelta(seconds=60)
    record=store.record_stage
    def crash(*a,**k):
        if k.get('stage')=='vet':
            raise RuntimeError('after expense commit before vet')
        return record(*a,**k)
    monkeypatch.setattr(store,'record_stage',crash)
    with pytest.raises(RuntimeError):
        J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)
    monkeypatch.setattr(store,'record_stage',record)
    clock.t += timedelta(seconds=60)
    J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)
    vet=next(s for s in store.get_ticket(databases[0],tid)['stages'] if s['stage']=='vet')
    assert vet['reason']=='judge_lost' and vet['response_at']==AT


def test_two_worker_claim_race_calls_model_once(databases, monkeypatch):
    ready(databases)
    slow_path=databases[0].execute('PRAGMA database_list').fetchone()[2]
    ledger_path=databases[1].execute('PRAGMA database_list').fetchone()[2]
    barrier=threading.Barrier(2)
    original=store.list_awaiting_judge
    def listed(conn,**kw):
        rows=original(conn,**kw)
        barrier.wait(timeout=5)
        return rows
    monkeypatch.setattr(store,'list_awaiting_judge',listed)
    calls=[]; errors=[]; results=[]
    @contextmanager
    def ledger_factory():
        conn=sqlite3.connect(ledger_path);conn.row_factory=sqlite3.Row
        try:yield conn
        finally:conn.close()
    def worker(name):
        conn=store.connect(slow_path)
        def model(system,user,**_):
            calls.append(name)
            return answer(json.loads(user))
        try:
            results.append(J.run_once(conn,ledger_factory,call_model=model,system_prompt='SYS',
                worker=name,now_fn=Clock(),calls_per_hour=100))
        except BaseException as e:errors.append(repr(e))
        finally:conn.close()
    a=threading.Thread(target=worker,args=('a',)); b=threading.Thread(target=worker,args=('b',))
    a.start();b.start();a.join(10);b.join(10)
    assert not a.is_alive() and not b.is_alive() and not errors
    assert len(calls)==1 and sorted(len(x) for x in results)==[0,1]


def test_sustained_three_times_cph_over_two_hours(databases):
    cohort,_=ready(databases,index=30)
    # Do not judge setup ticket; it is deliberately not part of the overflow denominator.
    store.claim_judge_call(databases[0],ticket_id='desk:U:t030',call_id='setup',worker='setup',claimed_at=AT)
    store.save_judge_response(databases[0],call_id='setup',call_started_at=AT,response_at=AT,
        elapsed_ms=0,result={'call_error':'budget_skipped'})
    J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=Clock())
    calls=[];clock=Clock()
    def model(system,user,**_):
        calls.append(clock.t)
        return answer(json.loads(user),result_id='r-%d'%len(calls))
    for k in range(12): # 6 arrivals/hour, cph=2, 3x overload
        feed(databases,cohort,clock,k,T0+timedelta(minutes=1+10*k),
            call_model=model,calls_per_hour=2)
    assert len(calls)==4
    assert [sum(T0+timedelta(hours=h)<=t<T0+timedelta(hours=h+1) for t in calls) for h in range(2)]==[2,2]
    assert all(sum(t-timedelta(hours=1)<=other<=t for other in calls)<=2 for t in calls)


def test_hash_admission_is_fixed_and_arrival_order_independent(databases):
    cohort,_=ready(databases,index=30,p=0.5)
    tid='desk:U:t030'
    # Compare protocol draw directly against independent sha256 implementation.
    for k in range(31):
        key='desk:U:t%03d'%k
        expected=int.from_bytes(hashlib.sha256(('desk-judge-admit-v1|'+key).encode()).digest()[:8],'big')/(1<<64)
        assert J.hash_unit(key)==expected
    calls=[]; clock=Clock(); selected=[]
    def model(system,user,**_):
        sheet=json.loads(user);calls.append(sheet['opportunity_id'])
        return answer(sheet,result_id='r-%d'%len(calls))
    for k in [7,2,8,1,9,0,6,3,5,4]:
        out=feed(databases,cohort,clock,k,T0+timedelta(minutes=40-k),call_model=model,calls_per_hour=100)
        selected += [r['ticket_id'] for r in out if r['reason']!='judge_budget_skipped']
    want={key for key in [tid]+['desk:U:t%03d'%k for k in range(10)] if J.hash_unit(key)<0.5}
    assert set(calls)==set(selected)==want


def test_immediate_restart_loop_eventually_records_lost_call(databases, monkeypatch, tmp_path):
    from test_memecoin_desk_judge_worker import W
    _,tid=ready(databases)
    store.claim_judge_call(databases[0],ticket_id=tid,call_id='startup-lost',worker='dead',claimed_at=AT)
    slow_path=databases[0].execute('PRAGMA database_list').fetchone()[2]
    ledger_path=databases[1].execute('PRAGMA database_list').fetchone()[2]
    clock=Clock()
    class FakeDatetime:
        @staticmethod
        def now(tz):return clock.t
    class EndWitness(Exception):pass
    original=J.run_once
    passes=[]
    def once(*a,**k):
        out=original(*a,**k);passes.append(out)
        if len(passes)==2:raise EndWitness
        return out
    monkeypatch.setattr(W,'datetime',FakeDatetime)
    monkeypatch.setattr(W.time,'sleep',lambda _:setattr(clock,'t',T0+timedelta(seconds=60)))
    monkeypatch.setattr(J,'run_once',once)
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_ENABLED','1')
    pb=tmp_path/'playbook.md';pb.write_text('prompt_version: `desk-judge-v6`\n---\nSYS')
    with pytest.raises(EndWitness):
        W.main(['--desk-db',slow_path,'--ledger-db',ledger_path,'--playbook',str(pb),
            '--calls-per-hour','2','--loop'])
    vet=[s for s in store.get_ticket(databases[0],tid)['stages'] if s['stage']=='vet']
    assert len(vet)==1 and vet[0]['reason']=='judge_lost'
