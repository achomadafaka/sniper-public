"""Base receipt/subscription regressions promoted from the 2026-10-10 EVM audit.

All RPC/socket dependencies are fakes; only in-memory SQLite is mutated.
Run: python3 -m pytest -q -n 2 -o addopts='' <this-file>
"""
import json

import pytest
from sniper.services import memecoin_base_preconf as P
from test_memecoin_base_preconf import TX, _db, _row


def test_missing_receipt_status_is_unknown_not_retracted():
    conn = _db()
    receipt = {'transactionHash': TX, 'blockHash': '0x' + 'cd' * 32, 'blockNumber': '0x64'}
    answer = P.resolve(conn, TX, read_fn=lambda *_: receipt, now=1001)
    assert (answer, _row(conn)[0]) == (None, 'preconfirmed')


def test_wrong_receipt_transaction_cannot_confirm_source():
    conn = _db()
    receipt = {'transactionHash': '0x' + 'ef' * 32, 'status': '0x1',
               'blockHash': '0x' + 'cd' * 32, 'blockNumber': '0x64'}
    answer = P.resolve(conn, TX, read_fn=lambda *_: receipt, now=1001)
    assert (answer, _row(conn)[0]) == (None, 'preconfirmed')


def test_two_null_reads_at_same_head_cannot_retract_source():
    tracker, events = P.Tracker(), []
    tracker.add(TX, block=100, first_at=1000)
    for head in [101, 101]:
        P.settle(tracker, head, resolve=lambda _: 'null', retract=lambda tx: events.append(tx),
                 unresolved=lambda _: None, page=lambda *_: None, confirmed=lambda _: None)
    assert not events and len(tracker) == 1


def test_moved_canonical_receipt_updates_initiation_block_number():
    conn = _db()
    P.confirm(conn, TX, block=101, block_hash='0x' + 'cd' * 32, sealed_at=1003)
    assert _row(conn)[2]['source_initiation']['block_number'] == 101


def test_notification_between_subscribe_replies_is_not_refusal():
    stopped, heads, messages = [False], [], []
    frames = [
        {'id': 1, 'result': P.IDENTITY},
        {'id': 2, 'result': 'heads'},
        {'method': 'eth_subscription', 'params': {'subscription': 'heads', 'result': {'number': '0x65'}}},
        {'id': 3, 'result': 'logs'},
    ]
    class Socket:
        def send(self, _): pass
        def close(self): pass
        def recv(self, **_):
            if frames:
                return json.dumps(frames.pop(0))
            stopped[0] = True
            raise TimeoutError()
    def sleep(_): stopped[0] = True
    P.run_preconf_stream(on_log=lambda _: None, on_head=heads.append,
        addresses_fn=lambda: ['0x' + 'ab' * 20], connect_fn=lambda _: Socket(),
        should_stop=lambda: stopped[0], now_fn=lambda: 1000, sleep_fn=sleep, say=messages.append)
    assert heads == [101], messages
    assert not any('refused' in message for message in messages)


@pytest.mark.parametrize('change', [
    {'status': None}, {'status': 'garbage'}, {'status': '0x2'}, {'status': -1},
    {'blockNumber': None}, {'blockNumber': -1}, {'blockHash': P.ZERO_HASH},
    {'transactionHash': None}, {'transactionHash': '0x' + 'ef' * 32}, {'chainId': '0x1'},
])
def test_malformed_or_misbound_canonical_receipt_stays_unknown(change):
    conn = _db()
    receipt = dict(transactionHash=TX, status='0x1', blockNumber='0x64', blockHash='0x' + 'cd' * 32)
    receipt.update(change)
    assert P.resolve(conn, TX, read_fn=lambda *_: receipt, now=1001) is None
    assert _row(conn)[0] == 'preconfirmed'


@pytest.mark.parametrize('status, state', [('0x0', 'retracted'), ('0x1', 'canonical')])
def test_valid_receipt_status_retains_exact_source_identity(status, state):
    conn = _db()
    receipt = dict(transactionHash=TX.upper().replace('0X', '0x'), status=status,
                   blockNumber='0x64', blockHash='0x' + 'CD' * 32, chainId='0x2105')
    assert P.resolve(conn, TX, read_fn=lambda *_: receipt, now=1001) == state
    assert _row(conn)[0] == state


