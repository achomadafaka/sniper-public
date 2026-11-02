"""Swap-only lost transactions prove pool absence from their complete key list (live receipts, 2026-10-28)."""
import json
from pathlib import Path

import base58
import pytest

from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_ps_losses as PSL

FIX = Path(__file__).parent / 'fixtures/memecoin/desk_swap_tx_envelope_20261028'
FOREIGN = 'So11111111111111111111111111111111111111112'


def receipt(name):
    tx = json.loads((FIX / (name + '.json')).read_text())
    m, meta = tx['transaction']['message'], tx['meta']
    loaded = meta.get('loadedAddresses') or {}
    keys = m['accountKeys'] + loaded.get('writable', []) + loaded.get('readonly', [])
    ix = lambda i: {'program_id_index': i['programIdIndex'], 'accounts': list(i['accounts']), 'data': base58.b58decode(i['data'])}
    instructions = [ix(i) for i in m['instructions']] + [ix(i) for g in meta['innerInstructions'] for i in g['instructions']]
    return list(meta['logMessages']), keys, instructions


def detail(ls, keys, instructions, complete=True):
    return P.loss_details(ls, reason='pumpswap_event_malformed', account_keys=keys, instructions=instructions,
                          inner_complete=complete)


@pytest.mark.parametrize('name', ['c62e_layout', 'f945_close_volume', 'a039_maintenance'])
def test_swap_only_loss_carries_every_key_and_proves_foreign_pool_absence(name):
    ls, keys, instructions = receipt(name)
    out = detail(ls, keys, instructions)
    assert out['scope'] == 'unknown' and out['pools'] == [] and 'event_account_keys' not in out
    assert out['swap_tx_account_keys'] == sorted(set(keys))
    bits = PSL.event_account_filter(out['swap_tx_account_keys'])
    assert all(not PSL.event_account_absent(bits, k) for k in keys)          # every touched account stays a loss
    assert PSL.event_account_absent(bits, FOREIGN) or FOREIGN in keys


@pytest.mark.parametrize('damage', ['admin_ix', 'unknown_event', 'incomplete', 'loader', 'index', 'extra_invoke'])
def test_any_unproven_shape_stays_global(damage):
    ls, keys, instructions = receipt('c62e_layout')
    amm = keys.index(P.AMM_PROGRAM)
    complete = True
    if damage == 'admin_ix':
        instructions.append({'program_id_index': amm, 'accounts': [], 'data': bytes(8)})
    if damage == 'unknown_event':
        event = next(i for i in instructions if i['program_id_index'] == amm and i['data'][:8] == P._EVENT_CPI)
        event['data'] = event['data'][:8] + bytes(8) + event['data'][16:]                  # unknown event
    if damage == 'incomplete':
        complete = False
    if damage == 'loader':
        keys.append('BPFLoaderUpgradeab1e11111111111111111111111')
        instructions.append({'program_id_index': len(keys) - 1, 'accounts': [], 'data': b'\x03'})
    if damage == 'index':
        instructions[0]['accounts'].append(len(keys))
    if damage == 'extra_invoke':
        ls += [f'Program {P.AMM_PROGRAM} invoke [1]', f'Program {P.AMM_PROGRAM} success']
    assert 'swap_tx_account_keys' not in detail(ls, keys, instructions, complete)


def test_truncated_logs_stay_global():
    ls, keys, instructions = receipt('c62e_layout')
    assert 'swap_tx_account_keys' not in detail(ls[:3] + ['Log truncated'], keys, instructions)


def test_reader_accepts_only_the_two_named_profiles():
    assert PSL.SWAP_TX_ENVELOPE_PROFILE != PSL.EVENT_ENVELOPE_PROFILE
    import inspect
    assert 'SWAP_TX_ENVELOPE_PROFILE' in inspect.getsource(PSL._validated_envelope)
