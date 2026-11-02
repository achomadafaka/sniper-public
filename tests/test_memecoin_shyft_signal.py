"""Shyft live input never changes funded authority or resets receipt time."""
import asyncio
import copy
import json
import threading
from pathlib import Path

import base58
import pytest

OWNER = '2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf'
FRAMES = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_signal_frames.json').read_text())


def update(kind='curve_buy', owner=OWNER):
    from sniper.services.memecoin_shyft_signal import pb
    frame = FRAMES[kind]['params']['result']
    u = pb.SubscribeUpdate()
    u.transaction.slot = frame['context']['slot']
    info = u.transaction.transaction
    info.signature = base58.b58decode(frame['value']['signature'])
    info.transaction.signatures.append(info.signature)
    info.transaction.message.header.num_required_signatures = 1
    info.transaction.message.account_keys.append(base58.b58decode(owner))
    info.meta.log_messages.extend(frame['value']['logs'])
    return u


def decode(u, owners=None):
    from sniper.services.memecoin_shyft_signal import decode_update
    return decode_update(u, owners or {OWNER: ('uid', 'buyer')},
                         recv_monotonic=0.0, recv_utc='receipt',
                         head_slot=u.transaction.slot, head_mono=0.0)


def test_successful_full_metadata_reuses_native_decoder_and_preserves_zero_clock():
    buys = decode(update())
    assert len(buys) == 1
    b = buys[0]
    assert (b.owner, b.source_user_id, b.recv_monotonic, b.recv_utc) == (OWNER, 'uid', 0.0, 'receipt')
    assert (b.venue, b.sol_amount, b.token_amount) == ('pump_curve', 98765431, 984478963817)
    assert b.source_initiation is None  # processed input never fabricates confirmed proof


@pytest.mark.parametrize('bad', ['meta_absent', 'failed', 'logs_absent', 'signature', 'signature_mismatch',
                                'key', 'slot', 'header', 'unsigned_owner'])
def test_partial_failed_or_wrong_identity_cannot_emit(bad):
    u = update()
    i = u.transaction.transaction
    if bad == 'meta_absent': i.ClearField('meta')
    if bad == 'failed': i.meta.err.err = b'failed'
    if bad == 'logs_absent': i.meta.log_messages_none = True
    if bad == 'signature': i.signature = b'wrong'
    if bad == 'signature_mismatch': i.transaction.signatures[0] = bytes(64)
    if bad == 'key': i.transaction.message.account_keys[0] = b'wrong'
    if bad == 'slot': u.transaction.slot = 0
    if bad == 'header': i.transaction.message.ClearField('header')
    if bad == 'unsigned_owner':
        i.transaction.message.account_keys.insert(0, bytes(32))
    assert decode(u) == ()


def test_unwatched_owner_and_sell_cannot_be_a_buy():
    assert decode(update(), {'D4pycXUXjs7FEWES39RMwfrs2E5LsbxGm5asCX6Tthqu': ('u', 'seller')}) == ()
    assert decode(update('curve_sell', 'D4pycXUXjs7FEWES39RMwfrs2E5LsbxGm5asCX6Tthqu')) == ()


@pytest.mark.parametrize('head,age', [(None, 0), (450781553, 0), (450781549, 3)])
def test_unknown_stale_head_and_replay_cannot_emit(head, age):
    from sniper.services.memecoin_shyft_signal import decode_update
    assert decode_update(update(), {OWNER: ('u', 'h')}, recv_monotonic=age, recv_utc='',
                         head_slot=head, head_mono=0) == ()


def test_config_is_explicit_and_never_exposes_token():
    from sniper.services.memecoin_shyft_signal import live_config
    assert live_config({}) is None
    with pytest.raises(ValueError, match='token_missing'):
        live_config({'MEMECOIN_SHYFT_LIVE_ENABLED': '1'})
    for url in ('http://beta-grpc.ams.shyft.to', 'https://foreign.test',
                'https://secret@beta-grpc.ams.shyft.to', 'https://beta-grpc.ams.shyft.to/?token=secret'):
        with pytest.raises(ValueError) as e:
            live_config({'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret',
                         'MEMECOIN_SHYFT_GRPC_URLS': url})
        assert 'secret' not in str(e.value)


@pytest.mark.parametrize('alias', ['https://beta-grpc.ams.shyft.to/',
                                   'https://beta-grpc.ams.shyft.to:443'])
def test_aliases_cannot_open_two_connections_with_one_provider_status(alias):
    from sniper.services.memecoin_shyft_signal import live_config
    with pytest.raises(ValueError, match='endpoint_invalid'):
        live_config({'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret',
                     'MEMECOIN_SHYFT_GRPC_URLS': 'https://beta-grpc.ams.shyft.to,' + alias})


def test_empty_roster_subscribes_to_no_transactions_and_never_requests_replay():
    from sniper.services.memecoin_shyft_signal import subscription
    req = subscription({})
    assert not req.transactions
    from sniper.services.memecoin_shyft_signal import pb
    assert req.commitment == pb.PROCESSED
    assert not req.HasField('from_slot')
    req = subscription({OWNER: ('u', 'h')})
    assert list(req.transactions['owners'].account_include) == [OWNER]
    assert req.transactions['owners'].failed is False
    assert req.commitment == 0


def test_shared_signal_loop_first_valid_feed_wins_without_duplicate(monkeypatch):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    b = decode(update())[0]
    received, stop, status = [], threading.Event(), {'signal_feeds': {}}

    async def helius(on_buy, *a, **kw):
        await asyncio.sleep(.01)
        on_buy(b)
        stop.set()

    async def shyft(url, token, owners_fn, on_buy, status, stop):
        on_buy(copy.copy(b))
        await asyncio.sleep(1)

    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(received.append, dict, 'wss://unused', status, stop, None,
                               None, None, None, ('secret', ('https://beta-grpc.ams.shyft.to',))))
    assert len(received) == 1
    assert received[0].signal_feed == 'shyft:beta-grpc.ams.shyft.to'
    assert received[0].recv_monotonic == 0.0
    assert status['signal_feeds']['helius']['duplicates'] == 1


