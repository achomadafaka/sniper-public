"""Crowded priority fee on the slot lane (operator YES 2026-10-15 via Alpha) and the processed feed's
drop rows naming their cause (Alpha (a), 2026-10-15)."""
import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_crowded_fee as C
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_RULE', 'early')
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 10_000_000)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS', 12_000_000)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 150_000_000)


# --- the price ------------------------------------------------------------------------------------

def test_the_crowded_price_buys_at_least_the_target_and_never_passes_the_cap():
    price, bid = curve.crowded_priority(7_600, {'priority_source': 'fallback_cold'}, 200_000, None, 10_000_000)
    assert bid['crowded'] == 'raised' and bid['priority_source'] == 'crowded_override'
    assert bid['priority_fee_lamports'] == -(-price * 200_000 // 1_000_000) >= 10_000_000
    price, bid = curve.crowded_priority(7_600, {}, 137_777, ('TipAcct', 5_000_000), 10_000_000)
    assert bid['priority_fee_lamports'] + 5_000_000 <= 12_000_000           # priority + tip <= MAX_SEND
    assert bid['priority_fee_lamports'] >= 7_000_000 - 1


def test_a_higher_normal_bid_stands_and_no_room_bids_normally():
    assert curve.crowded_priority(10 ** 9, {}, 200_000, None, 10_000_000) == (
        10 ** 9, {'crowded_target_lamports': 10_000_000, 'crowded_room_lamports': 12_000_000,
                  'crowded': 'normal_bid_higher'})
    price, bid = curve.crowded_priority(7_600, {}, 200_000, ('TipAcct', 12_000_000), 10_000_000)
    assert price == 7_600 and bid['crowded'] == 'no_room'


def test_select_priority_fee_only_moves_with_an_override():
    builder = SimpleNamespace(priority_fee_fn=None, priority_fee_microlamports=7_600)
    assert curve.select_priority_fee(builder, 'Pool', 200_000, None) == (7_600, None)
    price, bid = curve.select_priority_fee(builder, 'Pool', 200_000, None, 10_000_000)
    assert price > 7_600 and bid['priority_fee_lamports'] >= 10_000_000


# --- the arm --------------------------------------------------------------------------------------

def _fee(pages=None, now='2026-10-15T14:00:00+00:00'):
    return C.CrowdedFee(now_iso_fn=lambda: now, alert_fn=lambda *a: (pages if pages is not None else []).append(a))


BUY = SimpleNamespace(venue='pump_curve', graduation=None)


@pytest.mark.parametrize('head, reason', [(None, 'head_unknown'), (0, 'late'), (2, 'late'), ('x', 'head_unknown')])
def test_an_unknown_or_late_head_bids_normally(head, reason):
    got = _fee().arm(BUY, 'a1', head_minus_s=head)
    assert got['arm'] is None and got['reason'] == reason


def test_off_out_of_scope_and_capped_bid_normally(monkeypatch):
    fee = _fee()
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_VENUES', ('pump_curve', 'pump_amm'))   # today's scope
    assert fee.arm(SimpleNamespace(venue='raydium_cpmm', graduation=None), 'a', head_minus_s=-1)['reason'] == \
        'venue_not_in_scope'
    fee.spent = 150_000_000
    assert fee.arm(BUY, 'a', head_minus_s=-1)['reason'] == 'daily_cap'
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 0)
    assert fee.arm(BUY, 'a', head_minus_s=-1)['reason'] == 'off'
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 10_000_000.0)   # overlay float
    assert _fee().arm(BUY, 'b', head_minus_s=-1)['target_lamports'] == 10_000_000


def test_the_holdout_is_deterministic_per_attempt_and_a_quarter():
    ids = ['attempt-%d' % i for i in range(4000)]
    first = [C.holdout(i) for i in ids]
    assert first == [C.holdout(i) for i in ids]
    assert 0.22 < sum(first) / len(ids) < 0.28
    fee = _fee()
    arms = {fee.arm(BUY, i, head_minus_s=-1)['arm'] for i in ids[:40]}
    assert arms == {'treated', 'holdout'}
    assert fee.arm(BUY, ids[0], head_minus_s=-1)['arm'] == ('holdout' if first[0] else 'treated')


