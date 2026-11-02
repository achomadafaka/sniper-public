"""Paired arrival clocks never change native admission or manufacture missing times."""
import asyncio
import copy
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from sniper.services.memecoin_feed_arrivals import FeedArrivals


FEEDS = ('helius', 'shyft:ams', 'shyft:fra')


def test_later_feed_and_repeated_duplicate_keep_first_receipt_and_winner():
    m = FeedArrivals(FEEDS)
    m.observe('sig', 'owner', 'shyft:fra', 10.0)
    m.observe('sig', 'owner', 'helius', 10.040)
    m.observe('sig', 'owner', 'helius', 10.500)
    result = m.snapshot(now=16.0)
    assert result['winners'] == {'helius': 0, 'shyft:ams': 0, 'shyft:fra': 1}
    pair = result['pairs']['helius|shyft:fra']
    assert pair['n'] == 1
    assert pair['p50_ms'] == pytest.approx(-40.0)
    assert pair['p90_ms'] == pytest.approx(-40.0)
    assert pair['missing_left'] == pair['missing_right'] == 0
    assert result['pairs']['helius|shyft:ams']['missing_right'] == 1
    assert result['pairs']['helius|shyft:ams']['p50_ms'] is None


def test_signature_owner_is_the_sample_unit_and_zero_clock_is_valid():
    m = FeedArrivals(FEEDS)
    m.observe('sig', 'one', 'helius', 0.0)
    m.observe('sig', 'two', 'shyft:fra', 0.01)
    result = m.snapshot(now=6.0)
    assert result['samples'] == 2
    assert result['pairs']['helius|shyft:fra']['n'] == 0
    assert result['pairs']['helius|shyft:fra']['missing_left'] == 1
    assert result['pairs']['helius|shyft:fra']['missing_right'] == 1


def test_grace_distinguishes_pending_from_censored_and_keeps_single_feed_winner():
    m = FeedArrivals(FEEDS)
    m.observe('only', 'owner', 'shyft:fra', 10.0)
    early = m.snapshot(now=11.0)
    assert early['pairs']['helius|shyft:fra']['pending'] == 1
    assert early['pairs']['helius|shyft:fra']['missing_left'] == 0
    later = m.snapshot(now=16.0)
    assert later['winners']['shyft:fra'] == 1
    assert later['pairs']['helius|shyft:fra']['pending'] == 0
    assert later['pairs']['helius|shyft:fra']['missing_left'] == 1
    assert later['pairs']['helius|shyft:ams']['missing_both'] == 1


def test_sample_cap_and_time_window_are_bounded_without_replay_extending_age():
    m = FeedArrivals(FEEDS, max_samples=2, window_s=10.0)
    for sig, stamp in [('one', 0.0), ('two', 1.0), ('three', 2.0)]:
        m.observe(sig, 'owner', 'helius', stamp)
    assert m.snapshot(now=3.0)['samples'] == 2
    assert m.snapshot(now=3.0)['evicted_cap'] == 1
    m.observe('two', 'owner', 'helius', 9.0)
    assert m.snapshot(now=12.0)['samples'] == 1
    m.observe('four', 'owner', 'helius', 13.0)
    assert m.snapshot(now=13.0)['samples'] == 1
    assert m.snapshot(now=13.0)['evicted_age'] == 2


def test_quantiles_are_signed_median_and_nearest_rank_p90():
    m = FeedArrivals(FEEDS)
    for index, delta in enumerate([-10, 0, 10, 20]):
        m.observe(str(index), 'owner', 'helius', 10.0)
        m.observe(str(index), 'owner', 'shyft:fra', 10.0 + delta / 1000)
    pair = m.snapshot(now=20.0)['pairs']['helius|shyft:fra']
    assert pair['n'] == 4
    assert pair['p50_ms'] == pytest.approx(5.0)
    assert pair['p90_ms'] == pytest.approx(20.0)


