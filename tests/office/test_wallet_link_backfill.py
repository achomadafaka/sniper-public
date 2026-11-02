import json
import sqlite3

import base58
import pytest
from solders.keypair import Keypair

from sniper.office import wallet_link_backfill as L, wallet_watch as W
from sniper.office.wallet_network import WalletNetwork

A,B,C=[str(Keypair.from_seed(bytes([i])*32).pubkey()) for i in (1,2,3)]
SIG=base58.b58encode(bytes([4])*64).decode()


@pytest.fixture
def net(tmp_path,monkeypatch):
    (tmp_path/'desk/pumpswap').mkdir(parents=True)
    path=tmp_path/'chain.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE fetches(fetch_id TEXT PRIMARY KEY,state TEXT,result TEXT,shown_at REAL)')
    n=WalletNetwork(tmp_path/'graph.db',chain_db=path,clock=lambda:100.)
    W.add(n,tmp_path/'desk',address=A,operator_id=None,evidence='role',source='host',actor='test',now=90.)
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset())
    return n,tmp_path/'desk'


def link(n,recipient,funder,*,ref='proof',source='host',valid=True):
    fact=dict(state='known',kind='verified_funding',verified=True,funder=funder,recipient=recipient,funding_signature=SIG,funded_at=50.,amount_atomic='1000000000',method='confirmed_system_transfer',network_id='solana',provenance='research_rpc',tool='watch_verified_funding',asof=dict(kind='finalized_transaction',block_time_s=50.))
    if not valid:fact['recipient']=C
    with sqlite3.connect(n.chain_db) as c:c.execute('INSERT INTO fetches VALUES(?,?,?,?)',(ref,'known',json.dumps(fact),60.))
    with n._conn() as c:
        n._insert(c,src=recipient,dst=funder,edge_type='funded_by',source=source,actor='test',observed_at=50.,known_at=60.,proof_kind='chain_fetch',proof_ref=ref,proof_state='verified',method='wallet-watch-receipt-v1')


def test_old_verified_outgoing_link_is_watched_now_without_rewriting_graph(net):
    n,desk=net;link(n,B,A)
    assert L.expand_existing_links(n,desk,100.)['added']==1
    with n._conn() as c:
        assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==1
        assert c.execute('SELECT added_at FROM watch_list WHERE address=?',(B,)).fetchone()[0]==100.
    assert L.expand_existing_links(n,desk,100.)['added']==0


def test_old_verified_incoming_sender_can_be_observed_without_control_claim(net):
    n,desk=net;link(n,A,B)
    assert L.expand_existing_links(n,desk,100.)['added']==1
    with n._conn() as c:assert 'counterparty' in c.execute('SELECT evidence FROM watch_list WHERE address=?',(B,)).fetchone()[0]


def test_removed_and_infrastructure_targets_are_not_resurrected(net,monkeypatch):
    n,desk=net;link(n,B,A)
    W.add(n,desk,address=B,operator_id=None,evidence='role',source='host',actor='test',now=90.);W.remove(n,desk,address=B,now=91.)
    assert L.expand_existing_links(n,desk,100.)['added']==0
    link(n,C,A,ref='p2');monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({C}))
    assert L.expand_existing_links(n,desk,100.)['added']==0


def test_imports_and_wrong_receipt_endpoint_do_not_become_watch_authority(net):
    n,desk=net;link(n,B,A,valid=False);link(n,C,A,ref='import',source='import')
    assert L.expand_existing_links(n,desk,100.)['added']==0
    assert {r['address'] for r in W.read_list(n)}=={A}


def test_unproven_head_does_not_starve_next_link_after_restart(net):
    n,desk=net;link(n,B,A,valid=False);link(n,C,A,ref='good')
    assert L.expand_existing_links(n,desk,100.,limit=1)['added']==0
    assert L.expand_existing_links(n,desk,100.,limit=1)['added']==1
    assert C in {r['address'] for r in W.read_list(n)}


def test_malformed_stored_proof_is_deferred_not_a_worker_crash(net):
    n,desk=net;link(n,B,A)
    with sqlite3.connect(n.chain_db) as c:c.execute("UPDATE fetches SET result='[]'")
    assert L.expand_existing_links(n,desk,100.)['added']==0


def test_backfill_vm_work_ignores_unrelated_host_funding_history(net,monkeypatch):
    n,desk=net;link(n,B,A)
    with n._conn() as c:
        for i in range(100,5100):
            a=str(Keypair.from_seed(i.to_bytes(32,'little')).pubkey());b=str(Keypair.from_seed((i+6000).to_bytes(32,'little')).pubkey())
            n._insert(c,src=a,dst=b,edge_type='funded_by',source='host',actor='test',observed_at=50.,known_at=60.,proof_kind='research_stream',proof_ref=str(i),proof_state='verified',method='wallet-watch-stream-v1')
    queries=[];connect=n._conn
    def traced():
        c=connect();c.set_trace_callback(lambda sql:queries.append(sql) if 'watch_link_backfill_attempts tried' in sql else None);return c
    monkeypatch.setattr(n,'_conn',traced)
    assert L.expand_existing_links(n,desk,100.)['added']==1
    c=connect();steps=[0]
    def budget():steps[0]+=1;return steps[0]>20
    c.set_progress_handler(budget,1000)
    try:c.execute(queries[0]).fetchall()
    finally:c.close()


def test_finalized_receipt_cutoff_must_match_the_payment_time(net):
    n,desk=net;link(n,B,A)
    with sqlite3.connect(n.chain_db) as c:
        fact=json.loads(c.execute('SELECT result FROM fetches').fetchone()[0]);fact['asof']['block_time_s']=49.
        c.execute('UPDATE fetches SET result=?',(json.dumps(fact),))
    assert L.expand_existing_links(n,desk,100.)['added']==0
