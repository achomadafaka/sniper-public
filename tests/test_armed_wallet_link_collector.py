"""Receipt-bound research facts; never an ownership or entry-authority test."""
from copy import deepcopy
import importlib

import pytest

OWNER = '0x' + '11' * 20
PEER = '0x' + '22' * 20
TOKEN = '0x' + '33' * 20
QUOTE = '0x' + '44' * 20
HASH = '0x' + '55' * 32
BLOCK = '0x' + '66' * 32


def bundle(chain='8453', value=0):
    tx = dict(hash=HASH, blockHash=BLOCK, blockNumber='0x64', transactionIndex='0x2',
              chainId=hex(int(chain)), **{'from': OWNER, 'to': PEER}, value=hex(value), input='0x')
    receipt = dict(transactionHash=HASH, blockHash=BLOCK, blockNumber='0x64', transactionIndex='0x2',
                   status='0x1', **{'from': OWNER, 'to': PEER}, logs=[])
    header = dict(hash=BLOCK, number='0x64', timestamp='0x3e8')
    return receipt, tx, header


def transfer(receipt, token, sender, recipient, amount):
    from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC
    receipt['logs'].append(dict(address=token, topics=[TRANSFER_TOPIC,
        '0x' + '00' * 12 + sender[2:], '0x' + '00' * 12 + recipient[2:]], data='0x' + f'{amount:064x}',
        transactionHash=HASH, blockHash=BLOCK, blockNumber='0x64', transactionIndex='0x2', logIndex='0x0', removed=False))


@pytest.fixture
def proof():
    return importlib.import_module('sniper.services.wallet_link_proofs')


def test_direct_buy_requires_linked_payment_and_canonical_order(proof):
    r, t, h = bundle()
    transfer(r, QUOTE, OWNER, PEER, 100)
    transfer(r, TOKEN, PEER, OWNER, 200)
    buy = proof.evm_buy(r, t, h, chain='8453', mint=TOKEN, quotes=[QUOTE])
    assert buy['address'] == OWNER and buy['order'] == [100, 2]
    assert buy['t0'] == 1000 and buy['acquired_atomic'] == '200'
    assert buy['ownership_proven'] is False


@pytest.mark.parametrize('change', ['failed', 'chain', 'index', 'block', 'log_hash', 'removed', 'padded_topic', 'unlinked', 'relay', 'mixed_payment'])
def test_unsupported_or_incoherent_buy_is_unknown(proof, change):
    r, t, h = bundle()
    transfer(r, QUOTE, OWNER, PEER, 100)
    transfer(r, TOKEN, PEER, OWNER, 200)
    if change == 'failed': r['status'] = '0x0'
    elif change == 'chain': t['chainId'] = '0x38'
    elif change == 'index': t['transactionIndex'] = '0x3'
    elif change == 'block': h['hash'] = '0x' + '77' * 32
    elif change == 'log_hash': r['logs'][0]['transactionHash'] = '0x' + '77' * 32
    elif change == 'removed': r['logs'][0]['removed'] = True
    elif change == 'padded_topic': r['logs'][0]['topics'][1] = '0x' + 'ff' * 12 + OWNER[2:]
    elif change == 'unlinked': r['logs'][0]['topics'][2] = '0x' + '00' * 12 + TOKEN[2:]
    elif change == 'relay': t['from'] = r['from'] = TOKEN
    elif change == 'mixed_payment': t['value'] = '0x1'
    assert proof.evm_buy(r, t, h, chain='8453', mint=TOKEN, quotes=[QUOTE]) is None


@pytest.mark.parametrize('chain,symbol,decimals', [('56', 'BNB', 18), ('5042', 'USDC', 18)])
def test_native_funding_keeps_chain_units(proof, chain, symbol, decimals):
    r, t, h = bundle(chain, 10 ** decimals)
    facts = proof.evm_funding(r, t, h, chain=chain, assets={},recipient_code='0x')
    assert facts[0]['amount'] == '1' and facts[0]['amount_atomic'] == str(10 ** decimals)
    assert facts[0]['symbol'] == symbol and facts[0]['decimals'] == decimals
    assert facts[0]['asset'] == 'native'


def test_native_contract_recipient_credit_is_unknown_without_trace(proof):
    r,t,h=bundle(value=10**18)
    assert proof.evm_funding(r,t,h,chain='8453',assets={},recipient_code='0x6000')==[]
    assert proof.evm_funding(r,t,h,chain='8453',assets={})==[]


