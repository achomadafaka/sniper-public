"""Natural router receipts prove event-account absence, never multi-hop prices."""
import base64
import hashlib
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services.memecoin_opportunity_live import program_data_logs
from test_desk_ps_loss_scope import ix
from test_desk_ps_scope_live_shapes import receipt as legacy_receipt
from test_memecoin_desk_pumpswap import ACTOR, POOL, buy, logs

FIXTURES = Path(__file__).parent / 'fixtures/memecoin/desk_multihop_20261027'
MULTI = bytes.fromhex('2b644913e9f66f94')
CPI = bytes.fromhex('e445a52e51cb9a1d')
REASON = 'pumpswap_event_malformed:unsupported_or_inconsistent_buy:unsupported_instruction'
RAW_SHA = {'amm': '368f010d7c81045af1b3bf7023a49af936e0918cf1dd8268be2cc15dd751532a',
           'pump': 'b4d46fddb2950796984d369f5113150feac6c1fd66494f3b3c174747f3e456ef'}


def raw_receipt(kind='amm'):
    saved = json.loads((FIXTURES / ('26_' + kind + '.json')).read_text())
    raw = base64.b64decode(saved['raw_b64'], validate=True)
    assert hashlib.sha256(raw).hexdigest() == RAW_SHA[kind]
    return saved, raw


def receipt(kind='amm'):
    _, raw = raw_receipt(kind)
    info = pb.SubscribeUpdate.FromString(raw).transaction.transaction
    assert not info.meta.HasField('err')
    assert not info.meta.inner_instructions_none and not info.meta.log_messages_none
    message = info.transaction.message
    assert sum(len(a.writable_indexes) for a in message.address_table_lookups) == len(info.meta.loaded_writable_addresses)
    assert sum(len(a.readonly_indexes) for a in message.address_table_lookups) == len(info.meta.loaded_readonly_addresses)
    keys = list(message.account_keys) + list(info.meta.loaded_writable_addresses) + list(info.meta.loaded_readonly_addresses)
    all_ix = list(message.instructions) + [i for g in info.meta.inner_instructions for i in g.instructions]
    instructions = [{'program_id_index': i.program_id_index, 'accounts': list(i.accounts), 'data': bytes(i.data)}
                    for i in all_ix]
    return list(info.meta.log_messages), keys, instructions


def detail(ls, keys, instructions, complete=True):
    return P.loss_details(ls, reason=REASON, account_keys=keys, instructions=instructions, inner_complete=complete)


def multi(instructions):
    return next(i for i in instructions if i['data'].startswith(MULTI))


def replace_event(ls, instructions, change):
    ordinal, line = next(iter(program_data_logs(ls, P.AMM_PROGRAM)))
    old = anchor_event_bytes(line)
    raw = change(old)
    ls[ordinal] = 'Program data: ' + base64.b64encode(raw).decode()
    event_ix = next(i for i in instructions if i['data'] == CPI + old)
    event_ix['data'] = CPI + raw


@pytest.mark.parametrize('kind', ['amm', 'pump'])
def test_complete_natural_nested_route_adds_every_key_without_price_or_pool_authority(kind):
    ls, keys, instructions = receipt(kind)
    with pytest.raises(P.DecodeLoss, match='pumpswap_event_malformed'):
        P.events(ls)
    out = detail(ls, keys, instructions)
    assert out['event_account_keys'] == sorted({str(Pubkey.from_bytes(k)) for k in keys})
    assert out['scope'] == 'unknown' and out['pools'] == []
    assert out['reason'].endswith('scope_unknown_instruction_2b644913e9f66f94_a26_d24')
    assert out['invocations'] == 1  # Original first-unknown diagnostic remains intact.


def test_all_keys_include_readonly_and_unused_keys_without_promoting_them_to_pool_ids():
    ls, keys, instructions = receipt()
    keys.append(bytes(Pubkey.from_string(POOL)))
    out = detail(ls, keys, instructions)
    assert POOL in out['event_account_keys']
    assert P.PUMP_PROGRAM in out['event_account_keys']  # A readonly fixed program key.
    assert out['pools'] == []


