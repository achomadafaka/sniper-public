"""Resolve signed preconf address tables off-path; cache hits keep the original receipt."""
import asyncio
import base64
import struct
import threading
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature

from sniper.services import memecoin_preconf_signal as preconf
from sniper.services.memecoin_feed_arrivals import StampedArrivals
from test_memecoin_preconf_signal import OTHER, _raw


REC = next(rec for _, rec in OTHER if rec['lookups'])


def _account(keys, *, owner='AddressLookupTab1e1111111111111111111111111', extended=None,
             deactivated=2**64 - 1):
    raw = struct.pack('<IQQBBH', 1, deactivated, extended or REC['slot'] - 1, 0, 0, 0)
    raw += bytes(32) + b''.join(bytes(key) for key in keys)
    return dict(owner=owner, executable=False, data=[base64.b64encode(raw).decode(), 'base64'])


def _case(read=None):
    update, verify = preconf.frame_update(REC['slot'], _raw(REC))
    assert verify(REC['owner']) == 'ed25519_local'
    lookups = update.transaction.transaction.transaction.message.address_table_lookups
    vectors = [[Pubkey.new_unique() for _ in range(max(bytes(t.writable_indexes)
                    + bytes(t.readonly_indexes)) + 1)] for t in lookups]
    response = {'context': {'slot': REC['slot']}, 'value': [_account(keys) for keys in vectors]}
    clock = [1.]
    calls = []
    def rpc(method, params):
        calls.append((method, params, threading.current_thread().name))
        return read() if read is not None else response
    buyer = SimpleNamespace(read=rpc, seen={}, _tasks=set(), MAX_IN_FLIGHT=8,
        clock=SimpleNamespace(monotonic=lambda: clock[0]), stats={},
        backstop=SimpleNamespace(allowed=lambda _feed: True))
    return update, buyer, calls, vectors, response, clock


def test_signed_real_v0_resolves_only_in_existing_worker_then_reuses_exact_cached_vectors():
    async def run():
        update, buyer, calls, vectors, _, clock = _case()
        tables = preconf.LookupTables(buyer)
        ready = []
        assert tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED,
                            on_ready=lambda u: ready.append(u))
        assert calls == [] and ready == []  # receive callback performed no RPC
        await asyncio.gather(*buyer._tasks)
        assert len(calls) == 1 and calls[0][2] != threading.current_thread().name
        assert ready == [update]
        meta = update.transaction.transaction.meta
        lookups = update.transaction.transaction.transaction.message.address_table_lookups
        assert list(meta.loaded_writable_addresses) == [bytes(keys[i])
            for table, keys in zip(lookups, vectors) for i in table.writable_indexes]
        assert list(meta.loaded_readonly_addresses) == [bytes(keys[i])
            for table, keys in zip(lookups, vectors) for i in table.readonly_indexes]
        assert not meta.HasField('err') and not meta.log_messages
        warm, _verify = preconf.frame_update(REC['slot'], _raw(REC))
        clock[0] = 1.018
        assert tables.resolve(warm)
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['owner', 'executable', 'deactivated', 'same_slot_extension',
    'future_extension', 'missing_context', 'old_context', 'partial', 'encoding', 'short', 'unknown'])
def test_unverified_lookup_results_never_reach_a_candidate(damage):
    async def run():
        update, buyer, _, _, response, _ = _case()
        if damage == 'owner': response['value'][0]['owner'] = str(Pubkey.new_unique())
        if damage == 'executable': response['value'][0]['executable'] = True
        if damage == 'deactivated': response['value'][0] = _account([], deactivated=1)
        if damage in ('same_slot_extension', 'future_extension'):
            response['value'][0] = _account([], extended=REC['slot'] + (damage == 'future_extension'))
        if damage == 'missing_context': response.pop('context')
        if damage == 'old_context': response['context']['slot'] = REC['slot'] - 1
        if damage == 'partial': response['value'].pop()
        if damage == 'encoding': response['value'][0]['data'][1] = 'base58'
        if damage == 'short': response['value'][0]['data'][0] = base64.b64encode(bytes(5)).decode()
        if damage == 'unknown': response['value'][0] = None
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        await asyncio.gather(*buyer._tasks)
        assert ready == []
    asyncio.run(run())


@pytest.mark.parametrize('blocked', ['busy', 'processed', 'expired', 'revoked'])
def test_alt_lookup_never_reopens_a_processed_expired_or_unowned_source(blocked):
    async def run():
        update, buyer, calls, _, _, clock = _case()
        signature = str(Signature.from_bytes(update.transaction.transaction.signature))
        ready, tables = [], preconf.LookupTables(buyer)
        if blocked == 'busy': buyer._tasks.update(range(buyer.MAX_IN_FLIGHT))
        if blocked == 'processed': buyer.seen[(signature, REC['owner'])] = 1.
        if blocked == 'expired': clock[0] = 1.501
        if blocked == 'revoked': buyer.backstop.allowed = lambda _feed: False
        assert not tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED,
                                on_ready=ready.append)
        assert calls == [] and ready == []
    asyncio.run(run())


def test_processed_or_expired_during_lookup_cannot_offer_the_old_frame():
    async def run():
        update, buyer, _, _, response, clock = _case()
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        clock[0] = 1.501
        await asyncio.gather(*buyer._tasks)
        assert ready == []
        assert tables.stats['last_reason'] == 'expired'
    asyncio.run(run())


