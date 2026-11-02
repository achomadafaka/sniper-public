import sqlite3

import pytest

from sniper.office import ledger as L,seat_events as E,v2_orders as O

NOW=2000000000.


@pytest.fixture
def conn():
    c=sqlite3.connect(':memory:',isolation_level=None);E.ensure_schema(c);O.ensure_schema(c)
    c.execute("INSERT INTO office_v2_roster VALUES('j1','ep','judge',1,2,0,1.)");c.execute("INSERT INTO office_v2_roster VALUES('h1','ep','head',1,1,0,1.)")
    return c


def _enter(conn,**kw):
    args=dict(epoch_id='ep',book_id='b',actor_id='j1',role='judge',incarnation=2,candidate=dict(network_id='solana',mint='M'),feed_known_at=[NOW-5],reason='why',labels={},now=NOW)
    args.update(kw)
    return O.enter(conn,None,None,**args)


@pytest.mark.parametrize('kw,reason',[(dict(role='head'),'role_cannot_order'),(dict(role='scout'),'role_cannot_order'),(dict(incarnation=1),'stale_incarnation'),(dict(actor_id='ghost'),'seat_not_in_epoch'),
                                      (dict(epoch_id='other'),'seat_not_in_epoch'),(dict(reason=' '),'reason_invalid'),(dict(reason='x'*161),'reason_invalid'),(dict(candidate=dict(mint='M')),'candidate_invalid')])
def test_refused_before_any_ledger_write(conn,kw,reason):
    assert _enter(conn,**kw)['reason']==reason


def test_halt_refuses_and_only_head_or_operator_toggle_it(conn):
    assert O.set_halt(conn,epoch_id='ep',actor_id='j1',role='judge',action='halt',reason='x',now=NOW)['state']=='refused'
    assert O.set_halt(conn,epoch_id='ep',actor_id='h1',role='head',action='halt',reason='x',now=NOW)['state']=='halt'
    assert _enter(conn)['reason']=='book_halted'
    assert O.set_halt(conn,epoch_id='ep',actor_id='j1',role='judge',action='resume',reason='x',now=NOW)['state']=='refused'
    assert _enter(conn)['reason']=='book_halted'
    O.set_halt(conn,epoch_id='ep',actor_id='h1',role='head',action='resume',reason='x',now=NOW)
    assert not O.halted(conn,epoch_id='ep')


def test_source_is_host_assigned_and_a_ticket_after_the_cut_does_not_count():
    assert O.source_of([NOW-1],cut=NOW)=='feed' and O.source_of([NOW+1],cut=NOW)=='self' and O.source_of([],cut=NOW)=='self' and O.source_of([None],cut=NOW)=='self'


def test_unregistered_coin_refused_registered_self_named(conn):
    assert _enter(conn,feed_known_at=[])['reason']=='candidate_not_registered'
    O.register(conn,epoch_id='ep',actor_id='j1',network_id='solana',mint='M',pool=None,now=NOW)
    out=_enter(conn,feed_known_at=[])
    assert out['reason']=='self_sheet_unavailable' and out['source']=='self'


def test_first_registration_keeps_its_time(conn):
    first=O.register(conn,epoch_id='ep',actor_id='j1',network_id='solana',mint='M',pool=None,now=NOW)
    again=O.register(conn,epoch_id='ep',actor_id='h1',network_id='solana',mint='M',pool=None,now=NOW+99)
    assert again['known_at']==first['known_at']==NOW and again['first_actor']=='j1'


def test_order_id_is_one_per_coin_across_incarnations():
    assert O.order_id_for('ep','b','solana','M')==O.order_id_for('ep','b','solana','M')!=O.order_id_for('ep','b','solana','N')


def test_ledger_failure_is_unknown_not_refused(conn):
    out=_enter(conn)   # ledger None: nothing can be proved
    assert out['state']=='unknown'


# Real paper ledger, real receipts: the shape eval_orders is tested on.
from sniper.office import store as S
from sniper.office.tools import sheet as SH
from office.test_ledger import book, policy, NOW as LNOW, FX, MINT, manage
from office.test_pilot_seats import safe


@pytest.fixture
def real(book,tmp_path):
    ledger,quotes,_=book
    store=S.open_store(tmp_path/'office.db')
    E.ensure_schema(store);O.ensure_schema(store)
    store.execute("INSERT INTO office_v2_roster VALUES('j1','E1','judge',1,2,0,1.)");store.commit()
    request=dict(network_id='solana',mint=MINT,pool='P')
    sheet={'safety':safe(),'pool_state':{'base_decimals':6},'ticker':'TEST'}
    receipt=dict(kind='sheet',state='known',contract=SH.OFFICE_SHEET_CONTRACT['version'],request=request,cache_key=SH._sha(request),definition_bundle_hash='d'*64,strategy_epoch='strategy',
                 clock_sample={'corrected_utc':LNOW-1},sheet=sheet,sheet_hash=SH._sha(sheet),lenses={},lenses_hash=SH._sha({}))
    rid=S.append(store,'receipts',receipt)
    candidate=dict(network_id='solana',receipt_row_hash=rid,cache_key=receipt['cache_key'],sheet_hash=receipt['sheet_hash'],mint=MINT,pool='P',feed='pass',sheet=sheet,lenses={})
    yield store,ledger,candidate
    store.close()


