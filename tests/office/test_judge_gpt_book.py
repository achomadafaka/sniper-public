"""The second Judge end to end: SeatTools paper_order through execute_decision to ledger._submit, on its own ledger file and epoch, beside the main book. Fabricated coins, no provider, no production file."""
from dataclasses import replace
from contextlib import closing
import json
import sqlite3

import pytest

from sniper.office import ledger as L,seat_events as E,seat_tools as T,store as St,v2_explore as X,v2_orders as O,v2_paper as V,work as W
from sniper.office.tools import sheet as SH
from office.test_ledger import NOW,FX,MINT
from office.test_pilot_seats import safe

MINTS={'t1':MINT,'t2':MINT[:-1]+('A' if MINT[-1]!='A' else 'B')}


@pytest.fixture
def world(tmp_path,monkeypatch):
    import scripts.office_paper_watcher as WT
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(L,'_canonical_pool',lambda mint:'P')
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(WT,'read_fx',lambda d,now_host,offset:(FX,now_host))
    policy=L.OfficeBookPolicy(**{**json.load(open('config/office/book_policy_pilot.json'))['policy'],'chains':('solana',)})
    main=L.open_ledger(tmp_path/'main.db');L.open_epoch(main,epoch_id='E1',clip_usd=X.CLIP_USD,rule_version=X.EO.PODS_RULE['version'],actor='t',note='n',now=NOW-100,pods_rule=X.EO.PODS_RULE)
    L.open_book(main,book_id='E1',epoch_id='E1',kind='evaluated',actor='t',note='n',now=NOW-100);L.fund(main,book_id='E1',usd=X.CAPITAL_USD,kind='initial',actor='t',note='n',now=NOW-100);main.commit();main.close()
    gpt=L.open_ledger(tmp_path/'gpt.db');assert X.provision_gpt_ledger(gpt,now=NOW-100) is None;gpt.close()
    c=St.open_store(tmp_path/'o.db');W.ensure_schema(c);E.ensure_schema(c);O.ensure_schema(c)
    for actor,role in (('j1','judge'),('jg','judge_gpt'),('h1','head')):c.execute('INSERT INTO office_v2_roster VALUES(?,?,?,1,1,0,1.)',(actor,'E1',role))
    c.commit();c.close()
    def sheet(store,ticket,now_host):
        request=dict(network_id='solana',mint=MINTS[ticket],pool='P');body={'safety':safe(),'pool_state':{'base_decimals':6,'virtual_quote_source':'pumpswap_rpc:pool_state'},'ticker':'TEST'}
        receipt=dict(kind='sheet',state='known',contract=SH.OFFICE_SHEET_CONTRACT['version'],request=request,cache_key=SH._sha(request),definition_bundle_hash='d'*64,strategy_epoch='strategy',clock_sample={'corrected_utc':now_host-1},sheet=body,sheet_hash=SH._sha(body),lenses={},lenses_hash=SH._sha({}))
        St.append(store,'receipts',receipt);store.commit()
        return dict(state='known',cache_key=receipt['cache_key'],sheet_hash=receipt['sheet_hash'],sheet=body,lenses={})
    def service(ledger,book,ledger_epoch,policy=policy):
        s=V.PaperService(epoch_id='E1',book_id=book,ledger_epoch_id=ledger_epoch,store_path=str(tmp_path/'o.db'),ledger_path=str(tmp_path/ledger),desk_path=str(tmp_path/'d.db'),desk_dir=tmp_path,clock_file=tmp_path/'c',policy=policy,segment_of=lambda s:1,clock=lambda:NOW,sheet=sheet,policy_sha256='loaded')
        s._ticket=lambda t:dict(stages=[dict(stage='hard_filter',ts='2020-01-01T00:00:00+00:00')])
        return s
    def seat(actor,paper):
        t=T.SeatTools(actor=actor,epoch='E1',generation=1,incarnation=1,workspace=str(tmp_path),path=str(tmp_path/(actor+'.sock')),db_path=str(tmp_path/'o.db'),clock=lambda:NOW,office_uid=0,paper=paper,wake_labels=dict(model='gpt-6.1-sol' if actor=='jg' else 'claude-opus-5-5'))
        return lambda name,**args:(lambda r:(r['isError'],json.loads(r['content'][0]['text'])))(t._call(name,args))
    class World:pass
    w=World();w.path=tmp_path;w.policy=policy;w.service=service;w.seat=seat
    w.main=service('main.db','E1','E1');w.gpt=service('gpt.db',X.GPT_BOOK,X.GPT_EPOCH)
    w.judge=seat('j1',w.main);w.judge_gpt=seat('jg',w.gpt)
    w.rows=lambda ledger:sqlite3.connect(tmp_path/ledger).execute('SELECT order_id,epoch_id,book_id,mint,policy_json,state FROM orders').fetchall()
    return w