def test_funding_uint256_decimal_never_rounds(proof):
    from decimal import Decimal,localcontext
    amount=2**256-1
    r,t,h=bundle()
    transfer(r,QUOTE,OWNER,PEER,amount)
    fact=proof.evm_funding(r,t,h,chain='8453',assets={QUOTE:('USDC',6)},recipient_codes={PEER:'0x'})[0]
    with localcontext() as context:
        context.prec=100
        assert Decimal(fact['amount'])*10**6==amount


def test_arc_erc20_facade_is_six_decimals(proof):
    r,t,h=bundle('5042')
    transfer(r,QUOTE,OWNER,PEER,1000000)
    fact=proof.evm_funding(r,t,h,chain='5042',assets={QUOTE:('USDC',6)},recipient_codes={PEER:'0x'})[0]
    assert fact['decimals']==6 and fact['amount']=='1' and fact['method']=='confirmed_erc20_transfer'


def test_quote_funding_requires_signed_direct_sender_and_trusted_asset(proof):
    r, t, h = bundle()
    transfer(r, QUOTE, OWNER, PEER, 2500000)
    facts = proof.evm_funding(r, t, h, chain='8453', assets={QUOTE: ('USDC', 6)},recipient_codes={PEER:'0x'})
    assert facts[0]['amount'] == '2.5'
    r['logs'][0]['topics'][1] = '0x' + '00' * 12 + TOKEN[2:]
    assert proof.evm_funding(r, t, h, chain='8453', assets={QUOTE: ('USDC', 6)},recipient_codes={PEER:'0x'}) == []


@pytest.mark.parametrize('change',['refund','contract'])
def test_quote_funding_needs_net_credit_and_eoa_recipient(proof,change):
    r,t,h=bundle();transfer(r,QUOTE,OWNER,PEER,100)
    codes={PEER:'0x'}
    if change=='refund':transfer(r,QUOTE,PEER,OWNER,100)
    else:codes[PEER]='0x6000'
    assert proof.evm_funding(r,t,h,chain='8453',assets={QUOTE:('USDC',6)},recipient_codes=codes)==[]


def sol_window():
    unit = dict(chain='solana', uid='main', address='A' * 32, mint='M' * 32, signature='S' * 64, t0=1000)
    source = dict(signature=unit['signature'], t0=1000, order=[100, 2])
    rec = dict(signature='B' * 64, order=[100, 1], actor='C' * 32, actor_verified=True,
        payment_signer=None, protocol='pump', evidence_provenance='runtime_and_compiled_instruction_trace_v1',
        legs=[dict(asset='So11111111111111111111111111111111111111112', decimals=9, amount='2')])
    result = dict(method='wallet-universe-flow-v7', source_found=True, state=dict(
        version='wallet-universe-flow-v7', binding=dict(identity=unit), source=source,
        records=[rec], seen={rec['signature']: [1000, 100, 1]}))
    return unit, result


def test_cached_sol_before_in_same_slot_has_zero_wall_clock_lead(proof):
    u, r = sol_window()
    rows = proof.sol_cached_buys(u, r)
    assert rows[0]['source_order'] == [100, 2] and rows[0]['order'] == [100, 1]
    assert rows[0]['t0'] == rows[0]['source_t0'] == 1000


@pytest.mark.parametrize('change', ['source_missing', 'binding', 'actor', 'sell', 'unknown_quote', 'order', 'clock'])
def test_sol_source_actor_payment_order_and_clock_are_required(proof, change):
    u, r = sol_window()
    s, rec = r['state'], r['state']['records'][0]
    if change == 'source_missing': s['source'] = None
    elif change == 'binding': s['binding']['identity'] = dict(u, address='D' * 32)
    elif change == 'actor': rec['actor_verified'] = False
    elif change == 'sell': rec['legs'][0]['amount'] = '-2'
    elif change == 'unknown_quote': rec['legs'][0]['asset'] = 'X' * 32
    elif change == 'order': rec['order'] = [100, None]
    elif change == 'clock': s['seen'][rec['signature']] = [1000, 101, 1]
    assert proof.sol_cached_buys(u, r) == []


def test_collector_readonly_roster_keeps_unmapped_pairs(tmp_path):
    import sqlite3
    from scripts.research.armed_wallet_links import armed_pairs
    conn = sqlite3.connect(tmp_path / 'roster.db')
    conn.executescript("CREATE TABLE memecoin_wallets(source_user_id TEXT,real_approved INT,real_chains TEXT,paused INT,excluded INT);"
        "INSERT INTO memecoin_wallets VALUES ('a',1,'solana,56',0,0),('paused',1,'56',1,0),('watch',0,'solana',0,0);")
    rows = armed_pairs(conn, [])
    assert [(r['uid'], r['chain'], r['main_addresses']) for r in rows] == [('a','56',[]),('a','solana',[])]
    assert all(r['coverage']['reason'] == 'verified_address_unavailable' for r in rows)
    conn.close()


