"""Source first-buy evidence is exact transaction-touched inventory, never a wallet snapshot."""
import base64
import copy
import json
import struct
from pathlib import Path

import base58
import pytest

from sniper.services import memecoin_shyft_signal as shyft
from sniper.services import memecoin_slot_signal as slot

FRAMES = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_signal_frames.json').read_text())
OWNER = '2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf'
MINT = 'C1P7oNjsUyQ8XJpYJ21waw6tc1dJtFxNgTuH37bGpump'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
ACCOUNT = base58.b58encode(bytes([2]) * 32).decode()
SECOND = base58.b58encode(bytes([3]) * 32).decode()
OTHER_OWNER = base58.b58encode(bytes([4]) * 32).decode()
OTHER_MINT = base58.b58encode(bytes([5]) * 32).decode()
POOL = base58.b58encode(bytes([6]) * 32).decode()
OWNERS = {OWNER: ('uid', 'buyer')}


def amm_frame(pre):
    raw = bytearray(248)
    raw[:8] = slot._AMM_BUY
    struct.pack_into('<7Q', raw, 16, 15, 20, pre, 100, 1000, 2000, 10)
    for start, key in ((120, POOL), (152, OWNER), (184, ACCOUNT), (216, SECOND)):
        raw[start:start + 32] = base58.b58decode(key)
    frame = copy.deepcopy(FRAMES['curve_buy'])
    frame['params']['result']['value']['logs'] = [
        f'Program {slot.PUMP_AMM_PROGRAM} invoke [1]',
        'Program data: ' + base64.b64encode(raw).decode(),
        f'Program {slot.PUMP_AMM_PROGRAM} success']
    return frame


def wire(frame=None):
    frame = (frame or FRAMES['curve_buy'])['params']['result']
    update = shyft.pb.SubscribeUpdate()
    update.transaction.slot = frame['context']['slot']
    info = update.transaction.transaction
    info.signature = base58.b58decode(frame['value']['signature'])
    info.transaction.signatures.append(info.signature)
    msg = info.transaction.message
    msg.header.num_required_signatures = 1
    msg.account_keys.extend(base58.b58decode(key) for key in
        (OWNER, ACCOUNT, MINT, TOKEN, SECOND, OTHER_OWNER, OTHER_MINT))
    info.meta.log_messages.extend(frame['value']['logs'])
    info.meta.pre_balances.extend([100] * len(msg.account_keys))
    info.meta.post_balances.extend([100] * len(msg.account_keys))
    return update


def balance(update, *, index=1, owner=OWNER, mint=MINT, pre='0', post='15', initialize=False):
    info = update.transaction.transaction
    for name, amount in (('pre_token_balances', pre), ('post_token_balances', post)):
        if amount is not None:
            row = getattr(info.meta, name).add(account_index=index, owner=owner, mint=mint, program_id=TOKEN)
            row.ui_token_amount.amount = str(amount)
            row.ui_token_amount.decimals = 6
    if initialize:
        info.meta.pre_balances[index] = 0
        info.transaction.message.instructions.add(program_id_index=3, accounts=bytes([index, 2]),
            data=bytes([18]) + base58.b58decode(owner))
    return update


def decode(update):
    buys = shyft.decode_update(update, OWNERS, recv_monotonic=0, recv_utc='receipt',
        head_slot=update.transaction.slot, head_mono=0)
    assert len(buys) == 1
    return buys[0]


@pytest.mark.parametrize('pre', [0, 1, 2**63 + 7])
def test_pumpswap_event_preserves_exact_participating_base_account_pre(pre):
    buy = slot.decode_owner_buy(amm_frame(pre), OWNER, base58.b58decode(OWNER), 'uid',
        recv_monotonic=0, recv_utc='receipt')
    assert buy.source_pre_atomic == pre
    assert type(buy.source_pre_atomic) is int


def test_curve_log_and_missing_receipt_balances_stay_unknown():
    buy = slot.decode_owner_buy(FRAMES['curve_buy'], OWNER, base58.b58decode(OWNER), 'uid',
        recv_monotonic=0, recv_utc='receipt')
    assert buy.source_pre_atomic is None
    assert decode(wire()).source_pre_atomic is None


