"""Real research ChainData and SQLite, no live network."""
import sqlite3
from sniper.office.chain_data import ChainData
from sniper.office.wallet_network import WalletNetwork
from sniper.office import wallet_watch as W
A,B,C='1'*40,'2'*40,'3'*40
S,T='4'*88,'5'*88
NOW=1793536000

def module():
    from sniper.office import wallet_watch_collect
    return wallet_watch_collect

def tx(sig=S,failed=False,balance=True):
    return dict(slot=100,blockTime=NOW+1,transaction=dict(signatures=[sig],message=dict(accountKeys=[dict(pubkey=A,signer=True),dict(pubkey=B,signer=False)],instructions=[dict(program='system',programId='11111111111111111111111111111111',parsed=dict(type='transfer',info=dict(source=A,destination=B,lamports=100)))])),meta=dict(err={} if failed else None,fee=5,preBalances=[1000,0] if balance else None,postBalances=[895,100] if balance else None,preTokenBalances=[],postTokenBalances=[]))

def setup(tmp_path,pages,transactions,listed=(A,)):
    clock=[NOW];calls=[]
    def rpc(url,method,params):
        calls.append((method,params));clock[0]+=2
        if method=='getSignaturesForAddress':return 200,dict(result=pages(params))
        answer=transactions[params[0]]
        return (429,{}) if answer=='quota' else (200,dict(result=answer))
    chain=ChainData(tmp_path/'chain.db',lambda:'https://research.invalid',clock=lambda:clock[0],rpc=rpc)
    net=WalletNetwork(tmp_path/'wallet_network.db',chain_db=chain.path,chain=chain,clock=lambda:clock[0])
    (tmp_path/'pumpswap').mkdir()
    for a in listed:W.add(net,tmp_path,address=a,operator_id=None,evidence='fixture',source='host',actor='crew',now=NOW)
    return net,chain,clock,calls

def rows(net,table):
    with sqlite3.connect(net.path) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('SELECT * FROM '+table)]

def page(p):return [dict(signature=S,slot=100,blockTime=NOW+1,err=None)]

def test_durable_page_detail_restart_and_verified_funding(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()});m=module()
    m.Collector(net,tmp_path).tick(max_calls=1)
    assert len(rows(net,'watch_collect_pending'))==1
    assert rows(net,'watch_collect_state')[0]['cursor'] is None
    m.Collector(net,tmp_path).tick(max_calls=1)
    assert [(r['asset'],r['delta_atomic']) for r in rows(net,'watch_movements')]==[('native:sol','-105')]
    e=rows(net,'edges')[0]
    assert (e['src'],e['dst'],e['proof_state'],e['source'])==(B,A,'verified','host')
    assert B in [r['address'] for r in W.read_list(net)]
    assert rows(net,'watch_collect_state')[0]['cursor']==S

def test_full_pages_hold_cursor_until_exhausted_and_details_committed(tmp_path):
    def pages(p):
        before=p[1].get('before')
        return [] if before==T else [dict(signature=T if before else S,slot=99 if before else 100,blockTime=NOW+1,err=None)]
    older=tx(T);older["slot"]=99
    net,chain,clock,calls=setup(tmp_path,pages,{S:tx(),T:older})
    c=module().Collector(net,tmp_path,page_size=1,expand=False)
    for _ in range(3):c.tick(max_calls=1)
    assert rows(net,'watch_collect_state')[0]['cursor'] is None
    for _ in range(3):c.tick(max_calls=1)
    assert rows(net,'watch_collect_state')[0]['cursor']==S
    assert len(rows(net,'watch_movements'))==2

def test_fairness_and_quota_leave_pending_unknown(tmp_path):
    def pages(p):return [dict(signature=S if p[0]==A else T,slot=100,blockTime=NOW+1,err=None)]
    net,chain,clock,calls=setup(tmp_path,pages,{S:'quota',T:tx(T)},listed=(A,B))
    c=module().Collector(net,tmp_path)
    c.tick(max_calls=1);c.tick(max_calls=1)
    assert [p[0] for method,p in calls if method=='getSignaturesForAddress']==[A,B]
    c.tick(max_calls=1)
    assert len(rows(net,'watch_collect_pending'))==2
    assert any(r['reason']=='provider_rate_limited' for r in rows(net,'watch_collect_state'))
    assert not rows(net,'edges') and not rows(net,'watch_movements')

def test_unknown_balances_are_not_zero_or_funding(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx(balance=False)})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);c.tick(max_calls=1)
    r=rows(net,'watch_movements')[0]
    assert r['delta_atomic'] is None and r['state']=='unknown'
    assert not rows(net,'edges')

