"""Display enrichment reads exact stored book/order facts, never current FX or guessed supply."""
import json,sqlite3
from pathlib import Path
from sniper.office import order_price_fields as P
MAIN='office-v2-explore-1';GPT='office-v2-explore-gpt-1'

def fixture(root,book=MAIN):
    path=root/'v2'/('explore-ledger' if book==MAIN else 'explore-gpt-ledger');path.mkdir(parents=True)
    c=sqlite3.connect(path/'ledger.db');c.execute('CREATE TABLE orders(order_id TEXT,book_id TEXT,epoch_id TEXT,book_kind TEXT,network_id TEXT,mint TEXT,state TEXT,reason TEXT,requested_at REAL,filled_at REAL,closed_at REAL,entry_unit_price_usd REAL,exit_usd REAL,tokens_atomic TEXT,decimals INTEGER,decision_bundle_hash TEXT)')
    c.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('same',book,book,'evaluated','solana','mint','closed',None,10,20,30,1.1e-9,2.2,'1000000000000000000',9,'bundle'))
    c.commit();c.close()

def store(root):
    c=sqlite3.connect(root/'office.db');c.execute('CREATE TABLE decision_bundles(decision_id TEXT,payload_json TEXT,row_hash TEXT)');c.execute('CREATE TABLE receipts(row_hash TEXT,payload_json TEXT)')
    c.execute('INSERT INTO decision_bundles VALUES(?,?,?)',('same',json.dumps({'receipt_row_hash':'r'}),'bundle'))
    c.execute('INSERT INTO receipts VALUES(?,?)',('r',json.dumps({'sheet':{'pool_state':{'supply_atomic':'1000000000000000000','base_decimals':9,'metadata_known_at':19}}})))
    c.commit();c.close()

def report():return dict(epoch_id=MAIN,now=40,orders={'items':[dict(order_id='same',mint='mint',state='closed')]},separate_books=[dict(book_id=GPT,epoch_id=GPT,as_of=40,orders=[dict(order_id='same',mint='mint',state='closed')])])

def test_exact_ledger_prices_and_recorded_supply_both_books_are_separate(tmp_path):
    fixture(tmp_path);fixture(tmp_path,GPT);store(tmp_path);original=report();out=P.enrich(tmp_path,original)
    for row in [out['orders']['items'][0],out['separate_books'][0]['orders'][0]]:
        assert row['entry_unit_price_usd']==1.1e-9 and row['exit_unit_price_usd']==2.2e-9
        assert row['entry_mcap_usd']==1.1 and row['exit_mcap_usd']==2.2
        assert row['mcap_basis']=='recorded entry supply held constant; modelled'
        assert row['tokens_atomic']=='1000000000000000000'
    assert 'entry_unit_price_usd' not in original['orders']['items'][0]

def test_missing_sources_are_unavailable_without_creating_databases(tmp_path):
    out=P.enrich(tmp_path,report());assert list(tmp_path.iterdir())==[]
    assert out['orders']['items'][0]['price_fields_reason']=='ledger unavailable'

def test_future_supply_and_wrong_decimals_never_create_a_market_cap(tmp_path):
    fixture(tmp_path);store(tmp_path)
    c=sqlite3.connect(tmp_path/'office.db');c.execute('UPDATE receipts SET payload_json=?',(json.dumps({'sheet':{'pool_state':{'supply_atomic':'1000','base_decimals':6,'metadata_known_at':50}}}),));c.commit();c.close()
    row=P.enrich(tmp_path,report())['orders']['items'][0]
    assert row['entry_mcap_usd'] is None and row['exit_mcap_usd'] is None
    assert row['exit_unit_price_usd']==2.2e-9

def test_exact_identity_and_asof_prevent_newer_or_foreign_order_values(tmp_path):
    fixture(tmp_path);d=report();d['now']=25
    row=P.enrich(tmp_path,d)['orders']['items'][0];assert row['price_fields_reason']=='order identity or time differs'
    d=report();d['orders']['items'][0]['mint']='wrong';row=P.enrich(tmp_path,d)['orders']['items'][0]
    assert row['entry_unit_price_usd'] is None

def test_chart_book_identity_uses_the_same_exact_ledger_values(tmp_path):
    fixture(tmp_path,GPT)
    row=P.enrich(tmp_path,dict(epoch_id=MAIN,as_of=40,items=[dict(order_id='same',book_id=GPT,mint='mint',status='closed',state='known')]))['items'][0]
    assert row['exit_unit_price_usd']==2.2e-9 and row['entry_unit_price_usd']==1.1e-9


