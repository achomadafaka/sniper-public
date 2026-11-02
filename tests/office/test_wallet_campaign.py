"""Persist preparation before observed BUY; no funding graph becomes common control."""
import json
import sqlite3

import pytest
from solders.pubkey import Pubkey
from sniper.office.wallet_network import WalletNetwork


def addr(n):return str(Pubkey.from_bytes(n.to_bytes(32,'big')))
A,B,H,M,C=(addr(i) for i in range(1,6))
SIG='5'*88


@pytest.fixture
def source(tmp_path):
    clock=[1000.]
    chain=tmp_path/'chain.db'
    with sqlite3.connect(chain) as c:c.execute('CREATE TABLE fetches(fetch_id TEXT PRIMARY KEY,shown_at REAL,tool TEXT,asked TEXT,state TEXT,reason TEXT,asof TEXT,result TEXT)')
    net=WalletNetwork(tmp_path/'network.db',chain_db=chain,clock=lambda:clock[0]);net._conn().close()
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.executescript('''CREATE TABLE wallet_setup_events(setup_id INTEGER PRIMARY KEY,signature TEXT,outer_ix INTEGER,program TEXT,payer TEXT,wallet TEXT,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL);
CREATE TABLE ps_watch_events(signature TEXT,ordinal INTEGER,pool TEXT,mint TEXT,actor TEXT,slot INTEGER,tx_index INTEGER,is_buy INTEGER,received_at REAL);
CREATE INDEX watch_at ON ps_watch_events(received_at);
CREATE TABLE wallet_flow_transactions(flow_id INTEGER PRIMARY KEY,signature TEXT UNIQUE,slot INTEGER,received_at REAL,processed_at REAL,commitment TEXT);''')
    return net,desk,clock


def funding(fixture,*,known=970.,observed=960.,who=A,seed=H,**kwargs):
    net,_,_=fixture;c=net._conn()
    options=dict(source='host',proof_state='verified',proof_kind='research_stream',method='wallet-watch-stream-v1');options.update(kwargs)
    net._insert(c,src=who,dst=seed,edge_type='funded_by',actor='test',observed_at=observed,known_at=known,proof_ref=who+str(known),**options)
    c.commit();c.close()


def setup(source,*,payer=H,wallet=B,received=971.,processed=972.,program='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'):
    with sqlite3.connect(source[1]/'pumpswap/tape.db') as c:c.execute('INSERT INTO wallet_setup_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(1,'6'*88,0,program,payer,wallet,100,1,970,received,processed))


def launch(source,*,receipt=980.,signature=SIG):
    net,desk,_=source
    fact=dict(state='known',tool='watch_stream_launch',provenance='research_stream',commitment='confirmed',method='confirmed_pump_create_v1',mint=M,developer=A,curve=C,signature=signature,slot=101,block_time=979,outer_ix=0,asof=dict(kind='confirmed_transaction',block_time_s=979),asked=dict(source=str((desk/'pumpswap/tape.db').resolve()),flow_id=1,signature=signature))
    with sqlite3.connect(net.chain_db) as c:c.execute('INSERT INTO fetches VALUES(?,?,?,?,?,?,?,?)',('launch-proof',981.,'watch_stream_launch',json.dumps(fact['asked']),'known',None,'{}',json.dumps(fact)))
    c=net._conn();net._insert(c,src=A,dst=M,edge_type='created_mint',source='host',actor='test',observed_at=979.,known_at=982.,proof_kind='research_stream',proof_ref='launch-proof',proof_state='verified',method='wallet-watch-launch-v1');c.commit();c.close()
    if receipt is not None:
        with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('INSERT INTO wallet_flow_transactions VALUES(?,?,?,?,?,?)',(1,signature,101,receipt,981.,'confirmed'))


def buy(source,*,received=990.,signature='7'*88,mint=M,actor=A):
    with sqlite3.connect(source[1]/'pumpswap/tape.db') as c:c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?)',(signature,0,C,mint,actor,102,1,1,received))


