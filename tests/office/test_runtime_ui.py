import json
from sniper.office import store
from sniper.office.report import read_state


def fixture(tmp_path):
    c=store.open_store(tmp_path/'office.db')
    c.execute('INSERT INTO agents VALUES(?,?)',('a',json.dumps(dict(name='A',generation=2))))
    c.execute('INSERT INTO agents VALUES(?,?)',('b',json.dumps(dict(name='B',generation=3))))
    c.execute('INSERT INTO epochs VALUES(?,?)',('public',json.dumps(dict(epoch_id='public',kind='discovery',opened_at=1))))
    return c


def call(c,key='c1',**kw):
    row=dict(call_id=key,actor_id='a',generation=2,turn_id='t1',requested_provider='codex',requested_model='requested',started_at=90.,ended_at=100.,outcome='success',usage_state='unknown',cost_basis='unknown',epoch_id='public');row.update(kw)
    c.execute('INSERT INTO model_calls('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))


def edge(c,key='e1',**kw):
    row=dict(event_id=key,at=101.,from_actor_id='a',to_actor_id='b',from_record_kind='memo',from_record_id='receipt-real',to_record_kind='decision',to_record_id='decision-real',kind='decision.bundle',public_summary='Actual handoff',epoch_id='public');row.update(kw)
    c.execute('INSERT INTO handoff_events('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))


def test_typed_call_keeps_unknown_identity_zero_usage_and_real_handoff(tmp_path):
    c=fixture(tmp_path);call(c,input_tokens=0,served_model='pin',served_model_basis='requested_pinned');edge(c);c.commit();c.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    r=s['runtime_calls']['items'][0]
    assert r['input_tokens']==0 and r['cost_usd'] is None and r['served_provider'] is None
    assert r['served_model']=='pin' and r['served_model_basis']=='requested_pinned' and r['epoch_id']=='public'
    e=s['runtime_handoffs']['items'][0]
    assert e['from_record_id']=='receipt-real' and e['to_record_id']=='decision-real'
    assert any(x['id']=='typed:e1' and 'receipt-real' in x['reference'] and 'decision-real' in x['reference'] for x in s['handoffs']['items'])


def test_private_unknown_epoch_future_and_sealed_seat_are_withheld(tmp_path):
    c=fixture(tmp_path)
    c.execute('INSERT INTO epochs VALUES(?,?)',('private',json.dumps(dict(epoch_id='private',kind='evaluated',opened_at=1))))
    call(c,'private',epoch_id='private',served_model='SECRET');call(c,'missing',epoch_id='missing');call(c,'legacy',epoch_id=None)
    call(c,'future',ended_at=300.);edge(c,epoch_id='private',public_summary='SECRET')
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['runtime_calls']['items']==[] and s['runtime_handoffs']['items']==[]
    assert 'SECRET' not in json.dumps(s)


def test_known_empty_missing_clock_and_missing_tables(tmp_path):
    c=fixture(tmp_path);c.close()
    assert read_state(tmp_path,now=200.,host_now=200.)['runtime_calls']['state']=='known'
    assert read_state(tmp_path,now=None,host_now=200.)['runtime_calls']['state']=='unknown'
    c=store.open_store(tmp_path/'office.db');c.execute('DROP TABLE model_calls');c.commit();c.close()
    assert read_state(tmp_path,now=200.,host_now=200.)['runtime_calls']['state']=='unknown'


def test_typed_tail_is_bounded_and_retention_coverage_carried(tmp_path):
    c=fixture(tmp_path)
    for i in range(70):call(c,str(i))
    c.execute("UPDATE runtime_observation_state SET retained_from=80,pruned_n=3,dropped_n=2 WHERE table_name='model_calls'")
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    r=s['runtime_calls'];assert len(r['items'])==64 and r['limited']
    assert r['retained_from']==80 and r['pruned_n']==3 and r['dropped_n']==2
    assert s['read_budget']['queries']<=512




def test_dm_and_its_typed_handoff_draw_one_exact_edge(tmp_path):
    c=fixture(tmp_path)
    c.execute("INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id) VALUES('m1','a',2,'b',3,100,'Message','[]','receipt1')")
    edge(c,from_record_kind='receipts',from_record_id='receipt1',to_record_kind='office_dm_events',to_record_id='m1',kind='msg.send')
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    assert len(s['handoffs']['items'])==1
    assert 'receipt1' in s['handoffs']['items'][0]['reference']
