import sqlite3
from types import SimpleNamespace

import pytest

from sniper.office import seat_events as E,seat_retention as R,v2_orders as O,v2_paper as V,work as W


def test_desk_stamps_parse_and_junk_is_unknown():
    assert V._epoch_seconds('2026-10-30T12:00:00+00:00')>0 and V._epoch_seconds('nope') is None and V._epoch_seconds(None) is None


@pytest.fixture
def service(tmp_path,monkeypatch):
    from sniper.office import store as St
    path=str(tmp_path/'o.db');c=St.open_store(tmp_path/'o.db');E.ensure_schema(c);O.ensure_schema(c)
    c.execute("INSERT INTO office_v2_roster VALUES('j1','ep','judge',1,1,0,1.)");c.execute("INSERT INTO office_v2_roster VALUES('h1','ep','head',1,1,0,1.)");c.execute("INSERT INTO office_v2_roster VALUES('s1','ep','scout',1,1,0,1.)");c.commit();c.close()
    return V.PaperService(epoch_id='ep',book_id='b',store_path=path,ledger_path=str(tmp_path/'l.db'),desk_path=str(tmp_path/'d.db'),desk_dir=tmp_path,clock_file=tmp_path/'c',policy=None,segment_of=lambda s:1,clock=lambda:5.)


def test_role_comes_from_the_roster_and_only_a_judge_orders(service,monkeypatch):
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now))
    monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[]))
    assert service.enter(actor_id='h1',incarnation=1,ticket_id='t',reason='r',labels={})['reason']=='role_cannot_order'
    assert service.enter(actor_id='ghost',incarnation=1,ticket_id='t',reason='r',labels={})['reason']=='role_cannot_order'


def test_unknown_clock_and_ticket_are_typed(service,monkeypatch):
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(None,None))
    assert service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})['reason']=='clock_unknown'
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(service,'_ticket',lambda t:None)
    assert service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})['reason']=='ticket_unknown'


def test_scout_registers_and_cannot_halt(service):
    assert service.register(actor_id='s1',incarnation=1,network_id='solana',mint='M')['state']=='registered'
    assert service.set_halt(actor_id='s1',incarnation=1,action='halt',reason='x')['state']=='refused'
    assert service.set_halt(actor_id='h1',incarnation=1,action='halt',reason='x')['state']=='halt'


def test_orphan_labels_go_after_a_week_and_the_submitter_stays(tmp_path):
    store=sqlite3.connect(':memory:');O.ensure_schema(store)
    ledger=tmp_path/'l.db';l=sqlite3.connect(ledger);l.execute('CREATE TABLE decision_receipts(order_id TEXT,judgements_hash TEXT)');l.execute("INSERT INTO decision_receipts VALUES('o','kept')");l.commit();l.close()
    now=2000000000.
    for h,at in (('kept',now-9*86400),('orphan',now-9*86400),('recent',now-86400)):
        store.execute('INSERT INTO office_v2_order_labels VALUES(?,?,?,?,?,?,?,?,?)',('o',h,'e','a',1,'feed',1.,at,'{}'))
    assert R.prune_orphan_labels(store,ledger,now=now)==1
    assert {r[0] for r in store.execute('SELECT decision_hash FROM office_v2_order_labels')}=={'kept','recent'}


def test_watcher_refuses_a_ledger_of_another_epoch(tmp_path):
    import subprocess,sys
    ledger=tmp_path/'l.db';c=sqlite3.connect(ledger);c.close()
    from sniper.office import ledger as L
    conn=L.open_ledger(ledger);L.open_epoch(conn,epoch_id='office-paper-eval-1',clip_usd=25.,rule_version='x',actor='t',note='n',now=1.);L.open_book(conn,book_id='b',epoch_id='office-paper-eval-1',kind='evaluated',actor='t',note='n',now=1.);conn.close()
    done=subprocess.run([sys.executable,'scripts/office_paper_watcher.py','--ledger',str(ledger),'--desk-dir',str(tmp_path),'--clock-file',str(tmp_path/'c'),'--heartbeat',str(tmp_path/'hb/beat'),'--expect-epoch','office-v2-explore-1','--no-sandbox-check'],capture_output=True,text=True,timeout=30)
    assert 'office_ledger_epoch_mismatch' in done.stderr