def test_shyft_only_coordinator_needs_no_helius_and_retains_both_receipts(monkeypatch):
    from dataclasses import replace
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_feed_arrivals import _journal
    received, stop, status, journal = [], threading.Event(), {}, _journal()
    buy = decode(update())[0]
    async def forbidden(*a, **kw):
        raise AssertionError('Shyft profile opened Helius')
    async def feed(url, token, owners_fn, on_buy, feed_status, stop, **kw):
        second = 'fra' in url
        on_buy(replace(buy, recv_monotonic=.01 if second else 0))
        feed_status.update(connected=True, requested_owners=(OWNER,), acknowledged_owners=(OWNER,),
                           head_slot=buy.slot, head_slot_mono=0)
        if second:
            stop.set()
        await asyncio.sleep(1)
    monkeypatch.setattr(s, '_run', forbidden)
    monkeypatch.setattr(g, 'run_feed', feed)
    asyncio.run(s._run_signals(received.append, lambda: {OWNER: ('uid', 'h')}, None,
        status, stop, None, None, None, None,
        ('secret', ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')),
        infra_provider='shyft', measurement=journal))
    assert len(received) == 1 and received[0].recv_monotonic == 0
    assert 'helius' not in status['signal_feeds']
    assert status['acknowledged_owners'] == (OWNER,)
    assert len([r for r in journal.snapshot()['rows'] if r['kind'] == 'source']) == 2
    pair, = status['arrival_metrics'].snapshot(now=1)['pairs'].values()
    assert pair['n'] == 1 and pair['p50_ms'] == 10


@pytest.mark.parametrize('bad', [{}, {'MEMECOIN_SHYFT_LIVE_ENABLED': '0'},
    {'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret'},
    {'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret',
     'MEMECOIN_SHYFT_GRPC_URLS': 'https://beta-grpc.ams.shyft.to'},
    {'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret',
     'MEMECOIN_SHYFT_GRPC_URLS': 'https://grpc.ny.shyft.to'}])
def test_shyft_profile_requires_enabled_explicit_production_europe_endpoints(bad):
    from sniper.services.memecoin_shyft_signal import live_config
    with pytest.raises(ValueError):
        live_config(dict(bad, MEMECOIN_SOLANA_INFRA_PROVIDER='shyft'))


def test_shyft_profile_accepts_production_europe_only():
    from sniper.services.memecoin_shyft_signal import live_config
    urls = ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')
    assert live_config({'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
        'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret',
        'MEMECOIN_SHYFT_GRPC_URLS': ','.join(urls)}) == ('secret', urls)


def test_far_future_source_slot_is_rejected_but_next_slot_is_allowed():
    from sniper.services.memecoin_shyft_signal import decode_update
    u = update()
    kw = dict(recv_monotonic=0, recv_utc='', head_mono=0)
    assert decode_update(u, {OWNER: ('u', 'h')}, head_slot=u.transaction.slot - 1, **kw)
    assert not decode_update(u, {OWNER: ('u', 'h')}, head_slot=u.transaction.slot - 3, **kw)


@pytest.mark.parametrize('backwards', [False, True])
def test_dead_head_cannot_anchor_a_buy_until_new_live_head(monkeypatch, backwards):
    import grpc
    from sniper.services import memecoin_shyft_signal as g
    stop, status, snapshots = threading.Event(), {}, []
    async def exercise():
        async def subscribe(incoming, context):
            async for request in incoming:
                if not request.HasField('ping'):
                    continue
                pong = g.pb.SubscribeUpdate(); pong.pong.id = request.ping.id
                yield pong
                head = g.pb.SubscribeUpdate(); head.slot.slot = update().transaction.slot
                yield head
                dead = g.pb.SubscribeUpdate(); dead.slot.slot = head.slot.slot; dead.slot.status = g.pb.SLOT_DEAD
                yield dead
                if backwards:
                    old = g.pb.SubscribeUpdate(); old.slot.slot = head.slot.slot - 1
                    yield old
                    yield head  # Repeated status of the dead slot cannot restore it either.
                buy = update(); buy.transaction.slot += 1
                yield buy
                head.slot.slot += 1
                yield head
                yield buy
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel',
            lambda *a, **kw: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        def receive(buy):
            snapshots.append((buy, dict(status))); stop.set()
        try:
            await asyncio.wait_for(g.run_feed('https://grpc.ams.shyft.to', 'test-token',
                lambda: {OWNER: ('uid', 'h')}, receive, status, stop), timeout=3)
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    buy, observed = snapshots[0]
    assert observed['head_slot'] == buy.slot


def test_signal_key_overlap_uses_exact_shyft_credential_identity():
    from sniper.services.memecoin_slot_signal import _shares_funded_key
    env = {'MEMECOIN_SOLANA_RPC_POOL_URLS': 'https://rpc.ams.shyft.to/?api_key=shared',
           'MEMECOIN_SOLANA_RPC_URL': 'https://rpc.fra.shyft.to/?api_key=other'}
    assert _shares_funded_key('https://rpc.fra.shyft.to/?api_key=shared', env)
    assert not _shares_funded_key('https://rpc.ams.shyft.to/?api_key=share', env)


def test_heartbeat_exposes_provider_status_without_credentials():
    from sniper.services.memecoin_shyft_signal import live_config
    token, urls = live_config({'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret'})
    assert token == 'secret' and len(urls) == 2
    from sniper.services.memecoin_slot_lane import result_log_line
    from dataclasses import replace
    b = replace(decode(update())[0], signal_feed='shyft:beta-grpc.ams.shyft.to')
    assert 'feed=shyft:beta-grpc.ams.shyft.to' in result_log_line(b, {'status': 'filled'})


def test_provider_liveness_and_winners_reach_actual_heartbeat(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    feeds = {'shyft:beta-grpc.ams.shyft.to': {'connected': True, 'owners': 27, 'wins': 2,
             'last_frame_mono': 1.0, 'last_signature': 'sig', 'duplicates': 1,
             'requested_owners': (OWNER,), 'acknowledged_owners': (OWNER,)}}
    write_heartbeat({}, auth_ok=True, path=path, ts=1.0,
                    slot_lane_status={'signal': {'signal_feeds': feeds}})
    result = json.loads(path.read_text())['slot_lane']['signal_feeds']
    assert result['shyft:beta-grpc.ams.shyft.to']['wins'] == 2
    assert result['shyft:beta-grpc.ams.shyft.to']['connected'] is True
    assert result['shyft:beta-grpc.ams.shyft.to']['acknowledged_owners'] == [OWNER]
    assert result['shyft:beta-grpc.ams.shyft.to']['requested_owners'] == [OWNER]


def test_heartbeat_uses_one_provider_snapshot_while_signal_starts(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    class Growing(dict):
        calls = 0
        def copy(self):
            self.calls += 1
            if self.calls > 1:
                self['shyft:beta-grpc.fra.shyft.to'] = {}
            return dict(self)
    feeds = Growing({'helius': {}})
    path = tmp_path / 'heartbeat'
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={'signal': {'signal_feeds': feeds}})
    assert feeds.calls == 1


def test_real_startup_refuses_live_flag_without_token(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.delenv('MEMECOIN_SHYFT_GRPC_TOKEN', raising=False)
    with pytest.raises(RuntimeError, match='Shyft live feed invalid'):
        cfg.validate_real_exit_policy()


def test_shyft_profile_startup_refuses_disabled_feed(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    with pytest.raises(RuntimeError, match='Shyft live feed invalid'):
        cfg.validate_real_exit_policy()


def test_no_pong_deadline_applies_even_when_slots_keep_arriving(monkeypatch):
    from sniper.services import memecoin_shyft_signal as g
    import grpc
    clock = {'now': 0.0}
    stop, status = threading.Event(), {}

    class Stream:
        def __aiter__(self): return self
        async def __anext__(self):
            clock['now'] += 4
            if clock['now'] >= 16:
                stop.set()
            u = g.pb.SubscribeUpdate()
            u.slot.slot = 450781549
            return u
        def cancel(self): pass

    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream_stream(self, *a, **kw): return lambda *a, **kw: Stream()

    async def no_wait(*a): pass
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: Channel())
    from types import SimpleNamespace
    monkeypatch.setattr(g, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(g.asyncio, 'sleep', no_wait)
    asyncio.run(g.run_feed('https://beta-grpc.ams.shyft.to', 'secret', dict,
                          lambda b: pytest.fail('unacknowledged stream emitted'), status, stop))
    assert status['last_error'] == 'subscription_ack_timeout'
    assert status['connected'] is False


@pytest.mark.parametrize('pong_offset', [0, 999])
def test_actual_grpc_wire_ack_head_and_native_buy_close_cleanly(monkeypatch, pong_offset):
    from sniper.services import memecoin_shyft_signal as g
    import grpc
    received, requests, status, stop = [], [], {}, threading.Event()
    snapshots = []
    import sniper.util.clock as clock
    monkeypatch.setattr(clock, 'real_utc_now_iso', lambda: 'corrected-receipt')

    async def exercise():
        async def subscribe(incoming, context):
            assert dict(context.invocation_metadata())['x-token'] == 'test-token'
            async for req in incoming:
                requests.append(req)
                if req.HasField('ping'):
                    pong = g.pb.SubscribeUpdate(); pong.pong.id = req.ping.id + pong_offset
                    yield pong
                    head = g.pb.SubscribeUpdate(); head.slot.slot = update().transaction.slot
                    yield head
                    yield update()
                    if pong_offset:
                        pong.pong.id = req.ping.id
                        yield pong
                        yield update()

        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel',
                            lambda *a, **kw: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        def offer(b):
            received.append(b)
            snapshots.append(dict(status))
            stop.set()
        try:
            await asyncio.wait_for(g.run_feed('https://beta-grpc.ams.shyft.to', 'test-token',
                lambda: {OWNER: ('uid', 'h')}, offer, status, stop), timeout=3)
        finally:
            await server.stop(0)

    asyncio.run(exercise())
    assert len(received) == 1 and received[0].owner == OWNER
    assert received[0].recv_utc == 'corrected-receipt'
    assert requests[0].transactions['owners'].failed is False
    assert status['connected'] is False and status['decode_errors'] == 0
    assert status['decoded'] == 1
    assert snapshots[0]['requested_owners'] == (OWNER,)
    assert snapshots[0]['acknowledged_owners'] == (OWNER,)
    assert snapshots[0]['head_slot_mono'] is not None
    assert snapshots[0]['head_slot_mono'] <= received[0].recv_monotonic
    assert status['acknowledged_owners'] == ()


def test_processed_routed_metadata_reuses_pairer_and_cannot_grant_signer_payment_authority():
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    from sniper.services.memecoin_shyft_signal import routed_buys
    from sniper.services.memecoin_slot_signal import RoutedPairer
    u = wire_receipt()
    seen = []
    original = RoutedPairer.on_logs
    def checked(self, *args, **kwargs):
        seen.append(1)
        return original(self, *args, **kwargs)
    from unittest.mock import patch
    with patch.object(RoutedPairer, 'on_logs', checked):
        buys = routed_buys(u, {payer: ('uid', 'h')}, recv_monotonic=0, recv_utc='corrected',
            head_slot=FIX['slot'], head_mono=0)
    assert seen and len(buys) == 1
    buy = buys[0]
    assert (buy.usdc_spent_atomic, buy.token_delta_atomic) == (500000000, 16675750779965)
    assert buy.owner == payer and buy.recv_monotonic == 0 and buy.recv_utc == 'corrected'
    assert buy.paired_head_slot == FIX['slot']
    assert buy.source_initiation is None  # knob MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF off (default)


def test_processed_routed_refuses_failed_stale_unknown_roster_and_native_duplicate():
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    from sniper.services.memecoin_shyft_signal import routed_buys
    args = dict(recv_monotonic=1, recv_utc='receipt', head_slot=FIX['slot'], head_mono=1)
    assert not routed_buys(wire_receipt(), {}, **args)
    assert not routed_buys(wire_receipt(), {payer: ('uid', 'h')}, **dict(args, head_mono=-2))
    u = wire_receipt(); u.transaction.transaction.meta.err.err = b'failed'
    assert not routed_buys(u, {payer: ('uid', 'h')}, **args)
    native = update()
    assert not routed_buys(native, {OWNER: ('uid', 'h')}, **dict(args, head_slot=native.transaction.slot))


def test_processed_routed_binds_optional_receipt_metadata_in_existing_replacement(monkeypatch):
    from dataclasses import replace as real_replace
    from unittest.mock import patch
    from sniper.services.memecoin_shyft_signal import routed_buys
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    frozen = _journal().identity(0, 4)
    replacements = []
    def checked(buy, **fields):
        replacements.append(fields)
        return real_replace(buy, **fields)
    with patch('dataclasses.replace', checked):
        buy, = routed_buys(wire_receipt(), {payer: ('uid', 'original')},
            recv_monotonic=0, recv_utc='original', head_slot=FIX['slot'], head_mono=0,
            signal_feed='shyft:original', receipt_evidence=frozen)
    expected_pre = sum(int(row['uiTokenAmount']['amount']) for row in FIX['meta']['preTokenBalances']
        if row['owner'] == payer and row['mint'] == buy.mint)
    assert replacements == [dict(paired_head_slot=FIX['slot'],
        signal_feed='shyft:original', receipt_evidence=frozen, source_pre_atomic=expected_pre)]
    assert buy.receipt_evidence is frozen and buy.signal_feed == 'shyft:original'
    assert (buy.recv_monotonic, buy.recv_utc, buy.handle) == (0, 'original', 'original')
    assert not buy.requires_receipt_proof


@pytest.mark.parametrize('retained', [True, False])
def test_metadata_worker_offers_decoder_object_without_cloning_original_receipt(monkeypatch, retained):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    journal, stop, status, decoded, received = _journal(10), threading.Event(), {}, [], []
    frozen = journal.identity(0, 4)
    original_owners = {payer: ('uid', 'original')}
    current_owners = original_owners if retained else {}
    real_decode = g.routed_buys
    real_publish = g._publish_empty_receipt
    def publish_without_encoding(*args, **kwargs):
        from unittest.mock import patch
        with patch('base58.b58encode', side_effect=AssertionError('decoded signature encoded twice')):
            real_publish(*args, **kwargs)
    monkeypatch.setattr(g, '_publish_empty_receipt', publish_without_encoding)
    def decode_original(update, owners, **receipt):
        assert owners is original_owners
        buys = real_decode(update, owners, **receipt)
        decoded.extend(buys)
        return buys
    def forbidden_replace(*args, **kwargs):
        raise AssertionError('metadata worker reconstructed the immutable decoded candidate')
    monkeypatch.setattr(g, 'routed_buys', decode_original)
    monkeypatch.setattr(s, 'replace', forbidden_replace)
    async def helius(*args, **kwargs):
        await asyncio.sleep(2)
    async def shyft(url, token, owners_fn, on_buy, feed_status, halt, *, on_metadata):
        on_metadata(wire_receipt(), recv_monotonic=0, recv_utc='original',
            head_slot=FIX['slot'], head_mono=0, receipt_evidence=frozen,
            receipt_owners=original_owners, _pending_unknown=4)
        try:
            for _ in range(1000):
                worker = status['metadata_worker']
                if worker['decoded'] or worker['roster_refused'] or worker['errors']:
                    break
                await asyncio.sleep(.001)
        finally:
            stop.set()
    def offered(buy):
        assert not journal.snapshot()['rows']  # funded callback precedes publication
        received.append(buy)
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(lambda buy: pytest.fail('native'), lambda: dict(current_owners),
        'unused', status, stop, None, None, offered, None,
        ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal))
    assert status['metadata_worker']['errors'] == 0
    buy, = decoded
    assert buy.receipt_evidence is frozen and buy.signal_feed == 'shyft:beta-grpc.ams.shyft.to'
    assert (buy.recv_monotonic, buy.recv_utc, buy.paired_head_slot) == (0, 'original', FIX['slot'])
    assert received == ([buy] if retained else [])
    if retained:
        assert received[0] is buy
    row, raw = journal.snapshot()['rows']
    assert raw['decode_state'] == 'empty_decode_unknown'
    assert raw['source_sig'] == row['source_sig'] == buy.signature
    assert raw['recv_monotonic'] == 0 and raw['connection_generation'] == 4
    assert row['recv_monotonic'] == 0 and row['connection_generation'] == 4
    assert row['decode_state'] == ('decoded_buy' if retained else 'roster_refused_unknown')


def test_processed_metadata_worker_is_bounded_offloop_and_rechecks_roster(monkeypatch):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    entered, release = threading.Event(), threading.Event()
    from test_memecoin_feed_arrivals import _journal
    journal = _journal(100)
    stop, status, received = threading.Event(), {}, []
    watch = {payer: ('uid', 'h')}
    thread_ids = []
    decode_routed = g.routed_buys
    def blocked(*args, **kwargs):
        thread_ids.append(threading.get_ident()); entered.set(); release.wait(1)
        return decode_routed(*args, **kwargs)
    monkeypatch.setattr(g, 'routed_buys', blocked)
    async def helius(*args, **kwargs):
        await asyncio.sleep(2)
        stop.set()
    async def shyft(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None):
        for _ in range(66):
            on_metadata(wire_receipt(), recv_monotonic=0, recv_utc='receipt',
                head_slot=FIX['slot'], head_mono=0)
        while not entered.is_set():
            await asyncio.sleep(.001)
        watch.clear()  # removed while decode is in progress; no late authority/candidate
        release.set()
        await asyncio.sleep(.02)
        stop.set()
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(lambda buy: pytest.fail('native'), lambda: dict(watch), 'unused',
        status, stop, None, None, received.append, None,
        ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal))
    assert thread_ids[0] != threading.get_ident() and not received
    gaps = [r for r in journal.snapshot()['rows'] if r['kind'] == 'gap']
    assert sum(r['lost_count'] for r in gaps if r['reason'] == 'metadata_overflow') == 2
    assert status['metadata_worker']['overflow'] == 2
    assert status['metadata_worker']['capacity'] == 64
    assert sum(status['arrival_metrics'].snapshot(now=3)['winners'].values()) == 0


def test_routed_cross_feed_shared_dedup_keeps_native_arrival_denominator(monkeypatch):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    stop, status, received = threading.Event(), {}, []
    watch = {payer: ('uid', 'h')}
    buy = g.routed_buys(wire_receipt(), watch, recv_monotonic=0, recv_utc='receipt',
        head_slot=FIX['slot'], head_mono=0)[0]
    async def helius(on_buy, owners_fn, ws_url, status, stop, connect, on_sell, on_routed, baseline):
        on_routed(buy)
        await asyncio.sleep(.01)
        stop.set()
    async def shyft(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None):
        on_metadata(wire_receipt(), recv_monotonic=0, recv_utc='receipt',
            head_slot=FIX['slot'], head_mono=0)
        await asyncio.sleep(1)
    monkeypatch.setattr(s, '_run', helius); monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(lambda buy: pytest.fail('native'), lambda: watch, 'unused',
        status, stop, None, None, received.append, None,
        ('secret', ('https://beta-grpc.ams.shyft.to',))))
    assert len(received) == 1 and received[0].signal_feed == 'helius'
    # Codex P2 2026-10-15: a routed buy now joins the decoded-buy population on the receipt stamp
    # of the feed that saw its frame (shyft, at receipt); before, routed buys never paired at all.
    assert status['arrival_metrics'].snapshot(now=3)['winners'] == {
        'helius': 0, 'shyft:beta-grpc.ams.shyft.to': 1}
    assert status['signal_feeds']['shyft:beta-grpc.ams.shyft.to']['duplicates'] == 1


def test_actual_processed_grpc_wire_dispatches_unsigned_routed_candidate_without_confirmed_subscription(monkeypatch):
    from sniper.services import memecoin_shyft_signal as g
    from sniper.services import memecoin_slot_signal as s
    from test_memecoin_shyft_confirmed import wire_receipt
    import grpc
    from sniper.execution import raydium_cpmm_buy
    monkeypatch.setattr(raydium_cpmm_buy, 'lane_mode', lambda: 'live')
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin/sol-route-provf4-27MRyWXY.json').read_text())
    owner = fixture['transaction']['message']['accountKeys'][1]['pubkey']
    from test_memecoin_feed_arrivals import _journal
    journal = _journal(20)
    stop, status, received, requests = threading.Event(), {}, [], []
    async def helius(*args, **kwargs):
        await asyncio.sleep(3)
    monkeypatch.setattr(s, '_run', helius)
    async def exercise():
        async def subscribe(incoming, context):
            async for req in incoming:
                requests.append(req)
                if req.HasField('ping'):
                    pong = g.pb.SubscribeUpdate(); pong.pong.id = req.ping.id
                    yield pong
                    head = g.pb.SubscribeUpdate(); head.slot.slot = fixture['slot']
                    yield head
                    yield wire_receipt(fixture)
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel',
            lambda *a, **kw: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        def offer(buy):
            assert not journal.snapshot()['rows']
            received.append(buy); stop.set()
        try:
            await asyncio.wait_for(s._run_signals(lambda b: pytest.fail('native actor'),
                lambda: {owner: ('uid', 'h')}, 'unused', status, stop, None, None, offer, None,
                ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal), timeout=2)
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    assert len(received) == 1
    buy = received[0]
    assert buy.usdc_spent_atomic == 3000000000 and buy.venue_events
    assert buy.signal_feed == 'shyft:beta-grpc.ams.shyft.to'
    assert buy.source_initiation is None  # knob MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF off (default)
    assert requests[0].commitment == g.pb.PROCESSED
    assert len([x for x in requests if x.transactions]) == 1
    # Codex P2 2026-10-15: the routed buy pairs on its receipt stamp (one feed here, one winner).
    assert status['arrival_metrics'].snapshot()['winners'] == {
        'helius': 0, 'shyft:beta-grpc.ams.shyft.to': 1}
    rows = [r for r in journal.snapshot()['rows'] if r['kind'] == 'source']
    assert [r['decode_state'] for r in rows] == ['decoded_buy', 'empty_decode_unknown']
    assert rows[0]['source_sig'] == rows[1]['source_sig'] == buy.signature
    assert rows[1]['recv_monotonic'] == buy.recv_monotonic
    assert rows[1]['connection_generation'] == 1


@pytest.mark.parametrize('outcome', ['empty', 'failure', 'cancel', 'overflow'])
def test_pending_empty_receipts_survive_worker_failure_cancel_and_overflow(monkeypatch, outcome):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    journal, stop, status = _journal(150), threading.Event(), {}
    frozen = journal.identity(0, 7)
    watch = {payer: ('uid', 'original')}
    def decode(update, owners, **receipt):
        assert owners is watch and '_pending_unknown' not in receipt
        assert receipt['receipt_evidence'] is frozen
        if outcome == 'failure':
            raise ValueError('fixture')
        if outcome == 'cancel':
            stop.set()
            raise asyncio.CancelledError
        return ()
    monkeypatch.setattr(g, 'routed_buys', decode)
    async def helius(*args, **kwargs):
        await asyncio.sleep(2)
    async def shyft(url, token, owners_fn, on_buy, feed_status, halt, *, on_metadata):
        for _ in range(66 if outcome == 'overflow' else 1):
            on_metadata(wire_receipt(), recv_monotonic=0, recv_utc='original',
                head_slot=FIX['slot'], head_mono=0, receipt_evidence=frozen,
                receipt_owners=watch, _pending_unknown=7)
        if outcome != 'overflow':
            for _ in range(1000):
                if journal.snapshot()['rows'] or stop.is_set():
                    break
                await asyncio.sleep(.001)
        stop.set()
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(lambda b: pytest.fail('native'), dict, 'unused',
        status, stop, None, None, lambda b: pytest.fail('routed'), None,
        ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal))
    rows = journal.snapshot()['rows']
    sources = [r for r in rows if r['kind'] == 'source']
    assert len(sources) == (66 if outcome == 'overflow' else 1)
    assert all(r['decode_state'] == 'empty_decode_unknown' for r in sources)
    assert all(r['source_sig'] == FIX['transaction']['signatures'][0] for r in sources)
    assert all(r['recv_monotonic'] == 0 and r['connection_generation'] == 7 for r in sources)
    assert all(r['connection_generation'] == 7 for r in rows if r['kind'] == 'gap')
    if outcome == 'failure':
        assert any(r.get('reason') == 'metadata_decode_error' for r in rows)
    if outcome == 'overflow':
        assert sum(r['lost_count'] for r in rows if r.get('reason') == 'metadata_overflow') == 2