def test_the_day_pages_once_at_the_cap_and_resets_at_midnight():
    pages, clock = [], {'now': '2026-10-15T23:59:00+00:00'}
    fee = C.CrowdedFee(now_iso_fn=lambda: clock['now'], alert_fn=lambda *a: pages.append(a))
    fee.arm(BUY, 'a', head_minus_s=-1)
    for _ in range(16):
        fee.sent(10_000_000)
    assert len(pages) == 1 and pages[0][0] == 'SLOT-LANE CROWDED FEE CAP' and fee.status['capped']
    assert fee.arm(BUY, 'z', head_minus_s=-1)['reason'] == 'daily_cap'
    clock['now'] = '2026-10-16T00:00:01+00:00'
    assert fee.arm(BUY, 'z', head_minus_s=-1)['reason'] == 'eligible' and fee.spent == 0


# --- the lane -------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'ledger.db')
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _lane(conn, tmp_path, monkeypatch, *, status='filled', arm='treated'):
    from test_memecoin_slot_lane import _live_lane
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    builds = []

    def build_fn(buy, clip, snap, **kw):
        builds.append(kw)
        bid = ({'priority_fee_lamports': 10_000_120, 'crowded': 'raised', 'tip_lamports': 0}
               if kw.get('priority_lamports') else {'priority_fee_lamports': 1_520_000})
        return 'MESSAGE', {'priority_bid': bid}
    ln.build_fn = build_fn

    def execute(*a, **k):
        sent.append(1)
        return {'status': status, 'sig': 'OurSig1'}
    ln.executor = {'slot_execute_fn': execute}
    ln.crowded = _fee()
    monkeypatch.setattr(C, 'holdout', lambda attempt_id: arm == 'holdout')
    return ln, sent, builds


def _buy(head_minus_s):
    from test_memecoin_slot_lane import _buy as base
    return base(head_at_recv=450_000_000 + head_minus_s, head_at_recv_mono=time.monotonic())


def _events(conn):
    trace = json.loads(conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
    return {e['name']: e for e in trace['substage_events']}


@pytest.mark.parametrize('status', ['filled', 'failed'])
def test_a_treated_early_send_bids_the_target_and_counts_even_when_it_fails(conn, tmp_path, monkeypatch, status):
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch, status=status)
    ln.handle(_buy(-1))
    assert builds == [{'priority_lamports': 10_000_000}] and sent == [1]
    assert ln.crowded.spent == 10_000_120 and ln.crowded.status['sent_treated'] == 1
    ev = _events(conn)
    assert ev['crowded_fee']['arm'] == 'treated' and ev['crowded_fee']['head_minus_s'] == -1
    assert ev['crowded_fee_sent']['sent_lamports'] == 10_000_120


def test_the_holdout_and_an_unknown_head_send_the_normal_bid(conn, tmp_path, monkeypatch):
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch, arm='holdout')
    ln.handle(_buy(-1))
    assert builds == [{}] and ln.crowded.spent == 0 and _events(conn)['crowded_fee']['arm'] == 'holdout'


def test_an_unknown_head_on_the_lane_is_the_normal_bid(conn, tmp_path, monkeypatch):
    from test_memecoin_slot_lane import _buy as base
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch)
    ln.handle(base())                                      # no head reading at receipt
    assert builds == [{}] and _events(conn)['crowded_fee']['reason'] == 'head_unknown'


def test_a_restart_reseeds_todays_spend_from_the_ledger(conn, tmp_path, monkeypatch):
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(lane, 'utc_now_iso', lambda: '2026-09-26T20:30:00+00:00', raising=False)
    ln.handle(_buy(-1))
    fresh = C.CrowdedFee(now_iso_fn=lambda: '2026-09-26T21:00:00+00:00', alert_fn=lambda *a: None)
    assert fresh.seed(conn) == 10_000_120
    tomorrow = C.CrowdedFee(now_iso_fn=lambda: '2026-09-27T00:00:01+00:00', alert_fn=lambda *a: None)
    assert tomorrow.seed(conn) == 0


def test_dispatch_passes_the_fee_only_to_builders_that_take_it():
    seen = {}

    class Taker:
        accepts_priority_lamports = True

        def build(self, buy, clip, snap, **kw):
            seen['taker'] = kw
            return 'M', {}

    class Other:
        def build(self, buy, clip, snap, **kw):
            seen['other'] = kw
            return 'M', {}
    build = lane.dispatch_build({'pump_curve': Taker(), 'raydium_launchlab': Other()})
    build(SimpleNamespace(venue='pump_curve', graduation=None, ondemand=None), 25.0, None, priority_lamports=10)
    build(SimpleNamespace(venue='raydium_launchlab', graduation=None, ondemand=None), 25.0, None,
          priority_lamports=10)
    assert seen['taker'] == {'priority_lamports': 10} and 'priority_lamports' not in seen['other']


