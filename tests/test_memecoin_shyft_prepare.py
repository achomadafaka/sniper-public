"""Rabbit packets prepare static read targets; they never grant entry authority."""
import asyncio
import json
from pathlib import Path
import threading

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.execution import pumpswap_buy as amm

OWNER = base58.b58encode(bytes([7]) * 32).decode()
MINT = base58.b58encode(bytes([8]) * 32).decode()
POOL = base58.b58encode(bytes([9]) * 32).decode()


def packet(*, meta=False, slot=100, signature=1):
    update = pb.SubscribeUpdate()
    update.transaction.slot = slot
    info = update.transaction.transaction
    info.signature = bytes([signature]) * 64
    info.transaction.signatures.append(info.signature)
    message = info.transaction.message
    message.header.num_required_signatures = 1
    message.recent_blockhash = bytes([3]) * 32
    # The PumpSwap exact quote-in layout is already used by our native builder.
    keys = [OWNER, amm.AMM_PROGRAM, POOL, MINT]
    keys += [base58.b58encode(bytes([n]) * 32).decode() for n in range(10, 32)]
    message.account_keys.extend(base58.b58decode(k) for k in keys)
    ix = message.instructions.add()
    ix.program_id_index = 1
    ix.accounts = bytes([2, 0, 4, 3] + list(range(5, 26)))
    ix.data = amm.BUY_EXACT_QUOTE_IN_DISCRIMINATOR + bytes(17)
    if meta:
        info.meta.log_messages.append('metadata is present')
    return update


def candidate(update=None, *, mono=0, generation=1, owners=None):
    from sniper.services.memecoin_shyft_prepare import preparation_candidate
    return preparation_candidate(update or packet(), owners or {OWNER: ('uid', 'handle')},
        recv_monotonic=mono, generation=generation)


def test_no_metadata_decodes_only_structure_and_cannot_enter(monkeypatch):
    from sniper.services.memecoin_shyft_prepare import preparation_target
    from sniper.services.memecoin_shyft_signal import decode_update
    seen = []
    for module, name in (('sniper.services.memecoin_risk', 'reserve_entry'),
                         ('sniper.execution.solana_real_seams', 'sign_swap_tx'),
                         ('sniper.execution.submission_wiring', 'make_solana_send_fn')):
        monkeypatch.setattr(module + '.' + name, lambda *a, **k: seen.append(name))
    u = packet()
    got = candidate(u)
    assert set(got) == {'signature', 'owner', 'slot', 'recv_monotonic', 'generation',
                       'account_keys', 'instructions',
                       'version', 'signers', 'writable'}   # static structure (the decoder seam, 2026-10-17)
    assert (got['owner'], got['slot'], got['recv_monotonic']) == (OWNER, 100, 0)
    assert preparation_target(got) == {'venue': 'pump_amm', 'mint': MINT, 'pool': POOL}
    assert decode_update(u, {OWNER: ('uid', 'handle')}, recv_monotonic=0, recv_utc='',
        head_slot=100, head_mono=0) == ()
    assert not seen


@pytest.mark.parametrize('bad', ['no_tx', 'signature', 'signature_mismatch', 'nonsigner', 'header',
    'readonly_header', 'key', 'duplicate_key', 'index', 'alt', 'alt_key', 'config_unversioned', 'unknown_buy',
    'ambiguous_route', 'wrong_actor', 'vote', 'failed', 'slot'])
def test_bad_or_ambiguous_packet_is_a_preparation_miss(bad):
    u = packet()
    info = u.transaction.transaction
    msg = info.transaction.message
    if bad == 'no_tx': u.ClearField('transaction')
    if bad == 'signature': info.signature = b'bad'
    if bad == 'signature_mismatch': info.transaction.signatures[0] = bytes(64)
    if bad == 'nonsigner': msg.account_keys[0], msg.account_keys[4] = msg.account_keys[4], msg.account_keys[0]
    if bad == 'header': msg.ClearField('header')
    if bad == 'readonly_header': msg.header.num_readonly_unsigned_accounts = 255
    if bad == 'key': msg.account_keys[3] = b'bad'
    if bad == 'duplicate_key': msg.account_keys[3] = msg.account_keys[4]
    if bad == 'index': msg.instructions[0].program_id_index = 255
    if bad in ('alt', 'alt_key'):
        msg.versioned = True
        msg.address_table_lookups.add(account_key=bytes(32) if bad == 'alt' else b'bad',
                                     writable_indexes=b'\x00')
    if bad == 'config_unversioned': msg.config.compute_unit_limit = 200000
    if bad == 'unknown_buy': msg.instructions[0].data = bytes(25)
    if bad == 'ambiguous_route': msg.instructions.add().CopyFrom(msg.instructions[0])
    if bad == 'wrong_actor':
        ix = msg.instructions[0]; ix.accounts = bytes([2, 4]) + ix.accounts[2:]
    if bad == 'vote': info.is_vote = True
    if bad == 'failed': info.meta.err.err = b'failed'
    if bad == 'slot': u.transaction.slot = 0
    assert candidate(u) is None