@pytest.mark.parametrize('fixed', [3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15])
def test_changed_fixed_identity_cannot_attest_a_route(fixed):
    ls, keys, instructions = receipt()
    multi(instructions)['accounts'][fixed] = 0
    assert 'event_account_keys' not in detail(ls, keys, instructions)


@pytest.mark.parametrize('damage', ['data', 'groups', 'no_groups', 'too_many_groups', 'too_many_keys', 'too_many_instructions',
                                    'index', 'negative_index', 'bool_index', 'program_index', 'bad_key',
                                    'shape', 'unknown_handler', 'loader', 'loader_log', 'missing_cpi', 'cpi_payload',
                                    'cpi_authority', 'extra_invoke', 'truncated_logs', 'failed_logs'])
def test_partial_malformed_unknown_or_code_change_evidence_stays_global(damage):
    ls, keys, instructions = receipt()
    route = multi(instructions)
    if damage == 'data': route['data'] = route['data'][:-1]
    if damage == 'groups': route['accounts'] = route['accounts'][:-1]
    if damage == 'no_groups': route['accounts'] = route['accounts'][:16]
    if damage == 'too_many_groups': route['accounts'] = route['accounts'][:16] + route['accounts'][-5:] * 33
    if damage == 'too_many_keys': keys += [keys[0]] * (257 - len(keys))
    if damage == 'too_many_instructions': instructions *= 129
    if damage == 'index': route['accounts'][-1] = len(keys)
    if damage == 'negative_index': route['accounts'][-1] = -1
    if damage == 'bool_index': route['accounts'][-1] = True
    if damage == 'program_index': route['program_id_index'] = len(keys)
    if damage == 'bad_key': keys[-1] = b'bad'
    if damage == 'shape': route['data'] = None
    if damage == 'unknown_handler':
        instructions.append(dict(route, data=bytes(8)))
        ls += [f'Program {P.AMM_PROGRAM} invoke [1]', f'Program {P.AMM_PROGRAM} success']
    if damage == 'loader':
        keys.append(bytes(Pubkey.from_string('BPFLoaderUpgradeab1e11111111111111111111111')))
        instructions.append({'program_id_index': len(keys)-1, 'accounts': [], 'data': b'\x03'})
    if damage == 'loader_log':
        loader = 'BPFLoaderUpgradeab1e11111111111111111111111'
        ls += [f'Program {loader} invoke [1]', f'Program {loader} success']
    if damage == 'missing_cpi':
        instructions.remove(next(i for i in instructions if i['data'].startswith(CPI)))
    if damage == 'cpi_payload':
        event_ix = next(i for i in instructions if i['data'].startswith(CPI))
        event_ix['data'] = event_ix['data'][:-1] + bytes([event_ix['data'][-1] ^ 1])
    if damage == 'cpi_authority': next(i for i in instructions if i['data'].startswith(CPI))['accounts'] = [0]
    if damage == 'extra_invoke': ls += [f'Program {P.AMM_PROGRAM} invoke [1]', f'Program {P.AMM_PROGRAM} success']
    if damage == 'truncated_logs': ls = ls[:-1]
    if damage == 'failed_logs': ls[-1] = ls[-1].replace('success', 'failed: custom error')
    out = detail(ls, keys, instructions)
    assert 'event_account_keys' not in out
    assert out['scope'] == 'unknown' and out['pools'] == []


@pytest.mark.parametrize('complete', [False, None, 'yes'])
def test_missing_or_untyped_inner_and_alt_completeness_never_attests(complete):
    ls, keys, instructions = receipt()
    assert 'event_account_keys' not in detail(ls, keys, instructions, complete=complete)


