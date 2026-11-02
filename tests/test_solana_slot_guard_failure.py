"""The 2026-10-19 buys failed Lighthouse's deadline after their swaps succeeded."""
import copy
import json
from pathlib import Path

import base58
import pytest

from sniper.execution import solana_real_seams as seams, solana_slot_guard as guard


@pytest.fixture(params=['54Vuz8gk', '4hkkQery'])
def receipt(request):
    return json.loads((Path(__file__).parent / 'fixtures/memecoin' /
                       f'slot_expired_{request.param}.json').read_text())


def proof(receipt):
    return guard.slot_guard_failure(receipt, signature=receipt['transaction']['signatures'][0])


def test_exact_deadline_failure_keeps_signed_slots_and_paid_fee(receipt):
    result = proof(receipt)
    assert result == dict(reason='slot_window_expired',
        source_slot=458798277 if receipt['slot'] == 458798280 else 458801406,
        max_slot=458798278 if receipt['slot'] == 458798280 else 458801407,
        landing_slot=receipt['slot'], instruction_index=receipt['meta']['err']['InstructionError'][0],
        failed_program=guard.LIGHTHOUSE_PROGRAM, program_error='AssertionFailed',
        fee_lamports=receipt['meta']['fee'], no_tokens_bought=True)


@pytest.mark.parametrize('change', ['program', 'error', 'index', 'lower_bound', 'within_window',
                                    'missing_bound', 'malformed_data', 'accounts', 'signature'])
def test_hex_code_or_unbound_receipt_never_proves_expiry(receipt, change):
    tx = receipt['transaction']
    ixs = tx['message']['instructions']
    if change == 'program':
        tx['message']['accountKeys'][ixs[-1]['programIdIndex']] = seams._JUPITER_V6
    elif change == 'error':
        receipt['meta']['err']['InstructionError'][1]['Custom'] = 6002
    elif change == 'index':
        receipt['meta']['err']['InstructionError'][0] -= 1
    elif change == 'lower_bound':
        ixs[-2]['data'] = ixs[-1]['data']
    elif change == 'within_window':
        receipt['slot'] = int.from_bytes(base58.b58decode(ixs[-1]['data'])[3:11], 'little')
    elif change == 'missing_bound':
        ixs.pop(-2)
        receipt['meta']['err']['InstructionError'][0] -= 1
    elif change == 'malformed_data':
        ixs[-1]['data'] = 'not base58!'
    elif change == 'accounts':
        ixs[-1]['accounts'] = [0]
    elif change == 'signature':
        assert guard.slot_guard_failure(receipt, signature='different') is None
        return
    assert proof(receipt) is None


@pytest.mark.parametrize('fee,expected', [(None, None), (0, 0), (-1, None), (True, None)])
def test_missing_and_zero_fees_remain_distinct(receipt, fee, expected):
    receipt['meta']['fee'] = fee
    assert proof(receipt)['fee_lamports'] == expected


@pytest.mark.parametrize('accounts', [None, '', 0, False])
def test_malformed_empty_accounts_are_not_guard_proof(receipt, accounts):
    receipt['transaction']['message']['instructions'][-1]['accounts'] = accounts
    assert proof(receipt) is None


def test_confirmation_reuses_one_receipt_and_preserves_raw_error(receipt):
    calls = []
    def http(_method, _url, **kwargs):
        request = json.loads(kwargs['body'])
        calls.append(request['method'])
        return 200, {'result': copy.deepcopy(receipt)}, None
    result = seams._failed_confirm_state(receipt['meta']['err'],
        receipt['transaction']['signatures'][0], 'http://offline.invalid', http)
    assert calls == ['getTransaction']
    assert result == 'failed' and result.reason_code == 'slot_window_expired'
    assert result.err == receipt['meta']['err'] and result.deterministic
    assert result.slot_guard == proof(receipt)
    assert result.failed_program is None  # Lighthouse must never blacklist a trading venue.


def test_lighthouse_does_not_trigger_a_venue_lookup():
    def forbidden(*_args, **_kwargs):
        pytest.fail('a slot deadline triggered a venue-label request')
    assert seams.exclude_failed_venue('mint', guard.LIGHTHOUSE_PROGRAM, http=forbidden) is None


def test_unknown_receipts_do_not_invent_guard_proof():
    for receipt in (None, {}, {'meta': {'err': None}}):
        assert guard.slot_guard_failure(receipt, signature='sig') is None
