"""Full-only source sells require complete owner balances; all RPC is injected and offline."""
import copy
import base64
import json
import threading
import time
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from test_memecoin_source_sell_mirror import world, _open, _run, _sells, _watched, FX, ssm


def _answer(amounts, *, slot=999999999):
    owner, mint = FX['65826']['owner'], FX['65826']['mint']
    return {'result': {'context': {'slot': slot}, 'value': [
        {'pubkey': 'account%d' % i, 'account': {'owner': ssm.TOKEN_PROGRAM,
         'data': {'program': 'spl-token', 'parsed': {'type': 'account', 'info': {
             'owner': owner, 'mint': mint, 'tokenAmount': {'amount': str(amount)}}}}}}
        for i, amount in enumerate(amounts)]}}


def _watch(world, answer):
    calls = []
    owner, mint = FX['65826']['owner'], FX['65826']['mint']
    world.mirror._acct_by_key.setdefault((owner, mint), {'account': 'known-source-account'})

    def post(body):
        request = json.loads(body)
        calls.append((request, threading.current_thread().name))
        if request['method'] == 'getAccountInfo':
            return _account_answer(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    world.mirror.min_fraction = 1.0
    world.mirror.account_watch = ssm.OwnerAccountWatch(ws_url='ws://offline', rpc_url='http://offline',
                                                     sink=None, post=post)
    return calls


def _drain(world):
    records = []
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        records.extend(world.mirror.run(world.conn, world.contexts, exit_lock=world.lock))
        if not world.mirror._pending:
            return records
        time.sleep(.005)
    pytest.fail('offline resolver did not finish')


def _account_answer(amount, *, slot=999999999):
    value = None
    if amount is not None:
        raw = bytearray(165)
        raw[:32] = bytes(Pubkey.from_string(FX['65826']['mint']))
        raw[32:64] = bytes(Pubkey.from_string(FX['65826']['owner']))
        raw[64:72], raw[108] = amount.to_bytes(8, 'little'), 1
        value = {'owner': ssm.TOKEN_PROGRAM, 'data': [base64.b64encode(raw).decode(), 'base64']}
    return {'result': {'context': {'slot': slot}, 'value': value}}


def _watch_zero_with_account(world, account_answer):
    [sale] = _sells('65826')
    _watched(world, '65826', sale.sold_atomic * 2, sale.slot - 1)
    calls = _watch(world, None)
    def post(body):
        request = json.loads(body)
        calls.append((request, threading.current_thread().name))
        return (_answer([], slot=sale.slot + 1) if request['method'] == 'getTokenAccountsByOwner'
                else account_answer)
    world.mirror.account_watch._post = post
    return calls


@pytest.mark.parametrize('mutation', ['positive', 'wrong_owner', 'wrong_mint', 'wrong_program',
                                    'stale', 'rpc_error', 'missing_value', 'malformed'])
def test_zero_owner_index_cannot_hide_a_known_source_account(world, mutation):
    _open(world, '65826')
    [sale] = _sells('65826')
    answer = _account_answer(1 if mutation == 'positive' else 0, slot=sale.slot + 1)
    result = answer['result']
    value = result['value']
    if mutation in ('wrong_owner', 'wrong_mint'):
        raw = bytearray(base64.b64decode(value['data'][0]))
        offset = 32 if mutation == 'wrong_owner' else 0
        raw[offset:offset + 32] = bytes(Pubkey.new_unique())
        value['data'][0] = base64.b64encode(raw).decode()
    elif mutation == 'wrong_program': value['owner'] = '11111111111111111111111111111111'
    elif mutation == 'stale': result['context']['slot'] = sale.slot
    elif mutation == 'rpc_error': answer['error'] = {'code': -32005}
    elif mutation == 'missing_value': del result['value']
    elif mutation == 'malformed': value['data'] = ['AA==', 'base64']
    calls = _watch_zero_with_account(world, answer)
    _run(world, sale)
    _drain(world)
    assert not world.calls
    assert len(calls) == 2
    assert calls[1][0]['params'][1]['minContextSlot'] == sale.slot + 1
    assert calls[1][1] == 'source-owner-resolver'


def test_empty_owner_index_without_a_known_source_account_is_unknown():
    watch = ssm.OwnerAccountWatch(ws_url='ws://offline', rpc_url='http://offline', sink=None,
                                 post=lambda body: _answer([]))
    assert watch.full_owner_balance(FX['65826']['owner'], FX['65826']['mint'], 10) is None


@pytest.mark.parametrize('amount', [0, None])
def test_known_source_account_zero_or_closed_preserves_genuine_full_sell(world, amount):
    pid = _open(world, '65826')
    _watch_zero_with_account(world, _account_answer(amount))
    _run(world, *_sells('65826'))
    _drain(world)
    assert world.calls == [(pid, ssm.REASON, True)]


@pytest.mark.parametrize('watcher_first', [True, False])
@pytest.mark.parametrize('later_slot_offset', [0, 1])
@pytest.mark.parametrize('proof_shape', ['complete', 'missing_post', 'missing_pre'])
def test_same_signature_confirmed_partial_vetoes_native_zero_owner_index(world, watcher_first,
                                                                       later_slot_offset, proof_shape):
    _open(world, '65826')
    [sale] = _sells('65826')
    _watch_zero_with_account(world, _account_answer(0))
    confirmed = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=sale.sold_atomic, pre_atomic=sale.sold_atomic * 2,
        post_atomic=sale.sold_atomic, seen_at=world.mirror._real_now(), recv_mono=world.clock.t)
    if proof_shape != 'complete':
        confirmed = replace(confirmed, **{'post_atomic' if proof_shape == 'missing_post' else 'pre_atomic': None})
    if not watcher_first:
        _run(world, sale)
    world.mirror._watcher_rows = lambda conn: [confirmed]
    _run(world, *([sale] if watcher_first else []))
    _drain(world)
    assert not world.calls
    world.mirror._watcher_rows = lambda conn: []
    later = replace(confirmed, signature='later-full-sale', slot=sale.slot + later_slot_offset,
                    pre_atomic=sale.sold_atomic, post_atomic=0)
    world.mirror._watcher_rows = lambda conn: [later]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    _drain(world)
    assert len(world.calls) == 1


