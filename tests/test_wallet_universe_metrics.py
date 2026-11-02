import json
import pytest
from sniper.services.wallet_universe_flow import METHOD as FLOW_METHOD


def prices():
    return {s: dict(usd=p, observed_at='2026-10-04T16:00:00+00:00', source='fixture')
            for s,p in [('SOL',150),('ETH',3000),('BNB',600)]}


def test_arc_native_quote_is_counted_once():
    from sniper.services.wallet_universe_metrics import observe_capital
    calls=[]
    def call(method,params):
        calls.append((method,params))
        return {'eth_chainId':hex(5042),'eth_blockNumber':hex(100),
                'eth_getBlockByNumber':dict(number=hex(98),hash='0x'+'a'*64,timestamp=hex(1791129600)),
                'eth_getBalance':hex(12*10**18)}[method]
    row=observe_capital('5042','0x'+'1'*40,call,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['state']=='ok' and row['usd']==12
    assert len(row['components'])==1 and not any(m=='eth_call' for m,p in calls)
    assert row['components'][0]['atomic']==str(12*10**18)


def test_provider_failure_is_unknown_and_price_nan_is_invalid():
    from sniper.services.wallet_universe_metrics import observe_capital
    def fail(*args): raise ValueError('provider_timeout')
    row=observe_capital('56','0x'+'1'*40,fail,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['usd'] is None and row['state']=='provider_timeout'
    bad=prices();bad['SOL']['usd']=float('nan')
    from sniper.services.wallet_universe_metrics import price_for
    assert price_for('SOL',bad,now='2026-10-04T16:00:00+00:00') is None


def test_wrong_chain_rejected_before_balances():
    from sniper.services.wallet_universe_metrics import observe_capital
    calls=[]
    def wrong(method,params): calls.append(method);return hex(8453)
    row=observe_capital('56','0x'+'1'*40,wrong,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['state']=='wrong_chain' and row['usd'] is None and calls==['eth_chainId']


def test_solana_zero_and_wrong_owner_are_distinct():
    from sniper.services.wallet_universe_metrics import observe_capital
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    def call(method,params):
        if method=='getGenesisHash':return SOLANA_MAINNET_GENESIS
        if method=='getSlot':return 100
        if method=='getBlockTime':return 1791129600
        return dict(context=dict(slot=100),value=0 if method=='getBalance' else [])
    row=observe_capital('solana','Owner',call,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['state']=='ok' and row['usd']==0
    def wrong(method,params):
        if method in ('getGenesisHash','getBalance','getSlot','getBlockTime'):return call(method,params)
        return dict(context=dict(slot=100),value=[dict(account=dict(data=dict(parsed=dict(info=dict(
            owner='SomeoneElse',mint=params[1]['mint'],tokenAmount=dict(amount='10',decimals=6))))))])
    row=observe_capital('solana','Owner',wrong,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['state']=='partial' and row['usd'] is None


def test_projection_invalidates_removed_proof_stale_values_and_pending_denominator():
    from sniper.services.wallet_universe_metrics import project_metrics
    owners={('56','0x'+'1'*40):('A','Alpha')}
    snapshot=dict(version=1,generated_at='2026-10-04T16:00:00+00:00',
        capital=[dict(chain='56',address='0x'+'2'*40,uid='A',state='ok',usd=100,
                      observed_at='2026-10-04T16:00:00+00:00')],impact=[])
    out=project_metrics(snapshot,owners,chain='56',now='2026-10-04T16:00:00+00:00')
    assert out['wallets']['A']['capital']['usd'] is None
    snapshot['capital'][0]['address']='0x'+'1'*40
    assert project_metrics(snapshot,owners,chain='56',now='2026-10-04T19:00:00+00:00')['wallets']['A']['capital']['state']=='stale'
    snapshot['impact']=[dict(chain='56',uid='A',address='0x'+'1'*40,eligible=20,complete=10,
        priced=10,median_usd=5,observed_at='2026-10-04T16:00:00+00:00',method=FLOW_METHOD,valuation='post_buy_net_quote_flow')]
    row=project_metrics(snapshot,owners,chain='56',now='2026-10-04T16:00:00+00:00')['wallets']['A']['impact']
    assert row['state']=='incomplete' and row['value_usd'] is None and row['complete']==10


def test_isolated_rpc_attempt_budget_counts_failed_calls_and_never_retries():
    from sniper.services.wallet_universe_metrics import ReadRpc
    calls=[]
    def post(*args,**kwargs): calls.append(args);raise TimeoutError()
    rpc=ReadRpc('https://research.invalid/',limit=1,post=post,pace=lambda:None)
    with pytest.raises(ValueError,match='provider_timeout'):rpc('eth_chainId',[])
    with pytest.raises(ValueError,match='budget'):rpc('eth_chainId',[])
    assert len(calls)==1 and rpc.used==1
    with pytest.raises(ValueError,match='read_method'):rpc('eth_sendRawTransaction',['secret'])


def test_explicit_observer_endpoint_and_credential_isolation():
    from scripts.research.wallet_universe_metrics import endpoints
    from sniper.services.wallet_universe_metrics import isolated_url
    assert endpoints({'WALLET_UNIVERSE_RPC_URL_56':'https://research.invalid'})=={'56':'https://research.invalid'}
    with pytest.raises(ValueError,match='overlap'):
        isolated_url('https://other.invalid/?api-key=SameSecret',['https://funded.invalid/?key=SameSecret'])


def test_owned_store_rejects_funded_and_foreign_ledgers(tmp_path):
    from sniper.services.wallet_universe_metrics import open_metrics
    import sqlite3
    p=tmp_path/'main.db'
    with sqlite3.connect(p) as c:c.execute('CREATE TABLE money(secret TEXT)')
    with pytest.raises(ValueError,match='funded'):open_metrics(p,source=p)
    with pytest.raises(ValueError,match='foreign'):open_metrics(p)


def test_path_key_cannot_share_custody_capacity():
    from sniper.services.wallet_universe_metrics import isolated_url
    with pytest.raises(ValueError,match='overlap'):
        isolated_url('https://eth-mainnet.g.alchemy.com/v2/samekey',
                     ['https://base-mainnet.g.alchemy.com/v2/samekey'])


def test_component_throttle_stops_instead_of_spending_more_calls():
    from sniper.services.wallet_universe_metrics import observe_capital
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    calls=[]
    def call(method,params):
        calls.append(method)
        if method=='getGenesisHash':return SOLANA_MAINNET_GENESIS
        if method=='getBalance':return dict(context=dict(slot=100),value=1)
        if method=='getSlot':return 100
        raise ValueError('rate_limit')
    row=observe_capital('solana','Owner',call,prices(),now='2026-10-04T16:00:00+00:00')
    assert row['state']=='rate_limit' and calls.count('getTokenAccountsByOwner')==1


def test_unknown_quote_symbol_does_not_grant_a_price():
    from scripts.research.wallet_universe_metrics import quote_usd
    q=dict(asset='0x'+'9'*40,symbol='USDC',delta_atomic='100000000',net_after_atomic='100000000',decimals=6)
    assert quote_usd('56',q,prices(),'2026-10-04T16:00:00+00:00') is None


def test_capped_census_cannot_be_complete_coverage():
    from sniper.services.wallet_universe_metrics import project_metrics
    addr='0x'+'1'*40;owners={('56',addr):('A','a')}
    body=dict(generated_at='2026-10-04T16:00:00Z',capital=[],impact=[dict(chain='56',uid='A',address=addr,
        eligible=10,complete=10,priced=10,median_usd=50,method=FLOW_METHOD,valuation='post_buy_net_quote_flow')],collection=dict(source=dict(source_cap_reached=True)))
    row=project_metrics(body,owners,chain='56',now='2026-10-04T16:00:00Z')['wallets']['A']['impact']
    assert row['state']=='source_census_incomplete' and row['value_usd'] is None


def test_proof_expansion_invalidates_flow_exclusions():
    from sniper.services.wallet_universe_metrics import project_metrics
    a,b='0x'+'1'*40,'0x'+'2'*40
    body=dict(generated_at='2026-10-04T16:00:00Z',capital=[],impact=[dict(chain='56',uid='A',address=a,
        proof_addresses=[a],eligible=10,complete=10,priced=10,median_usd=50,method=FLOW_METHOD,valuation='post_buy_net_quote_flow')],collection={})
    row=project_metrics(body,{('56',a):('A','a'),('56',b):('A','a')},chain='56',now='2026-10-04T16:00:00Z')['wallets']['A']['impact']
    assert row['state']=='proof_changed' and row['value_usd'] is None


def test_ethereum_native_is_eth_even_when_pool_quote_is_usdc():
    from sniper.services.wallet_universe_metrics import native_symbol
    assert native_symbol('1')=='ETH'


def test_redirect_is_not_followed_or_counted_as_a_success():
    from sniper.services.wallet_universe_metrics import ReadRpc
    calls=[]
    class Reply:
        status_code=302
        def json(self):raise AssertionError('must not decode a redirect')
    def post(*args,**kwargs):calls.append(kwargs);return Reply()
    rpc=ReadRpc('https://research.invalid',post=post)
    with pytest.raises(ValueError,match='provider_redirect'):rpc('eth_chainId',[])
    assert calls[0]['allow_redirects'] is False and rpc.used==1


def test_old_provider_block_is_not_a_fresh_balance():
    from sniper.services.wallet_universe_metrics import observe_capital
    def call(method,params):
        return dict(eth_chainId=hex(5042),eth_blockNumber=hex(100),
            eth_getBlockByNumber=dict(number=hex(98),hash='0x'+'a'*64,timestamp=hex(1791128600)))[method]
    row=observe_capital('5042','0x'+'1'*40,call,prices(),now='2026-10-04T16:00:00Z')
    assert row['state']=='stale_provider_state' and row['usd'] is None


def test_bad_flow_counts_or_value_cannot_size_a_planet():
    from sniper.services.wallet_universe_metrics import project_metrics
    a='0x'+'1'*40;owner={('56',a):('A','a')}
    for counts in [dict(eligible=10,complete=11,priced=11,median_usd=50),
                   dict(eligible=10,complete=10,priced=10,median_usd=float('nan')),
                   dict(eligible=10,complete=10,priced=10,median_usd='50')]:
        b=dict(generated_at='2026-10-04T16:00:00Z',capital=[],impact=[dict(chain='56',uid='A',address=a,
            method=FLOW_METHOD,valuation='post_buy_net_quote_flow',**counts)])
        row=project_metrics(b,owner,chain='56',now='2026-10-04T16:00:00Z')['wallets']['A']['impact']
        assert row['state']=='malformed' and row['value_usd'] is None


def test_detection_key_is_not_a_transaction_hash_and_must_match_chain_and_coin():
    from scripts.research.wallet_universe_metrics import source_signature
    tx='0x'+'a'*64;mint='0x'+'b'*40
    assert source_signature('56',mint,'56:'+tx+':'+mint)==tx
    assert source_signature('56',mint,tx.upper().replace('0X','0x'))==tx
    assert source_signature('56',mint,'8453:'+tx+':'+mint) is None
    assert source_signature('56',mint,'56:'+tx+':'+'0x'+'c'*40) is None
    assert source_signature('solana','Mint','CaseSensitiveSignature')=='CaseSensitiveSignature'


def test_flow_queue_resumes_a_wallet_before_starting_its_new_window_and_rotates_wallets():
    from scripts.research.wallet_universe_metrics import ordered_pending
    from collections import Counter
    def row(uid,t0,partial=False):return (dict(uid=uid,t0=t0),dict(status='partial',state={}) if partial else {},'old' if partial else '')
    a_old,a_new,b_old=row('A',10,True),row('A',30),row('B',20)
    queue=ordered_pending([a_new,a_old,b_old],Counter())
    assert queue[0]==a_old and queue[1]==b_old and queue[2]==a_new


def test_endpoint_selection_preserves_pinned_import_path():
    import sys
    from scripts.research.wallet_universe_metrics import endpoints
    before=list(sys.path)
    try:
        endpoints({})
        assert sys.path==before
    finally:sys.path[:]=before


def test_arc_quote_can_be_priced_without_double_counting_capital():
    from scripts.research.wallet_universe_metrics import quote_usd
    from sniper.execution.evm_wallet import CHAIN_USDC
    quote=dict(asset=CHAIN_USDC['5042'],decimals=6,net_after_atomic='123000000')
    assert quote_usd('5042',quote,{},'2026-10-04T16:00:00Z')==123


def test_stuck_solana_provider_cannot_refresh_old_balances():
    from sniper.services.wallet_universe_metrics import observe_capital
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    def call(method,params):
        if method=='getGenesisHash':return SOLANA_MAINNET_GENESIS
        if method=='getSlot':return 100
        if method=='getBlockTime':return 1791128600
        return dict(context=dict(slot=100),value=0 if method=='getBalance' else [])
    row=observe_capital('solana','Owner',call,prices(),now='2026-10-04T16:00:00Z')
    assert row['state']=='stale_provider_state' and row['usd'] is None


def test_completed_cache_must_match_method_identity_and_own_exclusions():
    from copy import deepcopy
    from scripts.research.wallet_universe_metrics import current_result
    from sniper.services.wallet_universe_flow import METHOD
    unit=dict(chain='solana',uid='A',address='Addr',mint='Mint',signature='Sig',t0=100,
              proof_addresses=['Addr'],own_signatures=['OurSig'])
    binding=dict(identity={k:unit[k] for k in ('chain','uid','address','mint','signature','t0')},exclusions=['Addr'],own=['OurSig'])
    result=dict(method=METHOD,state=dict(version=METHOD,binding=binding),status='complete')
    assert current_result(unit,result)
    bad=deepcopy(result);bad['method']='flow-v1'
    assert not current_result(unit,bad)
    newer=deepcopy(unit);newer['own_signatures'].append('LateOwnFill')
    assert not current_result(newer,result)


def test_boolean_price_is_malformed():
    from sniper.services.wallet_universe_metrics import price_for
    p=prices();p['ETH']['usd']=True
    assert price_for('ETH',p,now='2026-10-04T16:00:00Z') is None


def test_web_projection_cannot_relabel_an_old_flow_method():
    from sniper.services.wallet_universe_metrics import project_metrics
    addr='0x'+'1'*40;owners={('56',addr):('A','a')}
    body=dict(generated_at='2026-10-04T16:00:00Z',capital=[],impact=[dict(chain='56',uid='A',address=addr,
        eligible=10,complete=10,priced=10,median_usd=50,method='wallet-universe-flow-v1')])
    row=project_metrics(body,owners,chain='56',now='2026-10-04T16:00:00Z')['wallets']['A']['impact']
    assert row['state']=='method_changed' and row['value_usd'] is None


def test_balance_read_can_cross_blocks_without_becoming_a_future_snapshot():
    from sniper.services.wallet_universe_metrics import observe_capital
    def call(method,params):
        return dict(eth_chainId=hex(5042),eth_blockNumber=hex(100),eth_getBalance=hex(12*10**18),
            eth_getBlockByNumber=dict(number=hex(98),hash='0x'+'a'*64,timestamp=hex(1791129608)))[method]
    row=observe_capital('5042','0x'+'1'*40,call,prices(),now='2026-10-04T16:00:00Z',clock=lambda:'2026-10-04T16:00:10Z')
    assert row['state']=='ok' and row['observed_at']=='2026-10-04T16:00:10Z'


def test_capital_cannot_stamp_a_stale_anchor_fresh_after_slow_balances():
    from sniper.services.wallet_universe_metrics import observe_capital
    current=['2026-10-04T16:00:00Z']
    def call(method,params):
        if method=='eth_getBalance':current[0]='2026-10-04T16:11:00Z'
        return dict(eth_chainId=hex(5042),eth_blockNumber=hex(100),eth_getBalance=hex(12*10**18),
            eth_getBlockByNumber=dict(number=hex(98),hash='0x'+'a'*64,timestamp=hex(1791129600)))[method]
    row=observe_capital('5042','0x'+'1'*40,call,prices(),now=current[0],clock=lambda:current[0])
    assert row['state']=='stale_provider_state' and row['usd'] is None
    assert row['observed_at']=='2026-10-04T16:11:00Z'


def test_collector_refreshes_many_addresses_with_fresh_balances_under_attempt_cap(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    at='2026-10-04T16:00:00Z'
    owners={('5042','0x'+format(i,'040x')):(str(i),'Wallet') for i in range(1,21)}
    monkeypatch.setattr(worker,'inputs',lambda root:(owners,[],[],dict(window_from=at,census_valid=True)))
    monkeypatch.setattr(worker,'prices',lambda:{})
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(worker,'endpoints',lambda env:{'5042':'https://research.invalid'})
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    monkeypatch.setattr(worker,'paced',lambda *args:(lambda:None,None))
    monkeypatch.setattr(worker,'publish',lambda *args:None)
    balances=[]
    class Rpc:
        def __init__(self,url,limit,pace):self.used=0;self.limit=limit
        def __call__(self,method,params):
            if self.used>=self.limit:raise ValueError('budget')
            self.used+=1
            if method=='eth_getBalance':balances.append(params[0]);return hex(int(params[0],16)*10**18)
            return dict(eth_chainId=hex(5042),eth_blockNumber=hex(100),eth_getBlockByNumber=dict(
                number=hex(98),hash='0x'+'a'*64,timestamp=hex(1791129600)))[method]
        def close(self):pass
    monkeypatch.setattr(worker.m,'ReadRpc',Rpc)
    report=worker.run_once(tmp_path,max_calls=30,capital_only=True)
    assert report['chains']['5042']['capital_reads']>=8
    assert len(balances)==len(set(balances))>=8 and report['chains']['5042']['calls']<=30
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:
        for row in db.execute('SELECT snapshot FROM capital'):
            value=json.loads(row[0]);assert value['state']=='ok' and value['usd']==int(value['address'],16)
    finally:db.close()


def test_independent_chains_refresh_together_with_private_readers_and_root_only_publish(tmp_path,monkeypatch):
    """A slow research chain must not serialize every other capital refresh."""
    import threading
    from scripts.research import wallet_universe_metrics as worker
    at='2026-10-04T16:00:00Z';root_thread=threading.get_ident()
    owners={(chain,'0x'+'1'*40):(chain,'Wallet') for chain in ('5042','56')}
    monkeypatch.setattr(worker,'inputs',lambda root:(owners,[],[],dict(window_from=at,census_valid=True)))
    monkeypatch.setattr(worker,'prices',prices)
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(worker,'endpoints',lambda env:{c:'https://research.invalid/'+c for c in ('5042','56')})
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    monkeypatch.setattr(worker,'paced',lambda *args:(lambda:None,None))
    monkeypatch.setattr(worker.m,'assets_for',lambda chain:[])
    barrier=threading.Barrier(2);readers=[];published=[]
    monkeypatch.setattr(worker,'publish',lambda *args:published.append(threading.get_ident()))
    class Rpc:
        def __init__(self,chain,limit):
            self.chain=chain;self.limit=limit;self.used=0;self.thread=threading.get_ident();self.closed=False
            readers.append(self)
        def __call__(self,method,params):
            assert threading.get_ident()==self.thread
            if self.used==0:barrier.wait(timeout=1)
            if self.used>=self.limit:raise ValueError('budget')
            self.used+=1
            height=88 if self.chain=='56' else 98
            return dict(eth_chainId=hex(int(self.chain)),eth_blockNumber=hex(100),
                eth_getBalance=hex(int(self.chain)*10**18),eth_getBlockByNumber=dict(
                    number=hex(height),hash='0x'+'a'*64,timestamp=hex(1791129600)))[method]
        def close(self):
            assert threading.get_ident()==self.thread
            self.closed=True
    monkeypatch.setattr(worker,'research_reader',lambda root,chain,url,limit:Rpc(chain,limit))
    report=worker.run_once(tmp_path,max_calls=20,capital_only=True)
    assert all(s['state']=='ok' and s['capital_reads']==1 and s['calls']<=20 for s in report['chains'].values())
    assert len({r.thread for r in readers})==2 and all(r.closed for r in readers)
    assert published and set(published)=={root_thread}
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:
        stored={r['chain']:json.loads(r['snapshot']) for r in db.execute('SELECT chain,snapshot FROM capital')}
        assert set(stored)=={'5042','56'}
        assert all(r['usd']==int(chain)*(600 if chain=='56' else 1) and r['state']=='ok'
                   for chain,r in stored.items())
    finally:db.close()


def test_concurrent_chain_urls_still_acquire_one_shared_account_budget(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services import memecoin_evm_exact_capture
    from sniper.services.memecoin_research_pacer import SharedPacer
    monkeypatch.setenv('MEMECOIN_RESEARCH_PACER_DIR',str(tmp_path))
    monkeypatch.setattr(memecoin_evm_exact_capture,'budget',lambda url:'drpc.org (keyed)')
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    acquired=[]
    monkeypatch.setattr(SharedPacer,'acquire',lambda self:acquired.append((self.path,self.gap_s)))
    a,pa=worker.paced(tmp_path,'56','https://one.research.invalid')
    b,pb=worker.paced(tmp_path,'8453','https://two.research.invalid')
    a();b()
    assert pa.path==pb.path and acquired==[(pa.path,1/1.5)]*2
    monkeypatch.setattr(worker,'flat',lambda root,chain:False)
    with pytest.raises(ValueError,match='funded_book_open'):a()
    assert len(acquired)==2
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    pa.pause(60)
    with pytest.raises(ValueError,match='rate_limit_pause'):b()
    assert len(acquired)==2


def test_published_capital_and_flow_share_one_read_snapshot(tmp_path,monkeypatch):
    import sqlite3
    from scripts.research import wallet_universe_metrics as worker
    from scripts.research import wallet_follow_funnel
    at='2026-10-04T16:00:00Z';chain='5042';address='0x'+'1'*40
    unit=dict(chain=chain,uid='A',address=address,mint='0x'+'2'*40,signature='0x'+'a'*64,t0=1000,
              proof_addresses=[address],own_signatures=[])
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    snap=dict(chain=chain,address=address,uid='A',state='ok',usd=1,observed_at=at)
    with db:
        db.execute('INSERT INTO capital VALUES (?,?,?,?,?)',(chain,address,'A',json.dumps(snap),at))
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',(worker.unit_key(unit),json.dumps(unit),json.dumps(dict(status='pending')),at))
    class View:
        def __getattr__(self,name):return getattr(db,name)
        def execute(self,sql,*args):
            cur=db.execute(sql,*args)
            if sql=='SELECT snapshot FROM capital':
                rows=cur.fetchall()
                with sqlite3.connect(tmp_path/'data/wallet_universe_metrics.db') as writer:
                    writer.execute('UPDATE capital SET snapshot=?',(json.dumps({**snap,'usd':2}),))
                    writer.execute('UPDATE flow_units SET result=?',(json.dumps(dict(status='censored',detail='new')),))
                class Rows:
                    def __iter__(self):return iter(rows)
                    def fetchall(self):return rows
                return Rows()
            return cur
    output=[]
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(wallet_follow_funnel,'publish_universe',lambda path,body:output.append(body))
    try:
        worker.publish(tmp_path,View(),[unit],{(chain,address):('A','Wallet')},{},dict(state='ok'))
        assert output[0]['capital'][0]['usd']==1
        assert output[0]['impact'][0]['states']=={'pending':1}
        assert not db.in_transaction
    finally:db.close()


def test_census_normalizes_evm_coin_identity_before_first_buy_dedup(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services import wallet_observation
    at='2026-10-04T16:00:00Z';a='0x'+'1'*40;mint='0x'+'ab'*20
    class Cursor:
        def __init__(self,rows):self.rows=rows
        def __iter__(self):return iter(self.rows)
        def fetchall(self):return self.rows
    class Ledger:
        def execute(self,sql,args=()):
            if 'memecoin_wallets' in sql:return Cursor([('A',)])
            if 'memecoin_fills' in sql:return Cursor([])
            return Cursor([('0x'+'a'*64,'A',a,'56',mint,1791129600,at,1,None),
                           ('0x'+'b'*64,'A',a,'56','0x'+mint[2:].upper(),1791129601,at,2,None)])
        def close(self):pass
    monkeypatch.setattr(worker,'get_readonly_connection',lambda path:Ledger())
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(wallet_observation,'observation_owners',lambda c,u:{('56',a):('A','Alpha')})
    owners,units,own,source=worker.inputs(tmp_path)
    assert len(units)==1 and units[0]['mint']==mint


def test_capital_throttle_stops_chain_dispatch_without_churning_pending_flow(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    at='2026-10-04T16:00:00Z';owners={('solana','Owner'):('A','Alpha')}
    units=[dict(chain='solana',uid='A',address='Owner',mint='Mint'+str(i),signature='Sig'+str(i),t0=1000,
                proof_addresses=['Owner'],own_signatures=[]) for i in range(20)]
    monkeypatch.setattr(worker,'inputs',lambda root:(owners,units,[],dict(window_from=at,census_valid=True)))
    monkeypatch.setattr(worker,'prices',lambda:{})
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(worker,'endpoints',lambda env:{'solana':'https://research.invalid'})
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    monkeypatch.setattr(worker,'publish',lambda *args:None)
    class Pacer:
        paused=False
        def pause(self,seconds):self.paused=True
    pacer=Pacer()
    def pace():
        if pacer.paused:raise ValueError('rate_limit_pause')
    monkeypatch.setattr(worker,'paced',lambda *args:(pace,pacer))
    class Rpc:
        def __init__(self,url,limit,pace):self.used=0;self.pace=pace
        def __call__(self,method,params):
            self.pace();self.used+=1
            if method=='getGenesisHash':return SOLANA_MAINNET_GENESIS
            raise ValueError('rate_limit')
        def close(self):pass
    monkeypatch.setattr(worker.m,'ReadRpc',Rpc)
    report=worker.run_once(tmp_path,max_calls=120)
    status=report['chains']['solana']
    assert status['state']=='rate_limit' and status['capital_reads']==1 and status['flow_reads']==0
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:assert db.execute('SELECT count(*) FROM flow_units').fetchone()[0]==0
    finally:db.close()


def test_archival_recovery_is_lazy_source_only_and_keeps_one_attempt_cap(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    urls=[]
    tx='0x'+'a'*64
    class Rpc:
        def __init__(self,url,limit,pace):self.url=url;self.limit=limit;self.used=0;urls.append(url)
        def __call__(self,method,params):
            if self.used>=self.limit:raise ValueError('budget')
            self.used+=1
            if method=='eth_chainId':return '0x38'
            if method=='eth_getBalance':return '0x0'
            if self.url=='https://research.invalid':raise ValueError('missing_result')
            return dict(transactionHash=tx,blockNumber='0x2',blockHash='0x'+'b'*64,transactionIndex='0x0')
        def close(self):pass
    monkeypatch.setattr(worker.m,'ReadRpc',Rpc)
    monkeypatch.setattr(worker,'paced',lambda *args:(lambda:None,None))
    monkeypatch.setattr(worker,'funded_endpoints',lambda env:[])
    reader=worker.research_reader(tmp_path,'56','https://research.invalid',5)
    assert reader('eth_getBalance',['0x'+'1'*40,'latest'])=='0x0' and len(urls)==1
    source=reader.for_unit(dict(chain='56',signature=tx,source_slot=None))
    assert source('eth_getTransactionReceipt',[tx])['transactionHash']==tx
    assert reader.used==4 and len(urls)==2
    import pytest
    with pytest.raises(ValueError,match='budget'):source('eth_getTransactionReceipt',[tx])
    assert reader.used==5
    reader.close()


def test_source_archive_cannot_use_a_configured_funded_endpoint(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    class Rpc:
        def __init__(self,*args,**kwargs):pass
    monkeypatch.setattr(worker.m,'ReadRpc',Rpc)
    monkeypatch.setattr(worker,'paced',lambda *args:(lambda:None,None))
    monkeypatch.setattr(worker,'funded_endpoints',lambda env:['https://bsc-dataseed-public.bnbchain.org'])
    assert isinstance(worker.research_reader(tmp_path,'56','https://research.invalid',5),Rpc)


def test_census_keeps_first_detection_when_later_history_has_an_earlier_block_clock(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services import wallet_observation
    at='2026-10-04T16:00:00Z';address='0x'+'1'*40;mint='0x'+'2'*40
    first='0x'+'a'*64;later='0x'+'b'*64
    class Cursor:
        def __init__(self,rows):self.rows=rows
        def __iter__(self):return iter(self.rows)
        def fetchall(self):return self.rows
    class Ledger:
        def execute(self,sql,args=()):
            if 'memecoin_wallets' in sql:return Cursor([('A',)])
            if 'memecoin_fills' in sql:return Cursor([])
            return Cursor([(first,'A',address,'56',mint,1791129600,at,1,None),
                           (later,'A',address,'56',mint,1791129500,'2026-10-04T16:01:00Z',2,None)])
        def close(self):pass
    monkeypatch.setattr(worker,'get_readonly_connection',lambda path:Ledger())
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(wallet_observation,'observation_owners',lambda c,u:{('56',address):('A','Alpha')})
    assert worker.inputs(tmp_path)[1][0]['signature']==first


def test_collector_publishes_census_before_price_provider_reads(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    events=[]
    monkeypatch.setattr(worker,'inputs',lambda root:({},[],[],dict(window_from='2026-10-04T16:00:00Z',census_valid=True)))
    monkeypatch.setattr(worker,'prices',lambda:events.append('prices') or {})
    monkeypatch.setattr(worker,'publish',lambda *args:events.append('published'))
    worker.run_once(tmp_path,max_calls=1)
    assert events[0]=='published'


def test_failed_windows_back_off_without_leaving_coverage_denominator(tmp_path,monkeypatch):
    from scripts.research import wallet_universe_metrics as worker
    at='2026-10-04T16:00:00Z';address='0x'+'1'*40
    unit=dict(chain='56',uid='A',address=address,mint='0x'+'2'*40,signature='0x'+'a'*64,
              t0=1791129000,proof_addresses=[address],own_signatures=[])
    owners={('56',address):('A','Alpha')}
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    failed=dict(status='censored',detail='source_sender_identity_mismatch',next_attempt_at='2026-10-04T16:10:00Z')
    db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',(worker.unit_key(unit),json.dumps(unit),json.dumps(failed),at));db.commit();db.close()
    monkeypatch.setattr(worker,'inputs',lambda root:(owners,[unit],[],dict(window_from=at,census_valid=True)))
    monkeypatch.setattr(worker,'prices',lambda:{})
    monkeypatch.setattr(worker,'now_iso',lambda:at)
    monkeypatch.setattr(worker,'endpoints',lambda env:{'56':'https://research.invalid'})
    monkeypatch.setattr(worker,'flat',lambda root,chain:True)
    monkeypatch.setattr(worker,'publish',lambda *args:None)
    class Rpc:
        used=0
        def __call__(self,method,params):self.used+=1;return '0x38'
        def close(self):pass
    monkeypatch.setattr(worker,'research_reader',lambda *args:Rpc())
    monkeypatch.setattr(worker,'paced',lambda *args:(lambda:None,None))
    monkeypatch.setattr(worker.m,'observe_capital',lambda *args,**kwargs:dict(state='ok',method=worker.m.CAPITAL_METHOD,address=address,usd=0,observed_at=at))
    report=worker.run_once(tmp_path,max_calls=120)
    assert report['chains']['56']['flow_reads']==0
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:
        row=worker.summarize(db,[unit],owners,{},at)[0]
        assert row['eligible']==1 and row['complete']==0 and row['states']=={'censored':1}
    finally:db.close()


def test_research_transport_allows_historical_code_reads_but_no_write():
    from sniper.services.wallet_universe_metrics import ReadRpc
    class Reply:
        status_code=200
        def json(self):return dict(jsonrpc='2.0',id=1,result='0x1234')
    rpc=ReadRpc('https://research.invalid',post=lambda *args,**kwargs:Reply())
    assert rpc('eth_getCode',['0x'+'1'*40,'0x20'])=='0x1234'
    import pytest
    with pytest.raises(ValueError,match='read_method_refused'):rpc('eth_sendRawTransaction',['0x1234'])
    assert rpc.used==1


def test_individual_flow_read_retains_beneficiary_only_source_attribution(tmp_path):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services.wallet_universe_flow import METHOD
    address='0x'+'1'*40
    unit=dict(chain='56',uid='A',address=address,mint='0x'+'2'*40,signature='0x'+'a'*64,
              t0=1791129000,proof_addresses=[address],own_signatures=[])
    identity={k:unit[k] for k in ('chain','uid','address','mint','signature','t0')}
    result=dict(method=METHOD,status='complete',after_complete=True,quotes=[],
        state=dict(version=METHOD,binding=dict(identity=identity,exclusions=[],own=[]),
                   to_block=1,headers={'2':dict(number=2,timestamp=unit['t0']+62,hash='0x')}),  # a real complete EVM row carries its upper bracket
        source_proof=dict(kind='relay_beneficiary_purchase',initiating_wallet_verified=False))
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',
                   (worker.unit_key(unit),json.dumps(unit),json.dumps(result),'2026-10-04T16:00:00Z'))
        row=worker.summarize(db,[unit],{('56',address):('A','Alpha')},{},'2026-10-04T16:00:00Z')[0]
        assert row['recent'][0]['source_attribution']=='beneficiary_only'
    finally:db.close()


def test_a_completed_evm_cache_without_its_upper_bracket_is_not_current():
    # Codex night review W6 residual at the production caller (Gamma): the worker skips every
    # current complete row, so the bracket check must live in current_result too.
    from copy import deepcopy
    from scripts.research.wallet_universe_metrics import current_result
    from sniper.services.wallet_universe_flow import METHOD
    unit=dict(chain='8453',uid='A',address='0x'+'44'*20,mint='0x'+'11'*20,signature='0x'+'aa'*32,t0=1000,
              source_initiation=None,source_slot=None,own_signatures=[])
    binding=dict(identity={k:unit[k] for k in ('chain','uid','address','mint','signature','t0')},
                 source_initiation=None,source_slot=None,exclusions=[],own=[])
    state=dict(version=METHOD,binding=binding,to_block=106,headers={'107':dict(number=107,timestamp=1062,hash='0x')})
    result=dict(method=METHOD,state=state,status='complete')
    assert current_result(unit,result)
    old=deepcopy(result);old['state']['headers'].pop('107')
    assert not current_result(unit,old)
    stamped=deepcopy(result);stamped['state']['headers']['107']['timestamp']=1060
    assert not current_result(unit,stamped)


def test_the_published_summary_keeps_a_bracketed_complete_evm_row(tmp_path):
    # The artifact path reads a trimmed state projection: it must still carry the upper bracket,
    # or every complete EVM row would read as invalidated.
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services.wallet_universe_flow import METHOD
    address='0x'+'1'*40
    unit=dict(chain='56',uid='A',address=address,mint='0x'+'2'*40,signature='0x'+'a'*64,
              t0=1791129000,proof_addresses=[address],own_signatures=[])
    identity={k:unit[k] for k in ('chain','uid','address','mint','signature','t0')}
    state=dict(version=METHOD,binding=dict(identity=identity,exclusions=[],own=[]),to_block=1,
               headers={'1':dict(number=1,timestamp=unit['t0']+59,hash='0x'),
                        '2':dict(number=2,timestamp=unit['t0']+62,hash='0x')})
    result=dict(method=METHOD,status='complete',after_complete=True,quotes=[],state=state)
    db=worker.m.open_metrics(tmp_path/'data/wallet_universe_metrics.db')
    try:
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',
                   (worker.unit_key(unit),json.dumps(unit),json.dumps(result),'2026-10-04T16:00:00Z'))
        projected=json.loads(db.execute(f'SELECT {worker._FLOW_METADATA_SQL} FROM flow_units').fetchone()[0])
        assert set(projected['state']['headers'])=={'2'} and worker.current_result(unit,projected)
        state['headers'].pop('2')
        db.execute('UPDATE flow_units SET result=?',(json.dumps(result),))
        projected=json.loads(db.execute(f'SELECT {worker._FLOW_METADATA_SQL} FROM flow_units').fetchone()[0])
        assert not worker.current_result(unit,projected)
    finally:db.close()