def run(source,**kwargs):
    from sniper.office.wallet_campaign import materialize_campaigns
    net,desk,clock=source
    return materialize_campaigns(net,desk,clock[0],**kwargs)


def report(source,**kwargs):
    from sniper.office.wallet_campaign import read_campaigns
    return read_campaigns(source[0],**kwargs)


def test_idle_preparation_denominator_persists_without_a_buy(source):
    funding(source);setup(source)
    out=run(source);assert out['state'] in ('known','partial')
    row,=report(source)['items']
    assert row['seed']==H and row['mints']==[] and row['ownership_proven'] is False
    assert row['status']=='no_observed_launch' and row['coverage']['launch_absence_proven'] is False
    assert {r['role'] for r in row['roles']}=={'funder','funding_recipient','setup_sponsor','setup_user'}
    assert row['first_recorded_at']>=1000 and row['prebuy_state']=='mint_unresolved'
    assert run(source)['changed']==0
    assert report(source)['items']==[row]
    assert report(source)['denominator']['preparations']==1


@pytest.mark.parametrize('kwargs',[dict(source='seat'),dict(proof_state='claimed'),dict(method='legacy'),dict(proof_kind='receipt'),dict(known=1001.),dict(observed=1001.)])
def test_noncausal_or_non_strict_funding_is_not_preparation(source,kwargs):
    funding(source,**kwargs);run(source)
    assert report(source)['items']==[]


def test_setup_does_not_guess_a_mint_or_treat_sponsor_as_controller(source):
    setup(source);run(source)
    row,=report(source)['items']
    assert row['mints']==[] and row['ownership_proven'] is False
    assert not any(r['role'] in ('developer','controller') for r in row['roles'])
    assert row['coverage']['inventory']['state']=='unknown'


def test_supported_launch_has_exact_receipt_and_a_postcommit_emission(source):
    funding(source);run(source)
    source[2][0]=1001.;launch(source);run(source)
    row,=report(source)['items'];bound,=row['launches']
    assert bound['mint']==M and bound['source_signature']==SIG and bound['curve']==C
    assert bound['source_receipt']==980. and bound['known_at']==982.
    assert bound['emitted_at']>=1001 and bound['version']=='wallet-campaign-launch-v1'
    assert row['mints']==[M]
    assert any(r['role']=='developer' and r['address']==A for r in row['roles'])
    with sqlite3.connect(source[0].path) as c:
        assert c.execute('SELECT count(*) FROM wallet_campaign_launches').fetchone()[0]==1
    assert run(source)['changed']==0


def test_missing_receipt_never_substitutes_block_or_known_time(source):
    funding(source);launch(source,receipt=None);run(source)
    bound,=report(source)['items'][0]['launches']
    assert bound['source_receipt'] is None
    assert bound['coverage']['receipt_state']=='unknown'


def test_same_transaction_buy_is_post_atomic_not_a_prebuy_lead(source):
    funding(source);launch(source);buy(source,signature=SIG);run(source)
    row,=report(source)['items']
    assert row['prebuy_state']=='post_atomic_seed'
    assert row['launches'][0]['coverage']['prebuy_state']=='post_atomic_seed'


def test_late_preparation_and_unrelated_mint_buy_never_retroclaim_a_lead(source):
    funding(source);launch(source);buy(source,received=990.);buy(source,received=991.,signature='8'*88,mint=addr(9));run(source)
    row,=report(source)['items']
    assert row['prebuy_state']=='late'
    assert row['mints']==[M]  # other observed token is not inferred to be this family's launch
    assert {b['mint'] for b in row['buys']}=={M,addr(9)}


def test_old_revisions_and_prior_patterns_are_asof_frozen(source):
    funding(source);run(source)
    old=report(source)['items'][0]
    source[2][0]=1061.;funding(source,known=1050.,observed=1021.,who=B);run(source)
    rows=report(source)['items'];assert len(rows)==2
    new=next(r for r in rows if r['bucket_start']==1020)
    assert new['prior_pattern']['preparations']==1 and new['prior_pattern']['as_of']<=new['first_recorded_at']
    launch(source);run(source)
    assert report(source,as_of=1000.)['items']==[old]


