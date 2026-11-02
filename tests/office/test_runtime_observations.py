"""Offline producer contracts: no provider, production ledger, clock file, or service."""
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.office import broker as B, capabilities as C, models as M, store as S
from sniper.office import ledger as L
_spec = importlib.util.spec_from_file_location('observation_step_fixtures', Path(__file__).with_name('test_runtime_steps.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
Client, context, runner = _fixtures.Client, _fixtures.context, _fixtures.runner


@pytest.fixture
def db(tmp_path):
    conn = S.open_store(tmp_path / 'office.db')
    try:
        yield conn
    finally:
        conn.close()


def observations():
    return importlib.import_module('sniper.office.runtime_observations')


def envelope(**changes):
    return replace(M.Envelope(requested_provider='claude', requested_model='claude-opus-5-5',
        served_model='claude-opus-5-5', outcome='success', started_at=1000., ended_at=1002.,
        usage={'input_tokens':12, 'output_tokens':0, 'cache_read_input_tokens':0},
        billing_basis='subscription'), **changes)


def receipt(conn, *, kind='memo.commit', actor='alice', at=900., refs=None, **extra):
    return S.append(conn, 'receipts', dict(kind=kind, actor_id=actor, known_at=at,
        outcome='success', refs=[] if refs is None else refs, **extra))


def test_additive_typed_tables_indexes_and_update_fence(db):
    required = {
        'model_calls':{'seq','call_id','actor_id','generation','turn_id','decision_id','requested_provider',
            'requested_model','served_provider','served_model','started_at','ended_at','latency_s','outcome',
            'reason','input_tokens','output_tokens','cached_tokens','cost_usd','usage_state','cost_basis'},
        'handoff_events':{'seq','event_id','at','from_actor_id','to_actor_id','from_record_kind',
            'from_record_id','to_record_kind','to_record_id','decision_id','order_id','kind','public_summary'},
    }
    for table, fields in required.items():
        assert fields <= {r[1] for r in db.execute('PRAGMA table_info('+table+')')}
    indexes = {r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    assert any('(actor_id, ended_at, call_id)' in sql for sql in indexes.values())
    assert any('(ended_at, call_id)' in sql for sql in indexes.values())
    assert any('(at, event_id)' in sql for sql in indexes.values())
    assert any('(from_actor_id, at)' in sql for sql in indexes.values())
    assert any('(to_actor_id, at)' in sql for sql in indexes.values())
    buffer = observations().Buffer(db, clock=lambda:1003.)
    buffer.capture_call(context(), envelope(), call_id='one')
    buffer.flush()
    with pytest.raises(Exception, match='insert_only'):
        db.execute("UPDATE model_calls SET outcome='invalid'")


def test_capture_is_memory_only_and_failure_usage_is_not_zero_filled(db):
    buffer = observations().Buffer(db, clock=lambda:1003.)
    calls = []
    db.set_trace_callback(calls.append)
    buffer.capture_call(context(), envelope(outcome='timeout', error_reason='timeout',
        usage={'output_tokens':0}, served_model=None), call_id='failed')
    assert calls == []
    buffer.flush()
    row = dict(db.execute('SELECT * FROM model_calls').fetchone())
    assert (row['actor_id'],row['turn_id'],row['generation']) == ('head','turn',1)
    assert row['outcome']=='timeout' and row['reason']=='timeout'
    assert row['input_tokens'] is None and row['output_tokens']==0 and row['cached_tokens'] is None
    assert row['served_provider'] is None and row['served_model'] is None
    assert row['cost_usd'] is None and row['usage_state']=='partial'
    assert not {'text','prompt','system','auth','usage'} & row.keys()


def test_idempotent_call_preserves_first_observation_and_rejects_identity_collision(db):
    buffer = observations().Buffer(db, clock=lambda:1003.)
    buffer.capture_call(context(), envelope(), call_id='stable')
    buffer.flush()
    buffer.capture_call(context(), envelope(outcome='partial',error_reason='existing_unknown'), call_id='stable')
    buffer.flush()
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0]==1
    assert db.execute('SELECT outcome FROM model_calls').fetchone()[0]=='success'
    buffer.capture_call(replace(context(),actor_id='other'), envelope(), call_id='stable')
    health = buffer.flush()
    assert health['last_error'] is not None
    assert db.execute('SELECT actor_id FROM model_calls').fetchone()[0]=='head'


def test_measured_latency_and_explicit_cost_survive_without_price_inference():
    result = M.call('claude','claude-opus-5-5','SECRET SYSTEM','SECRET PROMPT',deadline_s=10,
        clock=lambda:1000., schema_obj={'type':'object'}, transport=lambda *a,**kw:dict(
            text='{}',model_echo='claude-opus-5-5',cost_usd=0.,cost_basis='billed_receipt'))
    assert result.latency_s is not None and result.latency_s>=0
    assert result.cost_usd==0. and result.cost_basis=='billed_receipt'
    unknown = M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        schema_obj={'type':'object'},transport=lambda *a,**kw:dict(text='{}',model_echo='claude-opus-5-5',
            cost_usd=123,billing_basis='subscription_flat'))
    assert unknown.cost_usd is None


def test_agent_step_captures_failed_dispatched_call_before_return(db):
    buffer = observations().Buffer(db, clock=lambda:1003.)
    step = runner(Client({'call_error':'auth_revoked'}))
    step.observe_call = buffer.capture_call
    result = step(context(),{},10.)
    assert result.done and not result.actions and result.source_state=='off'
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0]==0
    buffer.flush()
    assert db.execute('SELECT outcome FROM model_calls').fetchone()[0]=='auth_revoked'


