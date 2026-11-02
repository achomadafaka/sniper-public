"""Retained block receipts repair only bounded producer-owned NULL timestamps."""
import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

spec=importlib.util.spec_from_file_location('metadata_repair_worker',Path(__file__).resolve().parents[1]/'scripts/memecoin_desk_universe_worker.py')
W=importlib.util.module_from_spec(spec)
with patch('sniper.util.envfiles.load_env_files'):spec.loader.exec_module(W)

@pytest.fixture
def worker(tmp_path,monkeypatch):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','1')
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_EMIT','0')
    cfg=W.knobs({});cfg.update(wallet_flow='1',txshape_pct='0')
    w=W.Worker(cfg,base=tmp_path/'data/desk',sol_price_fn=lambda:None,alert_fn=lambda *a:None)
    c=w.wallet_flow_conn(172800.)
    for i in range(1,26):
        c.execute('INSERT INTO wallet_flow_transactions(signature,slot,received_at,processed_at,commitment,payload_json) VALUES(?,?,?,?,?,?)',(str(i),i,172800.,172801.,'confirmed','{}'))
    c.execute('UPDATE wallet_flow_meta SET pending_metadata=25');c.commit()
    yield w,c
    c.close()

def blocks(w,day,rows):
    p=w.base/'blocks'/f'{day}.db';p.parent.mkdir(exist_ok=True)
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE blocks(slot INTEGER PRIMARY KEY,block_time INTEGER,received_at REAL)')
        c.executemany('INSERT INTO blocks VALUES(?,?,?)',[(s,t,172802.) for s,t in rows])
    return p

def test_repairs_bounded_exact_slots_previous_day_and_preserves_receipt_clock(worker):
    w,c=worker;blocks(w,'1970-01-02',[(i,172799) for i in range(1,26)])
    w.wallet_flow_maintenance(172900.)
    assert c.execute('SELECT count(*) FROM wallet_flow_transactions WHERE block_time=172799').fetchone()[0]==20
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==5
    assert c.execute('SELECT received_at,processed_at FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(172800.,172900.)
    c.rollback()
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==25
    w.wallet_flow_maintenance(172901.);c.commit()
    w.wallet_flow_maintenance(172902.);c.commit()
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==0

@pytest.mark.parametrize('damage',['absent','corrupt','wrong_slot','invalid_time','future_time'])
def test_missing_or_invalid_evidence_stays_unknown_and_later_rows_progress(worker,damage):
    w,c=worker
    if damage=='corrupt':
        p=w.base/'blocks'/'1970-01-03.db';p.parent.mkdir();p.write_bytes(b'bad sqlite')
    elif damage!='absent':blocks(w,'1970-01-03',[(999,172799)] if damage=='wrong_slot' else [(1,172901 if damage=='future_time' else -1)])
    blocks(w,'1970-01-02',[(25,172799)])
    w.wallet_flow_maintenance(172900.);c.commit()
    w.wallet_flow_maintenance(172901.);c.commit()
    assert c.execute('SELECT block_time FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(None,)
    assert c.execute('SELECT block_time FROM wallet_flow_transactions WHERE flow_id=25').fetchone()==(172799,)
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==24


def test_existing_timestamp_is_immutable_and_source_handles_are_readonly(worker,monkeypatch):
    w,c=worker;blocks(w,'1970-01-03',[(1,172700),(2,172799)])
    c.execute('UPDATE wallet_flow_transactions SET block_time=172600 WHERE flow_id=1')
    c.execute('UPDATE wallet_flow_meta SET pending_metadata=24');c.commit()
    real=W.sqlite3.connect;opened=[]
    def connect(path,*args,**kwargs):
        assert str(path).endswith('?mode=ro') and kwargs['timeout']==0
        opened.append(path)
        return real(path,*args,**kwargs)
    monkeypatch.setattr(W.sqlite3,'connect',connect)
    w.wallet_flow_maintenance(172900.);c.commit()
    assert c.execute('SELECT block_time FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(172600,)
    assert c.execute('SELECT block_time FROM wallet_flow_transactions WHERE flow_id=2').fetchone()==(172799,)
    assert len(opened)<=3
    assert not (w.base/'blocks'/'1970-01-02.db').exists()


def test_recent_null_waits_for_normal_metadata_delivery(worker):
    w,c=worker;blocks(w,'1970-01-03',[(1,172800)])
    c.execute('UPDATE wallet_flow_transactions SET received_at=172899 WHERE flow_id=1');c.commit()
    w.wallet_flow_maintenance(172900.);c.commit()
    assert c.execute('SELECT block_time FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(None,)


def test_metadata_arriving_after_midnight_uses_following_day_file(worker):
    w,c=worker
    c.execute('UPDATE wallet_flow_transactions SET received_at=172799 WHERE flow_id=1');c.commit()
    blocks(w,'1970-01-03',[(1,172798)])
    w.wallet_flow_maintenance(172900.);c.commit()
    assert c.execute('SELECT block_time,received_at,processed_at FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(172798,172799.,172900.)
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==24


def test_missing_retained_slot_repairs_from_finalized_research_cache_known_now(worker):
    import hashlib,json
    w,c=worker;office=w.base.parent/'office/v2';office.mkdir(parents=True)
    import base58
    signature=base58.b58encode(bytes([1])*64).decode();params=[signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')]
    key=hashlib.sha256(json.dumps(['getTransaction',params],sort_keys=True).encode()).hexdigest()
    identity=str((w.base/'pumpswap/tape.db').stat().st_dev)+':'+str((w.base/'pumpswap/tape.db').stat().st_ino)
    with sqlite3.connect(office/'metadata_jobs.db') as jobs:
        jobs.execute('CREATE TABLE metadata_schedule(id INTEGER PRIMARY KEY,source TEXT,identity TEXT,lane TEXT,clock_floor REAL)')
        jobs.execute('INSERT INTO metadata_schedule VALUES(1,?,?,?,?)',(str((w.base/'pumpswap/tape.db').resolve()),identity,'regular',172899.))
        jobs.execute('CREATE TABLE metadata_jobs(slot INTEGER PRIMARY KEY,signature TEXT,requested_at REAL,state TEXT,reason TEXT,next_try REAL,fetched_at REAL,known_at REAL,cache_key TEXT)')
        jobs.execute('INSERT INTO metadata_jobs VALUES(?,?,?,?,?,?,?,?,?)',(1,signature,172850.,'known',None,0.,172899.,172899.,key))
    with sqlite3.connect(office/'chain.db') as cache:
        cache.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,at REAL,expires REAL,doc TEXT)')
        cache.execute('INSERT INTO cache VALUES(?,?,?,?)',(key,172899.,200000.,json.dumps(dict(slot=1,blockTime=172799,transaction=dict(signatures=[signature])))))
    w.wallet_flow_maintenance(172900.);c.commit()
    assert c.execute('SELECT block_time,received_at,processed_at FROM wallet_flow_transactions WHERE flow_id=1').fetchone()==(172799,172800.,172900.)
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==24