@pytest.mark.parametrize('metadata_failure', [False, True])
def test_source_empty_receipt_legacy_and_metadata_offer_failure_publish_once(monkeypatch, metadata_failure):
    import grpc
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    journal, stop, status = _journal(10), threading.Event(), {}
    class Stream:
        def __init__(self, requests):
            self.requests, self.index = requests, 0
        def __aiter__(self): return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                await anext(self.requests)
                request = await anext(self.requests)
                pong = g.pb.SubscribeUpdate(); pong.pong.id = request.ping.id
                return pong
            if self.index == 2:
                head = g.pb.SubscribeUpdate(); head.slot.slot = FIX['slot']
                return head
            if self.index == 3:
                return wire_receipt()
            await asyncio.sleep(2)
        def cancel(self): pass
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs):
            return lambda requests, **kwargs: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def fail_offer(update, **receipt):
        assert not journal.snapshot()['rows']
        raise ValueError('fixture queue offer')
    async def exercise():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(g.run_feed('https://beta-grpc.ams.shyft.to', 'secret',
                lambda: {payer: ('uid', 'original')}, lambda b: pytest.fail('native'), status, stop,
                on_metadata=fail_offer if metadata_failure else None, measurement=journal), .05)
    asyncio.run(exercise())
    rows = journal.snapshot()['rows']
    sources = [r for r in rows if r['kind'] == 'source']
    assert len(sources) == 1 and sources[0]['decode_state'] == 'empty_decode_unknown'
    assert sources[0]['source_sig'] == FIX['transaction']['signatures'][0]
    assert sources[0]['connection_generation'] == 1
    assert any(r.get('reason') == 'metadata_offer_error' for r in rows) == metadata_failure


