"""Confirmed CPI shapes and bounded diagnostics; no live calls or captured payload export."""
import copy
import base64
import json
from pathlib import Path

import base58
import pytest
from solders.pubkey import Pubkey

from sniper.services import memecoin_desk_pumpswap as P
from test_desk_ps_loss_scope import ix
from test_memecoin_desk_pumpswap import logs, buy, POOL, ACTOR

FIXTURE = Path(__file__).parent / 'fixtures/memecoin/pumpswap_v2_receipts_20261008.json'
AUTHORITY = str(Pubkey.find_program_address([b'__event_authority'], Pubkey.from_string(P.AMM_PROGRAM))[0])
CPI = bytes.fromhex('e445a52e51cb9a1d')
REASON = 'pumpswap_event_malformed:unsupported_or_inconsistent_buy'


def receipt(name):
    tx = json.loads(FIXTURE.read_text())[name]['tx']
    assert tx['meta']['err'] is None
    message = tx['transaction']['message']
    keys = [k['pubkey'] if isinstance(k, dict) else k for k in message['accountKeys']]
    keys += tx['meta']['loadedAddresses']['writable'] + tx['meta']['loadedAddresses']['readonly']
    instructions = message['instructions'] + [i for group in tx['meta']['innerInstructions'] for i in group['instructions']]
    instructions = [dict(program_id_index=i['programIdIndex'], accounts=i['accounts'],
                         data=base58.b58decode(i['data'])) for i in instructions]
    return tx['meta']['logMessages'], keys, instructions


def detail(ls, keys, instructions, **kwargs):
    return P.loss_details(ls, reason=kwargs.pop('reason', REASON), account_keys=keys,
                          instructions=instructions, inner_complete=kwargs.pop('complete', True))


@pytest.mark.parametrize('name', ['tardigrade', 'v2_second'])
@pytest.mark.parametrize('legacy', [False, True])
def test_confirmed_single_authority_cpi_and_strict_legacy_identity(name, legacy):
    ls, keys, instructions = receipt(name)
    cpi = next(i for i in instructions if i['data'].startswith(CPI))
    assert [keys[n] for n in cpi['accounts']] == [AUTHORITY]
    assert len(cpi['data']) == 515
    trade = next(i for i in instructions if keys[i['program_id_index']] == P.AMM_PROGRAM
                 and not i['data'].startswith(CPI))
    if legacy:
        cpi['accounts'].append(keys.index(P.AMM_PROGRAM))
    out = detail(ls, keys, instructions)
    assert out['scope'] == 'complete' and out['pools'] == [keys[trade['accounts'][0]]]
    assert out['invocations'] == 2 and out['reason'] == REASON


@pytest.mark.parametrize('damage', ['wrong_authority', 'wrong_program', 'extra_account', 'missing_inner',
                                    'truncated_logs', 'unknown_instruction', 'unknown_event'])
def test_confirmed_receipt_cannot_grant_scope_with_unproven_metadata(damage):
    ls, keys, instructions = receipt('tardigrade')
    cpi = next(i for i in instructions if i['data'].startswith(CPI))
    complete = True
    if damage == 'wrong_authority': cpi['accounts'] = [keys.index(P.AMM_PROGRAM)]
    if damage == 'wrong_program': cpi['accounts'] += [0]
    if damage == 'extra_account': cpi['accounts'] += [keys.index(P.AMM_PROGRAM), 0]
    if damage == 'missing_inner': complete = False
    if damage == 'truncated_logs': ls = ls[:-1]
    if damage == 'unknown_instruction':
        next(i for i in instructions if keys[i['program_id_index']] == P.AMM_PROGRAM)['data'] = bytes(8)
    if damage == 'unknown_event': cpi['data'] = CPI + bytes(8)
    out = detail(ls, keys, instructions, complete=complete)
    assert out['scope'] == 'unknown' and out['pools'] == []
    assert out['reason'].startswith(REASON + ':scope_') and len(out['reason']) <= 160


