import json
from pathlib import Path
from sniper.office import store
from sniper.office.report import read_state


def test_runtime_shapes_become_public_timeline_and_daily_sheet(tmp_path):
    conn = store.open_store(tmp_path/'office.db')
    for table, identity, payload in [
        ('agents','scout',dict(role='Scout',generation=2,provider='claude',model='configured',persona={'name':'Scout-1'})),
        ('turn_runs','t',dict(actor_id='scout',generation=2,state='running',created_at=100,deadline_at=300,value={'task':'Scan candidates','memo':'Three buyers observed','served_model':'served'})),
        ('tickets','ticket',dict(author='scout',problem='Check liquidity',state='proposed',known_at=140)),
        ('messages','m',dict(actor_id='scout',role='Scout',known_at=150,request={'task':'Scan candidates','memo':'Three buyers observed','served_model':'served'},result={})),
        ('turn_runs','private',dict(actor_id='private',evaluation_derived=True,created_at=160,value={'memo':'PRIVATE'}))]:
        conn.execute('INSERT INTO '+table+' VALUES(?,?)',(identity,json.dumps(payload)))
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200)
    assert s['agents']['items'][0]['task']=='Scan candidates'
    assert s['agents']['items'][0]['served_model']=='served'
    assert any('Three buyers observed' in e['text'] for e in s['activity']['items'])
    assert any('Check liquidity' in e['text'] for e in s['activity']['items'])
    assert 'PRIVATE' not in json.dumps(s)
    row=next(r for r in s['stats']['items'] if r['agent_id']=='scout')
    assert row['turns']==1 and row['calls'] is None and row['cost_usd'] is None
    assert row['hit_rate'] is None and row['paper_pnl_usd'] is None


