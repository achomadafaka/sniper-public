"""Empty PCS V2 reverts need receipt-matched proof before the existing retry can run."""
import copy
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode

from sniper.execution.evm_real_seams import evm_revert_reason

FIXTURE = Path(__file__).parent / 'fixtures/memecoin/bsc_88d0335b_min_out.json'


def replay(fixture):
    def http(method, url, headers=None, body=None, timeout=10):
        request = json.loads(body)
        name, params = request['method'], request['params']
        replies = {
            'eth_getTransactionByHash': {'result': fixture['transaction']},
            'eth_getTransactionReceipt': {'result': fixture['receipt']},
            'eth_call': fixture['replay'],
            'eth_getBlockByHash': {'result': fixture['block']},
            'eth_getBlockReceipts': {'result': fixture['receipts']},
        }
        if name == 'eth_simulateV1':
            request, parent = params
            assert parent == '0x7839f7f'
            calls = request['blockStateCalls'][0]['calls']
            original = bytes.fromhex(calls[-2]['input'][2:])
            probe = bytes.fromhex(calls[-1]['input'][2:])
            deadline, legs = decode(['uint256', 'bytes[]'], original[4:])
            pdeadline, plegs = decode(['uint256', 'bytes[]'], probe[4:])
            assert (deadline, legs[0]) == (pdeadline, plegs[0])
            before = list(decode(['uint256', 'uint256', 'address[]', 'address'], legs[-1][4:]))
            after = list(decode(['uint256', 'uint256', 'address[]', 'address'], plegs[-1][4:]))
            assert before[1] == 318002515555048153093041 and after[1] == 1
            before[1] = 1
            assert before == after
            assert {k: v for k, v in calls[-2].items() if k != 'input'} == {
                k: v for k, v in calls[-1].items() if k != 'input'}
            return 200, fixture['simulation'], None
        return 200, replies[name], None
    return evm_revert_reason(fixture['transaction']['hash'], 'https://rpc.invalid', http=http)


def test_captured_empty_v2_revert_is_proven_min_out():
    kind, detail = replay(json.loads(FIXTURE.read_text()))
    assert kind == 'min_out'
    assert '315112063198328972857925' in detail
    assert '318002515555048153093041' in detail


def test_block_end_success_still_checks_the_mined_prefix():
    f = json.loads(FIXTURE.read_text())
    f['replay'] = {'result': '0x'}
    assert replay(f)[0] == 'min_out'


@pytest.mark.parametrize('failure', [
    'prior_status', 'prior_logs', 'prior_gas', 'target_gas', 'missing_receipt',
    'probe_reverted', 'probe_empty', 'probe_wrong_arity', 'probe_above_floor',
    'simulation_unavailable', 'wrong_chain', 'wrong_router', 'malformed_input',
    'confirmed_receipt', 'target_status_mismatch', 'probe_zero', 'non_revert_error',
])
def test_unproved_empty_revert_never_authorizes_retry(failure):
    f = copy.deepcopy(json.loads(FIXTURE.read_text()))
    rows = f['simulation']['result'][0]['calls']
    if failure == 'prior_status': rows[0]['status'] = '0x0'
    elif failure == 'prior_logs': next(row for row in rows[:-2] if row['logs'])['logs'] = []
    elif failure == 'prior_gas': rows[0]['gasUsed'] = '0x1'
    elif failure == 'target_gas': rows[-2]['gasUsed'] = '0x1'
    elif failure == 'target_status_mismatch':
        rows[-2]['status'] = '0x1'
        next(r for r in f['receipts'] if r['transactionHash'] == f['transaction']['hash'])['status'] = '0x1'
    elif failure == 'missing_receipt': f['receipts'] = []
    elif failure == 'probe_reverted': rows[-1]['status'] = '0x0'
    elif failure == 'probe_empty': rows[-1]['returnData'] = '0x'
    elif failure == 'probe_zero':
        rows[-1]['returnData'] = '0x' + encode(['bytes[]'], [[encode(['uint256'], [1]),
                                                          encode(['uint256'], [0])]]).hex()
    elif failure in ('probe_wrong_arity', 'probe_above_floor'):
        outputs = [encode(['uint256'], [318002515555048153093041])]
        if failure == 'probe_above_floor': outputs.insert(0, encode(['uint256'], [1]))
        rows[-1]['returnData'] = '0x' + encode(['bytes[]'], [outputs]).hex()
    elif failure == 'simulation_unavailable': f['simulation'] = {'error': {'code': -32601}}
    elif failure == 'wrong_chain': f['transaction']['chainId'] = '0x1'
    elif failure == 'wrong_router': f['transaction']['to'] = '0x' + 'aa' * 20
    elif failure == 'malformed_input': f['transaction']['input'] = '0x5ae401dc'
    elif failure == 'confirmed_receipt': f['receipt']['status'] = '0x1'
    elif failure == 'non_revert_error': f['replay'] = {'error': {'message': 'missing trie node'}}
    assert replay(f)[0] == 'unknown'
