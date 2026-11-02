"""Enhanced receipts reuse confirmed proof without touching the Shyft send path."""
import asyncio
import base64
import json
import threading

import pytest
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import MessageHeader, MessageV0, MessageAddressTableLookup
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from test_memecoin_shyft_confirmed import FIX, OWNER, WATCH, wire_receipt, decode


def notification():
    wire = wire_receipt().transaction.transaction
    m = wire.transaction.message
    msg = MessageV0(MessageHeader(m.header.num_required_signatures,
        m.header.num_readonly_signed_accounts, m.header.num_readonly_unsigned_accounts),
        [Pubkey(bytes(k)) for k in m.account_keys], Hash.default(),
        [CompiledInstruction(x.program_id_index, bytes(x.data), bytes(x.accounts)) for x in m.instructions],
        [MessageAddressTableLookup(Pubkey(bytes(x.account_key)), bytes(x.writable_indexes),
                                  bytes(x.readonly_indexes)) for x in m.address_table_lookups])
    tx = VersionedTransaction.populate(msg, [Signature(bytes(s)) for s in wire.transaction.signatures])
    meta = decode(wire_receipt())['meta']
    meta['innerInstructions'] = [{'index': g.index, 'instructions': [dict(
        programIdIndex=x.program_id_index, accounts=list(x.accounts),
        data=__import__('base58').b58encode(x.data).decode(),
        **({'stackHeight': x.stack_height} if x.HasField('stack_height') else {}))
        for x in g.instructions]}
        for g in wire.meta.inner_instructions]
    return dict(method='transactionNotification', params=dict(subscription=7, result=dict(
        slot=FIX['slot'], signature=FIX['transaction']['signatures'][0],
        transaction=dict(transaction=[base64.b64encode(bytes(tx)).decode(), 'base64'], meta=meta))))


def parse(frame):
    from sniper.services.memecoin_helius_evidence import confirmed_transaction
    return confirmed_transaction(frame, WATCH, head_slot=FIX['slot'],
                                 recv_monotonic=1.0, head_monotonic=1.0)


def test_full_enhanced_receipt_retains_exact_existing_proof():
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx = parse(notification())
    assert tx == decode(wire_receipt())
    proof = solana_receipt_initiation(tx, OWNER, value_source_buy(tx, OWNER, sol_usd=None, now=105))
    assert proof['payer'] == OWNER and proof['route_payment_atomic'] == '500000000'


@pytest.mark.parametrize('bad', ['signature', 'failed', 'missing_err', 'missing_balances',
                                'missing_inner', 'missing_tokens', 'wrong_encoding', 'slot'])
def test_partial_wrong_or_failed_enhanced_receipt_never_grants_proof(bad):
    f = notification(); r = f['params']['result']; m = r['transaction']['meta']
    if bad == 'signature': r['signature'] = 'wrong'
    if bad == 'failed': m['err'] = {'InstructionError': [0, 'failed']}
    if bad == 'missing_err': del m['err']
    if bad == 'missing_balances': del m['preBalances']
    if bad == 'missing_inner': del m['innerInstructions']
    if bad == 'missing_tokens': del m['postTokenBalances']
    if bad == 'wrong_encoding': r['transaction']['transaction'][1] = 'jsonParsed'
    if bad == 'slot': r['slot'] = True
    assert parse(f) is None


def test_subscription_is_confirmed_filtered_and_empty_roster_safe():
    from sniper.services.memecoin_helius_evidence import subscription
    assert subscription({}, 1) is None
    r = subscription(WATCH, 7)
    assert r['params'][0] == dict(accountInclude=[OWNER], failed=False, vote=False)
    assert r['params'][1]['commitment'] == 'confirmed'
    assert r['params'][1]['encoding'] == 'base64'


def test_rejected_subscription_reports_plan_without_echoing_provider_secrets():
    from sniper.services.memecoin_helius_evidence import run_feed
    stop, status = threading.Event(), {}
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): pass
        async def recv(self):
            stop.set()
            return json.dumps(dict(id=1, error=dict(code=-32600,
                message='transactionSubscribe is not available on the free plan secret-key')))
    asyncio.run(run_feed('wss://test', lambda: WATCH, object(), status, stop,
                         connect=lambda *a, **kw: Socket()))
    assert status['last_error'] == 'plan_unavailable'
    assert status['rejection_code'] == -32600
    assert 'secret-key' not in json.dumps(status)