def test_v1_direct_packet_prepares_without_using_transaction_config():
    from sniper.services.memecoin_shyft_prepare import preparation_target
    u = packet()
    msg = u.transaction.transaction.transaction.message
    msg.versioned = True
    msg.config.compute_unit_limit = 243606
    msg.config.priority_fee = 158516
    got = candidate(u)
    assert got is not None
    assert preparation_target(got) == {'venue': 'pump_amm', 'mint': MINT, 'pool': POOL}
    assert not {'config', 'cu_limit', 'priority_fee', 'clip', 'source_price', 'min_out'} & set(got)


def worm_packet():
    source = json.loads((Path(__file__).parent / 'fixtures/memecoin/worm-source-5ezd1jvs.json').read_text())
    update = pb.SubscribeUpdate()
    update.transaction.slot = source['slot']
    info = update.transaction.transaction
    info.signature = base58.b58decode(source['transaction']['signatures'][0])
    info.transaction.signatures.extend(base58.b58decode(s) for s in source['transaction']['signatures'])
    msg = info.transaction.message
    raw = source['transaction']['message']
    msg.header.num_required_signatures = raw['header']['numRequiredSignatures']
    msg.header.num_readonly_signed_accounts = raw['header']['numReadonlySignedAccounts']
    msg.header.num_readonly_unsigned_accounts = raw['header']['numReadonlyUnsignedAccounts']
    msg.account_keys.extend(base58.b58decode(k) for k in raw['accountKeys'])
    msg.recent_blockhash = base58.b58decode(raw['recentBlockhash'])
    msg.versioned = True
    msg.config.compute_unit_limit = raw['transactionConfig']['computeUnitLimit']
    msg.config.priority_fee = raw['transactionConfig']['priorityFee']
    msg.config.loaded_accounts_data_size_limit = raw['transactionConfig']['loadedAccountsDataSizeLimit']
    for ix in raw['instructions']:
        msg.instructions.add(program_id_index=ix['programIdIndex'],
            accounts=bytes(ix['accounts']), data=base58.b58decode(ix['data']))
    return update


WORM_OWNER = 'F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv'
WORM_TARGET = {'venue': 'pump_amm', 'mint': '5JRgtF4Fwnv3eNmNPuAur6dEzh83ix6mtYP4XALupump',
               'pool': '763jR65r5m9F25FAFK22n4UPQya6aLsK1uounP4jJxR'}


def test_worm_flash_v1_prepares_one_canonical_pool_without_execution_authority(monkeypatch):
    from sniper.services.memecoin_shyft_prepare import preparation_target, drain_preparations
    from sniper.execution.pumpswap_buy import PumpSwapPoolCache
    from types import SimpleNamespace
    import queue
    def forbidden(*a, **kw):
        pytest.fail('static preparation reached execution or a synchronous RPC')
    for module, name in (('sniper.services.memecoin_risk', 'reserve_entry'),
                         ('sniper.execution.solana_real_seams', 'sign_swap_tx'),
                         ('sniper.execution.submission_wiring', 'make_solana_send_fn')):
        monkeypatch.setattr(module + '.' + name, forbidden)
    got = candidate(worm_packet(), owners={WORM_OWNER: ('uid', 'handle')})
    assert got is not None
    assert preparation_target(got) == WORM_TARGET
    assert not {'source_initiation', 'clip', 'source_price', 'min_out', 'config', 'cu_limit'} & set(got)
    cache = PumpSwapPoolCache(rpc=forbidden)
    pending, status = queue.Queue(maxsize=128), {}
    pending.put_nowait(got)
    drain_preparations(pending, {'pump_amm': SimpleNamespace(pools=cache)}, status, now=0)
    assert cache.cached(WORM_TARGET['pool']) is None
    assert list(cache._stream) == [WORM_TARGET['pool']]
    assert status['delivered'] == 1