def test_positive_token_balance_is_observation_not_transfer(tmp_path):
    t=tx();t['meta']['postTokenBalances']=[dict(owner=A,mint=C,accountIndex=0,uiTokenAmount=dict(amount=str(2**70),decimals=6))]
    net,chain,clock,calls=setup(tmp_path,page,{S:t})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);c.tick(max_calls=1)
    r=next(r for r in rows(net,'watch_movements') if r['asset']==C)
    assert r['delta_atomic']==str(2**70) and r['kind']=='balance_observation' and r['transfer_proven']==0

def test_infrastructure_stops_expansion_and_disk_cap_stops_reads(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()})
    with net._conn() as conn:conn.execute('INSERT INTO labels VALUES(?,?,?,?,?,?)',('solana',A,'infrastructure','host','crew',NOW))
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);c.tick(max_calls=1)
    assert [r['address'] for r in W.read_list(net)]==[A]
    net.max_bytes=0;before=len(calls)
    assert c.tick(max_calls=1)['reason']=='disk_cap' and len(calls)==before

def test_known_clock_is_after_detail_response_and_failed_tx_is_explicit(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx(failed=True)})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);c.tick(max_calls=1)
    r=rows(net,'watch_movements')[0]
    assert r['kind']=='failed_transaction' and r['reason']=='transaction_failed'
    assert r['delta_atomic'] is None and r['known_at']==clock[0]
    assert not rows(net,'edges')

def test_retry_unknown_detail_does_not_starve_other_pending_receipts(tmp_path):
    def pages(p):return [dict(signature=s,slot=100,blockTime=NOW+1,err=None) for s in (S,T)]
    net,chain,clock,calls=setup(tmp_path,pages,{S:None,T:tx(T)})
    c=module().Collector(net,tmp_path,expand=False)
    for _ in range(4):c.tick(max_calls=1)
    assert any(r['signature']==T for r in rows(net,'watch_movements'))
    assert rows(net,'watch_collect_pending')[0]['signature']==S
    assert rows(net,'watch_collect_state')[0]['cursor'] is None

def test_daily_credit_cap_prevents_provider_call(tmp_path,monkeypatch):
    monkeypatch.setattr(module(),'CREDITS_PER_DAY',33333)
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()})
    with net._conn() as conn:net._count(conn,NOW,credits=33300)
    assert module().Collector(net,tmp_path).tick(max_calls=1)['reason']=='credit_cap'
    assert not calls

def test_cursor_missing_is_gap_not_successful_advance(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()})
    c=module().Collector(net,tmp_path,expand=False)
    with c._open() as conn:conn.execute('INSERT INTO watch_collect_state(address,cursor) VALUES(?,?)',(A,T))
    c.tick(max_calls=1);c.tick(max_calls=1)
    s=rows(net,'watch_collect_state')[0]
    assert s['cursor']==T and s['reason']=='cursor_not_found'

def test_setup_cursor_is_durable_sponsor_evidence_not_funding(tmp_path):
    net,chain,clock,calls=setup(tmp_path,lambda p:[],{})
    tape=tmp_path/'pumpswap/tape.db'
    with sqlite3.connect(tape) as c:
        c.execute('CREATE TABLE wallet_setup_events(setup_id INTEGER PRIMARY KEY AUTOINCREMENT,signature TEXT,outer_ix INTEGER,program TEXT,payer TEXT,wallet TEXT,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL)')
        c.execute('INSERT INTO wallet_setup_events VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',(S,0,'pump',A,B,100,None,NOW+1,NOW-1,NOW))
    collector=module().Collector(net,tmp_path)
    with net._conn() as c:net._count(c,NOW,credits=33333)
    result=collector.consume_setups(limit=1)
    assert result['added']==1 and not calls
    assert not rows(net,'edges')
    assert next(r for r in W.read_list(net) if r['address']==B)['evidence'].startswith('setup_sponsor:')
    assert module().Collector(net,tmp_path).consume_setups(limit=1)['added']==0
    assert next(r for r in rows(net,'watch_collect_sources') if r['source']=='setup')['cursor']==1

def test_setup_reader_absent_is_unknown_and_never_creates_tape(tmp_path):
    net,chain,clock,calls=setup(tmp_path,lambda p:[],{})
    result=module().Collector(net,tmp_path).consume_setups()
    assert result['state']=='unknown' and not (tmp_path/'pumpswap/tape.db').exists()

def test_cli_disabled_never_reads_credentials_or_creates_state(tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('collect_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--state-root',str(tmp_path/'absent')])==0
    assert not (tmp_path/'absent').exists()

