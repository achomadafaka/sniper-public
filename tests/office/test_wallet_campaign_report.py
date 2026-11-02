import sqlite3
import json
from pathlib import Path

def test_missing_sources_stay_unknown_and_report_creates_no_state(tmp_path):
    from sniper.office.wallet_campaign_report import build_report
    out=build_report(tmp_path,as_of=1793550000.)
    assert out['funded_authority'] is False and out['profit_proven'] is False
    assert out['quotes']['state']=='unknown' and out['timing']['state']=='unknown'
    assert out['inventory']['state']=='unknown'
    assert list(tmp_path.iterdir())==[]

def test_report_quote_returns_are_conditional_and_future_exits_are_unknown(tmp_path):
    from sniper.office.wallet_campaign_quotes import SCHEMA,USDC
    from sniper.office.wallet_campaign_report import build_report
    folder=tmp_path/'data/research/dex_boost';folder.mkdir(parents=True)
    with sqlite3.connect(folder/'2026-11-01.db') as c:
        c.executescript(SCHEMA)
        c.execute('INSERT INTO campaign_studies(study_id,launch_id,campaign_id,mint,source_signature,source_receipt,known_at,emitted_at,decision_at,recorded_at,state,version,snapshot_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('s','l','p','3'*40,'5'*88,1793550000.,1793550001.,1793550001.,1793550001.,1793550002.,'pending','wallet-campaign-launch-quotes-v1','{}'))
        c.execute('INSERT INTO campaign_quotes(study_id,arm,offset_s,state,due_host,requested_host,response_host,input_mint,output_mint,input_atomic,acquired_qty_atomic) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('s','ready',0,'known',1793550001.,1793550002.,1793550002.,USDC,'3'*40,'25000000','999'))
        c.execute('INSERT INTO campaign_quotes(study_id,arm,offset_s,state,due_host,requested_host,response_host,input_mint,output_mint,input_atomic,expected_output_atomic) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('s','ready',5,'known',1793550007.,1793550007.,1793550008.,'3'*40,USDC,'999','30000000'))
    early=build_report(tmp_path,as_of=1793550005.)
    assert not early['quotes']['round_trips']
    late=build_report(tmp_path,as_of=1793550010.)
    row=late['quotes']['round_trips'][0]
    assert row['quoted_gross_usdc_atomic']=='5000000' and row['net_usdc_atomic'] is None
    assert late['profit_proven'] is False
    assert row['resource_comparison']=='unattributed'
    with sqlite3.connect(folder/'2026-11-01.db') as c:
        c.executescript('CREATE TABLE resource_epochs(epoch_id TEXT PRIMARY KEY,policy_json TEXT,first_seen_host REAL); CREATE TABLE resource_record_epochs(table_name TEXT,record_key TEXT,origin_epoch TEXT,first_write_epoch TEXT,last_write_epoch TEXT);')
        c.execute('INSERT INTO resource_epochs VALUES(?,?,?)',('epoch','{}',1793550001.))
        for offset in (0.,5.):c.execute('INSERT INTO resource_record_epochs VALUES(?,?,?,?,?)',('campaign_quotes',json.dumps(['s','ready',offset],separators=(',',':')),'legacy_unattributed','epoch','epoch'))
    assert build_report(tmp_path,as_of=1793550010.)['quotes']['round_trips'][0]['resource_comparison']=='unattributed'


def test_report_keeps_cap_and_no_entry_censors_in_denominator(tmp_path):
    from sniper.office.wallet_campaign_quotes import SCHEMA
    from sniper.office.wallet_campaign_report import build_report
    folder=tmp_path/'data/research/dex_boost';folder.mkdir(parents=True)
    with sqlite3.connect(folder/'2026-11-01.db') as c:
        c.executescript(SCHEMA)
        c.execute('INSERT INTO campaign_studies(study_id,mint,recorded_at,state,version,snapshot_json) VALUES(?,?,?,?,?,?)',('s','3'*40,1793550001.,'censored','wallet-campaign-launch-quotes-v1','{}'))
        c.execute('INSERT INTO campaign_quotes(study_id,arm,offset_s,state,reason,due_host,requested_host) VALUES(?,?,?,?,?,?,?)',('s','ready',0,'censored','cap_reached',1793550001.,1793550002.))
    r=build_report(tmp_path,as_of=1793550005.)['quotes']
    assert r['censor_reasons']['cap_reached']==1


def test_token_rotation_roles_keep_exact_flow_provenance_without_ownership_claim(tmp_path):
    from sniper.office.wallet_token_flow import SCHEMA
    from sniper.office.wallet_watch_collect import SCHEMA as C_SCHEMA
    from sniper.office.wallet_campaign_report import build_report
    p=tmp_path/'data/office/network-unused';p.parent.mkdir(parents=True)
    with sqlite3.connect(p.parent/'wallet_network.db') as c:
        c.executescript(SCHEMA+C_SCHEMA)
        c.execute('INSERT INTO watch_flow_timing(source,flow_id,signature,received_at,graph_commit_before,graph_commit_after,selector_json,gaps_json,flow_state) VALUES(?,?,?,?,?,?,?,?,?)',('s',1,'sig',100.,101.,102.,'{}','{}','known'))
        c.execute('INSERT INTO watch_token_transfers(signature,outer_ix,inner_ix,source_owner,destination_owner,mint,amount_atomic,state,instruction_json,fetch_id,source,flow_id,known_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('sig',0,-1,'A','B','M','123','known','{}','proof','s',1,101.))
    out=build_report(tmp_path,as_of=200.)
    assert out['inventory']['total_wallet_holdings']=='unknown'
    receiver=next(r for r in out['roles']['items'] if r['role']=='token_recipient')
    assert receiver['address']=='B' and receiver['ownership_proven'] is False and receiver['source_flow_id']==1
    with sqlite3.connect(p.parent/'wallet_network.db') as c:c.execute("UPDATE watch_token_transfers SET signature='different'")
    assert build_report(tmp_path,as_of=200.)['roles']['items']==[]
