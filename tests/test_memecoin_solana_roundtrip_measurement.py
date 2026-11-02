"""Check unsigned, full-quantity sequential measurements without provider calls."""
import base64
import copy
import hashlib
import json

import pytest
from decimal import Decimal
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.execution import pump_native_exit as native
from sniper.execution import solana_roundtrip_measurement as measurement
from sniper.execution.solana_roundtrip_measurement import measure_roundtrip

JUP = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
USER, MINT, LEDGER, POOL = [str(Pubkey.new_unique()) for _ in range(4)]
USDC_ACCOUNT = str(native.ata(USER, native.USDC_MINT, native.TOKEN_PROGRAM))
TOKEN_ACCOUNT = str(native.ata(USER, MINT, native.TOKEN_PROGRAM))


def ix(name, accounts, *, program=JUP, data=None):
    raw = data if data is not None else hashlib.sha256(('global:' + name).encode()).digest()[:8]
    return {'programId': program, 'data': base64.b64encode(raw).decode(),
            'accounts': [{'pubkey': a, 'isSigner': a == USER, 'isWritable': True} for a in accounts]}


def balances(usdc):
    return [{'accountIndex': i, 'mint': mint, 'owner': USER,
             'uiTokenAmount': {'amount': str(amount), 'decimals': 6}}
            for i, mint, amount in [(0, native.USDC_MINT, usdc), (1, MINT, 77)]]


def transfer(source, destination, mint, amount):
    return {'programId': native.TOKEN_PROGRAM, 'parsed': {'type': 'transferChecked', 'info': {
        'source': source, 'destination': destination, 'mint': mint,
        'tokenAmount': {'amount': str(amount), 'decimals': 6}}}}