def test_shared_pace_preflight_does_not_burn_credit_or_retry(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()})
    original=chain.rpc
    def rpc(*args):
        result=original(*args);clock[0]-=2;return result
    chain.rpc=rpc
    result=module().Collector(net,tmp_path).tick(max_calls=10)
    assert len(calls)==1 and result['calls_attempted']==1
    assert rows(net,'daily')[0]['credits']==100

def test_unknown_balance_remains_partial_after_cursor_advances(tmp_path):
    net,chain,clock,calls=setup(tmp_path,page,{S:tx(balance=False)})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);result=c.tick(max_calls=1)
    s=rows(net,'watch_collect_state')[0]
    assert s['cursor']==S and s['state']=='partial' and s['reason']=='balance_coverage_partial'
    assert result['wallets'][0]['scan_age_s']==2

def test_cached_detail_and_local_setup_work_after_credit_cap(tmp_path,monkeypatch):
    monkeypatch.setattr(module(),'CREDITS_PER_DAY',33333)
    net,chain,clock,calls=setup(tmp_path,page,{S:tx()})
    c=module().Collector(net,tmp_path,expand=False);c.tick(max_calls=1)
    assert chain.transaction(S)['state']=='known'
    with net._conn() as conn:net._count(conn,clock[0],credits=33300)
    before=len(calls)
    result=c.tick(max_calls=1)
    assert len(rows(net,'watch_movements'))==1 and len(calls)==before
    assert result['credits_remaining_estimate_today']==0 and result['wallets'][0]['cursor']==S

def test_initial_lifetime_history_stops_at_forward_floor(tmp_path):
    def old_page(p):return [dict(signature=S,slot=100,blockTime=NOW-1000,err=None)]
    net,chain,clock,calls=setup(tmp_path,old_page,{S:tx()})
    c=module().Collector(net,tmp_path,page_size=1)
    result=c.tick(max_calls=1)
    assert not rows(net,'watch_collect_pending')
    assert result['wallets'][0]['forward_floor']==NOW
    assert result['wallets'][0]['history_before_floor']=='unknown'

def test_malformed_token_amount_and_duplicate_account_index_are_unknown():
    t=tx();t['meta']['postTokenBalances']=[dict(owner=A,mint=C,accountIndex=0,uiTokenAmount='bad')]
    result=module().movements(t,A)
    assert any(r['state']=='unknown' for r in result)
    row=dict(owner=A,mint=C,accountIndex=0,uiTokenAmount=dict(amount='100',decimals=6))
    t['meta']['postTokenBalances']=[row,row]
    result=module().movements(t,A)
    assert next(r for r in result if r['asset']==C)['delta_atomic'] is None

def test_stale_provider_head_cannot_regress_cursor(tmp_path):
    def old_page(p):return [dict(signature=T,slot=99,blockTime=NOW-1000,err=None)]
    net,chain,clock,calls=setup(tmp_path,old_page,{})
    c=module().Collector(net,tmp_path)
    with c._open() as conn:conn.execute('INSERT INTO watch_collect_state(address,cursor,cursor_slot,forward_floor) VALUES(?,?,?,?)',(A,S,100,NOW))
    c.tick(max_calls=1)
    r=rows(net,'watch_collect_state')[0]
    assert r['cursor']==S and r['reason']=='cursor_regression'

def test_cached_history_scan_age_is_not_relabelled_fresh(tmp_path):
    net,chain,clock,calls=setup(tmp_path,lambda p:[],{})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1)
    clock[0]+=20
    result=module().Collector(net,tmp_path).tick(max_calls=1)
    assert result['wallets'][0]['scan_age_s']==20
    assert result['cached_operations']==1
    assert result['wallets'][0]['forward_floor']==NOW

def test_malformed_transaction_envelope_is_typed_unknown_not_crash(tmp_path):
    broken=tx();broken['transaction']=['wrong']
    net,chain,clock,calls=setup(tmp_path,page,{S:broken})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);result=c.tick(max_calls=1)
    assert result['wallets'][0]['reason']=='transaction_identity_unknown'
    assert len(rows(net,'watch_collect_pending'))==1

def test_forward_floor_keeps_whole_activation_second(tmp_path):
    def current_page(p):return [dict(signature=S,slot=100,blockTime=NOW,err=None)]
    t=tx();t['blockTime']=NOW
    net,chain,clock,calls=setup(tmp_path,current_page,{S:t})
    clock[0]=NOW+0.8
    c=module().Collector(net,tmp_path);c.tick(max_calls=1)
    assert len(rows(net,'watch_collect_pending'))==1
    assert rows(net,'watch_collect_state')[0]['forward_floor']==NOW