@pytest.mark.parametrize('bad', ['actor_1', 'actor_8', 'actor_20', 'amm_18', 'amm_35',
    'quote', 'base_program', 'quote_program', 'system_program', 'router_program',
    'short_accounts', 'short_data', 'opcode', 'route_tail', 'ambiguous', 'unsigned', 'failed'])
def test_worm_flash_unknown_or_unbound_shape_never_prepares(bad):
    u = worm_packet()
    info = u.transaction.transaction
    msg = info.transaction.message
    ix = msg.instructions[2]
    accounts = bytearray(ix.accounts)
    if bad.startswith('actor_'): accounts[int(bad.split('_')[1])] = 13
    if bad.startswith('amm_'): accounts[int(bad.split('_')[1])] = 23
    if bad == 'quote': accounts[23] = 22
    if bad == 'base_program': accounts[28] = 15
    if bad == 'quote_program': accounts[29] = 30
    if bad == 'system_program': accounts[30] = 29
    ix.accounts = bytes(accounts)
    if bad == 'router_program': ix.program_id_index = 15
    if bad == 'short_accounts': ix.accounts = ix.accounts[:-1]
    if bad == 'short_data': ix.data = ix.data[:-1]
    if bad == 'opcode': ix.data = b'\x01' + ix.data[1:]
    if bad == 'route_tail': ix.data = ix.data[:-1] + b'\x01'
    if bad == 'ambiguous': msg.instructions.add().CopyFrom(ix)
    if bad == 'unsigned': msg.header.num_required_signatures = 0
    if bad == 'failed': info.meta.err.err = b'failed'
    assert candidate(u, owners={WORM_OWNER: ('uid', 'handle')}) is None


def test_complete_resolved_alt_vector_is_structural_only():
    u = packet()
    msg = u.transaction.transaction.transaction.message
    msg.versioned = True
    msg.address_table_lookups.add(account_key=bytes([33]) * 32, writable_indexes=b'\x00')
    u.transaction.transaction.meta.loaded_writable_addresses.append(bytes([34]) * 32)
    assert candidate(u)['account_keys'][-1] == base58.b58encode(bytes([34]) * 32).decode()


def test_transaction_only_subscription_never_asserts_success_or_requests_replay():
    from sniper.services.memecoin_shyft_prepare import subscription
    req = subscription({OWNER: ('u', 'h')})
    assert tuple(req.transactions['owners'].account_include) == (OWNER,)
    assert not req.transactions['owners'].HasField('failed')
    assert not req.slots and not req.accounts and not req.blocks and not req.blocks_meta
    assert not req.accounts_data_slice and not req.HasField('from_slot')
    assert not subscription({}).transactions  # empty account_include would subscribe to all


@pytest.mark.parametrize('url', ['http://rabbitstream.ams.shyft.to', 'https://rabbitstream.ny.shyft.to',
    'https://rabbitstream.ams.shyft.to/path', 'https://secret@rabbitstream.ams.shyft.to',
    'https://rabbitstream.ams.shyft.to/?key=secret', 'https://foreign.test'])