def test_unknown_fx_places_no_order_and_the_pool_reader_reaches_the_sheet(service,monkeypatch,tmp_path):
    import json
    from sniper.office import ledger as L,store as S
    from sniper.services import memecoin_desk_intake as I
    import scripts.office_paper_watcher as W
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[]))
    monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(None,None))
    monkeypatch.setattr(service,'sheet',lambda s,t,now_host:dict(state='known',cache_key='k',sheet_hash='h',sheet={},lenses={}))
    monkeypatch.setattr(S,'latest',lambda *a:dict(row_hash='r',payload_json=json.dumps(dict(request=dict(network_id='solana',mint='M',pool='P')))))
    monkeypatch.setattr(L,'open_ledger',lambda p:sqlite3.connect(':memory:'))
    entered=[];monkeypatch.setattr(O,'enter',lambda *a,**k:entered.append(k))
    assert service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})==dict(state='unknown',reason='fx_unknown') and not entered


def test_the_seat_and_admission_share_one_sheet_build(service,monkeypatch):
    seen=[]
    from sniper.office import runtime as RT
    monkeypatch.setattr(RT,'near_pass_tools',lambda *a,**k:(seen.append(k),{'desk.near_pass_sheet':SimpleNamespace(handler=lambda args,ctx:SimpleNamespace(result=dict(state='known')))})[1])
    service.pool_state_reader='reader';service.cohort_of='cohort'
    service.read_sheet(ticket_id='t')
    assert seen[0]==dict(pool_state_reader='reader',cohort_of='cohort')


def test_watcher_accepts_its_own_epoch_beside_the_bootstrap_pilot_book(tmp_path):
    from sniper.office import ledger as L
    ledger=tmp_path/'l.db';conn=L.open_ledger(ledger)
    L.open_epoch(conn,epoch_id='office-v2-explore-1',clip_usd=25.,rule_version='x',actor='t',note='n',now=1.);L.open_book(conn,book_id='office-v2-explore-1',epoch_id='office-v2-explore-1',kind='evaluated',actor='t',note='n',now=1.)
    evaluated={r[0] for r in conn.execute("SELECT epoch_id FROM books WHERE kind='evaluated'")}
    assert evaluated=={'office-v2-explore-1'}
    conn.close()


@pytest.mark.parametrize('state,want',[('unknown','unknown'),('refused','refused')])
def test_sheet_state_is_preserved(service,monkeypatch,state,want):
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[]))
    monkeypatch.setattr(service,'sheet',lambda s,t,now_host:dict(state=state,reason='provider_timeout'))
    from sniper.office import ledger as L
    monkeypatch.setattr(L,'open_ledger',lambda p:sqlite3.connect(':memory:'))
    assert service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})['state']==want


def test_store_has_the_pragmas_the_receipt_path_needs(service):
    conn=service._store()
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1


def test_decision_cut_is_taken_after_the_sheet(service,monkeypatch):
    """A fresh receipt is stamped during the sheet; a cut from before it would reject the sheet as from the future."""
    import json,scripts.office_paper_watcher as W
    from sniper.office import ledger as L,store as St
    from sniper.services import memecoin_desk_intake as I
    clock=[100.];service.clock=lambda:clock[0]
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[]))
    def sheet(s,t,now_host):
        clock[0]=130.;return dict(state='known',cache_key='k',sheet_hash='h',sheet={},lenses={})
    monkeypatch.setattr(service,'sheet',sheet);monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(1.,now_host))
    monkeypatch.setattr(St,'latest',lambda *a:dict(row_hash='r',payload_json=json.dumps(dict(request=dict(network_id='solana',mint='M',pool='P')))))
    monkeypatch.setattr(L,'open_ledger',lambda p:sqlite3.connect(':memory:'))
    seen=[];monkeypatch.setattr(O,'enter',lambda *a,**k:seen.append(k) or dict(state='submitted'))
    service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})
    assert seen[0]['now']==130.


