"""Offline host order path; actual persisted receipts, paper stores and ledger transactions."""
import hashlib
import importlib
import json
from dataclasses import replace

import pytest

from sniper.office import store as S, ledger as L
from sniper.office.tools import sheet as SH
from office.test_ledger import book, policy, NOW, FX, MINT, manage
from office.test_pilot_seats import safe, ctx


@pytest.fixture
def case(book,tmp_path):
    conn,quotes,_=book
    store=S.open_store(tmp_path/'office.db')
    request=dict(network_id='solana',mint=MINT,pool='P')
    sheet={'safety':safe(),'pool_state':{'base_decimals':6},'ticker':'TEST'}
    receipt=dict(kind='sheet',state='known',contract=SH.OFFICE_SHEET_CONTRACT['version'],
        request=request,cache_key=SH._sha(request),definition_bundle_hash='d'*64,strategy_epoch='strategy',
        clock_sample={'corrected_utc':NOW-1},sheet=sheet,sheet_hash=SH._sha(sheet),lenses={},lenses_hash=SH._sha({}))
    rid=S.append(store,'receipts',receipt)
    candidate=dict(receipt_row_hash=rid,cache_key=receipt['cache_key'],sheet_hash=receipt['sheet_hash'],
                   mint=MINT,pool='P',feed='pass',sheet=sheet,lenses={})
    context=ctx(book_id='A',epoch_id='E1',cut={'knowledge_cut':NOW},deadline_at=NOW+60)
    judgements={slot:dict(decision='ENTER',memo='m',served_model=model,receipt_row_hash=rid)
                for slot,model in [('judge_claude','claude-opus-5-5'),('judge_gpt','gpt-6.1')]}
    yield store,conn,context,candidate,judgements
    store.close()


def enter(case,**over):
    module=importlib.import_module('sniper.office.eval_orders')
    store,ledger,context,candidate,judgements=case
    return module.enter(store,ledger,policy(),**dict(context=context,candidate=candidate,
        judgements=judgements,now=NOW,sol_usd=FX,sol_at=NOW,**over))


def test_unanimous_bound_judges_submit_once_and_keep_verified_metadata(case):
    store,ledger,context,candidate,judgements=case
    first=enter(case)
    assert first['state']=='submitted' and first['bundle_hash']
    assert enter(case)==first
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1
    row=ledger.execute('SELECT * FROM orders').fetchone()
    assert row['ticker']=='TEST' and row['decimals']==6 and row['metadata_known_at']==NOW-1
    assert row['epoch_id']=='E1' and row['book_id']=='A'


def test_committed_decision_cannot_be_replayed_with_changed_judgements(case):
    assert enter(case)['state']=='submitted'
    case[4]['judge_gpt']['decision']='WAIT'
    changed=enter(case)
    assert changed['state']=='refused' and changed['reason']=='decision_identity_conflict'
    assert case[1].execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1


@pytest.mark.parametrize('after_commit',[False,True])
def test_crash_between_bundle_and_ledger_or_after_ledger_commit_retries_once(case,monkeypatch,after_commit):
    module=importlib.import_module('sniper.office.eval_orders')
    execute=module.T.execute_decision
    def crash(*args,**kwargs):
        if after_commit:execute(*args,**kwargs)
        raise SystemExit('lost host acknowledgement')
    monkeypatch.setattr(module.T,'execute_decision',crash)
    with pytest.raises(SystemExit):enter(case)
    assert case[0].execute('SELECT COUNT(*) FROM decision_bundles').fetchone()[0]==1
    monkeypatch.setattr(module.T,'execute_decision',execute)
    assert enter(case)['state']=='submitted'
    assert case[1].execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1


@pytest.mark.parametrize('decision,outcome',[('WAIT','split'),('SKIP','split'),(None,'unknown'),('BOGUS','unknown')])
def test_split_or_unknown_never_creates_an_order(case,decision,outcome):
    case[4]['judge_gpt']['decision']=decision
    got=enter(case)
    assert got['outcome']==outcome and got['state'] in ('refused','unknown')
    assert case[1].execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


@pytest.mark.parametrize('decision',['WAIT','SKIP'])
def test_known_nonentry_verdicts_remain_distinct(case,decision):
    for row in case[4].values():row['decision']=decision
    got=enter(case)
    assert got['state']=='refused' and got['outcome']==decision
    assert case[1].execute('SELECT COUNT(*) FROM decision_receipts').fetchone()[0]==1


@pytest.mark.parametrize('damage',['family','judge_receipt','candidate_identity','body','hash','future','epoch','role'])
def test_unknown_or_wrong_identity_never_reaches_submission(case,damage):
    store,ledger,context,candidate,judgements=case
    if damage=='family':judgements['judge_gpt']['served_model']='claude-echo'
    elif damage=='judge_receipt':judgements['judge_gpt']['receipt_row_hash']='a'*64
    elif damage=='candidate_identity':candidate['mint']='different'
    elif damage=='body':candidate['sheet']={'safety':safe()}
    elif damage=='hash':candidate['receipt_row_hash']='a'*64
    elif damage=='future':context=replace(context,cut={'knowledge_cut':NOW-2})
    elif damage=='epoch':context=replace(context,epoch_id='wrong')
    elif damage=='role':context=replace(context,role='judge')
    got=enter((store,ledger,context,candidate,judgements))
    assert got['state'] in ('unknown','refused') and got['reason']
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


