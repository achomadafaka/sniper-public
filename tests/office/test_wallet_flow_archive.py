import json
import sqlite3

import pytest

from sniper.office import wallet_flow_archive as A


def compact(path,directory,*,before=50.,limit=500):
    """Exercise the producer provisioning/prepare/stage/commit/observe pipeline in fixtures."""
    with sqlite3.connect(path) as c:
        if 'payload_archived' not in {r[1] for r in c.execute('PRAGMA table_info(wallet_flow_transactions)')}:
            c.execute('ALTER TABLE wallet_flow_transactions ADD COLUMN payload_archived INTEGER NOT NULL DEFAULT 0')
        c.execute('CREATE INDEX IF NOT EXISTS wallet_flow_archive_pending ON wallet_flow_transactions(received_at,flow_id) WHERE payload_archived=0')
    result=A.archive_rows(path,directory,before=before,limit=limit)
    if result['state']=='unknown':return result
    archived=0
    for _ in range(30):
        batch=A.next_batch(path,directory)
        if batch['state']=='unknown':return batch
        if not batch['items']:return dict(state='known',archived=archived)
        owner=sqlite3.connect(path,timeout=0)
        try:
            staged=A.apply_batch(owner,path,batch,opened_identity=(path.stat().st_dev,path.stat().st_ino))
            if staged['state']=='unknown':return staged
            owner.commit();archived+=staged['staged']
        finally:owner.close()
    raise AssertionError('bounded fixture pipeline did not drain')


@pytest.fixture
def tape(tmp_path):
    path=tmp_path/'tape.db'
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE wallet_flow_transactions(flow_id INTEGER PRIMARY KEY,signature TEXT,received_at REAL,payload_json TEXT)')
        c.execute('CREATE TABLE wallet_flow_meta(id INTEGER PRIMARY KEY,payload_bytes INTEGER,pending_metadata INTEGER)')
        for i,at in [(1,1.),(2,99.)]:
            c.execute('INSERT INTO wallet_flow_transactions VALUES(?,?,?,?)',(i,'signature'+str(i),at,json.dumps({'raw_update_b64':'AAAA'*20})))
        c.execute('INSERT INTO wallet_flow_meta SELECT 1,sum(length(payload_json)),0 FROM wallet_flow_transactions')
    return path,tmp_path/'archive'


def test_pack_is_lossless_retains_flow_identity_and_restart_does_not_repack(tape):
    path,directory=tape
    with sqlite3.connect(path) as c:before=c.execute('SELECT flow_id,signature,received_at,payload_json FROM wallet_flow_transactions ORDER BY flow_id').fetchall()
    result=compact(path,directory,before=50.)
    assert result['state']=='known' and result['archived']==1
    with sqlite3.connect(path) as c:
        after=c.execute('SELECT flow_id,signature,received_at,payload_json FROM wallet_flow_transactions ORDER BY flow_id').fetchall()
        assert c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==sum(len(r[3].encode()) for r in after)
    ref=json.loads(after[0][3])['archive_payload_v1']
    assert after[0][:3]==before[0][:3] and after[1]==before[1]
    assert A.read_payload(ref,directory)==before[0][3]
    assert compact(path,directory,before=50.)['archived']==0
    with sqlite3.connect(path) as c:
        plan=c.execute('EXPLAIN QUERY PLAN SELECT flow_id FROM wallet_flow_transactions WHERE payload_archived=0 AND received_at<? ORDER BY received_at,flow_id LIMIT 500',(50.,)).fetchall()
        assert any('wallet_flow_archive_pending' in row[3] for row in plan)


def test_publish_failure_never_replaces_the_active_payload(tape,monkeypatch):
    path,directory=tape
    def fail(*a):raise OSError('disk full')
    monkeypatch.setattr(A,'_publish',fail)
    assert compact(path,directory,before=50.)['state']=='unknown'
    with sqlite3.connect(path) as c:assert 'raw_update_b64' in c.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0]


def test_sql_lock_after_publish_preserves_original_and_can_retry(tape):
    path,directory=tape
    compact(path,directory,before=.5)
    with sqlite3.connect(path) as lock:
        lock.execute('BEGIN IMMEDIATE')
        assert compact(path,directory,before=50.)['state']=='unknown'
        assert 'raw_update_b64' in lock.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0]
    assert compact(path,directory,before=50.)['archived']==1


def test_reader_refuses_missing_manifest_corruption_and_path_escape(tape):
    path,directory=tape;compact(path,directory,before=50.)
    with sqlite3.connect(path) as c:ref=json.loads(c.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0])['archive_payload_v1']
    with pytest.raises(ValueError):A.read_payload(dict(ref,file='../outside.pack'),directory)
    pack=directory/ref['file'];raw=pack.read_bytes();pack.write_bytes(b'x'*len(raw))
    with pytest.raises(ValueError):A.read_payload(ref,directory)
    pack.write_bytes(raw);(directory/(ref['file']+'.json')).unlink()
    with pytest.raises(ValueError):A.read_payload(ref,directory)


def test_unknown_byte_accounting_rolls_back_payload_replacement(tape):
    path,directory=tape
    with sqlite3.connect(path) as c:c.execute('DELETE FROM wallet_flow_meta')
    result=compact(path,directory,before=50.)
    assert result['state']=='unknown' and result['reason']=='archive_accounting_unknown'
    with sqlite3.connect(path) as c:assert 'raw_update_b64' in c.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0]


@pytest.mark.parametrize('manifest',[[],None,{'version':True,'entries':[]},{'version':1,'entries':{}}])
def test_malformed_manifest_is_a_typed_unknown(tape,manifest):
    path,directory=tape;compact(path,directory,before=50.)
    with sqlite3.connect(path) as c:ref=json.loads(c.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0])['archive_payload_v1']
    (directory/(ref['file']+'.json')).write_text(json.dumps(manifest))
    with pytest.raises(ValueError):A.read_payload(ref,directory)


def test_manifest_offset_beyond_file_is_typed_unknown(tape):
    path,directory=tape;compact(path,directory,before=50.)
    with sqlite3.connect(path) as c:ref=json.loads(c.execute('SELECT payload_json FROM wallet_flow_transactions WHERE flow_id=1').fetchone()[0])['archive_payload_v1']
    ref['offset']=1<<100
    manifest=directory/(ref['file']+'.json');doc=json.loads(manifest.read_text());doc['entries']=[ref];manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError):A.read_payload(ref,directory)


def test_brief_writer_contention_does_not_starve_archival(tape):
    import threading
    path,directory=tape;held=threading.Event()
    def hold():
        with sqlite3.connect(path) as c:
            c.execute('BEGIN IMMEDIATE');held.set();threading.Event().wait(.03)
    thread=threading.Thread(target=hold);thread.start();assert held.wait(1)
    # Provision once before acquiring the lock, like producer startup.
    thread.join()
    assert compact(path,directory,before=50.)['archived']==1
