"""Live resolver is collect-only; no provider calls or production state."""
import importlib.util
import sqlite3
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('crawler_live_fixture',ROOT/'tests/test_memecoin_desk_crawler.py')
F=importlib.util.module_from_spec(spec);spec.loader.exec_module(F)


def live(desk):
    (desk/'narrative_guess').mkdir();(desk/'pumpswap').mkdir()
    c=sqlite3.connect(desk/'narrative_guess/narrative_guess.db')
    c.execute('CREATE TABLE launches(network_id TEXT,mint TEXT,symbol TEXT,first_seen_known_at REAL)')
    c.execute('INSERT INTO launches VALUES(?,?,?,?)',('solana',F.MINT,'FROG',F.T-100));c.commit();c.close()
    c=sqlite3.connect(desk/'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(mint TEXT,received_at REAL,network_id TEXT)');c.execute('CREATE INDEX swaps_ps_received ON swaps_ps(received_at)')
    c.execute('INSERT INTO swaps_ps VALUES(?,?,?)',(F.MINT,F.T-50,'solana'));c.commit();c.close()


def test_live_input_crawls_without_legacy_curve_files(tmp_path):
    d=F.world(tmp_path,posts=[('kol','$FROG',F.T)])
    live(d)
    for p in list((d/'launches').glob('*.db'))+list((d/'swaps').glob('*.db')):p.unlink()
    resolver=F.W.LiveResolver(d,F.T+10,clock_offset_s=0)
    db=F.C.connect(tmp_path/'out.db');calls=[]
    try:
        result=F.W.crawl_pass(db,x_dir=d/'x',desk_dir=d,call_model=lambda *_: calls.append(1) or {'text':F.answer(),'model_echo':F.C.MODEL},per_hour=20,per_day=600,now=F.T+10,clock=lambda:F.T+12,resolver=resolver)
        assert result['crawled']==1 and len(calls)==1
        assert result['resolver_source']=='guesser_ps_observed_lower_bound'
    finally:db.close()


def test_asof_chain_activity_and_ambiguity(tmp_path):
    d=F.world(tmp_path);live(d);other='1'*32
    g=sqlite3.connect(d/'narrative_guess/narrative_guess.db');p=sqlite3.connect(d/'pumpswap/tape.db')
    g.execute('INSERT INTO launches VALUES(?,?,?,?)',('solana',other,'FROG',F.T+5));g.commit()
    p.execute('INSERT INTO swaps_ps VALUES(?,?,?)',(other,F.T-20,'solana'));p.commit();g.close();p.close()
    r=F.W.LiveResolver(d,F.T+10,clock_offset_s=0)
    assert r.resolve('$FROG',F.T)==(F.MINT,None)
    assert r.resolve('$FROG',F.T+6)==(None,'ambiguous')
    assert r.resolve(F.MINT,F.T-60)==(None,None)
    assert r.resolve(F.MINT,F.T+86401)==(None,'outside_snapshot')
    assert r.resolve(other,F.T)==(None,None)


def test_source_failure_preserves_post_cursor_but_meta_still_works(tmp_path):
    d=F.world(tmp_path,posts=[('kol',F.MINT,F.T)],metas=[(F.MINT,None,F.T)])
    r=F.W.LiveResolver(d,F.T+10,clock_offset_s=0)
    db=F.C.connect(tmp_path/'out.db')
    try:
        stats={'unreadable_rows':0}
        assert F.W.ingest(db,d/'x',d,F.T+10,stats,resolver=r)==1
        assert db.execute("SELECT last_id FROM cursors WHERE kind='post'").fetchone() is None
        live(d)
        r=F.W.LiveResolver(d,F.T+11,clock_offset_s=0)
        assert F.W.ingest(db,d/'x',d,F.T+11,stats,resolver=r)==1
    finally:db.close()


@pytest.mark.parametrize('hour,day,n',[(20,600,20),(1000,600,600)])
def test_live_resolver_does_not_bypass_hour_or_day_caps(tmp_path,hour,day,n):
    d=F.world(tmp_path,posts=[('kol',F.MINT,F.T)]);live(d)
    r=F.W.LiveResolver(d,F.T+10,clock_offset_s=0);db=F.C.connect(tmp_path/'out.db')
    try:
        db.executemany('INSERT INTO attempts(mint,at) VALUES(?,?)',[(F.MINT,F.T)]*n);db.commit()
        result=F.W.crawl_pass(db,x_dir=d/'x',desk_dir=d,call_model=lambda *_:pytest.fail('cap bypass'),per_hour=hour,per_day=day,now=F.T+10,clock=lambda:F.T+12,resolver=r)
        assert result['cap_skipped']==1 and result['crawled']==0
    finally:db.close()


