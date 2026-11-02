"""Rabbit miss diagnostics report why preparation was absent, without widening it."""
import asyncio
import threading

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_shyft_prepare as prepare
from test_memecoin_shyft_prepare import OWNER, packet


ZERO_REASONS = dict(structure=0, signature=0, header=0, alt=0, config=0, unsupported_target=0)


@pytest.mark.parametrize('bad,reason', [
    ('no_tx', 'structure'), ('vote', 'structure'), ('failed', 'structure'),
    ('blockhash', 'structure'), ('key', 'structure'), ('index', 'structure'),
    ('signature', 'signature'), ('signature_count', 'signature'),
    ('signature_mismatch', 'signature'), ('nonsigner', 'signature'),
    ('header', 'header'), ('readonly_header', 'header'),
    ('alt_absent_meta', 'alt'), ('alt_short_vector', 'alt'),
    ('config_unversioned', 'config'), ('unknown_buy', 'unsupported_target'),
    ('ambiguous_route', 'unsupported_target'), ('wrong_actor', 'unsupported_target'),
])
def test_rejected_packet_counts_one_fixed_reason_without_changing_result(bad, reason):
    u = packet()
    info = u.transaction.transaction
    msg = info.transaction.message
    if bad == 'no_tx': u.ClearField('transaction')
    if bad == 'vote': info.is_vote = True
    if bad == 'failed': info.meta.err.err = b'failed'
    if bad == 'blockhash': msg.recent_blockhash = b'bad'
    if bad == 'key': msg.account_keys[3] = b'bad'
    if bad == 'index': msg.instructions[0].program_id_index = 255
    if bad == 'signature': info.signature = b'bad'
    if bad == 'signature_count': del info.transaction.signatures[:]
    if bad == 'signature_mismatch': info.transaction.signatures[0] = bytes(64)
    if bad == 'nonsigner':
        msg.account_keys[0], msg.account_keys[4] = msg.account_keys[4], msg.account_keys[0]
    if bad == 'header': msg.ClearField('header')
    if bad == 'readonly_header': msg.header.num_readonly_unsigned_accounts = 255
    if bad.startswith('alt_'):
        msg.versioned = True
        msg.address_table_lookups.add(account_key=bytes([33]) * 32, writable_indexes=b'\x00')
        if bad == 'alt_short_vector': info.meta.log_messages.append('metadata present')
    if bad == 'config_unversioned': msg.config.compute_unit_limit = 200000
    if bad == 'unknown_buy': msg.instructions[0].data = bytes(25)
    if bad == 'ambiguous_route': msg.instructions.add().CopyFrom(msg.instructions[0])
    if bad == 'wrong_actor':
        ix = msg.instructions[0]; ix.accounts = bytes([2, 4]) + ix.accounts[2:]
    stats = dict(ZERO_REASONS)
    args = dict(recv_monotonic=0, generation=1)
    assert prepare.preparation_candidate(u, {OWNER: ('u', 'h')}, **args) is None
    assert prepare.preparation_candidate(u, {OWNER: ('u', 'h')}, stats=stats, **args) is None
    assert stats == dict(ZERO_REASONS, **{reason: 1})


def test_valid_v1_config_never_creates_a_miss_or_changes_candidate():
    u = packet()
    msg = u.transaction.transaction.transaction.message
    msg.versioned = True
    msg.config.compute_unit_limit = 200000
    stats = dict(ZERO_REASONS)
    args = dict(recv_monotonic=0, generation=1)
    original = prepare.preparation_candidate(u, {OWNER: ('u', 'h')}, **args)
    assert original is not None
    assert prepare.preparation_candidate(u, {OWNER: ('u', 'h')}, stats=stats, **args) == original
    assert stats == ZERO_REASONS


def test_feed_starts_with_explicit_zero_reasons_without_a_connection():
    stop = threading.Event(); stop.set()
    status = {'miss_reasons': {'signature': 99, 'arbitrary-secret': 17}}
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.fra.shyft.to', 'test-token',
        lambda: {OWNER: ('u', 'h')}, lambda _: pytest.fail('callback after stop'), status, stop))
    assert status['miss_reasons'] == ZERO_REASONS


def test_feed_counts_only_acknowledged_decode_misses(monkeypatch):
    import grpc
    stop, status, received = threading.Event(), {}, []
    unsupported = packet()
    unsupported.transaction.transaction.transaction.message.instructions[0].data = bytes(25)

    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            yield unsupported  # no ACK: this is not a decode miss
            pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            yield unsupported
            yield packet()
        def cancel(self): pass

    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream_stream(self, *args, **kwargs): return lambda requests, **kw: Stream(requests)

    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *args, **kwargs: Channel())
    def receive(row): received.append(row); stop.set()
    asyncio.run(prepare.run_prepare_feed('https://rabbitstream.fra.shyft.to', 'test-token',
        lambda: {OWNER: ('u', 'h')}, receive, status, stop))
    assert len(received) == 1
    assert (status['transactions'], status['before_ack'], status['misses'], status['decoded']) == (3, 1, 1, 1)
    assert status['miss_reasons'] == dict(ZERO_REASONS, unsupported_target=1)
    assert 'test-token' not in repr(status)


def test_heartbeat_projects_only_fixed_reason_counts(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    lane = {'signal': {'preparation': {'feed': {'miss_reasons':
        dict(signature=2, unsupported_target=3, **{'secret-key': 'secret-value'})}}}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane)
    raw = path.read_text()
    reasons = json.loads(raw)['slot_lane']['preparation']['feed']['miss_reasons']
    assert reasons == dict(ZERO_REASONS, signature=2, unsupported_target=3)
    assert 'secret-key' not in raw and 'secret-value' not in raw


def test_heartbeat_absent_reason_telemetry_stays_unknown(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    write_heartbeat({}, auth_ok=True, path=path,
        slot_lane_status={'signal': {'preparation': {'feed': {'misses': 17}}}})
    feed = json.loads(path.read_text())['slot_lane']['preparation']['feed']
    assert feed['misses'] == 17 and feed['miss_reasons'] is None


@pytest.mark.parametrize('bad', ['secret-value', {'secret-key': 'secret-value'}, True, -1])
def test_heartbeat_invalid_reason_count_stays_unknown_and_cannot_leak(bad, tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={'signal': {
        'preparation': {'feed': {'miss_reasons': {'structure': bad}}}}})
    raw = path.read_text()
    reasons = json.loads(raw)['slot_lane']['preparation']['feed']['miss_reasons']
    assert reasons == dict(ZERO_REASONS, structure=None)
    assert 'secret-key' not in raw and 'secret-value' not in raw