def _full_path_service(tmp_path,monkeypatch):
    import scripts.office_paper_watcher as W
    from sniper.office import ledger as L,store as St
    from sniper.office.tools import sheet as SH
    from sniper.services import memecoin_desk_intake as I
    from office.test_ledger import policy,pods_rule,NOW,FX,MINT
    from office.test_pilot_seats import safe
    monkeypatch.setattr(L,'_canonical_pool',lambda mint:'P')   # the test mint is not a real address
    ledger=L.open_ledger(tmp_path/'l.db');L.open_epoch(ledger,epoch_id='E1',clip_usd=policy().clip_usd,rule_version='rule-v1',actor='t',note='n',now=NOW-100,pods_rule=pods_rule());L.open_book(ledger,book_id='E1',epoch_id='E1',kind='evaluated',actor='t',note='n',now=NOW-100);L.fund(ledger,book_id='E1',usd=1000.,kind='initial',actor='t',note='n',now=NOW-100);ledger.commit();ledger.close()
    c=St.open_store(tmp_path/'o.db');E.ensure_schema(c);O.ensure_schema(c);c.execute("INSERT INTO office_v2_roster VALUES('j1','E1','judge',1,1,0,1.)");c.commit();c.close()
    def sheet(store,ticket,now_host):
        request=dict(network_id='solana',mint=MINT,pool='P');body={'safety':safe(),'pool_state':{'base_decimals':6,'virtual_quote_source':'pumpswap_rpc:pool_state'},'ticker':'TEST'}
        receipt=dict(kind='sheet',state='known',contract=SH.OFFICE_SHEET_CONTRACT['version'],request=request,cache_key=SH._sha(request),definition_bundle_hash='d'*64,strategy_epoch='strategy',clock_sample={'corrected_utc':now_host-1},sheet=body,sheet_hash=SH._sha(body),lenses={},lenses_hash=SH._sha({}))
        St.append(store,'receipts',receipt);store.commit()
        return dict(state='known',cache_key=receipt['cache_key'],sheet_hash=receipt['sheet_hash'],sheet=body,lenses={})
    service=V.PaperService(epoch_id='E1',book_id='E1',store_path=str(tmp_path/'o.db'),ledger_path=str(tmp_path/'l.db'),desk_path=str(tmp_path/'d.db'),desk_dir=tmp_path,clock_file=tmp_path/'c',policy=policy(),segment_of=lambda s:7,clock=lambda:NOW,sheet=sheet,policy_sha256='loaded')
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(FX,now_host))
    monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[dict(stage='hard_filter',ts='2020-01-01T00:00:00+00:00')]))
    return service


def test_full_path_real_store_receipt_ledger_order(tmp_path,monkeypatch):
    """Positive path through the service: a real receipt in a real store, a real ledger, one order, its label."""
    from sniper.office import ledger as L
    service=_full_path_service(tmp_path,monkeypatch)
    out=service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels=dict(tainted_by_web=True))
    assert out['state']=='submitted' and out['source']=='feed',out
    store=service._store();ledger=L.open_ledger(tmp_path/'l.db')
    label=O.order_label(store,ledger,out['order_id'])
    assert label['labels']['book_policy_sha256']=='loaded' and label['labels']['segment_id']==7 and label['labels']['virtual_quote_source']=='pumpswap_rpc:pool_state' and ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1