def scenario(change=None, **measure_kwargs):
    buy = {'inputMint': native.USDC_MINT, 'outputMint': MINT, 'inAmount': '175000000',
           'outAmount': '100000', 'otherAmountThreshold': '85000', 'contextSlot': 10,
           'routePlan': [{'swapInfo': {'ammKey': POOL, 'inputMint': native.USDC_MINT, 'outputMint': MINT}}]}
    sell = {**buy, 'inputMint': MINT, 'outputMint': native.USDC_MINT,
            'inAmount': '85000', 'outAmount': '133597975', 'contextSlot': 11,
            'routePlan': [{'swapInfo': {'ammKey': POOL, 'inputMint': MINT, 'outputMint': native.USDC_MINT}}]}
    built_buy = {'swapInstruction': ix('route', [native.TOKEN_PROGRAM, USER, USDC_ACCOUNT,
                    TOKEN_ACCOUNT, JUP, MINT, JUP]), 'setupInstructions': [],
                 'addressLookupTableAddresses': []}
    built_sell = {'swapInstruction': ix('route_with_token_ledger', [native.TOKEN_PROGRAM, USER,
                     TOKEN_ACCOUNT, USDC_ACCOUNT, JUP, native.USDC_MINT, JUP, LEDGER]),
                  'tokenLedgerInstruction': ix('set_token_ledger', [LEDGER, TOKEN_ACCOUNT]),
                  'setupInstructions': [], 'addressLookupTableAddresses': []}
    value = {'err': None, 'preTokenBalances': balances(1000000000),
             'postTokenBalances': balances(990000000), 'fee': 5000, 'unitsConsumed': 220000}
    state = {'buy': buy, 'sell': sell, 'built_buy': built_buy, 'built_sell': built_sell,
             'value': value, 'slot': 12, 'calls': []}
    if change:
        change(state)

    def jup_call(method, path, *, body):
        assert (method, path) == ('POST', '/swap-instructions')
        payload = json.loads(body)
        state['calls'].append(payload)
        assert payload['userPublicKey'] == USER
        assert payload['dynamicComputeUnitLimit'] is False
        assert payload.get('prioritizationFeeLamports', 0) == 0
        is_sell = payload['quoteResponse']['inputMint'] == MINT
        assert payload.get('useTokenLedger', False) is is_sell
        return 200, copy.deepcopy(built_sell if is_sell else built_buy)

    def rpc(method, params):
        assert method == 'simulateTransaction', method
        state['rpc_calls'] = state.get('rpc_calls', 0) + 1
        if state.get('rpc_errors'):
            return state['rpc_errors'].pop(0)
        config = params[1]
        assert config['sigVerify'] is False and config['replaceRecentBlockhash'] is True
        assert config['innerInstructions'] is True
        assert config['minContextSlot'] == 11
        tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
        assert all(bytes(signature) == bytes(64) for signature in tx.signatures)
        keys = [str(k) for k in tx.message.account_keys]
        for field in ('preTokenBalances', 'postTokenBalances'):
            for row in state['value'].get(field, []):
                account = USDC_ACCOUNT if row['mint'] == native.USDC_MINT else TOKEN_ACCOUNT
                row['accountIndex'] = keys.index(account)
        data = [bytes(i.data) for i in tx.message.instructions]
        assert data[1] == hashlib.sha256(b'global:set_token_ledger').digest()[:8]
        assert data[2] == bytes(base64.b64decode(built_buy['swapInstruction']['data']))
        assert data[3] == bytes(base64.b64decode(built_sell['swapInstruction']['data']))
        state['value'].setdefault('innerInstructions', [
            {'index': 2, 'instructions': [transfer(USDC_ACCOUNT, POOL, native.USDC_MINT, 175000000),
                                         transfer(POOL, TOKEN_ACCOUNT, MINT, 123456)]},
            {'index': 3, 'instructions': [transfer(TOKEN_ACCOUNT, POOL, MINT, 123456),
                                         transfer(POOL, USDC_ACCOUNT, native.USDC_MINT, 165000000)]}])
        if state.get('compiled_inner'):
            import base58
            for group in state['value']['innerInstructions']:
                for pos, instruction in enumerate(group['instructions']):
                    info = instruction['parsed']['info']
                    accounts = [keys.index(info[name]) for name in ('source', 'mint', 'destination')]
                    accounts.append(keys.index(USER))
                    raw = bytes([12]) + int(info['tokenAmount']['amount']).to_bytes(8, 'little') + bytes([6])
                    group['instructions'][pos] = {
                        'programIdIndex': keys.index(native.TOKEN_PROGRAM),
                        'accounts': accounts, 'data': base58.b58encode(raw).decode()}
            for field, val in state.get('invalid_compiled', {}).items():
                first = state['value']['innerInstructions'][0]['instructions'][0]
                if field == 'account':
                    first['accounts'][0] = val
                else:
                    first[field] = val
        return {'context': {'slot': state['slot']}, 'value': state['value']}

    return measure_roundtrip(buy, sell, user=USER, jup_call=jup_call, rpc=rpc, **measure_kwargs), state