def test_no_provider_observation_for_pre_dispatch_refusal(db):
    buffer = observations().Buffer(db, clock=lambda:1003.)
    step = runner(Client())
    step.observe_call = buffer.capture_call
    step(replace(context(),generation=2),{},10.)
    buffer.flush()
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0]==0


def test_queue_overflow_is_visible_and_bounded(db):
    buffer = observations().Buffer(db, clock=lambda:1003., max_pending=1)
    buffer.capture_call(context(), envelope(), call_id='one')
    buffer.capture_call(context(), envelope(), call_id='two')
    assert buffer.health()['pending_n']==1 and buffer.health()['dropped_n']==1
    health = buffer.flush()
    assert health['dropped_n']==1
    assert db.execute('SELECT dropped_n FROM runtime_observation_state WHERE table_name=?',('model_calls',)).fetchone()[0]==1


def test_typed_reference_validation_and_real_memo_response_only(db):
    O = observations()
    first = receipt(db)
    refs = [{'kind':'memo','receipt_id':first}]
    assert O.validate_refs(db,refs,before=1000.)==refs
    target = receipt(db,actor='judge',at=1000.,refs=refs,role='judge',decision_id='d')
    buffer = O.Buffer(db, clock=lambda:1001.)
    buffer.flush()
    event = dict(db.execute('SELECT * FROM handoff_events').fetchone())
    assert event['from_actor_id']=='alice' and event['to_actor_id']=='judge'
    assert event['from_record_id']==first and event['to_record_id']==target
    assert event['kind']=='memo_to_judge' and event['at']==1000.
    assert 'SECRET' not in event['public_summary']
    buffer.flush()
    assert db.execute('SELECT count(*) FROM handoff_events').fetchone()[0]==1


@pytest.mark.parametrize('bad',['missing','wrong_kind','future','private','malformed'])
def test_reference_requires_known_public_kind_and_prior_receipt(db,bad):
    O = observations()
    key = receipt(db,at=1001. if bad=='future' else 900.,
                  evaluation_derived=bad=='private')
    ref = {'kind':'proposal' if bad=='wrong_kind' else 'memo',
           'receipt_id':'0'*64 if bad=='missing' else key}
    if bad=='malformed':ref['memo']='SECRET'
    with pytest.raises(ValueError):O.validate_refs(db,[ref],before=1000.)


