import sqlite3
from sniper.persistence.db_wallet_execution import open_store
from sniper.services.wallet_execution_research import collect_once, build_coverage, roster_manifest


def source_db(tmp_path):
    p=tmp_path/'source.db'; c=sqlite3.connect(p)
    c.executescript('''CREATE TABLE memecoin_wallets(source_user_id TEXT,handle TEXT,paused INTEGER,
                      excluded INTEGER,real_approved INTEGER,real_chains TEXT);
                      CREATE TABLE memecoin_feed_observations_v2(id INTEGER PRIMARY KEY,
                      upstream_event_id TEXT,source_user_id TEXT,source_handle TEXT,network_id TEXT,
                      mint TEXT,normalized_side TEXT,observed_at TEXT,raw_json TEXT);''')
    c.execute('INSERT INTO memecoin_wallets VALUES (?,?,?,?,?,?)', ('alice','Alice',0,0,1,'["56","solana"]'))
    c.execute('INSERT INTO memecoin_wallets VALUES (?,?,?,?,?,?)', ('bob','Bob',0,0,0,None))
    for i,chain in [(1,'56'), (2,'8453'), (3,'56')]:
        c.execute('INSERT INTO memecoin_feed_observations_v2 VALUES (?,?,?,?,?,?,?,?,?)',
                  (i,str(i),'alice','Alice',chain,'mint','buy','2026-10-04T00:00:00+00:00','{}'))
    c.commit(); c.close(); return p


def test_incremental_all_source_census_without_attempt_or_funded_reads(tmp_path):
    source=source_db(tmp_path); db=open_store(tmp_path/'r.db')
    # Bootstrap gaps are explicit; tests request collection from the start.
    report=collect_once(db, source, now='2026-10-04T00:01:00+00:00', limit=2, bootstrap='beginning')
    assert report['new_origins']==2
    report=collect_once(db, source, now='2026-10-04T00:02:00+00:00', limit=2, bootstrap='beginning')
    assert report['new_origins']==1
    cards=build_coverage(db, now='2026-10-04T00:02:00+00:00')
    alice56=next(c for c in cards['cells'] if c['wallet']=='alice' and c['chain']=='56')
    assert alice56['signals']==2 and alice56['complete_episodes']==0
    assert alice56['status']=='blocked' and alice56['strategy'] is None
    sol=next(c for c in cards['cells'] if c['wallet']=='alice' and c['chain']=='solana')
    assert sol['signals']==0
    assert any(c['wallet']=='bob' and c['chain']=='unknown' for c in cards['cells'])
    assert cards['provider_requests']==0
    assert sqlite3.connect(source).execute('SELECT count(*) FROM memecoin_wallets').fetchone()[0]==2


def test_tail_bootstrap_does_not_pretend_historical_census_complete(tmp_path):
    db=open_store(tmp_path/'r.db'); collect_once(db, source_db(tmp_path), now='2026-10-04T00:01:00+00:00')
    cards=build_coverage(db, now='2026-10-04T00:01:00+00:00')
    assert cards['legacy_gap']=='not_enrolled_before_start'
    assert cards['total_origins']==0


def test_missing_source_is_not_empty_success(tmp_path):
    db=open_store(tmp_path/'r.db')
    result=collect_once(db, tmp_path/'missing.db', now='2026-10-04T00:00:00+00:00')
    assert result['source_status']=='unavailable' and not (tmp_path/'missing.db').exists()
    assert build_coverage(db, now='2026-10-04T00:00:00+00:00')['source_status']=='unavailable'


def test_coverage_counts_distinct_coins_and_utc_days_without_pooling_or_guessing_clocks(tmp_path):
    source = source_db(tmp_path)
    with sqlite3.connect(source) as c:
        c.execute("UPDATE memecoin_feed_observations_v2 SET mint='second', observed_at='2026-10-05T00:00:00' WHERE id=3")
    db = open_store(tmp_path/'r.db')
    collect_once(db, source, now='2026-10-05T00:02:00+00:00', bootstrap='beginning')
    cells = {(r['wallet'], r['chain']): r for r in build_coverage(db, now='2026-10-05T00:02:00+00:00')['cells']}
    assert cells['alice', '56']['distinct_coins'] == 2
    assert cells['alice', '56']['observed_days'] == 1
    assert cells['alice', '56']['unknown_clock_buys'] == 1
    assert cells['alice', '8453']['distinct_coins'] == 1
    assert cells['alice', 'solana']['distinct_coins'] == 0


def test_coverage_reports_sealed_size_round_without_claiming_quote_capture(tmp_path):
    from test_wallet_execution_study import plan
    from sniper.research.wallet_execution.study import seal_round
    db = open_store(tmp_path/'r.db')
    collect_once(db, source_db(tmp_path), now='1970-01-01T00:00:00+00:00', bootstrap='beginning')
    key = seal_round(db, plan(), now=0.)
    body = build_coverage(db, now='1970-01-01T00:01:00+00:00')
    assert body['studies'][0]['study_id'] == key
    assert body['studies'][0]['state'] == 'sealed_awaiting_execution_evidence'
    alice = next(c for c in body['cells'] if c['wallet'] == 'alice' and c['chain'] == '56')
    assert alice['study'] == 'sealed' and alice['status'] == 'blocked'
    assert not body['funded_authority'] and body['provider_requests'] == 0