def test_the_priority_knob_is_a_hot_cockpit_overlay():
    from sniper.services import memecoin_entry_knobs as knobs
    knob = knobs.KNOBS['MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS']
    assert knob.adjustable and knob.off_value == 0 and knob.maximum == 20_000_000
    with pytest.raises(ValueError):
        knob.coerce(25_000_000)


# --- the processed feed's drop rows ---------------------------------------------------------------

def test_a_dropped_frame_names_its_signature_and_tracked_signer():
    from sniper.services.memecoin_shyft_signal import _dropped_frame
    from test_memecoin_rabbit_signal_stage1 import CURVE, frame, owner
    assert _dropped_frame(frame(CURVE), {owner(CURVE): ('u', 'h')}) == dict(
        source_sig=CURVE['signature'], owner=owner(CURVE))
    assert _dropped_frame(frame(CURVE), {}) == dict(source_sig=CURVE['signature'], owner=None)
    assert _dropped_frame(object(), {}) == {}


def test_a_head_window_drop_records_why_with_head_age_and_slots(monkeypatch):
    from sniper.services import memecoin_shyft_signal as g
    from sniper.services import memecoin_slot_signal as s
    from test_memecoin_feed_arrivals import _journal
    from test_memecoin_rabbit_signal_stage1 import CURVE, frame, owner
    import grpc
    journal = _journal(20)
    stop, status = threading.Event(), {}
    wallet = owner(CURVE)

    async def exercise():
        async def subscribe(incoming, context):
            async for req in incoming:
                if req.HasField('ping'):
                    pong = g.pb.SubscribeUpdate()
                    pong.pong.id = req.ping.id
                    yield pong
                    head = g.pb.SubscribeUpdate()
                    head.slot.slot = CURVE['slot'] - 5           # the frame is 5 slots above the head
                    yield head
                    tx = frame(CURVE)
                    tx.transaction.transaction.meta.fee = 5000
                    yield tx
                    await asyncio.sleep(.05)
                    stop.set()
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=g.pb.SubscribeRequest.FromString,
                response_serializer=g.pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        try:
            await asyncio.wait_for(g.run_feed('https://beta-grpc.ams.shyft.to', 'secret', lambda: {wallet: ('u', 'h')},
                                              lambda buy: None, status, stop, measurement=journal), timeout=3)
        except asyncio.TimeoutError:
            pass
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    gaps = [r for r in journal.snapshot()['rows'] if r['kind'] == 'gap' and r['reason'] == 'transaction_slot_unusable']
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap['why'] == 'slot_above_head' and gap['tx_slot'] == CURVE['slot']
    assert gap['head_slot'] == CURVE['slot'] - 5 and gap['head_age_ms'] >= 0
    assert gap['source_sig'] == CURVE['signature'] and gap['owner'] == wallet


# --- the SIGNED fee (2026-10-15): the entry-bid cap clamped every treated send to 520k / 1.52M -------
# cap_entry_priority_fee caps priority at MAX_TOTAL_LAMPORTS (6.52M) minus the Nozomi tip, after the
# build: slot_build said 10M, entry_bid_signed and the receipts said 520k / 1.52M (73892: 1.525M fee).

class _Trace:
    """Records substages; every other trace call is a no-op (the executor's timing marks)."""
    def __init__(self):
        self.events = []

    def mark_substage(self, name, **kw):
        self.events.append((name, kw))

    def __getattr__(self, name):
        return lambda *a, **k: True