def test_all_affected_pools_remain_in_scope_after_confirmed_cpi():
    ls, keys, instructions = receipt('tardigrade')
    trade = next(i for i in instructions if keys[i['program_id_index']] == P.AMM_PROGRAM)
    first_pool = keys[trade['accounts'][0]]
    keys.append(POOL)
    other = copy.deepcopy(trade); other['accounts'][0] = len(keys)-1
    instructions.append(other)
    ls += logs(buy())
    out = detail(ls, keys, instructions)
    assert out['scope'] == 'complete' and out['pools'] == sorted([first_pool, POOL])


@pytest.mark.parametrize('damage,suffix', [
    ('accounts', 'scope_layout_accounts_c62e1552b4d9e870_a22_d25'),
    ('data', 'scope_layout_data_c62e1552b4d9e870_a25_d24'),
    ('instruction', 'scope_unknown_instruction_0000000000000000_a25_d8'),
    ('count', 'scope_invocation_count_n0_l1'),
])
def test_first_instruction_layout_diagnostics_name_exact_rejection(damage, suffix):
    instruction = ix()
    if damage == 'accounts': instruction['accounts'] = instruction['accounts'][:22]
    if damage == 'data': instruction['data'] = instruction['data'][:-1]
    if damage == 'instruction': instruction['data'] = bytes(8)
    out = detail(logs(buy()), [P.AMM_PROGRAM, POOL, ACTOR], [] if damage == 'count' else [instruction])
    assert out['scope'] == 'unknown'
    assert out['reason'] == REASON + ':' + suffix


def test_diagnostics_are_bounded_and_never_include_raw_payload_or_account_keys():
    out = detail(logs(buy()), [P.AMM_PROGRAM, POOL, ACTOR], [dict(ix(), data=bytes(8))], reason='x'*300)
    assert len(out['reason']) == 160 and out['reason'].endswith(':scope_unknown_instruction_0000000000000000_a25_d8')
    assert set(out) == {'reason', 'scope', 'pools', 'invocations', 'discriminator', 'event_length'}


def test_unknown_event_diagnostic_is_distinct_from_invocation_count():
    ls = logs('Program data: ' + base64.b64encode(bytes(8)).decode())
    out = detail(ls, [P.AMM_PROGRAM, POOL, ACTOR], [ix()])
    assert out['scope'] == 'unknown' and out['reason'] == REASON + ':scope_unknown_event'


@pytest.mark.parametrize('damage,suffix', [
    ('inner', 'scope_inner_incomplete'), ('logs', 'scope_logs_incomplete'),
    ('overflow', 'scope_instruction_overflow'), ('program', 'scope_program_index'),
    ('account', 'scope_account_index_c62e1552b4d9e870_a25_d25'),
    ('shape', 'scope_instruction_shape'), ('key', 'scope_metadata_malformed')])
def test_dependency_failures_remain_global_with_closed_diagnostics(damage, suffix):
    ls, keys, instructions = logs(buy()), [P.AMM_PROGRAM, POOL, ACTOR], [ix()]
    if damage == 'logs': ls = ls[:-1]
    if damage == 'overflow': instructions *= 129
    if damage == 'program': instructions[0]['program_id_index'] = 99
    if damage == 'account': instructions[0]['accounts'][0] = 99
    if damage == 'shape': instructions[0]['data'] = None
    if damage == 'key': keys[0] = 'not a public key'
    out = detail(ls, keys, instructions, complete=damage != 'inner')
    assert out['scope'] == 'unknown' and out['pools'] == []
    assert out['reason'] == REASON + ':' + suffix


@pytest.mark.parametrize('overrides,category', [
    ({'ix_name': 'some_future_buy'}, 'unsupported_instruction'),
    ({'user_quote_amount_in': 1005}, 'inconsistent_gross_quote_buy'),
])
def test_buy_rejection_class_is_closed_without_changing_acceptance(overrides, category):
    with pytest.raises(P.DecodeLoss) as error:
        P.events(logs(buy(**overrides)))
    assert str(error.value) == 'pumpswap_event_malformed'
    assert error.value.detail_reason == REASON + ':' + category
    assert 'some_future_buy' not in error.value.detail_reason