@pytest.mark.parametrize('venue', ['damm', 'dbc'])
def test_processed_routed_uses_existing_discovery_markers_only_when_mode_enabled(monkeypatch, venue):
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    from sniper.services.memecoin_shyft_signal import routed_buys
    from sniper.execution import meteora_damm_v2_buy as damm, meteora_dbc_buy as dbc
    module = damm if venue == 'damm' else dbc
    program = damm.PROGRAM if venue == 'damm' else dbc.PROGRAM
    mode = ['off']
    monkeypatch.setattr(module, 'lane_mode', lambda: mode[0])
    u = wire_receipt()
    u.transaction.transaction.meta.log_messages.extend([
        'Program ' + program + ' invoke [1]', 'Program ' + program + ' success'])
    args = dict(recv_monotonic=1, recv_utc='receipt', head_slot=FIX['slot'], head_mono=1)
    assert not routed_buys(u, {payer: ('uid', 'h')}, **args)[0].venue_events
    mode[0] = 'live'
    events = routed_buys(u, {payer: ('uid', 'h')}, **args)[0].venue_events
    assert len(events) == 1 and type(events[0]).__name__ == ('DammV2Seen' if venue == 'damm' else 'DbcSeen')


def test_processed_full_metadata_never_duplicates_direct_native_owner(monkeypatch):
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    from sniper.services import memecoin_shyft_signal as g
    from types import SimpleNamespace
    monkeypatch.setattr(g, 'decode_update', lambda *args, **kwargs: (SimpleNamespace(owner=payer, refusal=None),))
    assert not g.routed_buys(wire_receipt(), {payer: ('uid', 'h')}, recv_monotonic=1,
        recv_utc='receipt', head_slot=FIX['slot'], head_mono=1)