def test_enhanced_watcher_uses_existing_writer_and_preserves_arrival(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services import memecoin_helius_evidence as h
    monkeypatch.setenv('MEMECOIN_HELIUS_EVIDENCE_ENABLED', '1')
    monkeypatch.setenv('ONCHAIN_WATCH_HELIUS_RPC_URL', 'https://mainnet.helius-rpc.com/?api-key=test-secret')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    monkeypatch.setattr(w, '_receipt_utc', lambda mono: 'original-arrival')
    processed = []; done = threading.Event()
    def process(*a, **kw):
        processed.append(kw); done.set(); return True
    monkeypatch.setattr(w, 'process_transaction', process)
    async def feed(url, owners_fn, worker, status, stop):
        worker.offer(notification(), owners_fn(), head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_epoch=105, signal_feed='helius_enhanced_confirmed')
        await asyncio.to_thread(done.wait, 1)
    monkeypatch.setattr(h, 'run_feed', feed)
    async def exercise():
        group = w.start_confirmed_feed(lambda: WATCH, object(), {})
        try:
            await asyncio.gather(*group['tasks'])
            assert group['worker'].has_receipt(FIX['transaction']['signatures'][0], OWNER)
            assert 'test-secret' not in json.dumps(group['status'])
        finally:
            group['stop'].set()
            for worker in group['workers']: worker.close()
    asyncio.run(exercise())
    assert processed[0]['detected_at'] == 'original-arrival'
    assert processed[0]['require_initiation'] is True
    assert processed[0]['commitment'] == 'confirmed'


def test_receiver_only_offers_acknowledged_confirmed_frames_and_counts_bytes():
    from sniper.services.memecoin_helius_evidence import run_feed
    stop, offered, sent, status = threading.Event(), [], [], {}
    frames = [notification(), dict(id=1, result=7), dict(id=0, result=9),
              _slot_frame(), notification()]
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): sent.append(json.loads(raw))
        async def recv(self):
            if len(frames) == 1: stop.set()
            return json.dumps(frames.pop(0))
    class Worker:
        def offer(self, frame, owners, **receipt): offered.append((frame, owners, receipt))
    asyncio.run(run_feed('wss://test', lambda: WATCH, Worker(), status, stop,
                         connect=lambda *a, **kw: Socket()))
    assert len(offered) == 1
    assert offered[0][2]['signal_feed'] == 'helius_enhanced_confirmed'
    assert offered[0][2]['head_slot'] == FIX['slot']
    assert status['bytes_received'] > 0
    assert sent[1]['params'][1]['commitment'] == 'confirmed'


def test_ack_deadline_holds_even_when_slots_keep_arriving(monkeypatch):
    from sniper.services import memecoin_helius_evidence as h
    from types import SimpleNamespace
    stop, status, clock = threading.Event(), {}, [0.0]
    monkeypatch.setattr(h, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 105))
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): pass
        async def recv(self):
            clock[0] += 1
            return json.dumps(dict(method='slotNotification',
                params=dict(result=dict(slot=FIX['slot'] + int(clock[0])))))
    class Worker:
        def offer(self, *a, **kw): pytest.fail('unacknowledged stream offered proof')
    async def exercise():
        task = asyncio.create_task(h.run_feed('wss://test', lambda: WATCH, Worker(), status, stop,
                                             connect=lambda *a, **kw: Socket()))
        for _ in range(100):
            await asyncio.sleep(0)
            if status.get('last_error'):
                break
        try:
            assert status['last_error'] == 'subscription_ack_timeout'
        finally:
            stop.set(); task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize('value', ['bad', 'https://mainnet.helius-rpc.com/?api-key=funded',
                                  'https://foreign.test/?api-key=secret'])
def test_config_rejects_bad_or_custody_keys_without_exposing_them(value):
    from sniper.services.memecoin_helius_evidence import live_config
    assert live_config({}) is None
    with pytest.raises(ValueError) as e:
        live_config(dict(MEMECOIN_HELIUS_EVIDENCE_ENABLED='1', ONCHAIN_WATCH_HELIUS_RPC_URL=value,
                         MEMECOIN_SOLANA_RPC_URL='https://mainnet.helius-rpc.com/?api-key=funded'))
    assert 'secret' not in str(e.value) and 'funded' not in str(e.value)