def test_unreferenced_message_and_same_coin_never_create_edges(db):
    S.append(db,'receipts',dict(kind='memo.commit',actor_id='a',known_at=900.,mint='coin'))
    S.append(db,'receipts',dict(kind='memo.commit',actor_id='b',known_at=901.,mint='coin'))
    observations().Buffer(db,clock=lambda:1000.).flush()
    assert db.execute('SELECT count(*) FROM handoff_events').fetchone()[0]==0


def test_broker_refuses_unknown_typed_reference_before_persisting_it(db):
    cut={'basis':'corrected_utc','receipt_cut':1000.,'knowledge_cut':1000.,'slot_cut':None,
         'clock_sample':{'corrected_utc':1000.,'offset_s':0.}}
    ctx=C.Context('judge','judge','pilot-floor',1,'d',dict(cut,slot_cut=10))
    bad={'kind':'memo','receipt_id':'0'*64}
    tool=B.Tool(lambda args,c:B.ToolResult({'memo':'public'}, {},None,[bad],'observed'),{},'test')
    broker=B.Broker(db,None,{'decision.bundle':tool},generation_check=lambda _:True)
    result=broker.execute(B.ToolRequest('judge','decision.bundle',{},'d',ctx.cut),context=ctx)
    assert result.outcome=='refused' and result.source_refs==[]
    assert '0'*64 not in db.execute('SELECT payload_json FROM receipts').fetchone()[0]


def test_decision_receipt_creates_only_verified_order_edge(db,tmp_path):
    ledger=L.open_ledger(tmp_path/'ledger.db')
    try:
        # Reuse the real order schema, without running paper quotes or execution.
        ledger.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('order','book','epoch','discovery','solana','mint','pool','bundle','{}','policy','fixture','pending',900.))
        ledger.execute('INSERT INTO decision_receipts(action_key,decision_id,book_id,bundle_hash,outcome,reason,order_id,risk_version,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('action','decision','book','bundle','submitted',None,'order','risk','real-actor',900.))
        O=observations();buffer=O.Buffer(db,ledger=ledger,clock=lambda:1000.)
        buffer.flush()
        event=dict(db.execute('SELECT * FROM handoff_events').fetchone())
        assert event['from_record_kind']=='decision_receipts' and event['from_record_id']=='action'
        assert event['to_record_kind']=='orders' and event['order_id']=='order'
        assert event['from_actor_id']=='real-actor' and event['to_actor_id'] is None
        buffer.flush()
        assert db.execute('SELECT count(*) FROM handoff_events').fetchone()[0]==1
    finally:ledger.close()


def test_mark_consumed_joins_transaction_and_is_monotonic(db):
    O=observations();buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),envelope(),call_id='one');buffer.flush()
    db.execute('BEGIN')
    O.mark_consumed(db,'model_calls',1)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT consumed_seq FROM runtime_observation_state WHERE table_name='model_calls'").fetchone()[0]==0
    O.mark_consumed(db,'model_calls',1);db.commit()
    O.mark_consumed(db,'model_calls',0);db.commit()
    assert db.execute("SELECT consumed_seq FROM runtime_observation_state WHERE table_name='model_calls'").fetchone()[0]==1


def test_retention_waits_for_rollup_protects_open_epoch_and_is_bounded(db):
    O=observations();buffer=O.Buffer(db,clock=lambda:1003.)
    for i in range(5):
        buffer.capture_call(context(),envelope(started_at=float(i+1),ended_at=float(i+2)),call_id=str(i))
    buffer.flush()
    now=O.RETENTION_S+100.
    assert O.prune(db,now=now,limit=2)['model_calls']==0
    O.mark_consumed(db,'model_calls',5);db.commit()
    db.execute('INSERT INTO epochs VALUES(?,?)',('open',json.dumps(dict(opened_at=3.,closed_at=None))));db.commit()
    assert O.prune(db,now=now,limit=2)['model_calls']==1
    assert db.execute('SELECT min(ended_at) FROM model_calls').fetchone()[0]==3.
    db.execute('UPDATE epochs SET payload_json=?', (json.dumps(dict(opened_at=3.,closed_at=10.)),));db.commit()
    assert O.prune(db,now=now,limit=2)['model_calls']==2
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0]==2
    assert db.execute("SELECT retained_from FROM runtime_observation_state WHERE table_name='model_calls'").fetchone()[0]==5.
    buffer.capture_call(context(),envelope(),call_id='new');buffer.flush()
    assert db.execute("SELECT seq FROM model_calls WHERE call_id='new'").fetchone()[0]==6


