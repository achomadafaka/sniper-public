"""Processed witnesses are diagnostics, never funded authority."""
import copy
import json
import math
import threading
from pathlib import Path

import pytest


def observer():
    from sniper.services import memecoin_execution_observer
    return memecoin_execution_observer


@pytest.fixture
def frame():
    frames = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_signal_frames.json').read_text())
    value = copy.deepcopy(frames['curve_buy'])
    value['method'] = 'logsNotification'
    value['params']['subscription'] = 42
    return value


def parse(frame, **kw):
    args = dict(subscription_id=42, generation=3, received_generation=3,
                received_monotonic=10.190, boot_id='B', clock_domain='C')
    args.update(kw)
    return observer().processed_observation(frame, **args)


def test_strict_processed_success_and_missing_error(frame):
    row, reason = parse(frame)
    assert reason is None and row['recv_monotonic'] == 10.190
    assert row['error_state'] == 'success'
    frame['params']['result']['value'].pop('err')
    assert parse(frame) == (None, 'missing_err')


@pytest.mark.parametrize('field,value', [('slot', True), ('slot', '1'), ('slot', 1.0), ('slot', 0),
    ('slot', -1), ('subscription', True), ('subscription', '42'), ('subscription', 42.0),
    ('subscription', 0), ('subscription', 43), ('signature', '1' * 63), ('signature', 'bad')])
def test_malformed_notification_never_success(frame, field, value):
    if field == 'slot':
        frame['params']['result']['context']['slot'] = value
    elif field == 'subscription':
        frame['params'][field] = value
    else:
        frame['params']['result']['value'][field] = value
    row, reason = parse(frame)
    assert row is None and reason


@pytest.mark.parametrize('value', [False, {}, {'InstructionError': [0, 'error']}])
def test_non_null_err_retained_as_failed(frame, value):
    frame['params']['result']['value']['err'] = value
    row, reason = parse(frame)
    assert reason is None and row['error_state'] == 'failed'


@pytest.mark.parametrize('kw', [dict(received_generation=2), dict(received_generation=True),
    dict(received_monotonic=True), dict(received_monotonic=-1), dict(received_monotonic=math.nan),
    dict(received_monotonic=math.inf), dict(received_monotonic='10'), dict(subscription_id=True)])
def test_bad_generation_clock_is_unknown(frame, kw):
    row, reason = parse(frame, **kw)
    assert row is None and reason


@pytest.mark.parametrize('value', [[], {'id': 3, 'result': 42}, {'method': 'signatureNotification',
    'params': {'subscription': 42, 'result': {'value': 'receivedSignature'}}}])
def test_non_logs_frames_cannot_witness(value):
    row, reason = parse(value)
    assert row is None and reason


def receipt(frame):
    from sniper.execution.solana_swap_executor import USDC_MINT
    sig = frame['params']['result']['value']['signature']
    def balance(ix, mint, amount, decimals=6):
        return dict(accountIndex=ix, owner='PAYER', mint=mint,
                    uiTokenAmount=dict(amount=amount, decimals=decimals))
    return dict(slot=100, transaction=dict(signatures=[sig], message=dict(accountKeys=[
        dict(pubkey='PAYER', signer=True), dict(pubkey='TOKEN', signer=False),
        dict(pubkey='USDC', signer=False)])), meta=dict(err=None, fee=5000,
        preBalances=[10000000, 2039280, 2039280], postBalances=[9995000, 2039280, 2039280],
        preTokenBalances=[balance(1, 'MINT', '0'), balance(2, USDC_MINT, '30000000')],
        postTokenBalances=[balance(1, 'MINT', '9007199254740993'), balance(2, USDC_MINT, '10000000')]))


def evidence(value, **kw):
    args = dict(custody_pubkey='PAYER', mint='MINT', payment_asset='usdc')
    args.update(kw)
    return observer().receipt_execution_evidence(value, **args)


def test_receipt_atoms_exact_and_exotic_leg_still_usdc(frame):
    tx = receipt(frame)
    tx['meta']['postTokenBalances'].append(dict(accountIndex=0, owner='PAYER', mint='EXOTIC',
        uiTokenAmount=dict(amount='5', decimals=9)))
    tx['meta']['preTokenBalances'].append(dict(accountIndex=0, owner='PAYER', mint='EXOTIC',
        uiTokenAmount=dict(amount='9', decimals=9)))
    result = evidence(tx)
    assert result['state'] == 'verified'

    assert result['payment_atomic'] == '20000000'
    assert result['acquired_atomic'] == '9007199254740993'
    assert result['receipt_slot'] == 100


@pytest.mark.parametrize('mutation', ['ownerless', 'missing_pre', 'duplicates', 'decimals',
    'missing_err', 'failed', 'payer', 'slot', 'zero_acquired', 'zero_payment', 'amount_float'])