def test_out_of_order_heads_do_not_repeat_canonical_receipt_reads():
    tracker, reads, events = P.Tracker(), [], []
    tracker.add(TX, block=100, first_at=1000)
    for head in [103, 102, 103, 104]:
        P.settle(tracker, head, resolve=lambda tx, h=head: reads.append(h) or 'null',
                 retract=events.append, unresolved=lambda _: None,
                 page=lambda *_: None, confirmed=lambda _: None)
    assert reads == [103, 104]
    assert events == [TX] and not len(tracker)


def test_confirmation_preserves_original_preconf_block_and_rejects_negative_block():
    conn = _db()
    assert P.confirm(conn, TX, block=-1, block_hash='0x' + 'cd' * 32, sealed_at=1003) == 0
    assert _row(conn)[0] == 'preconfirmed'
    assert P.confirm(conn, TX, block=101, block_hash='0x' + 'cd' * 32, sealed_at=1003) == 1
    _, slot, valuation = _row(conn)
    assert slot == valuation['source_initiation']['block_number'] == 101
    assert valuation['preconf']['block_number'] == 100


def test_moved_canonical_row_passes_exact_source_initiation_guard():
    from types import SimpleNamespace
    from sniper.services.memecoin_source_initiation import DIRECT_PROOF_VERSION, source_initiation_refusal
    conn = _db()
    wallet, mint = '0x' + '11' * 20, '0x' + 'bb' * 20
    valuation = _row(conn)[2]
    valuation['source_initiation'].update(schema_version=1, proof_version=DIRECT_PROOF_VERSION,
        network_id='8453', source_user_id='uid', tx_hash=TX, mint=mint, beneficiary=wallet, payer=wallet)
    conn.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?', (json.dumps(valuation),))
    P.confirm(conn, TX, block=101, block_hash='0x' + 'cd' * 32, sealed_at=1003)
    _, slot, valuation = _row(conn)
    event = SimpleNamespace(signal_source='onchain_watch', fomo_event_id=f'onchain:8453:{TX}:{mint}',
        source_user_id='uid', mint=mint, source_wallet=wallet, source_block_number=slot,
        source_initiation=valuation['source_initiation'])
    assert source_initiation_refusal(event, '8453') is None


def test_subscribe_reply_demux_buffers_pending_logs_and_ignores_unrelated_ids():
    stopped, heads, logs, messages = [False], [], [], []
    frames = [
        {'id': 999, 'result': '0x1'},
        {'id': 1, 'result': P.IDENTITY},
        {'id': 2, 'result': 'heads'},
        {'method': 'eth_subscription', 'params': {'subscription': 'logs', 'result': {'transactionHash': TX}}},
        {'method': 'eth_subscription', 'params': {'subscription': 'heads', 'result': {'number': '0x65'}}},
        {'id': 3, 'result': 'logs'},
    ]
    class Socket:
        def send(self, _): pass
        def close(self): pass
        def recv(self, **_):
            if frames:
                return json.dumps(frames.pop(0))
            stopped[0] = True
            raise TimeoutError()
    P.run_preconf_stream(on_log=logs.append, on_head=heads.append,
        addresses_fn=lambda: ['0x' + 'ab' * 20], connect_fn=lambda _: Socket(),
        should_stop=lambda: stopped[0], now_fn=lambda: 1000,
        sleep_fn=lambda _: stopped.__setitem__(0, True), say=messages.append)
    assert heads == [101] and logs == [{'transactionHash': TX}]
    assert not any('wrong chain' in message or 'refused' in message for message in messages)


def test_notification_flood_cannot_extend_subscribe_response_deadline():
    stopped, messages, now = [False], [], [1000]
    frames = [{'id': 1, 'result': P.IDENTITY}, {'id': 2, 'result': 'heads'}]
    class Socket:
        def send(self, _): pass
        def close(self): pass
        def recv(self, **_):
            now[0] += 1
            return json.dumps(frames.pop(0) if frames else {
                'method': 'eth_subscription', 'params': {'subscription': 'heads', 'result': {'number': '0x65'}}})
    P.run_preconf_stream(on_log=lambda _: None, on_head=lambda _: None,
        addresses_fn=lambda: ['0x' + 'ab' * 20], connect_fn=lambda _: Socket(),
        should_stop=lambda: stopped[0], now_fn=lambda: now[0],
        sleep_fn=lambda _: stopped.__setitem__(0, True), say=messages.append)
    assert now[0] <= 1012
    assert any('response deadline' in message for message in messages)