@pytest.mark.parametrize('pre', [0, 1, 2**63 + 7])
def test_shyft_curve_pre_is_exact_and_preserves_receipt_identity(pre):
    buy = decode(balance(wire(), pre=pre, post=pre + 15))
    assert buy.source_pre_atomic == pre
    assert type(buy.source_pre_atomic) is int
    assert (buy.owner, buy.mint, buy.recv_monotonic, buy.recv_utc) == (OWNER, MINT, 0, 'receipt')
    assert buy.source_initiation is None


def test_shyft_sums_all_touched_source_owner_mint_accounts_only():
    update = balance(wire(), pre=7, post=22)
    balance(update, index=4, pre=13, post=13)
    balance(update, index=5, owner=OTHER_OWNER, pre=100, post=100)
    balance(update, index=6, mint=OTHER_MINT, pre=200, post=200)
    assert decode(update).source_pre_atomic == 20


def test_shyft_new_account_zero_needs_matching_initialization_and_zero_prior_lamports():
    assert decode(balance(wire(), pre=None, initialize=True)).source_pre_atomic == 0


@pytest.mark.parametrize('bad', ['missing_init', 'positive_lamports', 'wrong_init_owner',
    'wrong_init_mint', 'invalid_amount', 'oversize_amount', 'wrong_program', 'wrong_identity',
    'duplicate_row', 'missing_lamports'])
def test_shyft_unvalidated_metadata_never_fabricates_zero(bad):
    update = balance(wire(), pre=None, initialize=True)
    info = update.transaction.transaction
    if bad == 'missing_init':
        del info.transaction.message.instructions[:]
    elif bad == 'positive_lamports':
        info.meta.pre_balances[1] = 100
    elif bad == 'wrong_init_owner':
        info.transaction.message.instructions[0].data = bytes([18]) + base58.b58decode(OTHER_OWNER)
    elif bad == 'wrong_init_mint':
        info.transaction.message.instructions[0].accounts = bytes([1, 6])
    elif bad == 'invalid_amount':
        info.meta.post_token_balances[0].ui_token_amount.amount = '0.0'
    elif bad == 'oversize_amount':
        info.meta.post_token_balances[0].ui_token_amount.amount = str(2**64)
    elif bad == 'wrong_program':
        info.meta.post_token_balances[0].program_id = slot.PUMP_PROGRAM
    elif bad == 'wrong_identity':
        balance(update, pre=1, post=None)
        info.meta.pre_token_balances[0].mint = OTHER_MINT
    elif bad == 'duplicate_row':
        info.meta.post_token_balances.add().CopyFrom(info.meta.post_token_balances[0])
    elif bad == 'missing_lamports':
        del info.meta.pre_balances[:]
    assert decode(update).source_pre_atomic is None


def test_shyft_other_owner_or_mint_rows_do_not_prove_source_zero():
    update = balance(wire(), owner=OTHER_OWNER, pre=0, post=15)
    balance(update, index=4, mint=OTHER_MINT, pre=0, post=15)
    assert decode(update).source_pre_atomic is None


def test_shyft_pumpswap_resolves_event_base_account_and_sums_touched_owner_mint():
    update = balance(wire(amm_frame(0)), pre=0)
    balance(update, index=4, pre=1, post=1)
    assert decode(update).source_pre_atomic == 1


def test_shyft_pumpswap_positive_swap_pre_cannot_be_erased_by_transaction_start_zero():
    assert decode(balance(wire(amm_frame(9)), pre=0)).source_pre_atomic == 9


def test_shyft_pumpswap_event_base_account_owned_by_another_wallet_is_unknown():
    update = balance(wire(amm_frame(0)), owner=OTHER_OWNER, pre=0)
    assert decode(update).source_pre_atomic is None


def test_shyft_routed_buy_keeps_validated_transaction_pre_inventory():
    from test_memecoin_shyft_route_metadata import FIX, OWNER as ROUTED_OWNER, COIN, routed
    expected = sum(int(row['uiTokenAmount']['amount']) for row in FIX['meta']['preTokenBalances']
        if row['owner'] == ROUTED_OWNER and row['mint'] == COIN)
    buy, = routed()
    assert buy.source_pre_atomic == expected


