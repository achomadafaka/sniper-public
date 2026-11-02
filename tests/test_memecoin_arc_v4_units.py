"""ARC native and ERC-20 USDC share value but use different atomic units."""
from sniper.execution import evm_v4_quote as v
from sniper.execution.evm_wallet import CHAIN_QUOTE
from sniper.settings import memecoin as cfg

ARC='5042';USDC=CHAIN_QUOTE[ARC]['token'];MINT='0x'+'ee'*20;NATIVE='0x'+'00'*20
KEY={'currency0':NATIVE,'currency1':MINT,'fee':3000,'tick_spacing':60,'hooks':'0x'+'ab'*20}


def setup_pool(monkeypatch):
    monkeypatch.setattr(v,'_quote_pools_for_mint',lambda *a,**k:['pool'])
    monkeypatch.setattr(v,'_pool_key',lambda *a,**k:KEY)


def test_native_pool_buy_converts_six_to_eighteen(monkeypatch):
    setup_pool(monkeypatch);calls=[]
    monkeypatch.setattr(v,'_quote_single',lambda chain,key,zf,amount,*a,**k:calls.append(amount) or 500)
    out=v.v4_quote_amount_out(ARC,USDC,MINT,20_000_000,rpc_url='unused')
    assert calls==[20*10**18] and out['amount_out']==500


def test_native_pool_sell_returns_six_decimal_usdc(monkeypatch):
    setup_pool(monkeypatch)
    monkeypatch.setattr(v,'_quote_single',lambda *a,**k:25*10**18)
    out=v.v4_quote_amount_out(ARC,MINT,USDC,500,rpc_url='unused')
    assert out['amount_out']==25_000_000


def test_hooked_native_buy_never_calls_withdraw(monkeypatch):
    setup_pool(monkeypatch)
    monkeypatch.setattr(v,'_quote_single',lambda *a,**k:500)
    q=v.dex_v4_quote(ARC,USDC,MINT,20_000_000,'0x'+'11'*20,rpc_url='unused',now_fn=lambda:1)
    assert q['pre_transactions']==[]
    assert int(q['transaction']['value'],16)==20*10**18


def test_native_sell_never_wraps_usdc(monkeypatch):
    setup_pool(monkeypatch);commands=[]
    monkeypatch.setattr(v,'_quote_single',lambda *a,**k:25*10**18)
    monkeypatch.setattr(v,'_permit2_input',lambda *a,**k:'00')
    monkeypatch.setattr(v,'_ur_execute_data',lambda c,*a:commands.append(c) or '0x')
    q=v.dex_v4_quote(ARC,MINT,USDC,500,'0x'+'11'*20,rpc_url='unused',acct=object(),now_fn=lambda:1)
    assert q['buyAmount']=='25000000'
    assert v._CMD_WRAP_ETH not in commands[0]


def test_hooked_erc20_pool_uses_six_decimals_and_native_prefund(monkeypatch):
    setup_pool(monkeypatch);key=dict(KEY,currency0=USDC);amounts=[];commands=[]
    monkeypatch.setattr(cfg,'MEMECOIN_EVM_V4_WETH_CURRENCY',False)
    monkeypatch.setattr(v,'_pool_key',lambda *a,**k:key)
    monkeypatch.setattr(v,'_quote_single',lambda c,k,z,amount,*a,**kw:amounts.append(amount) or 500)
    monkeypatch.setattr(v,'_ur_execute_data',lambda c,*a:commands.append(c) or '0x')
    q=v.dex_v4_quote(ARC,USDC,MINT,20_000_000,'0x'+'11'*20,rpc_url='unused',now_fn=lambda:1)
    assert amounts==[20_000_000]
    assert q['pre_transactions']==[]
    assert int(q['transaction']['value'],16)==20*10**18
    assert commands==[bytes([v._CMD_V4_SWAP])]


def test_native_exit_encodes_eighteen_decimal_minimum(monkeypatch):
    setup_pool(monkeypatch);minimums=[]
    monkeypatch.setattr(v,'_quote_single',lambda *a,**k:25*10**18)
    monkeypatch.setattr(v,'_permit2_input',lambda *a,**k:'00')
    monkeypatch.setattr(v,'_v4_swap_input_path',lambda key,zf,amount,minimum,**kw:minimums.append(minimum) or '00')
    q=v.dex_v4_quote(ARC,MINT,USDC,500,'0x'+'11'*20,rpc_url='unused',acct=object(),now_fn=lambda:1,slippage_bps=100)
    assert q['minBuyAmount']=='24750000'
    assert minimums==[24_750_000*10**12]


def test_scaled_native_minimum_overflow_returns_no_route(monkeypatch):
    setup_pool(monkeypatch)
    monkeypatch.setattr(v,'_quote_single',lambda *a,**k:1<<150)
    monkeypatch.setattr(v,'_permit2_input',lambda *a,**k:'00')
    assert v.dex_v4_quote(ARC,MINT,USDC,500,'0x'+'11'*20,rpc_url='unused',acct=object(),now_fn=lambda:1) is None