def test_ledger_sells_actual_acquired_delta_and_uses_simulation_usdc_proceeds():
    result, state = scenario()
    assert result['status'] == 'measured', result
    assert result['buy_spent_atomic'] == '175000000'
    assert result['acquired_atomic'] == result['sold_atomic'] == '123456'
    assert result['sell_received_atomic'] == '165000000'
    assert result['cost_pct'] == pytest.approx(1000 / 175)
    assert result['simulation_fee_lamports'] == '5000'
    assert result['simulated_slot'] == 12
    assert len(state['calls']) == 2
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('change,reason', [
    (lambda s: s['built_sell'].pop('tokenLedgerInstruction'), 'unsupported_ledger'),
    (lambda s: s['built_sell']['tokenLedgerInstruction']['accounts'][1].update(pubkey=POOL), 'account_identity'),
    (lambda s: s['built_sell'].update(otherInstructions=[ix('anything', [USER])]), 'unsupported_instructions'),
    (lambda s: s['value'].pop('preTokenBalances'), 'missing_balance_evidence'),
    (lambda s: s['value']['postTokenBalances'][1]['uiTokenAmount'].update(amount='78'), 'residual_inventory'),
    (lambda s: s['value']['postTokenBalances'][0]['uiTokenAmount'].update(amount='999999999'), 'usdc_reconciliation'),
    (lambda s: s['value'].update(err={'InstructionError': [3, 'Custom']}), 'simulation_failed'),
    (lambda s: s.update(slot=10), 'slot_mismatch'),
    (lambda s: s['value'].update(innerInstructions=[]), 'missing_transfer_evidence'),
    (lambda s: s['value'].pop('fee'), 'missing_fee_evidence'),
    (lambda s: s['built_buy']['swapInstruction'].update(
        data=base64.b64encode(hashlib.sha256(b'global:route').digest()[:8] + bytes(2000)).decode()),
        'transaction_limits'),
    (lambda s: s['built_sell']['swapInstruction'].update(
        data=base64.b64encode(hashlib.sha256(b'global:route').digest()[:8]).decode()),
        'unsupported_route'),
])
def test_unproved_results_are_unknown(change, reason):
    result, _ = scenario(change)
    assert result['status'] == 'unknown', result
    assert result['reason'] == reason
    assert result['cost_pct'] is None


def test_target_balance_metadata_is_required():
    def change(state):
        state['value']['postTokenBalances'] = state['value']['postTokenBalances'][:1]
        state['value']['preTokenBalances'] = state['value']['preTokenBalances'][:1]
    result, _ = scenario(change)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'missing_balance_evidence'


def test_plain_binary_token_transfers_prove_the_same_amounts():
    import base58
    def raw(source, destination, amount):
        return {'programId': native.TOKEN_PROGRAM, 'accounts': [source, destination, USER],
                'data': base58.b58encode(bytes([3]) + amount.to_bytes(8, 'little')).decode()}
    def change(state):
        state['value']['innerInstructions'] = [
            {'index': 2, 'instructions': [raw(USDC_ACCOUNT, POOL, 175000000),
                                         raw(POOL, TOKEN_ACCOUNT, 123456)]},
            {'index': 3, 'instructions': [raw(TOKEN_ACCOUNT, POOL, 123456),
                                         raw(POOL, USDC_ACCOUNT, 165000000)]}]
    result, _ = scenario(change)
    assert result['status'] == 'measured', result
    assert result['sold_atomic'] == '123456'


def test_provider_exception_is_unknown():
    def unavailable(*args, **kwargs):
        raise TimeoutError('provider unavailable')
    buy = {'inputMint': native.USDC_MINT, 'outputMint': MINT, 'inAmount': '175000000',
           'outAmount': '100000', 'contextSlot': 10, 'routePlan': []}
    sell = {**buy, 'inputMint': MINT, 'outputMint': native.USDC_MINT}
    result = measure_roundtrip(buy, sell, user=USER, jup_call=unavailable, rpc=unavailable)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'build_failed'


def compiled_inner(state):
    state['compiled_inner'] = True
    for name in ('built_buy', 'built_sell'):
        state[name]['swapInstruction']['accounts'].append(
            {'pubkey': POOL, 'isSigner': False, 'isWritable': True})


def test_compiled_program_and_account_indexes_measure_exact_transfers():
    result, _ = scenario(compiled_inner)
    assert result['status'] == 'measured', result
    assert result['acquired_atomic'] == '123456'
    assert result['sell_received_atomic'] == '165000000'


@pytest.mark.parametrize('field,index', [('programIdIndex', -1), ('programIdIndex', 999),
                                         ('programIdIndex', True), ('account', -1),
                                         ('account', 999), ('account', True)])
def test_invalid_compiled_indexes_are_unknown(field, index):
    def change(state):
        compiled_inner(state)
        state['invalid_compiled'] = {field: index}
    result, _ = scenario(change)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'invalid_account_index'