def test_old_pong_cannot_ack_a_replaced_roster(monkeypatch):
    from sniper.services import memecoin_shyft_signal as g
    import grpc
    watch = {OWNER: ('uid', 'h')}
    status, stop, snapshots = {}, threading.Event(), []
    other = 'D4pycXUXjs7FEWES39RMwfrs2E5LsbxGm5asCX6Tthqu'

    async def exercise():
        async def subscribe(incoming, context):
            async for req in incoming:
                if not req.HasField('ping'):
                    continue
                pong = g.pb.SubscribeUpdate(); pong.pong.id = 1
                yield pong
                if req.ping.id == 1:
                    watch[other] = ('other', 'other')
                    head = g.pb.SubscribeUpdate(); head.slot.slot = update().transaction.slot
                    yield head
                    yield update()
                else:
                    buy = update(); buy.transaction.slot += 1
                    head = g.pb.SubscribeUpdate(); head.slot.slot = buy.transaction.slot
                    yield head
                    yield buy
                    pong.pong.id = req.ping.id
                    yield pong
                    yield buy
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel',
                            lambda *a, **kw: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        def receive(buy):
            snapshots.append(dict(status)); stop.set()
        try:
            await asyncio.wait_for(g.run_feed('https://beta-grpc.ams.shyft.to', 'test-token',
                lambda: dict(watch), receive, status, stop), timeout=12)
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    assert snapshots[0]['requested_owners'] == tuple(sorted(watch))
    assert snapshots[0]['acknowledged_owners'] == tuple(sorted(watch))


