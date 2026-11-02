"""RabbitStream stage 1: DFlow routes decode from the pinned IDL, and owner-signed pre-execution frames
are stamped beside the processed feeds on one clock. MEMECOIN_SLOT_LANE_RABBIT_SIGNAL off = today."""
import asyncio
import json
import statistics
import threading
import time
from pathlib import Path

import base58
import pytest

from sniper.execution import dflow_swap as D
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_shyft_prepare as prep
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / 'fixtures/memecoin/dflow_swap_routes_20261015.json').read_text())
ROUTES = {r['signature']: r for r in FX['routes']}
CURVE = next(r for r in FX['routes'] if r['buy_leg'] == 'pump_curve')
DAMM = next(r for r in FX['routes'] if r['buy_leg'] == 'meteora_damm_v2')


def frame(route, *, slot=None):
    update = pb.SubscribeUpdate()
    update.transaction.slot = slot or route['slot']
    info = update.transaction.transaction
    tx = route['transaction']
    info.signature = base58.b58decode(tx['signatures'][0])
    info.transaction.signatures.extend(base58.b58decode(s) for s in tx['signatures'])
    msg, raw = info.transaction.message, tx['message']
    for src, dst in [('numRequiredSignatures', 'num_required_signatures'),
                     ('numReadonlySignedAccounts', 'num_readonly_signed_accounts'),
                     ('numReadonlyUnsignedAccounts', 'num_readonly_unsigned_accounts')]:
        setattr(msg.header, dst, raw['header'][src])
    msg.account_keys.extend(base58.b58decode(k) for k in raw['accountKeys'])
    msg.recent_blockhash = base58.b58decode(raw['recentBlockhash'])
    msg.versioned = True
    msg.config.compute_unit_limit = raw['transactionConfig']['computeUnitLimit']
    for ix in raw['instructions']:
        msg.instructions.add(program_id_index=ix['programIdIndex'], accounts=bytes(ix['accounts']),
                             data=base58.b58decode(ix['data']))
    return update


def owner(route):
    return route['signers'][1]


def candidate(route, **kw):
    return prep.preparation_candidate(frame(route), {owner(route): ('uid', 'h')},
                                      recv_monotonic=kw.pop('mono', 0.0), generation=1, **kw)


def test_pinned_idl_decodes_every_recorded_route_exactly():
    assert D.idl_ok()
    for route in FX['routes']:
        got = D.decode_swap(base58.b58decode(route['data']))
        assert got is not None and list(got['actions']) == route['actions']
        assert got['quoted_out_amount'] == route['quoted_out_amount']
        assert D.buy_leg(got) == route['buy_leg']
    assert sorted(str(r['buy_leg']) for r in FX['routes']) == [
        'None', 'None', 'meteora_damm_v2', 'pump_curve', 'pump_curve', 'pump_curve']


def test_a_changed_idl_file_disables_the_decoder(tmp_path, monkeypatch):
    changed = tmp_path / 'idl.json'
    changed.write_bytes(D.IDL_PATH.read_bytes().replace(b'"RecordId"', b'"RecordIx"', 1))
    monkeypatch.setattr(D, 'IDL_PATH', changed)
    D._idl.cache_clear()
    try:
        assert not D.idl_ok()
        assert all(D.decode_swap(base58.b58decode(r['data'])) is None for r in FX['routes'])
    finally:
        D._idl.cache_clear()


@pytest.mark.parametrize('damage', ['truncated', 'trailing', 'unknown_variant', 'other_instruction'])
def test_data_the_idl_does_not_consume_exactly_decodes_to_nothing(damage):
    data = bytearray(base58.b58decode(CURVE['data']))
    if damage == 'truncated':
        data = data[:-1]
    elif damage == 'trailing':
        data += b'\x00'
    elif damage == 'unknown_variant':
        data[12] = 250                       # first action tag (after discriminator + vec length)
    else:
        data[:8] = bytes(8)
    assert D.decode_swap(bytes(data)) is None