def test_order_lands_on_its_own_book_with_the_configured_clip(world):
    error,out=world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')
    assert not error and out['state']=='submitted',out
    rows=world.rows('gpt.db')
    assert [(r[1],r[2],r[3]) for r in rows]==[(X.GPT_EPOCH,X.GPT_BOOK,MINT)] and world.rows('main.db')==[]
    assert json.loads(rows[0][4])['clip_usd']==world.policy.clip_usd
    label=O.order_label(world.gpt._store(),L.open_ledger(world.path/'gpt.db'),out['order_id'])
    assert label['actor_id']=='jg' and label['labels']['model']=='gpt-6.1-sol'   # joined through the decision hash on the ledger row


def test_same_coin_enters_each_book_once(world):
    assert world.judge('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'
    assert world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'
    assert world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='duplicate'
    assert len(world.rows('gpt.db'))==1 and len(world.rows('main.db'))==1


def test_clip_cannot_be_overridden(world):
    assert world.gpt.enter(actor_id='jg',incarnation=1,ticket_id='t1',reason='r',labels={},rails=dict(clip_usd=1.))['reason']=='rail_out_of_bounds:clip_usd'
    assert world.rows('gpt.db')==[]


def test_concurrency_is_per_book(world):
    one=world.service('gpt.db',X.GPT_BOOK,X.GPT_EPOCH,replace(world.policy,max_concurrent=1));seat=world.seat('jg',one)
    assert world.judge('paper_order',ticket_id='t2',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'   # the other book's order does not count here
    assert seat('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'
    error,out=seat('paper_order',ticket_id='t2',reason='r',thesis='t',disproof='d',exit_plan='e')
    assert error and out['reason']=='max_concurrent',out
    assert len(world.rows('gpt.db'))==1


def test_daily_loss_halt_is_per_book(world,monkeypatch):
    # The sum itself is tested in test_ledger; this proves _submit reads it for this book only.
    monkeypatch.setattr(L,'daily_realized_usd',lambda conn,*,book_id,now:-1e9 if book_id==X.GPT_BOOK else 0.)
    error,out=world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')
    assert error and out['reason']=='daily_loss_halt',out
    assert world.judge('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'


def test_exit_of_the_other_books_order_is_refused(world):
    other=world.judge('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['order_id']
    mine=world.judge_gpt('paper_order',ticket_id='t2',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['order_id']
    assert world.judge_gpt('paper_exit',order_id=other,reason='x')[1]['reason']=='order_scope_mismatch'
    assert world.judge('paper_exit',order_id=mine,reason='x')[1]['reason']=='order_scope_mismatch'
    assert world.judge_gpt('paper_exit',order_id=mine,reason='x')[1]['reason']=='not_open'   # reached its own book


def test_head_halt_stops_both_books(world):
    head=world.seat('h1',world.main)
    assert head('book_halt',action='halt',reason='stop')[1]['state']=='halt'
    assert world.judge('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['reason']=='book_halted'
    assert world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['reason']=='book_halted'
    assert world.judge_gpt('book_halt',action='resume',reason='x')[0] is True
    assert world.rows('gpt.db')==[] and world.rows('main.db')==[]


@pytest.mark.parametrize('tamper',["UPDATE books SET kind='discovery'","UPDATE epoch_pods_rules SET rule_json=json_set(rule_json,'$.max_concurrent_total',1)","UPDATE capital_flows SET usd=1","UPDATE ledger_epochs SET clip_usd=1"])
def test_a_tampered_gpt_ledger_is_not_ready(world,tamper):
    conn=L.open_ledger(world.path/'gpt.db');conn.execute(tamper+(" WHERE book_id='%s'"%X.GPT_BOOK if 'SET kind' in tamper or 'capital' in tamper else " WHERE epoch_id='%s'"%X.GPT_EPOCH));conn.commit()
    assert X.provision_gpt_ledger(conn,now=NOW)=='gpt_book_mismatch'


def test_exit_scope_refuses_a_row_of_another_book_in_the_same_file(world):
    """Files alone already separate the books, so the scope check needs its own witness."""
    order=world.judge_gpt('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['order_id']
    wrong=world.service('gpt.db','other-book',X.GPT_EPOCH);assert world.seat('jg',wrong)('paper_exit',order_id=order,reason='x')[1]['reason']=='order_scope_mismatch'
    wrong=world.service('gpt.db',X.GPT_BOOK,'other-epoch');assert world.seat('jg',wrong)('paper_exit',order_id=order,reason='x')[1]['reason']=='order_scope_mismatch'


def test_book_view_shows_the_halt_paper_order_enforces_on_both_books(world,monkeypatch):
    """Seats read entry state as unknown after a resume because book_view had no flag. Both books share the store epoch's halt."""
    import scripts.office_paper_watcher as W
    from sniper.services import memecoin_desk_intake as I
    monkeypatch.setattr(I,'clock_sample',lambda f,now:(0.,now));monkeypatch.setattr(W,'read_fx',lambda d,now_host,offset:(150.,now_host))
    def shown(seat):
        error,out=seat('book_view')
        assert not error,out
        return out['entry_halt']
    def mark(action):
        with closing(world.main._store()) as store:
            assert O.set_halt(store,epoch_id=world.main.epoch_id,actor_id='op',role='operator',action=action,reason='why',now=5.)['state']==action
    for seat in (world.judge,world.judge_gpt):
        view=shown(seat)
        assert view['state']=='never_set' and view['entries_halted'] is False and view['receipt'] is None
    mark('halt')
    for seat in (world.judge,world.judge_gpt):
        view=shown(seat)
        assert view['state']=='halted' and view['entries_halted'] is True
        assert view['receipt']==dict(action='halt',actor_id='op',at=5.,reason='why')
        assert seat('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['reason']=='book_halted'
    mark('resume')
    for seat in (world.judge,world.judge_gpt):
        view=shown(seat)
        assert view['state']=='resumed' and view['entries_halted'] is False and view['receipt']['action']=='resume'
        assert seat('paper_order',ticket_id='t1',reason='r',thesis='t',disproof='d',exit_plan='e')[1]['state']=='submitted'


def test_a_close_reaches_only_its_own_judge_once_with_plan_and_path(world):
    error,out=world.judge_gpt('paper_order',ticket_id='t1',reason='why',thesis='t',disproof='d',exit_plan='e')
    assert not error
    conn=L.open_ledger(world.path/'gpt.db')
    order=out['order_id']
    now=conn.execute('SELECT requested_at FROM orders WHERE order_id=?',(order,)).fetchone()[0]
    conn.execute("UPDATE orders SET state='closed',filled_at=?,closed_at=?,spend_lamports='1000',peak_lamports='1500',proceeds_lamports='500',exit_reason='catastrophe' WHERE order_id=?",(now,now+60,order))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind) VALUES(?,?, '1500','f','carried')",(order,now+10))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source) VALUES(?,?,NULL,'f')",(order,now+20));conn.commit();conn.close()
    got=world.gpt.postmortems(actor_id='jg',seen=set())
    assert [g['order_id'] for g in got]==[order]
    assert got[0]['entry_reason']=='why' and got[0]['plan']==dict(thesis='t',disproof='d',exit_plan='e')
    assert got[0]['price_path']==[dict(t_s=10,pct=50.,kind='carried'),dict(t_s=20,pct=None,kind=None)] and got[0]['grade']['order_id']==order
    assert got[0]['entry_sheet']['state']=='known'
    store=world.gpt._store();bundle=json.loads(store.execute('SELECT payload_json FROM decision_bundles WHERE decision_id=? ORDER BY seq DESC LIMIT 1',(order,)).fetchone()[0])
    frozen=json.loads(store.execute('SELECT payload_json FROM receipts WHERE row_hash=?',(bundle['receipt_row_hash'],)).fetchone()[0])
    assert got[0]['entry_sheet']['safety']==frozen['sheet'].get('safety');store.close()
    assert world.gpt.postmortems(actor_id='jg',seen={order})==[] and world.gpt.postmortems(actor_id='j1',seen=set())==[]


def test_an_order_without_a_plan_is_refused_before_the_book(world):
    error,out=world.judge_gpt('paper_order',ticket_id='t1',reason='why')
    assert error and out['reason']=='plan_required' and world.rows('gpt.db')==[]


def _desk(world,**checks):
    import sqlite3
    path=world.path/'d.db';c=sqlite3.connect(path)
    c.execute('CREATE TABLE IF NOT EXISTS desk_tickets(id INTEGER PRIMARY KEY,ticket_id TEXT,stage TEXT,inputs_json TEXT)')
    for ticket,value in checks.items():c.execute("INSERT INTO desk_tickets(ticket_id,stage,inputs_json) VALUES(?,'hard_filter',?)",(ticket,json.dumps(dict(evidence=dict(checks=value)))))
    c.commit();c.close()


def test_a_seat_filter_hides_logs_and_never_hides_on_missing_data(world):
    _desk(world,hi={'holders':{'top10_pct':80}},lo={'holders':{'top10_pct':10}},none={'holders':{'top10_pct':None}})
    world.gpt.feed=lambda:dict(state='known',items=[dict(ticket_id=t) for t in ('hi','lo','none','absent')])
    assert world.judge_gpt('feed_filter_set',rules=[dict(path='nope',op='what',value=1)],reason='r')[1]['reason']=='rules_invalid'
    assert world.gpt.visible_tickets(actor_id='jg',ticket_ids=['hi'])==['hi']      # no filter yet
    assert not world.judge_gpt('feed_filter_set',rules=[dict(path='holders.top10_pct',op='gt',value=50)],reason='r')[0]
    _,out=world.judge_gpt('feed_list')
    assert [i['ticket_id'] for i in out['items']]==['lo','none','absent'] and out['hidden_count']==1 and out['items'][0]['facts']['top10_pct']==10
    _,out=world.judge_gpt('feed_list',include_hidden=True)
    assert [i['ticket_id'] for i in out['items'] if i.get('hidden_by')]==['hi']
    logged=sqlite3.connect(world.path/'o.db').execute('SELECT actor_id,ticket_id FROM office_v2_feed_hidden').fetchall()
    assert logged==[('jg','hi')]
    assert world.gpt.visible_tickets(actor_id='jg',ticket_ids=['hi'])==[] and world.gpt.visible_tickets(actor_id='j1',ticket_ids=['hi'])==['hi']   # the filter is the seat's own


def test_largest_wallet_is_net_sol_against_the_pool_and_no_rows_is_unknown(tmp_path):
    (tmp_path/'pumpswap').mkdir();c=sqlite3.connect(tmp_path/'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool,actor,is_buy,quote_atomic,post_quote_reserve_atomic,received_at)');c.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    c.executemany('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?)',[('P','a',1,'60000000000','100000000000',90.),('P','a',0,'10000000000','90000000000',91.),('P','b',1,'5000000000','100000000000',92.),('P','z',1,'99000000000','1',1.)]);c.commit();c.close()
    got=V.largest_wallet(tmp_path,'P',100.,window_s=50)
    assert got['actor']=='a' and got['net_sol']==50. and got['pct_of_pool_sol']==50. and got['state']=='known'
    assert V.largest_wallet(tmp_path,'other',100.)['state']=='unknown' and V.largest_wallet(tmp_path/'none','P',100.)['reason']=='tape_unreadable'


def test_filter_report_counts_hidden_listed_and_not_listed_against_the_full_feed(world):
    from sniper.office import feed_filter_report as R
    c=sqlite3.connect(world.path/'d2.db');c.execute('CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY,ticket_id TEXT,stage TEXT,verdict TEXT,ts TEXT,inputs_json TEXT)')
    for t in ('hi','lo','never'):c.execute("INSERT INTO desk_tickets(ticket_id,stage,verdict,ts,inputs_json) VALUES(?,'hard_filter','PASS','2026',?)",(t,json.dumps(dict(evidence=dict(mint='m'+t,checks={'holders':{'top10_pct':80 if t=='hi' else 10}})))))
    c.commit();c.close();world.gpt.desk_path=str(world.path/'d2.db')
    o=sqlite3.connect(world.path/'out.db');o.execute('CREATE TABLE outcome_horizons(mint,horizon_s,status,collapsed,ret)');o.execute("INSERT INTO outcome_horizons VALUES('mhi',300,'ok',1,-.9)");o.commit();o.close()
    world.gpt.release_sha=None
    world.judge_gpt('feed_filter_set',rules=[dict(path='holders.top10_pct',op='gt',value=50)],reason='r',after_case='o1')
    world.gpt.feed=lambda:dict(state='known',items=[dict(ticket_id='hi'),dict(ticket_id='lo')])
    world.judge_gpt('feed_list')
    seat=R.report(store_path=world.path/'o.db',desk_path=world.path/'d2.db',outcomes_path=world.path/'out.db',epoch_id='E1',since_iso='2000')['seats']['jg']
    assert (seat['pass_tickets'],seat['hidden'],seat['listed_visible'],seat['not_listed'])==(3,1,1,1)
    assert seat['filters']==dict(changes=1,after_a_case=1) and seat['hidden_outcomes']==dict(horizon_s=300,known=1,unknown=0,collapsed=1)


def test_filter_refuses_nan_future_tape_and_probes_are_not_listings(world,tmp_path):
    assert not O.valid_rules([dict(path='a',op='ne',value=float('nan'))])
    assert not O.rule_hides(dict(path='a',op='ne',value=5),{'a':float('nan')})
    (tmp_path/'pumpswap').mkdir();c=sqlite3.connect(tmp_path/'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool,actor,is_buy,quote_atomic,post_quote_reserve_atomic,received_at)');c.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    c.executemany('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?)',[('P','past',1,'1000000000','10000000000',99.),('P','future',1,'99000000000','100000000000',101.)]);c.commit();c.close()
    assert V.largest_wallet(tmp_path,'P',100.)['actor']=='past'
    _desk(world,t={'holders':{'top10_pct':10}})
    world.gpt.visible_tickets(actor_id='jg',ticket_ids=['t'])
    assert sqlite3.connect(world.path/'o.db').execute('SELECT count(*) FROM office_v2_feed_seen').fetchone()[0]==0


def test_order_audit_is_own_orders_and_results_cover_both_books(world):
    _,out=world.judge_gpt('paper_order',ticket_id='t1',reason='why',thesis='t',disproof='d',exit_plan='e')
    order=out['order_id']
    error,got=world.judge_gpt('order_audit',order_id=order)
    assert not error and got['entry_ordinal_in_book']==1 and got['policy']['sl_pct']>0 and got['entry_reason']=='why' and got['plan']['thesis']=='t'
    c=L.open_ledger(world.path/'gpt.db');c.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at) SELECT '0',book_id,epoch_id,book_kind,network_id,'x',pool,'d','{}','h','v','censored',requested_at FROM orders WHERE order_id=?",(order,));c.commit();c.close()
    assert world.judge_gpt('order_audit',order_id=order)[1]['entry_ordinal_in_book']==1   # a same-time later order does not move it
    assert got['bundle_hash'] and got['sheet_hash'] and got['receipt_row_hash']!='unavailable'
    assert world.judge('order_audit',order_id=order)[1]['reason']=='order_not_in_this_book' and world.judge_gpt('order_audit',order_id='nope')[1]['reason']=='order_not_in_this_book'
    for db in ('gpt.db','main.db'):
        c=L.open_ledger(world.path/db)
        c.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,reason,requested_at) SELECT ?,book_id,epoch_id,kind,'solana',?,'P','d','{}','h','v','censored','r',1. FROM books WHERE kind='evaluated' LIMIT 1",(db,db));c.commit();c.close()
    world.gpt.results_ledgers=[str(world.path/'main.db'),str(world.path/'gpt.db')]
    items=world.gpt.results_view(actor_id='jg',incarnation=1)['items']
    assert {i['order_id'] for i in items}=={'0','gpt.db','main.db'} and {i['epoch_id'] for i in items}>={X.GPT_EPOCH}