def _signed_priority(validity, *, tip=5_000_000, price=25_000_000, limit=400_000):
    """Through the real slot_execute_fn: the priority fee in the bytes that were signed and sent."""
    import base64
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from sniper.execution import solana_real_seams as S, solana_sender_ab as A, solana_slot_executor as E
    from test_memecoin_solana_slot_executor import _reserve
    kp = Keypair()
    ixs = [set_compute_unit_limit(limit), set_compute_unit_price(price)]
    ixs += A.tip_instructions(str(kp.pubkey()), (A.NOZOMI_TIP_ACCOUNTS[0], tip))
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    seen = []

    class Transport:
        def provider(self, label):
            return object() if label == 'solana:nozomi' else None
    E_exec = E.execute_prebuilt_entry
    try:
        E.execute_prebuilt_entry = lambda _conn, **kw: seen.append(kw) or {'status': 'unknown', 'sig': kw['sig']}
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(conn)
        ex = S.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
                                          submission_transport=Transport(), entry_bids=object())
        trace = _Trace()
        result = ex['slot_execute_fn'](conn, _reserve(conn), msg,
            validity={'nozomi_tip_lamports': tip, **validity},
            now_iso='2026-10-15T16:00:00Z', trace=trace,
            entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
        assert seen, result
    finally:
        E.execute_prebuilt_entry = E_exec
    signed = VersionedTransaction.from_bytes(base64.b64decode(seen[0]['signed_b64']))
    limit, price = budget(signed.message)
    bid = dict(next(kw for name, kw in trace.events if name == 'entry_bid_signed'))
    return (price * limit + 999_999) // 1_000_000, bid


def test_a_treated_send_signs_its_crowded_priority_at_the_signing_seam(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS', 16_000_000)
    signed, bid = _signed_priority({'crowded_priority_lamports': 10_000_000})
    assert signed == 10_000_000 and bid['priority_fee_lamports'] == signed        # 10M + the 5M tip
    small, _ = _signed_priority({'crowded_priority_lamports': 10_000_000}, tip=1_000_000)
    assert small == 10_000_000                                                    # the tip never adds


def test_the_crowded_priority_never_passes_the_send_cap_with_its_tip(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS', 12_000_000)
    signed, _ = _signed_priority({'crowded_priority_lamports': 10_000_000})
    assert signed == 7_000_000                                                    # 12M - the 5M tip


@pytest.mark.parametrize('crowded', [None, 0, -5, '10000000', 1.5, True])
def test_without_a_crowded_value_the_signed_fee_is_todays_cap(monkeypatch, crowded):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS', 16_000_000)
    validity = {} if crowded is None else {'crowded_priority_lamports': crowded}
    signed, _ = _signed_priority(validity)
    assert signed == 1_520_000                                                    # 6.52M - the 5M tip


def _signed_lane(conn, tmp_path, monkeypatch, *, signed_priority, arm='treated'):
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch, arm=arm)
    seen = []

    def execute(conn_, pending, message, *, validity, trace, **k):
        seen.append(dict(validity))
        trace.mark_substage('entry_bid_signed', priority_fee_lamports=signed_priority, tip_lamports=6_000_000)
        sent.append(1)
        return {'status': 'failed', 'sig': 'OurSig1'}
    ln.executor = {'slot_execute_fn': execute}
    return ln, seen


def test_the_daily_counter_and_crowded_fee_sent_read_the_signed_bid(conn, tmp_path, monkeypatch):
    ln, seen = _signed_lane(conn, tmp_path, monkeypatch, signed_priority=520_000)
    ln.handle(_buy(-1))
    assert seen[0]['crowded_priority_lamports'] == 10_000_000
    ev = _events(conn)
    assert ev['crowded_fee_sent']['sent_lamports'] == ev['entry_bid_signed']['priority_fee_lamports'] == 520_000
    assert ev['crowded_fee_sent']['built_lamports'] == 10_000_120
    assert ln.crowded.spent == 520_000


def test_a_holdout_send_carries_no_crowded_value(conn, tmp_path, monkeypatch):
    ln, seen = _signed_lane(conn, tmp_path, monkeypatch, signed_priority=520_000, arm='holdout')
    ln.handle(_buy(-1))
    assert 'crowded_priority_lamports' not in seen[0] and ln.crowded.spent == 0


def test_the_seed_recounts_the_signed_bid_not_a_phantom_build_value(conn):
    from sniper.services.memecoin_entry_trace import EntryAttemptTrace  # noqa: F401 (the shape below)
    trace = {'substage_events': [
        {'name': 'crowded_fee_sent', 'arm': 'treated', 'sent_lamports': 10_000_000},
        {'name': 'entry_bid_signed', 'priority_fee_lamports': 520_000}]}
    m.start_entry_attempt(conn, attempt_id='phantom', decision_started_at='2026-10-15T16:01:06+00:00',
                          network_id='solana', mint='M', signal_source='slot_lane',
                          delivery_lane='slot_lane_processed')
    m.finish_entry_attempt(conn, 'phantom', 'failed', 'chain_error:x', '2026-10-15T16:01:07+00:00',
                           json.dumps(trace), tx_sig='PhantomSig')
    fresh = C.CrowdedFee(now_iso_fn=lambda: '2026-10-15T20:00:00+00:00', alert_fn=lambda *a: None)
    assert fresh.seed(conn) == 520_000


# --- every slot-lane venue (Alpha GO 2026-10-16; MIDWAY 7de9dad9: a routed DAMM v2 buy at 1.52M, S+1 index 52) ---

ALL_VENUES = ('pump_curve', 'pump_amm', 'meteora_damm_v2', 'raydium_cpmm', 'raydium_launchlab', 'meteora_dbc')


def budget(msg):
    """(cu_limit, micro-lamports per CU) of a built message's ComputeBudget instructions."""
    import struct
    keys = [str(k) for k in msg.account_keys]
    limit = price = None
    for ix in msg.instructions:
        if keys[ix.program_id_index] != 'ComputeBudget111111111111111111111111111111':
            continue
        data = bytes(ix.data)
        if data[:1] == b'\x02':
            limit = struct.unpack('<I', data[1:])[0]
        elif data[:1] == b'\x03':
            price = struct.unpack('<Q', data[1:])[0]
    return limit, price


def fee_lamports(limit, price):
    return -(-price * limit // 1_000_000)


@pytest.mark.parametrize('venue', ALL_VENUES)
def test_every_slot_lane_venue_is_in_scope_by_default(venue):
    got = _fee().arm(SimpleNamespace(venue=venue, graduation=None), 'a', head_minus_s=-1)
    assert got['reason'] == 'eligible' and got['arm'] in ('treated', 'holdout')


def test_the_knob_narrowed_to_pump_restores_todays_scope(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_VENUES', ('pump_curve', 'pump_amm'))
    for venue in ALL_VENUES[2:]:
        got = _fee().arm(SimpleNamespace(venue=venue, graduation=None), 'a', head_minus_s=-1)
        assert got['reason'] == 'venue_not_in_scope' and got['arm'] is None
    assert _fee().arm(BUY, 'a', head_minus_s=-1)['reason'] == 'eligible'


@pytest.mark.parametrize('raw, venues', [(None, ALL_VENUES), ('pump_curve,pump_amm', ('pump_curve', 'pump_amm')),
                                         (' meteora_damm_v2 , pump_curve ', ('meteora_damm_v2', 'pump_curve')),
                                         ('', ())])
def test_the_venue_knob_parses(monkeypatch, raw, venues):
    import importlib
    if raw is None:
        monkeypatch.delenv('MEMECOIN_SLOT_LANE_CROWDED_VENUES', raising=False)
    else:
        monkeypatch.setenv('MEMECOIN_SLOT_LANE_CROWDED_VENUES', raw)
    try:
        assert importlib.reload(cfg).MEMECOIN_SLOT_LANE_CROWDED_VENUES == venues
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_an_unknown_venue_name_fails_at_import(monkeypatch):
    import importlib
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_CROWDED_VENUES', 'pump_curve,exotic')
    try:
        with pytest.raises(ValueError):
            importlib.reload(cfg)
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_a_routed_buy_reads_the_head_its_pair_completed_at():
    routed = SimpleNamespace(paired_head_slot=449_999_999)
    buy = SimpleNamespace(slot=450_000_000, head_at_recv=None, head_at_recv_mono=None, recv_monotonic=1.0,
                          routed=routed)
    got = lane.receipt_head(buy)
    assert got['head_at_recv'] == 449_999_999 and got['head_at_recv_minus_s'] == -1
    assert got['head_at_recv_basis'] == 'routed_pair_complete' and got['head_at_recv_age_ms'] is None
    buy.routed = SimpleNamespace(paired_head_slot=None)
    assert lane.receipt_head(buy)['head_at_recv_minus_s'] is None
    assert _fee().arm(BUY, 'a', head_minus_s=lane.receipt_head(buy)['head_at_recv_minus_s'])['reason'] == \
        'head_unknown'
    direct = SimpleNamespace(slot=450_000_000, head_at_recv=450_000_001, head_at_recv_mono=1.0,
                             recv_monotonic=1.0, routed=SimpleNamespace(paired_head_slot=449_999_999))
    got = lane.receipt_head(direct)                         # the receipt's own head wins
    assert got['head_at_recv_minus_s'] == 1 and got['head_at_recv_basis'] == 'receipt'


def test_a_static_builders_price_reaches_the_target_from_its_own_cu_limit():
    b = SimpleNamespace(priority_fee_microlamports=3_800_000)
    assert curve.static_priority(b, 400_000, None, None, 1) == (3_800_000, None)
    price, bid = curve.static_priority(b, 400_000, None, 10_000_000, 1)
    assert fee_lamports(400_000, price) >= 10_000_000 and bid['crowded'] == 'raised'
    price, bid = curve.static_priority(b, 520_000, None, 10_000_000, 1)       # an exotic leg's CU limit
    assert fee_lamports(520_000, price) >= 10_000_000
    assert curve.static_priority(SimpleNamespace(priority_fee_microlamports=None), 400_000, None, None, 7) == (7, None)


@pytest.mark.parametrize('tip', [None, ('TipAcct', 5_000), ('TipAcct', 5_000_000), ('TipAcct', 12_000_000)])
def test_a_static_build_never_passes_max_send_with_its_tip(tip):
    b = SimpleNamespace(priority_fee_microlamports=3_800_000)
    price, bid = curve.static_priority(b, 400_000, tip, 10_000_000, 1)
    tip_lamports = tip[1] if tip else 0
    if bid.get('crowded') == 'no_room':
        assert price == 3_800_000                            # no room: the static price, not raised
    else:
        assert fee_lamports(400_000, price) + tip_lamports <= 12_000_000


def test_the_cap_bound_time_is_recorded_reset_at_midnight_and_exported(tmp_path):
    import json as _json
    from sniper.services import memecoin_runner as run
    clock = {'now': '2026-10-16T19:00:00+00:00'}
    fee = C.CrowdedFee(now_iso_fn=lambda: clock['now'], alert_fn=lambda *a: None)
    fee.arm(BUY, 'a', head_minus_s=-1)
    assert fee.status['bound_at'] is None
    for _ in range(15):
        fee.sent(10_000_000)
    assert fee.status['capped'] and fee.status['bound_at'] == '2026-10-16T19:00:00+00:00'
    path = tmp_path / 'hb.json'
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={'crowded_fee': fee.status})
    got = _json.loads(path.read_text())['slot_lane']['crowded_fee']
    assert got['bound_at'] == '2026-10-16T19:00:00+00:00' and got['spent_lamports'] == 150_000_000
    assert got['daily_cap_lamports'] == 150_000_000
    assert got['accounting_state'] == 'known'
    clock['now'] = '2026-10-17T00:00:01+00:00'
    fee.arm(BUY, 'b', head_minus_s=-1)
    assert fee.status['bound_at'] is None and not fee.status['capped']


# --- Codex F9 (review through 648ada5d7): the last treated send stays inside the daily cap ------------

@pytest.mark.parametrize('cu_limit', [137_777, 200_000, 400_000, 1_400_000])
def test_the_last_treated_send_is_bounded_by_the_remaining_allowance(monkeypatch, cu_limit):
    """arm() checked prior spend only: at 144M of a 150M cap, with the allowed hot target 20M and the
    12M per-send cap, the thirteenth treated send charged 12M and reached 156M. Now the target is
    bounded by what is left of the day, including the build's CU rounding."""
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 20_000_000)
    fee = _fee()
    fee._roll(fee.now_iso_fn())                           # today's day, so the spend is not rolled away
    fee.spent = 144_000_000
    got = fee.arm(BUY, 'treated-one', head_minus_s=-1)
    assert got['reason'] == 'eligible'
    _price, bid = curve.crowded_priority(7_600, {}, cu_limit, None, got['target_lamports'])
    assert bid['crowded'] == 'raised'
    assert fee.spent + bid['priority_fee_lamports'] <= 150_000_000, (got, bid)
    assert got['bounded_by_daily_cap'] is True


def test_no_allowance_left_bids_normally_without_refusing_the_entry():
    fee = _fee()
    fee._roll(fee.now_iso_fn())
    fee.spent = 150_000_000 - 2
    got = fee.arm(BUY, 'a', head_minus_s=-1)
    assert got['arm'] is None and got['reason'] == 'daily_cap'
    fee.spent = 100_000_000
    got = fee.arm(BUY, 'b', head_minus_s=-1)
    assert got['target_lamports'] == 10_000_000 and got['bounded_by_daily_cap'] is False


def test_a_treated_send_that_kept_the_normal_bid_is_no_crowded_spend(conn, tmp_path, monkeypatch):
    """A bounded target below the normal bid signs the normal bid (normal_bid_higher): that is not
    crowded spend, so it is not counted against the cap, live or on re-seed."""
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch)
    ln.build_fn = lambda buy, clip, snap, **kw: ('MESSAGE', {'priority_bid': {
        'priority_fee_lamports': 1_520_000, 'crowded': 'normal_bid_higher'}})
    monkeypatch.setattr(lane, 'utc_now_iso', lambda: '2026-09-26T20:30:00+00:00', raising=False)
    ln.handle(_buy(-1))
    assert sent == [1] and ln.crowded.spent == 0
    assert _events(conn)['crowded_fee_sent']['crowded'] == 'normal_bid_higher'
    fresh = C.CrowdedFee(now_iso_fn=lambda: '2026-09-26T21:00:00+00:00', alert_fn=lambda *a: None)
    assert fresh.seed(conn) == 0


def test_the_day_pages_once_when_bounded_sends_exhaust_the_allowance(monkeypatch):
    pages = []
    fee = _fee(pages)
    fee._roll(fee.now_iso_fn())
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 20_000_000)
    while True:
        got = fee.arm(BUY, 'treated-one', head_minus_s=-1)
        if got['arm'] is None:
            break
        _p, bid = curve.crowded_priority(7_600, {}, 400_000, None, got['target_lamports'])
        fee.sent(bid['priority_fee_lamports'])
    assert got['reason'] == 'daily_cap' and fee.spent <= 150_000_000
    assert len(pages) == 1 and fee.status['capped']


