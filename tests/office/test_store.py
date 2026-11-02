"""Office evidence survives concurrency, crashes and rejected edits."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from sniper.office import store

IMMUTABLE=('claims','decisions','org_changes','grades','receipts','decision_bundles','trial_registry','opportunity_log','cost_allocations','outcome_captures','fee_attestations','sealed_views')


def test_canonical_json_is_byte_stable():
    assert store.canonical_json({'b':2,'a':'é'})==store.canonical_json({'a':'é','b':2})
    assert b'\xc3\xa9' in store.canonical_json({'a':'é'})
    for invalid in (float('nan'),float('inf'),float('-inf')):
        with pytest.raises(ValueError):store.canonical_json({'a':invalid})


def test_open_store_has_wal_foreign_keys_and_reopens(tmp_path):
    p=tmp_path/'office.db'
    con=store.open_store(p)
    assert con.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert con.execute('PRAGMA foreign_keys').fetchone()[0]==1
    digest=store.append(con,'receipts',{'value':1});con.close()
    con=store.open_store(p)
    assert con.execute('SELECT row_hash FROM receipts').fetchone()[0]==digest
    con.close()


@pytest.mark.parametrize('table',IMMUTABLE)
def test_append_only_tables_refuse_update_and_delete(tmp_path,table):
    con=store.open_store(tmp_path/'office.db',evaluation=table in ('grades','outcome_captures'));store.append(con,table,{'n':1})
    for sql,args in ((f'UPDATE {table} SET payload_json=?',('{}',)),(f'DELETE FROM {table} WHERE seq=?',(1,))):
        with pytest.raises(sqlite3.IntegrityError,match='append_only'):
            con.execute(sql,args)
        con.rollback()
    assert store.verify_chain(con,table)==(True,None)
    con.close()


def test_chain_detects_tamper(tmp_path):
    path=tmp_path/'office.db';con=store.open_store(path)
    for n in range(3):store.append(con,'claims',{'n':n})
    assert store.verify_chain(con,'claims')==(True,None)
    raw=sqlite3.connect(path)
    raw.execute('DROP TRIGGER claims_no_update')
    raw.execute('UPDATE claims SET payload_json=? WHERE seq=2',(json.dumps({'n':999}),));raw.commit();raw.close()
    assert store.verify_chain(con,'claims')==(False,2)
    con.close()


def test_append_joins_outer_transaction_and_rollback(tmp_path):
    con=store.open_store(tmp_path/'office.db')
    with pytest.raises(RuntimeError):
        with store.write_transaction(con):
            store.append(con,'claims',{'n':1})
            with store.write_transaction(con):store.append(con,'claims',{'n':2})
            raise RuntimeError('crash before outer commit')
    assert con.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==0
    assert store.verify_chain(con,'claims')==(True,None)
    con.close()


def test_table_identifier_and_record_validation(tmp_path):
    con=store.open_store(tmp_path/'office.db')
    for table in ('claims; DROP TABLE agents','agents','not_a_table'):
        with pytest.raises(ValueError):store.append(con,table,{'n':1})
    with pytest.raises((TypeError,ValueError)):store.append(con,'claims',[])
    with pytest.raises(ValueError):store.append(con,'claims',{'n':float('nan')})
    assert con.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==0
    con.close()


def test_concurrent_appends_form_one_chain(tmp_path):
    p=tmp_path/'office.db';store.open_store(p).close()
    def writer(author):
        con=store.open_store(p)
        try:
            return [store.append(con,'receipts',{'author':author,'n':n}) for n in range(10)]
        finally:con.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        hashes=list(pool.map(writer,('a','b')))
    con=store.open_store(p)
    assert len(set(hashes[0]+hashes[1]))==20
    assert con.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==20
    assert store.verify_chain(con,'receipts')==(True,None)
    con.close()


def test_private_evaluation_tables_are_separate(tmp_path):
    office=store.open_store(tmp_path/'office.db')
    private=store.open_store(tmp_path/'evaluation.db',evaluation=True)
    public_names={r[0] for r in office.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    private_names={r[0] for r in private.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {'grades','outcome_captures'}&public_names
    assert {'grades','outcome_captures'}<=private_names and 'agents' not in private_names
    with pytest.raises(ValueError,match='purpose'):store.open_store(tmp_path/'office.db',evaluation=True)
    office.close();private.close()


def test_opportunity_uniqueness_and_latest_capture_are_indexed(tmp_path):
    office=store.open_store(tmp_path/'office.db')
    row={'epoch_id':'e','network_id':'solana','mint':'m'}
    store.append(office,'opportunity_log',row)
    with pytest.raises(sqlite3.IntegrityError):store.append(office,'opportunity_log',{**row,'extra':1})
    plan=office.execute('EXPLAIN QUERY PLAN SELECT seq FROM opportunity_log WHERE epoch_id=? AND network_id=? AND mint=?',('e','solana','m')).fetchall()
    assert any('SEARCH' in r[3] for r in plan)
    private=store.open_store(tmp_path/'evaluation.db',evaluation=True)
    for state in ('pending','observed'):store.append(private,'outcome_captures',{'candidate_seq':1,'state':state})
    latest=store.latest(private,'outcome_captures','candidate_seq',1)
    assert json.loads(latest['payload_json'])['state']=='observed' and latest['seq']==2
    plan=private.execute('EXPLAIN QUERY PLAN SELECT seq FROM outcome_captures WHERE candidate_seq=? ORDER BY seq DESC LIMIT 1',(1,)).fetchall()
    assert any('SEARCH' in r[3] for r in plan) and not any('TEMP B-TREE' in r[3] for r in plan)
    office.close();private.close()


def test_receipt_cache_and_single_bundle_per_decision(tmp_path):
    con=store.open_store(tmp_path/'office.db')
    digest=store.append(con,'receipts',{'cache_key':'cache','result':{'n':1}})
    assert store.latest(con,'receipts','cache_key','cache')['row_hash']==digest
    plan=con.execute('EXPLAIN QUERY PLAN SELECT seq FROM receipts WHERE cache_key=? ORDER BY seq DESC LIMIT 1',('cache',)).fetchall()
    assert any('SEARCH' in r[3] for r in plan) and not any('TEMP B-TREE' in r[3] for r in plan)
    store.append(con,'decision_bundles',{'decision_id':'d','receipt_hash':digest})
    with pytest.raises(sqlite3.IntegrityError):
        store.append(con,'decision_bundles',{'decision_id':'d','receipt_hash':'different'})
    assert store.verify_chain(con,'decision_bundles')==(True,None)
    con.close()


def test_existing_receipts_gain_cache_index_without_rewriting(tmp_path):
    path=tmp_path/'office.db';old=sqlite3.connect(path)
    old.execute('PRAGMA foreign_keys=ON')
    old.execute('CREATE TABLE receipts(seq INTEGER PRIMARY KEY,payload_json TEXT,prev_hash TEXT UNIQUE,row_hash TEXT UNIQUE)');old.commit()
    digest=store.append(old,'receipts',{'cache_key':'legacy','n':1});old.close()
    con=store.open_store(path)
    assert store.latest(con,'receipts','cache_key','legacy')['row_hash']==digest
    assert store.verify_chain(con,'receipts')==(True,None)
    con.close()


def test_claim_reference_exists_before_decision_and_decision_id_is_unique(tmp_path):
    con=store.open_store(tmp_path/'office.db')
    with pytest.raises(sqlite3.IntegrityError):
        store.append(con,'decisions',{'decision_id':'d','claim_hash':'missing','candidate_seq':1})
    with store.write_transaction(con):
        for i in range(5):store.append(con,'claims',{'claim_hash':'c'+str(i)})
        store.append(con,'decisions',{'decision_id':'d','claim_hash':'c4','candidate_seq':1})
    # Sequences are local to tables: claim seq5 legitimately precedes decision seq1.
    assert con.execute('SELECT seq FROM decisions').fetchone()[0]==1
    assert store.latest(con,'claims','claim_hash','c4')['seq']==5
    with pytest.raises(sqlite3.IntegrityError):
        store.append(con,'decisions',{'decision_id':'d','claim_hash':'c0','candidate_seq':2})
    assert store.latest(con,'decisions','candidate_seq',1)['seq']==1
    assert store.verify_chain(con,'decisions')==(True,None)
    con.close()


def test_private_grade_latest_lookup_is_indexed(tmp_path):
    con=store.open_store(tmp_path/'evaluation.db',evaluation=True)
    store.append(con,'grades',{'decision_seq':1,'state':'censored'})
    store.append(con,'grades',{'decision_seq':1,'state':'graded'})
    assert json.loads(store.latest(con,'grades','decision_seq',1)['payload_json'])['state']=='graded'
    plan=con.execute('EXPLAIN QUERY PLAN SELECT seq FROM grades WHERE decision_seq=? ORDER BY seq DESC LIMIT 1',(1,)).fetchall()
    assert any('SEARCH' in row[3] for row in plan) and not any('TEMP B-TREE' in row[3] for row in plan)
    con.close()


@pytest.mark.parametrize('broken',[False,True])
def test_claim_fk_migration_preserves_or_refuses_legacy_records(tmp_path,broken):
    path=tmp_path/'old.db';old=sqlite3.connect(path);old.execute('PRAGMA foreign_keys=ON')
    for table in ('claims','decisions'):
        old.execute('CREATE TABLE '+table+'(seq INTEGER PRIMARY KEY,payload_json TEXT,prev_hash TEXT UNIQUE,row_hash TEXT UNIQUE)')
    old.commit()
    claim=store.append(old,'claims',{'claim_hash':'c'})
    decision=store.append(old,'decisions',{'decision_id':'d','claim_hash':'missing' if broken else 'c'})
    old.close()
    if broken:
        with pytest.raises(sqlite3.IntegrityError):store.open_store(path)
        raw=sqlite3.connect(path)
        assert raw.execute('SELECT row_hash FROM decisions').fetchone()[0]==decision
        raw.close()
    else:
        con=store.open_store(path)
        assert store.latest(con,'claims','claim_hash','c')['row_hash']==claim
        assert store.latest(con,'decisions','decision_id','d')['row_hash']==decision
        assert con.execute('PRAGMA foreign_key_check').fetchall()==[]
        con.close()


def test_append_refuses_connection_with_foreign_keys_disabled(tmp_path):
    path=tmp_path/'office.db';store.open_store(path).close()
    raw=sqlite3.connect(path)
    with pytest.raises(ValueError,match='foreign_keys_required'):
        store.append(raw,'decisions',{'decision_id':'d','claim_hash':'missing'})
    assert raw.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]==0
    raw.close()


def test_memory_clock_snapshot_identity_and_fts_rollback(tmp_path):
    import sqlite3
    conn = store.open_store(tmp_path / 'office.db')
    for value in (None, 'unknown', True):
        with pytest.raises((sqlite3.IntegrityError, ValueError)):
            store.append(conn, 'memory_entries', dict(namespace='office/lessons', content='alpha lesson', known_at=value))
    first = store.append(conn, 'memory_entries', dict(namespace='office/lessons', content='alpha lesson', known_at=10.))
    assert conn.execute("SELECT rowid FROM memory_entries_fts WHERE memory_entries_fts MATCH 'alpha'").fetchone()[0] == 1
    with pytest.raises(RuntimeError):
        with store.write_transaction(conn):
            store.append(conn, 'memory_entries', dict(namespace='office/lessons', content='rollback marker', known_at=11.))
            raise RuntimeError('abort')
    assert conn.execute("SELECT rowid FROM memory_entries_fts WHERE memory_entries_fts MATCH 'rollback'").fetchone() is None
    assert store.latest(conn, 'memory_entries', 'known_at', 10.)['row_hash'] == first
    store.append(conn, 'memory_snapshots', dict(snapshot_id='sealed', epoch_id='e', cut=10., members=[]))
    with pytest.raises(sqlite3.IntegrityError):
        store.append(conn, 'memory_snapshots', dict(snapshot_id='sealed', epoch_id='e', cut=11., members=[]))
    assert store.verify_chain(conn, 'memory_entries') == (True, None)


def test_memory_fts_filters_before_top_k_and_reopens_without_duplicate_index_rows(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    for text, known_at in [('alpha alpha alpha', 1.), ('alpha visible', 2.), ('alpha future', 100.)]:
        store.append(conn, 'memory_entries', dict(namespace='office/lessons', content=text, known_at=known_at))
    query = '''WITH visible(seq) AS (SELECT CAST(value AS INTEGER) FROM json_each(?))
        SELECT e.seq,bm25(memory_entries_fts) AS rank FROM memory_entries_fts
        JOIN memory_entries e ON e.seq=memory_entries_fts.rowid
        JOIN visible v ON v.seq=e.seq
        WHERE memory_entries_fts MATCH ? AND e.known_at<=?
        ORDER BY rank,e.seq LIMIT ?'''
    assert [r[0] for r in conn.execute(query, ('[2,3]', 'alpha', 10., 1))] == [2]
    conn.close()
    conn = store.open_store(tmp_path / 'office.db')
    assert conn.execute("SELECT COUNT(*) FROM memory_entries_fts WHERE memory_entries_fts MATCH 'alpha'").fetchone()[0] == 3