def test_capture_success_is_reused_but_failed_read_can_retry(tmp_path):
    from scripts.research.armed_wallet_links import Captures
    attempts=[]
    def read(method,params):
        read.used+=1
        attempts.append(method)
        if len(attempts)==1:raise ValueError('provider_timeout')
        return {'hash':HASH}
    read.used=0
    captures=Captures(tmp_path,'8453',read)
    with pytest.raises(ValueError,match='provider_timeout'):captures('eth_getTransactionByHash',[HASH])
    assert captures('eth_getTransactionByHash',[HASH])=={'hash':HASH}
    assert captures('eth_getTransactionByHash',[HASH])=={'hash':HASH}
    assert len(attempts)==2 and len(list((tmp_path/'8453').glob('*.failed.json')))==1


def test_repeated_failed_method_stops_spending_and_retry_is_next_run(tmp_path):
    from scripts.research.armed_wallet_links import Captures
    calls=[]
    def read(method,params):read.used+=1;calls.append(params);raise ValueError('provider_http_error')
    read.used=0
    captured=Captures(tmp_path,'8453',read)
    for i in range(4):
        with pytest.raises(ValueError,match='provider_http_error'):captured('eth_getLogs',[i])
    assert len(calls)==3
    fresh=Captures(tmp_path,'8453',read)
    with pytest.raises(ValueError):fresh('eth_getLogs',[4])
    assert len(calls)==4


@pytest.mark.parametrize('method,params',[('eth_blockNumber',[]),('getTransactionsForAddress',['main']),
    ('blockscout_address_transactions',['main']),('eth_call',[{'to':QUOTE},'latest'])])
def test_mutable_reads_are_run_scoped_and_old_capture_is_retained(tmp_path,method,params):
    from scripts.research.armed_wallet_links import Captures
    def read(m,p):read.used+=1;return read.used
    read.used=0
    first=Captures(tmp_path,'8453',read)
    # The transport-specific GET is irrelevant to the cache boundary.
    first._explorer=lambda p:read(method,p)
    assert first(method,params)==first(method,params)==1
    second=Captures(tmp_path,'8453',read);second._explorer=lambda p:read(method,p)
    assert second(method,params)==2
    captures=[p for p in (tmp_path/'8453').glob('*.json') if p.name!='rpc_usage.json']
    assert len(captures)==2


def test_saved_evm_funding_survives_unavailable_history_and_rpc(tmp_path):
    from scripts.research.armed_wallet_links import Captures,retained_funding
    r,t,h=bundle();transfer(r,QUOTE,OWNER,PEER,1000000)
    def read(method,params):
        read.used+=1
        return {'eth_chainId':'0x2105','eth_getTransactionReceipt':r,'eth_getTransactionByHash':t,
            'eth_getBlockByNumber':h,'eth_getCode':'0x'}[method]
    read.used=0
    stored=Captures(tmp_path,'8453',read)
    for method,params in [('eth_chainId',[]),('eth_getTransactionReceipt',[HASH]),('eth_getTransactionByHash',[HASH]),
                         ('eth_getBlockByNumber',['0x64',False]),('eth_getCode',[PEER,'0x64'])]:stored(method,params)
    offline=Captures(tmp_path,'8453')
    facts=retained_funding(offline,{QUOTE:('USDC',6)},'1970-01-01T00:30:00+00:00')
    assert len(facts)==1 and facts[0]['recipient']==PEER and facts[0]['amount_atomic']=='1000000'


def test_statistics_preserve_distinct_coins_before_evidence_cap():
    from scripts.research.armed_wallet_links import _summarize
    pair=dict(uid='u',chain='solana',main_addresses=['main'],coverage={'capped':[]})
    facts=[dict(uid='u',chain='solana',address='peer',mint=str(i),source_order=[20,2],
        order=[20,1],source_t0=1000,t0=1000,buy_signature=str(i),source_signature='s'+str(i)) for i in range(12)]
    saved=_summarize([pair],facts,[])
    assert len(saved)==10 and pair['statistics'][0]['n_coins']==12
    assert pair['statistics'][0]['before_coins']==12 and pair['statistics'][0]['lead_p50_s']==0
    assert pair['coverage']['evidence_omitted']==2


def test_bundle_rejects_provider_returning_different_transaction():
    from scripts.research.armed_wallet_links import _bundle
    r,t,h=bundle()
    def read(method,params):return {'eth_getTransactionReceipt':r,'eth_getTransactionByHash':t,'eth_getBlockByNumber':h}[method]
    with pytest.raises(ValueError,match='requested_transaction_mismatch'):
        _bundle(read,'0x'+'77'*32)