def test_zero_daily_allowance_keeps_crowded_bids_without_paging(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 0)
    monkeypatch.setattr(C, 'holdout', lambda _: False)
    pages = []
    fee = _fee(pages)
    fee.arm(BUY, 'a', head_minus_s=-1)
    fee.sent(250_000_000)
    out = fee.arm(BUY, 'b', head_minus_s=-1)
    assert out['arm'] == 'treated' and out['target_lamports'] == 10_000_000
    assert out['bounded_by_daily_cap'] is False
    assert fee.status['spent_lamports'] == 250_000_000
    assert fee.status['daily_cap_lamports'] == 0 and fee.status['capped'] is False
    assert fee.status['bound_at'] is None and pages == []


def test_unset_daily_allowance_defaults_to_unlimited(monkeypatch):
    import importlib
    monkeypatch.delenv('MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', raising=False)
    try:
        importlib.reload(cfg)
        monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS', 10_000_000)
        fee = _fee()
        fee.arm(BUY, 'a', head_minus_s=-1)
        fee.sent(250_000_000)
        assert fee.arm(BUY, 'b', head_minus_s=-1)['reason'] == 'eligible'
        assert fee.status['daily_cap_lamports'] == 0
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_seed_restores_cap_status_and_sent_count_without_double_counting(monkeypatch):
    traces = [json.dumps({'substage_events': [
        {'name': 'crowded_fee_sent', 'arm': 'treated', 'sent_lamports': 150_000_000,
         'crowded': 'raised'}]})]
    monkeypatch.setattr(m, 'slot_lane_traces_since', lambda *a: traces)
    fee = _fee()
    assert fee.seed(None) == 150_000_000
    assert fee.status['accounting_state'] == 'known'
    assert fee.status['rule'] == 'early' and fee.status['target_lamports'] == 10_000_000
    assert fee.status['daily_cap_lamports'] == 150_000_000
    assert fee.status['capped'] is True and fee.status['bound_at'] == '2026-10-15T14:00:00+00:00'
    assert fee.status['sent_treated'] == 1
    assert fee.arm(BUY, 'a', head_minus_s=-1)['reason'] == 'daily_cap'
    assert fee.seed(None) == 150_000_000 and fee.status['sent_treated'] == 1
    monkeypatch.setattr(m, 'slot_lane_traces_since', lambda *a: ['{broken'])
    assert fee.seed(None) is None
    assert fee.status['capped'] is None and fee.status['bound_at'] is None
    assert fee.status['spent_lamports'] is None and fee.status['sent_treated'] is None
    assert fee.status['accounting_state'] == 'unavailable'


