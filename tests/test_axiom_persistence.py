"""Axiom upgrades preserve identity, source units, and receipt provenance."""
import sqlite3

from sniper.persistence import db_axiom as db

NOW = '2026-09-16T12:00:00+00:00'


def test_upgrade_keeps_first_duplicate_and_keeps_distinct_chains():
    c = sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE axiom_wallet_events (
        id INTEGER PRIMARY KEY, received_mono_ns INTEGER NOT NULL, received_at TEXT NOT NULL,
        address TEXT NOT NULL, chain TEXT NOT NULL, mint TEXT NOT NULL, side TEXT NOT NULL,
        amount_usd REAL, tx_sig TEXT, axiom_ts TEXT, raw_id INTEGER);
        INSERT INTO axiom_wallet_events VALUES (1,10,'old','A','56','M','buy',1,'TX',NULL,NULL);
        INSERT INTO axiom_wallet_events VALUES (2,20,'new','A','56','M','buy',1,'TX',NULL,NULL);
        INSERT INTO axiom_wallet_events VALUES (3,30,'new','A','8453','M','buy',1,'TX',NULL,NULL);''')
    db.ensure_axiom_tables(c)
    assert c.execute('SELECT id,received_mono_ns FROM axiom_wallet_events ORDER BY id').fetchall() == [(1,10),(3,30)]
    assert c.execute('SELECT DISTINCT observation_kind,clock_domain FROM axiom_wallet_events').fetchall() == [('unknown',None)]


def test_history_does_not_consume_the_first_live_receipt():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    ev={'address':'A','chain':'solana','mint':'M','side':'buy','tx_sig':'TX'}
    db.insert_wallet_event(c,ev,received_mono_ns=10,received_at=NOW,transport='rest',observation_kind='history')
    db.insert_wallet_event(c,ev,received_mono_ns=20,received_at=NOW,transport='ws',observation_kind='live',clock_domain='host:boot',capture_run_id='run')
    db.insert_wallet_event(c,ev,received_mono_ns=30,received_at=NOW,transport='ws',observation_kind='live',clock_domain='host:boot',capture_run_id='run')
    assert c.execute('SELECT observation_kind,received_mono_ns FROM axiom_wallet_events ORDER BY id').fetchall()==[('history',10),('live',20)]


def test_receipt_domain_and_identity_survive_storage():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    db.record_j7_receipts(c,[dict(fomo_event_id='E',received_mono_ns=1,received_at=NOW,clock_domain='h:b',capture_run_id='r',identity_json={'chain':'56','tx_sig':'T'})])
    row=c.execute('SELECT clock_domain,capture_run_id,identity_json FROM j7_receipts').fetchone()
    import json
    assert row[:2]==('h:b','r') and json.loads(row[2])=={'chain':'56','tx_sig':'T'}


def test_discovery_retains_native_units_and_unknown_volume_window():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    db.insert_discovery(c,[dict(chain='solana',mint='M',mcap=100,mcap_unit='SOL',liquidity=10,liquidity_unit='SOL',volume_value=9,volume_unit='USD',volume_window_s=None,parser_version='v2')],NOW)
    assert c.execute('SELECT mcap_unit,liquidity_unit,volume_m5,volume_window_s,parser_version FROM axiom_discovery_tokens').fetchone()==('SOL','SOL',None,None,'v2')


def test_raw_size_counts_utf8_and_prunes_oldest_payload():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    for payload in ('abc','éé','last'):
        db.insert_raw(c,received_mono_ns=1,received_at=NOW,transport='ws',source='s',page='p',payload=payload)
    c.commit()
    assert c.execute('SELECT byte_len FROM axiom_raw_messages ORDER BY id').fetchall()==[(3,),(4,),(4,)]
    assert db.prune_raw(c,now_iso=NOW,retention_days=7,max_bytes=8)==1
    assert c.execute('SELECT payload FROM axiom_raw_messages ORDER BY id').fetchall()==[('éé',),('last',)]


def test_callout_id_survives_content_edits_and_separates_sources():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    row=dict(kol_handle='K',mint='M',chain='solana',text='one',source_kind='gmgn',source_id='123',identity_kind='source_id')
    db.insert_kol_callouts(c,[row],NOW)
    db.insert_kol_callouts(c,[dict(row,text='edit')],NOW)
    db.insert_kol_callouts(c,[dict(row,source_kind='axiom')],NOW)
    assert c.execute('SELECT COUNT(*) FROM axiom_kol_callouts').fetchone()[0]==2
    assert c.execute('SELECT DISTINCT identity_kind FROM axiom_kol_callouts').fetchall()==[('source_id',)]


def test_identity_refresh_preserves_metric_age():
    c=sqlite3.connect(':memory:');db.ensure_axiom_tables(c)
    row=dict(x_handle='K',chain='solana',address='A',pnl_24h=5)
    db.upsert_directory(c,[row],NOW)
    db.upsert_directory(c,[dict(x_handle='K',chain='solana',address='A',source='axiom_tracked_identity')],'2026-09-16T13:00:00+00:00')
    assert c.execute('SELECT pnl_24h,metrics_seen_at,seen_at FROM axiom_trader_directory').fetchone()==(5,NOW,'2026-09-16T13:00:00+00:00')
