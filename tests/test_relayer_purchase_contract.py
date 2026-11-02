"""Reject an unlinked gift and decode a confirmed Relay/Kyber purchase."""
import copy
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode

from sniper.services import memecoin_evm_onchain_watch as watch

FIXTURE = json.loads((Path(__file__).parent/'fixtures/axiom/4663_relay_kyber_purchase.json').read_text())
PROXY = '0xccc88a9d1b4ed6b0eaba998850414b24f1c315be'
ROUTER = '0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f'
KYBER = '0x6131b5fae19ea4f9d964eac0408e4408b66337b5'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
TYPES = ['address','((address,uint256)[],uint256,uint256)',
         '(address,bool,uint256,bytes)[]','address','address','bytes','bytes']


def classify(data=None, *, chain='4663', contracts=True):
    data = data or FIXTURE
    fn = getattr(watch, 'relayer_purchase_from_receipt', lambda *a, **kw: None)
    return fn(data['receipt'],data['transaction'],data['wallet'],chain=chain,
              contracts_verified=contracts)


def mutate_call(data, change):
    args=list(decode(TYPES,bytes.fromhex(data['transaction']['input'][10:])))
    change(args)
    data['transaction']['input']='0x0a2b8f36'+encode(TYPES,args).hex()


def test_real_receipt_links_permit_payment_swap_output_and_beneficiary():
    proof=classify()
    assert proof is not None
    assert proof['payment_token']==USDG
    assert proof['payment_atomic']==598918556
    assert proof['payer']=='0xf70da97812cb96acdf810712aa562db8dfa3dbef'
    assert proof['relayer']==FIXTURE['transaction']['from'].lower()
    assert proof['beneficiary']==FIXTURE['wallet']
    assert proof['mint']==FIXTURE['mint']
    assert proof['output_atomic']==int('1459523557ed8e68ff5eec',16)
    assert proof['transaction_hash']==FIXTURE['transaction']['hash']


@pytest.mark.parametrize('case',[
    'gift_with_unrelated_payment','wrong_swap_mint','wrong_swap_amount','foreign_swap_emitter',
    'unverified_contracts','other_chain','failed_receipt','mismatched_transaction',
    'wrong_beneficiary','empty_signature','empty_permit','allow_failure',
    'extra_cleanup_beneficiary','extra_swap','removed_log','malformed_topic',
    'refund_to_payer','payment_refund','recipient_forwards_output',
])
def test_rejects_unlinked_or_ambiguous_evidence(case):
    x=copy.deepcopy(FIXTURE);r=x['receipt'];tx=x['transaction']
    swapped=next(l for l in r['logs'] if l['address'].lower()==KYBER and l['topics'][0].startswith('0xd6d4'))
    if case=='gift_with_unrelated_payment':
        r['logs'].remove(swapped)
    elif case in ('wrong_swap_mint','wrong_swap_amount'):
        a=list(decode(['address']*4+['uint256']*2,bytes.fromhex(swapped['data'][2:])))
        if case=='wrong_swap_mint':a[2]='0x'+'99'*20
        else:a[4]+=1
        swapped['data']='0x'+encode(['address']*4+['uint256']*2,a).hex()
    elif case=='foreign_swap_emitter':swapped['address']='0x'+'99'*20
    elif case=='unverified_contracts':assert classify(x,contracts=False) is None;return
    elif case=='other_chain':assert classify(x,chain='56') is None;return
    elif case=='failed_receipt':r['status']='0x0'
    elif case=='mismatched_transaction':tx['hash']='0x'+'11'*32
    elif case=='wrong_beneficiary':x['wallet']='0x'+'99'*20
    elif case=='empty_signature':mutate_call(x,lambda a:a.__setitem__(6,b''))
    elif case=='empty_permit':mutate_call(x,lambda a:a.__setitem__(1,((),1,2)))
    elif case=='allow_failure':
        def change(a):
            calls=list(a[2]);t,f,v,d=calls[1];calls[1]=(t,True,v,d);a[2]=calls
        mutate_call(x,change)
    elif case=='extra_cleanup_beneficiary':
        def change(a):
            calls=list(a[2]);t,f,v,d=calls[-1]
            token,recipients,mins,meta=decode(['address[]','address[]','uint256[]','bytes'],d[4:])
            calls[-1]=(t,f,v,d[:4]+encode(['address[]','address[]','uint256[]','bytes'],[token+token,recipients+('0x'+'99'*20,),mins+mins,meta]));a[2]=calls
        mutate_call(x,change)
    elif case=='extra_swap':r['logs'].append(copy.deepcopy(swapped))
    elif case=='removed_log':swapped['removed']=True
    elif case=='malformed_topic':
        next(l for l in r['logs'] if l['topics'][0]==watch.TRANSFER_TOPIC)['topics'][1]='0x1'
    elif case=='refund_to_payer':mutate_call(x,lambda a:a.__setitem__(3,a[0]))
    elif case in ('payment_refund','recipient_forwards_output'):
        token=USDG if case=='payment_refund' else x['mint']
        sender=ROUTER if case=='payment_refund' else x['wallet']
        receiver='0xf70da97812cb96acdf810712aa562db8dfa3dbef' if case=='payment_refund' else '0x'+'99'*20
        r['logs'].append({'address':token,'topics':[watch.TRANSFER_TOPIC,'0x'+sender[2:].rjust(64,'0'),'0x'+receiver[2:].rjust(64,'0')],'data':'0x'+format(1,'064x'),'transactionHash':tx['hash'],'blockHash':r['blockHash'],'logIndex':'0x48'})
    assert classify(x) is None