def test_bad_clock_unknown_feed_or_empty_identity_is_unavailable_not_zero():
    m = FeedArrivals(FEEDS)
    for sig, owner, feed, stamp in [('sig', 'owner', 'unknown', 1.0),
            ('', 'owner', 'helius', 1.0), ('sig', '', 'helius', 1.0),
            ('sig', 'owner', 'helius', float('nan')),
            ('sig', 'owner', 'helius', float('inf'))]:
        m.observe(sig, owner, feed, stamp)
    result = m.snapshot(now=2.0)
    assert result['samples'] == 0 and result['invalid'] == 5
    assert result['pairs']['helius|shyft:fra']['p50_ms'] is None


def test_shared_sink_keeps_duplicate_arrivals_and_dispatches_winner_first(monkeypatch):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from solders.pubkey import Pubkey
    frames = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_signal_frames.json').read_text())
    owner = '2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf'
    b = s.decode_owner_buy(frames['curve_buy'], owner, bytes(Pubkey.from_string(owner)), 'uid',
                           recv_monotonic=10.0, recv_utc='receipt')
    received, status, stop = [], {}, threading.Event()
    journal = _journal()
    observe = FeedArrivals.observe
    def checked(self, *args):
        assert received  # measurement does not postpone winning callback
        return observe(self, *args)
    monkeypatch.setattr(FeedArrivals, 'observe', checked)
    async def helius(on_buy, *args):
        await asyncio.sleep(.01)
        on_buy(replace(b, recv_monotonic=10.040))
        stop.set()
    async def shyft(url, token, owners_fn, on_buy, feed_status, stop):
        on_buy(copy.copy(b))
        await asyncio.sleep(1)
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(received.append, dict, 'wss://unused', status, stop, None,
                              None, None, None, ('secret', ('https://beta-grpc.fra.shyft.to',)), measurement=journal))
    assert len(received) == 1
    receipts = journal.snapshot()['rows']
    assert len(receipts) == 2 and receipts[1]['decode_state'] == 'duplicate_buy'
    assert receipts[0]['recv_monotonic'] == 10.0
    assert received[0].signal_feed == 'shyft:beta-grpc.fra.shyft.to'
    result = status['arrival_metrics'].snapshot(now=16.0)
    assert result['pairs']['helius|shyft:beta-grpc.fra.shyft.to']['n'] == 1
    assert result['pairs']['helius|shyft:beta-grpc.fra.shyft.to']['p50_ms'] == pytest.approx(-40)


def _journal(cap=4):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    return ReceiptJournal(producer_run_id='source-A', boot_id='boot-A',
                          clock_domain='monotonic-A', max_samples=cap)


def _record(journal, mono=10.0, **fields):
    journal.record_source(**dict(dict(network_id='solana', source_sig='SOURCE',
        source_wallet='WALLET', mint='MINT', pool='POOL', venue='pump_amm', feed='helius',
        recv_monotonic=mono, connection_generation=1, roster_generation='roster-1',
        config_generation='config-1', decode_state='decoded_buy'), **fields))


def test_receipt_identity_capture_never_imports_and_freezes_caller_provenance(monkeypatch):
    journal = _journal()
    provenance = {'roster_generation': 'original', 'config_generation': 'config'}
    journal.provenance_fn = lambda: provenance
    def forbidden_import(*args, **kwargs):
        raise AssertionError('receipt capture imported on the source dispatch path')
    with monkeypatch.context() as capture:
        capture.setattr('builtins.__import__', forbidden_import)
        evidence = journal.identity(0, 7)
    provenance['roster_generation'] = 'later'
    assert evidence['roster_generation'] == 'original'
    assert evidence['recv_monotonic'] == 0 and evidence['connection_generation'] == 7
    with pytest.raises(TypeError):
        evidence['recv_monotonic'] = 1