@pytest.mark.parametrize('traces', [None, [None], [''], ['{}'], ['{broken'], ['[]'], ['42'],
                                  ['{"substage_events":null}'], ['{"substage_events":{}}'],
                                  [json.dumps({'substage_events': [
                                      {'name': 'crowded_fee_sent', 'arm': 'treated', 'crowded': 'raised'}]})]])
def test_unknown_accounting_blocks_only_finite_crowded_bids_and_can_recover(monkeypatch, traces):
    def read(*a):
        if traces is None:
            raise sqlite3.OperationalError('fixture read failure')
        return traces
    monkeypatch.setattr(m, 'slot_lane_traces_since', read)
    monkeypatch.setattr(C, 'holdout', lambda _: False)
    fee = _fee()
    assert fee.seed(None) is None
    assert fee.spent is None and fee.status['spent_lamports'] is None
    assert fee.status['sent_treated'] is None and fee.status['accounting_state'] == 'unavailable'
    out = fee.arm(BUY, 'a', head_minus_s=-1)
    assert out['arm'] is None and out['reason'] == 'accounting_unavailable'
    assert fee.status['capped'] is None
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 0)
    assert fee.arm(BUY, 'b', head_minus_s=-1)['arm'] == 'treated'
    fee.sent(10_000_000)
    assert fee.status['spent_lamports'] is None and fee.status['capped'] is False
    monkeypatch.setattr(m, 'slot_lane_traces_since', lambda *a: [])
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 150_000_000)
    assert fee.seed(None) == 0 and fee.status['accounting_state'] == 'known'
    assert fee.arm(BUY, 'c', head_minus_s=-1)['arm'] == 'treated'