def test_enhanced_evidence_uses_paid_research_key_instead_of_free_watch_key():
    from sniper.services.memecoin_helius_evidence import live_config
    url = live_config(dict(MEMECOIN_HELIUS_EVIDENCE_ENABLED='1',
        MEMECOIN_RESEARCH_HELIUS_RPC_URL='https://mainnet.helius-rpc.com/?api-key=paid-research',
        ONCHAIN_WATCH_HELIUS_RPC_URL='https://mainnet.helius-rpc.com/?api-key=free-watch'))
    assert url == 'wss://mainnet.helius-rpc.com/?api-key=paid-research'


def test_helius_evidence_never_queues_behind_shyft_receipts(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services import memecoin_helius_evidence as h, memecoin_shyft_confirmed as g
    monkeypatch.setenv('MEMECOIN_HELIUS_EVIDENCE_ENABLED', '1')
    monkeypatch.setenv('ONCHAIN_WATCH_HELIUS_RPC_URL', 'https://mainnet.helius-rpc.com/?api-key=test')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'test')
    workers = {}
    async def helius(url, owners_fn, worker, status, stop): workers['helius'] = worker
    async def shyft(url, token, owners_fn, worker, status, stop): workers['shyft'] = worker
    monkeypatch.setattr(h, 'run_feed', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    async def exercise():
        group = w.start_confirmed_feed(lambda: WATCH, object(), {})
        try:
            await asyncio.gather(*group['tasks'])
            assert workers['helius'] is not workers['shyft']
        finally:
            group['stop'].set()
            for worker in set(workers.values()): worker.close()
    asyncio.run(exercise())


def test_enhanced_receipt_rechecks_roster_after_decode(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services import memecoin_helius_evidence as h
    monkeypatch.setenv('MEMECOIN_HELIUS_EVIDENCE_ENABLED', '1')
    monkeypatch.setenv('ONCHAIN_WATCH_HELIUS_RPC_URL', 'https://mainnet.helius-rpc.com/?api-key=test')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    watch, processed = dict(WATCH), []
    original = h.confirmed_transaction
    def decoder(*a, **kw):
        tx = original(*a, **kw)
        watch.clear()  # Removed while queued evidence was decoding.
        return tx
    monkeypatch.setattr(h, 'confirmed_transaction', decoder)
    monkeypatch.setattr(w, '_receipt_utc', lambda mono: 'original-arrival')
    monkeypatch.setattr(w, 'process_transaction', lambda *a, **kw: processed.append(kw) or True)
    async def feed(url, owners_fn, worker, status, stop):
        worker.offer(notification(), owners_fn(), head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_epoch=105, signal_feed='helius_enhanced_confirmed')
        await asyncio.to_thread(worker._jobs.join)
    monkeypatch.setattr(h, 'run_feed', feed)
    async def exercise():
        group = w.start_confirmed_feed(lambda: watch, object(), {})
        try:
            await asyncio.gather(*group['tasks'])
            assert not processed
        finally:
            group['stop'].set()
            for worker in group['workers']: worker.close()
    asyncio.run(exercise())


def _slot_frame(subscription_id=9):
    return dict(method='slotNotification', params=dict(subscription=subscription_id,
                                                     result=dict(slot=FIX['slot'])))


def _run_frames(frames, *, worker_result=True, connect_kwargs=None):
    from sniper.services.memecoin_helius_evidence import run_feed
    stop, offered, sent, status = threading.Event(), [], [], {}
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): sent.append(json.loads(raw))
        async def recv(self):
            if len(frames) == 1: stop.set()
            return json.dumps(frames.pop(0))
    class Worker:
        def offer(self, frame, owners, **receipt):
            offered.append((frame, owners, receipt))
            return worker_result
    def connect(*a, **kw):
        if connect_kwargs is not None: connect_kwargs.append(kw)
        return Socket()
    asyncio.run(run_feed('wss://offline', lambda: WATCH, Worker(), status, stop, connect=connect))
    return offered, sent, status


@pytest.mark.parametrize('bad_id', [True, 1.0, '1'])
def test_transaction_ack_requires_exact_request_id_type(bad_id):
    offered, _, _ = _run_frames([dict(id=0, result=9), _slot_frame(),
                                 dict(id=bad_id, result=7), notification()])
    assert not offered


@pytest.mark.parametrize('slot_frames', [
    [_slot_frame()],
    [dict(id=0, result=9), _slot_frame(10)],
    [dict(id=False, result=9), _slot_frame()],
])
def test_head_requires_exact_slot_ack_and_subscription_binding(slot_frames):
    offered, _, _ = _run_frames([dict(id=1, result=7), *slot_frames, notification()])
    assert not offered


@pytest.mark.parametrize('bad_id', [True, 1.0, '1'])
def test_transaction_notification_requires_exact_subscription_id_type(bad_id):
    frame = notification(); frame['params']['subscription'] = bad_id
    offered, _, _ = _run_frames([dict(id=0, result=9), _slot_frame(),
                                 dict(id=1, result=1), frame])
    assert not offered


def test_subscription_identifier_zero_is_valid():
    frame = notification(); frame['params']['subscription'] = 0
    offered, _, _ = _run_frames([dict(id=0, result=9), _slot_frame(), dict(id=1, result=0), frame])
    assert len(offered) == 1


def test_arrival_epoch_is_captured_before_json_work(monkeypatch):
    from sniper.services import memecoin_helius_evidence as h
    from types import SimpleNamespace
    epoch = [105.0]
    loads, dumps = json.loads, json.dumps
    def slow_json(raw):
        frame = loads(raw)
        if frame.get('method') == 'transactionNotification': epoch[0] += 5
        return frame
    monkeypatch.setattr(h, 'time', SimpleNamespace(monotonic=lambda: 1.0, time=lambda: epoch[0]))
    monkeypatch.setattr(h, 'json', SimpleNamespace(loads=slow_json, dumps=dumps))
    offered, _, _ = _run_frames([dict(id=0, result=9), _slot_frame(), dict(id=1, result=7), notification()])
    assert offered[0][2]['recv_monotonic'] == 1.0
    assert offered[0][2]['recv_epoch'] == 105.0


def test_queue_refusal_is_visible_without_waiting():
    _, _, status = _run_frames([dict(id=0, result=9), _slot_frame(),
                                dict(id=1, result=7), notification()], worker_result=False)
    assert status['queue_refused'] == 1


def test_subscription_rejects_filter_over_provider_limit():
    from sniper.services.memecoin_helius_evidence import subscription
    with pytest.raises(ValueError, match='owner_limit_exceeded'):
        subscription({str(i): None for i in range(50_001)}, 1)


@pytest.mark.parametrize('watch,funded', [('fu%6Eded', 'funded'), ('funded', 'fu%6Eded')])
def test_percent_encoded_custody_key_cannot_be_reused(watch, funded):
    from sniper.services.memecoin_helius_evidence import live_config
    with pytest.raises(ValueError, match='separate watch key'):
        live_config(dict(MEMECOIN_HELIUS_EVIDENCE_ENABLED='1',
            MEMECOIN_RESEARCH_HELIUS_RPC_URL='https://mainnet.helius-rpc.com/?api-key='+watch,
            MEMECOIN_SOLANA_RPC_URL='https://mainnet.helius-rpc.com/?api-key='+funded))


def test_bad_port_config_never_echoes_key_material():
    from sniper.services.memecoin_helius_evidence import live_config
    with pytest.raises(ValueError) as exc:
        live_config(dict(MEMECOIN_HELIUS_EVIDENCE_ENABLED='1',
            MEMECOIN_RESEARCH_HELIUS_RPC_URL='https://mainnet.helius-rpc.com:port-secret/?api-key=test'))
    assert 'port-secret' not in str(exc.value)


@pytest.mark.parametrize('trailing', [b'garbage', b'\x00' * 4096])
def test_noncanonical_or_oversized_transaction_wire_is_rejected(trailing):
    frame = notification(); wire = frame['params']['result']['transaction']['transaction']
    wire[0] = base64.b64encode(base64.b64decode(wire[0]) + trailing).decode()
    assert parse(frame) is None


def test_reconnect_clears_head_evidence_before_resubscribe(monkeypatch):
    from sniper.services import memecoin_helius_evidence as h
    stop, status, second = threading.Event(), {}, []
    frames = [dict(id=0, result=9), _slot_frame(), dict(id=1, result=7)]
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): pass
        async def recv(self):
            if frames: return json.dumps(frames.pop(0))
            raise RuntimeError('secret-provider-url')
    class Reconnected(Socket):
        async def send(self, raw):
            second.append(dict(status)); stop.set()
    calls = []
    def connect(*a, **kw):
        calls.append(kw)
        return Socket() if len(calls) == 1 else Reconnected()
    asyncio.run(h.run_feed('wss://offline', lambda: WATCH, object(), status, stop, connect=connect))
    assert second[0]['head_slot'] is None
    assert second[0]['connected'] is False
    assert 'secret-provider-url' not in json.dumps(status)