def test_campaign_members_do_not_union_upstream_or_shared_funder_buys(source):
    funding(source);funding(source,seed=addr(8),who=H);buy(source,actor=H);run(source)
    row=next(r for r in report(source)['items'] if r['seed']==H)
    assert row['buys']==[] and row['ownership_proven'] is False


def test_caps_roll_back_all_new_campaign_state(source,monkeypatch):
    from sniper.office import wallet_network as N
    funding(source);monkeypatch.setattr(N,'MAX_ROWS_PER_DAY',1)
    assert run(source)['reason']=='daily_row_cap'
    assert report(source)['items']==[]


def test_missing_store_read_does_not_create_it(tmp_path):
    from sniper.office.wallet_campaign import read_campaigns
    path=tmp_path/'absent.db';net=WalletNetwork(path,clock=lambda:1000.)
    assert read_campaigns(net)['state']=='unknown' and not path.exists()


@pytest.mark.parametrize('limit',[0,101,True])
def test_invalid_work_limits_refused(source,limit):
    assert run(source,limit=limit)['state']=='refused'


def test_no_pumpswap_rows_cannot_prove_creation_had_no_curve_seed_buy(source):
    funding(source);launch(source);run(source)
    bound,=report(source)['items'][0]['launches']
    assert bound['coverage']['phase']=='unknown'
    assert bound['coverage']['creation_verified'] is True
    assert bound['coverage']['launch_proof_ref']=='launch-proof'
    assert bound['coverage']['launch_method']=='wallet-watch-launch-v1'


def test_launch_across_epoch_boundary_keeps_exact_member_binding(source):
    funding(source,observed=959.9);launch(source);run(source)
    bound,=report(source)['items'][0]['launches']
    assert bound['mint']==M and bound['coverage']['attachment']=='outside_preparation_bucket'


def test_future_evidence_is_revisited_without_retroactive_revision(source):
    funding(source,known=1001.);run(source)
    assert report(source)['items']==[]
    source[2][0]=1002.;run(source)
    assert report(source)['denominator']['preparations']==1
    assert report(source,as_of=1000.)['items']==[]


def test_source_page_skips_import_history_with_existing_overview_index(source):
    net=source[0];c=net._conn()
    for n in range(100,220):
        net._insert(c,src=addr(n),dst=H,edge_type='funded_by',source='import',actor='test',observed_at=960.,known_at=970.,proof_kind='import',proof_ref=str(n),proof_state='none',method='seed')
    c.commit();c.close();funding(source)
    run(source,limit=1)
    assert report(source)['denominator']['preparations']==1


def test_read_report_never_uses_writable_network_connection(source,monkeypatch):
    funding(source);run(source)
    monkeypatch.setattr(source[0],'_conn',lambda:pytest.fail('read report opened writer'))
    assert report(source)['state']=='known'


