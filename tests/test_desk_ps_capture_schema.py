"""Capture certificates use the real writer schema and never invent clean history."""
from contextlib import closing
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_capture as C
from test_memecoin_desk_pumpswap_worker import W,worker
from test_desk_ps_commit_watermark import source_block,heartbeat
from sniper.util import clock

NOW=1000.0


def beat(path,**changes):
    data=dict(ts=NOW,connected=True,pumpswap_capture=True,ps_capture_from=900.,ps_committed_through=999.)
    data.update(changes);(path/'universe.heartbeat').write_text(json.dumps(data))


@pytest.fixture
def tape(tmp_path):
    files=W.DayFiles(tmp_path)
    conn=files.conn('gaps_ps',NOW);conn.commit();beat(tmp_path)
    yield tmp_path,conn
    for c in files._open.values():c.close()


def proof(path,now=NOW):return C.capture_proof(path,now,venues=('ps',))


def test_real_dayfiles_schema_proves_capture_without_a_synthetic_span_table(tape):
    path,conn=tape
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() is None
    assert proof(path)=={'from':900.,'through':999.}
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() is None


@pytest.mark.parametrize('changes',[{'ts':994.},{'ts':1001.},{'connected':False},{'pumpswap_capture':False},
    {'ps_committed_through':994.},{'ps_committed_through':1001.},{'ps_committed_through':None},
    {'ps_capture_from':None},{'ps_capture_from':1000.},{'ps_capture_from':-1}])
def test_missing_stale_future_or_disconnected_proof_refuses(tape,changes):
    path,_=tape;beat(path,**changes)
    assert proof(path) is None


@pytest.mark.parametrize('cause,allowed',[('disconnect',False),('worker_start',False),('write_error',False),
                                        ('decode_loss',True),('virtual_quote_unproven',True)])
def test_actual_point_gaps_and_existing_decode_loss_semantics(tape,cause,allowed):
    path,conn=tape;conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(950,cause,1));conn.commit()
    assert (proof(path) is not None)==allowed


def test_gap_at_capture_start_is_not_skipped_by_recovery(tape):
    path,conn=tape;conn.execute("INSERT INTO gaps_ps VALUES(900,'disconnect',1)");conn.commit()
    assert proof(path) is None


@pytest.mark.parametrize('start,end,allowed',[(950,None,False),(850,900,False),(850,899,True),(1001,None,True)])
def test_present_legacy_overlay_still_blocks_overlapping_intervals(tape,start,end,allowed):
    path,conn=tape
    conn.execute('CREATE TABLE ps_gap_spans(start_at REAL,end_at REAL)')
    conn.execute('INSERT INTO ps_gap_spans VALUES(?,?)',(start,end));conn.commit()
    assert (proof(path) is not None)==allowed


@pytest.mark.parametrize('ddl',['DROP TABLE gaps_ps','CREATE TABLE ps_gap_spans(wrong_column REAL)'])
def test_missing_mandatory_or_malformed_optional_schema_refuses(tape,ddl):
    path,conn=tape;conn.execute(ddl);conn.commit()
    assert proof(path) is None


def test_missing_tape_is_unknown_even_with_a_fresh_heartbeat(tmp_path):
    beat(tmp_path)
    assert proof(tmp_path) is None


def test_real_writer_reset_requires_new_committed_source_and_cannot_bridge_old_receipts(tmp_path,monkeypatch):
    now=[1000.]
    monkeypatch.setattr(W.time,'time',lambda:now[0]);monkeypatch.setattr(clock,'offset_s',lambda:0.)
    w=worker(tmp_path,monkeypatch)
    source_block(w,1000.);w.commit_files();heartbeat(w)
    assert proof(tmp_path)=={'from':1000.,'through':1000.}
    now[0]=1001.;w.ps_gap('disconnect',1001.);w.commit_files();heartbeat(w)
    assert proof(tmp_path,1001.) is None
    now[0]=1002.;w.commit_files();heartbeat(w)
    assert proof(tmp_path,1002.) is None
    source_block(w,1002.);w.commit_files();heartbeat(w)
    cert=proof(tmp_path,1002.)
    assert cert=={'from':1002.,'through':1002.} and cert['from']>1000.
    restarted=worker(tmp_path,monkeypatch);restarted.gap('worker_start');restarted.commit_files();heartbeat(restarted)
    assert proof(tmp_path,1002.) is None
    now[0]=1003.;source_block(restarted,1003.);restarted.commit_files();heartbeat(restarted)
    assert proof(tmp_path,1003.)=={'from':1003.,'through':1003.}
    for f in (w.files,restarted.files):
        for c in f._open.values():c.close()


def test_unreadable_tape_is_unknown(tmp_path):
    beat(tmp_path)
    path = tmp_path / "pumpswap" / "tape.db"
    path.parent.mkdir()
    path.write_bytes(b"not a sqlite database")
    assert proof(tmp_path) is None


def test_idle_heartbeat_releases_its_maintenance_transaction(tmp_path,monkeypatch):
    monkeypatch.setattr(W.time,'time',lambda:1000.)
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    w=worker(tmp_path,monkeypatch)
    try:
        source_block(w,1000.);w.commit_files()
        conn=w.files._open[('pumpswap','all')]
        assert not conn.in_transaction
        heartbeat(w)
        assert not conn.in_transaction
    finally:
        for conn in w.files._open.values():conn.close()


def test_heartbeat_does_not_commit_pending_capture_rows(tmp_path,monkeypatch):
    monkeypatch.setattr(W.time,'time',lambda:1000.)
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    w=worker(tmp_path,monkeypatch)
    try:
        source_block(w,1000.);w.commit_files()
        conn=w.files._open[('pumpswap','all')]
        conn.execute('INSERT INTO ps_decode_losses VALUES(?,?,?,?)',('pending','decode_loss',1001,1001.))
        heartbeat(w)
        assert conn.in_transaction
        with closing(sqlite3.connect((tmp_path/'pumpswap/tape.db').as_uri()+'?mode=ro',uri=True))as reader:
            assert reader.execute("SELECT count(*) FROM ps_decode_losses WHERE signature='pending'").fetchone()==(0,)
        assert conn.execute("SELECT count(*) FROM ps_decode_losses WHERE signature='pending'").fetchone()==(1,)
    finally:
        for conn in w.files._open.values():conn.close()