def test_hot_daily_limit_changes_update_status_and_restore_bids(monkeypatch):
    fee = _fee()
    fee.arm(BUY, 'a', head_minus_s=-1)
    fee.sent(100_000_000)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 50_000_000)
    assert fee.arm(BUY, 'b', head_minus_s=-1)['reason'] == 'daily_cap'
    assert fee.status['capped'] is True and fee.status['bound_at'] is not None
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS', 200_000_000)
    assert fee.arm(BUY, 'c', head_minus_s=-1)['reason'] == 'eligible'
    assert fee.status['capped'] is False and fee.status['bound_at'] is None


def test_unavailable_finite_budget_still_sends_normal_entry_and_exports_unknown(conn, tmp_path, monkeypatch):
    from sniper.services import memecoin_runner as run
    ln, sent, builds = _lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(m, 'slot_lane_traces_since', lambda *a: ['{broken'])
    ln.crowded.seed(conn)
    ln.handle(_buy(-1))
    assert sent == [1] and builds == [{}]
    assert _events(conn)['crowded_fee']['reason'] == 'accounting_unavailable'
    path = tmp_path / 'hb.json'
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={'crowded_fee': ln.crowded.status})
    got = json.loads(path.read_text())['slot_lane']['crowded_fee']
    assert got['accounting_state'] == 'unavailable'
    assert got['spent_lamports'] is None and got['capped'] is None