def test_unverified_source_is_unknown_and_never_sampled(tmp_path):
    import json,sqlite3
    from scripts.research.armed_wallet_links import _local_windows
    u,r=sol_window();r['state']['source']=None
    (tmp_path/'data').mkdir()
    c=sqlite3.connect(tmp_path/'data/wallet_universe_metrics.db')
    c.execute('CREATE TABLE flow_units(unit TEXT,result TEXT)')
    c.execute('INSERT INTO flow_units VALUES (?,?)',(json.dumps(u),json.dumps(r)));c.commit();c.close()
    pair=dict(uid='main',chain='solana',main_addresses=[u['address']],coverage=dict(source_units=0,capped=[]))
    rows,_pending=_local_windows(tmp_path,[pair],'1970-01-01T00:30:00+00:00')
    assert rows==[] and pair['coverage']['sampled_coins']==0
    assert pair['coverage']['source_coins']==pair['coverage']['unknown_coins']==1


def test_current_custody_is_never_linked_as_repeat_buyer_or_funding_alt():
    from scripts.research.armed_wallet_links import without_custody
    buyers=[dict(address=OWNER),dict(address=PEER)]
    funds=[dict(funder=OWNER,recipient=TOKEN),dict(funder=PEER,recipient=TOKEN)]
    assert without_custody(buyers,funds,{OWNER})==([buyers[1]],[funds[1]])


def test_saved_token_funding_recovers_metadata_when_fresh_provider_is_unavailable(tmp_path,monkeypatch):
    import scripts.research.armed_wallet_links as collector
    monkeypatch.setattr(collector,'assets_for',lambda chain:[(QUOTE,'USDC',None)])
    r,t,h=bundle();transfer(r,QUOTE,OWNER,PEER,1000000)
    def read(method,params):
        read.used+=1
        return {'eth_chainId':'0x2105','eth_call':'0x6','eth_getTransactionReceipt':r,
            'eth_getTransactionByHash':t,'eth_getBlockByNumber':h,'eth_getCode':'0x'}[method]
    read.used=0
    stored=collector.Captures(tmp_path,'8453',read)
    for method,params in [('eth_chainId',[]),('eth_call',[dict(to=QUOTE,data='0x313ce567'),'latest']),
        ('eth_getTransactionReceipt',[HASH]),('eth_getTransactionByHash',[HASH]),
        ('eth_getBlockByNumber',['0x64',False]),('eth_getCode',[PEER,'0x64'])]:stored(method,params)
    offline=collector.Captures(tmp_path,'8453')
    facts=collector.retained_funding(offline,{},'1970-01-01T00:30:00+00:00')
    assert len(facts)==1 and facts[0]['amount']=='1'


def test_resumed_funding_phase_reserves_half_remaining_buyer_reads():
    from types import SimpleNamespace
    from scripts.research.armed_wallet_links import funding_phase_limit
    rpc=SimpleNamespace(limit=12,used=3)
    assert funding_phase_limit(rpc)==7
    assert funding_phase_limit(SimpleNamespace(limit=160,used=3))==81


def test_fresh_roster_drops_changed_claim_activity_and_unbound_funding():
    from scripts.research.armed_wallet_links import refresh_snapshot
    old=[dict(uid='u',chain='8453',main_addresses=[OWNER],coverage={'state':'partial'},statistics=[{'address':PEER}]),
         dict(uid='gone',chain='8453',main_addresses=[TOKEN],coverage={},statistics=[])]
    fresh=[dict(uid='u',chain='8453',main_addresses=[PEER],coverage={'state':'unavailable','reason':None},statistics=[]),
           dict(uid='new',chain='1',main_addresses=[OWNER],coverage={'state':'unavailable','reason':None},statistics=[])]
    buys=[dict(uid='u',chain='8453',main_address=OWNER)]
    funding=[dict(chain='8453',funder=TOKEN,recipient=QUOTE),dict(chain='8453',funder=TOKEN,recipient=PEER)]
    pairs,buys,bound=refresh_snapshot(old,fresh,buys,funding)
    assert [(p['uid'],p['chain']) for p in pairs]==[('u','8453'),('new','1')]
    assert not buys and bound==[funding[1]]
    assert all(p['coverage']['reason']=='roster_changed_during_collection' for p in pairs)
    assert all(not p['statistics'] for p in pairs)