def test_pending_serialized_byte_limit_drops_without_wait_and_releases_once(monkeypatch):
    from sniper.services import memecoin_helius_evidence as h
    frame = notification(); size = len(json.dumps(frame).encode())
    monkeypatch.setattr(h, 'MAX_PENDING_BYTES', 2 * size, raising=False)
    connects = []
    offered, _, status = _run_frames([dict(id=0, result=9), _slot_frame(), dict(id=1, result=7),
                                      frame, frame, frame], connect_kwargs=connects)
    assert len(offered) == 2
    assert status['byte_budget_refused'] == 1 and status['pending_bytes'] == 2 * size
    assert connects[0]['max_queue'] == 2
    offered[0][2]['on_released'](); offered[0][2]['on_released']()
    assert status['pending_bytes'] == size
    offered[1][2]['on_released']()
    assert status['pending_bytes'] == 0


def test_byte_budget_survives_reconnect_and_recovers_after_release(monkeypatch):
    from sniper.services import memecoin_helius_evidence as h
    frame = notification(); size = len(json.dumps(frame).encode())
    monkeypatch.setattr(h, 'MAX_PENDING_BYTES', size, raising=False)
    stop, status, offered, calls = threading.Event(), {}, [], []
    first = [dict(id=0, result=9), _slot_frame(), dict(id=1, result=7), frame]
    second_frame = notification(); second_frame['params']['subscription'] = 8
    second = [dict(id=0, result=10), _slot_frame(10), dict(id=1, result=8), second_frame,
              dict(method='offline_release'), second_frame]
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): pass
        async def recv(self):
            frames = first if len(calls) == 1 else second
            if not frames: raise RuntimeError('disconnect')
            item = frames.pop(0)
            if item.get('method') == 'offline_release': offered[0]['on_released']()
            if len(calls) == 2 and not frames: stop.set()
            return json.dumps(item)
    class Worker:
        def offer(self, frame, owners, **receipt): offered.append(receipt); return True
    def connect(*a, **kw):
        calls.append(kw)
        if len(calls) > 2: stop.set()
        return Socket()
    asyncio.run(h.run_feed('wss://offline', lambda: WATCH, Worker(), status, stop, connect=connect))
    assert len(offered) == 2 and status['byte_budget_refused'] == 1
    assert status['pending_bytes'] == size
    offered[-1]['on_released']()
    assert status['pending_bytes'] == 0