@pytest.mark.parametrize('connected', [0, 1])
def test_shyft_readiness_uses_actual_current_roster_region_ack(monkeypatch, connected):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    stop, status = threading.Event(), {}
    async def forbidden(*a, **kw):
        raise AssertionError('legacy feed')
    async def feed(url, token, owners_fn, on_buy, row, stop, **kw):
        acknowledged = bool(connected and 'ams' in url)
        row.update(connected=acknowledged, requested_owners=(OWNER,),
                   acknowledged_owners=(OWNER,) if acknowledged else (), head_slot=20,
                   head_slot_mono=1)
        if 'fra' in url:
            stop.set()
        await asyncio.sleep(1)
    monkeypatch.setattr(s, '_run', forbidden)
    monkeypatch.setattr(g, 'run_feed', feed)
    asyncio.run(s._run_signals(lambda _: pytest.fail('unexpected buy'),
        lambda: {OWNER: ('uid', 'h')}, None, status, stop, None, None, None, None,
        ('secret', ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')),
        infra_provider='shyft'))
    assert status['connected'] is bool(connected)
    assert status['acknowledged_owners'] == ((OWNER,) if connected else ())
    assert status['head_slot'] == (20 if connected else None)


@pytest.mark.parametrize('prewarm', [False, True])
def test_shyft_start_from_context_uses_neutral_http_without_helius_and_keeps_routed_callback(monkeypatch, prewarm):
    from types import SimpleNamespace
    from sniper.services import memecoin_slot_lane as lane
    from sniper.services import memecoin_slot_signal as signal
    from sniper.execution import pump_curve_buy as curve, pumpswap_buy as amm
    from sniper.execution import raydium_cpmm_buy as cpmm, raydium_launchlab_buy as launch
    from sniper.execution import meteora_damm_v2_buy as damm, meteora_dbc_buy as dbc
    url = 'https://rpc.ams.shyft.to/?api_key=signal'
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'test')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS', 'https://grpc.ams.shyft.to,https://grpc.fra.shyft.to')
    monkeypatch.setenv('ONCHAIN_WATCH_SOLANA_RPC_URL', url)
    monkeypatch.delenv('ONCHAIN_WATCH_HELIUS_RPC_URL', raising=False)
    monkeypatch.setattr(lane, 'routed_mode', lambda: 'live')
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SLOT_LANE_MIGRATION_PREWARM', prewarm)
    monkeypatch.setattr(curve, 'SlotLaneCurveBuilder', lambda **kw: SimpleNamespace())
    monkeypatch.setattr(amm, 'SlotLanePumpSwapBuilder', lambda **kw: SimpleNamespace(rpc=None))
    monkeypatch.setattr(cpmm, 'lane_mode', lambda: 'live')
    monkeypatch.setattr(cpmm, 'SlotLaneCpmmBuilder', lambda **kw: SimpleNamespace(pools=SimpleNamespace(quote_usd=None)))
    monkeypatch.setattr(launch, 'lane_mode', lambda: 'off')
    monkeypatch.setattr(damm, 'lane_mode', lambda: 'off')
    monkeypatch.setattr(dbc, 'dbc_live', lambda: False)
    urls, called = [], []
    monkeypatch.setattr(launch, 'http_rpc', lambda selected, **kw: urls.append(selected) or object())
    monkeypatch.setattr(lane, 'token_accounts_baseline', lambda selected: urls.append(selected) or object())
    monkeypatch.setattr(signal, 'start_signal_thread', lambda *a, **kw: called.append(kw))
    monkeypatch.setattr(lane, 'start_slot_lane', lambda rc, **kw: kw)
    wiring = lane.start_from_context({'pubkey': OWNER}, log=lambda _: None)
    assert wiring['routed_baseline_fn'] is not None
    assert wiring['migration_on_signal'] is prewarm and wiring['migration_start_fn'] is None
    wiring['signal_start_fn'](lambda _: None, owners_fn=dict, status={})
    assert urls == [url, url]
    assert called[0]['ws_url'] is None and called[0]['infra_provider'] == 'shyft'
    assert called[0]['shyft_config'][1] == ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')