@pytest.mark.parametrize('actions, leg', [
    (('RecordId', 'PumpFunBuyV3'), 'pump_curve'),
    (('RecordId', 'PumpFunSellV3', 'PumpFunBuyV3'), None),          # sells something: not read
    (('PumpFunBuyV3', 'PumpFunBuyV3'), None),                       # split into two buy legs
    (('MeteoraDammV2Swap', 'MeteoraDlmmSwap'), None),               # last swap is not a buy leg
    (('RecordId', 'SetSponsor'), None)])                            # no swap at all
def test_buy_leg_is_the_single_final_known_venue(actions, leg):
    assert D.buy_leg({'actions': actions}) == leg


def test_knob_off_keeps_todays_dflow_decoder(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', False)
    got = candidate(CURVE)
    assert got is None          # today's decoder knows no PumpFunBuyV3 route: unsupported_target


def test_knob_on_targets_the_final_leg_by_venue_with_references_only(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    for route in (CURVE, DAMM):
        got = candidate(route)
        target = prep.preparation_target(got)
        assert target['venue'] == route['buy_leg']
        refs = target['candidate_accounts']
        assert len(refs) == len(set(refs)) <= 64 and set(refs) <= set(route['accounts'])
        assert not {'pool', 'mint', 'source_price', 'clip', 'min_out', 'source_initiation'} & set(target)


def test_drain_names_a_venue_it_cannot_prebuild_instead_of_an_error(monkeypatch):
    import queue
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    pending = queue.Queue()
    pending.put(candidate(CURVE, mono=0.0))
    status = {}
    prep.drain_preparations(pending, {'pump_curve': type('B', (), {'pools': object()})()}, status, now=0.1)
    assert status['errors'] == 0 and status['no_prebuild_pump_curve'] == 1


def test_owner_signed_frames_are_stamped_once_and_others_are_not(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    seen = []
    prep.preparation_candidate(frame(CURVE), {owner(CURVE): ('uid', 'h')}, recv_monotonic=5.0,
                               generation=3, on_owner_frame=lambda *a: seen.append(a))
    prep.preparation_candidate(frame(CURVE), {'NotTheOwner11111111111111111111111111111111': ('u', 'h')},
                               recv_monotonic=6.0, generation=3, on_owner_frame=lambda *a: seen.append(a))
    assert seen == [(CURVE['signature'], owner(CURVE), CURVE['slot'], 5.0, 3)]


def test_a_failing_stamp_never_changes_the_preparation_result(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    def boom(*a):
        raise RuntimeError('observer')
    stats = {}
    got = prep.preparation_candidate(frame(CURVE), {owner(CURVE): ('uid', 'h')}, recv_monotonic=0.0,
                                     generation=1, stats=stats, on_owner_frame=boom)
    assert got is not None and stats.get('observer_errors') == 1


def test_signal_coordinator_pairs_rabbit_with_processed_and_journals_it_pre_execution(monkeypatch):
    """One clock, one stream: the rabbit arm joins the feed-arrival pairs and the journal row says
    rabbit_pre_execution, never processed."""
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    stop, status = threading.Event(), {}
    journal = ReceiptJournal(producer_run_id='p', boot_id='b', clock_domain='c')
    t0 = signal.time.monotonic()
    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, *, on_owner_frame=None,
                          on_owner_miss=None, on_rejected_candidate=None):
        prep.preparation_candidate(frame(CURVE), owners_fn(), recv_monotonic=t0, generation=1,
                                   on_owner_frame=on_owner_frame)
        while not stop.is_set():
            await asyncio.sleep(.01)
    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None, **kwargs):
        await asyncio.sleep(.02)
        from types import SimpleNamespace
        from dataclasses import replace
        from sniper.services.memecoin_slot_signal import SlotBuy
        buy = SlotBuy(venue='pump_curve', source_user_id='uid', handle='h', owner=owner(CURVE),
                      signature=CURVE['signature'], slot=CURVE['slot'], mint='M', pool=None, sol_amount=1,
                      token_amount=1, virtual_sol_reserves=1, virtual_token_reserves=1, real_sol_reserves=1,
                      real_token_reserves=1, token_program=None, trade_event_log='', recv_monotonic=t0 + .037,
                      recv_utc='')
        on_buy(buy)
        await asyncio.sleep(.02)
        stop.set()
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(lambda b: None, lambda: {owner(CURVE): ('uid', 'h')},
        None, status, stop, None, None, None, None, ('token', ('https://grpc.ams.shyft.to',)),
        infra_provider='shyft', prepare_config=('token', 'https://rabbitstream.fra.shyft.to'),
        on_prepare=lambda c: None, measurement=journal))
    snap = status['arrival_metrics'].snapshot(now=t0 + 1)
    pair = snap['pairs']['shyft:grpc.ams.shyft.to|rabbit:rabbitstream.fra.shyft.to']
    assert pair['n'] == 1 and pair['p50_ms'] == pytest.approx(-37.0, abs=0.5)   # rabbit 37 ms earlier
    rows = [r for r in journal.snapshot()['rows'] if r.get('feed') == 'rabbit:rabbitstream.fra.shyft.to']
    assert len(rows) == 1 and rows[0]['decode_state'] == 'rabbit_pre_execution'
    assert rows[0]['slot_status'] == 'pre_execution' and rows[0]['slot'] == CURVE['slot']
    assert rows[0]['source_sig'] == CURVE['signature'] and rows[0]['recv_monotonic'] == t0


def test_knob_off_adds_no_rabbit_arm(monkeypatch):
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services import memecoin_shyft_signal as full
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', False)
    stop, status, kwargs_seen = threading.Event(), {}, []
    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, **kwargs):
        kwargs_seen.append(kwargs)
        stop.set()
    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, **kwargs):
        while not stop.is_set():
            await asyncio.sleep(.01)
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(lambda b: None, lambda: {}, None, status, stop, None, None, None,
        None, ('token', ('https://grpc.ams.shyft.to',)), infra_provider='shyft',
        prepare_config=('token', 'https://rabbitstream.fra.shyft.to'), on_prepare=lambda c: None))
    assert kwargs_seen == [{}]
    assert status['arrival_metrics'].feeds == ('shyft:grpc.ams.shyft.to',)