def test_duplicate_alt_frame_cannot_renew_the_first_receipt_clock():
    async def run():
        update, buyer, _, _, _, clock = _case()
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        clock[0] = 1.501
        await asyncio.gather(*buyer._tasks)
        assert not tables.offer(update, owner=REC['owner'], mono=1.501, feed=preconf.FEED,
                                on_ready=ready.append)
        assert ready == []
    asyncio.run(run())


def test_out_of_order_alt_arrival_retains_the_earliest_clock_for_future_duplicates():
    _update, buyer, *_rest = _case()
    tables = preconf.LookupTables(buyer)
    assert tables.receipt('signature', REC['owner'], 1.) == 1.
    assert tables.receipt('signature', REC['owner'], .9) == .9
    assert tables.receipt('signature', REC['owner'], 1.2) == .9


def test_earlier_duplicate_also_expires_an_already_pending_lookup():
    async def run():
        update, buyer, calls, _, _, clock = _case()
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        tables.offer(update, owner=REC['owner'], mono=.8, feed=preconf.FEED, on_ready=ready.append)
        clock[0] = 1.31
        await asyncio.gather(*buyer._tasks)
        assert calls == [] and ready == []
    asyncio.run(run())


def test_reconnect_during_lookup_discards_the_old_generation():
    async def run():
        update, buyer, _, _, _, _ = _case()
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        tables.generation(preconf.FEED, 2)
        await asyncio.gather(*buyer._tasks)
        assert ready == [] and not tables.resolve(update)
        assert tables.stats['last_reason'] == 'reconnected'
    asyncio.run(run())


@pytest.mark.parametrize('reason,error', [('timeout', TimeoutError()),
    ('transport', ConnectionError()), ('rpc_rejected', RuntimeError('secret provider message'))])
def test_lookup_dependency_failures_keep_only_a_bounded_typed_reason(reason, error):
    async def run():
        def fail():
            raise error
        update, buyer, _, _, _, _ = _case(fail)
        tables, ready = preconf.LookupTables(buyer), []
        tables.offer(update, owner=REC['owner'], mono=1., feed=preconf.FEED, on_ready=ready.append)
        await asyncio.gather(*buyer._tasks)
        assert ready == []
        assert tables.stats['last_reason'] == reason
        assert tables.stats['reasons'][reason] == 1
        assert 'secret' not in str(tables.stats)
    asyncio.run(run())


def _signed_direct():
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from sniper.execution import pump_curve_buy as curve
    payer, table = Keypair.from_seed(bytes([99]) * 32), Pubkey.new_unique()
    accounts = [Pubkey.new_unique() for _ in range(18)]
    accounts[6] = payer.pubkey()
    keys = [key for key in accounts if key != payer.pubkey()]
    message = MessageV0.try_compile(payer.pubkey(), [Instruction(Pubkey.from_string(curve.PUMP_PROGRAM),
        curve.BUY_EXACT_SOL_IN_DISCRIMINATOR + struct.pack('<QQ?', 1_000_000_000, 1, False),
        [AccountMeta(key, key == payer.pubkey(), True) for key in accounts])],
        [AddressLookupTableAccount(table, keys)], Hash.default())
    return bytes(VersionedTransaction(message, [payer])), str(payer.pubkey()), keys


def test_sink_warm_alt_reuse_recovers_early_candidate_without_rpc_or_clock_reset():
    async def run():
        raw, owner, keys = _signed_direct()
        offered, calls = [], []
        now = [1.]
        buyer = SimpleNamespace(read=lambda *a: calls.append(a) or {
            'context': {'slot': REC['slot']}, 'value': [_account(keys)]}, seen={}, _tasks=set(),
            MAX_IN_FLIGHT=8, stats={}, clock=SimpleNamespace(monotonic=lambda: now[0]),
            backstop=SimpleNamespace(allowed=lambda _: True), offer=lambda *a, **k: offered.append((a, k)))
        arrivals = StampedArrivals((preconf.FEED,))
        sink = preconf.FrameSink(feed=preconf.FEED, owners_fn=lambda: {owner: ('uid', 'h')},
            arrivals=arrivals, frames=None, status={}, buyer=buyer, clock=lambda: now[0])
        sink(REC['slot'], preconf.STATUS_SUCCESS, raw, 1., 1)
        assert calls == [] and offered == []
        await asyncio.gather(*buyer._tasks)
        assert len(calls) == 1 and len(offered) == 1
        assert offered[0][0][0]['recv_monotonic'] == 1.
        assert offered[0][1]['basis'] == 'ed25519_local'
        now[0] = 1.018
        sink(REC['slot'], preconf.STATUS_SUCCESS, raw, 1.018, 1)
        assert len(calls) == 1
        assert offered[-1][0][0]['recv_monotonic'] == 1.  # duplicate keeps its first clock
    asyncio.run(run())


def test_sink_live_connection_status_closes_old_lookup_without_a_new_frame():
    async def run():
        raw, owner, _keys = _signed_direct()
        _update, buyer, calls, _, _, _ = _case()
        offered, status = [], {'feed': {'connection_generation': 1, 'connected': True}}
        buyer.offer = lambda *a, **k: offered.append(a)
        sink = preconf.FrameSink(feed=preconf.FEED, owners_fn=lambda: {owner: ('uid', 'h')},
            arrivals=StampedArrivals((preconf.FEED,)), frames=None, status=status, buyer=buyer)
        sink(REC['slot'], preconf.STATUS_SUCCESS, raw, 1., 1)
        status['feed'].update(connection_generation=2, connected=False)
        await asyncio.gather(*buyer._tasks)
        assert calls == [] and offered == []
        assert buyer.preconf_tables.stats['last_reason'] == 'reconnected'
    asyncio.run(run())