def test_confirmed_full_sale_still_holds_when_another_owner_account_has_tokens(world):
    _open(world, '65826')
    calls = _watch(world, _answer([0, 1]))
    [sale] = _sells('65826')
    confirmed = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=sale.sold_atomic, pre_atomic=sale.sold_atomic,
        post_atomic=0, seen_at=world.mirror._real_now(), recv_mono=world.clock.t)
    world.mirror._watcher_rows = lambda conn: [confirmed]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    _drain(world)
    assert not world.calls
    assert len(calls) == 1


def _unsigned_full_retry(world, status, defer_attempts=1):
    pid = _open(world, '65826')
    [sale] = _sells('65826')
    _watch_zero_with_account(world, _account_answer(0))

    def exit_fn(conn, pos, reason, now):
        world.calls.append((pos['id'], reason, world.lock.locked()))
        return {'status': status if len(world.calls) <= defer_attempts else 'closed'}

    world.contexts['solana']['exit_fn'] = exit_fn
    _run(world, sale)
    _drain(world)
    assert world.calls == [(pid, ssm.REASON, True)]
    assert len(world.mirror._retries) == 1
    partial = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=sale.sold_atomic, pre_atomic=sale.sold_atomic * 2,
        post_atomic=sale.sold_atomic, seen_at=world.mirror._real_now(), recv_mono=world.clock.t)
    return pid, partial


