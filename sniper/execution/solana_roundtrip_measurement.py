"""Measure one unsigned sequential swap pair. This module does not grant entry authority.

Only Jupiter v1 ledger routes, idempotent ATA setup, and wallet WSOL cleanup are supported.
Missing balances, unknown token instructions, and unsupported routes produce unknown results.
The result excludes two-transaction fees, latency, and external trades between transactions.
"""
from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal

from sniper.execution.entry_attribution import public_quote
from sniper.execution.pump_native_exit import (
    ATA_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, USDC_MINT, ata,
    instruction_from_json, lookup_table_from_account,
)

JUPITER = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
COMPUTE = 'ComputeBudget111111111111111111111111111111'
WSOL = 'So11111111111111111111111111111111111111112'
# A PROVIDER NODE BEHIND THE QUOTE'S SLOT IS A RETRY, NOT A VERDICT (2026-09-22, kakacoin d447feee).
# Every read pins `minContextSlot` to the Jupiter quote's contextSlot, and a load-balanced fleet
# (Helius) answers from nodes that run tens of slots apart: -32016 "Minimum context slot has not
# been reached" came back on 81 of 81 refusals since 2026-09-13, so the sequential measurement
# never corrected the independent reading and every Solana round-trip refusal stood on two impacts
# read off one untouched pool. Re-ask a few times; a different node usually answers.
MIN_SLOT_RETRIES = 6
MIN_SLOT_RETRY_S = 0.25
_sleep = None  # tests inject; production uses time.sleep
LIMITATIONS = ['atomic_same_bank_simulation', 'two_transaction_fees_unmeasured',
               'account_rent_unmeasured', 'execution_latency_unmeasured',
               'external_intervening_trades_unmeasured']


