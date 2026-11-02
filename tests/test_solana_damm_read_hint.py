"""WDYT's processed DAMM CPI supplies read targets, never funded source proof."""
import base64
import copy
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import base58
import pytest
from solders.pubkey import Pubkey

from sniper.execution import meteora_damm_v2_buy as D
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services.memecoin_shyft_signal import routed_buys
from sniper.settings import memecoin as cfg

FIX = json.loads((Path(__file__).parent / 'fixtures/memecoin/wdyt-source-259kthyl.json').read_text())
POOL_FIX = json.loads((Path(__file__).parent / 'fixtures/memecoin/damm_v2_sol_pools.json').read_text())['pools'][0]
COIN = 'wdysfTqUsJ8qAVB1796BXgGisThoBzu1keQmdgswdyt'
OWNER = '6xmMW5JPSEfeRuNdkxixHWsm4Sf57SdXybA3BdwZzrM1'
POOL = 'Bu65yUfdrxgwSnXXvrKxeNzmcjaSCsAEn5JtPMWYZVQG'
VAULT_A = '4jinop2RVXF6p3aQHd3Ci8mjVaBTWnB7kuJAPPho7Ywy'
VAULT_B = '9LMqiLFhZyGw5Mx2ouqLoxRxFna85JWwZthhC4XKsHDy'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'


def wire_receipt(fixture=FIX):
    """Encode the captured compiled v1 receipt without manufacturing instruction identities."""
    u = pb.SubscribeUpdate()
    u.transaction.slot = fixture['slot']
    info = u.transaction.transaction
    info.signature = base58.b58decode(fixture['transaction']['signatures'][0])
    info.transaction.signatures.extend(base58.b58decode(s) for s in fixture['transaction']['signatures'])
    source = fixture['transaction']['message']
    message = info.transaction.message
    for target, key in (('num_required_signatures', 'numRequiredSignatures'),
                        ('num_readonly_signed_accounts', 'numReadonlySignedAccounts'),
                        ('num_readonly_unsigned_accounts', 'numReadonlyUnsignedAccounts')):
        setattr(message.header, target, source['header'][key])
    message.account_keys.extend(base58.b58decode(k) for k in source['accountKeys'])
    message.recent_blockhash = base58.b58decode(source['recentBlockhash'])
    message.versioned = True
    message.config.priority_fee = source['transactionConfig']['priorityFee']
    message.config.compute_unit_limit = source['transactionConfig']['computeUnitLimit']
    def compiled(src, dst):
        dst.program_id_index = src['programIdIndex']
        dst.accounts = bytes(src['accounts'])
        dst.data = base58.b58decode(src['data'])
        if hasattr(dst, 'stack_height') and src.get('stackHeight') is not None:
            dst.stack_height = src['stackHeight']
    for ix in source['instructions']:
        compiled(ix, message.instructions.add())
    meta = fixture['meta']
    info.meta.fee = meta['fee']
    info.meta.pre_balances.extend(meta['preBalances'])
    info.meta.post_balances.extend(meta['postBalances'])
    info.meta.log_messages.extend(meta['logMessages'])
    for src in meta['innerInstructions']:
        group = info.meta.inner_instructions.add(index=src['index'])
        for ix in src['instructions']:
            compiled(ix, group.instructions.add())
    for attr, key in (('pre_token_balances', 'preTokenBalances'), ('post_token_balances', 'postTokenBalances')):
        for b in meta[key]:
            row = getattr(info.meta, attr).add(account_index=b['accountIndex'], owner=b['owner'],
                mint=b['mint'], program_id=b['programId'])
            row.ui_token_amount.amount = b['uiTokenAmount']['amount']
            row.ui_token_amount.decimals = b['uiTokenAmount']['decimals']
    return u


def decode(update=None):
    update = update or wire_receipt()
    return routed_buys(update, {OWNER: ('uid', 'starcatcher444')}, recv_monotonic=10.0,
        recv_utc='original-receipt', head_slot=456215414, head_mono=10.0,
        signal_feed='shyft:grpc.ams.shyft.to')


