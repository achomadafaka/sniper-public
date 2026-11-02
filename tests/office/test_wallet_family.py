import json
import sqlite3

import pytest
from sniper.office.wallet_network import WalletNetwork
from sniper.office.wallet_family import materialize_candidates

A,B,F,P=('1'*32,'2'*32,'3'*32,'4'*32)
SIG='a'*64


@pytest.fixture
def source(tmp_path):
    desk=tmp_path/'desk'; (desk/'pumpswap').mkdir(parents=True)
    c=sqlite3.connect(desk/'pumpswap'/'tape.db')
    c.executescript('''CREATE TABLE ps_watch_events(signature TEXT,ordinal INTEGER,pool TEXT,mint TEXT,actor TEXT,slot INTEGER,tx_index INTEGER,is_buy INTEGER,received_at REAL,quote_atomic TEXT,pre_quote_reserve_atomic TEXT);
    CREATE INDEX watch_at ON ps_watch_events(received_at);
    CREATE TABLE wallet_setup_events(signature TEXT,outer_ix INTEGER,program TEXT,payer TEXT,wallet TEXT,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL);''')
    c.executemany('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(SIG,i,P,'5'*32,a,10,2,1,995.,'1','2') for i,a in enumerate((A,B))]);c.commit();c.close()
    net=WalletNetwork(tmp_path/'wallet_network.db',clock=lambda:1000.)
    c=net._conn()
    for actor in (A,B):
        net._insert(c,src=actor,dst=F,edge_type='funded_by',source='host',actor='collector',observed_at=990.,known_at=994.,proof_kind='receipt',proof_ref=actor,proof_state='verified',method='test')
    c.commit();c.close()
    return net,desk


def test_groups_atomic_transaction_and_freezes_on_restart(source):
    net,desk=source
    out=materialize_candidates(net,desk,1000.)
    assert out['state']=='known'
    row,=out['items']
    assert row['actors']==[A,B] and row['source_signature']==SIG
    assert row['source_receipt']==995. and row['due']==1005. and row['emitted_at']==1000.
    assert row['early'] is True and row['evidence_known_at']==994.
    assert row['policy']=='research_only' and row['ownership_proven'] is False
    net.clock=lambda:1010.
    again=materialize_candidates(net,desk,1010.)['items'][0]
    assert again==row
    c=sqlite3.connect(net.path)
    assert c.execute('SELECT count(*) FROM wallet_family_candidates').fetchone()[0]==1
    c.close()


def test_late_discovery_cannot_become_early(source):
    net,desk=source
    net.clock=lambda:1006.
    row,=materialize_candidates(net,desk,1006.)['items']
    assert row['early'] is False and row['coverage']['timing']=='late'
    net.clock=lambda:1007.
    assert materialize_candidates(net,desk,1007.)['items'][0]==row


def test_future_and_claimed_edges_do_not_supply_evidence(source):
    net,desk=source
    c=net._conn();c.execute('UPDATE edges SET known_at=1001');c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is None and row['evidence_known_at'] is None
    assert row['coverage']['funding']=='unknown' and row['evidence']['funding']==[]


def test_setup_is_sponsorship_and_future_setup_excluded(source):
    net,desk=source
    c=net._conn();c.execute('DELETE FROM edges');c.commit();c.close()
    c=sqlite3.connect(desk/'pumpswap'/'tape.db')
    program='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
    c.executemany('INSERT INTO wallet_setup_events VALUES(?,?,?,?,?,?,?,?,?,?)',[(SIG,0,program,F,A,1,1,None,991.,992.),(SIG,1,program,F,B,1,1,None,991.,1001.)]);c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    setup,=row['evidence']['setup']
    assert setup['wallet']==A and setup['kind']=='account_setup_sponsor'
    assert row['evidence']['funding']==[] and row['ownership_proven'] is False
    assert row['evidence_known_at']==992.


def test_missing_tape_is_unknown_not_empty(tmp_path):
    net=WalletNetwork(tmp_path/'network.db',clock=lambda:1000.)
    out=materialize_candidates(net,tmp_path/'missing',1000.)
    assert out['state']=='unknown' and out['reason']=='tape_unreadable'


@pytest.mark.parametrize('now,lookback,limit',[(float('nan'),60,100),(1000,-1,100),(1000,60,501),(True,60,100)])
def test_bad_bounds_refused(source,now,lookback,limit):
    net,desk=source
    assert materialize_candidates(net,desk,now,lookback_s=lookback,limit=limit)['state']=='refused'


def test_sell_and_future_receipts_are_excluded_and_limit_is_explicit(source):
    net,desk=source
    c=sqlite3.connect(desk/'pumpswap'/'tape.db')
    c.executemany('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(SIG+'b',0,P,'5'*32,A,10,3,0,996.,'1','2'),(SIG+'c',0,P,'5'*32,A,10,4,1,1001.,'1','2')]);c.commit();c.close()
    out=materialize_candidates(net,desk,1000.,limit=1)
    assert out['truncated'] is True
    assert all(r['source_receipt']<=1000. for r in out['items'])


def test_exact_due_is_late_and_family_sells_are_observations(source):
    net,desk=source
    c=sqlite3.connect(desk/'pumpswap'/'tape.db')
    c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(SIG+'s',3,P,'5'*32,F,10,3,0,1002.,'3598678930','10'))
    c.commit();c.close()
    net.clock=lambda:1005.
    row,=materialize_candidates(net,desk,1005.)['items']
    assert row['early'] is False
    assert row['source_slot']==10 and row['source_tx_index']==2
    sell,=row['evidence']['sells']
    assert sell['actor']==F and sell['quote_atomic']=='3598678930'
    assert row['coverage']['sells']=='partial'


def test_claimed_edges_and_missing_setup_table_stay_unknown(source):
    net,desk=source
    c=net._conn();c.execute("UPDATE edges SET proof_state='claimed'");c.commit();c.close()
    c=sqlite3.connect(desk/'pumpswap'/'tape.db');c.execute('DROP TABLE wallet_setup_events');c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['evidence']['funding']==[] and row['evidence']['setup']==[]
    assert row['coverage']['setup']=='unknown' and row['early'] is None


def test_cap_refuses_without_persisting_a_candidate(source):
    net,desk=source
    net.max_bytes=1
    out=materialize_candidates(net,desk,1000.)
    assert out['state']=='refused' and out['reason']=='disk_cap'
    c=net._conn();assert c.execute('SELECT count(*) FROM wallet_family_candidates').fetchone()[0]==0;c.close()


def test_future_observation_does_not_supply_early_evidence(source):
    net,desk=source
    c=net._conn();c.execute('UPDATE edges SET observed_at=1001');c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is None and row['evidence']['funding']==[]


def test_unindexed_watch_history_is_unknown_instead_of_unbounded_scan(source):
    net,desk=source
    c=sqlite3.connect(desk/'pumpswap'/'tape.db');c.execute('DROP INDEX watch_at');c.commit();c.close()
    out=materialize_candidates(net,desk,1000.)
    assert out['state']=='unknown' and out['reason']=='watch_index_missing'


def test_causal_creator_edges_join_observed_sells_without_retroactive_knowledge(source):
    net,desk=source
    creator='6'*32;mint='5'*32
    c=net._conn()
    net._insert(c,src=creator,dst=mint,edge_type='created_mint',source='host',actor='collector',observed_at=990.,known_at=999.,proof_kind='receipt',proof_ref='creator',proof_state='verified',method='test')
    net._insert(c,src='7'*32,dst=mint,edge_type='created_mint',source='host',actor='collector',observed_at=990.,known_at=1001.,proof_kind='receipt',proof_ref='future',proof_state='verified',method='test')
    c.commit();c.close()
    c=sqlite3.connect(desk/'pumpswap'/'tape.db');c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',('b'*64,0,P,mint,creator,10,3,0,999.,'1','2'));c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    edge,=row['evidence']['creators']
    assert edge['src']==creator and edge['known_at']==999.
    assert row['evidence']['sells'][0]['actor']==creator


def test_truncated_atomic_group_is_deferred_then_frozen_complete(source):
    net,desk=source
    out=materialize_candidates(net,desk,1000.,limit=1)
    assert out['items']==[]
    assert out['state']=='partial' and out['reason']=='atomic_group_truncated'
    c=net._conn();assert c.execute('SELECT count(*) FROM wallet_family_candidates').fetchone()[0]==0;c.close()
    net.clock=lambda:1001.
    row,=materialize_candidates(net,desk,1001.,limit=100)['items']
    assert row['actors']==[A,B] and len(row['actor_order'])==2
    assert row['emitted_at']==1001.


def test_expired_during_work_cannot_be_frozen_early(source,monkeypatch):
    from sniper.office import wallet_family as family
    net,desk=source
    original=family.WW.funding_links
    def slow_read(*args,**kwargs):
        out=original(*args,**kwargs)
        net.clock=lambda:1006.
        return out
    monkeypatch.setattr(family.WW,'funding_links',slow_read)
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is False and row['emitted_at']>=1006.


def test_excluded_funders_are_diagnostics_not_family_eligibility(source,monkeypatch):
    from sniper.office import wallet_watch
    net,desk=source
    monkeypatch.setattr(wallet_watch,'_known_infrastructure',lambda:frozenset({F}))
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is None and row['evidence']['funding']==[]
    assert len(row['evidence']['excluded_funders'])==2


def test_caller_future_time_is_refused(source):
    net,desk=source
    assert materialize_candidates(net,desk,1001.)['state']=='refused'


def test_commit_finishing_after_due_is_late(source,monkeypatch):
    net,desk=source
    original=net._conn
    class Connection:
        def __init__(self):self.conn=original()
        def __getattr__(self,name):return getattr(self.conn,name)
        def commit(self):
            self.conn.commit()
            net.clock=lambda:1006.
    monkeypatch.setattr(net,'_conn',Connection)
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is False and row['emitted_at']==1006.


def test_interrupted_timing_finalization_never_leaves_early_true(source,monkeypatch):
    net,desk=source
    original=net._conn
    class Connection:
        def __init__(self):self.conn=original();self.commits=0
        def __getattr__(self,name):return getattr(self.conn,name)
        def commit(self):
            self.commits+=1
            if self.commits==2:raise sqlite3.OperationalError('interrupted')
            self.conn.commit()
    monkeypatch.setattr(net,'_conn',Connection)
    assert materialize_candidates(net,desk,1000.)['state']=='unknown'
    monkeypatch.setattr(net,'_conn',original)
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['early'] is None and row['coverage']['timing']=='emission_pending'


def test_legacy_verified_link_is_not_strict_transfer_proof(source):
    net,desk=source
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='unknown'
    assert {r['method_version'] for r in row['evidence']['funding']}=={'test'}
    assert {r['transfer_proven'] for r in row['evidence']['funding']}=={False}


def test_receipt_collector_method_is_labeled_strict_without_rewriting_history(source):
    net,desk=source
    c=net._conn()
    c.execute("UPDATE edges SET method_version='wallet-watch-receipt-v1',proof_kind='chain_fetch'")
    c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='known'
    assert all(r['transfer_proven'] is True for r in row['evidence']['funding'])
    c=net._conn();c.execute("UPDATE edges SET method_version='seat-v1'");c.commit();c.close()
    assert materialize_candidates(net,desk,1000.)['items'][0]==row


@pytest.mark.parametrize('column,value',[('source','seat'),('proof_kind','signature'),('method_version','seat-v1')])
def test_method_name_alone_cannot_upgrade_legacy_proof(source,column,value):
    net,desk=source
    c=net._conn()
    c.execute("UPDATE edges SET method_version='wallet-watch-receipt-v1',proof_kind='chain_fetch'")
    c.execute(f'UPDATE edges SET {column}=?',(value,))
    c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='unknown'
    assert not any(r['transfer_proven'] for r in row['evidence']['funding'])


def test_confirmed_stream_receipt_is_strict_without_claiming_rpc_finality(source):
    net,desk=source
    c=net._conn()
    c.execute("UPDATE edges SET method_version='wallet-watch-stream-v1',proof_kind='research_stream'")
    c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='known'
    assert all(r['transfer_proven'] for r in row['evidence']['funding'])
    assert {r['proof_kind'] for r in row['evidence']['funding']}=={'research_stream'}


@pytest.mark.parametrize('kind,method',[('chain_fetch','wallet-watch-stream-v1'),('research_stream','wallet-watch-receipt-v1')])
def test_mixed_stream_and_rpc_provenance_does_not_upgrade_proof(source,kind,method):
    net,desk=source
    c=net._conn();c.execute('UPDATE edges SET proof_kind=?,method_version=?',(kind,method));c.commit();c.close()
    row,=materialize_candidates(net,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='unknown'
    assert not any(r['transfer_proven'] for r in row['evidence']['funding'])