def test_unknown_epoch_state_blocks_pruning_and_core_chains_are_untouched(db):
    O=observations();key=receipt(db)
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),envelope(),call_id='one');buffer.flush()
    O.mark_consumed(db,'model_calls',1);db.commit()
    db.execute('INSERT INTO epochs VALUES(?,?)',('bad','{}'));db.commit()
    assert O.prune(db,now=O.RETENTION_S+2000.,limit=200)['model_calls']==0
    assert db.execute('SELECT row_hash FROM receipts').fetchone()[0]==key
    assert S.verify_chain(db,'receipts')[0]


def test_overflow_count_survives_supervisor_restart(db):
    O=observations()
    first=O.Buffer(db,clock=lambda:1003.,max_pending=1)
    first.capture_call(context(),envelope(),call_id='one')
    first.capture_call(context(),envelope(),call_id='lost')
    first.flush()
    second=O.Buffer(db,clock=lambda:1004.)
    second.flush()
    assert db.execute("SELECT dropped_n FROM runtime_observation_state WHERE table_name='model_calls'").fetchone()[0]==1
    assert second.health()['state']=='partial'


def test_reporting_queries_use_init_indexes_without_reader_ddl(db,tmp_path):
    queries=[(db,"SELECT id FROM turn_runs WHERE json_extract(payload_json,'$.created_at')>=?",(0,), 'turn_runs_created_at')]
    ledger=L.open_ledger(tmp_path/'report-ledger.db')
    try:
        queries.extend([(ledger,'SELECT action_key FROM decision_receipts WHERE created_at>=?',(0,),'decision_receipts_created_at'),
                        (ledger,'SELECT action_key FROM decision_receipts WHERE order_id=?',('order',),'decision_receipts_order'),
                        (ledger,'SELECT order_id FROM orders WHERE closed_at>=?',(0,),'orders_closed_at')])
        assert any(r['name']=='orders_open' and r['partial']==1 for r in ledger.execute('PRAGMA index_list(orders)'))
        for conn,sql,params,index in queries:
            detail=' '.join(str(tuple(row)) for row in conn.execute('EXPLAIN QUERY PLAN '+sql,params))
            assert index in detail
    finally:ledger.close()


def test_served_model_unknown_remains_typed(db):
    buffer = observations().Buffer(db, clock=lambda: 100.)
    buffer.capture_call(context(), envelope(outcome='invalid', error_reason='served_model_unknown', served_model=None), call_id='unknown-model')
    assert buffer.pending[0]['reason'] == 'served_model_unknown'
    assert buffer.pending[0]['served_model'] is None


def test_requested_model_basis_is_recorded_without_backfilling_legacy(db):
    buffer=observations().Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),envelope(served_model_basis='requested_pinned'),call_id='pinned')
    buffer.flush()
    assert db.execute("SELECT served_model_basis FROM model_calls WHERE call_id='pinned'").fetchone()[0]=='requested_pinned'
    buffer.capture_call(context(),envelope(served_model_basis='Bearer private'),call_id='bad-basis')
    assert buffer.pending[0]['served_model_basis']=='unknown'


def test_claude_output_guard_reasons_are_typed(db):
    buffer=observations().Buffer(db,clock=lambda:1003.)
    for reason in ('tool_event','structured_output_missing'):
        buffer.capture_call(context(),envelope(outcome='invalid',error_reason=reason),call_id=reason)
    assert [r['reason'] for r in buffer.pending]==['tool_event','structured_output_missing']