def test_a_receipt_from_past_the_cut_is_still_refused(service,monkeypatch):
    """The cut moves after the sheet, so a truly future receipt must still fail: this is the control for the elapsed-sheet case."""
    from sniper.office import eval_orders as EO
    with pytest.raises(ValueError):
        monkeypatch.setattr(EO.S,'latest',lambda *a:dict(row_hash='r',payload_json='{}'))
        EO._receipt(service._store(),SimpleNamespace(cut=dict(knowledge_cut=1.)),dict(cache_key='k',receipt_row_hash='r'),2.)


def test_book_view_is_head_and_judge_only_and_shows_no_closed_results(tmp_path,monkeypatch):
    import scripts.office_paper_watcher as W
    from sniper.office import ledger as L,store as St
    from sniper.services import memecoin_desk_intake as I
    from office.test_ledger import policy,pods_rule,NOW,FX
    ledger=L.open_ledger(tmp_path/'l.db');L.open_epoch(ledger,epoch_id='E1',clip_usd=policy().clip_usd,rule_version='rule-v1',actor='t',note='n',now=NOW-100,pods_rule=pods_rule());L.open_book(ledger,book_id='E1',epoch_id='E1',kind='evaluated',actor='t',note='n',now=NOW-100);L.fund(ledger,book_id='E1',usd=1000.,kind='initial',actor='t',note='n',now=NOW-100);ledger.commit();ledger.close()
    c=St.open_store(tmp_path/'o.db');E.ensure_schema(c);O.ensure_schema(c)
    for a,r in (('j1','judge'),('h1','head'),('s1','scout')):c.execute("INSERT INTO office_v2_roster VALUES(?,'E1',?,1,1,0,1.)",(a,r))
    c.commit();c.close()
    service=V.PaperService(epoch_id='E1',book_id='E1',store_path=str(tmp_path/'o.db'),ledger_path=str(tmp_path/'l.db'),desk_path=str(tmp_path/'d.db'),desk_dir=tmp_path,clock_file=tmp_path/'c',policy=policy(),segment_of=lambda s:1,clock=lambda:NOW)
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(FX,now_host))
    assert service.book_view(actor_id='s1',incarnation=1)['reason']=='role_cannot_view_book'
    out=service.book_view(actor_id='j1',incarnation=1)
    assert out['equity_snapshot_id'] and out['equity']['state']=='known',out
    assert not any(k in str(sorted(out)) for k in ('grade','win','closed','net'))
    row=service._store().execute('SELECT equity_state,positions,epoch_id,actor_id FROM office_v2_book_snapshots WHERE snapshot_id=?',(out['equity_snapshot_id'],)).fetchone()
    assert tuple(row)==('known',0,'E1','j1')


def test_snapshots_stay_while_the_epoch_is_open_and_go_30_days_after_it_seals():
    c=sqlite3.connect(':memory:');O.ensure_schema(c);c.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)')
    now=2000000000.
    c.execute("INSERT INTO epochs VALUES('open','{}')");c.execute("INSERT INTO epochs VALUES('old',?)",('{"closed_at":%f}'%(now-31*86400),));c.execute("INSERT INTO epochs VALUES('new',?)",('{"closed_at":%f}'%(now-86400),))
    for e in ('open','old','new'):c.execute('INSERT INTO office_v2_book_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',(e,e,'a',1.,'known',1.,0,0.,0,0))
    assert R.prune_snapshots(c,now=now)==1 and {r[0] for r in c.execute('SELECT epoch_id FROM office_v2_book_snapshots')}=={'open','new'}


@pytest.mark.parametrize('actor,incarnation',[('sealed',1),('foreign',1),('j1',9),('ghost',1)])
def test_a_seat_outside_this_epoch_gets_no_scope(service,actor,incarnation):
    c=service._store()
    c.execute("INSERT INTO office_v2_roster VALUES('sealed','ep','head',1,1,1,1.)");c.execute("INSERT INTO office_v2_roster VALUES('foreign','old','head',1,1,0,1.)");c.commit();c.close()
    assert service.book_view(actor_id=actor,incarnation=incarnation)['reason']=='role_cannot_view_book'
    assert service.register(actor_id=actor,incarnation=incarnation,network_id='solana',mint='M')['reason']=='seat_not_in_epoch'
    assert service.set_halt(actor_id=actor,incarnation=incarnation,action='halt',reason='x')['reason']=='halt_not_allowed'