def test_receipt_ambiguity_unknown(frame, mutation):
    tx = receipt(frame)
    meta = tx['meta']
    if mutation == 'ownerless': meta['postTokenBalances'][0].pop('owner')
    if mutation == 'missing_pre': meta['preTokenBalances'].pop(0)
    if mutation == 'duplicates': meta['postTokenBalances'].append(copy.deepcopy(meta['postTokenBalances'][0]))
    if mutation == 'decimals': meta['postTokenBalances'][0]['uiTokenAmount']['decimals'] = 9
    if mutation == 'missing_err': meta.pop('err')
    if mutation == 'failed': meta['err'] = False
    if mutation == 'payer': tx['transaction']['message']['accountKeys'][0]['signer'] = False
    if mutation == 'slot': tx['slot'] = True
    if mutation == 'zero_acquired': meta['postTokenBalances'][0]['uiTokenAmount']['amount'] = '0'
    if mutation == 'zero_payment': meta['postTokenBalances'][1]['uiTokenAmount']['amount'] = '30000000'
    if mutation == 'amount_float': meta['postTokenBalances'][0]['uiTokenAmount']['amount'] = 1.0
    assert evidence(tx)['state'] == 'unknown'


def test_missing_balance_zero_only_proven_created_account(frame):
    tx = receipt(frame)
    tx['meta']['preTokenBalances'].pop(0)
    assert evidence(tx)['state'] == 'unknown'
    tx['meta']['preBalances'][1] = 0
    assert evidence(tx)['state'] == 'verified'


def test_native_needs_validated_same_receipt_accounting(frame):
    tx = receipt(frame)
    assert evidence(tx, payment_asset='sol')['state'] == 'unknown'
    from sniper.execution.solana_swap_executor import receipt_sol_entry_fill
    tx['meta']['postBalances'][0] -= 1000000
    accounting = receipt_sol_entry_fill(tx, signature=tx['transaction']['signatures'][0],
        owner='PAYER', mint='MINT', maximum_lamports=2000000)
    result = evidence(tx, payment_asset='sol', native_accounting=accounting)
    assert result['state'] == 'verified' and result['payment_atomic'] == '1000000'
    accounting['sol_spent_lamports'] = 999999
    assert evidence(tx, payment_asset='sol', native_accounting=accounting)['state'] == 'unknown'


def bound_data():
    source = dict(network_id='solana', source_sig='SRC', source_wallet='TRADER', mint='MINT',
                  recv_monotonic=10.0, boot_id='B', clock_domain='C')
    rows = [dict(network_id='solana', our_sig='OUR', slot=100, error_state='success',
                 recv_monotonic=t, boot_id='B', clock_domain='C') for t in (10.190, 10.500)]
    attempt = dict(attempt_id='a', pending_id=1, terminal_status='filled', network_id='solana',
        tx_sig='OUR', mint='MINT', source_sig='SRC', source_wallet='TRADER', landing_slot=100)
    pending = dict(pending_id=1, attempt_id='a', state='confirmed', tx_sig='OUR',
                   custody_pubkey='PAYER', network_id='solana', mint='MINT')
    fill = dict(pending_id=1, attempt_id='a', network_id='solana', tx_sig='OUR', mint='MINT',
        side='buy', chain_confirmed=True, custody_pubkey='PAYER', payment_atomic='1',
        acquired_atomic='9007199254740993', receipt_slot=100)
    return source, rows, attempt, pending, fill


def bound(data):
    from sniper.services.memecoin_fill_timing import execution_bound
    return execution_bound(*data, custody_pubkey='PAYER')


def test_bound_first_receipt_confirmed_identity():
    data = bound_data()
    result = bound(data)
    assert result['observed_bound_ms'] == pytest.approx(190)
    assert result['sub200'] is True and result['canonical_execution_time'] == 'unverified'
    data[3]['state'] = 'submitted_unknown'
    assert bound(data)['sub200'] is None


@pytest.mark.parametrize('which,key,value', [(2, 'terminal_status', 'failed'), (3, 'tx_sig', 'our'),
    (4, 'chain_confirmed', False), (4, 'payment_atomic', '0'), (4, 'acquired_atomic', 'NaN'),
    (4, 'payment_atomic', 1.1), (4, 'acquired_atomic', '-1'), (4, 'custody_pubkey', 'OTHER'),
    (4, 'mint', 'OTHER'), (4, 'network_id', '8453'), (4, 'attempt_id', 'b'),
    (4, 'pending_id', 2), (4, 'receipt_slot', 101), (2, 'landing_slot', 101),
    (0, 'recv_monotonic', 11), (0, 'boot_id', 'A'), (0, 'clock_domain', 'A')])
def test_bound_unknown_on_unbound_or_nonpositive(which, key, value):
    data = bound_data()
    data[which][key] = value
    assert bound(data)['sub200'] is None


def test_bound_first_error_fork_never_replaced_by_success():
    data = bound_data()
    data[1][0]['error_state'] = 'failed'
    assert bound(data)['sub200'] is None
    data[1][0]['error_state'] = 'success'
    data[1][0]['slot'] = 99
    assert bound(data)['sub200'] is None


