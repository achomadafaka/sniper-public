"""Read-only communication metadata; never config/model inference or pooled books."""
import json
import sqlite3
from office.test_v2_feed import fixture,event,read
from sniper.office import quota_measurement as Q,ledger as L


def wake(root,**over):
    (root/'v2').mkdir(exist_ok=True)
    c=sqlite3.connect(root/'v2/quota.db');Q.ensure_schema(c)
    doc=dict(epoch_id='ep',actor_id='a',generation=2,incarnation='3',session_id='s',wake_id='w',account_alias='alias',outcome='ok',counters={},result_id=None,started_at=90.,ended_at=110.,model='requested-model')
    doc.update(over);Q.record_wake(c,**doc);c.close()


def test_model_attribution_matches_identity_and_message_time(tmp_path):
    c=fixture(tmp_path);event(c,1);event(c,2,corrected_at=120.);c.commit();c.close();wake(tmp_path)
    values={e['event_id']:e for e in read(tmp_path)['events']['items']}
    assert values['e1']['model']=='requested-model'
    assert values['e1']['model_basis']=='recorded_wake_requested'
    assert values['e2']['model'] is None


def test_missing_or_foreign_wake_never_guesses_model(tmp_path):
    c=fixture(tmp_path);event(c,1);c.commit();c.close()
    assert read(tmp_path)['events']['items'][0]['model'] is None
    wake(tmp_path,incarnation='999')
    assert read(tmp_path)['events']['items'][0]['model'] is None


def test_gpt_host_state_uses_real_fixed_filename(tmp_path):
    from sniper.office.floor_view import _host_state
    p=tmp_path/'v2/explore-judge-gpt.json';p.parent.mkdir();p.write_text(json.dumps(dict(incarnation=3,sleep_until=250.)))
    import os
    os.utime(p,(100,100))
    assert _host_state(tmp_path,dict(epoch_id='office-v2-explore-1',role='judge_gpt',incarnation=3),200.)['sleep_until']==250.


def test_gpt_book_missing_is_unknown_without_creating_sources(tmp_path):
    from sniper.office.operator_view import _gpt_book
    view=_gpt_book(tmp_path,200.,{})
    assert view['book_id']=='office-v2-explore-gpt-1' and view['state']=='unknown'
    assert not list(tmp_path.rglob('*.db'))


def test_gpt_book_ignores_foreign_book_capital_and_tickers(tmp_path):
    from sniper.office.operator_view import _gpt_book
    path=tmp_path/'v2/explore-gpt-ledger/ledger.db';path.parent.mkdir(parents=True)
    c=L.open_ledger(path)
    from sniper.office.v2_explore import provision_gpt_ledger,GPT_EPOCH
    provision_gpt_ledger(c,now=100.)
    L.open_book(c,book_id='foreign',epoch_id=GPT_EPOCH,kind='evaluated',actor='fixture',note='fixture',now=100.)
    L.fund(c,book_id='foreign',usd=99999.,kind='initial',actor='fixture',note='fixture',now=100.)
    c.commit();c.close()
    out=_gpt_book(tmp_path,200.,dict(sol_usd=100.,sol_at=199.))
    assert out['cash_usd']==1000. and out['book_id']=='office-v2-explore-gpt-1'
    assert out['orders']==[] and '99999' not in json.dumps(out)
    from sniper.office.operator_view import read_operator_report
    full=read_operator_report(tmp_path,200.)
    assert full['state']=='unknown' and full['separate_books'][0]['cash_usd']==1000.


def test_ticker_maker_requires_unambiguous_causal_decision_link():
    from sniper.office.operator_view import _makers
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE decision_receipts(order_id TEXT,actor_id TEXT,created_at REAL)')
    c.executemany('INSERT INTO decision_receipts VALUES(?,?,?)',[('one','head',90),('one','head',95),('two','head',90),('two','judge',90),('future','judge',201)])
    assert _makers(c,200)=={'one':'head'}
    c.executemany('INSERT INTO decision_receipts VALUES(?,?,?)',[('tail','head',100)]*401)
    assert _makers(c,200)=={}
    c.close()