@pytest.mark.parametrize('status', ['balance_unreadable', 'stuck'])
@pytest.mark.parametrize('defer_attempts', [1, 2])
@pytest.mark.parametrize('proof_shape', ['complete', 'missing_post', 'missing_pre'])
def test_confirmed_partial_vetoes_a_previously_queued_unsigned_full_retry(world, status, defer_attempts,
                                                                      proof_shape):
    pid, partial = _unsigned_full_retry(world, status, defer_attempts)
    if proof_shape != 'complete':
        partial = replace(partial, **{'post_atomic' if proof_shape == 'missing_post' else 'pre_atomic': None})
    for _ in range(defer_attempts - 1):
        world.clock.t = world.mirror._retries[0]['due'] + .01
        world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    world.mirror._watcher_rows = lambda conn: [partial]
    world.clock.t = world.mirror._retries[0]['due'] + .01
    records = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.calls == [(pid, ssm.REASON, True)] * defer_attempts, records
    assert not world.mirror._retries
    assert any(row['outcome'] == 'retry_vetoed' for row in records)
    later = replace(partial, signature='later-genuine-full', pre_atomic=partial.sold_atomic,
                    post_atomic=0)
    world.mirror._watcher_rows = lambda conn: [later]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    _drain(world)
    assert world.calls == [(pid, ssm.REASON, True)] * (defer_attempts + 1)


def test_partial_proof_does_not_cancel_an_independent_source_failure_exit_retry(world):
    from sniper.services.memecoin_rabbit_signal import EXIT_REASONS
    pid, partial = _unsigned_full_retry(world, 'balance_unreadable')
    reason = next(iter(EXIT_REASONS))
    world.mirror._retries[0]['reason'] = reason
    world.mirror._watcher_rows = lambda conn: [partial]
    world.clock.t += ssm.RETRY_DELAYS_S[0] + .01
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.calls == [(pid, ssm.REASON, True), (pid, reason, True)]


def test_late_partial_proof_does_not_release_a_signed_unknown_close(world):
    from sniper.persistence import db_memecoin as m
    pid = _open(world, '65826')
    [sale] = _sells('65826')
    _watch_zero_with_account(world, _account_answer(0))

    def exit_fn(conn, pos, reason, now):
        world.calls.append((pos['id'], reason, world.lock.locked()))
        claim = m.claim_position_close(conn, pid, now, reason)
        assert m.mark_position_close_submitted(conn, pid, claim, now, 'our-signed-close')
        return {'status': 'unknown', 'sig': 'our-signed-close'}

    world.contexts['solana']['exit_fn'] = exit_fn
    _run(world, sale)
    _drain(world)
    before = m.get_position(world.conn, pid)
    partial = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=sale.sold_atomic, pre_atomic=sale.sold_atomic * 2,
        post_atomic=sale.sold_atomic, seen_at=world.mirror._real_now(), recv_mono=world.clock.t)
    world.mirror._watcher_rows = lambda conn: [partial]
    world.clock.t += ssm.RETRY_DELAYS_S[0] + .01
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    after = m.get_position(world.conn, pid)
    assert world.calls == [(pid, ssm.REASON, True)] and not world.mirror._retries
    assert after['close_sig'] == before['close_sig'] == 'our-signed-close'
    assert after['close_claim_id'] == before['close_claim_id'] is not None


@pytest.mark.parametrize('pre,sold', [(100, 50), (100, 30), (10**19, 10**19 - 1)])
def test_full_only_known_partial_never_fires_or_reads(world, monkeypatch, pre, sold):
    _open(world, '65826')
    calls = _watch(world, _answer([]))
    monkeypatch.setattr(ssm, '_first_sell_policy', lambda pos: True)
    [sale] = _sells('65826')
    records = _run(world, replace(sale, pre_atomic=pre, sold_atomic=sold))
    assert not world.calls and not calls, records
    assert records[0]['outcome'] == 'held_below'
    assert records[0]['share'] is None and records[0]['cumulative'] is None
    audit = json.loads(world.conn.execute(
        "SELECT detail FROM memecoin_exit_trigger_audit WHERE reason=?", (ssm.REASON,)).fetchone()[0])
    assert audit['share'] is None and audit['cumulative'] is None