def migration_update():
    from sniper.services.memecoin_shyft_signal import pb
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT
    frame = json.loads((Path(__file__).parent / 'fixtures/memecoin/pump_migrate_frames.json').read_text())['wsol']
    u = pb.SubscribeUpdate(filters=['migrations'])
    u.transaction.slot = frame['slot']
    info = u.transaction.transaction
    info.signature = base58.b58decode(frame['signature'])
    info.transaction.signatures.append(info.signature)
    info.transaction.message.header.num_required_signatures = 1
    info.transaction.message.account_keys.extend(base58.b58decode(key)
        for key in (OWNER, PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT))
    info.meta.log_messages.extend(frame['logs'])
    return u


def test_migration_filter_is_explicit_program_and_migration_account_not_catchall():
    from sniper.services.memecoin_shyft_signal import subscription
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT
    req = subscription({}, migration=True, migration_label='migrations:c1:w1')
    assert list(req.transactions) == ['migrations:c1:w1']
    filt = req.transactions['migrations:c1:w1']
    assert set(filt.account_include) == {PUMP_PROGRAM, PUMP_AMM_PROGRAM}
    assert tuple(filt.account_required) == (PUMP_MIGRATION_ACCOUNT,)
    assert filt.vote is False and filt.failed is False and req.commitment == 0


@pytest.mark.parametrize('bad', [None, 'failed', 'signature', 'unrelated_program', 'wrong_keys', 'truncated'])
def test_only_successful_provenanced_pump_migration_can_offer_a_pool(bad):
    from sniper.services.memecoin_shyft_signal import migration_pool
    u = migration_update()
    info = u.transaction.transaction
    if bad == 'failed': info.meta.err.err = b'failed'
    if bad == 'signature': info.signature = b'wrong'
    if bad == 'wrong_keys': del info.transaction.message.account_keys[1:]
    if bad == 'unrelated_program':
        from sniper.services.memecoin_slot_signal import PUMP_AMM_PROGRAM
        info.meta.log_messages[:] = [line.replace(PUMP_AMM_PROGRAM, OWNER) for line in info.meta.log_messages]
    if bad == 'truncated': info.meta.ClearField('log_messages')
    hit = migration_pool(u)
    assert bool(hit) == (bad is None)
    if hit:
        assert hit == (base58.b58encode(info.signature).decode(),
                       'qNadZjDebjUwHS5ZRcLTLtoGk4wAU15kydQLFSJGUUe')


def test_two_processed_regions_offer_same_migration_once_without_receipt_authority(monkeypatch):
    from sniper.services import memecoin_slot_signal as signal, memecoin_shyft_signal as g
    pools, calls, status, stop = [], [], {}, threading.Event()
    async def feed(url, token, owners_fn, on_buy, feed_status, stop, *, on_migration=None, **kw):
        calls.append(url)
        on_migration('source-signature', 'canonical-pool')
        if len(calls) == 2: stop.set()
        await asyncio.sleep(1)
    monkeypatch.setattr(g, 'run_feed', feed)
    asyncio.run(signal._run_signals(lambda _: pytest.fail('migration became a buy'), dict, None,
        status, stop, None, None, None, None,
        ('test', ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')),
        infra_provider='shyft', on_migration=pools.append))
    assert pools == ['canonical-pool'] and len(calls) == 2


def test_migration_rejects_oversize_logs_before_any_log_parser(monkeypatch):
    from sniper.services.memecoin_shyft_signal import migration_pool
    from sniper.services import memecoin_opportunity_live as live
    u = migration_update()
    u.transaction.transaction.meta.log_messages.append('x' * 65537)
    monkeypatch.setattr(live, 'program_data_logs', lambda *_: pytest.fail('unbounded log parser'))
    assert migration_pool(u) is None


@pytest.mark.parametrize('kind', ['current', 'old_filter', 'wrong_pong'])
def test_migration_wire_requires_own_current_filter_ack_and_never_publishes_source(monkeypatch, kind):
    from sniper.services import memecoin_shyft_signal as g
    import grpc
    status, stop, offered, snapshots = {}, threading.Event(), [], []
    async def exercise():
        label = None
        async def subscribe(incoming, context):
            nonlocal label
            async for req in incoming:
                if req.transactions:
                    label = next(iter(req.transactions))
                if not req.HasField('ping'): continue
                pong = g.pb.SubscribeUpdate(); pong.pong.id = req.ping.id + (999 if kind == 'wrong_pong' else 0)
                yield pong
                row = migration_update()
                head = g.pb.SubscribeUpdate(); head.slot.slot = row.transaction.slot
                yield head
                del row.filters[:]
                row.filters.append('migrations:c0:r1' if kind == 'old_filter' else label)
                yield row
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel',
                            lambda *_a, **_k: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        def offer(signature, pool):
            offered.append((signature, pool)); snapshots.append(dict(status)); stop.set()
        try:
            call = g.run_feed('https://grpc.ams.shyft.to', 'test', dict,
                             lambda _: pytest.fail('migration acquired source authority'),
                             status, stop, on_migration=offer)
            if kind == 'current': await asyncio.wait_for(call, 3)
            else:
                with pytest.raises(TimeoutError): await asyncio.wait_for(call, .08)
        finally: await server.stop(None)
    asyncio.run(exercise())
    assert len(offered) == int(kind == 'current') and status['transactions'] == 0
    if snapshots:
        assert snapshots[0]['migration_filter_acknowledged'] is True
        assert snapshots[0]['migration_filter_generation'] == 'migrations:c1:r1'


def test_shyft_native_decoder_carries_feed_during_original_construction():
    from sniper.services.memecoin_shyft_signal import decode_update
    from test_memecoin_feed_arrivals import _journal
    evidence = _journal().identity(0, 4)
    buy, = decode_update(update(), {OWNER: ('u', 'h')}, recv_monotonic=0,
        recv_utc='original', head_slot=FRAMES['curve_buy']['params']['result']['context']['slot'], head_mono=0,
        receipt_evidence=evidence, signal_feed='shyft:beta-grpc.ams.shyft.to')
    assert buy.signal_feed == 'shyft:beta-grpc.ams.shyft.to'
    assert buy.receipt_evidence is evidence and buy.recv_monotonic == 0


@pytest.mark.parametrize('direct_states', [(), (None,), ('refused',), (None, 'refused'), ('refused', 'refused')])
def test_routed_native_decode_reuse_preserves_owner_exclusion_and_refusal(monkeypatch, direct_states):
    from dataclasses import replace
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    native, = decode(update())
    direct = tuple(replace(native, owner=payer, refusal=state) for state in direct_states)
    args = dict(recv_monotonic=0, recv_utc='original', head_slot=FIX['slot'], head_mono=0)
    watch = {payer: ('uid', 'original')}
    monkeypatch.setattr(g, 'decode_update', lambda *a, **kw: direct)
    expected = g.routed_buys(wire_receipt(), watch, **args)
    assert bool(expected) == (None not in direct_states)
    if expected:
        assert expected[0].requires_receipt_proof == bool(direct_states)
    def forbidden(*a, **kw):
        raise AssertionError('same update native ownership decoded twice')
    monkeypatch.setattr(g, 'decode_update', forbidden)
    assert g.routed_buys(wire_receipt(), watch, native_buys=direct, **args) == expected


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('retained', [False, True])
def test_real_shyft_source_reuses_empty_native_decode_and_frozen_roster(monkeypatch, enabled, retained):
    import grpc
    from sniper.services import memecoin_slot_signal as s, memecoin_shyft_signal as g
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER as payer, FIX
    journal = _journal(10) if enabled else None
    stop, status, received, decodes = threading.Event(), {}, [], []
    watch = {payer: ('uid', 'original')}
    real_native, real_routed = g.decode_update, g.routed_buys
    def native(update, owners, **receipt):
        decodes.append(update)
        assert len(decodes) == 1, 'same update native ownership decoded twice'
        return real_native(update, owners, **receipt)
    def routed(update, owners, **receipt):
        assert receipt['native_buys'] == ()  # decoded empty is not absent
        assert owners == {payer: ('uid', 'original')}
        if not retained:
            watch.clear()  # in-place roster change cannot rewrite receipt-time ownership
        return real_routed(update, owners, **receipt)
    async def idle(*a, **kw):
        await asyncio.Event().wait()
    class Stream:
        index = 0
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                await self.requests.__anext__()
                ping = await self.requests.__anext__()
                return g.pb.SubscribeUpdate(pong=g.pb.SubscribeUpdatePong(id=ping.ping.id))
            if self.index == 2:
                return g.pb.SubscribeUpdate(slot=g.pb.SubscribeUpdateSlot(slot=FIX['slot']))
            if self.index == 3:
                return wire_receipt()
            for _ in range(1000):
                worker = status['metadata_worker']
                if worker['decoded'] or worker['roster_refused'] or worker['errors']:
                    break
                await asyncio.sleep(.001)
            stop.set()
            await asyncio.Event().wait()
        def cancel(self): pass
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *a, **kw): return lambda requests, **kw: Stream(requests)
    def offer(buy):
        if enabled:
            assert journal.snapshot()['rows'] == ()
            assert buy.receipt_evidence['recv_monotonic'] == buy.recv_monotonic
        received.append(buy)
    monkeypatch.setattr(g, 'decode_update', native)
    monkeypatch.setattr(g, 'routed_buys', routed)
    monkeypatch.setattr(s, '_run', idle)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: Channel())
    asyncio.run(asyncio.wait_for(s._run_signals(lambda b: pytest.fail('native'), lambda: watch,
        'unused', status, stop, None, None, offer, None,
        ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal), 3))
    assert status['metadata_worker']['errors'] == 0
    assert len(decodes) == 1 and len(received) == int(retained)
    assert status['metadata_worker']['roster_refused'] == int(not retained)
    if enabled:
        rows = [r for r in journal.snapshot()['rows'] if r['kind'] == 'source']
        assert [r['decode_state'] for r in rows] == [
            'decoded_buy' if retained else 'roster_refused_unknown', 'empty_decode_unknown']
        assert rows[0]['recv_monotonic'] == rows[1]['recv_monotonic']
        assert rows[0]['source_sig'] == rows[1]['source_sig'] == FIX['transaction']['signatures'][0]
        assert rows[0]['connection_generation'] == rows[1]['connection_generation'] == 1