def test_frozen_decision_cannot_switch_receipts_and_conflicting_coin_is_refused(case):
    first=enter(case)
    other=replace(case[2],decision_id='another')
    got=enter((case[0],case[1],other,case[3],case[4]))
    assert got['state']=='refused' and got['reason']=='coin_already_entered'
    assert case[1].execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1


def asker(slot='judge_claude',decision_id='d-exit',other=None):
    return dict(slot=slot,decision_id=decision_id,answers={'judge_claude':'EXIT' if slot=='judge_claude' else other,'judge_gpt':'EXIT' if slot=='judge_gpt' else other})


def test_scoped_exit_is_delayed_full_close_and_replay_keeps_original_deadline(case):
    module=importlib.import_module('sniper.office.eval_orders')
    got=enter(case);ledger=case[1]
    manage(ledger,policy(),NOW+L.ENTRY_DELAY_S+1)
    result=module.exit(ledger,context=seen_open(case),order_id=got['order_id'],reason='judge_exit',now=NOW+10,asker=asker())
    assert result['state']=='submitted'
    row=ledger.execute('SELECT * FROM orders').fetchone()
    assert row['exit_by']=='judge' and row['exit_requested_by']=='judge'
    assert row['state']=='open'
    module.exit(ledger,context=seen_open(case),order_id=got['order_id'],reason='judge_exit',now=NOW+11,asker=asker())
    assert ledger.execute('SELECT due_at FROM orders').fetchone()[0]==NOW+10+L.EXIT_DELAY_S
    manage(ledger,policy(),NOW+10+L.EXIT_DELAY_S+1)
    assert ledger.execute('SELECT state FROM orders').fetchone()[0]=='closed'


@pytest.mark.parametrize('field,value',[('book_id','other'),('epoch_id','other'),('role','judge')])
def test_exit_refuses_another_book_epoch_or_role(case,field,value):
    module=importlib.import_module('sniper.office.eval_orders')
    got=enter(case);manage(case[1],policy(),NOW+L.ENTRY_DELAY_S+1)
    context=replace(seen_open(case),**{field:value})
    result=module.exit(case[1],context=context,order_id=got['order_id'],reason='exit',now=NOW+10,asker=asker())
    assert result['state']=='refused'
    assert case[1].execute('SELECT exit_requested_at FROM orders').fetchone()[0] is None


def opened(case):
    got=enter(case);manage(case[1],policy(),NOW+L.ENTRY_DELAY_S+1)
    return importlib.import_module('sniper.office.eval_orders'),got['order_id']


def seen_open(case):
    filled=case[1].execute('SELECT filled_at FROM orders').fetchone()[0]
    return replace(case[2],cut={'knowledge_cut':filled+1})


def askers(ledger):
    return [tuple(r) for r in ledger.execute('SELECT slot,decision_id,reason,answers_json FROM exit_askers ORDER BY slot')]


def test_exit_records_who_asked_and_what_the_other_judge_committed(case):
    module,oid=opened(case)
    r=module.exit(case[1],context=seen_open(case),order_id=oid,reason='first',now=NOW+10,asker=asker('judge_claude',other='WAIT'))
    assert r['state']=='submitted'
    assert askers(case[1])==[('judge_claude','d-exit','first',json.dumps({'judge_claude':'EXIT','judge_gpt':'WAIT'},sort_keys=True))]


def test_second_judge_exit_with_another_reason_is_an_additional_asker_not_a_refusal(case):
    module,oid=opened(case)
    module.exit(case[1],context=seen_open(case),order_id=oid,reason='first',now=NOW+10,asker=asker('judge_claude'))
    r=module.exit(case[1],context=seen_open(case),order_id=oid,reason='second',now=NOW+12,asker=asker('judge_gpt','d-exit2'))
    assert r['state']=='submitted' and r['outcome']=='EXIT'
    assert [a[0] for a in askers(case[1])]==['judge_claude','judge_gpt']
    row=case[1].execute('SELECT exit_reason,due_at FROM orders').fetchone()
    assert row['exit_reason']=='first' and row['due_at']==NOW+10+L.EXIT_DELAY_S


def test_same_slot_replay_is_idempotent_and_a_changed_reason_conflicts(case):
    module,oid=opened(case)
    for _ in range(2):
        assert module.exit(case[1],context=seen_open(case),order_id=oid,reason='r',now=NOW+10,asker=asker())['state']=='submitted'
    assert len(askers(case[1]))==1
    r=module.exit(case[1],context=seen_open(case),order_id=oid,reason='other',now=NOW+11,asker=asker())
    assert r['state']=='refused' and r['reason']=='exit_asker_conflict' and len(askers(case[1]))==1