def test_rabbit_synthetic_tape_holder_zero_is_not_source_pre_evidence():
    from test_memecoin_rabbit_signal_stage2 import R, CREATOR, DIRECT, DIRECT_EVENT, pre_trade, curve
    state = dict(pre_trade(), creator=CREATOR, slot=457_074_090,
        token_program=curve.TOKEN_PROGRAM, holder_atomic=0)
    fill = R.source_fill(state, spend=3_000_000)
    candidate = dict(signature=DIRECT['signature'], owner=DIRECT_EVENT['user'], slot=450_780_443,
        recv_monotonic=0, generation=1)
    buy = R.rabbit_slot_buy(candidate, DIRECT_EVENT['mint'], state, fill, source_user_id='uid',
        handle='buyer', signature_basis='leader_sigverify_v1', route='direct', read_ms=0,
        recv_epoch=1760000000, feed='rabbit', now_mono=0, read_basis='tape')
    assert buy.rabbit['holder_atomic'] == 0
    assert buy.source_pre_atomic is None


def receipt(update):
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    tx = transaction_metadata(update, OWNERS, head_slot=update.transaction.slot,
        recv_monotonic=0, head_monotonic=0)
    assert tx is not None
    return tx


@pytest.mark.parametrize('pre', [0, 1, 2**63 + 7])
def test_shared_receipt_extractor_preserves_exact_pre(pre):
    from sniper.services.memecoin_source_valuation import source_pre_atomic
    tx = receipt(balance(wire(), pre=pre, post=pre + 15))
    assert source_pre_atomic(tx, OWNER, MINT) == pre


def test_shared_receipt_new_account_zero_has_executed_initialization_binding():
    from sniper.services.memecoin_source_valuation import source_pre_atomic
    tx = receipt(balance(wire(), pre=None, initialize=True))
    assert source_pre_atomic(tx, OWNER, MINT) == 0


def test_shared_receipt_sums_touched_accounts_and_keeps_absent_owner_mint_unknown():
    from sniper.services.memecoin_source_valuation import source_pre_atomic
    update = balance(wire(), pre=0)
    balance(update, index=4, pre=1, post=1)
    balance(update, index=5, owner=OTHER_OWNER, pre=100, post=100)
    tx = receipt(update)
    assert source_pre_atomic(tx, OWNER, MINT) == 1
    assert source_pre_atomic(tx, OWNER, OTHER_MINT) is None


@pytest.mark.parametrize('bad', ['rows_absent', 'amount_float', 'amount_bool', 'amount_negative',
    'amount_overflow', 'duplicate', 'index_bool', 'wrong_mint', 'wrong_program', 'identity_change',
    'missing_init', 'positive_lamports', 'wrong_init_owner', 'bad_key', 'missing_lamports',
    'failed', 'unbound_transfer'])
def test_shared_receipt_malformed_or_incomplete_is_unknown(bad):
    from sniper.services.memecoin_source_valuation import source_pre_atomic
    tx = receipt(balance(wire(), pre=None, initialize=True))
    meta = tx['meta']
    row = meta['postTokenBalances'][0]
    if bad == 'rows_absent': del meta['preTokenBalances']
    elif bad == 'amount_float': row['uiTokenAmount']['amount'] = 0.0
    elif bad == 'amount_bool': row['uiTokenAmount']['amount'] = False
    elif bad == 'amount_negative': row['uiTokenAmount']['amount'] = '-1'
    elif bad == 'amount_overflow': row['uiTokenAmount']['amount'] = str(2**64)
    elif bad == 'duplicate': meta['postTokenBalances'].append(copy.deepcopy(row))
    elif bad == 'index_bool': row['accountIndex'] = True
    elif bad == 'wrong_mint': row['mint'] = 'wrong'
    elif bad == 'wrong_program': row['programId'] = slot.PUMP_PROGRAM
    elif bad == 'identity_change':
        meta['preTokenBalances'] = [dict(copy.deepcopy(row), owner=OTHER_OWNER)]
    elif bad == 'missing_init': tx['transaction']['message']['instructions'] = []
    elif bad == 'positive_lamports': meta['preBalances'][1] = 1
    elif bad == 'wrong_init_owner':
        tx['transaction']['message']['instructions'][0]['parsed']['info']['owner'] = OTHER_OWNER
    elif bad == 'bad_key': tx['transaction']['message']['accountKeys'][1] = 'wrong'
    elif bad == 'missing_lamports': del meta['preBalances']
    elif bad == 'failed': meta['err'] = {'InstructionError': [0, 'InvalidAccountData']}
    elif bad == 'unbound_transfer':
        tx['transaction']['message']['instructions'].append(dict(programId=TOKEN, program='spl-token',
            parsed=dict(type='transfer', info=dict(source=SECOND, destination=ACCOUNT,
                authority=OWNER, amount='1'))))
    assert source_pre_atomic(tx, OWNER, MINT) is None


