"""Real producer schema; no live store or service calls."""
import json
import sqlite3
import pytest
from sniper.office import store, seat_events


def fixture(tmp_path):
    conn=store.open_store(tmp_path/'office.db');seat_events.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('a','ep','Judge',2,3,0,100)")
    conn.commit();return conn


def event(conn,n,**kw):
    row=dict(event_id='e'+str(n),epoch_id='ep',actor_id='a',generation=2,incarnation=3,corrected_at=100.,kind='thought',state='known',text='<script>x</script>',truncated=0,refs_json='{}',web_tainted=0);row.update(kw)
    conn.execute('INSERT INTO office_v2_events('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))


def read(tmp_path,**kw):
    from sniper.office.seat_feed import read_feed
    return read_feed(tmp_path,now=200.,**kw)


def test_feed_latest_tail_and_incremental_pages_use_real_sequence(tmp_path):
    conn=fixture(tmp_path)
    for n in range(1,8):event(conn,n)
    conn.commit();conn.close()
    r=read(tmp_path,limit=2)
    assert [e['event_id'] for e in r['events']['items']]==['e7','e6']
    assert r['events']['limited'] and r['events']['cursor']=='7'
    r=read(tmp_path,after='2',limit=2,actor='a')
    assert [e['event_id'] for e in r['events']['items']]==['e3','e4']
    assert r['events']['cursor']=='4'
    assert r['mail']['cursor'] is None


def test_stale_future_unknown_clock_and_poisoned_refs_are_not_current_work(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,generation=1,text='STALE');event(conn,2,corrected_at=300.,text='FUTURE')
    event(conn,3,refs_json='[]');event(conn,4,corrected_at=None,state='unknown',text=None)
    event(conn,5,actor_id='unregistered',text='UNKNOWN_ACTOR')
    event(conn,6,kind='workspace',refs_json=json.dumps({'files':[{'path':'<img onerror=x>', 'size_bytes':0}], 'notebook_text':'<script>x</script>'}))
    conn.commit();conn.close()
    r=read(tmp_path)
    assert {e['event_id'] for e in r['events']['items']}=={'e4','e6'}
    assert r['events']['state']=='partial' and r['events']['omitted']==4
    assert 'STALE' not in json.dumps(r) and 'FUTURE' not in json.dumps(r)
    assert r['events']['items'][0]['refs']['files'][0]['size_bytes']==0
    assert r['events']['items'][1]['state']=='unknown'


def test_mail_has_own_cursor_and_operator_sender_is_host_identity(tmp_path):
    conn=fixture(tmp_path)
    conn.execute("""INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id) VALUES('m','operator',0,'a',2,100,'hello','{"epoch_id":"ep","from_incarnation":null,"to_incarnation":3}','r')""")
    conn.execute("INSERT INTO office_v2_mail_bind VALUES('m','ep',NULL,3)")
    conn.commit();conn.close()
    r=read(tmp_path)
    assert r['mail']['items'][0]['from_actor_id']=='operator'
    assert r['mail']['cursor']=='1' and r['events']['cursor'] is None
    assert read(tmp_path,mail_after='1')['mail']['items']==[]


def test_missing_source_does_not_create_database_or_claim_empty(tmp_path):
    r=read(tmp_path)
    assert r['state']=='unknown' and r['events']['state']=='unknown'
    assert not (tmp_path/'office.db').exists()


def test_empty_source_known_and_roster_cut_or_sealed_filters(tmp_path):
    conn=fixture(tmp_path);conn.execute("UPDATE office_v2_roster SET sealed=1");event(conn,1);conn.commit();conn.close()
    r=read(tmp_path)
    assert r['roster']['items']==[] and r['events']['items']==[]
    assert r['events']['state']=='partial'


def test_query_plan_uses_actor_sequence_index(tmp_path):
    conn=fixture(tmp_path)
    plan=conn.execute('EXPLAIN QUERY PLAN SELECT * FROM office_v2_events WHERE actor_id=? AND seq>? ORDER BY seq ASC LIMIT 3',('a',0)).fetchall()
    assert any('office_v2_events_actor' in str(tuple(r)) for r in plan)
    conn.close()


def test_unbound_or_old_epoch_mail_is_withheld(tmp_path):
    conn=fixture(tmp_path)
    for n,refs in enumerate(({},dict(epoch_id='old',from_incarnation=None,to_incarnation=3)),1):
        conn.execute('INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id) VALUES(?,?,?,?,?,?,?,?,?)',(str(n),'operator',0,'a',2,100.,'OLD_MAIL',json.dumps(refs),'r'+str(n)))
    conn.commit();conn.close()
    r=read(tmp_path)
    assert r['mail']['items']==[] and r['mail']['state']=='partial'
    assert r['mail']['omitted']==2