def test_feed_rejects_non_europe_or_secret_bearing_endpoints(url):
    from sniper.services.memecoin_shyft_prepare import prepare_config
    with pytest.raises(ValueError) as exc:
        prepare_config({'MEMECOIN_SHYFT_PREPARE_ENABLED': '1',
            'MEMECOIN_SHYFT_PREPARE_GRPC_URL': url, 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret'})
    assert 'secret' not in str(exc.value)


def test_optional_feed_config_requires_explicit_entitled_single_endpoint():
    from sniper.services.memecoin_shyft_prepare import prepare_config
    assert prepare_config({}) is None
    env = {'MEMECOIN_SHYFT_PREPARE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'secret'}
    with pytest.raises(ValueError, match='endpoint_invalid'): prepare_config(env)
    env['MEMECOIN_SHYFT_PREPARE_GRPC_URL'] = 'https://rabbitstream.ams.shyft.to'
    assert prepare_config(env) == ('secret', env['MEMECOIN_SHYFT_PREPARE_GRPC_URL'])


def test_pending_exact_join_dedup_retains_first_receipt_and_final_values_stay_local():
    from sniper.services.memecoin_shyft_prepare import PendingPreparation
    bank = PendingPreparation()
    first = candidate(mono=1)
    assert bank.offer(first, now=1)
    assert not bank.offer(candidate(mono=1.1, generation=2), now=1.1)
    authority = candidate(packet(meta=True), mono=1.2, generation=99)
    got = bank.take(authority, now=1.2)
    assert got == first and got['recv_monotonic'] == 1
    assert not bank.take(authority, now=1.3)
    assert not bank.offer(candidate(mono=1.4), now=1.4)
    assert not {'clip', 'source_price', 'min_out', 'blockhash'} & set(got)
    assert bank.stats['duplicate'] == 2 and bank.stats['matched'] == 1


@pytest.mark.parametrize('field', ['signature', 'owner', 'slot', 'account_keys', 'instructions'])
def test_pending_wrong_exact_identity_never_matches(field):
    from sniper.services.memecoin_shyft_prepare import PendingPreparation
    bank = PendingPreparation(); first = candidate(mono=1); bank.offer(first, now=1)
    changed = dict(first)
    changed[field] = 'wrong'
    assert bank.take(changed, now=1.1) is None


def test_processed_first_late_rabbit_no_followup_and_expiry_are_bounded():
    from sniper.services.memecoin_shyft_prepare import PendingPreparation
    bank = PendingPreparation(); first = candidate(mono=1)
    assert bank.take(first, now=1) is None
    assert not bank.offer(candidate(mono=1.1), now=1.1)
    bank = PendingPreparation(); bank.offer(first, now=1)
    assert bank.take(candidate(packet(meta=True), mono=3), now=3) is None
    assert bank.stats['expired'] == 1
    bank = PendingPreparation()
    for n in range(1, 130):
        assert bank.offer(candidate(packet(signature=n), mono=1), now=1)
    assert len(bank) == 128 and bank.stats['overflow'] == 1
    bank.expire(now=3)
    assert len(bank) == 0 and bank.stats['expired'] == 128


def test_authenticated_feed_has_one_connection_and_only_enqueues_structure(monkeypatch):
    import grpc
    from sniper.services import memecoin_shyft_prepare as prepare
    stop, status, received, requests, auth, targets = threading.Event(), {}, [], [], [], []
    async def exercise():
        async def subscribe(incoming, context):
            auth.extend(context.invocation_metadata())
            async for req in incoming:
                requests.append(req)
                if req.HasField('ping'):
                    # Before the roster ack, even a real-looking intent is ignored.
                    yield packet()
                    pong = pb.SubscribeUpdate(); pong.pong.id = req.ping.id; yield pong
                    yield packet(); yield packet()
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=pb.SubscribeRequest.FromString,
                response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        def channel(target, *a, **kw):
            targets.append(target); return grpc.aio.insecure_channel(f'127.0.0.1:{port}')
        monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
        await server.start()
        def receive(row): received.append(row); stop.set()
        try:
            await asyncio.wait_for(prepare.run_prepare_feed('https://rabbitstream.ams.shyft.to',
                'test-token', lambda: {OWNER: ('uid', 'h')}, receive, status, stop), timeout=3)
        finally: await server.stop(0)
    asyncio.run(exercise())
    assert len(received) == 1 and targets == ['rabbitstream.ams.shyft.to:443']
    assert ('x-token', 'test-token') in auth
    assert status['before_ack'] == 1 and status['decoded'] == 1
    assert not any(req.slots or req.accounts or req.blocks for req in requests)


def test_authoritative_failure_never_matches_or_revives_unsigned_intent():
    from sniper.services.memecoin_shyft_prepare import PendingPreparation
    bank = PendingPreparation(); first = candidate(mono=1); bank.offer(first, now=1)
    u = packet(meta=True); u.transaction.transaction.meta.err.err = b'failed'
    assert candidate(u, mono=1.1) is None
    # No callback may turn the absent full candidate into a take/permission.
    bank.expire(now=3)
    assert len(bank) == 0 and bank.stats['matched'] == 0


def test_all_supported_direct_layouts_use_actual_account_positions():
    from sniper.services.memecoin_shyft_prepare import preparation_target
    from sniper.execution import pump_curve_buy as curve, raydium_launchlab_buy as launch
    from sniper.execution import raydium_cpmm_buy as cpmm
    for program, discriminator, length, count, actor, mint_index, pool_index, venue in (
        (curve.PUMP_PROGRAM, curve.BUY_EXACT_SOL_IN_DISCRIMINATOR, 25, 18, 6, 2, 3, 'pump_curve'),
        (curve.PUMP_PROGRAM, curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR, 24, 27, 13, 1, 10, 'pump_curve'),
        (launch.LAUNCHLAB_PROGRAM, launch.BUY_EXACT_IN_DISCRIMINATOR, 32, 18, 0, 9, 4, 'raydium_launchlab'),
        (cpmm.CPMM_PROGRAM, cpmm.SWAP_BASE_INPUT_DISCRIMINATOR, 24, 13, 0, 11, 3, 'raydium_cpmm')):
        accounts = [base58.b58encode(bytes([n + 50]) * 32).decode() for n in range(count)]
        accounts[actor], accounts[mint_index], accounts[pool_index] = OWNER, MINT, POOL
        row = dict(owner=OWNER, instructions=((program, tuple(accounts),
            discriminator + bytes(length - 8)),))
        assert preparation_target(row) == dict(venue=venue, mint=MINT, pool=POOL)


def test_reconnect_cancels_previous_stream_and_masks_auth_errors(monkeypatch):
    from sniper.services import memecoin_shyft_prepare as prepare
    import grpc
    stop, status, events, received = threading.Event(), {}, [], []
    class Stream:
        def __init__(self, requests, attempt): self.requests, self.attempt = requests, attempt; self.step = 0
        def __aiter__(self): return self
        async def __anext__(self):
            if self.attempt == 1: raise RuntimeError('auth failure test-token')
            if self.step == 0:
                await self.requests.__anext__(); ping = await self.requests.__anext__()
                self.step += 1
                u = pb.SubscribeUpdate(); u.pong.id = ping.ping.id; return u
            return packet()
        def cancel(self): events.append(('cancel', self.attempt))
    class Channel:
        def __init__(self, attempt): self.attempt = attempt
        async def __aenter__(self): events.append(('open', self.attempt)); return self
        async def __aexit__(self, *args): events.append(('close', self.attempt))
        def stream_stream(self, *args, **kwargs):
            return lambda requests, **kw: Stream(requests, self.attempt)
    attempts = []
    def channel(*args, **kwargs):
        attempts.append(1); return Channel(len(attempts))
    monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
    def receive(row): received.append(row); stop.set()
    asyncio.run(asyncio.wait_for(prepare.run_prepare_feed('https://rabbitstream.fra.shyft.to',
        'test-token', lambda: {OWNER: ('uid', 'h')}, receive, status, stop), timeout=3))
    assert len(received) == 1 and status['reconnects'] == 1
    assert events.index(('close', 1)) < events.index(('open', 2))
    assert events.index(('cancel', 1)) < events.index(('open', 2))
    assert 'test-token' not in str(status)


def test_existing_prefetch_drain_is_bounded_paced_and_preserves_original_age():
    import queue
    from types import SimpleNamespace
    from sniper.services.memecoin_shyft_prepare import drain_preparations
    q = queue.Queue(maxsize=128); calls, stats = [], {}
    class PoolCache:
        def offer_stream(self, pool): calls.append(pool)
    builders = {'pump_amm': SimpleNamespace(pools=PoolCache())}
    for n in range(1, 129): q.put_nowait(candidate(packet(signature=n), mono=1))
    with pytest.raises(queue.Full): q.put_nowait(candidate())
    drain_preparations(q, builders, stats, now=1.1)
    assert calls == [POOL, POOL] and stats['paced_drop'] == 126 and stats['delivered'] == 2
    q.put_nowait(candidate(mono=1)); drain_preparations(q, builders, stats, now=3)
    assert calls == [POOL, POOL] and stats['expired'] == 1


def test_prepare_fifo_reads_existing_native_pool_caches_only():
    import queue
    from types import SimpleNamespace
    from sniper.services.memecoin_shyft_prepare import drain_preparations
    from sniper.execution import raydium_cpmm_buy as cpmm
    calls, stats, q = [], {}, queue.Queue(maxsize=128)
    class Cache:
        def get(self, pool): calls.append(pool)  # existing get only queues a cold read
    accounts = [OWNER] + [base58.b58encode(bytes([n + 50]) * 32).decode() for n in range(12)]
    accounts[3], accounts[11] = POOL, MINT
    row = dict(candidate(), instructions=((cpmm.CPMM_PROGRAM, tuple(accounts),
        cpmm.SWAP_BASE_INPUT_DISCRIMINATOR + bytes(16)),))
    q.put_nowait(row)
    drain_preparations(q, {'raydium_cpmm': SimpleNamespace(pools=Cache())}, stats, now=0)
    assert calls == [POOL] and stats['delivered'] == 1
    row = dict(row, instructions=())
    q.put_nowait(row); drain_preparations(q, {}, stats, now=0)
    assert stats['unsupported'] == 1


def test_signal_coordinator_rabbit_first_join_runs_without_granting_extra_authority(monkeypatch):
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_shyft_prepare as prepare
    stop, status, prepared, authoritative = threading.Event(), {}, [], []
    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop):
        on_prepare(candidate(mono=signal.time.monotonic()))
        on_prepare(candidate(mono=signal.time.monotonic(), generation=2))
        while not stop.is_set(): await asyncio.sleep(.01)
    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None, **kwargs):
        await asyncio.sleep(.01)
        on_metadata(packet(meta=True), recv_monotonic=signal.time.monotonic(), recv_utc='',
                    head_slot=100, head_mono=signal.time.monotonic(),
                    receipt_owners={OWNER: ('uid', 'h')})
        await asyncio.sleep(.02); stop.set()
    monkeypatch.setattr(prepare, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(authoritative.append, lambda: {OWNER: ('uid', 'h')},
        None, status, stop, None, None, None, None, ('token', ('https://grpc.ams.shyft.to',)),
        infra_provider='shyft', prepare_config=('token', 'https://rabbitstream.ams.shyft.to'),
        on_prepare=prepared.append))
    assert len(prepared) == 1 and authoritative == []
    assert status['preparation']['pending']['matched'] == 1
    assert status['preparation']['pending']['duplicate'] == 1


def test_failed_full_metadata_discards_prep_and_cannot_call_entry(monkeypatch):
    from sniper.services.memecoin_shyft_prepare import PendingPreparation
    bank = PendingPreparation(); first = candidate(); bank.offer(first, now=0)
    bank.discard_signature(first['signature'], now=.1)
    assert bank.take(candidate(packet(meta=True), mono=.2), now=.2) is None
    assert not bank.offer(candidate(mono=.3), now=.3)
    assert bank.stats['failed'] == 1


def test_preparation_heartbeat_carries_typed_gap_counters_without_credentials(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    lane = {'preparation': dict(capacity=128, queued=9, delivered=2, overflow=1, paced_drop=6,
                expired=3, token='secret', url='https://secret@foreign.test'),
            'signal': {'preparation': {'pending': dict(capacity=128, matched=2, late=1, failed=1),
                'feed': dict(endpoint_hostname='rabbitstream.ams.shyft.to', connected=True,
                    requested_owners=(OWNER,), acknowledged_owners=(OWNER,), transactions=9,
                    decoded=8, misses=1, before_ack=0, token='secret', url='secret'),
                'callback_errors': 0}}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane)
    raw = path.read_text(); row = json.loads(raw)['slot_lane']['preparation']
    assert row['queue']['overflow'] == 1 and row['queue']['paced_drop'] == 6
    assert row['pending']['matched'] == 2 and row['pending']['failed'] == 1
    assert row['feed']['endpoint_hostname'] == 'rabbitstream.ams.shyft.to'
    assert row['feed']['connected'] and row['feed']['requested_owners'] == [OWNER]
    assert 'secret' not in raw and 'foreign.test' not in raw


# Reuse Task8's real isolated reservation/signing/durable-identity fixture.
from test_solana_same_slot_ready_path import ready  # noqa: E402,F401


def source_curve_packet(*, meta=False):
    from test_solana_same_slot_ready_path import EVENT, SOURCE
    from sniper.execution import pump_curve_buy as curve
    u = pb.SubscribeUpdate(); u.transaction.slot = SOURCE['slot']
    info = u.transaction.transaction; info.signature = base58.b58decode(SOURCE['signature'])
    info.transaction.signatures.append(info.signature)
    msg = info.transaction.message; msg.header.num_required_signatures = 1
    msg.recent_blockhash = bytes([3]) * 32
    account_metas = curve.curve_buy_accounts(user=EVENT['user'], mint=EVENT['mint'],
        token_program=curve.TOKEN_PROGRAM, creator=EVENT['creator'],
        fee_recipient=EVENT['fee_recipient'], buyback_fee_recipient=EVENT['fee_recipient'])
    keys = list(dict.fromkeys([EVENT['user'], curve.PUMP_PROGRAM] + [str(a[0]) for a in account_metas]))
    msg.account_keys.extend(base58.b58decode(key) for key in keys)
    msg.instructions.add(program_id_index=keys.index(curve.PUMP_PROGRAM),
        accounts=bytes(keys.index(str(a[0])) for a in account_metas),
        data=curve.BUY_EXACT_SOL_IN_DISCRIMINATOR + bytes(17))
    if meta: info.meta.log_messages.append('full metadata present')
    return u


def test_rabbit_structure_cannot_override_current_clip_floor_or_signed_payload(ready, monkeypatch):
    import time
    from sniper.services import memecoin_slot_lane as lane
    from sniper.services.memecoin_shyft_prepare import PendingPreparation, preparation_candidate
    from test_solana_same_slot_ready_path import EVENT, _dispatch
    bank = PendingPreparation(); now = time.monotonic(); owners = {EVENT['user']: ('w1', 'fixture')}
    before = preparation_candidate(source_curve_packet(), owners, recv_monotonic=now - 1, generation=1)
    assert bank.offer(before, now=now)
    full = preparation_candidate(source_curve_packet(meta=True), owners, recv_monotonic=now, generation=2)
    assert bank.take(full, now=now) == before
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_TRADE_SIZE_USD', 20.0)
    result = _dispatch(ready)
    from sniper.persistence import db_memecoin as db
    pending = db.pending_row(ready['conn'], result['pending_id'])
    assert pending['reserved_notional_usd'] == 20.0
    assert not {'clip', 'source_price', 'min_out', 'blockhash', 'signed_payload'} & set(before)


def test_two_second_preparation_cannot_extend_original_source_500ms(ready):
    import time
    from sniper.services.memecoin_shyft_prepare import PendingPreparation, preparation_candidate
    from test_solana_same_slot_ready_path import EVENT
    now = time.monotonic(); owners = {EVENT['user']: ('w1', 'fixture')}; bank = PendingPreparation()
    before = preparation_candidate(source_curve_packet(), owners, recv_monotonic=now - 1, generation=1)
    assert bank.offer(before, now=now)
    full = preparation_candidate(source_curve_packet(meta=True), owners, recv_monotonic=now - .6, generation=2)
    assert bank.take(full, now=now)
    result = ready['lane'].handle(ready['buy'](recv_monotonic=now - .6))
    assert result['status'] == 'rejected' and result['reason'] == 'slot_lane_signal_too_old' and ready['sent'] == [] and ready['network'] == []
    assert ready['conn'].execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0] == 0


@pytest.mark.parametrize('dynamic,timely', [(False, False), (True, False), (True, True)])
def test_continuous_frames_cannot_admit_late_startup_or_changed_roster_ack(monkeypatch, dynamic, timely):
    """Captured review witness: available frames must not hide an expired ACK deadline."""
    from types import SimpleNamespace
    import grpc
    from sniper.services import memecoin_shyft_prepare as prepare
    now, stop, status, received = [0.0], threading.Event(), {}, []
    owners = {OWNER: ('uid', 'h')}
    monkeypatch.setattr(prepare, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def no_wait(_): await asyncio.sleep(0)
    monkeypatch.setattr(prepare, 'asyncio', SimpleNamespace(wait_for=asyncio.wait_for,
        sleep=no_wait, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(prepare, 'PING_S', 0.0)       # the ping wait was the no_wait sleep
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            if dynamic:
                now[0] = 1.0
                pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
                now[0] = 20.0; yield packet()  # healthy ACK stays usable past startup deadline
                owners[base58.b58encode(bytes([44]) * 32).decode()] = ('other', 'other')
                now[0] = 21.0
                changed = await self.requests.__anext__()
                assert len(changed.transactions['owners'].account_include) == 2
                ping = await self.requests.__anext__()
            start = now[0]
            for elapsed in range(1, 5 if timely else 12):
                now[0] = start + elapsed; yield packet()
            pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            yield packet()
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs): return lambda requests, **kwargs: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def receive(row):
        received.append(row)
        if len(received) > int(dynamic): stop.set()
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.ams.shyft.to', 'test-token',
        lambda: dict(owners), receive, status, stop))
    assert len(received) == int(dynamic) + int(timely), 'ACK deadline was reset by traffic or not reset by changed roster'
    assert status['reconnects'] == int(not timely)
    assert status['last_error'] == (None if timely else 'TimeoutError')
    assert status['acknowledged_owners'] == ()


def test_matching_pong_rechecks_deadline_after_owner_snapshot(monkeypatch):
    from types import SimpleNamespace
    import grpc
    from sniper.services import memecoin_shyft_prepare as prepare
    now, delayed, stop, status, received = [0.0], [False], threading.Event(), {}, []
    monkeypatch.setattr(prepare, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def owners():
        if delayed[0]: now[0], delayed[0] = 11.0, False
        return {OWNER: ('uid', 'h')}
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            now[0], delayed[0] = 9.0, True
            pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            yield packet()
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs): return lambda requests, **kwargs: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def receive(row): received.append(row); stop.set()
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.ams.shyft.to', 'test-token',
        owners, receive, status, stop))
    assert not received and status['reconnects'] == 1 and status['last_error'] == 'TimeoutError'


