"""External archival never owns a tape write; producer staging follows outer commit."""
import json
import sqlite3
import pytest
from sniper.office import wallet_flow_archive as A


@pytest.fixture
def tape(tmp_path):
    path=tmp_path/'tape.db';directory=tmp_path/'archive'
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE wallet_flow_transactions(flow_id INTEGER PRIMARY KEY,signature TEXT,received_at REAL,payload_json TEXT,payload_archived INTEGER NOT NULL DEFAULT 0)')
        c.execute('CREATE INDEX wallet_flow_archive_pending ON wallet_flow_transactions(received_at,flow_id) WHERE payload_archived=0')
        c.execute('CREATE TABLE wallet_flow_meta(id INTEGER PRIMARY KEY,payload_bytes INTEGER,pending_metadata INTEGER)')
        for i in range(1,4):c.execute('INSERT INTO wallet_flow_transactions VALUES(?,?,?,?,0)',(i,'signature'+str(i),1.,json.dumps({'raw_update_b64':'AAAA'*20})))
        c.execute('INSERT INTO wallet_flow_meta SELECT 1,sum(length(payload_json)),0 FROM wallet_flow_transactions')
    return path,directory


def payloads(path):
    with sqlite3.connect(path) as c:return c.execute('SELECT payload_json FROM wallet_flow_transactions ORDER BY flow_id').fetchall()


def test_external_prepare_succeeds_while_producer_holds_writer_and_never_mutates(tape):
    path,directory=tape;before=payloads(path)
    with sqlite3.connect(path) as owner:
        owner.execute('BEGIN IMMEDIATE')
        result=A.archive_rows(path,directory,before=50.,limit=2)
        assert result['state']=='known' and result['queued']==2 and result['archived']==0
        assert owner.execute('SELECT payload_json FROM wallet_flow_transactions ORDER BY flow_id').fetchall()==before
    assert payloads(path)==before
    second=A.archive_rows(path,directory,before=50.,limit=2)
    assert second['reason']=='archive_apply_pending'
    assert len(list(directory.glob('*.pack')))==1


def test_producer_staging_is_invisible_until_outer_commit_and_rollback_retries(tape):
    path,directory=tape;before=payloads(path)
    A.archive_rows(path,directory,before=50.,limit=2)
    batch=A.next_batch(path,directory,limit=1)
    assert batch['state']=='known' and len(batch['items'])==1
    owner=sqlite3.connect(path)
    try:
        result=A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino))
        assert result['state']=='partial' and result['staged']==1 and owner.in_transaction
        assert payloads(path)==before
        owner.rollback()
        assert len(A.next_batch(path,directory,limit=1)['items'])==1
        A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino));owner.commit()
    finally:owner.close()
    after=payloads(path)
    assert after[0]!=before[0] and after[1:]==before[1:]
    assert A.read_payload(json.loads(after[0][0])['archive_payload_v1'],directory)==before[0][0]


def test_restart_after_commit_does_not_double_account_and_only_observed_markers_finish(tape):
    path,directory=tape;A.archive_rows(path,directory,before=50.,limit=2)
    batch=A.next_batch(path,directory,limit=2)
    with sqlite3.connect(path) as owner:
        A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino));owner.commit()
        accounted=owner.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
        repeated=A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino));owner.commit()
        assert repeated['staged']==0
        assert owner.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==accounted
    finished=A.next_batch(path,directory,limit=2)
    assert finished['state']=='known' and finished['items']==[]
    assert A.archive_rows(path,directory,before=50.,limit=2)['queued']==1


def test_changed_payload_refuses_batch_without_touching_other_rows_or_accounting(tape):
    path,directory=tape;A.archive_rows(path,directory,before=50.,limit=2)
    batch=A.next_batch(path,directory,limit=2)
    with sqlite3.connect(path) as owner:
        owner.execute("UPDATE wallet_flow_transactions SET payload_json='{}' WHERE flow_id=2");owner.commit()
        before=payloads(path);accounted=owner.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
        result=A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino))
        assert result['state']=='unknown'
        assert payloads(path)==before
        assert owner.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==accounted
        owner.rollback()