@pytest.mark.parametrize('fixture_key', ['65891', '65906'])
def test_shyft_regions_and_rotation_deliver_signer_bound_source_sell_once_after_buy(monkeypatch, fixture_key):
    import base64
    import grpc
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    fixtures = json.loads((Path(__file__).parent / 'fixtures/memecoin/source_sell_frames.json').read_text())
    source = fixtures[fixture_key]
    wire = update()
    # A real sell leg rotated by the same signer after this fixture's buy.
    for line in source['frame']['params']['result']['value']['logs']:
        if line.startswith('Program data: '):
            raw = base64.b64decode(line.split(': ', 1)[1])
            raw = raw.replace(base58.b58decode(source['owner']), base58.b58decode(OWNER))
            line = 'Program data: ' + base64.b64encode(raw).decode()
        wire.transaction.transaction.meta.log_messages.append(line)
    unsigned = copy.deepcopy(wire)
    unsigned.transaction.transaction.transaction.message.account_keys.insert(0, bytes(32))
    stop, status, delivered, connections = threading.Event(), {}, [], {}
    decoder = s.decode_owner_sells
    def after_buy(*args, **kwargs):
        assert delivered and delivered[0][0] == 'buy'
        return decoder(*args, **kwargs)
    monkeypatch.setattr(s, 'decode_owner_sells', after_buy)
    class Stream:
        def __init__(self, requests, target):
            self.requests, self.target, self.index = requests, target, 0
        def __aiter__(self): return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                await anext(self.requests)
                ping = await anext(self.requests)
                pong = g.pb.SubscribeUpdate(); pong.pong.id = ping.ping.id
                return pong
            if self.index == 2:
                head = g.pb.SubscribeUpdate(); head.slot.slot = wire.transaction.slot
                return head
            if self.index == 3:
                return unsigned  # watched actor is present but not a transaction signer
            if self.index == 4:
                return wire
            if self.target.startswith('grpc.ams.') and connections[self.target] >= 2:
                stop.set()
            raise StopAsyncIteration
        def cancel(self): pass
    class Channel:
        def __init__(self, target): self.target = target
        async def __aenter__(self):
            connections[self.target] = connections.get(self.target, 0) + 1
            return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs):
            return lambda requests, **kwargs: Stream(requests, self.target)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda target, *a, **kw: Channel(target))
    async def exercise():
        await asyncio.wait_for(s._run_signals(
            lambda buy: delivered.append(('buy', buy)), lambda: {OWNER: ('uid', 'buyer')},
            None, status, stop, None,
            lambda rows: delivered.extend(('sell', row) for row in rows), None, None,
            ('test-token', ('https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')),
            infra_provider='shyft'), timeout=3)
    asyncio.run(exercise())
    assert [kind for kind, _ in delivered] == ['buy', 'sell']
    sell = delivered[1][1]
    assert sell.owner == OWNER and sell.signature == delivered[0][1].signature
    assert sell.source_user_id == 'uid'
    assert sell.venue == ('pump_curve' if fixture_key == '65891' else 'pump_amm')
    assert connections['grpc.ams.shyft.to:443'] == 2
    assert status['signal_feeds']['shyft:grpc.ams.shyft.to']['connection_generation'] == 2