def test_clock_offset_and_non_solana_never_resolve(tmp_path):
    d=F.world(tmp_path);live(d)
    r=F.W.LiveResolver(d,F.T+200,clock_offset_s=150)
    assert r.resolve(F.MINT,F.T)==(None,None)
    assert r.resolve(F.MINT,F.T+100)==(F.MINT,None)
    c=sqlite3.connect(d/'narrative_guess/narrative_guess.db');c.execute("UPDATE launches SET network_id='56'");c.commit();c.close()
    assert F.W.LiveResolver(d,F.T+200,clock_offset_s=0).resolve(F.MINT,F.T)==(None,None)



def test_runtime_main_uses_live_resolver_and_never_exceeds_operator_caps(tmp_path,monkeypatch):
    from sniper.util import state_paths
    monkeypatch.setattr(state_paths,'require_state_root_outside_release',lambda:None)
    monkeypatch.setattr(F.W,'state_root',lambda:tmp_path)
    monkeypatch.setattr(F.W,'load_env_files',lambda *_:None)
    monkeypatch.setenv('MEMECOIN_DESK_CRAWLER_ENABLED','1')
    monkeypatch.setenv('MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR','999')
    monkeypatch.setenv('MEMECOIN_DESK_CRAWLER_CALLS_PER_DAY','999')
    seen=[]
    monkeypatch.setattr(F.W,'crawl_pass',lambda *a,**kw:seen.append(kw) or {})
    assert F.W.main([],call_model=lambda *_:pytest.fail('no real model'))==0
    assert isinstance(seen[0]['resolver'],F.W.LiveResolver)
    assert (seen[0]['per_hour'],seen[0]['per_day'])==(20,600)


def test_metadata_cap_disables_tickers_instead_of_false_uniqueness(tmp_path,monkeypatch):
    from sniper.services import memecoin_desk_crawler_live as L
    d=F.world(tmp_path);live(d)
    c=sqlite3.connect(d/'narrative_guess/narrative_guess.db')
    c.execute('INSERT INTO launches VALUES(?,?,?,?)',('solana','1'*32,'FROG',F.T-10));c.commit();c.close()
    monkeypatch.setattr(L,'META_LIMIT',1)
    r=L.LiveResolver(d,F.T+10,clock_offset_s=0)
    assert r.metadata_capped and r.resolve('$FROG',F.T)==(None,'coverage_incomplete')


@pytest.mark.parametrize('kind',['post','meta'])
def test_future_receipt_defers_cursor_and_recovers_at_cut(tmp_path,kind):
    d=F.world(tmp_path);live(d)
    F.add(d,posts=[('kol',F.MINT,F.T+100),('kol',F.MINT,F.T+1)] if kind=='post' else [],
          metas=[(F.MINT,None,F.T+100),(F.MINT,None,F.T+1)] if kind=='meta' else [])
    db=F.C.connect(tmp_path/'out.db')
    try:
        r=F.W.LiveResolver(d,F.T+10,clock_offset_s=0)
        assert F.W.ingest(db,d/'x',d,F.T+10,{'unreadable_rows':0},resolver=r)==0
        row=db.execute('SELECT last_id FROM cursors WHERE kind=?',(kind,)).fetchone()
        assert row is None or row[0]==0
        assert db.execute('SELECT COUNT(*) FROM inputs').fetchone()[0]==0
        r=F.W.LiveResolver(d,F.T+100,clock_offset_s=0)
        assert F.W.ingest(db,d/'x',d,F.T+100,{'unreadable_rows':0},resolver=r)==2
        assert F.W.ingest(db,d/'x',d,F.T+100,{'unreadable_rows':0},resolver=r)==0
    finally:db.close()


def test_snapshot_horizon_limits_resolution_and_ingestion(tmp_path):
    d=F.world(tmp_path,posts=[('kol',F.MINT,F.T+15)]);live(d)
    r=F.W.LiveResolver(d,F.T+10,clock_offset_s=0)
    assert r.resolve(F.MINT,F.T+11)==(None,'outside_snapshot')
    db=F.C.connect(tmp_path/'out.db')
    try:
        assert F.W.ingest(db,d/'x',d,F.T+20,{'unreadable_rows':0},resolver=r)==0
    finally:db.close()


@pytest.mark.parametrize('kind',['post','meta'])
@pytest.mark.parametrize('receipt',[None,float('inf')])
def test_invalid_receipt_is_deferred_not_consumed(tmp_path,kind,receipt):
    d=F.world(tmp_path,posts=[('kol',F.MINT,F.T)] if kind=='post' else [],
              metas=[(F.MINT,None,F.T)] if kind=='meta' else []);live(d)
    c=sqlite3.connect(d/'x'/f'{F.DAY}.db')
    c.execute('UPDATE '+('posts' if kind=='post' else 'token_meta')+' SET received_at=?',(receipt,));c.commit();c.close()
    db=F.C.connect(tmp_path/'out.db');stats={'unreadable_rows':0}
    try:
        assert F.W.ingest(db,d/'x',d,F.T+10,stats,resolver=F.W.LiveResolver(d,F.T+10,clock_offset_s=0))==0
        row=db.execute('SELECT last_id FROM cursors WHERE kind=?',(kind,)).fetchone()
        assert row is None or row[0]==0
        assert stats['deferred_receipts']==1
    finally:db.close()