@pytest.mark.parametrize('damage',['oversize','source','identity','duplicate','pack','missing_pack'])
def test_corrupt_plan_or_pack_stays_unknown_and_originals_remain(tape,damage):
    path,directory=tape;before=payloads(path);A.archive_rows(path,directory,before=50.)
    queue=directory/'pending.json';plan=json.loads(queue.read_text())
    if damage=='oversize':queue.write_text(' '*(A.PLAN_BYTES+1))
    elif damage=='source':plan['source']='/wrong/tape.db';queue.write_text(json.dumps(plan))
    elif damage=='identity':plan['identity'][1]+=1;queue.write_text(json.dumps(plan))
    elif damage=='duplicate':plan['entries'].append(plan['entries'][0]);queue.write_text(json.dumps(plan))
    elif damage=='pack':(directory/plan['entries'][0]['file']).write_bytes(b'broken')
    else:(directory/plan['entries'][0]['file']).unlink()
    result=A.next_batch(path,directory)
    assert result['state']=='unknown' and result['items']==[]
    assert payloads(path)==before and queue.exists()


def test_opened_connection_identity_cannot_be_rebound_by_replacing_path(tape):
    path,directory=tape;A.archive_rows(path,directory,before=50.);batch=A.next_batch(path,directory)
    opened=(path.stat().st_dev,path.stat().st_ino);owner=sqlite3.connect(path)
    statements=[];owner.set_trace_callback(statements.append)
    path.rename(path.with_name('original.db'))
    replacement=sqlite3.connect(path);replacement.execute('CREATE TABLE unrelated(value)');replacement.commit()
    try:
        result=A.apply_batch(owner,path,batch,opened_identity=opened)
        assert result['state']=='unknown' and result['staged']==0
        assert statements==[]  # Identity refusal precedes any statement on the stale handle.
    finally:owner.close();replacement.close()


def test_apply_refuses_bad_marker_and_missing_connection_identity(tape):
    path,directory=tape;before=payloads(path);A.archive_rows(path,directory,before=50.);batch=A.next_batch(path,directory)
    with sqlite3.connect(path) as owner:
        assert A.apply_batch(owner,path,batch)['state']=='unknown'
        batch['items'][0]['marker']='[]'
        assert A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino))['state']=='unknown'
    assert payloads(path)==before


def test_decoded_batch_bytes_and_row_work_stay_bounded(tape):
    path,directory=tape;big=json.dumps({'raw_update_b64':'A'*200000})
    with sqlite3.connect(path) as c:
        c.executemany('INSERT INTO wallet_flow_transactions VALUES(?,?,?,?,0)',[(i,'sig'+str(i),1.,big) for i in range(4,34)])
        c.execute('UPDATE wallet_flow_meta SET payload_bytes=(SELECT sum(length(payload_json)) FROM wallet_flow_transactions)')
    A.archive_rows(path,directory,before=50.,limit=33)
    batch=A.next_batch(path,directory)
    assert 0<len(batch['items'])<=20
    assert sum(len(r['original'].encode()) for r in batch['items'])<=A.BATCH_BYTES


def test_pack_verification_happens_after_readonly_connections_close(tape,monkeypatch):
    path,directory=tape;real_connect=sqlite3.connect;readers=[];read=A.read_payload
    class Reader(sqlite3.Connection):
        def close(self):
            readers.remove(self);super().close()
    def connect(database,*args,**kwargs):
        assert '?mode=ro' in database and kwargs.get('uri') is True
        conn=real_connect(database,*args,**kwargs,factory=Reader);readers.append(conn);return conn
    def verify(*args):assert not readers;return read(*args)
    monkeypatch.setattr(A.sqlite3,'connect',connect);monkeypatch.setattr(A,'read_payload',verify)
    result=A.archive_rows(path,directory,before=50.)
    assert result['queued']==3,result
    assert len(A.next_batch(path,directory)['items'])==3
    assert not readers
