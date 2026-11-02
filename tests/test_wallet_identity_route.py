"""Identity evidence reaches the served Universe without granting trade authority."""
import json
import sqlite3

import pytest
from flask import Flask

from test_wallet_identity import A,B,NOW,inputs,project
from sniper.persistence import db_memecoin as m


@pytest.fixture
def client(tmp_path,monkeypatch):
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    monkeypatch.setenv('DB_PATH',str(tmp_path/'ledger.db'))
    monkeypatch.setattr('sniper.util.clock.real_utc_now_iso',lambda **kw:NOW)
    (tmp_path/'data').mkdir()
    c=sqlite3.connect(tmp_path/'ledger.db',check_same_thread=False);c.row_factory=sqlite3.Row
    m.ensure_memecoin_tables(c);m.add_wallet(c,'user-a','Alpha','')
    c.execute("INSERT INTO memecoin_address_registry(source_user_id,network_id,address,status,evidence_kind,first_seen_at,status_at) VALUES ('user-a','solana',?,'verified','receipt_match',?,?)",(A,NOW,NOW))
    c.commit()
    (tmp_path/'data/wallet_identity.json').write_text(json.dumps(project()))
    app=Flask(__name__);app.register_blueprint(create_api_memecoin_blueprint({'conn':c}))
    yield app.test_client(),c,tmp_path
    c.close()


def test_current_address_and_research_link_reach_live_route_without_writes(client):
    cl,c,_=client;before=c.total_changes
    response=cl.get('/api/memecoin/wallet-map/universe?chain=solana')
    assert response.status_code==200
    body=response.get_json();w,=body['wallets'];related,=w['identity']['related_wallets']
    assert w['identity']['verified_addresses'][0]['address']==A
    assert related['address']==B and related['status']=='suspected_alternate'
    assert related['n_coins']==3 and related['eligible_coins']==4
    assert all(k not in related for k in ('armed','effective','funded','capital','real_approved'))
    assert c.total_changes==before and w['armed'] is False
    assert body['identity_research']['state']=='ok'


def test_live_revocation_removes_account_address_and_downgrades_cached_suspect(client):
    cl,c,_=client
    c.execute("UPDATE memecoin_address_registry SET status='refuted'");c.commit()
    w,=cl.get('/api/memecoin/wallet-map/universe?chain=solana').get_json()['wallets']
    assert w['identity']['verified_addresses']==[]
    assert w['identity']['related_wallets'][0]['status']=='related_trader'


def test_verified_funding_reaches_live_route_without_activity_or_authority_writes(client):
    from test_wallet_identity_funding import funding_receipts
    cl,c,p=client;before=c.total_changes
    (p/'data/wallet_funding_receipts.json').write_text(json.dumps(funding_receipts()))
    (p/'data/wallet_identity.json').unlink()
    body=cl.get('/api/memecoin/wallet-map/universe?chain=solana').get_json()
    w,=body['wallets'];related,=w['identity']['related_wallets']
    assert related['address']==B and related['status']=='funding_verified_link'
    assert related['funding_clues'][0]['amount_sol']=='1'
    assert related['ownership_proven'] is False and w['armed'] is False
    assert body['identity_research']['funding_state']=='ok' and c.total_changes==before


def test_missing_artifact_and_other_chain_keep_unknown_separate_from_zero(client):
    cl,c,p=client;(p/'data/wallet_identity.json').unlink()
    w,=cl.get('/api/memecoin/wallet-map/universe?chain=solana').get_json()['wallets']
    assert w['identity']['verified_addresses'][0]['address']==A
    assert w['identity']['related_wallets'] is None
    w,=cl.get('/api/memecoin/wallet-map/universe?chain=8453').get_json()['wallets']
    assert w['identity']['state']=='not_measured'
    assert w['identity']['verified_addresses']==[]