def test_workspace_manifest_does_not_require_thought_text(tmp_path):
    conn=fixture(tmp_path);event(conn,1,kind='workspace',text=None,refs_json='{"files":[],"notebook_text":"notes","state":"known","truncated":false}')
    conn.commit();conn.close()
    assert read(tmp_path)['events']['items'][0]['state']=='known'


def test_tasks_use_host_bound_workspace_manifest_instead_of_legacy_heads(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='workspace',text=None,refs_json=json.dumps(dict(files=[],tasks=[dict(task_id='t',status='blocked',title='Review',note='Wait for receipt')],state='known',truncated=False)))
    conn.commit();conn.close()
    r=read(tmp_path)
    assert r['tasks']['state']=='known' and r['tasks']['items'][0]['actor_id']=='a'
    assert r['tasks']['items'][0]['status']=='blocked'


def test_invalid_latest_workspace_never_resurrects_older_desk_or_tasks(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='workspace',refs_json=json.dumps(dict(files=[],notebook_text='OBSOLETE_NOTE',tasks=[dict(task_id='t',status='open',title='OBSOLETE_TASK')],state='known',truncated=False)))
    event(conn,2,kind='workspace',refs_json='BROKEN_JSON')
    conn.commit();conn.close()
    r=read(tmp_path)
    assert r['tasks']['state']=='unknown' and r['tasks']['items']==[]
    assert 'OBSOLETE' not in json.dumps(r)


@pytest.mark.parametrize('clock,state,expected',[(None,'known','unknown'),(100.,'unknown','unknown'),(100.,'partial','partial')])
def test_task_state_preserves_workspace_event_uncertainty(tmp_path,clock,state,expected):
    conn=fixture(tmp_path)
    event(conn,1,kind='workspace',corrected_at=clock,state=state,
          refs_json=json.dumps(dict(files=[],tasks=[dict(task_id='t',status='open',title='Review')],state='known',truncated=False)))
    conn.commit();conn.close()
    tasks=read(tmp_path)['tasks']
    assert tasks['state']==expected
    if expected=='unknown':assert tasks['items']==[]


def test_incomplete_roster_schema_returns_unknown(tmp_path):
    conn=fixture(tmp_path)
    conn.execute('ALTER TABLE office_v2_roster DROP COLUMN incarnation')
    conn.commit();conn.close()
    result=read(tmp_path)
    assert result['state']=='unknown' and result['roster']['items']==[]


def test_sealed_proof_actor_does_not_disable_active_seat_mail(tmp_path):
    conn=fixture(tmp_path)
    conn.execute("INSERT INTO office_v2_roster VALUES('proof','proof-epoch','Head',1,1,1,100)")
    conn.commit();conn.close()
    result=read(tmp_path)
    assert result['roster']['state']=='known'
    assert [a['actor_id'] for a in result['roster']['items']]==['a']


def test_tool_result_projection_keeps_bounded_error_metadata(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='tool_result',refs_json=json.dumps(dict(tool='sheet_read',is_error=True,other='PRIVATE')))
    event(conn,2,kind='tool_result',refs_json=json.dumps(dict(tool='book_view',is_error=False)))
    event(conn,3,kind='tool_result',refs_json=json.dumps(dict(tool=[],is_error='false')))
    conn.commit();conn.close()
    refs={r['event_id']:r['refs'] for r in read(tmp_path)['events']['items']}
    assert refs['e1']==dict(tool='sheet_read',is_error=True)
    assert refs['e2']==dict(tool='book_view',is_error=False)
    assert refs['e3']==dict(tool=None,is_error=None)