@pytest.mark.parametrize('kind', ['direct', 'routed'])
def test_source_valuation_keeps_exact_pre_even_without_usd_price(kind):
    from sniper.services.memecoin_source_valuation import value_source_buy
    if kind == 'direct':
        from test_onchain_source_valuation import receipt as direct_receipt
        tx, owner = direct_receipt('notdecu-later')
    else:
        from test_route_source_valuation import _tx, OWNER as owner
        tx = _tx()
    facts = value_source_buy(tx, owner, sol_usd=None, now=105)
    expected = sum(int(row['uiTokenAmount']['amount']) for row in tx['meta']['preTokenBalances']
        if row['owner'] == owner and row['mint'] == facts['mint'])
    assert expected > 0
    assert facts['source_pre_atomic'] == expected
    assert facts['source_size_usd'] is None


def test_source_valuation_cannot_erase_positive_swap_pre_with_transaction_start_zero():
    from sniper.services.memecoin_source_valuation import value_source_buy
    from test_onchain_source_valuation import receipt as direct_receipt
    tx, owner = direct_receipt('untaxxable')
    original = value_source_buy(tx, owner, sol_usd=None, now=105)
    pre = next(row for row in tx['meta']['preTokenBalances']
        if row['owner'] == owner and row['mint'] == original['mint'])
    post = next(row for row in tx['meta']['postTokenBalances'] if row['accountIndex'] == pre['accountIndex'])
    post['uiTokenAmount']['amount'] = str(int(post['uiTokenAmount']['amount']) - int(pre['uiTokenAmount']['amount']))
    pre['uiTokenAmount']['amount'] = '0'
    assert value_source_buy(tx, owner, sol_usd=None, now=105)['source_pre_atomic'] == original['source_pre_atomic']


@pytest.mark.parametrize('pre', [0, 7])
@pytest.mark.parametrize('freeze', [b'\x00', b'\x01' + bytes([9]) * 32])
def test_direct_mint_creation_keeps_inventory_without_widening_payment_parser(pre, freeze):
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    update = balance(wire(), pre=pre, post=pre + 15)
    update.transaction.transaction.transaction.message.instructions.add(
        program_id_index=3, accounts=bytes([2]),
        data=bytes([20, 6]) + base58.b58decode(OWNER) + freeze)
    why = []
    assert transaction_metadata(update, OWNERS, head_slot=update.transaction.slot,
        recv_monotonic=0, head_monotonic=0, why=why) is None
    assert why == ['token_instruction_unsupported:20']
    buy = decode(update)
    assert buy.source_pre_atomic == pre
    assert buy.source_initiation is None


def _creation_inventory_wire():
    import hashlib
    from test_memecoin_shyft_confirmed import wire_receipt
    fixture = json.loads((Path(__file__).parent /
        'fixtures/memecoin/watcher_no_row_xsc9_creation_20261016.json').read_text())
    tx = fixture['transaction']
    update = wire_receipt(tx)
    keys = [row['pubkey'] for row in tx['transaction']['message']['accountKeys']]
    for raw_group, group in zip(update.transaction.transaction.meta.inner_instructions,
                                tx['meta']['innerInstructions']):
        for raw, ix in zip(raw_group.instructions, group['instructions']):
            if raw.data != b'\xff':
                continue
            kind, info = ix['parsed']['type'], ix['parsed']['info']
            if kind == 'initializeMetadataPointer':
                names, data = ['mint'], bytes([39, 0]) + bytes(32) + base58.b58decode(info['metadataAddress'])
            elif kind == 'initializeMint2':
                names, data = ['mint'], bytes([20, info['decimals']]) + base58.b58decode(info['mintAuthority']) + b'\x00'
            elif kind == 'initializeTokenMetadata':
                names = ['metadata', 'updateAuthority', 'mint', 'mintAuthority']
                data = hashlib.sha256(b'spl_token_metadata_interface:initialize_account').digest()[:8]
                for field in ('name', 'symbol', 'uri'):
                    value = info[field].encode()
                    data += struct.pack('<I', len(value)) + value
            elif kind == 'updateTokenMetadataAuthority':
                names = ['metadata', 'updateAuthority']
                data = hashlib.sha256(b'spl_token_metadata_interface:update_the_authority').digest()[:8] + bytes(32)
            elif kind == 'mintTo':
                names, data = ['mint', 'account', 'mintAuthority'], b'\x07' + struct.pack('<Q', int(info['amount']))
            elif kind == 'setAuthority':
                assert info['authorityType'] == 'mintTokens' and info['newAuthority'] is None
                names, data = ['mint', 'authority'], bytes([6, 0, 0])
            else:
                raise AssertionError(kind)
            raw.accounts = bytes(keys.index(info[name]) for name in names)
            raw.data = data
    return update, fixture['owner'], keys[1]