def _real(real,**kw):
    store,ledger,candidate=real
    args=dict(epoch_id='E1',book_id='A',actor_id='j1',role='judge',incarnation=2,candidate=candidate,feed_known_at=[LNOW-5],reason='why',labels=dict(tainted_by_web=True),now=LNOW,sol_usd=FX,sol_at=LNOW)
    args.update(kw)
    return O.enter(store,ledger,policy(),**args)


def test_a_judge_alone_places_one_paper_order_with_labels(real):
    store,ledger,_=real
    first=_real(real)
    assert first['state']=='submitted' and first['source']=='feed' and first['sheet_age_s']==1
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1
    label=O.order_label(store,ledger,first['order_id'])
    assert label['source']=='feed' and label['incarnation']==2 and label['labels']['tainted_by_web'] is True


def test_retry_and_other_judge_on_the_same_coin_make_no_second_order(real):
    store,ledger,_=real
    first=_real(real);again=_real(real)
    assert again['state']=='duplicate' and again['order_id']==first['order_id']
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1


def test_stale_sheet_refused_and_no_order(real):
    out=_real(real,now=LNOW+O.MAX_SHEET_AGE_S+5,sol_at=LNOW+O.MAX_SHEET_AGE_S+5,feed_known_at=[LNOW-5])
    assert out['reason']=='sheet_stale' and real[1].execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


def test_forged_sheet_in_the_candidate_is_refused(real):
    store,ledger,candidate=real
    forged=dict(candidate,sheet=dict(candidate['sheet'],ticker='FAKE'))
    assert _real(real,candidate=forged)['state']=='unknown'
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


def test_a_crash_at_submission_leaves_the_label_never_an_unlabelled_order(real,monkeypatch):
    store,ledger,_=real
    def boom(*a,**k):raise ValueError('crash')
    monkeypatch.setattr(O.T,'execute_decision',boom)
    assert _real(real)['state']=='unknown'
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0
    assert store.execute('SELECT source FROM office_v2_order_labels').fetchone()[0]=='feed'


def test_the_submitting_decision_owns_the_label_not_an_abandoned_attempt(real,monkeypatch):
    store,ledger,_=real
    store.execute("INSERT INTO office_v2_roster VALUES('j2','E1','judge',1,1,0,1.)");store.commit()
    real_execute=O.T.execute_decision
    monkeypatch.setattr(O.T,'execute_decision',lambda *a,**k:(_ for _ in ()).throw(ValueError('crash')))
    assert _real(real,labels=dict(tainted_by_web=False))['state']=='unknown'
    monkeypatch.setattr(O.T,'execute_decision',real_execute)
    out=_real(real,actor_id='j2',incarnation=1,labels=dict(tainted_by_web=True))
    assert out['state']=='submitted'
    label=O.order_label(store,ledger,out['order_id'])
    assert label['actor_id']=='j2' and label['labels']['tainted_by_web'] is True


def test_an_order_with_no_matching_label_reads_missing_never_feed(real):
    store,ledger,_=real
    out=_real(real);store.execute('DELETE FROM office_v2_order_labels');store.commit()
    assert O.order_label(store,ledger,out['order_id']) is None


def test_a_later_retry_records_its_own_sheet_age(real,monkeypatch):
    store,ledger,_=real
    real_execute=O.T.execute_decision
    monkeypatch.setattr(O.T,'execute_decision',lambda *a,**k:(_ for _ in ()).throw(ValueError('crash')))
    _real(real)
    monkeypatch.setattr(O.T,'execute_decision',real_execute)
    out=_real(real,now=LNOW+10,sol_at=LNOW+10)
    assert out['state']=='submitted' and O.order_label(store,ledger,out['order_id'])['sheet_age_s']==out['sheet_age_s']==11


def test_the_sheet_expiry_is_written_with_the_order_itself():
    from pathlib import Path
    source=Path('sniper/office/v2_orders.py').read_text()
    assert "'sheet_expires_at':now-age+MAX_SHEET_AGE_S" in source and 'UPDATE orders SET sheet_expires_at' not in source


def _refusing(monkeypatch,reason):
    real_submit=L.submit
    monkeypatch.setattr(L,'submit',lambda conn,policy,**kw:L.SubmitResult(kw['order_id'],'refused',reason))
    return real_submit


@pytest.mark.parametrize('reason',sorted(O.REVERSIBLE_REFUSALS))
def test_a_reversible_refusal_does_not_burn_the_coin(real,monkeypatch,reason):
    store,ledger,_=real
    real_submit=_refusing(monkeypatch,reason)
    assert _real(real)['reason']==reason
    assert _real(real)['reason']==reason      # still refused while the state lasts, a new attempt each time, not duplicate
    monkeypatch.setattr(L,'submit',real_submit)
    filled=_real(real)
    assert filled['state']=='submitted' and ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1
    again=_real(real)
    assert again['state']=='duplicate' and again['order_id']==filled['order_id']
    label=O.order_label(store,ledger,filled['order_id'])
    assert label['source']=='feed' and label['labels']['attempt_n']==2 and label['labels']['attempt_chain']==O.order_id_for('E1','A','solana',MINT)


def test_a_verdict_refusal_still_ends_the_coin(real,monkeypatch):
    _refusing(monkeypatch,'mint_blocked')
    assert _real(real)['state']=='refused' and _real(real)['state']=='duplicate'
