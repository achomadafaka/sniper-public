"""HODL's real Infinity receipt; no RPC, custody keys, or submission."""
import copy
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from sniper.execution import evm_real_seams as S
from sniper.settings import memecoin as cfg

MINT = '0xc242c264efe56109c495b308985f2694593477f8'
PID = '0x004d613e322edc0d65baaae5d12e51b1c1d8bafd6501a293e9a21ab3fde3df10'
OWNER = '0x' + '12' * 20
WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
KEY = {'currency0': '0x' + '00' * 20, 'currency1': MINT,
       'hooks': '0xf02d1ea4d71e87f791b478a289e9faaccd408e1a',
       'pool_manager': '0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b',
       'fee': 0, 'parameters': '0x' + '00' * 29 + 'c80cc1'}
KEY_TYPE = '(address,address,address,address,uint24,bytes32)'

def mod():
    from sniper.execution import evm_pancake_infinity as I
    return I

def key_tuple(key=KEY):
    return (key['currency0'], key['currency1'], key['hooks'], key['pool_manager'],
            key['fee'], bytes.fromhex(key['parameters'][2:]))

def test_bsc_ladder_quotes_infinity_when_existing_venues_miss(monkeypatch):
    I = mod()
    monkeypatch.setattr(I, 'stored_keys', lambda coin: [(PID, KEY)] if coin == MINT else [])
    monkeypatch.setattr(I, 'quote_amount', lambda key, buy, amount, **kw: 1000 if buy else 97)
    monkeypatch.setattr(I, 'build_quote', lambda key, pid, buy, amount, out, taker, **kw:
        {'sellAmount': str(amount), 'buyAmount': str(out), 'route': {'protocol': 'pancake_infinity'}})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_ENTRY_MEASURED_LADDER', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_FLAP_CURVE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_MULTIHOP_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_RELAY_ENABLED', False)
    monkeypatch.setattr(S, '_rung_ladder_chains', lambda _: {'56'})
    rungs = []
    fn = S.venue_quote_with_v2_fallback(lambda *a, **k: None, chain='56', quote_token=WBNB,
        taker=OWNER, enabled=True, v2_quote_fn=lambda *a, **k: None, rpc_url='https://rpc',
        on_rung=rungs.append)
    q = fn(WBNB, MINT, 100)
    assert q and q['route']['protocol'] == 'pancake_infinity', [(r['rung'], r.get('reason'), r.get('blind_reason')) for r in rungs]
    assert q['route']['round_trip_pct'] == pytest.approx(-3)
    assert any(r['rung'] == 'pancake_infinity' and r['hit'] for r in rungs)

@pytest.mark.parametrize('mutation', ['manager', 'hash', 'mint', 'receipt_status'])
def test_receipt_warming_rejects_unbound_keys(mutation):
    I = mod()
    receipt = json.loads((Path(__file__).parent / 'fixtures/memecoin/bsc_hodl_infinity_receipt.json').read_text())
    key = copy.deepcopy(KEY)
    if mutation == 'manager': key['pool_manager'] = OWNER
    if mutation == 'hash': key['parameters'] = '0x' + '00' * 32
    mint = OWNER if mutation == 'mint' else MINT
    if mutation == 'receipt_status': receipt['status'] = '0x0'
    writes = []
    I.warm_receipt('56', receipt, mint, read_fn=lambda *a: '0x' + encode([KEY_TYPE], [key_tuple(key)]).hex(),
                   store_fn=lambda *a: writes.append(a))
    assert writes == []

def test_receipt_warming_reads_only_the_verified_manager():
    I = mod()
    receipt = json.loads((Path(__file__).parent / 'fixtures/memecoin/bsc_hodl_infinity_receipt.json').read_text())
    reads, writes = [], []
    def read(method, params):
        reads.append((method, params))
        return '0x' + encode([KEY_TYPE], [key_tuple()]).hex()
    I.warm_receipt('56', receipt, MINT, read_fn=read, store_fn=lambda *args: writes.append(args))
    assert len(reads) == len(writes) == 1
    assert reads[0][0] == 'eth_call' and reads[0][1][0]['to'] == I.MANAGER
    assert writes[0] == (PID, KEY)

@pytest.mark.parametrize('buy', [True, False])
def test_quoter_uses_infinity_key_abi_and_correct_direction(buy):
    I = mod()
    seen = []
    def rpc(url, method, params, **kw):
        seen.append(params[0])
        signature = 'quoteExactInputSingle(((address,address,address,address,uint24,bytes32),bool,uint128,bytes))'
        assert params[0]['data'][2:10] == keccak(text=signature)[:4].hex()
        p = decode(['(' + KEY_TYPE + ',bool,uint128,bytes)'], bytes.fromhex(params[0]['data'][10:]))[0]
        assert p == (key_tuple(), buy, 100, b'')
        return {'result': '0x' + encode(['uint256', 'uint256'], [123, 100000]).hex()}
    assert I.quote_amount(KEY, buy, 100, rpc_url='https://rpc', rpc_fn=rpc) == 123
    assert seen[0]['to'] == I.QUOTER

@pytest.mark.parametrize('response,kind', [({'error': {'code': -32005, 'message': 'limit exceeded'}}, 'rpc_transport'),
                                         ({'result': '0x'}, 'malformed_response')])