def test_real_create_v2_inventory_uses_validated_setup_without_payment_authority():
    from types import SimpleNamespace
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    update, owner, mint = _creation_inventory_wire()
    args = dict(head_slot=update.transaction.slot, recv_monotonic=0, head_monotonic=0)
    owners = {owner: ('creator', 'source')}
    assert transaction_metadata(update, owners, **args) is None
    tx = transaction_metadata(update, owners, inventory_only=True, **args)
    assert tx is not None
    assert shyft._source_pre_atomic(tx, SimpleNamespace(owner=owner, mint=mint,
        source_pre_atomic=None)) == 0


@pytest.mark.parametrize('bad', ['account_owner', 'unknown', 'init_short', 'init_trailing',
    'init_option', 'init_decimals', 'init_wrong_decimals', 'init_token_account',
    'pointer_short', 'pointer_variant', 'metadata_short',
    'metadata_utf8', 'metadata_trailing', 'authority_short', 'mint_to_short',
    'mint_to_unbound', 'mint_to_wrong_mint', 'transfer_unbound'])
def test_inventory_mode_rejects_malformed_or_unbound_token_instructions(bad):
    update = balance(wire(), pre=None, initialize=True)
    info = update.transaction.transaction
    data, accounts = bytes([20, 6]) + bytes(32) + b'\x00', [2]
    if bad == 'account_owner': data, accounts = bytes([6, 2, 0]), [1, 0]
    elif bad == 'unknown': data = bytes([99])
    elif bad == 'init_short': data = data[:-1]
    elif bad == 'init_trailing': data += b'\x00'
    elif bad == 'init_option': data = data[:-1] + b'\x02'
    elif bad == 'init_decimals': data = bytes([20, 19]) + data[2:]
    elif bad == 'init_wrong_decimals': data = bytes([20, 5]) + data[2:]
    elif bad == 'init_token_account': accounts = [1]
    elif bad == 'pointer_short': data = bytes([39, 0]) + bytes(63)
    elif bad == 'pointer_variant': data = bytes([39, 1]) + bytes(64)
    elif bad.startswith('metadata_'):
        import hashlib
        data = hashlib.sha256(b'spl_token_metadata_interface:initialize_account').digest()[:8]
        data += struct.pack('<I', 1) + (b'\xff' if bad == 'metadata_utf8' else b'a') + bytes(8)
        if bad == 'metadata_short': data = data[:-1]
        if bad == 'metadata_trailing': data += b'\x00'
        accounts = [2, 0, 2, 0]
    elif bad == 'authority_short': data, accounts = bytes([6, 0, 1]) + bytes(31), [2, 0]
    elif bad == 'mint_to_short': data, accounts = bytes([7]) + bytes(7), [2, 1, 0]
    elif bad == 'mint_to_unbound': data, accounts = bytes([7]) + struct.pack('<Q', 1), [2, 4, 0]
    elif bad == 'mint_to_wrong_mint': data, accounts = bytes([7]) + struct.pack('<Q', 1), [6, 1, 0]
    elif bad == 'transfer_unbound': data, accounts = bytes([3]) + struct.pack('<Q', 1), [4, 1, 0]
    if bad.startswith(('metadata_', 'pointer_')):
        token2022 = slot.TOKEN_2022_PROGRAM
        info.transaction.message.account_keys[3] = base58.b58decode(token2022)
        info.transaction.message.instructions[0].program_id_index = 3
        info.meta.post_token_balances[0].program_id = token2022
    info.transaction.message.instructions.add(program_id_index=3, accounts=bytes(accounts), data=data)
    assert decode(update).source_pre_atomic is None