def test_full_only_cumulative_derisks_do_not_fire(world):
    _open(world, '65826')
    calls = _watch(world, _answer([]))
    [sale] = _sells('65826')
    for i in range(5):
        _run(world, replace(sale, signature='trim%d' % i, slot=sale.slot + i,
                            pre_atomic=100, sold_atomic=50))
    assert not world.calls and not calls


@pytest.mark.parametrize('answer', [_answer([1]), _answer([0, 1]), None,
                                    {'error': {'code': 429}}, RuntimeError('offline')])
def test_full_only_nonzero_or_unknown_owner_snapshot_holds(world, answer):
    _open(world, '65826')
    calls = _watch(world, answer)
    [sale] = _sells('65826')
    _run(world, sale)
    _drain(world)
    assert not world.calls
    assert len(calls) == 1


@pytest.mark.parametrize('amounts', [[], [0], [0, 0]])
def test_complete_zero_owner_snapshot_exits_once_and_off_thread(world, amounts):
    pid = _open(world, '65826')
    calls = _watch(world, _answer(amounts))
    [sale] = _sells('65826')
    _run(world, sale)
    _drain(world)
    _run(world, sale)
    assert world.calls == [(pid, ssm.REASON, True)]
    assert len(calls) == 2
    request, thread = calls[0]
    assert thread == 'source-owner-resolver'
    assert request['method'] == 'getTokenAccountsByOwner'
    assert request['params'] == [sale.owner, {'mint': sale.mint}, {
        'encoding': 'jsonParsed', 'commitment': 'processed', 'minContextSlot': sale.slot}]


def test_account_empty_transfer_cannot_independently_activate_full_only(world):
    _open(world, '65826')
    calls = _watch(world, _answer([]))
    [sale] = _sells('65826')
    account = _watched(world, '65826', 100, sale.slot - 1)
    world.mirror.offer_account(account, sale.slot, 0, world.clock.t)
    records = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert not world.calls and not calls, records


def test_late_confirmed_proof_survives_partial_native_and_account_dedupe(world):
    pid = _open(world, '65826')
    [sale] = _sells('65826')
    account = _watched(world, '65826', 100, sale.slot - 1)
    calls = _watch(world, _answer([]))
    world.mirror.offer_account(account, sale.slot, 0, world.clock.t)
    _run(world, replace(sale, pre_atomic=100, sold_atomic=50))
    confirmed = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=100, pre_atomic=100, seen_at=world.mirror._real_now(),
        recv_mono=world.clock.t)
    world.mirror._watcher_rows = lambda conn: [confirmed]
    records = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    records.extend(_drain(world))
    records.extend(world.mirror.run(world.conn, world.contexts, exit_lock=world.lock))
    assert world.calls == [(pid, ssm.REASON, True)], records
    assert len(calls) == 2


def test_snapshot_after_match_window_never_fires(world):
    _open(world, '65826')
    entered, release = threading.Event(), threading.Event()

    def post(body):
        entered.set()
        assert release.wait(2)
        return _answer([])

    _watch(world, None)
    world.mirror.account_watch._post = post
    [sale] = _sells('65826')
    _run(world, sale)
    try:
        assert entered.wait(1), 'owner read was not queued'
        assert not world.calls
        world.clock.t += world.mirror._window + 1
    finally:
        release.set()
    _drain(world)
    assert not world.calls


def test_unknown_native_snapshot_can_be_replaced_by_later_confirmed_proof(world):
    pid = _open(world, '65826')
    _watch(world, None)
    [sale] = _sells('65826')
    _run(world, sale)
    _drain(world)
    assert not world.calls
    world.mirror.account_watch._post = lambda body: (
        _account_answer(0) if json.loads(body)['method'] == 'getAccountInfo' else _answer([]))
    confirmed = ssm.SourceSell(path=ssm.PATH_WATCHER, signature=sale.signature, slot=sale.slot,
        source_user_id=sale.source_user_id, handle=sale.handle, owner=sale.owner, venue='watcher',
        mint=sale.mint, pool=None, sold_atomic=100, pre_atomic=100, seen_at=world.mirror._real_now(),
        recv_mono=world.clock.t)
    world.mirror._watcher_rows = lambda conn: [confirmed]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    _drain(world)
    assert world.calls == [(pid, ssm.REASON, True)]