def test_provider_failures_do_not_become_no_route(response, kind):
    I = mod()
    with pytest.raises(I.InfinityReadUnavailable) as e:
        I.quote_amount(KEY, True, 100, rpc_url='https://rpc', rpc_fn=lambda *a, **k: response)
    assert e.value.failure_type == kind

def test_real_buy_encoding_keeps_native_value_and_output_floor():
    I = mod()
    q = I.build_quote(KEY, PID, True, 100, 1000, OWNER, slippage_bps=300, now_fn=lambda: 100)
    assert q['pre_transactions'][0]['to'] == WBNB
    assert q['transaction']['value'] == '0x64' and q['minBuyAmount'] == '970'
    commands, inputs, deadline = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(q['transaction']['data'][10:]))
    assert commands == b'\x10' and deadline > 100
    actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    assert actions == bytes.fromhex('060c0f')
    p = decode(['(' + KEY_TYPE + ',bool,uint128,uint128,bytes)'], params[0])[0]
    assert p == (key_tuple(), True, 100, 970, b'')

@pytest.mark.parametrize('chain', ['4663', '8453', '1'])
def test_other_chains_do_not_use_infinity(chain):
    assert mod().dex_infinity_quote(chain, WBNB, MINT, 100, OWNER) is None

def test_stale_aggregator_rechecks_a_cut_infinity_quote():
    from sniper.execution.evm_swap_executor import stale_aggregator_cut_rungs
    quote = {'_sniper_winner': 'kyber', '_sniper_rungs': [
        {'rung': 'pancake_infinity', 'blind_reason': 'ladder_grace'}]}
    assert stale_aggregator_cut_rungs(quote) == ('pancake_infinity',)

def test_failed_key_read_is_not_cached(monkeypatch):
    from sniper.execution.metadata_cache import BoundedLru
    I = mod()
    monkeypatch.setattr(I, '_KEYS', BoundedLru(4))
    writes, calls = [], []
    monkeypatch.setattr(I, '_store_key', lambda *args: writes.append(args))
    receipt = json.loads((Path(__file__).parent / 'fixtures/memecoin/bsc_hodl_infinity_receipt.json').read_text())
    def read(*args):
        calls.append(args)
        if len(calls) == 1: raise TimeoutError()
        return '0x' + encode([KEY_TYPE], [key_tuple()]).hex()
    assert I.warm_receipt('56', receipt, MINT, read_fn=read) == 0
    assert I.warm_receipt('56', receipt, MINT, read_fn=read) == 1
    assert I.warm_receipt('56', receipt, MINT, read_fn=read) == 1
    assert len(calls) == 2 and len(writes) == 1


@pytest.mark.parametrize('bad', ['overflow', 'invalid'])
def test_complete_pool_reader_rejects_overflow_and_invalid_keys(monkeypatch, bad):
    from sniper.persistence import db_core
    I = mod()
    key = dict(KEY)
    if bad == 'invalid':
        key['parameters'] = '0x' + '00' * 32
    rows = [(PID, json.dumps(key))] * (9 if bad == 'overflow' else 1)
    class Conn:
        def execute(self, query, args):
            assert 'LIMIT 9' in query
            return rows
        def close(self):
            pass
    monkeypatch.setattr(db_core, 'get_readonly_connection', lambda: Conn())
    with pytest.raises(I.InfinityReadUnavailable):
        I.stored_keys(MINT, require_complete=True)


def test_source_reference_does_not_wait_for_infinity_warm(monkeypatch):
    import threading
    from sniper.services import memecoin_evm_post_trade as P
    I = mod()
    entered, release = threading.Event(), threading.Event()
    def slow(*a, **k):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(I, 'warm_receipt', slow)
    receipt = json.loads((Path(__file__).parent / 'fixtures/memecoin/bsc_hodl_infinity_receipt.json').read_text())
    try:
        future = P.submit_receipt_reference('56', '0xhash', MINT, OWNER,
            read_fn=lambda *args: receipt)
        assert future.result(timeout=.5) is not None
        assert entered.wait(.5) and not release.is_set()
    finally:
        release.set()


def test_infinity_sell_permit_uses_deployed_domain_and_wraps_native(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_v4_quote as V
    I = mod()
    acct = Account.create()
    seen = []
    def rpc(url, method, params, **kw):
        seen.append(params[0]['to'])
        return {'result': '0x' + encode(['uint160', 'uint48', 'uint48'], [0, 0, 7]).hex()}
    monkeypatch.setattr(V, '_rpc', rpc)
    q = I.build_quote(KEY, PID, False, 1000, 97, acct.address, acct=acct,
                      rpc_url='https://rpc', slippage_bps=300, now_fn=lambda: 100)
    assert seen == [I.PERMIT2] and q['issues']['allowance']['spender'] == I.PERMIT2
    commands, inputs, _ = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(q['transaction']['data'][10:]))
    assert commands == bytes.fromhex('0a100b') and q['transaction']['value'] == '0x0'
    permit, sig = decode(['((address,uint160,uint48,uint48),address,uint256)', 'bytes'], inputs[0])
    assert permit[0] == (MINT, 1000, 1900, 7) and permit[1] == I.ROUTER
    # The native output is wrapped entirely, to the message sender.
    assert decode(['address', 'uint256'], inputs[2]) == ('0x' + '00'*19 + '01', 1 << 255)