def test_actual_ledger_path_and_bounded_equity(book, monkeypatch):
    from office.test_ledger import opened,policy,NOW,FX
    from sniper.office import ledger
    conn,quotes,root=book
    opened(conn,quotes,policy())
    conn.commit()
    conn.close()
    old=root/'office'/'ledger.db'
    nested=root/'office'/'ledger'/'ledger.db'
    nested.parent.mkdir(exist_ok=True)
    old.rename(nested)
    monkeypatch.setattr(ledger,'equity',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unbounded')))
    s=read_state(root/'office',now=NOW+10,sol_usd=FX,sol_at=NOW)
    assert s['ledger']['state']=='known'
    assert len(s['positions']['items'])==1
    assert s['positions']['items'][0]['mark_state'] in ('unknown','unpriced')


from office.test_ledger import book


def test_capped_turns_do_not_claim_daily_total(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for i in range(202):
        conn.execute('INSERT INTO turn_runs VALUES(?,?)',(str(i),json.dumps(dict(actor_id='a',created_at=100+i,state='done'))))
    conn.commit();conn.close()
    s=read_state(tmp_path,now=500)
    assert s['stats']['limited'] is True
    assert s['stats']['items'][0]['turns']==200
    assert s['stats']['coverage']=='observed_window'


def test_guard_is_bounded_public_metadata_not_auth_probe(tmp_path):
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    control=tmp_path/'data'/'office-control';(control/'guard').mkdir(parents=True)
    (control/'guard'/'guard.json').write_text(json.dumps({'at':100,'state':'paused','reason':'cpu_pressure','resume_eligible':False,'secret':'PRIVATE'}))
    s=read_state(root,now=200,host_now=102)
    assert s['health']['guard']['reason']=='cpu_pressure'
    assert s['health']['guard']['freshness']=='fresh'
    assert 'PRIVATE' not in json.dumps(s)
    (control/'health.json').symlink_to(control/'guard'/'guard.json')
    assert read_state(root,now=200,host_now=102)['health']['monitor']['state']=='read_error'


def test_handoff_edges_require_explicit_seats_and_references(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for actor in ('scout','judge'):
        conn.execute('INSERT INTO agents VALUES(?,?)',(actor,json.dumps({'role':actor})))
    for ident,payload in [('memo-1',dict(actor_id='scout',known_at=99,text='proposal')),('dangling',dict(from_actor_id='scout',to_actor_id='judge',refs=['missing'],known_at=100)),('real',dict(from_actor_id='scout',to_actor_id='judge',refs=['memo-1'],known_at=100)),('coincidence',dict(actor_id='scout',decision_id='same',known_at=100)),('future',dict(from_actor_id='scout',to_actor_id='judge',refs=['memo-2'],known_at=300))]:
        conn.execute('INSERT INTO messages VALUES(?,?)',(ident,json.dumps(payload)))
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200)
    assert [e['id'] for e in s['handoffs']['items']]==['real']


def test_measured_zero_cost_and_unknown_cost_are_distinct(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for ident,actor,cost in [('one','a',0),('two','b',None)]:
        conn.execute('INSERT INTO spend VALUES(?,?)',(ident,json.dumps(dict(actor_id=actor,at=100,cost_usd=cost))))
    conn.commit();conn.close()
    rows={r['agent_id']:r for r in read_state(tmp_path,now=200)['stats']['items']}
    assert rows['a']['calls']==1 and rows['a']['cost_usd']==0
    assert rows['b']['calls']==1 and rows['b']['cost_usd'] is None


def test_truncated_capital_cannot_publish_exact_cash(book):
    from office.test_ledger import NOW,FX
    conn,quotes,root=book
    for i in range(201):
        conn.execute("INSERT INTO capital_flows(book_id,at,kind,usd,actor,note) VALUES('A',?,'deposit',1,'test','fixture')",(NOW,))
    conn.commit()
    s=read_state(root/'office',now=NOW+10,sol_usd=FX,sol_at=NOW)
    assert s['equity']['state']=='unknown'
    assert s['equity']['books'][0]['cash'] is None
    assert s['equity']['limited'] is True


def test_query_paths_are_indexed_and_never_call_unbounded_frontiers(tmp_path,monkeypatch):
    from sniper.office import supervisor
    monkeypatch.setattr(supervisor,'progress_frontiers',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('unbounded')))
    conn=store.open_store(tmp_path/'office.db');conn.close()
    assert read_state(tmp_path,now=200)['frontiers']['state']=='observed_window'


def test_evaluation_memo_payload_does_not_escape(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO messages VALUES(?,?)',('private',json.dumps(dict(evaluation_derived=True,actor_id='scout',known_at=100,request={'memo':'PRIVATE'}))))
    conn.commit();conn.close()
    assert 'PRIVATE' not in json.dumps(read_state(tmp_path,now=200))


def test_nested_blind_payloads_never_reach_tickets_or_activity(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for table,identity,payload in [('tickets','ticket',{'problem':'PRIVATE','nested':{'evaluation_derived':True}}),('messages','msg',{'known_at':100,'text':'PRIVATE','request':{'evaluation_derived':True}}),('turn_runs','turn',{'actor_id':'scout','created_at':100,'value':{'memo':'PRIVATE','nested':{'evaluation_derived':True}}})]:
        conn.execute('INSERT INTO '+table+' VALUES(?,?)',(identity,json.dumps(payload)))
    conn.commit();conn.close()
    assert 'PRIVATE' not in json.dumps(read_state(tmp_path,now=200))


def test_whole_response_has_budget_and_declared_identity(tmp_path):
    state=read_state(tmp_path,now=200)
    assert state['read_budget']['queries']<=512
    assert state['read_budget']['bytes']<=1048576
    assert state['identity']['runtime_sha'] is None
    here=Path(__file__).resolve().parents[2].name
    assert state['identity']['served_web_sha']==(here if len(here)==40 and set(here)<=set('0123456789abcdef') else None)    # a pinned release dir (the deploy gate) names its SHA; a plain checkout has none


def test_payload_budget_refuses_large_history_instead_of_growing_response(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for i in range(70):
        conn.execute('INSERT INTO agents VALUES(?,?)',(str(i),json.dumps({'name':'n','padding':'x'*20000})))
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200)
    assert s['office']['state']=='read_error'
    assert s['read_budget']['exhausted'] is True
    assert s['read_budget']['bytes']<=1048576
    assert len(json.dumps(s))<2097152


def test_future_ledger_change_invalidates_equity_at_cut(book):
    from office.test_ledger import opened,policy,NOW,FX
    conn,quotes,root=book
    opened(conn,quotes,policy())
    conn.commit()
    s=read_state(root/'office',now=NOW-10,sol_usd=FX,sol_at=NOW-20)
    assert s['equity']['state']=='unknown'
    assert s['equity']['books'][0]['cash'] is None


def test_paper_outcomes_require_exact_actor_receipt_and_use_close_day(book):
    from office.test_ledger import opened,policy,NOW,FX
    from datetime import datetime,timezone
    conn,quotes,root=book
    opened(conn,quotes,policy())
    conn.execute("UPDATE orders SET state='closed',closed_at=?,exit_usd=40,costs_usd=1 WHERE order_id='o1'",(NOW+86400,))
    conn.execute("INSERT INTO decision_receipts(action_key,decision_id,book_id,bundle_hash,outcome,reason,order_id,risk_version,actor_id,created_at) VALUES('action','decision','A','hash','accepted',NULL,'o1','v1','judge',?)",(NOW,))
    conn.commit()
    public=store.open_store(root/'office'/'office.db');public.close()
    s=read_state(root/'office',now=NOW+86410,sol_usd=FX,sol_at=NOW+86400)
    outcome=next(r for r in s['stats']['items'] if r['paper_pnl_usd'] is not None)
    assert outcome['agent_id']=='judge'
    assert outcome['day']==datetime.fromtimestamp(NOW+86400,timezone.utc).date().isoformat()
    assert outcome['paper_pnl_usd']==9
    assert outcome['hit_rate']==1 and outcome['closed_n']==1


def test_funnel_keeps_raw_intake_filter_and_office_universes_separate(tmp_path):
    import sqlite3
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    desk=root.parent/'desk';feed=desk/'intake'/'pumpfun';feed.mkdir(parents=True)
    with sqlite3.connect(feed/'1970-01-01.db') as conn:
        conn.execute('CREATE TABLE intake_rows(id INTEGER PRIMARY KEY,observed_at REAL)')
        conn.executemany('INSERT INTO intake_rows(observed_at) VALUES(?)',[(None,),(100,)])
    with sqlite3.connect(desk/'intake_emitter.db') as conn:
        conn.execute('CREATE TABLE outcomes(intake_row_ref TEXT PRIMARY KEY,feed TEXT,filter TEXT,reason TEXT,at_host REAL)')
        conn.execute("INSERT INTO outcomes VALUES('row','pumpfun','new','too_young',100)")
    s=read_state(root,now=200,host_now=200)
    rows=s['funnel']['items']
    assert next(r for r in rows if r['source']=='Desk intake' and r['stage']=='pumpfun')['count']==2
    assert next(r for r in rows if r['source']=='Desk filter')['count']==1
    assert next(r for r in rows if r['source']=='Desk intake' and r['stage']=='fomo')['count'] is None
    assert 'conversion_rate' not in s['funnel']


def test_malformed_public_runtime_config_is_unknown_not_server_error(tmp_path):
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    config=tmp_path/'config'/'office';config.mkdir(parents=True)
    (config/'runtime.json').write_text('[]')
    s=read_state(root,now=200)
    assert s['identity']['runtime_sha'] is None
    assert s['budget']['state']=='unknown'
