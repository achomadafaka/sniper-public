"""Offline real materializer -> launch quote owner; no BUY or network client required."""
import importlib.util
import json
import sqlite3
from pathlib import Path


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_preparation_then_actual_creation_enters_quote_study_before_any_buy(tmp_path):
    helper=load('campaign_pipeline_fixture',Path(__file__).with_name('test_wallet_campaign.py'))
    source=helper.source.__wrapped__(tmp_path);net,desk,clock=source
    helper.funding(source);helper.run(source)
    first=helper.report(source)['items'][0]
    assert first['mints']==[] and first['status']=='no_observed_launch'
    helper.launch(source,receipt=1100.)
    with sqlite3.connect(net.path) as c:c.execute("UPDATE edges SET observed_at=1100,known_at=1101 WHERE type='created_mint'")
    with sqlite3.connect(net.chain_db) as c:
        raw=json.loads(c.execute("SELECT result FROM fetches WHERE fetch_id='launch-proof'").fetchone()[0]);raw['block_time']=1100;raw['asof']['block_time_s']=1100
        c.execute('UPDATE fetches SET shown_at=1101,result=? WHERE fetch_id=?',(json.dumps(raw),'launch-proof'))
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET processed_at=1101')
    clock[0]=1102.;helper.run(source)
    latest=helper.report(source)['items'][0]
    assert latest['first_recorded_at']==first['first_recorded_at']
    assert latest['mints']==[helper.M] and latest['buys']==[]
    owner_module=load('campaign_pipeline_quote_owner',Path(__file__).resolve().parents[2]/'scripts/research/dex_boost_collector.py')
    calls=[]
    def quote(i,o,n):
        calls.append((i,o,n));return dict(inputMint=i,outputMint=o,inAmount=str(n),outAmount=str(2**63),otherAmountThreshold=str(2**63-1),routePlan=[dict(swapInfo=dict(ammKey=helper.C))]),None
    owner=owner_module.Collector(intake_dir=tmp_path/'empty',out_dir=tmp_path/'quotes',desk_dir=desk,clock_fn=lambda:(0.,clock[0]),jupiter_fn=quote,ps_buy=None,ps_sell=None,paper_throttled_fn=lambda:False,now_fn=lambda:clock[0],campaign_probe=True,campaign_db=net.path)
    result=owner.tick()
    assert result['campaign']['source_reason'] is None and len(calls)==1
    assert calls[0][1]==helper.M and calls[0][2]==25_000_000
    with sqlite3.connect(tmp_path/'quotes/1970-01-01.db') as c:
        row=c.execute('SELECT source_receipt,emitted_at,snapshot_json FROM campaign_studies').fetchone()
        assert row[0]==1100 and row[1]>=1102
        assert json.loads(json.loads(row[2])['coverage_json'])['creation_verified'] is True
