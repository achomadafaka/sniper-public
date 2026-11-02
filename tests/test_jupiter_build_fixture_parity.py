"""Offline Task 8 control. Doc-shaped /build fixtures cannot approve a migration."""
from copy import deepcopy
import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from solders.compute_budget import set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction

from sniper.execution import pump_curve_buy as P
from sniper.execution.solana_token_safety import token_safety_verdict
from test_slot_lane_pump_alt import _table_account


def _json_ix(ix):
    return {'programId': str(ix.program_id), 'data': base64.b64encode(ix.data).decode(),
            'accounts': [{'pubkey': str(a.pubkey), 'isSigner': a.is_signer,
                          'isWritable': a.is_writable} for a in ix.accounts]}


def _fixture():
    """Synthetic amounts/API responses; captured mint/payer, existing opaque test-leg bytes.

    The one-byte Jupiter payload comes from test_pump_curve_buy's injected /swap-instructions
    fixture. It is NOT a live response, valid route or known V2 instruction. Never decode a floor
    from it. The documented V2 envelope is the experiment, not executable-provider evidence.
    """
    live = json.loads((Path(__file__).parent / 'fixtures/pump_curve_buy_frames.json').read_text())
    captured = live['direct_buy_exact_sol_in']
    user = captured['accounts'][6][0]
    mint = P.parse_trade_event(captured['trade_event_log'])['mint']
    program = P.TOKEN_2022_PROGRAM
    setup = Instruction(P._pk(P.ATA_PROGRAM), b'\x01', [
        AccountMeta(P._pk(user), True, True),
        AccountMeta(P.ata(user, mint, program), False, True),
        AccountMeta(P._pk(user), False, False), AccountMeta(P._pk(mint), False, False),
        AccountMeta(P._pk(P.SYSTEM_PROGRAM), False, False),
        AccountMeta(P._pk(program), False, False)])
    table_key, keys = P.USDC_MINT, P.pump_alt_keys(user)
    swap = {'programId': 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4',
            'data': base64.b64encode(b'\x00').decode(),
            'accounts': [{'pubkey': user, 'isSigner': True, 'isWritable': True},
                         {'pubkey': keys[0], 'isSigner': False, 'isWritable': True}]}
    quote = {'inputMint': P.USDC_MINT, 'outputMint': mint, 'inAmount': '5000000',
             'outAmount': '30400000', 'otherAmountThreshold': '30000000',
             'swapMode': 'ExactIn', 'slippageBps': 132,
             'routePlan': [{'percent': 100, 'swapInfo': {'ammKey': keys[0],
                           'label': 'fixture-only', 'inputMint': P.USDC_MINT,
                           'outputMint': mint, 'inAmount': '5000000', 'outAmount': '30400000'}}]}
    built = {'setupInstructions': [_json_ix(setup)], 'swapInstruction': swap,
             'cleanupInstruction': None, 'addressLookupTableAddresses': [table_key]}
    v2 = {**deepcopy(quote), **deepcopy(built), 'transactionVersion': 0,
          'computeBudgetInstructions': [_json_ix(set_compute_unit_price(200000))],
          'otherInstructions': [], 'tipInstruction': None,
          'addressesByLookupTableAddress': {table_key: keys},
          'blockhashWithMetadata': {'blockhash': list(bytes(Hash.default())),
                                   'lastValidBlockHeight': 450780600}}
    del v2['addressLookupTableAddresses']
    v2['routePlan'][0]['bps'] = 10000
    reverse = {'inputMint': mint, 'outputMint': P.USDC_MINT, 'inAmount': '30400000',
               'outAmount': '4850000', 'otherAmountThreshold': '4800000',
               'swapMode': 'ExactIn'}
    return {'user': user, 'mint': mint, 'program': program, 'quote': quote, 'v1': built,
            'v2': v2, 'clip_atomic': 5000000, 'source_price': '0.17', 'cap_pct': 35,
            'reverse': reverse, 'max_roundtrip_pct': 15,
            'alt_account': _table_account(keys, authority=user),
            'safety': {'owner': program, 'data': {'parsed': {'type': 'mint', 'info': {
                'decimals': 6, 'mintAuthority': None, 'freezeAuthority': None,
                'extensions': []}}}},
            'hash': str(Hash.default()), 'height': 450780599,
            'last_valid_height': 450780600}