def test_first_uncached_wallet_at_credit_cap_does_not_starve_cached_second(tmp_path,monkeypatch):
    monkeypatch.setattr(module(),'CREDITS_PER_DAY',33333)
    net,chain,clock,calls=setup(tmp_path,lambda p:[],{S:tx()},listed=(A,B))
    assert chain.transaction(S)['state']=='known'
    c=module().Collector(net,tmp_path,expand=False)
    with c._open() as conn:
        conn.execute('INSERT INTO watch_collect_state(address,forward_floor) VALUES(?,?)',(A,NOW))
        conn.execute('INSERT INTO watch_collect_state(address,forward_floor) VALUES(?,?)',(B,NOW))
        conn.execute('INSERT INTO watch_collect_pending(address,signature,slot,block_time) VALUES(?,?,?,?)',(B,S,100,NOW+1))
        net._count(conn,clock[0],credits=33333)
    before=len(calls)
    result=c.tick(max_calls=1)
    assert len(calls)==before and result['cached_operations']==1
    assert not rows(net,'watch_collect_pending')
    assert any(r['address']==B for r in rows(net,'watch_movements'))

def test_unknown_history_clock_resolved_before_floor_cannot_write_or_expand(tmp_path):
    def unknown_page(p):return [dict(signature=S,slot=100,blockTime=None,err=None)]
    old=tx();old['blockTime']=NOW-1000
    net,chain,clock,calls=setup(tmp_path,unknown_page,{S:old})
    c=module().Collector(net,tmp_path);c.tick(max_calls=1);result=c.tick(max_calls=1)
    assert not rows(net,'watch_movements') and not rows(net,'edges')
    assert not rows(net,'watch_collect_pending')
    assert [r['address'] for r in W.read_list(net)]==[A]
    assert result['wallets'][0]['history_before_floor']=='unknown'


def test_expansion_reuses_existing_funder_hub_threshold(tmp_path):
    from sniper.services.memecoin_desk_universe import b58encode
    net, chain, clock, calls = setup(tmp_path, page, {S: tx()})
    collector = module().Collector(net, tmp_path)
    with net._conn() as c:
        for i in range(20):
            c.execute('INSERT INTO edges(network_id,src,dst,type,source,known_at,proof_state,method_version) VALUES(?,?,?,?,?,?,?,?)',
                      ('solana', b58encode(bytes([i + 8]) * 32), A, 'funded_by', 'host', NOW, 'verified', 'test'))
    collector.tick(max_calls=1); collector.tick(max_calls=1)
    assert B in {r['address'] for r in W.read_list(net)}, 'twenty recipients are below the existing hub threshold'


def test_existing_hub_threshold_uses_bounded_funding_campaign(tmp_path):
    from sniper.services.memecoin_desk_universe import b58encode
    net, chain, clock, calls = setup(tmp_path, page, {S: tx()})
    collector = module().Collector(net, tmp_path)
    with net._conn() as c:
        for i in range(W.FANOUT_HUB):
            c.execute('INSERT INTO edges(network_id,src,dst,type,source,known_at,proof_state,method_version) VALUES(?,?,?,?,?,?,?,?)',
                      ('solana', b58encode(bytes([i + 8]) * 32), A, 'funded_by', 'host', NOW, 'verified', 'test'))
    collector.tick(max_calls=1); collector.tick(max_calls=1)
    child=next(r for r in W.read_list(net) if r['address']==B)
    assert child['operator_id']==f'hub60:{A}:{int((NOW+1)//60)*60}'


def test_expansion_reuses_known_infrastructure_list(tmp_path, monkeypatch):
    net, chain, clock, calls = setup(tmp_path, page, {S: tx()})
    monkeypatch.setattr(W, '_known_infrastructure', lambda: frozenset({A}))
    collector = module().Collector(net, tmp_path)
    collector.tick(max_calls=1); collector.tick(max_calls=1)
    assert B not in {r['address'] for r in W.read_list(net)}


def test_unknown_infrastructure_classification_is_not_a_clean_expansion(tmp_path, monkeypatch):
    net, chain, clock, calls = setup(tmp_path, page, {S: tx()})
    monkeypatch.setattr(W, '_known_infrastructure', lambda: None)
    collector = module().Collector(net, tmp_path)
    collector.tick(max_calls=1); collector.tick(max_calls=1)
    assert B not in {r['address'] for r in W.read_list(net)}
    assert rows(net, 'watch_collect_state')[0]['reason'] == 'expansion_infrastructure_unknown'
    assert rows(net, 'edges')[0]['proof_state'] == 'verified'