def test_full_only_without_background_owner_reader_holds(world):
    _open(world, '65826')
    world.mirror.min_fraction = 1
    _run(world, *_sells('65826'))
    assert not world.calls


def test_confirmed_reader_preserves_one_atom_remainder(world):
    from sniper.persistence import db_memecoin as m
    _open(world, '65826')
    calls = _watch(world, _answer([]))
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    [sale] = _sells('65826')
    at = world.mirror._real_now()
    m.record_onchain_sell(world.conn, signature=sale.signature,
        source_user_id=sale.source_user_id, handle=sale.handle, address=sale.owner,
        mint=sale.mint, sold_atomic=100, pre_atomic=None, post_atomic=1, slot=sale.slot,
        block_time=None, detected_at=at, created_at=at)
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    _drain(world)
    assert not world.calls and not calls


def test_full_owner_balance_aggregates_classic_and_token2022_accounts():
    answer = _answer([0, 1])
    second = answer['result']['value'][1]['account']
    second['owner'] = ssm.TOKEN_2022_PROGRAM
    second['data']['program'] = 'spl-token-2022'
    watch = ssm.OwnerAccountWatch(ws_url='ws://offline', rpc_url='http://offline', sink=None,
                                 post=lambda body: answer)
    assert watch.full_owner_balance(FX['65826']['owner'], FX['65826']['mint'], 10) == (999999999, 1)


@pytest.mark.parametrize('mutation', ['stale', 'null', 'bad_list', 'bad_context', 'bool_context',
    'wrong_owner', 'wrong_mint', 'wrong_program', 'bad_amount', 'negative', 'float', 'bool',
    'huge', 'duplicate', 'unparsed', 'rpc_error', 'missing_value', 'wrong_parsed_program'])
def test_invalid_complete_owner_snapshot_is_unknown(mutation):
    answer = _answer([0], slot=20)
    result = answer['result']
    row = result['value'][0]
    info = row['account']['data']['parsed']['info']
    if mutation == 'stale': result['context']['slot'] = 9
    elif mutation == 'null': result['value'] = None
    elif mutation == 'bad_list': result['value'] = {}
    elif mutation == 'bad_context': result['context']['slot'] = '20'
    elif mutation == 'bool_context': result['context']['slot'] = True
    elif mutation == 'wrong_owner': info['owner'] = 'other'
    elif mutation == 'wrong_mint': info['mint'] = 'other'
    elif mutation == 'wrong_program': row['account']['owner'] = 'other'
    elif mutation == 'bad_amount': info['tokenAmount']['amount'] = None
    elif mutation == 'negative': info['tokenAmount']['amount'] = '-1'
    elif mutation == 'float': info['tokenAmount']['amount'] = 0.0
    elif mutation == 'bool': info['tokenAmount']['amount'] = False
    elif mutation == 'huge': info['tokenAmount']['amount'] = str(2**64)
    elif mutation == 'duplicate': result['value'].append(copy.deepcopy(row))
    elif mutation == 'unparsed': row['account']['data'] = ['AA==', 'base64']
    elif mutation == 'rpc_error': answer['error'] = {'code': 429}
    elif mutation == 'missing_value': del result['value']
    elif mutation == 'wrong_parsed_program': row['account']['data']['program'] = 'other'
    watch = ssm.OwnerAccountWatch(ws_url='ws://offline', rpc_url='http://offline', sink=None,
                                 post=lambda body: answer)
    assert watch.full_owner_balance(FX['65826']['owner'], FX['65826']['mint'], 10) is None