def test_bound_over_target_only_observed_upper_bound():
    data = bound_data()
    data[1][0]['recv_monotonic'] = 10.201
    result = bound(data)
    assert result['state'] == 'observed_over_target' and result['sub200'] is False
    assert result['canonical_execution_time'] == 'unverified'


def test_observer_stamps_before_parse_and_independent_stop(frame, monkeypatch):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    obs = observer()
    stop = threading.Event()
    journal = ReceiptJournal(producer_run_id='observer', boot_id='B', clock_domain='C')
    status, sent, events = {}, [], []
    class Clock:
        @staticmethod
        def monotonic():
            events.append('clock')
            return 10.190
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, raw): sent.append(json.loads(raw))
        def recv(self, **kw):
            events.append('recv')
            if len(sent) and not status.get('subscription'):
                return json.dumps(dict(jsonrpc='2.0', id=1, result=42))
            stop.set()
            return json.dumps(frame)
    original = obs.json.loads
    def loads(raw):
        events.append('parse')
        return original(raw)
    monkeypatch.setattr(obs.json, 'loads', loads)
    thread = obs.start_execution_observer(custody_pubkey='PAYER', ws_url='wss://research',
        journal=journal, status=status, stop=stop, connect=lambda *_args, **_kw: Socket(), clock=Clock)
    thread.join(2)
    assert not thread.is_alive() and thread.daemon
    assert sent[0]['params'] == [{'mentions': ['PAYER']}, {'commitment': 'processed'}]
    assert len(sent) == 1
    assert [dict(row)['kind'] for row in journal.snapshot()['rows']] == ['gap', 'processed']
    assert events[-3:] == ['recv', 'clock', 'parse']


def test_observer_connect_matches_installed_sync_client(frame):
    import inspect
    from websockets.sync.client import connect as installed_connect
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal

    stop = threading.Event()
    journal = ReceiptJournal(producer_run_id='O', boot_id='B', clock_domain='C')
    status = {}
    calls = []

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, raw): calls.append(json.loads(raw))
        def recv(self, **_kwargs):
            if not status.get('subscription'):
                return json.dumps(dict(jsonrpc='2.0', id=1, result=42))
            stop.set()
            return json.dumps(frame)

    def connect(*args, **kwargs):
        try:
            inspect.signature(installed_connect).bind(*args, **kwargs)
        except TypeError:
            stop.set()
            raise
        assert kwargs == dict(open_timeout=5, close_timeout=1, max_size=1024 * 1024)
        return Socket()

    thread = observer().start_execution_observer(custody_pubkey='PAYER',
        ws_url='wss://research', journal=journal, status=status, stop=stop, connect=connect)
    thread.join(2)
    assert not thread.is_alive()
    rows = journal.snapshot()['rows']
    assert rows[0]['reason'] == 'observer_ready'
    assert rows[1]['kind'] == 'processed' and rows[1]['connection_generation'] == 1
    assert len(calls) == 1


def test_native_cannot_relabel_payment_as_rent(frame):
    tx = receipt(frame)
    tx['meta']['postBalances'][0] -= 1000000
    fake = dict(sol_spent_lamports=999999, got=1.0, decimals=6, fee_lamports=5000,
                rent_lamports=1)
    assert evidence(tx, payment_asset='sol', native_accounting=fake)['state'] == 'unknown'


@pytest.mark.parametrize('ack', [dict(jsonrpc='2.0', id=True, result=42),
    dict(jsonrpc='2.0', id=2, result=42), dict(jsonrpc='2.0', id=1, result=True),
    dict(jsonrpc='2.0', id=1, result='42'), dict(jsonrpc='2.0', id=1, error={'code': -1}),
    dict(method='logsNotification', params={})])
def test_observer_ack_refusal_never_witnesses(ack):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    obs = observer()
    journal = ReceiptJournal(producer_run_id='O', boot_id='B', clock_domain='C')
    class Stop:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, _duration): self.stopped = True
    stop = Stop()
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, _raw): pass
        def recv(self, **_kw): return json.dumps(ack)
    thread = obs.start_execution_observer(custody_pubkey='PAYER', ws_url='wss://research',
        journal=journal, status={}, stop=stop, connect=lambda *_a, **_kw: Socket())
    thread.join(2)
    rows = journal.snapshot()['rows']
    assert len(rows) == 1 and rows[0]['reason'] == 'ack_refused' and rows[0]['kind'] == 'gap'


def test_bound_nonfinite_difference_unknown():
    data = bound_data()
    data[0]['recv_monotonic'] = 0.0
    for row in data[1]: row['recv_monotonic'] = 1e308
    assert bound(data)['observed_bound_ms'] is None


def test_bound_later_fork_error_invalidates_first_witness():
    data = bound_data()
    data[1][1]['error_state'] = 'failed'
    assert bound(data)['sub200'] is None


def test_bound_missing_fill_unknown():
    data = list(bound_data())
    data[-1] = None
    assert bound(data)['sub200'] is None