@pytest.mark.parametrize('bad',[dict(slot='head',decision_id='d',answers={}),
    dict(slot='judge_claude',decision_id='d',answers={'judge_claude':'WAIT','judge_gpt':'WAIT'}),
    dict(slot='judge_claude',decision_id='',answers={'judge_claude':'EXIT','judge_gpt':None}),
    dict(slot='judge_claude',decision_id='d',answers={'judge_claude':'EXIT'})])
def test_invalid_asker_requests_nothing(case,bad):
    module,oid=opened(case)
    r=module.exit(case[1],context=seen_open(case),order_id=oid,reason='r',now=NOW+10,asker=bad)
    assert r['state']=='refused' and r['reason']=='exit_asker_invalid'
    assert case[1].execute('SELECT exit_requested_at FROM orders').fetchone()[0] is None and askers(case[1])==[]


def test_judge_ask_after_rails_is_recorded_but_still_refused(case):
    module,oid=opened(case)
    case[1].execute("UPDATE orders SET exit_requested_at=?,exit_reason='sl',exit_requested_by='rails',exit_by='rails',due_at=? WHERE order_id=?",(NOW+9,NOW+9+L.EXIT_DELAY_S,oid))
    r=module.exit(case[1],context=seen_open(case),order_id=oid,reason='late',now=NOW+10,asker=asker())
    assert r['state']=='refused' and r['reason']=='exit_already_requested'
    assert [a[0] for a in askers(case[1])]==['judge_claude']


def test_asker_rows_are_append_only(case):
    module,oid=opened(case)
    module.exit(case[1],context=seen_open(case),order_id=oid,reason='r',now=NOW+10,asker=asker())
    for sql in ("UPDATE exit_askers SET reason='x'","DELETE FROM exit_askers"):
        with pytest.raises(Exception):case[1].execute(sql)


def test_second_judge_exit_after_close_is_recorded_when_the_position_was_open_at_its_cut(case):
    module,oid=opened(case)
    module.exit(case[1],context=seen_open(case),order_id=oid,reason='first',now=NOW+10,asker=asker('judge_claude'))
    manage(case[1],policy(),NOW+10+L.EXIT_DELAY_S+1)
    row=case[1].execute('SELECT state,filled_at,closed_at,exit_requested_at,due_at,proceeds_lamports FROM orders').fetchone()
    assert row['state']=='closed'
    before=tuple(row)
    late=replace(seen_open(case),cut={'knowledge_cut':row['closed_at']-1e-3})
    r=module.exit(case[1],context=late,order_id=oid,reason='second',now=NOW+11,asker=asker('judge_gpt','d-exit2'))
    assert r['state']=='refused' and r['reason']=='already_closed' and r['outcome']=='EXIT'
    assert [a[0] for a in askers(case[1])]==['judge_claude','judge_gpt']
    assert tuple(case[1].execute('SELECT state,filled_at,closed_at,exit_requested_at,due_at,proceeds_lamports FROM orders').fetchone())==before


@pytest.mark.parametrize('when',['before_fill','after_close'])
def test_exit_ask_for_a_position_not_open_at_the_judge_cut_leaves_no_trace(case,when):
    module,oid=opened(case)
    module.exit(case[1],context=seen_open(case),order_id=oid,reason='first',now=NOW+10,asker=asker('judge_claude'))
    manage(case[1],policy(),NOW+10+L.EXIT_DELAY_S+1)
    row=case[1].execute('SELECT filled_at,closed_at FROM orders').fetchone()
    cut=row['filled_at']-1e-3 if when=='before_fill' else row['closed_at']+1
    r=module.exit(case[1],context=replace(seen_open(case),cut={'knowledge_cut':cut}),order_id=oid,reason='second',now=NOW+11,asker=asker('judge_gpt','d2'))
    assert r['state']=='refused' and r['reason']=='order_not_open_at_cut'
    assert [a[0] for a in askers(case[1])]==['judge_claude']


def test_first_exit_ask_for_a_position_filled_after_the_judge_cut_requests_nothing(case):
    module,oid=opened(case)
    filled=case[1].execute('SELECT filled_at FROM orders').fetchone()[0]
    early=replace(case[2],cut={'knowledge_cut':filled-1e-3})
    r=module.exit(case[1],context=early,order_id=oid,reason='r',now=NOW+10,asker=asker())
    assert r['state']=='refused' and r['reason']=='order_not_open_at_cut'
    assert case[1].execute('SELECT exit_requested_at FROM orders').fetchone()[0] is None and askers(case[1])==[]


def test_exit_ask_with_a_cut_after_now_requests_nothing(case):
    module,oid=opened(case)
    future=replace(case[2],cut={'knowledge_cut':NOW+1000},deadline_at=NOW+5000)
    r=module.exit(case[1],context=future,order_id=oid,reason='r',now=NOW+10,asker=asker())
    assert r['state']=='refused' and r['reason']=='order_not_open_at_cut'
    assert case[1].execute('SELECT exit_requested_at FROM orders').fetchone()[0] is None and askers(case[1])==[]