def test_shared_ledger_uses_the_official_account_layout():
    def change(state):
        # Source: jup-ag/jupiter-cpi/idl.json, sharedAccountsRouteWithTokenLedger.
        state['built_sell']['swapInstruction'] = ix('shared_accounts_route_with_token_ledger', [
            native.TOKEN_PROGRAM, POOL, USER, TOKEN_ACCOUNT, POOL, POOL, USDC_ACCOUNT,
            MINT, native.USDC_MINT, JUP, native.TOKEN_2022_PROGRAM, LEDGER, POOL, JUP])
    result, _ = scenario(change)
    assert result['status'] == 'measured', result
    assert result['sold_atomic'] == '123456'


_BEHIND = {'jsonrpc': '2.0', 'id': 1,
           'error': {'code': -32016, 'message': 'Minimum context slot has not been reached',
                     'data': {'contextSlot': 9}}}


def test_a_node_behind_the_quote_slot_is_retried_and_the_measurement_lands(monkeypatch):
    """kakacoin d447feee (2026-09-22): Helius answered -32016 on 81 of 81 refusals since 09-13, so
    the sequential measurement never ran and every refusal stood on the independent reading."""
    monkeypatch.setattr(measurement, '_sleep', lambda _s: None)
    result, state = scenario(lambda s: s.update(rpc_errors=[dict(_BEHIND), dict(_BEHIND)]))
    assert result['status'] == 'measured', result
    assert state['rpc_calls'] == 3


def test_a_node_that_stays_behind_is_unknown_and_names_the_error(monkeypatch):
    monkeypatch.setattr(measurement, '_sleep', lambda _s: None)
    result, state = scenario(lambda s: s.update(
        rpc_errors=[dict(_BEHIND) for _ in range(measurement.MIN_SLOT_RETRIES + 1)]))
    assert result['status'] == 'unknown' and result['reason'] == 'rpc_failed'
    assert 'Minimum context slot' in result['detail']
    assert state['rpc_calls'] == measurement.MIN_SLOT_RETRIES + 1


def test_a_different_rpc_error_is_not_retried():
    other = {'error': {'code': -32602, 'message': 'bad params'}}
    result, state = scenario(lambda s: s.update(rpc_errors=[other]))
    assert result['reason'] == 'rpc_failed' and 'bad params' in result['detail']
    assert state['rpc_calls'] == 1


def _fee_token(sold):
    """A fee-on-transfer mint: the buy delivers 123456 gross, the whole-balance sell moves `sold`."""
    def change(state):
        state['value']['innerInstructions'] = [
            {'index': 2, 'instructions': [transfer(USDC_ACCOUNT, POOL, native.USDC_MINT, 175000000),
                                         transfer(POOL, TOKEN_ACCOUNT, MINT, 123456)]},
            {'index': 3, 'instructions': [transfer(TOKEN_ACCOUNT, POOL, MINT, sold),
                                         transfer(POOL, USDC_ACCOUNT, native.USDC_MINT, 165000000)]}]
    return change


def test_a_fee_on_transfer_sell_inside_the_configured_cap_is_measured():
    """DINU 2a7527b9 (2026-09-22, transfer_fee_bps=300): sold 3.0% less than acquired."""
    result, _ = scenario(_fee_token(119752), max_transfer_fee_bps=300)
    assert result['status'] == 'measured', result
    assert 299 <= result['transfer_shortfall_bps'] <= 301
    assert result['cost_pct'] == float((Decimal(175000000 - 165000000) / Decimal(175000000)) * 100)


def test_the_same_shortfall_is_a_mismatch_without_the_cap():
    result, _ = scenario(_fee_token(119752))
    assert result['reason'] == 'acquired_quantity_mismatch'


def test_a_shortfall_past_the_cap_is_still_a_mismatch():
    result, _ = scenario(_fee_token(118000), max_transfer_fee_bps=300)   # 4.4% short
    assert result['reason'] == 'acquired_quantity_mismatch'