def _compare(f):
    """Test-local field/assembly control, deliberately unable to approve V2 signed semantics."""
    q, v2 = f['quote'], f['v2']
    fields = ('inputMint', 'outputMint', 'inAmount', 'outAmount', 'otherAmountThreshold',
              'swapMode', 'slippageBps')
    if any(q.get(k) != v2.get(k) for k in fields):
        raise ValueError('quote mismatch/missing field')
    if (q['inputMint'] != P.USDC_MINT or q['outputMint'] != f['mint']
            or q['swapMode'] != 'ExactIn' or not 0 < int(q['otherAmountThreshold']) <= int(q['outAmount'])):
        raise ValueError('quote identity/atomic bound')
    if int(q['inAmount']) != f['clip_atomic']:
        raise ValueError('clip differs from current approval')
    required = P.source_price_floor_atomic(f['clip_atomic'], f['source_price'], f['cap_pct'], 6)
    if int(q['otherAmountThreshold']) < required:
        raise ValueError('source floor exceeds quote floor')
    if v2.get('transactionVersion') != 0:
        raise ValueError('version unsupported')
    price = [_json_ix(set_compute_unit_price(200000))]
    if (v2.get('computeBudgetInstructions') != price or v2.get('otherInstructions') != []
            or v2.get('tipInstruction') is not None):
        raise ValueError('fee/extra instruction mismatch')
    for key in ('setupInstructions', 'cleanupInstruction'):
        if v2.get(key) != f['v1'].get(key):
            raise ValueError('setup/cleanup mismatch')
    swap = v2['swapInstruction']
    if any(a.get('isSigner') and a['pubkey'] != f['user'] for a in swap['accounts']):
        raise ValueError('signer mismatch')
    table_key = f['v1']['addressLookupTableAddresses'][0]
    table, reason = P.verified_pump_alt(table_key, f['alt_account'], f['user'])
    if reason or table is None or v2.get('addressesByLookupTableAddress') != {
            table_key: [str(k) for k in table.addresses]}:
        raise ValueError('ALT missing/owner/deactivation/addresses')
    bh = v2.get('blockhashWithMetadata') or {}
    if (bh.get('blockhash') != list(bytes(Hash.from_string(f['hash'])))
            or bh.get('lastValidBlockHeight') != f['last_valid_height']):
        raise ValueError('hash identity/expiry pair')
    if f['height'] > f['last_valid_height']:
        raise ValueError('expiry passed')
    safety = f.get('safety')
    ok, _ = token_safety_verdict(safety, None, max_transfer_fee_bps=300,
                                allow_live_fee_authority=False)
    if (not ok or (safety or {}).get('owner') != f['program']
            or safety['data']['parsed']['info'].get('decimals') != 6):
        raise ValueError('safety unknown/program/decimals/refusal')
    reverse = f.get('reverse') or {}
    if (reverse.get('inputMint') != f['mint'] or reverse.get('outputMint') != P.USDC_MINT
            or reverse.get('inAmount') != q['outAmount']
            or reverse.get('swapMode') != 'ExactIn'
            or not 0 < int(reverse.get('otherAmountThreshold', 0)) <= int(reverse.get('outAmount', 0))
            or 100 * (1 - int(reverse['outAmount']) / f['clip_atomic']) > f['max_roundtrip_pct']):
        raise ValueError('independent reverse quote missing/refused')
    requests = {'v1': [], 'v2': []}
    def request(version, method, path, body=None):
        query = parse_qs(urlsplit(path).query)
        reverse_call = query.get('inputMint') == [f['mint']]
        requests[version].append((method, path if reverse_call or method == 'POST' else
                                  ('forward_quote' if version == 'v1' else 'forward_build')))
        if reverse_call:
            return 200, deepcopy(reverse)
        return 200, deepcopy(q if version == 'v1' and method == 'GET' else f[version])
    # Current production V1 prefetch/JSON instruction parser, with already validated private ALT.
    leg = P.prefetch_usdc_sol_leg(f['clip_atomic'], q['slippageBps'], user=f['user'],
        jup_call=lambda *a, **kw: request('v1', *a, **kw),
        rpc=lambda *a: pytest.fail('fixture unexpectedly requested RPC'),
        alt_cache={table_key: table}, clock=lambda: 1000, unwrap=False, output_mint=f['mint'])
    assert leg is not None and leg['min_out_lamports'] == int(q['otherAmountThreshold'])
    _, candidate = request('v2', 'GET', '/build?' + urlencode({
        'inputMint': P.USDC_MINT, 'outputMint': f['mint'], 'amount': f['clip_atomic'],
        'taker': f['user'], 'slippageBps': q['slippageBps'], 'wrapAndUnwrapSol': 'false',
        'transactionVersion': 0}))
    candidate_ixs = [P.instruction_from_json(ix) for ix in candidate['setupInstructions']]
    candidate_ixs.append(P.instruction_from_json(candidate['swapInstruction']))
    # Current assembler adds our approved 400k CU limit/200k CU price; no simulation/sign/send.
    candidate_message, size = P._assemble(f['user'], candidate_ixs, [table], f['hash'],
                                          400000, 200000, None)
    current_message, _ = P._assemble(f['user'], leg['ixs'], leg['tables'], f['hash'],
                                    400000, 200000, None)
    if bytes(candidate_message) != bytes(current_message):
        raise ValueError('instruction/message differs despite matching quote JSON')
    assert len(candidate_message.address_table_lookups) == 1
    for version in requests:
        request(version, 'GET', '/quote?inputMint=' + f['mint'] + '&outputMint=' +
                P.USDC_MINT + '&amount=' + q['outAmount'])
    return {'fields_match': True, 'signed_floor': 'unknown', 'route_fee_parity': 'unknown',
            'replacement_allowed': False,
            'reason': 'opaque fixture instruction; V2 floor, route fees and simulation unproved',
            'jupiter_requests': {v: len(calls) for v, calls in requests.items()},
            'requests': requests, 'packet_bytes': size,
            'approved_clip_atomic': f['clip_atomic'], 'source_floor_atomic': required,
            'quote_floor_atomic': int(q['otherAmountThreshold']),
            'cu_limit': 400000, 'cu_price_microlamports': 200000,
            'priority_fee_lamports': 80000, 'rpc_calls': 0,
            'alt_provenance': 'independently validated synthetic account'}