@pytest.mark.parametrize('commit_upper',[1000.,1001.,None])
def test_actual_token_consumer_rows_are_visible_at_successful_commit_upper(source,commit_upper):
    pytest.importorskip('sniper.office.wallet_token_flow',reason='Run again in frozen Delta+campaign integration')
    import base64
    import base58
    import struct
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from sniper.office.chain_data import ChainData
    from sniper.office.wallet_watch_collect import Collector
    from sniper.office import wallet_watch as W
    net,desk,clock=source
    chain=ChainData(net.chain_db,lambda:pytest.fail('RPC forbidden'),clock=lambda:clock[0]);net.chain=chain
    W.add(net,desk,address=A,operator_id=None,evidence='fixture',source='host',actor='test',now=990.)
    W.add(net,desk,address=B,operator_id=None,evidence='token owner observed',source='host',actor='test',now=990.)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        for name,kind in [('tx_index','INTEGER'),('block_time','INTEGER'),('payload_json','TEXT')]:c.execute('ALTER TABLE wallet_flow_transactions ADD COLUMN '+name+' '+kind)
        u=pb.SubscribeUpdate();u.transaction.slot=100
        info=u.transaction.transaction;info.signature=bytes([3])*64;info.transaction.signatures.append(info.signature)
        msg=info.transaction.message;msg.header.num_required_signatures=1;msg.header.num_readonly_unsigned_accounts=3
        token='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
        msg.account_keys.extend([base58.b58decode(v) for v in (A,B,C,M,token,'1'*32)]);msg.recent_blockhash=bytes([4])*32
        ix=msg.instructions.add();ix.program_id_index=5;ix.accounts=b'\x00\x01';ix.data=struct.pack('<IQ',2,10000)
        meta=info.meta;meta.fee=5000;meta.pre_balances.extend([100000,0,2039280,1,0,0]);meta.post_balances.extend([85000,10000,2039280,1,0,0]);meta.log_messages.append('Program success')
        for balances,amount in ((meta.pre_token_balances,'10'),(meta.post_token_balances,'11')):
            balance=balances.add();balance.account_index=2;balance.owner=B;balance.mint=M;balance.program_id=token;balance.ui_token_amount.amount=amount;balance.ui_token_amount.decimals=6
        payload=json.dumps(dict(raw_update_b64=base64.b64encode(u.SerializeToString()).decode()))
        c.execute('INSERT INTO wallet_flow_transactions(flow_id,signature,slot,received_at,processed_at,commitment,tx_index,block_time,payload_json) VALUES(?,?,?,?,?,?,?,?,?)',(1,base58.b58encode(info.signature).decode(),100,998.,999.,'confirmed',None,999,payload))
    assert Collector(net,desk,expand=False).consume_flows()['rows']==1
    with sqlite3.connect(net.path) as c:c.execute('UPDATE watch_flow_timing SET graph_commit_after=?',(commit_upper,))
    run(source)
    row,=report(source)['items'];inventory=row['coverage']['inventory']
    if commit_upper is None or commit_upper>clock[0]:
        assert inventory['state']=='unknown' and inventory['items']==[]
        return
    assert inventory['state']=='partial' and inventory['items'][0]['post_atomic']=='11'
    assert inventory['whole_wallet_holdings'] is False
    assert inventory['items'][0]['availability_upper']<=1000.


def test_unrelated_mint_buy_in_create_transaction_does_not_classify_this_launch(source):
    funding(source);launch(source);buy(source,signature=SIG,mint=addr(9));run(source)
    bound,=report(source)['items'][0]['launches']
    assert bound['coverage']['phase']=='unknown'
    assert report(source)['items'][0]['prebuy_state']=='unknown'


@pytest.mark.parametrize('entry_clock',[1000.,1002.])
def test_payload_without_emission_metadata_is_reported_pending_not_empty(source,monkeypatch,entry_clock):
    funding(source);launch(source)
    from sniper.office import wallet_campaign as module
    net,_,clock=source;original=net.clock;committed=[False];connect=module._connect
    def track(*args,**kwargs):
        c=connect(*args,**kwargs)
        if kwargs.get('write'):
            version_written=[False]
            def trace(sql):
                if sql.startswith('INSERT INTO wallet_campaign_versions'):version_written[0]=True
                if sql=='COMMIT' and version_written[0]:committed[0]=True
            c.set_trace_callback(trace)
        return c
    monkeypatch.setattr(module,'_connect',track)
    monkeypatch.setattr(net,'clock',lambda:entry_clock-1 if committed[0] else entry_clock)
    assert run(source)['changed']==1
    monkeypatch.setattr(net,'clock',original)
    pending=report(source)
    assert pending['state']=='partial'
    assert pending['pending_emissions']==dict(state='known',campaigns=1,revisions=1,launches=1,total_markers=3)
    assert pending['denominator']['preparations']==0 and pending['denominator']['finalized_only'] is True
    assert pending['items']==[]
    clock[0]=1002.;run(source)
    recovered=report(source)
    assert recovered['pending_emissions']['total_markers']==0
    assert recovered['items'][0]['first_recorded_at']==1002.
    assert report(source,as_of=1000.)['pending_emissions']['state']=='unknown'
    assert report(source,as_of=1000.)['items']==[]