def test_per_frame_decode_cost_is_bounded(monkeypatch):
    """Measured on this host 2026-10-15 (see the commit): the whole owner-frame decode, structure
    plus IDL route plus the stamp, stays well under a millisecond on the signal loop."""
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    frames = [frame(r) for r in FX['routes']] * 40
    owners = {owner(r): ('uid', 'h') for r in FX['routes']}
    samples = []
    for update in frames:
        start = time.perf_counter()
        got = prep.preparation_candidate(update, owners, recv_monotonic=0.0, generation=1,
                                         on_owner_frame=lambda *a: None)
        if got is not None:
            prep.preparation_target(got)
        samples.append((time.perf_counter() - start) * 1e6)
    samples.sort()
    assert samples[len(samples) * 9 // 10] < 1500.0          # p90 bound, microseconds (measured ~590)


def test_heartbeat_names_unprebuilt_legs_and_observer_errors(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    lane = {'preparation': dict(capacity=128, delivered=1, no_prebuild_pump_curve=4,
                                no_prebuild_meteora_damm_v2=2, no_prebuild_bogus='x'),
            'signal': {'preparation': {'pending': {}, 'callback_errors': 0,
                'feed': dict(endpoint_hostname='rabbitstream.fra.shyft.to',
                             miss_reasons=dict(signature=3, observer_errors=1))}}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane)
    row = json.loads(path.read_text())['slot_lane']['preparation']
    assert row['queue']['no_prebuild'] == {'pump_curve': 4, 'meteora_damm_v2': 2}
    assert row['feed']['observer_errors'] == 1 and row['feed']['miss_reasons']['signature'] == 3
