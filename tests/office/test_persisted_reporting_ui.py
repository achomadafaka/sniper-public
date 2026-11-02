import json
import sqlite3
from sniper.office.reporting_view import read_daily, read_equity_history


def database():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE agent_daily(utc_day TEXT,actor_id TEXT,generation INTEGER,turns_started INTEGER,turns_completed INTEGER,turns_partial INTEGER,calls INTEGER,calls_not_ok INTEGER,measured_cost_usd REAL,cost_unknown_n INTEGER,handoffs_out INTEGER,handoffs_in INTEGER,late_after_close INTEGER,decisions INTEGER,linked_closed_n INTEGER,wins_n INTEGER,realized_paper_pnl_usd REAL,pnl_unknown_n INTEGER,open_n INTEGER,overlap_n INTEGER,source_heads TEXT,covered_through REAL,missing_n INTEGER,written_at REAL,PRIMARY KEY(utc_day,actor_id,generation)) WITHOUT ROWID')
    c.execute('CREATE TABLE equity_snapshots(book_id TEXT,at REAL,bucket_key REAL,cash_usd REAL,known_liq_usd REAL,equity_usd REAL,priced_n INTEGER,unpriced_n INTEGER,fx_usd REAL,fx_at REAL,source_heads TEXT,state TEXT,fill_model_version TEXT,written_at REAL,PRIMARY KEY(book_id,at)) WITHOUT ROWID')
    c.execute('CREATE INDEX equity_snapshots_at_book ON equity_snapshots(at,book_id)')
    return c


def daily(c,**kw):
    d=dict(utc_day='1970-01-01',actor_id='scout',generation=-1,turns_started=None,turns_completed=None,turns_partial=None,calls=2,calls_not_ok=0,measured_cost_usd=.1,cost_unknown_n=1,handoffs_out=1,handoffs_in=0,late_after_close=0,decisions=2,linked_closed_n=2,wins_n=1,realized_paper_pnl_usd=5.,pnl_unknown_n=0,open_n=0,overlap_n=0,source_heads='{"model_calls_seq":8}',covered_through=100.,missing_n=0,written_at=101.);d.update(kw)
    c.execute('INSERT INTO agent_daily('+','.join(d)+') VALUES('+','.join('?' for _ in d)+')',tuple(d.values()))


def snap(c,book='one',at=100.,**kw):
    d=dict(book_id=book,at=at,bucket_key=0.,cash_usd=25.,known_liq_usd=5.,equity_usd=30.,priced_n=1,unpriced_n=0,fx_usd=100.,fx_at=at-1,source_heads='{"marks_max_id":2}',state='known',fill_model_version='paper',written_at=at);d.update(kw)
    c.execute('INSERT INTO equity_snapshots VALUES('+','.join('?' for _ in d)+')',tuple(d.values()))


def test_daily_preserves_unattributed_generation_and_partial_cost():
    c=database();daily(c)
    out=read_daily(c,now=200.,public_scope=True);r=out['items'][0]
    assert r['generation']==-1 and r['measured_cost_usd']==.1 and r['cost_unknown_n']==1
    assert r['source_heads']=={'model_calls_seq':8} and r['covered_through']==100.
    assert out['state']=='partial'


def test_missing_sealed_future_and_bad_rows_are_unknown_not_zero():
    c=database();daily(c,written_at=300.)
    assert read_daily(c,now=200.,public_scope=True)['items']==[]
    assert read_daily(c,now=200.,public_scope=False)['reason']=='release_scope_unknown'
    assert read_daily(sqlite3.connect(':memory:'),now=200.,public_scope=True)['state']=='unknown'