@pytest.mark.parametrize('damage', ['unknown_name', 'truncated', 'unknown_disc', 'state_change', 'foreign_pool'])
def test_matched_but_unsupported_or_inconsistent_events_still_refuse(damage):
    ls, keys, instructions = receipt()
    fake = bytes(Pubkey.from_string(POOL))
    if damage == 'foreign_pool': keys.append(fake)
    def change(raw):
        if damage == 'unknown_name': return raw.replace(b'multi_hop_swap', b'future_hop_xxx')
        if damage == 'truncated': return raw[:200]
        if damage == 'unknown_disc': return bytes(8) + raw[8:]
        if damage == 'state_change': return sorted(P._STATE_CHANGES)[0] + raw[8:]
        return raw[:120] + fake + raw[152:]
    replace_event(ls, instructions, change)
    assert 'event_account_keys' not in detail(ls, keys, instructions)


@pytest.mark.parametrize('cut', [120, 151, 418, 450, 467, 499])
def test_truncating_the_known_pinned_prefix_never_grants_identity_proof(cut):
    ls, keys, instructions = receipt()
    replace_event(ls, instructions, lambda raw: raw[:cut])
    assert 'event_account_keys' not in detail(ls, keys, instructions)


def test_bounded_opaque_extension_is_compared_but_never_interpreted_as_price():
    ls, keys, instructions = receipt()
    before = anchor_event_bytes(next(iter(program_data_logs(ls, P.AMM_PROGRAM)))[1])
    assert len(before) == 508  # The pinned BuyEvent ends at500; the final8bytes remain opaque.
    replace_event(ls, instructions, lambda raw: raw[:500] + bytes([7]) * 8)
    assert detail(ls, keys, instructions)['event_account_keys'] == sorted({str(Pubkey.from_bytes(k)) for k in keys})
    with pytest.raises(P.DecodeLoss, match='pumpswap_event_malformed'):
        P.events(ls)
    replace_event(ls, instructions, lambda raw: raw + bytes(4096))
    assert 'event_account_keys' not in detail(ls, keys, instructions)


def test_legacy_pool_scope_is_not_replaced_or_weakened():
    out = detail(logs(buy()), [P.AMM_PROGRAM, POOL, ACTOR], [ix()])
    assert out['scope'] == 'complete' and out['pools'] == [POOL]
    assert 'event_account_keys' not in out


def test_legacy_and_multi_route_never_yield_partial_complete_pool_scope():
    ls, keys, instructions = receipt()
    keys += [bytes(Pubkey.from_string(POOL)), bytes(Pubkey.from_string(ACTOR))]
    legacy = ix()
    legacy['program_id_index'] = next(i for i, k in enumerate(keys) if str(Pubkey.from_bytes(k)) == P.AMM_PROGRAM)
    legacy['accounts'] = [len(keys)-2] + [len(keys)-1] * 24
    instructions.insert(0, legacy)
    ls = logs(buy()) + ls
    out = detail(ls, keys, instructions)
    assert out['scope'] == 'unknown' and out['pools'] == []
    # A legacy log without its event-CPI cannot repair the complete transaction proof.
    assert 'event_account_keys' not in out


def test_complete_legacy_and_multi_route_keeps_unknown_pool_scope_and_full_key_envelope():
    ls, keys, instructions = receipt()
    old_logs, old_keys, old_ix = legacy_receipt('tardigrade')
    remap = {}
    for i, key in enumerate(old_keys):
        raw = bytes(Pubkey.from_string(key))
        if raw not in keys:
            keys.append(raw)
        remap[i] = keys.index(raw)
    old_ix = [dict(i, program_id_index=remap[i['program_id_index']],
                   accounts=[remap[n] for n in i['accounts']]) for i in old_ix]
    out = detail(old_logs + ls, keys, old_ix + instructions)
    assert out['scope'] == 'unknown' and out['pools'] == []
    assert out['event_account_keys'] == sorted({str(Pubkey.from_bytes(k)) for k in keys})