def test_floor_seats_use_host_state_and_real_daily_quota_with_unknown_preserved(tmp_path):
    from sniper.office import quota_measurement as Q
    conn=fixture(tmp_path)
    event(conn,1,kind='lifecycle',text='wake_end',corrected_at=100.)
    event(conn,2,kind='thought',text='A **known** thought',corrected_at=101.)
    conn.commit();conn.close()
    # Host-state path is role/epoch scoped; no role-derived arbitrary filesystem read.
    c=sqlite3.connect(tmp_path/'office.db');c.execute("UPDATE office_v2_roster SET role='judge',epoch_id='office-v2-explore-1'")
    c.execute("UPDATE office_v2_events SET epoch_id='office-v2-explore-1'");c.commit();c.close()
    root=tmp_path/'v2';root.mkdir();(root/'explore-judge.json').write_text(json.dumps(dict(incarnation=3,sleep_until=300.,parked_reason=None)))
    import os;os.utime(root/'explore-judge.json',(100.,100.))
    q=sqlite3.connect(root/'quota.db');Q.ensure_schema(q)
    q.execute('INSERT INTO quota_wakes VALUES(?,?,?,?,?,?,?,?,?)',('w','a','acc','s','r',100.,20,'known',json.dumps(dict(epoch_id='office-v2-explore-1'))))
    q.commit();q.close()
    seat=read(tmp_path)['seats']['items'][0]
    assert seat['activity']['kind']=='sleeping' and seat['activity']['until']==300.
    assert seat['last_thought']=='A **known** thought'
    assert seat['wakes_today']==1 and seat['tokens_today']==20
    q=sqlite3.connect(root/'quota.db');q.execute('INSERT INTO quota_wakes VALUES(?,?,?,?,?,?,?,?,?)',('w2','a','acc','s','r2',110.,None,'unknown',json.dumps(dict(epoch_id='office-v2-explore-1'))));q.commit();q.close()
    seat=read(tmp_path)['seats']['items'][0]
    assert seat['wakes_today']==2 and seat['tokens_today'] is None
    assert seat['tokens_known']==20 and seat['tokens_unknown_wakes']==1


def test_new_working_wake_never_claims_the_previous_wakes_tool_is_current(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='tool_call',text='sleep',corrected_at=90.)
    event(conn,2,kind='lifecycle',text='wake_start',corrected_at=100.)
    conn.commit();conn.close()
    seat=read(tmp_path)['seats']['items'][0]
    assert seat['activity']['kind']=='working' and seat['current_tool'] is None


def test_unknown_latest_lifecycle_does_not_resurrect_an_older_working_state(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='lifecycle',text='wake_start',corrected_at=100.)
    event(conn,2,kind='lifecycle',text='parked',state='unknown',corrected_at=None)
    conn.commit();conn.close()
    assert read(tmp_path)['seats']['items'][0]['activity']['kind']=='unknown'


def test_a_seat_reply_and_crew_mail_reach_the_feed_and_a_spoofed_seat_does_not(tmp_path):
    conn=fixture(tmp_path)
    seat_events.seat_to_operator(conn,from_actor='a',from_generation=2,from_incarnation=3,text='ack',now=150.)
    seat_events.seat_dm(conn,from_actor='crew',from_generation=0,from_incarnation=0,to_actor_id='a',text='note',now=151.)
    seat_events.seat_dm(conn,from_actor='nobody',from_generation=0,from_incarnation=0,to_actor_id='a',text='fake',now=152.)
    conn.commit();conn.close()
    assert sorted((m['from_actor_id'],m['to_actor_id'],m['text']) for m in read(tmp_path)['mail']['items'])==[('a','operator','ack'),('crew','a','note')]


def test_operator_questions_are_not_lost_under_a_busy_floor_tail(tmp_path):
    conn=fixture(tmp_path)
    event(conn,1,kind='request',text='Need a ruling',refs_json='{"kind":"policy","why":"missing reference"}')
    for n in range(2,72):event(conn,n,kind='thought',text='Recent activity')
    conn.commit();conn.close()
    result=read(tmp_path,limit=2)
    assert len(result['events']['items'])==2
    assert result['questions']['items'][0]['text']=='Need a ruling'
    assert result['questions']['items'][0]['actor_id']=='a'


def test_optional_metrics_cannot_expire_other_seats_or_requests(tmp_path,monkeypatch):
    import threading,time
    from sniper.office import report,floor_view
    conn=fixture(tmp_path)
    conn.execute("INSERT INTO office_v2_roster VALUES('b','ep','Scout',2,3,0,100)")
    event(conn,1,kind='request',text='Host help needed')
    for n in range(2,702):event(conn,n)
    conn.commit();conn.close()
    owner=threading.current_thread();real=time.monotonic;offset=[0.]
    monkeypatch.setattr(report.time,'monotonic',lambda:real()+offset[0] if threading.current_thread() is owner else real())
    def delayed_metrics(*args):
        offset[0]+=.1
        return dict(wakes_today=None,tokens_today=None,tokens_known=None,tokens_unknown_wakes=None)
    monkeypatch.setattr(floor_view,'_metrics',delayed_metrics)
    result=read(tmp_path,limit=200)
    assert result['questions']['state']=='known'
    assert result['questions']['items'][0]['text']=='Host help needed'
    assert result['seats']['state']=='known'
    assert len(result['seats']['items'])==2