def test_daily_tail_is_bounded_without_global_aggregate():
    c=database()
    for i in range(205):daily(c,actor_id=str(i))
    seen=[];c.set_trace_callback(seen.append)
    out=read_daily(c,now=200.,public_scope=True)
    assert len(out['items'])==200 and out['limited']
    queries=[q for q in seen if ' FROM agent_daily ' in q]
    assert len(queries)==1 and 'LIMIT 201' in queries[0] and 'COUNT(' not in queries[0]
    plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN '+queries[0]))
    assert 'SEARCH agent_daily USING PRIMARY KEY' in plan and 'TEMP B-TREE' not in plan


def test_history_keeps_null_gaps_separate_books_and_causal_cut():
    c=database();snap(c);snap(c,at=120.,equity_usd=None,state='partial',unpriced_n=1);snap(c,book='two',equity_usd=900.);snap(c,at=300.)
    books=[dict(book_id=x,kind='discovery',epoch_id='d') for x in ['one','two']]
    out=read_equity_history(c,books,now=200.)
    assert len(out['items'])==3
    one=[r for r in out['items'] if r['book_id']=='one']
    assert [r['equity_usd'] for r in one]==[30.,None] and one[1]['unpriced_n']==1
    assert all(r['at']<=200. for r in out['items'])


def test_unknown_book_scope_and_nonfinite_equity_never_price_a_point():
    c=database();snap(c,equity_usd=float('inf'))
    assert read_equity_history(c,[dict(book_id='one',kind='evaluated')],now=200.)['items']==[]
    out=read_equity_history(c,[dict(book_id='one',kind='discovery')],now=200.)
    assert out['items'][0]['equity_usd'] is None
    json.dumps(out,allow_nan=False)


def test_report_exposes_missing_producers_as_unknown_without_writing(tmp_path):
    from sniper.office.report import read_state
    state=read_state(tmp_path,now=200.,host_now=200.)
    assert state['persisted_daily']['state']=='unknown'
    assert state['equity_history']['state']=='unknown'
    assert not list(tmp_path.iterdir())


def test_history_query_uses_time_index_and_explicit_limit():
    c=database();snap(c);seen=[];c.set_trace_callback(seen.append)
    read_equity_history(c,[dict(book_id='one',kind='discovery')],now=200.)
    assert len(seen)==1 and 'INDEXED BY equity_snapshots_at_book' in seen[0] and 'LIMIT 201' in seen[0]
    query=seen[0]
    plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN '+query))
    assert 'SEARCH equity_snapshots USING INDEX equity_snapshots_at_book' in plan
    assert 'TEMP B-TREE' not in plan


def test_snapshot_with_stale_or_future_fx_cannot_claim_priced_inventory():
    c=database();snap(c,fx_at=200.)
    out=read_equity_history(c,[dict(book_id='one',kind='discovery')],now=200.)
    assert out['items'][0]['equity_usd'] is None


def test_summary_coverage_cannot_follow_its_write_time():
    c=database();daily(c,covered_through=150.,written_at=120.)
    assert read_daily(c,now=200.,public_scope=True)['items']==[]


def test_epoch_keyed_daily_filters_mixed_epochs_without_hiding_discovery():
    c=database();daily(c,actor_id='public');daily(c,actor_id='private')
    c.execute('ALTER TABLE agent_daily ADD COLUMN epoch_id TEXT')
    c.execute("UPDATE agent_daily SET epoch_id=CASE actor_id WHEN 'public' THEN 'discovery-2' ELSE 'blind' END")
    out=read_daily(c,now=200.,public_scope=False,public_epochs={'discovery-2'})
    assert [r['actor_id'] for r in out['items']]==['public']
    assert out['items'][0]['epoch_id']=='discovery-2'
    assert out['omitted']==1


def test_null_epoch_usage_stays_separate_and_is_suppressed_for_mixed_scope():
    c=database();daily(c)
    c.execute('ALTER TABLE agent_daily ADD COLUMN epoch_id TEXT')
    one=read_daily(c,now=200.,public_scope=True,public_epochs={'d'})['items'][0]
    assert one['epoch_id'] is None and one['epoch_scope']=='not epoch-attributed' and one['calls']==2
    assert read_daily(c,now=200.,public_scope=False,public_epochs={'d'})['items']==[]