def _mutate(f, which):
    v2 = f['v2']
    if which == 'floor': v2['otherAmountThreshold'] = '29999999'
    elif which == 'fee': v2['computeBudgetInstructions'] = [_json_ix(set_compute_unit_price(200001))]
    elif which == 'alt': v2['addressesByLookupTableAddress'][P.USDC_MINT].reverse()
    elif which == 'hash': v2['blockhashWithMetadata']['blockhash'][0] = 1
    elif which == 'safety': f['safety']['data']['parsed']['info']['freezeAuthority'] = f['user']
    elif which == 'clip': f['clip_atomic'] += 1
    elif which == 'setup': v2['setupInstructions'] = []
    elif which == 'mint': v2['outputMint'] = P.WSOL_MINT
    elif which == 'expiry': f['height'] = f['last_valid_height'] + 1
    elif which == 'signer': v2['swapInstruction']['accounts'][0]['pubkey'] = P.USDC_MINT
    elif which == 'packet': v2['swapInstruction']['data'] = base64.b64encode(b'x' * 1500).decode()
    elif which == 'reverse': f['reverse']['outAmount'] = '100'
    elif which == 'version': v2['transactionVersion'] = 1
    elif which == 'alt_owner': f['alt_account']['owner'] = P.TOKEN_PROGRAM
    elif which == 'alt_deactivated':
        f['alt_account'] = _table_account(P.pump_alt_keys(f['user']), deactivation=123,
                                         authority=f['user'])
    elif which == 'floor_missing': del v2['otherAmountThreshold']
    elif which == 'fee_missing': del v2['computeBudgetInstructions']
    elif which == 'alt_missing': del v2['addressesByLookupTableAddress']
    elif which == 'hash_missing': del v2['blockhashWithMetadata']
    elif which == 'safety_missing': del f['safety']
    else: raise AssertionError(which)


def test_matching_fixture_still_cannot_prove_v2_signed_floor():
    result = _compare(_fixture())
    assert result['fields_match'] is True
    assert result['signed_floor'] == 'unknown'
    assert result['replacement_allowed'] is False
    assert result['jupiter_requests'] == {'v1': 3, 'v2': 2}
    assert result['packet_bytes'] <= 1232


@pytest.mark.parametrize('mutation, reason', [
    ('floor', 'quote'), ('fee', 'fee'), ('alt', 'ALT'), ('hash', 'hash'),
    ('safety', 'safety'), ('clip', 'clip'), ('setup', 'setup'),
    ('mint', 'quote'), ('expiry', 'expiry'), ('signer', 'signer'),
    ('packet', 'packet'), ('reverse', 'reverse'), ('version', 'version'),
    ('alt_owner', 'ALT'), ('alt_deactivated', 'ALT'),
    ('floor_missing', 'quote'), ('fee_missing', 'fee'),
    ('alt_missing', 'ALT'), ('hash_missing', 'hash'), ('safety_missing', 'safety'),
])
def test_changed_or_absent_boundary_cannot_approve_replacement(mutation, reason):
    fixture = _fixture()
    _mutate(fixture, mutation)
    with pytest.raises(ValueError, match=reason):
        _compare(fixture)


def test_opaque_instruction_cannot_gain_authority_from_a_json_floor_claim():
    fixture = _fixture()
    fixture['v2']['signedMinimumOutput'] = fixture['v2']['otherAmountThreshold']
    fixture['v2']['platformFee'] = {'feeBps': 0, 'amount': '0'}
    result = _compare(fixture)
    assert result['signed_floor'] == 'unknown'
    assert result['route_fee_parity'] == 'unknown'
    assert result['replacement_allowed'] is False


def test_an_independent_reverse_quote_is_retained_in_both_request_graphs():
    result = _compare(_fixture())
    assert result['requests']['v1'][-1][1].startswith('/quote?inputMint=' + _fixture()['mint'])
    assert result['requests']['v2'][-1] == result['requests']['v1'][-1]
    assert result['requests']['v1'][:2] == [('GET', 'forward_quote'), ('POST', '/swap-instructions')]
    assert result['requests']['v2'][0] == ('GET', 'forward_build')


def test_approved_clip_and_source_floor_are_independent_of_the_candidate_json():
    fixture = _fixture()
    fixture['source_price'] = '0.1'
    with pytest.raises(ValueError, match='source floor'):
        _compare(fixture)


def test_matching_quote_json_does_not_prove_unchanged_instruction_bytes():
    fixture = _fixture()
    fixture['v2']['swapInstruction']['data'] = base64.b64encode(b'\x01').decode()
    with pytest.raises(ValueError, match='instruction'):
        _compare(fixture)