def test_midnight_clears_unavailable_accounting_and_daily_send_count(monkeypatch):
    clock = {'now': '2026-10-15T23:59:00+00:00'}
    fee = C.CrowdedFee(now_iso_fn=lambda: clock['now'], alert_fn=lambda *a: None)
    monkeypatch.setattr(m, 'slot_lane_traces_since', lambda *a: ['{broken'])
    fee.seed(None)
    clock['now'] = '2026-10-16T00:00:01+00:00'
    assert fee.arm(BUY, 'a', head_minus_s=-1)['reason'] == 'eligible'
    assert fee.status['accounting_state'] == 'known' and fee.status['spent_lamports'] == 0
    assert fee.status['sent_treated'] == 0 and fee.status['bound_at'] is None


def test_midnight_send_keeps_its_decision_day_matching_reseed(conn):
    clock = {'now': '2026-10-15T23:59:59+00:00'}
    fee = C.CrowdedFee(now_iso_fn=lambda: clock['now'], alert_fn=lambda *a: None)
    assert fee.seed(conn) == 0
    assert fee.arm(BUY, 'midnight', head_minus_s=-1)['reason'] == 'eligible'
    m.start_entry_attempt(conn, attempt_id='midnight', decision_started_at=clock['now'],
                          network_id='solana', mint='M', signal_source='slot_lane',
                          delivery_lane='slot_lane_processed')
    clock['now'] = '2026-10-16T00:00:00+00:00'
    trace = {'substage_events': [
        {'name': 'crowded_fee_sent', 'arm': 'treated', 'sent_lamports': 10_000_000,
         'crowded': 'raised'}]}
    m.finish_entry_attempt(conn, 'midnight', 'failed', 'chain_error:x', clock['now'],
                           json.dumps(trace), tx_sig='MidnightSig')
    fee.sent(10_000_000)
    previous = _fee(now='2026-10-15T23:59:59+00:00')
    assert previous.seed(conn) == fee.spent == 10_000_000
    assert fee.status['day'] == previous.status['day'] == '2026-10-15'
    fresh = _fee(now=clock['now'])
    assert fresh.seed(conn) == 0
    assert fee.arm(BUY, 'next-day', head_minus_s=-1)['reason'] == 'eligible'
    assert fee.status['day'] == fresh.status['day'] == '2026-10-16'
    assert fee.spent == fresh.spent == 0 and fee.status['sent_treated'] == 0