def test_real_pumpswap_exotic_usdc_receipt_exact_atoms():
    fixtures = json.loads((Path(__file__).parent / 'fixtures/memecoin/pumpswap_v2_receipts_20261008.json').read_text())
    row = fixtures['tardigrade']
    tx = row['tx']
    result = observer().receipt_execution_evidence(tx, custody_pubkey=row['owner'],
        mint='DXvY5Ep1j9Eqwf6qHkemgWgBsEfhEKDsSizDjT4ipump', payment_asset='usdc')
    assert result['state'] == 'verified'
    assert result['payment_atomic'] == '750000000' and result['acquired_atomic'] == '8882606584596'
    assert result['receipt_slot'] == tx['slot']


def geyser_receipt(*, failed=False, loaded=False):
    """Structural wire fixture, not a claimed captured transaction or fill."""
    import base58
    import struct
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    owner = base58.b58encode(bytes([31]) * 32).decode()
    update = pb.SubscribeUpdate(filters=['custody'])
    update.transaction.slot = 100
    info = update.transaction.transaction
    info.signature = bytes([9]) * 64
    info.transaction.signatures.append(info.signature)
    message = info.transaction.message
    message.header.num_required_signatures = 1
    message.account_keys.append(bytes([31]) * 32)
    info.meta.fee = 5000
    info.meta.pre_balances.append(100000)
    info.meta.post_balances.append(95000)
    info.meta.log_messages.append('Program fixture success')
    if loaded:
        message.versioned = True
        lookup = message.address_table_lookups.add(account_key=bytes([32]) * 32)
        lookup.writable_indexes = b'\x00'
        info.meta.loaded_writable_addresses.append(bytes([33]) * 32)
        info.meta.pre_balances.append(0)
        info.meta.post_balances.append(0)
    if failed:
        info.meta.err.err = struct.pack('<IBII', 8, 3, 25, 6004)
    return update, owner


def yellowstone_parse(update, owner, **kw):
    args = dict(custody_pubkey=owner, generation=2, received_generation=2,
        received_monotonic=10.190, boot_id='B', clock_domain='C',
        head_slot=100, head_monotonic=10.0)
    args.update(kw)
    return observer().yellowstone_observation(update, **args)


def test_yellowstone_custody_subscription_keeps_failed_transactions():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    _, owner = geyser_receipt()
    request = observer().yellowstone_subscription(owner)
    assert request.commitment == pb.PROCESSED
    assert set(request.transactions) == {'custody'}
    tx = request.transactions['custody']
    assert list(tx.account_include) == [owner]
    assert tx.HasField('vote') and tx.vote is False
    assert not tx.HasField('failed')
    assert not tx.account_exclude and not tx.account_required
    assert not request.HasField('from_slot')


@pytest.mark.parametrize('failed', [False, True])
def test_yellowstone_exact_identity_and_typed_failure(failed):
    update, owner = geyser_receipt(failed=failed, loaded=True)
    row, reason = yellowstone_parse(update, owner)
    assert reason is None
    assert row['slot'] == 100 and row['recv_monotonic'] == 10.190
    assert row['error_state'] == ('failed' if failed else 'success')
    assert json.loads(row['error_json']) == ({'InstructionError': [3, {'Custom': 6004}]} if failed else None)
    assert row['fee_lamports'] == '5000'
    assert row['connection_generation'] == 2


@pytest.mark.parametrize('damage', ['metadata', 'signature', 'header', 'loaded', 'balance',
    'error', 'payer', 'vote', 'token_amount', 'token_identity', 'instruction_index'])