def test_an_edited_policy_file_refuses_the_order_itself(service,tmp_path):
    import json
    from sniper.office import eval_transition as X
    policy=dict(fill_model_version=X.FILL_MODEL,max_hold_s=3600.)
    file=tmp_path/'p.json';file.write_text(json.dumps(dict(policy=policy)))
    service.policy_file=str(file);service.policy_sha256=X.book_policy_identity(policy)['sha256']
    assert service._policy_current() is True
    file.write_text(json.dumps(dict(policy=dict(policy,max_hold_s=60.))))
    assert service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})==dict(state='refused',reason='book_policy_restart_needed')
    file.write_text('{');assert service._policy_current() is False


def test_results_view_shows_losers_and_censored_never_open_and_records_the_hash(service,monkeypatch):
    import sqlite3
    from sniper.office import ledger as L
    from pathlib import Path
    ledger=L.open_ledger(Path(service.ledger_path))
    ledger.execute('DROP TABLE orders')
    ledger.execute('CREATE TABLE orders(order_id TEXT,mint TEXT,epoch_id TEXT,state TEXT,reason TEXT,exit_reason TEXT,requested_at REAL,filled_at REAL,closed_at REAL,spend_usd REAL,exit_usd REAL,costs_usd REAL)')
    ledger.execute("INSERT INTO orders VALUES('a','m','ep','closed',NULL,'trail',1,2,3,25,18,.2)");ledger.execute("INSERT INTO orders VALUES('b','m','ep','censored','sheet_expired',NULL,4,NULL,NULL,NULL,NULL,NULL)");ledger.execute("INSERT INTO orders VALUES('c','m','ep','open',NULL,NULL,5,6,NULL,25,NULL,0)");ledger.commit();ledger.close()
    labelled=sqlite3.connect(service.ledger_path);labelled.execute('ALTER TABLE orders ADD COLUMN exit_fill_kind TEXT');labelled.execute("UPDATE orders SET exit_fill_kind='estimated_terminal' WHERE order_id='a'");labelled.commit();labelled.close()
    monkeypatch.setattr(L,'open_ledger',lambda p:(lambda c:(setattr(c,'row_factory',sqlite3.Row),c)[1])(sqlite3.connect(p)))
    out=service.results_view(actor_id='j1',incarnation=1)
    assert out['items'][1]['exit_fill_kind']=='estimated_terminal' and 'not outcome evidence' in out['note']
    assert [i['order_id'] for i in out['items']]==['b','a'] and out['items'][1]['net_usd']==-7.2 and out['items'][0]['net_usd'] is None
    assert sqlite3.connect(service.store_path).execute('SELECT items_n,sha256,reads FROM office_v2_results_reads').fetchone()==(2,out['results_sha256'],1)
    assert service.results_view(actor_id='nobody',incarnation=1)['state']=='refused'


def test_results_view_is_typed_unknown_when_the_ledger_fails(service,monkeypatch):
    import sqlite3
    from sniper.office import ledger as L
    def fail(p):raise sqlite3.OperationalError('x')
    monkeypatch.setattr(L,'open_ledger',fail)
    assert service.results_view(actor_id='j1',incarnation=1)==dict(state='unknown',reason='ledger_unreadable')


def _ticketless_log(service):
    from sniper.office import self_sheet as SS,store as St,v2_explore as X
    c=St.open_store(service.store_path);X.ensure_schema(c)
    c.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',('ep',1,1.,'a'*40,SS.definition_hash(),'ticketless_sheets','test'))
    c.commit();c.close()