class _Unknown(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise _Unknown(reason)


def _integer(value):
    _require(not isinstance(value, bool) and isinstance(value, (str, int))
             and str(value).isdigit(), 'malformed_amount')
    return int(value)


def _disc(name):
    return hashlib.sha256(('global:' + name).encode()).digest()[:8]


def _route_accounts(ix, *, ledger=False):
    _require(ix.get('programId') == JUPITER, 'unsupported_route')
    data = base64.b64decode(ix['data'], validate=True)
    accounts = [a['pubkey'] for a in ix['accounts']]
    suffix = '_with_token_ledger' if ledger else ''
    if data[:8] == _disc('route' + suffix):
        _require(len(accounts) >= (8 if ledger else 7), 'account_identity')
        return accounts[1], accounts[2], accounts[3], accounts[7] if ledger else None
    if data[:8] == _disc('shared_accounts_route' + suffix):
        _require(len(accounts) >= 12, 'account_identity')
        return accounts[2], accounts[3], accounts[6], accounts[11] if ledger else None
    raise _Unknown('unsupported_route')


def _preparations(builds, user):
    setup, cleanup, seen_setup, seen_cleanup = [], [], set(), set()
    for built in builds:
        _require(not built.get('otherInstructions'), 'unsupported_instructions')
        for ix in built.get('computeBudgetInstructions') or []:
            _require(ix.get('programId') == COMPUTE, 'unsupported_instructions')
        for ix in built.get('setupInstructions') or []:
            _require(ix.get('programId') == ATA_PROGRAM
                     and base64.b64decode(ix['data'], validate=True) == b'\x01',
                     'unsupported_setup')
            key = json.dumps(ix, sort_keys=True)
            if key not in seen_setup:
                setup.append(ix)
                seen_setup.add(key)
        ix = built.get('cleanupInstruction')
        if ix:
            accounts = [a['pubkey'] for a in ix['accounts']]
            _require(ix.get('programId') == TOKEN_PROGRAM
                     and base64.b64decode(ix['data'], validate=True) == b'\x09'
                     and accounts == [str(ata(user, WSOL, TOKEN_PROGRAM)), user, user],
                     'unsupported_cleanup')
            key = json.dumps(ix, sort_keys=True)
            if key not in seen_cleanup:
                cleanup.append(ix)
                seen_cleanup.add(key)
    return setup, cleanup


def _balances(value, user, keys, usdc_account, target_account, mint):
    totals = []
    for key in ('preTokenBalances', 'postTokenBalances'):
        rows = value.get(key)
        _require(isinstance(rows, list), 'missing_balance_evidence')
        amounts, seen, accounts = {}, set(), set()
        for row in rows:
            _require(isinstance(row, dict), 'missing_balance_evidence')
            if row.get('owner') != user:
                continue
            account = _integer(row.get('accountIndex'))
            _require(account not in seen and account < len(keys), 'missing_balance_evidence')
            seen.add(account)
            address = keys[account]
            accounts.add(address)
            token = row['mint']
            expected = {usdc_account: USDC_MINT, target_account: mint}.get(address)
            _require(expected is None or token == expected, 'account_identity')
            places = _integer(row['uiTokenAmount']['decimals'])
            _require(places <= 255 and (token != USDC_MINT or places == 6), 'malformed_amount')
            amounts[token] = amounts.get(token, 0) + _integer(row['uiTokenAmount']['amount'])
        _require(usdc_account in accounts and (key != 'postTokenBalances' or target_account in accounts),
                 'missing_balance_evidence')
        totals.append(amounts)
    pre, post = totals
    _require(USDC_MINT in pre and USDC_MINT in post, 'missing_balance_evidence')
    for token in set(pre) | set(post):
        if token != USDC_MINT:
            _require(pre.get(token, 0) == post.get(token, 0), 'residual_inventory')
    return post[USDC_MINT] - pre[USDC_MINT]


def _indexed_key(index, keys):
    _require(type(index) is int and 0 <= index < len(keys), 'invalid_account_index')
    return keys[index]


def _transfer(ix, keys):
    program = ix.get('programId')
    if 'programIdIndex' in ix:
        resolved = _indexed_key(ix['programIdIndex'], keys)
        _require(program in (None, resolved), 'account_identity')
        program = resolved
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return None
    parsed = ix.get('parsed')
    if isinstance(parsed, dict):
        kind, info = parsed.get('type'), parsed.get('info') or {}
        if kind == 'transferChecked':
            return info['source'], info['destination'], _integer(info['tokenAmount']['amount']), info['mint']
        if kind == 'transfer':
            return info['source'], info['destination'], _integer(info['amount']), None
        # These instructions do not transfer token balances.
        _require(kind in ('initializeAccount', 'initializeAccount2', 'initializeAccount3',
                          'initializeImmutableOwner', 'getAccountDataSize', 'syncNative',
                          'closeAccount', 'approve', 'approveChecked', 'revoke'),
                 'unsupported_token_instruction')
        return None
    import base58
    data = base58.b58decode(ix['data'])
    accounts = [a if isinstance(a, str) else _indexed_key(a, keys) for a in ix['accounts']]
    if len(data) == 9 and data[0] == 3:
        return accounts[0], accounts[1], int.from_bytes(data[1:9], 'little'), None
    if len(data) == 10 and data[0] == 12:
        return accounts[0], accounts[2], int.from_bytes(data[1:9], 'little'), accounts[1]
    _require(data and data[0] in (1, 4, 5, 9, 16, 17, 18, 21, 22),
             'unsupported_token_instruction')
    return None


def _leg_transfers(value, index, keys, usdc_account, target_account, mint):
    rows = value.get('innerInstructions')
    _require(isinstance(rows, list), 'missing_transfer_evidence')
    groups = [r for r in rows if r.get('index') == index]
    _require(len(groups) == 1, 'missing_transfer_evidence')
    delta = {usdc_account: 0, target_account: 0}
    seen = set()
    for ix in groups[0]['instructions']:
        parsed = _transfer(ix, keys)
        if parsed is None:
            continue
        source, dest, amount, token = parsed
        for account, expected_mint in ((usdc_account, USDC_MINT), (target_account, mint)):
            if account in (source, dest):
                _require(token in (None, expected_mint), 'account_identity')
                seen.add(account)
                delta[account] += amount * ((dest == account) - (source == account))
    _require(len(seen) == 2, 'missing_transfer_evidence')
    return delta[usdc_account], delta[target_account]


def measure_roundtrip(buy_quote, sell_quote, *, user, jup_call, rpc, max_transfer_fee_bps=0):
    """Return versioned evidence from one unsigned simulation and two instruction builds.

    The callbacks perform provider reads only. No callback can receive a signed transaction.
    Jupiter callbacks return (HTTP status, JSON). RPC callbacks return an envelope or its result.
    """
    from solders.compute_budget import set_compute_unit_limit
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    result = {'version': 'solana_sequential_roundtrip_v1', 'status': 'unknown', 'reason': None,
              'cost_pct': None, 'limitations': list(LIMITATIONS)}
    phase = 'invalid_input'
    try:
        mint = buy_quote['outputMint']
        spent = _integer(buy_quote['inAmount'])
        _require(spent > 0 and _integer(buy_quote['outAmount']) > 0, 'invalid_input')
        _require(buy_quote['inputMint'] == sell_quote['outputMint'] == USDC_MINT
                 and sell_quote['inputMint'] == mint and mint != USDC_MINT, 'quote_identity')
        _require(all(q.get('swapMode', 'ExactIn') == 'ExactIn' and not q.get('_venue')
                     for q in (buy_quote, sell_quote)), 'unsupported_route')
        min_slot = max(_integer(q['contextSlot']) for q in (buy_quote, sell_quote))
        result.update(mint=mint, user=user, quoted_buy_output_atomic=str(buy_quote['outAmount']),
                      quoted_sell_input_atomic=str(sell_quote['inAmount']),
                      route_ids=[[hop['swapInfo']['ammKey'] for hop in q['routePlan']]
                                 for q in (buy_quote, sell_quote)])
        phase = 'build_failed'
        builds = []
        for q, use_ledger in ((buy_quote, False), (sell_quote, True)):
            status, built = jup_call('POST', '/swap-instructions', body=json.dumps({
                'userPublicKey': user, 'quoteResponse': public_quote(q),
                'useTokenLedger': use_ledger, 'dynamicComputeUnitLimit': False,
                'prioritizationFeeLamports': 0, 'wrapAndUnwrapSol': True}))
            _require(status == 200 and isinstance(built, dict) and built.get('swapInstruction'), phase)
            builds.append(built)
        buy, sell = builds
        ledger = sell.get('tokenLedgerInstruction')
        _require(isinstance(ledger, dict) and not buy.get('tokenLedgerInstruction'), 'unsupported_ledger')
        _require(ledger.get('programId') == JUPITER
                 and base64.b64decode(ledger['data'], validate=True) == _disc('set_token_ledger'),
                 'unsupported_ledger')
        owner, usdc_account, target_account, _ = _route_accounts(buy['swapInstruction'])
        sell_owner, sell_source, sell_destination, ledger_account = _route_accounts(
            sell['swapInstruction'], ledger=True)
        _require(owner == sell_owner == user and sell_source == target_account
                 and sell_destination == usdc_account
                 and [a['pubkey'] for a in ledger['accounts']] == [ledger_account, target_account]
                 and usdc_account == str(ata(user, USDC_MINT, TOKEN_PROGRAM))
                 and target_account in {str(ata(user, mint, p)) for p in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)},
                 'account_identity')
        setup, cleanup = _preparations(builds, user)
        raw = setup + [ledger, buy['swapInstruction'], sell['swapInstruction']] + cleanup
        _require(all(not a.get('isSigner') or a['pubkey'] == user
                     for ix in raw for a in ix['accounts']), 'unsupported_signer')
        buy_index, sell_index = len(setup) + 2, len(setup) + 3
        instructions = [set_compute_unit_limit(1_400_000)] + [instruction_from_json(i) for i in raw]
        table_keys = list(dict.fromkeys(key for b in builds for key in b.get('addressLookupTableAddresses', [])))
        _require(len(table_keys) <= 64, 'transaction_limits')
        phase = 'rpc_failed'

        def read(method, params):
            import time as _time
            response = None
            for attempt in range(MIN_SLOT_RETRIES + 1):
                response = rpc(method, params)
                err = response.get('error') if isinstance(response, dict) else None
                if isinstance(err, dict) and err.get('code') == -32016 and attempt < MIN_SLOT_RETRIES:
                    (_sleep or _time.sleep)(MIN_SLOT_RETRY_S)
                    continue
                break
            if not (isinstance(response, dict) and not response.get('error')):
                # The evidence used to drop the message; 81 refusals read "rpc_failed" and nothing else.
                result['detail'] = str(response.get('error') if isinstance(response, dict)
                                       else response)[:200]
                _require(False, 'rpc_failed')
            return response.get('result', response)

        tables = []
        if table_keys:
            values = read('getMultipleAccounts', [table_keys, {'encoding': 'base64'}]).get('value')
            _require(isinstance(values, list) and len(values) == len(table_keys), 'lookup_unavailable')
            for key, value in zip(table_keys, values):
                _require(isinstance(value, dict), 'lookup_unavailable')
                tables.append(lookup_table_from_account(key, value['data'][0]))
        message = MessageV0.try_compile(Pubkey.from_string(user), instructions, tables, Hash.default())
        unsigned = VersionedTransaction(message, [NullSigner(Pubkey.from_string(user))])
        _require(len(bytes(unsigned)) <= 1232, 'transaction_limits')
        keys = [str(k) for k in message.account_keys]
        by_table = {str(t.key): t for t in tables}
        for field in ('writable_indexes', 'readonly_indexes'):
            for lookup in message.address_table_lookups:
                keys.extend(str(by_table[str(lookup.account_key)].addresses[i]) for i in getattr(lookup, field))
        _require(len(keys) <= 64, 'transaction_limits')
        simulation = read('simulateTransaction', [base64.b64encode(bytes(unsigned)).decode(), {
            'encoding': 'base64', 'sigVerify': False, 'replaceRecentBlockhash': True,
            'commitment': 'processed', 'minContextSlot': min_slot, 'innerInstructions': True,
            'accounts': {'encoding': 'base64', 'addresses': [usdc_account, target_account]}}])
        phase = 'malformed_simulation'
        result['simulated_slot'] = _integer(simulation['context']['slot'])
        _require(result['simulated_slot'] >= min_slot, 'slot_mismatch')
        value = simulation['value']
        _require(isinstance(value, dict) and 'err' in value and value['err'] is None, 'simulation_failed')
        delta = _balances(value, user, keys, usdc_account, target_account, mint)
        buy_usdc, acquired = _leg_transfers(value, buy_index, keys, usdc_account, target_account, mint)
        sell_usdc, sold = _leg_transfers(value, sell_index, keys, usdc_account, target_account, mint)
        _require(buy_usdc == -spent, 'entry_amount_mismatch')
        # A FEE-ON-TRANSFER TOKEN CAN NEVER SELL EXACTLY WHAT IT BOUGHT (2026-09-22, DINU
        # 2a7527b9, transfer_fee_bps=300). The buy leg's transfer is the gross amount and the fee
        # is withheld from our account, so the whole-balance sell moves gross minus fee. Exact
        # equality made every such token "acquired_quantity_mismatch" -> unknown, and the refusal
        # fell back to the independent reading, which counts that fee twice on top of two impacts.
        # The tolerance is the SAME cap token safety already enforces on the mint's fee
        # (MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS), plus one unit of rounding. The cost
        # itself is read from the USDC legs and needs no token identity at all.
        shortfall = acquired + sold                      # sold is negative: what left our account
        _require(acquired > 0 and 0 < -sold <= acquired
                 and shortfall * 10_000 <= acquired * int(max_transfer_fee_bps or 0) + 10_000,
                 'acquired_quantity_mismatch')
        result['transfer_shortfall_bps'] = float(Decimal(shortfall * 10_000) / Decimal(acquired))
        _require(sell_usdc > 0 and buy_usdc + sell_usdc == delta, 'usdc_reconciliation')
        _require('fee' in value, 'missing_fee_evidence')
        fee = _integer(value['fee'])
        result.update(status='measured', reason=None, buy_spent_atomic=str(spent),
                      acquired_atomic=str(acquired), sold_atomic=str(-sold),
                      sell_received_atomic=str(sell_usdc), usdc_delta_atomic=str(delta),
                      cost_pct=float((Decimal(spent - sell_usdc) / Decimal(spent)) * 100),
                      simulation_fee_lamports=str(fee), units_consumed=value.get('unitsConsumed'))
    except _Unknown as exc:
        result['reason'] = str(exc)
    except Exception:
        result['reason'] = phase
    return result