def test_short_calldata_is_unknown():
    x=copy.deepcopy(FIXTURE);x['transaction']['input']='0x0a2b8f3600'
    assert classify(x) is None


def test_price_uses_payment_decimals_and_a_measured_usd_value():
    fn=getattr(watch,'price_relayer_purchase',lambda *a,**kw:None)
    result=fn(classify(), decimals_fn=lambda token: 6 if token==USDG else 18,
              price_fn=lambda token: .997 if token==USDG else None)
    assert result is not None
    assert result['source_size_usd']==pytest.approx(598.918556*.997)
    assert result['token_delta']==pytest.approx(int('1459523557ed8e68ff5eec',16)/10**18)
    assert result['source_price']==pytest.approx(result['source_size_usd']/result['token_delta'])


@pytest.mark.parametrize('value',[None,0,-1,float('nan'),float('inf')])
def test_unknown_or_invalid_payment_price_cannot_emit_a_detection(value):
    fn=getattr(watch,'price_relayer_purchase',lambda *a,**kw:None)
    assert fn(classify(),decimals_fn=lambda token:6,price_fn=lambda token:value) is None


def test_relayer_code_identity_is_required_before_the_shared_reader_accepts():
    fn=getattr(watch,'read_relayer_purchase',lambda *a,**kw:None)
    assert fn(FIXTURE['receipt'],FIXTURE['transaction'],FIXTURE['wallet'],chain='4663',
              read_fn=lambda method,params:'0x00') is None


def test_verifier_uses_the_same_relayer_classifier(monkeypatch):
    from sniper.services import axiom_address_verify as av
    def shared(receipt, transaction, wallet, **kwargs):
        return watch.relayer_purchase_from_receipt(receipt,transaction,wallet,
                    chain=kwargs['chain'],contracts_verified=True)
    monkeypatch.setattr(watch,'read_relayer_purchase',shared)
    def call(_url,method,params):
        return (FIXTURE['receipt'] if method=='eth_getTransactionReceipt' else FIXTURE['transaction']),None
    logs=[l for l in FIXTURE['receipt']['logs'] if l['address']==FIXTURE['mint']]
    evidence={}
    assert av._evm_spend_verdict(call,'offline',FIXTURE['wallet'],av._chain_quote_token('4663'),
                logs,0,lambda _:None,mint=FIXTURE['mint'],chain='4663',evidence=evidence)==av.BOUGHT
    assert evidence['purchase']['payment_token']==USDG
    assert evidence['purchase']['beneficiary']==FIXTURE['wallet']