def test_wrong_bundle_cannot_supply_another_orders_market_cap(tmp_path):
    fixture(tmp_path);store(tmp_path)
    c=sqlite3.connect(tmp_path/'office.db');c.execute("UPDATE decision_bundles SET row_hash='foreign'");c.commit();c.close()
    row=P.enrich(tmp_path,report())['orders']['items'][0]
    assert row['entry_mcap_usd'] is None and row['exit_mcap_usd'] is None


def test_censored_reason_is_read_from_exact_gpt_order(tmp_path):
    fixture(tmp_path,GPT);c=sqlite3.connect(tmp_path/'v2/explore-gpt-ledger/ledger.db')
    c.execute("UPDATE orders SET state='censored',reason='watcher_outage',filled_at=NULL,closed_at=NULL,entry_unit_price_usd=NULL,exit_usd=NULL,tokens_atomic=NULL");c.commit();c.close()
    d=report();d['separate_books'][0]['orders'][0]['state']='censored'
    row=P.enrich(tmp_path,d)['separate_books'][0]['orders'][0]
    assert row['reason']=='watcher_outage' and row['entry_unit_price_usd'] is None and row['exit_unit_price_usd'] is None


def test_both_authenticated_routes_enrich_but_unauthorized_routes_do_not_read(tmp_path,monkeypatch):
    from flask import Flask
    from sniper.routes import api_office as A
    calls=[]
    monkeypatch.setattr(P,'enrich',lambda root,data:(calls.append(root) or dict(data,enriched=True)))
    monkeypatch.setattr(A,'read_operator_report',lambda *a,**k:dict(state='known'))
    monkeypatch.setattr(A.OfficeCharts,'read',lambda *a,**k:dict(state='known'))
    for authorized in (False,True):
        app=Flask(__name__);app.register_blueprint(A.create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:40,operator_authenticated=lambda:authorized)))
        for path in ('/api/office/explore','/api/office/positions/charts'):
            r=app.test_client().get(path);assert r.status_code==(200 if authorized else 401)
            if authorized:assert r.json['enriched'] and r.headers['Cache-Control']=='no-store'
        if not authorized:assert calls==[]
    assert len(calls)==2


def test_malformed_optional_lists_do_not_erase_the_report(tmp_path):
    data=dict(state='partial',orders={'items':None},separate_books=[None,{'book_id':GPT,'orders':None}],items=None)
    assert P.enrich(tmp_path,data)==data


def test_numeric_underflow_does_not_turn_missing_display_prices_into_zero(tmp_path):
    assert P.unit(5e-324,'9'*80,18) is None
    assert P.unit(0,'1000',9)==0
    fixture(tmp_path);store(tmp_path)
    c=sqlite3.connect(tmp_path/'v2/explore-ledger/ledger.db');c.execute('UPDATE orders SET entry_unit_price_usd=?',(5e-324,));c.commit();c.close()
    c=sqlite3.connect(tmp_path/'office.db');c.execute('UPDATE receipts SET payload_json=?',(json.dumps({'sheet':{'pool_state':{'supply_atomic':'1','base_decimals':9,'metadata_known_at':19}}}),));c.commit();c.close()
    row=P.enrich(tmp_path,report())['orders']['items'][0]
    assert row['entry_mcap_usd'] is None and row['entry_mcap_reason']=='market cap outside supported display range'


def test_foreign_payload_epoch_and_explicit_wrong_target_book_do_not_enrich(tmp_path):
    fixture(tmp_path)
    d=report();d['epoch_id']='foreign';assert P.enrich(tmp_path,d)['orders']['items'][0].get('entry_unit_price_usd') is None
    d=report();d['epoch_id']=MAIN;d['orders']['items'][0]['book_id']=GPT
    assert P.enrich(tmp_path,d)['orders']['items'][0]['entry_unit_price_usd'] is None


def test_missing_filled_or_closed_timestamp_cannot_prove_snapshot_prices(tmp_path):
    fixture(tmp_path);c=sqlite3.connect(tmp_path/'v2/explore-ledger/ledger.db');c.execute('UPDATE orders SET closed_at=NULL');c.commit();c.close()
    row=P.enrich(tmp_path,report())['orders']['items'][0]
    assert row['exit_unit_price_usd'] is None and row['price_fields_reason']=='order identity or time differs'