def test_standalone_page_loads_communications_before_floor_controller(tmp_path):
    from flask import Flask
    from sniper.routes.api_office import create_api_office_blueprint
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    app=Flask(__name__,static_folder=str(root/'static'))
    app.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.,operator_authenticated=lambda:False)))
    response=app.test_client().get('/office')
    assert response.status_code==200
    text=response.get_data(as_text=True)
    assert text.index('office-communications.js?v=')<text.index('office-v2.js?v=')


def test_marked_equity_values_a_carried_mark_and_proven_equity_stays_unknown(tmp_path):
    from sniper.office import ledger as L,operator_view as V
    from office.test_ledger import setup,FX,NOW
    conn=L.open_ledger(tmp_path/'l.db');setup(conn,'E1',epoch='E1')
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,filled_at,spend_lamports,tokens_atomic,spend_usd) VALUES('o','E1','E1','evaluated','solana','M','P','d','{}','h','v','open',?,?,'1000000000','1000',25.)",(NOW-20,NOW-10))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES('o',?,'1000000000','ps_paper.sell','carried','{}')",(NOW-1,));conn.commit()
    fx=dict(sol_usd=FX,sol_at=NOW)
    assert L.equity(conn,book_id='E1',now=NOW,mark_max_age_s=60.,require_proven=True,**fx).state!='known'
    out=V._marked(conn,now=NOW,fx=fx,book_id='E1')
    assert out['marked_state']=='known' and out['marked_equity_usd'] is not None and 'estimated' in out['marked_basis']


def test_main_marked_failure_is_unknown_and_never_raises(tmp_path):
    from sniper.office import operator_view as V
    out=V._main_marked(tmp_path,1.,{})
    assert out['marked_state']=='unknown' and out['marked_equity_usd'] is None


def test_watcher_heartbeat_comes_from_its_file_and_missing_is_unknown(tmp_path):
    from sniper.office import operator_view as V
    path=tmp_path/'beat.json'
    assert V._watcher_heartbeat(path,10.)['state']=='unknown'
    path.write_text(json.dumps(dict(state='managed',ts_host=7.,fx_state='known',last_good_utc=6.)));assert V._watcher_heartbeat(path,10.)['state']=='unknown'
    path.write_text(json.dumps(dict(state='managed',ts_host=7.,fx_state='known',last_good_utc=6.,clock_sample=dict(host_utc=7.,corrected_utc=7.))))
    out=V._watcher_heartbeat(path,10.)
    assert out['state']=='known' and out['watcher_state']=='managed' and out['last_good_utc']==6.
    path.write_text('{"state":"managed","ts_host":Infinity}');assert V._watcher_heartbeat(path,10.)['state']=='unknown'
    path.write_text('');assert V._watcher_heartbeat(path,10.)['state']=='unknown'


def test_order_plan_is_the_admitted_decisions_and_known_by_the_read(tmp_path):
    from sniper.office import operator_view as V
    c=sqlite3.connect(tmp_path/'office.db');c.execute('CREATE TABLE office_v2_order_labels(order_id,decision_hash,at,labels_json)')
    c.executemany('INSERT INTO office_v2_order_labels VALUES(?,?,?,?)',[('o','good',10.,json.dumps(dict(entry_reason='r',plan=dict(thesis='admitted')))),('o','other',12.,json.dumps(dict(plan=dict(thesis='abandoned'))))]);c.commit();c.close()
    l=sqlite3.connect(':memory:');l.execute('CREATE TABLE decision_receipts(order_id,judgements_hash,created_at)');l.execute("INSERT INTO decision_receipts VALUES('o','good',10.)")
    assert V._order_plan(tmp_path,l,'o',100.)['plan']==dict(thesis='admitted')
    assert V._order_plan(tmp_path,l,'o',5.)==dict(entry_reason=None,plan=None) and V._order_plan(tmp_path,l,'x',100.)==dict(entry_reason=None,plan=None)