def test_fresh_roster_keeps_unchanged_pair_evidence():
    from scripts.research.armed_wallet_links import refresh_snapshot
    old=dict(uid='u',chain='8453',main_addresses=[OWNER],coverage={'state':'partial'},statistics=[{'address':PEER}])
    fresh=dict(uid='u',chain='8453',main_addresses=[OWNER],coverage={},statistics=[])
    buys=[dict(uid='u',chain='8453',main_address=OWNER)]
    pairs,bound_buys,_funding=refresh_snapshot([old],[fresh],buys,[])
    assert pairs==[old] and bound_buys==buys


def large_valid_artifact():
    pairs=[];buys=[]
    for i in range(252):
        uid='u'+str(i);main='0x'+f'{i+1:040x}'
        stats=[]
        for peer in range(20):
            address='0x'+f'{10000+peer:040x}'
            stats.append(dict(address=address,n_coins=10,before_coins=10,after_coins=0,
                days=1,lead_p50_s=1,lead_p90_s=1))
            for coin in range(10):
                buys.append(dict(uid=uid,chain='8453',main_address=main,address=address,
                    mint='0x'+f'{20000+coin:040x}',source_signature='0x'+f'{i*10+coin+1:064x}',
                    buy_signature='0x'+f'{100000+i*200+peer*10+coin:064x}',source_order=[100+coin,2],
                    order=[100+coin,1],source_t0=1000+coin,t0=999+coin,
                    proof_method='direct-wallet-paid-v1',ownership_proven=False,proof_detail='x'*256))
        pairs.append(dict(uid=uid,chain='8453',main_addresses=[main],statistics=stats,
            coverage=dict(state='partial',source_units=10,sampled_coins=10,complete_coins=0,
                unknown_coins=10,evidence_omitted=0,capped=[])))
    return dict(version=1,method='armed-wallet-links-v1',generated_at='1970-01-01T00:30:00+00:00',
        pairs=pairs,buys=buys,funding=[])


def test_global_evidence_cap_is_fair_and_matches_reader_exact_bytes(tmp_path):
    import json
    from scripts.research.armed_wallet_links import publish_links
    from sniper.services.wallet_identity_links import _validate,MAX_BYTES
    artifact=large_valid_artifact();before=len(artifact['buys'])
    path=tmp_path/'links.json'
    publish_links(path,artifact,now_iso=artifact['generated_at'])
    saved=json.loads(path.read_text());_validate(saved,1800)
    assert path.stat().st_size<=MAX_BYTES and len(saved['buys'])<=12000
    assert len(saved['pairs'])==252 and all(len(p['statistics'])==20 for p in saved['pairs'])
    assert {b['uid'] for b in saved['buys']}=={p['uid'] for p in saved['pairs']}
    assert sum(p['coverage']['evidence_omitted'] for p in saved['pairs'])==before-len(saved['buys'])
    assert all('artifact_evidence' in p['coverage']['capped'] for p in saved['pairs'])


@pytest.mark.parametrize('failure',['invalid','oversize_metadata'])
def test_failed_link_publication_preserves_previous_file(tmp_path,failure):
    from scripts.research.armed_wallet_links import publish_links
    artifact=large_valid_artifact();artifact['buys']=[]
    if failure=='invalid':artifact['method']='unproven'
    else:artifact['metadata']='x'*(4*1024*1024)
    path=tmp_path/'links.json';path.write_text('previous valid artifact')
    with pytest.raises(ValueError):publish_links(path,artifact,now_iso=artifact['generated_at'])
    assert path.read_text()=='previous valid artifact'


def test_confirmed_funding_preserves_cached_global_hub_degree(tmp_path):
    import sqlite3
    from scripts.research.armed_wallet_links import funding_hubs
    (tmp_path/'data').mkdir()
    conn=sqlite3.connect(tmp_path/'data/memecoin_wallet_graph.db')
    conn.execute('CREATE TABLE memecoin_wallet_funders(wallet TEXT,status TEXT,funder TEXT)')
    conn.executemany('INSERT INTO memecoin_wallet_funders VALUES (?,?,?)',
        [(str(i),'resolved',OWNER) for i in range(4)]+[('0','unavailable',OWNER),('only','resolved',PEER)])
    conn.commit();conn.close()
    funding=[dict(chain='solana',funder=OWNER,ownership_proven=False),dict(chain='solana',funder=PEER,ownership_proven=False)]
    funding_hubs(tmp_path,funding)
    assert funding[0]['hub'] is True and funding[0]['funder_degree_lower_bound']==4
    assert funding[0]['hub_source']=='wallet_graph_cached_funding_degree_lower_bound'
    assert funding[0]['ownership_proven'] is False and not funding[1].get('hub')