def test_watcher_uses_the_same_classification_and_payment_price(monkeypatch):
    import importlib.util
    spec=importlib.util.spec_from_file_location('relay_watch',Path('scripts/research/onchain_evm_wallet_watch.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module._configure('4663')
    monkeypatch.setattr(watch,'read_relayer_purchase',lambda receipt,transaction,wallet,**kw:
         watch.relayer_purchase_from_receipt(receipt,transaction,wallet,chain=kw['chain'],contracts_verified=True))
    def read(method,params):
        return FIXTURE['transaction'] if method=='eth_getTransactionByHash' else FIXTURE['receipt']
    result=module.push_detection('offline',{'transactionHash':FIXTURE['transaction']['hash']},
           {FIXTURE['wallet']:('source1','trader1')},decimals_cache=FIXTURE['decimals'].copy(),
           quote_usd=2500,now='2026-09-16T12:00:00+00:00',read_fn=read,
           payment_price_fn=lambda token: .997 if token==USDG else None)
    assert result is not None
    assert result['source_user_id']=='source1'
    assert result['buy']['source_size_usd']==pytest.approx(598.918556*.997)


@pytest.mark.parametrize('anchors,distinct,verdict',[(3,3,'axiom_confirmed'),(2,3,'unverified'),(3,1,'unverified')])
def test_relayer_identity_needs_three_distinct_anchored_swaps(anchors,distinct,verdict):
    from test_axiom_address_verify import _conn,_buys,NOW
    from sniper.services import axiom_address_verify as av
    events=list(_buys(3,chain='4663'))
    if distinct==1:events=[(uid,'ONE',side,chain,at,source) for uid,mint,side,chain,at,source in events]
    c=_conn(directory_rows=[('trader','Trader',FIXTURE['wallet'],'4663',NOW)],
            wallet_rows=[('u1','trader',1,'4663')],events=events,
            entries=[(f'e{i}',1,f'OURTX{i}') for i in range(anchors)])
    def read(chain,wallet,mint,**kw):
        read.last_purchase=dict(classify(),mint=mint)
        return av.BOUGHT
    result=av.verify_candidates(c,rpc_read_fn=read,now_iso=lambda:NOW)[0]
    assert result['verify_status']==verdict
    detail=json.loads(c.execute('SELECT detail FROM axiom_verified_addresses').fetchone()[0])
    assert detail['proof_version']=='relay-kyber-linked-v1'
    eligible=av.confirmed_addresses(c)
    assert bool(eligible)==(verdict=='axiom_confirmed')
    if eligible:
        c.execute("UPDATE axiom_verified_addresses SET detail=?",(json.dumps({'proof_version':'relayer-settled-v1'}),))
        assert av.confirmed_addresses(c)=={}
    c.close()


def test_shared_reader_checks_every_pinned_contract(monkeypatch):
    calls=[]
    def read(method,params):
        calls.append((method,params))
        return hex(4663) if method=='eth_chainId' else FIXTURE['contract_code_latest'][params[0]]
    result=watch.read_relayer_purchase(FIXTURE['receipt'],FIXTURE['transaction'],FIXTURE['wallet'],
                                      chain='4663',read_fn=read)
    assert result and result['payment_atomic']==598918556
    assert {params[0] for method,params in calls if method=='eth_getCode'}=={PROXY,ROUTER,KYBER}
    assert all(params[1]==FIXTURE['receipt']['blockNumber'] for method,params in calls if method=='eth_getCode')


def test_failed_historical_code_read_cannot_fall_back_to_current_code():
    calls=[]
    def read(method,params):
        calls.append((method,params))
        if method=='eth_chainId':return hex(4663)
        raise RuntimeError('historical state unavailable')
    evidence={}
    assert watch.read_relayer_purchase(FIXTURE['receipt'],FIXTURE['transaction'],FIXTURE['wallet'],
                 chain='4663',read_fn=read,evidence=evidence) is None
    assert evidence['reason']=='contract_code_unavailable'
    assert all('latest' not in params for method,params in calls)


def test_mixed_old_matches_do_not_complete_the_relayer_proof():
    from test_axiom_address_verify import _conn,_buys,NOW
    from sniper.services import axiom_address_verify as av
    c=_conn(directory_rows=[('trader','Trader',FIXTURE['wallet'],'4663',NOW)],
            wallet_rows=[('u1','trader',1,'4663')],events=_buys(3,chain='4663'),
            entries=[(f'e{i}',1,f'OURTX{i}') for i in range(3)])
    calls=[]
    def read(chain,wallet,mint,**kw):
        calls.append(mint)
        read.last_purchase=dict(classify(),mint=mint) if len(calls)==1 else None
        return av.BOUGHT
    result=av.verify_candidates(c,rpc_read_fn=read,now_iso=lambda:NOW)[0]
    assert result['verify_status']==av.UNVERIFIED
    assert av.confirmed_addresses(c)=={}
    c.close()


@pytest.mark.parametrize('payload,expected', [
    ({'error': [], 'result': {'USDGUSD': {'b': ['0.997']}}}, .997),
    ({'error': ['unavailable'], 'result': {}}, None),
])
def test_payment_price_uses_usd_market_and_caches_unknown(monkeypatch, payload, expected):
    from scripts.research import onchain_evm_wallet_watch as module
    from sniper.execution import evm_wallet
    module._configure('4663')
    monkeypatch.setattr(module, '_PAYMENT_PRICES', {})
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.0)
    calls = []
    def http(method, url, **kwargs):
        calls.append(url)
        assert url == 'https://api.kraken.com/0/public/Ticker?pair=USDGUSD'
        return 200, payload, ''
    monkeypatch.setattr(evm_wallet, '_http', http)
    assert module._payment_usd_price(USDG, 2500) == expected
    assert module._payment_usd_price(USDG, 2500) == expected
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [('hash', 7), ('input', []), ('to', None)])
def test_malformed_rpc_transaction_remains_unknown(field, value):
    data = copy.deepcopy(FIXTURE)
    data['transaction'][field] = value
    assert classify(data) is None