def test_idle_cursor_updates_spend_no_evidence_quota_but_new_preparation_does(source,monkeypatch):
    from sniper.office import wallet_network as N
    funding(source)
    for _ in range(3):run(source)  # Initialize every cursor that this source uses.
    def charged():
        with sqlite3.connect(source[0].path) as c:return c.execute('SELECT sum(rows_added) FROM daily').fetchone()[0]
    before=charged()
    for _ in range(10):assert run(source)['changed']==0
    assert charged()==before
    monkeypatch.setattr(N,'MAX_ROWS_PER_DAY',before)
    funding(source,who=B)
    assert run(source)['reason']=='daily_row_cap'
    assert charged()==before


def test_clock_floor_survives_pending_payload_retry_and_new_network_instance(source,monkeypatch):
    from sniper.office import wallet_campaign as module
    funding(source);launch(source)
    net,desk,clock=source;connect=module._connect;payload_committed=[False]
    def track(*args,**kwargs):
        c=connect(*args,**kwargs)
        if kwargs.get('write'):
            # The payload commit follows its version insert; earlier clock-only commits do not trigger regression.
            version_written=[False]
            def trace(sql):
                if sql.startswith('INSERT INTO wallet_campaign_versions'):version_written[0]=True
                if sql=='COMMIT' and version_written[0]:payload_committed[0]=True
            c.set_trace_callback(trace)
        return c
    monkeypatch.setattr(module,'_connect',track)
    monkeypatch.setattr(net,'clock',lambda:1001. if payload_committed[0] else 1002.)
    assert run(source)['changed']==1
    restarted=WalletNetwork(net.path,chain_db=net.chain_db,clock=lambda:1001.)
    failed=module.materialize_campaigns(restarted,desk,1001.)
    assert failed['state']=='unknown' and failed['reason']=='campaign_clock_regressed'
    assert module.read_campaigns(restarted)['pending_emissions']['launches']==1
    with sqlite3.connect(net.path) as c:
        assert c.execute("SELECT last_read FROM wallet_campaign_cursor WHERE source='clock'").fetchone()[0]==1002.
        assert c.execute('SELECT emitted_at FROM wallet_campaign_launches').fetchone()[0] is None
    restarted.clock=lambda:1002.
    module.materialize_campaigns(restarted,desk,1002.)
    assert module.read_campaigns(restarted)['items'][0]['launches'][0]['emitted_at']==1002.


def _capture_creation_lookup(source,monkeypatch,unrelated=0):
    from sniper.office import wallet_campaign as module
    net=source[0];c=net._conn()
    for n in range(100,100+unrelated):
        net._insert(c,src=addr(n),dst=addr(n+unrelated+100),edge_type='created_mint',source='import',actor='fixture',observed_at=900.,known_at=910.,proof_kind='import',proof_ref=str(n),proof_state='none',method='seed')
    c.commit();c.close()
    funding(source);launch(source)
    queries=[];connect=module._connect
    def track(*args,**kwargs):
        c=connect(*args,**kwargs)
        if kwargs.get('write'):c.set_trace_callback(lambda sql:queries.append(sql) if sql.startswith('SELECT DISTINCT e.*') else None)
        return c
    monkeypatch.setattr(module,'_connect',track)
    assert run(source)['changed']==1
    return queries[0]


def test_creation_lookup_plan_drives_members_and_binds_wallet_index(source,monkeypatch):
    query=_capture_creation_lookup(source,monkeypatch)
    c=source[0]._conn();plan=[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+query)];c.close()
    lookup=next(item for item in plan if item.startswith('SEARCH e '))
    assert 'edges_src (network_id=? AND src=?)' in lookup
    assert next(i for i,p in enumerate(plan) if p.startswith('SEARCH p '))<next(i for i,p in enumerate(plan) if p.startswith('SEARCH e '))


