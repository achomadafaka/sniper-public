"""Typed DM/task reader contract; all databases and messages are local fixtures."""
import json
import sqlite3
from sniper.office import store
from sniper.office.report import read_state


def office(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for aid,generation in [('a',2),('b',3)]:
        conn.execute('INSERT INTO agents VALUES(?,?)',(aid,json.dumps(dict(name=aid,generation=generation,role='Scout'))))
    conn.commit()
    return conn


def dm(conn,seq=1,**kw):
    row=dict(message_id='m'+str(seq),from_actor_id='a',from_generation=2,to_actor_id='b',to_generation=3,created_at=100.,text='Check WALT liquidity',refs_json='[]',receipt_id='receipt'+str(seq));row.update(kw)
    conn.execute('INSERT INTO office_dm_events(seq,'+','.join(row)+') VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,*row.values()))


def task(conn,seq,status,**kw):
    row=dict(event_id='e'+str(seq),task_id='task',actor_id='a',generation=2,status=status,title='Review WALT',note='',created_at=100.+seq,receipt_id='receipt'+str(seq));row.update(kw)
    conn.execute('INSERT INTO office_task_events(seq,'+','.join(row)+') VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,*row.values()))
    conn.execute('INSERT INTO office_task_heads(task_id,actor_id,generation,status,event_id,title,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET actor_id=excluded.actor_id,generation=excluded.generation,status=excluded.status,event_id=excluded.event_id,title=excluded.title,updated_at=excluded.updated_at',tuple(row[k] for k in ('task_id','actor_id','generation','status','event_id','title','created_at')))


def test_dm_thread_has_exact_generation_and_real_graph_reference(tmp_path):
    conn=office(tmp_path);dm(conn);conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    msg=s['direct_messages']['items'][0]
    assert msg['from_actor_id']=='a' and msg['to_actor_id']=='b'
    assert msg['from_generation']==2 and msg['to_generation']==3 and msg['message_id']=='m1'
    assert msg['text']=='Check WALT liquidity'
    assert any(e['source']=='a' and e['target']=='b' and 'm1' in e['reference'] for e in s['handoffs']['items'])


def test_task_board_uses_latest_causal_update_and_preserves_block_reason(tmp_path):
    conn=office(tmp_path);task(conn,1,'open');task(conn,2,'blocked',note='Waiting for pool proof');task(conn,3,'done',task_id='future-task',created_at=300.);conn.commit();conn.close()
    row=read_state(tmp_path,now=200.,host_now=200.)['seat_tasks']['items'][0]
    assert row['status']=='blocked' and row['note']=='Waiting for pool proof'
    assert row['event_id']=='e2'


def test_stale_generation_and_future_dm_never_inherit_current_seat(tmp_path):
    conn=office(tmp_path);dm(conn,1,from_generation=1,text='OLD_GENERATION');dm(conn,2,created_at=300.,text='FUTURE');conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['direct_messages']['items']==[]
    assert not any(e.get('kind')=='Written DM' for e in s['handoffs']['items'])
    assert 'OLD_GENERATION' not in json.dumps(s) and 'FUTURE' not in json.dumps(s)


def test_missing_typed_tables_are_unknown_and_get_does_not_create_them(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for table in ('office_dm_events','office_task_events','office_task_heads'):conn.execute('DROP TABLE '+table)
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['direct_messages']['state']=='unknown' and s['seat_tasks']['state']=='unknown'
    with sqlite3.connect((tmp_path/'office.db').as_uri()+'?mode=ro',uri=True) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name IN ('office_dm_events','office_task_events')").fetchall()


def test_typed_event_tails_are_bounded_and_states_not_claimed_complete(tmp_path):
    conn=office(tmp_path)
    for i in range(1,71):dm(conn,i)
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert len(s['direct_messages']['items'])==64 and s['direct_messages']['limited'] is True
    assert s['read_budget']['queries']<=512


def test_invalid_latest_task_event_does_not_resurrect_old_open_state(tmp_path):
    conn=office(tmp_path);task(conn,1,'open');task(conn,2,'invalid');conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['seat_tasks']['items']==[] and s['seat_tasks']['state']=='partial'


def test_unknown_clock_and_oversized_utf8_dm_stay_unavailable(tmp_path):
    conn=office(tmp_path);dm(conn,text='😀'*1025);conn.commit();conn.close()
    assert read_state(tmp_path,now=200.,host_now=200.)['direct_messages']['items']==[]
    s=read_state(tmp_path,now=None,host_now=200.)
    assert s['direct_messages']['state']=='unknown' and not s['direct_messages']['items']


def test_current_head_projection_keeps_old_open_task_when_other_task_is_busy(tmp_path):
    conn=office(tmp_path);task(conn,1,'open',task_id='old')
    for i in range(2,101):task(conn,i,'blocked',task_id='busy')
    conn.commit();conn.close()
    s=read_state(tmp_path,now=250.,host_now=250.)
    assert {r['task_id'] for r in s['seat_tasks']['items']}=={'old','busy'}
    assert s['seat_tasks']['projection']=='task_heads'


def test_foreign_or_future_task_head_cannot_publish_older_state(tmp_path):
    conn=office(tmp_path);task(conn,1,'open')
    conn.execute("UPDATE office_task_heads SET actor_id='b',generation=3 WHERE task_id='task'")
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['seat_tasks']['items']==[] and s['seat_tasks']['state']=='partial'


def test_done_head_survives_pruned_event_without_inventing_note_or_receipt(tmp_path):
    conn=office(tmp_path)
    conn.execute("INSERT INTO office_task_heads(task_id,actor_id,generation,status,event_id,title,updated_at) VALUES('old','a',2,'done','pruned','Archived work',101)")
    conn.commit();conn.close()
    row=read_state(tmp_path,now=200.,host_now=200.)['seat_tasks']['items'][0]
    assert row['status']=='done' and row['title']=='Archived work'
    assert row['note'] is None and row['receipt_id'] is None and row['history_state']=='unavailable'


def test_empty_projection_with_existing_events_is_unknown_not_empty_board(tmp_path):
    conn=office(tmp_path);task(conn,1,'open')
    conn.execute('DELETE FROM office_task_heads')
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['seat_tasks']['state']=='unknown' and not s['seat_tasks']['items']


def test_empty_real_store_tables_are_known_empty_not_missing(tmp_path):
    conn=store.open_store(tmp_path/'office.db');conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['direct_messages']['state']=='known' and s['direct_messages']['items']==[]
    assert s['seat_tasks']['state']=='known' and s['seat_tasks']['items']==[]
    assert s['seat_tasks']['projection']=='task_heads'


def test_legacy_missing_projection_uses_labelled_causal_event_tail(tmp_path):
    conn=office(tmp_path);task(conn,1,'open');task(conn,2,'blocked',note='Waiting for pool proof');task(conn,3,'done',created_at=300.)
    conn.execute('DROP TABLE office_task_heads');conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['seat_tasks']['projection']=='event_tail'
    assert s['seat_tasks']['items'][0]['status']=='blocked'


def test_future_real_head_never_resurrects_older_task_state(tmp_path):
    conn=office(tmp_path);task(conn,1,'open');task(conn,2,'done',created_at=300.)
    conn.commit();conn.close()
    s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['seat_tasks']['items']==[] and s['seat_tasks']['state']=='partial'