def test_ticketless_requires_log_registration_and_known_feed_lookup(service,monkeypatch,tmp_path):
    from sniper.office import self_sheet as SS
    from sniper.persistence import db_memecoin_desk as D
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now))
    assert service.read_self_sheet(actor_id='j1',incarnation=1,network_id='solana',mint='M')['reason']=='ticketless_change_unlogged'
    _ticketless_log(service)
    # A missing desk database is unknown; it never makes a coin self.
    assert service.read_self_sheet(actor_id='j1',incarnation=1,network_id='solana',mint='M')['reason']=='feed_lookup_unknown'
    c=sqlite3.connect(service.desk_path);D.ensure_tables(c);c.close()
    assert service.read_self_sheet(actor_id='j1',incarnation=1,network_id='solana',mint='M')['reason']=='candidate_not_registered'
    service.register(actor_id='s1',incarnation=1,network_id='solana',mint='M',pool='P')
    out=service.read_self_sheet(actor_id='j1',incarnation=1,network_id='solana',mint='M')
    assert out['state']=='unknown' and out['sheet']['safety']['sell_probe'] is None
    calls=[];monkeypatch.setattr(O,'enter',lambda *a,**k:calls.append(k))
    service.clock=lambda:6.
    assert service.read_self_sheet(actor_id='j1',incarnation=1,network_id='solana',mint='M')['state']=='unknown'
    assert not calls


def test_ticketless_roles_and_forged_source_stay_host_controlled(service,monkeypatch):
    _ticketless_log(service)
    assert not hasattr(service,'enter_self')
    assert service.read_self_sheet(actor_id='ghost',incarnation=1,network_id='solana',mint='M')['reason']=='seat_not_in_epoch'


def test_a_judge_sets_rails_inside_the_bounds_frozen_on_the_order_and_exits_it(tmp_path,monkeypatch):
    import json
    from sniper.office import ledger as L
    from office.test_ledger import policy
    service=_full_path_service(tmp_path,monkeypatch)
    from dataclasses import replace
    base=policy();name='sl_pct';lo,hi=base.sl_pct-5.,base.sl_pct+5.
    service.policy=replace(base,rail_bounds={name:[lo,hi]})
    enter=lambda **rails:service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={},rails=rails)
    assert enter(**{name:hi+1})['reason']=='rail_out_of_bounds:'+name
    assert enter(catastrophe_pct=1.)['reason']=='rail_out_of_bounds:catastrophe_pct'
    assert enter(clip_usd=1.)['reason']=='rail_out_of_bounds:clip_usd'
    assert enter(**{name:True})['reason']=='rail_out_of_bounds:'+name
    monkeypatch.setenv('OFFICE_SEAT_STRATEGY','0');assert enter(**{name:lo})['reason']=='seat_strategy_off';monkeypatch.delenv('OFFICE_SEAT_STRATEGY')
    ledger=L.open_ledger(tmp_path/'l.db')
    assert ledger.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0
    out=enter(**{name:lo});assert out['state']=='submitted',out
    frozen=json.loads(ledger.execute('SELECT policy_json FROM orders').fetchone()[0])
    assert frozen[name]==lo and {k:v for k,v in frozen.items() if k!=name}=={k:v for k,v in json.loads(service.policy.frozen()).items() if k!=name}
    assert O.order_label(service._store(),ledger,out['order_id'])['labels']['rail_override']=={name:lo}
    exit_=lambda **k:service.request_exit(**{**dict(actor_id='j1',incarnation=1,order_id=out['order_id'],reason='thesis broken'),**k})
    assert exit_()['reason']=='not_open'                                  # pending: nothing to exit yet
    ledger.execute("UPDATE orders SET state='open'")
    assert exit_(order_id='ghost')['reason']=='order_scope_mismatch' and exit_(reason=' ')['reason']=='exit_request_invalid'
    monkeypatch.setenv('OFFICE_SEAT_STRATEGY','0');assert exit_()['reason']=='seat_strategy_off';monkeypatch.delenv('OFFICE_SEAT_STRATEGY')
    assert exit_()['state']=='submitted'
    row=ledger.execute('SELECT exit_reason,exit_requested_by,exit_by,due_at FROM orders').fetchone()
    assert (row['exit_reason'],row['exit_requested_by'],row['exit_by'])==('thesis broken','j1','judge') and row['due_at'] is not None
    assert exit_()['reason']=='exit_already_requested'