def test_creation_lookup_vm_work_ignores_unrelated_graph_history(source,monkeypatch):
    query=_capture_creation_lookup(source,monkeypatch,unrelated=5000)
    c=source[0]._conn();steps=[0]
    def budget():steps[0]+=1;return steps[0]>20
    c.set_progress_handler(budget,1000)
    try:
        rows=c.execute(query).fetchall()
        assert len(rows)==1 and rows[0]['dst']==M
    finally:c.close()


def _retrospective(source):
    launch(source)
    with sqlite3.connect(source[0].chain_db) as c:
        fact=json.loads(c.execute("SELECT result FROM fetches WHERE fetch_id='launch-proof'").fetchone()[0])
        fact.update(tool='watch_verified_creation',provenance='research_rpc',commitment='finalized',asked=dict(signature=SIG))
        c.execute("UPDATE fetches SET tool='watch_verified_creation',asked=?,result=? WHERE fetch_id='launch-proof'",(json.dumps(fact['asked']),json.dumps(fact)))
    c=source[0]._conn();c.execute("UPDATE edges SET proof_kind='chain_fetch',method_version='confirmed_pump_create_v1' WHERE type='created_mint'");c.commit();c.close()


def test_finalized_manual_creation_is_retrospective_never_an_early_quote(source):
    funding(source);_retrospective(source);run(source)
    bound,=report(source)['items'][0]['launches']
    assert bound['version']=='wallet-campaign-retrospective-launch-v1'
    assert bound['source_receipt'] is None
    assert bound['coverage']['creation_verified'] is True
    assert bound['coverage']['quote_eligible'] is False and bound['coverage']['retrospective'] is True
    assert bound['coverage']['launch_method']=='confirmed_pump_create_v1'
    assert bound['known_at']==982. and bound['emitted_at']>=1000.


@pytest.mark.parametrize('field,value',[('tool','chain_transaction'),('provenance','seat'),('commitment','processed'),('mint',B)])
def test_manual_creation_requires_exact_validated_profile(source,field,value):
    funding(source);_retrospective(source)
    with sqlite3.connect(source[0].chain_db) as c:
        doc=json.loads(c.execute("SELECT result FROM fetches WHERE fetch_id='launch-proof'").fetchone()[0]);doc[field]=value
        c.execute("UPDATE fetches SET result=? WHERE fetch_id='launch-proof'",(json.dumps(doc),))
    run(source)
    assert report(source)['items'][0]['launches']==[]


@pytest.mark.parametrize('encoding',['legacy','zlib'])
def test_creation_receipt_uses_shared_payload_decoder_and_archive_directory(source,monkeypatch,encoding):
    import base64
    import base58
    import importlib.util
    from pathlib import Path
    from sniper.office import wallet_stream_proof as codec,wallet_campaign as module
    if not hasattr(codec,'decode_payload'):pytest.skip('Run with frozen shared codec successor')
    spec=importlib.util.spec_from_file_location('receipt_fixture',Path(__file__).with_name('test_wallet_flow_consumer.py'))
    fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    update=fixture.wire();update.transaction.slot=101
    signature=base58.b58encode(update.transaction.transaction.signature).decode()
    funding(source);launch(source,signature=signature)
    raw=update.SerializeToString()
    payload=json.dumps(dict(raw_update_b64=base64.b64encode(raw).decode())) if encoding=='legacy' else codec.encode_payload(raw)
    with sqlite3.connect(source[1]/'pumpswap/tape.db') as c:
        c.execute('ALTER TABLE wallet_flow_transactions ADD COLUMN payload_json TEXT')
        c.execute('UPDATE wallet_flow_transactions SET payload_json=?',(payload,))
    calls=[];decoder=codec.decode_payload
    def decode(value,**kwargs):calls.append((value,kwargs));return decoder(value,**kwargs)
    monkeypatch.setattr(codec,'decode_payload',decode)
    monkeypatch.setattr(module,'_seed_buy_from_receipt',lambda tx,mint:'present')
    run(source)
    assert calls==[(payload,dict(archive_dir=(source[1]/'pumpswap/wallet-flow-archive').resolve()))]
    assert report(source)['items'][0]['launches'][0]['coverage']['phase']=='post_atomic_seed'