def test_receipts_survive_unattempted_and_duplicate_deliveries():
    journal = _journal()
    for feed, mono, mint, state in [('helius', 10.0, 'MINT', 'decoded_buy'),
            ('shyft', 10.1, 'MINT', 'duplicate_buy'), ('helius', 10.2, None, 'pool_unresolved')]:
        _record(journal, mono, feed=feed, mint=mint, decode_state=state)
    rows = journal.snapshot()['rows']
    assert len(rows) == 3
    assert rows[0]['recv_monotonic'] == 10.0 and rows[2]['mint'] is None


def test_journal_single_eviction_and_immutable_scalar_rows():
    journal = _journal(2)
    caller = {'eligibility': 'eligibility_unknown'}
    _record(journal, 0, evidence=caller)
    snapshot = journal.snapshot()
    caller['eligibility'] = 'eligible'
    assert snapshot['rows'][0]['eligibility'] == 'eligibility_unknown'
    with pytest.raises(TypeError):
        snapshot['rows'][0]['mint'] = 'other'
    _record(journal, 1); _record(journal, 2)
    result = journal.snapshot()
    assert result['first_sequence'] == 2 and result['last_sequence'] == 3
    assert result['evicted_cap'] == 1 and len(result['rows']) == 2
    assert snapshot['rows'][0]['recv_monotonic'] == 0


@pytest.mark.parametrize('bad', [True, False, -1, float('nan'), float('inf'), '1', None])
def test_invalid_receipt_clock_is_typed_unknown(bad):
    journal = _journal()
    _record(journal, bad)
    row, = journal.snapshot()['rows']
    assert row['recv_monotonic'] is None
    assert row['timestamp_state'] == 'invalid_recv_monotonic'


def test_processed_and_gap_records_share_bounded_sequence():
    journal = _journal()
    journal.record_processed(network_id='solana', our_sig='ours', slot=7,
        error_state='success', recv_monotonic=0, connection_generation=2)
    journal.record_gap(component='helius', reason='decode_error', connection_generation=2,
        mono_start=0, mono_end=1, lost_count=None)
    rows = journal.snapshot()['rows']
    assert [r['kind'] for r in rows] == ['processed', 'gap']
    assert [r['sequence'] for r in rows] == [1, 2]
    assert rows[1]['lost_count'] is None


def test_prepared_shared_dispatch_reuses_immutable_candidates_and_keeps_duplicate_receipts(monkeypatch):
    from sniper.services import memecoin_slot_signal as s
    from sniper.services import memecoin_shyft_signal as g
    from test_memecoin_slot_signal import _decode, CURVE_BUYER
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER, FIX
    journal, stop, status, received = _journal(10), threading.Event(), {}, []
    native = replace(_decode('curve_buy', CURVE_BUYER),
        receipt_evidence=journal.identity(1, 2))
    routed = replace(g.routed_buys(wire_receipt(), {OWNER: ('u', 'h')},
        recv_monotonic=0, recv_utc='original', head_slot=FIX['slot'], head_mono=0)[0],
        receipt_evidence=journal.identity(0, 2))
    def forbidden_replace(*a, **k):
        raise AssertionError('prepared dispatch reconstructed an immutable candidate')
    monkeypatch.setattr(s, 'replace', forbidden_replace)
    async def helius(on_buy, owners_fn, ws_url, feed_status, halt, connect, on_sell, on_routed, baseline):
        try:
            on_buy(native)
            on_routed(routed)
            on_buy(native)
        finally:
            stop.set()
    async def shyft(*a, **k): await asyncio.sleep(1)
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(received.append, dict, 'unused', status, stop, None,
        None, received.append, None, ('secret', ('https://beta-grpc.ams.shyft.to',)), measurement=journal))
    assert len(received) == 2 and received[0] is native and received[1] is routed
    rows = journal.snapshot()['rows']
    assert len(rows) == 3 and rows[-1]['decode_state'] == 'duplicate_buy'
    assert [r['recv_monotonic'] for r in rows] == [1, 0, 1]
    assert all(r['connection_generation'] == 2 for r in rows)
