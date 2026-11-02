"""Behavioral checks for real local capture; fixtures do not prove executable state."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from sniper.persistence import db_wallet_execution as store

BASE = datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp()


def iso(seconds):
    return datetime.fromtimestamp(BASE + seconds, timezone.utc).isoformat()


def tape(tmp_path, name='volume.db'):
    path = tmp_path / name
    c = sqlite3.connect(path)
    c.executescript('''
        CREATE TABLE pool_ticks(id INTEGER PRIMARY KEY,chain_id TEXT,pool TEXT,kind TEXT,
            block_no INTEGER,tx_sig TEXT,log_index INTEGER,amount0 TEXT,amount1 TEXT,
            liquidity TEXT,sqrt_price_x96 TEXT,observed_at TEXT,block_hash TEXT);
        CREATE INDEX pool_block ON pool_ticks(chain_id,pool,block_no);
        CREATE TABLE block_times(chain_id TEXT,block_no INTEGER,block_ts TEXT,block_hash TEXT,
            PRIMARY KEY(chain_id,block_no));
        CREATE TABLE onchain_pool_protocols(chain_id TEXT,pool TEXT,protocol TEXT,mint TEXT);
        CREATE INDEX mint_pools ON onchain_pool_protocols(chain_id,mint);
        CREATE TABLE pool_meta(chain_id TEXT,pool TEXT,token0 TEXT,token1 TEXT,
            PRIMARY KEY(chain_id,pool));
        CREATE TABLE volume_feed(chain_id TEXT PRIMARY KEY,since REAL,through REAL,
            received REAL,logs_seen INTEGER);
        CREATE TABLE volume_feed_lag(chain_id TEXT PRIMARY KEY,v4_lag_s REAL,v3_lag_s REAL);
        CREATE TABLE volume_feed_resets(chain_id TEXT,at REAL,reason TEXT,window_s REAL);
        CREATE TABLE tick_gaps(id INTEGER PRIMARY KEY,chain_id TEXT,from_block INTEGER,
            to_block INTEGER,reason TEXT,detected_at TEXT,resolved_at TEXT);
    ''')
    c.execute('INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)',('4663','pool','v4','mint'))
    c.execute('INSERT INTO pool_meta VALUES (?,?,?,?)',('4663','pool','mint','quote'))
    c.execute('INSERT INTO volume_feed VALUES (?,?,?,?,?)',('4663',BASE-300,BASE+10,BASE+10,5))
    c.execute('INSERT INTO volume_feed_lag VALUES (?,?,?)',('4663',.5,.5))
    for block, seconds in [(1,-121),(2,-120),(3,0),(4,1),(5,10),(6,1800)]:
        c.execute('INSERT INTO block_times VALUES (?,?,?,?)',('4663',block,iso(seconds),'hash'+str(block)))
    for row_id,block,log in [(1,2,5),(2,3,8),(3,3,2),(4,4,1)]:
        add_tick(c,row_id,block,log)
    c.commit(); c.close(); return path


def add_tick(c,row_id,block,log,*,chain='4663',pool='pool',hash_value=None):
    c.execute('INSERT INTO pool_ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (row_id,chain,pool,'v3_swap',block,'tx'+str(row_id),log,'-100','20','1000','2000',
               iso(block),hash_value or 'hash'+str(block)))


def origin(db, event='event', wallet='alice', chain='4663', mint='mint', seconds=0):
    with db:
        return store.record_origin(db,dict(upstream_event_id=event,source_user_id=wallet,
            source_handle=wallet,network_id=chain,mint=mint,normalized_side='buy',
            observed_at=iso(seconds),source_ts=iso(seconds-2),raw_json='{}',
            was_curated=0,was_real_approved=0))


def rows(db,source):
    return [json.loads(r[0]) for r in db.execute('SELECT payload FROM we_raw WHERE source=? ORDER BY rowid',(source,))]


def run(db,path,seconds=10,**kwargs):
    from sniper.research.wallet_execution.ordered_capture import archive_ordered_once
    return archive_ordered_once(db,path,now=iso(seconds),**kwargs)


def test_worker_selects_active_volume_without_legacy_override(monkeypatch,tmp_path):
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    monkeypatch.setenv('MEMECOIN_TICKS_DB',str(tmp_path/'stale.db'))
    from scripts.research.wallet_execution_worker import default_volume_path
    assert default_volume_path()==tmp_path/'data/memecoin_onchain_volume.db'


def test_every_feed_origin_enrolled_and_same_coin_shared(tmp_path):
    db=store.open_store(tmp_path/'r.db'); path=tape(tmp_path)
    ids=[origin(db),origin(db,'refused','bob'),origin(db,'other','watch',chain='solana')]
    report=run(db,path)
    requests=rows(db,'ordered_requests')
    assert {r['origin_id'] for r in requests}==set(ids)
    assert len(rows(db,'ordered_coins'))==2
    assert report['enrolled_origins']==3 and report['provider_requests']==0
    assert len(rows(db,'ordered_ticks'))==4
    assert db.execute('SELECT count(*) FROM we_episodes').fetchone()[0]==0
    assert all(r['window_start']==BASE-120 and r['window_end']>=BASE+1800 for r in requests)
    assert all(r['denominator']=='committed_feed_buy' for r in requests)


def test_ordered_pages_use_chain_order_not_insertion_order(tmp_path):
    db=store.open_store(tmp_path/'r.db'); path=tape(tmp_path);origin(db)
    report=run(db,path,row_limit=2)
    page=rows(db,'ordered_pages')[0]
    assert page['orders']==[[2,5,1],[3,2,3]]
    assert report['archived_ticks']==2 and report['row_limit']==2
    run(db,path,row_limit=2)
    assert rows(db,'ordered_pages')[1]['orders']==[[3,8,2],[4,1,4]]


def test_restart_dedup_tail_growth_and_late_insert_rescan(tmp_path):
    db_path=tmp_path/'r.db';db=store.open_store(db_path); path=tape(tmp_path);origin(db)
    run(db,path,row_limit=2);db.close();db=store.open_store(db_path)
    run(db,path,row_limit=2);run(db,path,row_limit=2)
    c=sqlite3.connect(path);add_tick(c,5,3,1);c.commit();c.close()
    for _ in range(3):run(db,path,row_limit=2)
    assert len(rows(db,'ordered_ticks'))==5
    origin(db,'later','bob',seconds=15);run(db,path,seconds=16,row_limit=20)
    later=next(r for r in rows(db,'ordered_requests') if r['origin_id']==store.digest({'feed_id':'later'}))
    assert later['window_end']==BASE+1815
    assert len(rows(db,'ordered_coins'))==1


def test_header_and_gap_revisions_are_immutable_not_overwritten(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    c=sqlite3.connect(path);c.execute('INSERT INTO tick_gaps VALUES (1,?,?,?,?,?,NULL)',
        ('4663',3,3,'lost',iso(1)));c.commit();c.close()
    run(db,path)
    c=sqlite3.connect(path);c.execute("UPDATE block_times SET block_hash='replacement' WHERE block_no=3")
    c.execute('UPDATE tick_gaps SET resolved_at=? WHERE id=1',(iso(5),));c.commit();c.close()
    report=run(db,path)
    headers=[r['row'] for r in rows(db,'ordered_headers') if r['row']['block_no']==3]
    gaps=[r['row'] for r in rows(db,'ordered_gaps')]
    assert {r['block_hash'] for r in headers}=={'hash3','replacement'}
    assert {r['resolved_at'] for r in gaps}=={None,iso(5)}
    assert report['header_mismatches']>0
    assert report['execution_path_status']=='blocked'


def test_observed_frontier_and_unknown_state_never_become_quiet_complete(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    report=run(db,path)
    assert report['observed_frontiers'][0]['through']==BASE+10
    assert report['observed_frontiers'][0]['scope']=='producer_reported_swap_frontier'
    assert report['execution_path_status']=='blocked'
    assert 'complete_venue_state_missing' in report['missing']
    assert 'quantity_bound_pricing_missing' in report['missing']
    assert report['tail_pending_coins']==1
    assert report['precontext_s']==120 and report['minimum_tail_s']==1800


@pytest.mark.parametrize('through',[None,0])
def test_absent_and_zero_frontier_retained_without_success(tmp_path,through):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    c=sqlite3.connect(path);c.execute('UPDATE volume_feed SET through=?',(through,));c.commit();c.close()
    report=run(db,path)
    observed=report['observed_frontiers'][0]['through']
    assert observed is None if through is None else observed==0
    assert report['execution_path_status']=='blocked'


def test_missing_tape_does_not_create_it_or_drop_enrollment(tmp_path):
    db=store.open_store(tmp_path/'r.db');origin(db)
    report=run(db,tmp_path/'missing.db')
    assert report['source_status']=='unavailable'
    assert report['enrolled_origins']==1
    assert not (tmp_path/'missing.db').exists()


def test_retention_and_replacement_are_explicit(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    run(db,path)
    path.rename(tmp_path/'old.db');replacement=tape(tmp_path)
    report=run(db,replacement)
    assert 'source_replaced' in report['collection_gaps']
    c=sqlite3.connect(path);c.execute('DELETE FROM block_times WHERE block_no<3');c.commit();c.close()
    report=run(db,path)
    assert 'precontext_retained_short' in report['collection_gaps']


def test_tail_is_captured_after_actual_book_would_have_closed(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    run(db,path)
    c=sqlite3.connect(path);add_tick(c,10,6,0)
    c.execute('UPDATE volume_feed SET through=?,received=?',(BASE+1801,BASE+1801));c.commit();c.close()
    report=run(db,path,seconds=1801)
    assert any(r['row']['block_no']==6 for r in rows(db,'ordered_ticks'))
    assert report['tail_pending_coins']==0
    assert report['execution_path_status']=='blocked'


def test_source_queries_and_enrollment_are_bounded(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path)
    for i in range(5):origin(db,str(i),wallet='watch'+str(i))
    report=run(db,path,origin_limit=2,row_limit=1,coin_limit=1)
    assert report['enrolled_origins']==2 and report['archived_ticks']==1
    assert report['origin_backlog']==3
    report=run(db,path,origin_limit=2,row_limit=1,coin_limit=1)
    assert report['enrolled_origins']==2 and report['origin_backlog']==1


def test_service_integration_keeps_feed_success_separate_from_capture_failure(tmp_path):
    from test_wallet_execution_worker import source_db
    from sniper.services.wallet_execution_research import collect_once
    db=store.open_store(tmp_path/'r.db')
    report=collect_once(db,source_db(tmp_path),now=iso(10),bootstrap='beginning',
                        volume_path=tmp_path/'missing.db')
    assert report['source_status']=='ok'
    assert report['ordered_capture']['source_status']=='unavailable'
    assert report['ordered_capture']['enrolled_origins']==3


def test_missing_log_order_is_explicit_and_pagination_still_advances(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    c=sqlite3.connect(path);c.execute('UPDATE pool_ticks SET log_index=NULL WHERE id=1');c.commit();c.close()
    first=run(db,path,row_limit=1)
    assert 'ordering_fields_missing' in first['collection_gaps']
    second=run(db,path,row_limit=1)
    assert second['scanned_ticks']==1
    assert len(rows(db,'ordered_ticks'))==2


def test_bad_receipt_clock_cannot_pin_coin_queue(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path)
    bad_mint=next('bad'+str(i) for i in range(100)
                  if store.digest({'chain':'4663','mint':'bad'+str(i)}) <
                  store.digest({'chain':'4663','mint':'mint'}))
    with db:
        store.record_origin(db,dict(upstream_event_id='bad',source_user_id='u',network_id='4663',
                                  mint=bad_mint,observed_at='unknown',raw_json='{}'))
    origin(db)
    for _ in range(3):run(db,path,coin_limit=1)
    assert len(rows(db,'ordered_ticks'))==4
    assert any(r['clock_status']=='receipt_clock_unknown' for r in rows(db,'ordered_requests'))


def test_archived_expired_window_does_not_rescan_forever(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    c=sqlite3.connect(path);c.execute('UPDATE volume_feed SET through=?,received=?',(BASE+1801,BASE+1801));c.commit();c.close()
    first=run(db,path,seconds=1801)
    assert first['processed_coins']==1
    second=run(db,path,seconds=1900)
    assert second['scanned_ticks']==0
    assert second['skipped_expired_coins']==1


def test_new_disjoint_window_records_older_unfinished_capture_cut(tmp_path):
    db=store.open_store(tmp_path/'r.db');path=tape(tmp_path);origin(db)
    run(db,path,row_limit=1)
    origin(db,'new_window',seconds=2000)
    report=run(db,path,seconds=2001,row_limit=1)
    cuts=rows(db,'ordered_window_cuts')
    assert len(cuts)==1 and cuts[0]['reason']=='prior_window_unfinished'
    assert cuts[0]['previous_window']['window_end']==BASE+1800
    assert db.execute("SELECT count(*) FROM we_source_gaps WHERE reason='prior_window_unfinished'").fetchone()[0]==1


def test_registered_four_hour_size_study_extends_capture_and_waits_for_its_tail(tmp_path):
    from test_wallet_execution_study import plan
    from test_wallet_execution_replay import policy
    from sniper.research.wallet_execution.study import seal_round, evaluate_round
    db = store.open_store(tmp_path/'r.db'); p = plan(); p.update(chain='4663',
        discovery_end=BASE-30000, validation_start=BASE+1, validation_end=BASE+20000,
        test_start=BASE+40000, test_end=BASE+60000, embargo_s=14460., outcome_tail_s=14460.)
    p['baseline'] = policy(clip_usd=131.25, max_hold_s=14400.)
    p['challengers'] = [policy(policy_id='full', clip_usd=175., max_hold_s=14400.)]
    key = seal_round(db, p, now=BASE)
    origin(db, seconds=10); run(db, tape(tmp_path), seconds=11)
    request = rows(db, 'ordered_requests')[0]
    assert request['window_end'] == BASE+10+14460
    assert request['study_ids'] == [key]
    assert evaluate_round(db, key, now=BASE+60001)['status'] == 'accruing'
    assert evaluate_round(db, key, now=BASE+74461)['status'] == 'blocked'