@pytest.mark.parametrize('aba,timely', [(False, True), (False, False), (True, True)])
def test_receiver_first_roster_epoch_is_bounded_and_old_pong_cannot_ack_it(monkeypatch, aba, timely):
    from types import SimpleNamespace
    import grpc
    from sniper.services import memecoin_shyft_prepare as prepare
    now, stop, status, received, changed = [0.0], threading.Event(), {}, [], []
    owners = {OWNER: ('uid', 'h')}; other = base58.b58encode(bytes([44]) * 32).decode()
    monkeypatch.setattr(prepare, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def no_wait(_): await asyncio.sleep(0)
    monkeypatch.setattr(prepare, 'asyncio', SimpleNamespace(wait_for=asyncio.wait_for,
        sleep=no_wait, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(prepare, 'PING_S', 0.0)       # the ping wait was the no_wait sleep
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            now[0] = 1; pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            now[0] = 20; yield packet()
            old_ping = await self.requests.__anext__()
            assert old_ping.HasField('ping')
            owners[other] = ('other', 'other'); now[0] = 21; yield packet()
            if aba:
                del owners[other]; now[0] = 23; yield packet()
            now[0] = 24
            old_pong = pb.SubscribeUpdate(); old_pong.pong.id = old_ping.ping.id; yield old_pong
            yield packet()  # still unacknowledged, even if current roster returned to the old set
            assert len(received) == 1
            now[0] = 28
            request = await self.requests.__anext__(); changed.append(request)
            assert request.transactions and not request.HasField('ping')
            current_ping = await self.requests.__anext__()
            now[0] = 29 if timely else 32
            pong = pb.SubscribeUpdate(); pong.pong.id = current_ping.ping.id; yield pong
            yield packet()
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs): return lambda requests, **kwargs: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def receive(row):
        received.append(row)
        if len(received) == 2: stop.set()
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.ams.shyft.to', 'test-token',
        lambda: dict(owners), receive, status, stop))
    assert changed, 'receiver-first change must get a subscription opportunity before timeout'
    assert len(received) == 1 + int(timely)
    assert status['reconnects'] == int(not timely)
    assert status['last_error'] == (None if timely else 'TimeoutError')


def test_roster_snapshot_does_not_renew_original_preparation_receipt(monkeypatch):
    from types import SimpleNamespace
    import grpc
    from sniper.services import memecoin_shyft_prepare as prepare
    now, delayed, stop, status, received = [0.0], [False], threading.Event(), {}, []
    monkeypatch.setattr(prepare, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def owners():
        if delayed[0]: now[0], delayed[0] = 6.0, False
        return {OWNER: ('uid', 'h')}
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            now[0] = 1; pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            now[0], delayed[0] = 5.0, True; yield packet()
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs): return lambda requests, **kwargs: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def receive(row): received.append(row); stop.set()
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.ams.shyft.to', 'test-token',
        owners, receive, status, stop))
    assert received[0]['recv_monotonic'] == 5 and status['last_frame_mono'] == 5