def test_yellowstone_incomplete_or_unrelated_receipt_is_unknown(damage):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    update, owner = geyser_receipt(loaded=True)
    info = update.transaction.transaction
    if damage == 'metadata': info.ClearField('meta')
    if damage == 'signature': info.signature = bytes([8]) * 64
    if damage == 'header': info.transaction.message.header.num_required_signatures = 2
    if damage == 'loaded': info.meta.ClearField('loaded_writable_addresses')
    if damage == 'balance': info.meta.ClearField('post_balances')
    if damage == 'error': info.meta.err.err = b'unknown encoding'
    if damage == 'payer': owner = str(__import__('solders.pubkey', fromlist=['Pubkey']).Pubkey.new_unique())
    if damage == 'vote': info.is_vote = True
    if damage.startswith('token_'):
        token = info.meta.pre_token_balances.add(account_index=1, owner=owner, mint=owner,
            program_id='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
        token.ui_token_amount.amount = '1.5' if damage == 'token_amount' else '1'
        token.ui_token_amount.decimals = 6
        other = info.meta.post_token_balances.add()
        other.CopyFrom(token)
        if damage == 'token_identity': other.owner = str(__import__('solders.pubkey', fromlist=['Pubkey']).Pubkey.new_unique())
    if damage == 'instruction_index': info.transaction.message.instructions.add(program_id_index=255)
    row, reason = yellowstone_parse(update, owner)
    assert row is None and reason


@pytest.mark.parametrize('kwargs', [dict(received_generation=1), dict(received_generation=True),
    dict(received_monotonic=float('nan')), dict(head_slot=110), dict(head_monotonic=1.0),
    dict(boot_id=''), dict(clock_domain='')])
def test_yellowstone_clock_generation_and_replay_unknown(kwargs):
    update, owner = geyser_receipt()
    row, reason = yellowstone_parse(update, owner, **kwargs)
    assert row is None and reason


def test_yellowstone_second_signer_is_not_custody_payer():
    update, owner = geyser_receipt()
    info = update.transaction.transaction
    info.transaction.message.account_keys.insert(0, bytes([32]) * 32)
    info.transaction.message.header.num_required_signatures = 2
    info.transaction.signatures.append(bytes([10]) * 64)
    info.meta.pre_balances.append(0)
    info.meta.post_balances.append(0)
    row, reason = yellowstone_parse(update, owner)
    assert row is None and reason == 'custody_not_payer'


def test_yellowstone_failure_cannot_be_successful_source_or_acquisition():
    from sniper.services.memecoin_shyft_confirmed import confirmed_transaction, transaction_metadata
    update, owner = geyser_receipt(failed=True)
    clocks = dict(head_slot=100, recv_monotonic=10.190, head_monotonic=10.0)
    assert confirmed_transaction(update, {owner: {}}, **clocks) is None
    assert transaction_metadata(update, {owner: {}}, **clocks) is None
    receipt = transaction_metadata(update, {owner: {}}, allow_failed=True, **clocks)
    assert receipt['meta']['err'] == {'InstructionError': [3, {'Custom': 6004}]}
    assert observer().receipt_execution_evidence(receipt, custody_pubkey=owner,
        mint=owner, payment_asset='sol')['reason'] == 'receipt_not_successful'


def run_yellowstone(monkeypatch, batches, *, journal_size=4096, clocks=None, pong_decode_delay=0,
                   established_pong_delay=0):
    import asyncio
    import grpc
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    obs = observer()
    _, owner = geyser_receipt()
    stop = threading.Event()
    journal = ReceiptJournal(producer_run_id='G', boot_id='B', clock_domain='C', max_samples=journal_size)
    status, calls, events = {}, [], []
    pending = list(batches)
    class Clock:
        mono = 10.0
        @classmethod
        def monotonic(cls):
            events.append('clock')
            cls.mono = next(clocks) if clocks is not None else cls.mono + .001
            return cls.mono
    original_decode = pb.SubscribeUpdate.FromString
    def decode(raw):
        update = original_decode(raw)
        if update.HasField('pong'):
            Clock.mono += pong_decode_delay
        return update
    monkeypatch.setattr(pb.SubscribeUpdate, 'FromString', staticmethod(decode))
    class Stream:
        def __init__(self, requests): self.requests = requests; self.frames = iter(pending.pop(0))
        def __aiter__(self): return self
        async def __anext__(self):
            if not calls or len(calls) < len(batches) - len(pending):
                calls.append([await self.requests.__anext__(), await self.requests.__anext__()])
            frame = next(self.frames)
            if isinstance(frame, BaseException): raise frame
            if frame == 'stop':
                stop.set()
                raise StopAsyncIteration
            if (isinstance(frame, pb.SubscribeUpdate) and frame.HasField('pong')
                    and frame.pong.id == 2 and status.get('connected')):
                calls[-1].append(await self.requests.__anext__())
                Clock.mono += established_pong_delay
            events.append('recv')
            return frame.SerializeToString() if isinstance(frame, pb.SubscribeUpdate) else frame
        def cancel(self): events.append('cancel')
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def stream_stream(self, path, **kwargs):
            assert path == '/geyser.Geyser/Subscribe'
            assert kwargs['response_deserializer'](b'bytes') == b'bytes'
            def call(requests, metadata):
                assert metadata == (('x-token', 'test-token'),)
                return Stream(requests)
            return call
    def channel(target, credentials, **kwargs):
        assert target == 'grpc.fra.shyft.to:443'
        assert ('grpc.max_receive_message_length', 4 * 1024 * 1024) in kwargs['options']
        return Channel()
    async def no_wait(_seconds): pass
    monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
    monkeypatch.setattr(obs, '_observer_backoff', no_wait)
    monkeypatch.setattr(obs.asyncio, 'sleep', no_wait)
    asyncio.run(obs._run_yellowstone_execution_observer(custody_pubkey=owner,
        url='https://grpc.fra.shyft.to', token='test-token', journal=journal,
        status=status, stop=stop, clock=Clock))
    return journal.snapshot(), status, calls, events


def geyser_ready(slot=100, *, pong=1):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    return [pb.SubscribeUpdate(pong=pb.SubscribeUpdatePong(id=pong)),
        pb.SubscribeUpdate(filters=['head'], slot=pb.SubscribeUpdateSlot(slot=slot,
            status=pb.SLOT_PROCESSED))]


def test_yellowstone_stream_early_observation_replay_conflict_and_failure(monkeypatch):
    success, _ = geyser_receipt()
    failure, _ = geyser_receipt(failed=True)
    snapshot, status, calls, events = run_yellowstone(monkeypatch,
        [geyser_ready() + [success, success, failure, 'stop']])
    rows = [dict(row) for row in snapshot['rows']]
    witnessed = [row for row in rows if row['kind'] == 'processed']
    assert [row['error_state'] for row in witnessed] == ['success', 'failed']
    assert witnessed[0]['recv_monotonic'] < witnessed[1]['recv_monotonic']
    assert status['provider'] == 'shyft' and status['hostname'] == 'grpc.fra.shyft.to'
    assert status['duplicates'] == 1 and status['observations'] == 2
    assert status['requested_count'] == 1 and len(calls) == 1
    assert status['head_slot'] == 100 and status['generation'] == 1
    assert events[events.index('recv') + 1] == 'clock'
    assert all('attempt_id' not in row for row in witnessed)


def test_yellowstone_stream_reconnect_generation_and_replay_gap(monkeypatch):
    update, _ = geyser_receipt()
    snapshot, status, calls, _ = run_yellowstone(monkeypatch,
        [geyser_ready() + [update, TimeoutError()], geyser_ready(300) + [update, 'stop']])
    rows = snapshot['rows']
    assert len([row for row in rows if row['kind'] == 'processed']) == 1
    assert [row['connection_generation'] for row in rows if row.get('reason') == 'observer_ready'] == [1, 2]
    assert any(row.get('reason') == 'stream_silent' for row in rows)
    assert any(row.get('reason') == 'replay_depth_exceeded' for row in rows)
    assert any(row.get('reason') == 'stale_or_dead_slot' for row in rows)
    assert len(calls) == 2 and status['generation'] == 2


@pytest.mark.parametrize('damage,reason', [('no_ack', 'unacknowledged_generation'),
    ('wrong_filter', 'wrong_filter'), ('missing_meta', 'invalid_transaction_metadata'),
    ('dead', 'stale_or_dead_slot'), ('invalid_bytes', 'invalid_frame')])
def test_yellowstone_stream_unknown_gaps(monkeypatch, damage, reason):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    update, _ = geyser_receipt()
    frames = geyser_ready()
    if damage == 'no_ack': frames = geyser_ready(pong=2)
    if damage == 'wrong_filter': update.filters[:] = ['owners']
    if damage == 'missing_meta': update.transaction.transaction.ClearField('meta')
    if damage == 'dead': frames.append(pb.SubscribeUpdate(filters=['head'],
        slot=pb.SubscribeUpdateSlot(slot=100, status=pb.SLOT_DEAD)))
    if damage == 'invalid_bytes': update = b'not protobuf'
    snapshot, status, _, _ = run_yellowstone(monkeypatch, [frames + [update, 'stop']])
    assert not any(row['kind'] == 'processed' for row in snapshot['rows'])
    assert any(row.get('reason') == reason and row['lost_count'] is None for row in snapshot['rows'])
    assert status['coverage_gaps'] >= 1


def test_yellowstone_journal_eviction_is_coverage_gap(monkeypatch):
    first, _ = geyser_receipt()
    second = copy.deepcopy(first)
    second.transaction.transaction.signature = bytes([8]) * 64
    second.transaction.transaction.transaction.signatures[0] = bytes([8]) * 64
    snapshot, status, _, _ = run_yellowstone(monkeypatch,
        [geyser_ready() + [first, second, 'stop']], journal_size=2)
    assert snapshot['evicted_cap'] > 0
    assert any(row.get('reason') == 'journal_overflow' for row in snapshot['rows'])
    assert status['journal_evicted'] > 0


@pytest.mark.parametrize('url,token', [('https://grpc.fra.shyft.to', ''),
    ('https://grpc.ams.shyft.to?token=secret', 'secret'), ('https://grpc.nyc.shyft.to', 'secret'),
    ('http://grpc.fra.shyft.to', 'secret'), ('https://user@grpc.fra.shyft.to', 'secret'),
    ('https://grpc.fra.shyft.to:invalid', 'secret'), (None, 'secret')])
def test_yellowstone_starter_refuses_unauthenticated_or_non_europe(url, token):
    _, owner = geyser_receipt()
    with pytest.raises(ValueError, match='observer_endpoint_invalid'):
        observer().start_yellowstone_execution_observer(custody_pubkey=owner, url=url,
            token=token, journal=None, status={}, stop=threading.Event())


def test_yellowstone_starter_is_background_and_publickey_only(monkeypatch):
    _, owner = geyser_receipt()
    seen = []
    async def run(**kwargs): seen.append(kwargs)
    monkeypatch.setattr(observer(), '_run_yellowstone_execution_observer', run)
    status, stop, journal = {}, threading.Event(), object()
    thread = observer().start_yellowstone_execution_observer(custody_pubkey=owner,
        url='https://grpc.fra.shyft.to', token='test-token', journal=journal, status=status, stop=stop)
    thread.join(2)
    assert thread.daemon and not thread.is_alive()
    assert set(seen[0]) == {'custody_pubkey', 'url', 'token', 'journal', 'status', 'stop'}


def test_yellowstone_reconnect_does_not_reuse_old_head_or_ack(monkeypatch):
    update, _ = geyser_receipt()
    snapshot, status, _, _ = run_yellowstone(monkeypatch,
        [geyser_ready() + [TimeoutError()], [update] + geyser_ready()[:1] + [update, 'stop']])
    assert not any(row['kind'] == 'processed' for row in snapshot['rows'])
    assert any(row.get('reason') == 'unacknowledged_generation' and row['connection_generation'] == 2
        for row in snapshot['rows'])
    assert any(row.get('reason') == 'stale_or_dead_slot' and row['connection_generation'] == 2
        for row in snapshot['rows'])
    assert status['head_slot'] is None


def test_yellowstone_dead_notice_persists_gap_after_observation(monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    update, _ = geyser_receipt()
    dead = pb.SubscribeUpdate(filters=['head'], slot=pb.SubscribeUpdateSlot(slot=100, status=pb.SLOT_DEAD))
    snapshot, status, _, _ = run_yellowstone(monkeypatch, [geyser_ready() + [update, dead, 'stop']])
    assert any(row['kind'] == 'processed' for row in snapshot['rows'])
    assert any(row.get('reason') == 'dead_slot' and row['lost_count'] is None for row in snapshot['rows'])
    assert status['head_slot'] is None


def test_yellowstone_failure_without_logs_keeps_typed_error():
    update, owner = geyser_receipt(failed=True)
    update.transaction.transaction.meta.ClearField('log_messages')
    update.transaction.transaction.meta.log_messages_none = True
    row, reason = yellowstone_parse(update, owner)
    assert reason is None and row['error_state'] == 'failed'
    update.transaction.transaction.meta.ClearField('err')
    assert yellowstone_parse(update, owner)[0] is None


def test_yellowstone_failed_unexecuted_transfer_is_not_inventory_proof():
    import base58
    import struct
    update, owner = geyser_receipt(failed=True)
    info = update.transaction.transaction
    info.transaction.message.account_keys.extend([bytes([32]) * 32, bytes([33]) * 32,
        base58.b58decode('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')])
    info.meta.pre_balances.extend([0, 0, 0])
    info.meta.post_balances.extend([0, 0, 0])
    info.transaction.message.instructions.add(program_id_index=3, accounts=bytes([1, 2, 0]),
        data=b'\x03' + struct.pack('<Q', 10))
    row, reason = yellowstone_parse(update, owner)
    assert reason is None and row['error_state'] == 'failed'
    info.meta.ClearField('err')
    assert yellowstone_parse(update, owner)[0] is None


def test_yellowstone_unknown_error_variant_and_trailing_bytes_are_unknown():
    import struct
    update, owner = geyser_receipt(failed=True)
    for raw in (struct.pack('<I', 999), b'\x08',
            bytes(update.transaction.transaction.meta.err.err) + b'\x00', b'\x00' * 1025):
        update.transaction.transaction.meta.err.err = raw
        assert yellowstone_parse(update, owner)[0] is None


def test_yellowstone_matching_pong_after_deadline_never_acknowledges(monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    clocks = iter([10.0, 10.1, 10.2, 10.3, 20.1, 20.2, 20.3, 20.4])
    snapshot, status, _, _ = run_yellowstone(monkeypatch,
        [[pb.SubscribeUpdate(ping=pb.SubscribeUpdatePing()), geyser_ready()[0]], ['stop']], clocks=clocks)
    assert not any(row.get('reason') == 'observer_ready' for row in snapshot['rows'])
    assert any(row.get('reason') == 'subscription_ack_timeout' for row in snapshot['rows'])
    assert status['acknowledged_count'] == 0


def test_yellowstone_pong_decode_crossing_deadline_never_acknowledges(monkeypatch):
    snapshot, status, _, _ = run_yellowstone(monkeypatch,
        [[geyser_ready()[0]], ['stop']], pong_decode_delay=10.1)
    assert not any(row.get('reason') == 'observer_ready' for row in snapshot['rows'])
    assert any(row.get('reason') == 'subscription_ack_timeout' for row in snapshot['rows'])
    assert status['acknowledged_count'] == 0


def test_yellowstone_established_pong_after_startup_deadline_preserves_receive_clock(monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    pong = pb.SubscribeUpdate(pong=pb.SubscribeUpdatePong(id=2))
    snapshot, status, calls, _ = run_yellowstone(monkeypatch,
        [geyser_ready() + [pong, 'stop'], ['stop']], pong_decode_delay=.5, established_pong_delay=10.1)
    ready = [row for row in snapshot['rows'] if row.get('reason') == 'observer_ready']
    assert len(ready) == 1
    assert ready[0]['mono_start'] == pytest.approx(10.003)
    assert calls[0][-1].ping.id == 2
    assert status['generation'] == 1 and status['reconnects'] == 0
    assert status['last_frame_monotonic'] > 20
    assert status['coverage_gaps'] == 0


def test_yellowstone_observer_has_no_http_ledger_or_executor_calls(monkeypatch):
    import sqlite3
    import requests
    from sniper.execution import solana_swap_executor, solana_real_seams
    def forbidden(*_args, **_kwargs):
        raise AssertionError('observer reached an execution, HTTP or ledger boundary')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(solana_swap_executor, 'execute_entry', forbidden)
    monkeypatch.setattr(solana_real_seams, 'sign_swap_tx', forbidden)
    monkeypatch.setattr(solana_swap_executor, 'receipt_entry_fill', forbidden)
    monkeypatch.setattr(solana_swap_executor, 'receipt_sol_entry_fill', forbidden)
    update, _ = geyser_receipt(failed=True)
    snapshot, _, _, _ = run_yellowstone(monkeypatch, [geyser_ready() + [update, 'stop']])
    assert [row['error_state'] for row in snapshot['rows'] if row['kind'] == 'processed'] == ['failed']


def test_observer_reconnect_generation_and_timeout_gap(frame):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    journal = ReceiptJournal(producer_run_id='O', boot_id='B', clock_domain='C')
    stop = threading.Event()
    requests = []
    class Socket:
        def __init__(self, generation): self.generation, self.acked = generation, False
        def __enter__(self): return self
        def __exit__(self, *_a): pass
        def send(self, raw): requests.append(json.loads(raw))
        def recv(self, **_kw):
            if self.generation == 1: raise TimeoutError()
            if not self.acked:
                self.acked = True
                return json.dumps(dict(jsonrpc='2.0', id=2, result=42))
            stop.set()
            return json.dumps(frame)
    class ImmediateStop:
        def is_set(self): return stop.is_set()
        def wait(self, _delay): return stop.is_set()
    thread = observer().start_execution_observer(custody_pubkey='PAYER', ws_url='wss://research',
        journal=journal, status={}, stop=ImmediateStop(),
        connect=lambda *_a, **_kw: Socket(len(requests) + 1))
    thread.join(2)
    assert [request['id'] for request in requests] == [1, 2]
    rows = journal.snapshot()['rows']
    assert rows[0]['reason'] == 'timeout'
    assert rows[-1]['kind'] == 'processed' and rows[-1]['connection_generation'] == 2


def test_observer_journal_overflow_is_explicit_and_survives_no_skip_rows(frame, tmp_path):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    from sniper.services import memecoin_fill_timing as ft
    journal = ReceiptJournal(producer_run_id='O', boot_id='B', clock_domain='C', max_samples=2)
    row, _ = parse(frame)
    for _ in range(3): journal.record_processed(**row)
    conn = ft.connect(tmp_path / 'timing.db')
    try:
        flush_journal(conn, journal.snapshot(), 0)
        gap = conn.execute('SELECT reason,lost_count FROM memecoin_execution_coverage').fetchone()
        assert gap == ('producer_overwrite', 1)
        assert conn.execute('SELECT count(*) FROM memecoin_processed_receipts').fetchone()[0] == 2
    finally:
        conn.close()


def test_observer_missing_socket_dependency_records_offpath_gap(monkeypatch):
    import builtins
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    journal = ReceiptJournal(producer_run_id='O', boot_id='B', clock_domain='C')
    status = {}
    original = builtins.__import__
    def guarded(name, *args, **kw):
        if name == 'websockets.sync.client': raise ImportError('missing socket dependency')
        return original(name, *args, **kw)
    monkeypatch.setattr(builtins, '__import__', guarded)
    thread = observer().start_execution_observer(custody_pubkey='PAYER', ws_url='wss://research',
        journal=journal, status=status, stop=threading.Event())
    thread.join(2)
    assert status['reason'] == 'observer_dependency_unavailable'
    assert journal.snapshot()['rows'][0]['reason'] == 'observer_dependency_unavailable'


@pytest.mark.parametrize('slot', [101, True, '100', 100.0, 0, -1, None])
def test_bound_later_success_slot_conflict_is_unknown(slot):
    data = bound_data()
    data[1][1]['slot'] = slot
    result = bound(data)
    assert result['state'] == 'unknown' and result['sub200'] is None
    assert result['reason'] == 'receipt_slot_mismatch'


def test_bound_exact_200ms_is_within_target():
    data = bound_data()
    data[0]['recv_monotonic'] = 0.0
    data[1][0]['recv_monotonic'] = 0.200
    data[1][1]['recv_monotonic'] = 0.500
    result = bound(data)
    assert result['observed_bound_ms'] == 200.0
    assert result['state'] == 'witnessed' and result['sub200'] is True
    data[1][0]['recv_monotonic'] = 0.200000001
    assert bound(data)['sub200'] is False