def test_wdyt_processed_metadata_attaches_exact_read_target_without_changing_source_clock(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_DAMM_V2', 'live')
    buys = decode()
    assert len(buys) == 1 and buys[0].mint == COIN
    buy = buys[0]
    hint = getattr(buy, 'damm_read_hint', None)
    assert hint is not None, 'the exact processed DAMM CPI was discarded and required a mint scan'
    assert (hint.pool, hint.mint_a, hint.mint_b, hint.vault_a, hint.vault_b) == (
        POOL, COIN, D.WSOL_MINT, VAULT_A, VAULT_B)
    assert (hint.owner, hint.coin) == (OWNER, COIN)
    assert (buy.recv_monotonic, buy.recv_utc, buy.slot) == (10.0, 'original-receipt', 456215414)
    assert buy.usdc_spent_atomic == 843129689 and buy.token_delta_atomic == 6244657740903
    assert getattr(buy, 'source_initiation', None) is None


def canonical_accounts():
    raw = bytearray(base64.b64decode(POOL_FIX['pool_account']['data'][0]))
    for offset, key in ((D.OFF_MINT_A, COIN), (D.OFF_MINT_B, D.WSOL_MINT),
                        (D.OFF_VAULT_A, VAULT_A), (D.OFF_VAULT_B, VAULT_B)):
        raw[offset:offset + 32] = bytes(Pubkey.from_string(key))
    state = D.parse_pool(base64.b64decode(POOL_FIX['pool_account']['data'][0]))
    mints = dict(zip((state['mint_a'], state['mint_b']), POOL_FIX['mints']))
    return {
        POOL: {'owner': D.PROGRAM, 'data': [base64.b64encode(raw).decode(), 'base64']},
        COIN: copy.deepcopy(mints[POOL_FIX['coin']]), D.WSOL_MINT: copy.deepcopy(mints[D.WSOL_MINT]),
        VAULT_A: {'owner': TOKEN, 'data': {'parsed': {'type': 'account', 'info': {
            'mint': COIN, 'owner': D.POOL_AUTHORITY, 'tokenAmount': {'amount': '1000000000000'}}}}},
        VAULT_B: {'owner': TOKEN, 'data': {'parsed': {'type': 'account', 'info': {
            'mint': D.WSOL_MINT, 'owner': D.POOL_AUTHORITY, 'tokenAmount': {'amount': '200000000000'}}}}},
    }


def test_wdyt_exact_hint_learns_verified_pool_in_one_batched_read(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_DAMM_V2', 'live')
    hint = getattr(decode()[0], 'damm_read_hint', None)
    assert hint is not None, 'the one-read learner has no exact pool hint'
    accounts = canonical_accounts()
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        assert method == 'getMultipleAccounts', 'an exact pool must not perform program-wide scans'
        return {'value': [accounts[key] for key in params[0]]}
    cache = D.DammV2PoolCache(rpc=None)
    learner = D.DammV2MintLearner(cache, rpc, sol_usd_fn=lambda: 150.0, min_depth_usd=2500.0)
    assert learner.learn(COIN, read_hint=hint) is True
    facts = cache.get(COIN)
    assert facts['pool'] == POOL and facts['depth_usd'] == 30000.0
    assert facts['coin_facts'] is not None and facts['quote_facts'] == (9, 0, 0)
    assert len(calls) == 1 and set(calls[0][1][0]) == {POOL, COIN, D.WSOL_MINT, VAULT_A, VAULT_B}


def _swap(tx):
    keys = D._account_keys(tx)
    return next(ix for group in tx['meta']['innerInstructions'] for ix in group['instructions']
        if keys[ix['programIdIndex']] == D.PROGRAM
        and base58.b58decode(ix['data'])[:8] == D.SWAP2_DISCRIMINATOR)


@pytest.mark.parametrize('bad', ['actor', 'authority', 'program', 'event', 'referral', 'token_program',
    'unknown_mint', 'layout', 'mode', 'unsigned_owner', 'duplicate_swap', 'wrong_index', 'failed'])
def test_wrong_or_ambiguous_source_cpi_cannot_supply_a_read_hint(bad):
    tx = copy.deepcopy(FIX)
    ix = _swap(tx)
    keys = D._account_keys(tx)
    replacements = {'actor': 8, 'authority': 0, 'event': 12, 'referral': 11, 'token_program': 9}
    if bad in replacements:
        ix['accounts'][replacements[bad]] = keys.index('11111111111111111111111111111111')
    elif bad == 'program': ix['programIdIndex'] = keys.index(TOKEN)
    elif bad == 'unknown_mint': ix['accounts'][6] = keys.index(TOKEN)
    elif bad == 'layout': ix['accounts'].pop()
    elif bad == 'mode': ix['data'] = base58.b58encode(base58.b58decode(ix['data'])[:-1] + b'\x00').decode()
    elif bad == 'unsigned_owner': tx['transaction']['message']['header']['numRequiredSignatures'] = 0
    elif bad == 'duplicate_swap': tx['meta']['innerInstructions'][0]['instructions'].append(copy.deepcopy(ix))
    elif bad == 'wrong_index': ix['accounts'][1] = -1
    elif bad == 'failed': tx['meta']['err'] = {'InstructionError': [0, 'Custom']}
    assert D.pool_read_hint(tx, COIN, OWNER) is None


def test_hint_is_frozen_and_ambiguous_processed_swap_keeps_ordinary_routed_candidate(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_DAMM_V2', 'live')
    hint = decode()[0].damm_read_hint
    with pytest.raises(FrozenInstanceError):
        hint.pool = COIN
    tx = copy.deepcopy(FIX)
    tx['meta']['innerInstructions'][0]['instructions'].append(copy.deepcopy(_swap(tx)))
    buys = decode(wire_receipt(tx))
    assert len(buys) == 1 and buys[0].mint == COIN and buys[0].damm_read_hint is None
    assert buys[0].recv_monotonic == 10.0


@pytest.mark.parametrize('bad', ['timeout', 'rate_limit', 'partial', 'null_pool', 'pool_program',
    'pool_mint', 'pool_vault', 'layout', 'mint_program', 'vault_program', 'vault_mint',
    'vault_authority', 'vault_amount', 'quote_decimals', 'shallow', 'sol_usd_unknown'])
def test_bad_canonical_response_is_not_adopted_or_retried(bad):
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    accounts = canonical_accounts()
    if bad == 'null_pool': accounts[POOL] = None
    elif bad == 'pool_program': accounts[POOL]['owner'] = TOKEN
    elif bad in ('pool_mint', 'pool_vault', 'layout'):
        raw = bytearray(base64.b64decode(accounts[POOL]['data'][0]))
        if bad == 'layout': raw[0] ^= 1
        else:
            offset = D.OFF_MINT_A if bad == 'pool_mint' else D.OFF_VAULT_A
            raw[offset:offset + 32] = bytes(Pubkey.from_string(TOKEN))
        accounts[POOL]['data'][0] = base64.b64encode(raw).decode()
    elif bad == 'mint_program': accounts[COIN]['owner'] = D.PROGRAM
    elif bad == 'vault_program': accounts[VAULT_B]['owner'] = D.PROGRAM
    elif bad in ('vault_mint', 'vault_authority', 'vault_amount', 'shallow'):
        info = accounts[VAULT_B]['data']['parsed']['info']
        if bad == 'vault_mint': info['mint'] = COIN
        elif bad == 'vault_authority': info['owner'] = OWNER
        elif bad == 'vault_amount': info['tokenAmount']['amount'] = '-1'
        elif bad == 'shallow': info['tokenAmount']['amount'] = '1000000000'
    elif bad == 'quote_decimals': accounts[D.WSOL_MINT]['data']['parsed']['info']['decimals'] = 6
    calls = []
    def rpc(method, params):
        calls.append(method)
        if bad == 'timeout': raise TimeoutError('provider timeout')
        if bad == 'rate_limit': return {'error': {'code': -32429, 'message': 'quota'}}
        values = [accounts[k] for k in params[0]]
        return {'value': values[:-1] if bad == 'partial' else values}
    cache = D.DammV2PoolCache(rpc=None)
    learner = D.DammV2MintLearner(cache, rpc,
        sol_usd_fn=lambda: None if bad == 'sol_usd_unknown' else 150.0, min_depth_usd=2500.0)
    assert learner.learn(COIN, read_hint=hint) is False
    assert cache.get(COIN) is None
    assert calls == ['getMultipleAccounts']


def test_no_hint_and_unsupported_quote_keep_existing_bounded_mint_scan():
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    for read_hint in (None, replace(hint, mint_b=TOKEN)):
        calls = []
        def rpc(method, params):
            calls.append(method)
            return []
        cache = D.DammV2PoolCache(rpc=None)
        learner = D.DammV2MintLearner(cache, rpc, sol_usd_fn=lambda: 150.0, min_depth_usd=2500.0)
        assert learner.learn(COIN, read_hint=read_hint) is False and cache.get(COIN) is None
        assert calls == ['getProgramAccounts', 'getProgramAccounts']


@pytest.mark.parametrize('quote,coin_side', [(D.WSOL_MINT, 'b'), (D.USDC_MINT, 'a')])
def test_canonical_depth_uses_quote_units_on_either_mint_side(monkeypatch, quote, coin_side):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', ('meteora_damm_v2',))
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    accounts = canonical_accounts()
    if coin_side == 'b':
        hint = replace(hint, mint_a=D.WSOL_MINT, mint_b=COIN)
    else:
        hint = replace(hint, mint_b=D.USDC_MINT)
        accounts[D.USDC_MINT] = accounts.pop(D.WSOL_MINT)
        accounts[D.USDC_MINT]['data']['parsed']['info']['decimals'] = 6
    raw = bytearray(base64.b64decode(accounts[POOL]['data'][0]))
    for offset, key in ((D.OFF_MINT_A, hint.mint_a), (D.OFF_MINT_B, hint.mint_b)):
        raw[offset:offset + 32] = bytes(Pubkey.from_string(key))
    accounts[POOL]['data'][0] = base64.b64encode(raw).decode()
    for vault, mint in ((VAULT_A, hint.mint_a), (VAULT_B, hint.mint_b)):
        info = accounts[vault]['data']['parsed']['info']
        info['mint'] = mint
        info['tokenAmount']['amount'] = ('200000000000' if quote == D.WSOL_MINT else '30000000000')
    def rpc(method, params):
        assert method == 'getMultipleAccounts'
        return {'value': [accounts[k] for k in params[0]]}
    cache = D.DammV2PoolCache(rpc=None)
    learner = D.DammV2MintLearner(cache, rpc, sol_usd_fn=lambda: 150.0, min_depth_usd=2500.0)
    assert learner.learn(COIN, read_hint=hint) is True
    assert cache.get(COIN)['quote'] == quote and cache.get(COIN)['depth_usd'] == 30000.0