def test_disconnected_status_cannot_keep_an_old_head():
    _, _, status = _run_frames([dict(id=0, result=9), _slot_frame(), dict(id=1, result=7), notification()])
    assert status['head_slot'] is None and status['head_slot_monotonic'] is None


@pytest.mark.parametrize('userinfo', ['@', ':@'])
def test_even_empty_url_userinfo_is_rejected(userinfo):
    from sniper.services.memecoin_helius_evidence import live_config
    with pytest.raises(ValueError, match='separate watch key'):
        live_config(dict(MEMECOIN_HELIUS_EVIDENCE_ENABLED='1',
            MEMECOIN_RESEARCH_HELIUS_RPC_URL='https://'+userinfo+'mainnet.helius-rpc.com/?api-key=test'))


@pytest.mark.parametrize('kind,code', [('transaction', 'subscription_ack_timeout'),
                                     ('slot', 'slot_subscription_ack_timeout')])
def test_ack_received_after_deadline_cannot_mark_feed_connected(monkeypatch, kind, code):
    from sniper.services import memecoin_helius_evidence as h
    from types import SimpleNamespace
    stop, status, clock = threading.Event(), {}, [0.0]
    monkeypatch.setattr(h, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 105))
    frames = ([dict(id=0, result=9), _slot_frame(), dict(id=1, result=7)] if kind == 'transaction'
              else [dict(id=1, result=7), dict(id=0, result=9)])
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def send(self, raw): pass
        async def recv(self):
            if len(frames) == 1:
                clock[0] = 10.01; stop.set()
            return json.dumps(frames.pop(0))
    asyncio.run(h.run_feed('wss://offline', lambda: WATCH, object(), status, stop,
                          connect=lambda *a, **kw: Socket()))
    assert status['last_error'] == code


def test_error_and_result_response_cannot_acknowledge_subscription():
    offered, _, status = _run_frames([dict(id=0, result=9), _slot_frame(),
        dict(id=1, result=7, error=dict(code=-32600, message='secret-key')), notification()])
    assert not offered and status['last_error'] == 'subscription_rejected'