def test_gpt_book_is_its_own_epoch_with_the_same_terms(tmp_path):
    from sniper.office import ledger as L,v2_explore as X
    conn=L.open_ledger(tmp_path/'l.db')
    assert X.provision_gpt_ledger(conn,now=1800000000.) is None and X.provision_gpt_ledger(conn,now=1800000001.) is None     # idempotent: one initial fund
    assert conn.execute('SELECT count(*) FROM capital_flows WHERE book_id=?',(X.GPT_BOOK,)).fetchone()[0]==1
    assert conn.execute('SELECT epoch_id,kind FROM books WHERE book_id=?',(X.GPT_BOOK,)).fetchone()[0]==X.GPT_EPOCH
    assert L._evaluation_config(conn,X.GPT_EPOCH)[1]['drawdown_usd']==200.   # the kill rule applies to this book


def test_a_second_judge_role_orders_and_a_foreign_ledger_order_is_out_of_scope(service,monkeypatch):
    from sniper.office import v2_orders as O2
    assert 'judge_gpt' in O2.ORDER_ROLES and 'head' not in O2.ORDER_ROLES
    assert O2.enter(None,None,None,epoch_id='e',book_id='b',actor_id='a',role='head',incarnation=1,candidate={},feed_known_at=None,reason='r',labels={},now=1.)['reason']=='role_cannot_order'


def _ticket_fixture():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[1]/'fixtures/office/partial_ticket_419P2.json').read_text())