def test_epoch_rows_cannot_claim_unattributed_calls_and_release_identity_is_checked():
    c=database();daily(c,source_heads=json.dumps({'release_sha':'a'*40}))
    c.execute('ALTER TABLE agent_daily ADD COLUMN epoch_id TEXT');c.execute("UPDATE agent_daily SET epoch_id='d'")
    row=read_daily(c,now=200.,public_scope=False,public_epochs={'d'})['items'][0]
    assert row['calls'] is None and row['measured_cost_usd'] is None and row['decisions']==2
    c.execute("UPDATE agent_daily SET source_heads=?",(json.dumps({'release_sha':'invalid'}),))
    assert read_daily(c,now=200.,public_scope=False,public_epochs={'d'})['items']==[]


def test_final_epoch_keyed_ddl_tail_uses_unique_day_index():
    c=database()
    sql=c.execute("SELECT sql FROM sqlite_master WHERE name='agent_daily'").fetchone()[0]
    c.execute('DROP TABLE agent_daily')
    c.execute(sql.replace('PRIMARY KEY(utc_day,actor_id,generation)','epoch_id TEXT').replace(' WITHOUT ROWID',''))
    c.execute("CREATE UNIQUE INDEX agent_daily_key ON agent_daily(utc_day,actor_id,generation,COALESCE(epoch_id,''))")
    c.execute('CREATE INDEX agent_daily_actor_day ON agent_daily(actor_id,utc_day)')
    daily(c)
    seen=[];c.set_trace_callback(seen.append)
    assert read_daily(c,now=200.,public_scope=True)['items'][0]['epoch_id'] is None
    query=next(q for q in seen if ' FROM agent_daily ' in q)
    plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN '+query))
    assert 'SEARCH agent_daily USING INDEX agent_daily_key' in plan and 'TEMP B-TREE' not in plan


def test_missing_daily_index_refuses_before_sorting_unbounded_rows():
    c=database();sql=c.execute("SELECT sql FROM sqlite_master WHERE name='agent_daily'").fetchone()[0]
    c.execute('DROP TABLE agent_daily');c.execute(sql.replace(',PRIMARY KEY(utc_day,actor_id,generation)','').replace(' WITHOUT ROWID',''))
    daily(c)
    out=read_daily(c,now=200.,public_scope=True)
    assert out['state']=='unknown' and out['items']==[]


def test_report_wires_persisted_sources_in_same_read_budget_and_host_clock(tmp_path):
    from sniper.office import store,ledger
    from sniper.office.report import read_state
    schema=database()
    c=store.open_store(tmp_path/'office.db')
    c.execute('INSERT INTO epochs VALUES(?,?)',('d',json.dumps({'epoch_id':'d','kind':'discovery','opened_at':1.})))
    c.execute(schema.execute("SELECT sql FROM sqlite_master WHERE name='agent_daily'").fetchone()[0]);daily(c)
    c.commit();c.close()
    c=ledger.open_ledger(tmp_path/'ledger'/'ledger.db')
    ledger.open_epoch(c,epoch_id='d',clip_usd=25.,rule_version='fixture',actor='fixture',note='local',now=1.)
    ledger.open_book(c,book_id='one',epoch_id='d',kind='discovery',actor='fixture',note='local',now=1.)
    c.execute(schema.execute("SELECT sql FROM sqlite_master WHERE name='equity_snapshots'").fetchone()[0])
    c.execute('CREATE INDEX equity_snapshots_at_book ON equity_snapshots(at,book_id)');snap(c);c.commit();c.close()
    out=read_state(tmp_path,now=90.,host_now=200.)
    assert out['persisted_daily']['items'][0]['actor_id']=='scout'
    assert out['equity_history']['items'][0]['at']==100.
    assert out['read_budget']['queries']<512 and not out['read_budget']['exhausted']
