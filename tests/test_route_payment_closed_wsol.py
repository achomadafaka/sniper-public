"""Captured successful KRAW receipt: USDC -> closed router WSOL -> native SOL buy."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from sniper.services.memecoin_source_valuation import route_payment_evidence
from sniper.services.memecoin_source_initiation import solana_receipt_initiation, USDC


def receipt():
    tx = json.loads((Path(__file__).parent / 'fixtures/kraw_closed_wsol_route.json').read_text())
    return tx, tx['meta']['preTokenBalances'][0]['owner']


def test_captured_closed_wsol_route_excludes_external_usdc_fee():
    tx, owner = receipt()
    assert route_payment_evidence(tx, owner, 500_000_000) == (499_050_000, None)
    assert route_payment_evidence(tx, owner, 490_000_000) == (490_000_000, None)


def test_captured_receipt_carries_fee_excluded_resume_payment():
    tx, owner = receipt()
    mint = next(b['mint'] for b in tx['meta']['postTokenBalances']
                if b['owner'] == owner and b['mint'] != USDC)
    proof = solana_receipt_initiation(tx, owner, dict(
        basis='route_owner_quote_out', owner=owner, source_slot=tx['slot'], mint=mint,
        token_amount_atomic='11946258428914', quote_out_atomic={USDC: '500000000'}))
    assert proof['route_payment_atomic'] == '499050000'
    assert proof['payment_atomic'] == '500000000'
    assert proof['acquired_atomic'] == '11946258428914'
    assert 'route_payment_unknown' not in proof


@pytest.mark.parametrize('mutation', ['authority', 'recipient', 'unrelated', 'future_payment',
                                    'wrong_mint', 'preheld', 'duplicate_close', 'duplicate_init',
                                    'future_close', 'intervening_payment', 'failed',
                                    'missing_balances', 'short_balances', 'outside_parent',
                                    'late_init', 'duplicate_inner_index'])
def test_closed_wsol_route_requires_causal_native_payment(mutation):
    tx, owner = receipt()
    group = tx['meta']['innerInstructions'][-1]['instructions']
    close = group[9]['parsed']['info']
    if mutation == 'authority':
        close['owner'] = owner
    elif mutation == 'recipient':
        close['destination'] = owner
    elif mutation == 'unrelated':
        group[14]['parsed']['info']['lamports'] -= 1
    elif mutation == 'future_payment':
        group.append(group.pop(3))
    elif mutation == 'wrong_mint':
        group[3]['parsed']['info']['mint'] = 'unrelated_mint'
    elif mutation == 'preheld':
        tx['meta']['preBalances'][19] = 1
    elif mutation == 'duplicate_close':
        group.append(deepcopy(group[9]))
    elif mutation == 'duplicate_init':
        tx['meta']['innerInstructions'][1]['instructions'].append(
            deepcopy(tx['meta']['innerInstructions'][1]['instructions'][2]))
    elif mutation == 'future_close':
        group.append(group.pop(9))
    elif mutation == 'intervening_payment':
        group.insert(10, {'program': 'system', 'stackHeight': 2, 'parsed': {'type': 'transfer',
                         'info': {'source': close['owner'], 'destination': owner, 'lamports': 1}}})
    elif mutation == 'failed':
        tx['meta']['err'] = {'InstructionError': [3, 'InvalidArgument']}
    elif mutation == 'missing_balances':
        del tx['meta']['preBalances']
    elif mutation == 'short_balances':
        tx['meta']['postBalances'] = []
    elif mutation == 'outside_parent':
        # End the close's root before starting an unrelated native-payment tree.
        group.insert(10, {'programId': 'unrelated', 'stackHeight': 1})
    elif mutation == 'late_init':
        initialization = tx['meta']['innerInstructions'][1]['instructions'].pop(2)
        group.insert(4, initialization)  # After the first WSOL payout, before the second.
    elif mutation == 'duplicate_inner_index':
        tx['meta']['innerInstructions'].append(deepcopy(tx['meta']['innerInstructions'][-1]))
    assert route_payment_evidence(tx, owner, 500_000_000)[0] is None