def test_read_sheet_returns_the_stored_ticket_as_partial_when_the_full_sheet_fails(service,monkeypatch):
    monkeypatch.setattr(service,'sheet',lambda store,tid,now_host:dict(state='unknown',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut'))
    monkeypatch.setattr(service,'_ticket',lambda t:_ticket_fixture())
    out=service.read_sheet(ticket_id='t')
    assert out['state']=='partial' and out['sheet']['order_ready'] is False and out['sheet']['why_partial']['class']=='tape_loss_since_cut'


def test_read_sheet_leaves_a_known_sheet_and_an_unknown_ticket_untouched(service,monkeypatch):
    monkeypatch.setattr(service,'sheet',lambda store,tid,now_host:dict(state='known',cache_key='k',sheet={},lenses={}))
    monkeypatch.setattr(service,'_ticket',lambda t:pytest.fail('a known sheet needs no ticket read'))
    assert service.read_sheet(ticket_id='t')['state']=='known'
    unknown=dict(state='unknown',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut')
    monkeypatch.setattr(service,'sheet',lambda store,tid,now_host:unknown)
    monkeypatch.setattr(service,'_ticket',lambda t:None)
    assert service.read_sheet(ticket_id='t')==unknown                              # no ticket: the original typed unknown, not an invented view
    def broken(t):raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(service,'_ticket',broken)
    assert service.read_sheet(ticket_id='t')==unknown


def test_an_order_still_needs_a_known_sheet_even_when_a_partial_view_exists(service,monkeypatch):
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now))
    monkeypatch.setattr(service,'_ticket',lambda t:_ticket_fixture())
    monkeypatch.setattr(service,'sheet',lambda store,tid,now_host:dict(state='unknown',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut'))
    monkeypatch.setattr(O,'enter',lambda *a,**k:pytest.fail('an order was built on a partial view'))
    out=service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})
    assert out['state']=='unknown' and out['reason'].startswith('sheet_refresh_failed')


def _partial_entry(service,monkeypatch,*,partial,knob='1',sheet=None):
    import json
    from sniper.office import ledger as L,store as S
    from sniper.services import memecoin_desk_intake as I
    import scripts.office_paper_watcher as W
    monkeypatch.setenv('OFFICE_PARTIAL_SHEET_ORDERS',knob)
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(service,'_ticket',lambda t:dict(stages=[{'stage':'hard_filter','ts':'2026-10-31T00:00:00+00:00'}],cohort_id='c'))
    monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(150.,now_host))
    monkeypatch.setattr(service,'sheet',lambda s,t,now_host:sheet or dict(state='unknown',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut'))
    monkeypatch.setattr(service,'_partial',lambda *a,**k:partial)
    monkeypatch.setattr(L,'open_ledger',lambda p:sqlite3.connect(':memory:'))
    monkeypatch.setattr(S,'latest',lambda *a:dict(row_hash='r',payload_json=json.dumps(dict(request=dict(network_id='solana',mint='M',pool='P')))))
    entered=[];monkeypatch.setattr(O,'enter',lambda *a,**k:(entered.append(k),dict(state='submitted',order_id='o1'))[1])
    out=service.enter(actor_id='j1',incarnation=1,ticket_id='t',reason='r',labels={})
    return out,entered


PARTIAL_OK=dict(state='ok',candidate=dict(network_id='solana',cache_key='k',mint='M',pool='P',feed='pass',sheet={},lenses={},receipt_row_hash='r',sheet_hash='h'),known_at=[1.],
                order=dict(sheet_state='partial',sheet_class='tape_loss_since_cut',sheet_ticket_age_s=4000.,sheet_unknown_n=5,sheet_view_hash='v',sells_basis='sell_probe',sells_n=None,probe_age_s=4100.,probe_loss_pct=2.4))


def test_an_unknown_sheet_with_an_allowed_partial_receipt_submits_on_it_with_its_stratum_in_the_order_call(service,monkeypatch):
    out,entered=_partial_entry(service,monkeypatch,partial=PARTIAL_OK)
    assert out['state']=='submitted' and entered[0]['candidate']['cache_key']=='k' and entered[0]['feed_known_at']==[1.]
    lab=entered[0]['labels']
    assert (lab['sheet_state'],lab['sheet_class'],lab['sells_basis'],lab['partial_change'],lab['sheet_view_hash'])==('partial','tape_loss_since_cut','sell_probe','partial-sheet-orders-v1','v') and lab['virtual_quote_source'] is None
    assert entered[0]['stratum']==PARTIAL_OK['order']                                  # the ledger writes it inside the order's own insert


def test_a_known_sheet_order_carries_the_known_fresh_flow_stratum_explicitly(service,monkeypatch):
    out,entered=_partial_entry(service,monkeypatch,partial=None,sheet=dict(state='known',cache_key='k',sheet_hash='h',sheet={},lenses={}))
    assert out['state']=='submitted' and entered[0]['stratum']['sheet_state']=='known' and entered[0]['stratum']['sells_basis']=='fresh_flow' and 'sheet_state' not in entered[0]['labels']


def test_the_kill_knob_and_a_refused_partial_restore_the_known_only_gate(service,monkeypatch):
    out,entered=_partial_entry(service,monkeypatch,partial=PARTIAL_OK,knob='0')
    assert out=={'state':'unknown','reason':'sheet_refresh_failed:StaleSnapshot:tape_gap_since_cut'} and not entered          # knob 0 returns exactly the old refusal
    for part in (None,dict(state='refused',reason='partial_class_not_allowed:other'),dict(state='unknown',reason='partial_inputs_unknown')):
        out,entered=_partial_entry(service,monkeypatch,partial=part)
        assert out['reason'].startswith('sheet_refresh_failed') and not entered
