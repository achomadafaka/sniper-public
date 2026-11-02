"""RabbitStream stage 2 commit 3: source_not_landed (operator 2026-10-15, live), the hot kill and the
breaker. A rabbit-led fire whose source buy fails or never lands is released before it signs and
exited after it fills; a read error is unknown and never sells; only the fire's own position exits."""
import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_source_sell_mirror as ssm
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_signal_stage1 import CURVE, frame, owner
from test_memecoin_rabbit_signal_stage2 import (_buyer, _curve_candidate, _offer, _rabbit_buy,
                                                 backstop, fire_frame, fire_rpc, FIRE, FIRE_MINT)

SIG, OWNER, UID, MINT, SLOT = 'Sig1111', 'Owner111', 'uid1', 'Mint111', 1000


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N', 3)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BREAKER_WINDOW_MIN', 15.0)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BREAKER_PAUSE_MIN', 60.0)


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def monotonic(self):
        return self.t


def processed(signature=SIG, *, err=False):
    import base58
    update = pb.SubscribeUpdate()
    info = update.transaction.transaction
    info.signature = base58.b58decode(signature.ljust(88, '1'))[:64].rjust(64, b'\0')
    info.meta.fee = 5000
    if err:
        info.meta.err.err = b'\x01'
    return update, base58.b58encode(info.signature).decode()


def watch(*, statuses=None, head=(None, None), clock=None, node_slot=SLOT + 4):
    """A LandingWatch whose getSignatureStatuses answers from `statuses` (a list: dict, None or an
    exception per call), the read node at `node_slot`."""
    calls = []
    answers = list(statuses or [])

    def read(method, params):
        calls.append((method, params))
        assert method == 'getSignatureStatuses' and params[1] == {'searchTransactionHistory': False}
        answer = answers.pop(0) if answers else None
        if isinstance(answer, Exception):
            raise answer
        return {'context': {'slot': node_slot}, 'value': [answer]}
    exits = []
    rabbit = backstop(read, exits)
    stats = {}
    w = R.LandingWatch(backstop=rabbit, head_fn=lambda: head, status=stats, clock=clock or time)
    return w, rabbit, exits, calls, stats


def expect(w, signature, *, mono=None):
    w.expect(signature=signature, owner=OWNER, source_user_id=UID, mint=MINT, slot=SLOT,
             frame_mono=mono if mono is not None else time.monotonic())


async def settle(w, rounds=200):
    for _ in range(rounds):
        for rec in w.due():
            w._checking.add(rec['signature'])
            task = asyncio.get_running_loop().create_task(w._confirm(rec['signature'], rec))
            w._tasks.add(task)
            task.add_done_callback(w._tasks.discard)
        if not w._watch and not w._tasks:
            return
        await asyncio.sleep(0.005)


# --- what "landed" means ----------------------------------------------------------------------------

def test_a_processed_success_lands_the_source_and_nothing_exits():
    w, rabbit, exits, calls, stats = watch()
    update, sig = processed()
    expect(w, sig)
    w.observe(update, time.monotonic())
    assert stats['landed'] == 1 and exits == [] and rabbit.not_landed(sig) is None and calls == []


def test_a_processed_failure_is_source_failed_at_once_with_the_rabbit_lead():
    w, rabbit, exits, calls, stats = watch()
    update, sig = processed(err=True)
    expect(w, sig, mono=50.0)
    w.observe(update, 50.312)
    assert stats['source_failed'] == 1 and calls == []
    assert rabbit.not_landed(sig) == R.SOURCE_FAILED
    [verdict] = exits
    assert verdict['reason'] == 'source_failed' and verdict['basis'] == 'processed_feed'
    assert verdict['lead_ms'] == pytest.approx(312.0) and verdict['mint'] == MINT


def test_a_failure_seen_before_the_fire_blocks_the_fire_without_a_read():
    calls = []
    buyer, stats, fired = _buyer(fire_rpc(calls=calls))
    update, _ = processed(CURVE['signature'], err=True)
    update.transaction.transaction.signature = fire_frame().transaction.transaction.signature
    buyer.landing.observe(update, time.monotonic())
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and stats['refused'] == {'source_failed': 1} and calls == []


def test_a_failure_seen_during_the_read_blocks_the_fire():
    buyer, stats, fired = _buyer(fire_rpc())
    update, _ = processed(CURVE['signature'], err=True)
    update.transaction.transaction.signature = fire_frame().transaction.transaction.signature
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame(),
                       before_read=lambda: buyer.landing.observe(update, time.monotonic())))
    assert fired == [] and stats['refused'] == {'source_failed': 1}


def test_no_sign_by_s_plus_4_and_two_null_statuses_is_source_not_landed():
    clock = Clock()
    w, rabbit, exits, calls, stats = watch(statuses=[None, None], head=(SLOT + 3, 100.0), clock=clock)
    expect(w, 'SigA', mono=100.0)
    assert w.due() == []                               # head S+3: not due yet
    w.head_fn = lambda: (SLOT + 4, 100.0)
    asyncio.run(settle(w))
    assert len(calls) == 2 and stats['source_not_landed'] == 1
    assert exits[0]['reason'] == 'source_not_landed' and exits[0]['lead_ms'] is None
    assert exits[0]['basis'] == 'signature_status_null'


def test_a_null_then_a_landed_status_is_a_feed_miss_and_never_exits():
    w, rabbit, exits, calls, stats = watch(statuses=[None, {'slot': SLOT, 'err': None}],
                                           head=(SLOT + 4, time.monotonic()))
    expect(w, 'SigB')
    asyncio.run(settle(w))
    assert stats['feed_missed'] == 1 and exits == [] and rabbit.not_landed('SigB') is None


def test_a_status_with_an_error_is_source_failed():
    w, rabbit, exits, calls, stats = watch(statuses=[{'slot': SLOT, 'err': {'InstructionError': [3, 1]}}],
                                           head=(SLOT + 4, time.monotonic()))
    expect(w, 'SigC')
    asyncio.run(settle(w))
    assert exits[0]['reason'] == 'source_failed' and exits[0]['basis'] == 'signature_status_err'


def test_a_read_error_is_retried_and_never_reads_as_not_landed(monkeypatch):
    monkeypatch.setattr(R.LandingWatch, 'ERROR_RETRY_S', (0.001, 0.001))
    monkeypatch.setattr(R.LandingWatch, 'NULL_RECHECK_S', 0.001)
    w, rabbit, exits, calls, stats = watch(statuses=[RuntimeError('429'), {'slot': SLOT, 'err': None}],
                                           head=(SLOT + 4, time.monotonic()))
    expect(w, 'SigD')
    asyncio.run(settle(w))
    assert len(calls) == 2 and stats['check_errors'] == 1 and stats['feed_missed'] == 1 and exits == []


def test_a_read_that_keeps_failing_is_unknown_and_the_position_keeps_its_rails(monkeypatch):
    monkeypatch.setattr(R.LandingWatch, 'ERROR_RETRY_S', (0.001, 0.001))
    monkeypatch.setattr(R.LandingWatch, 'UNKNOWN_AFTER_S', 0.05)
    w, rabbit, exits, calls, stats = watch(statuses=[RuntimeError('timeout')] * 1000,
                                           head=(SLOT + 4, time.monotonic()))
    expect(w, 'SigE')
    asyncio.run(settle(w, rounds=400))
    assert stats['unknown'] == 1 and exits == [] and rabbit.not_landed('SigE') is None


@pytest.mark.parametrize('node_slot', [SLOT - 5, SLOT + 3, None])
def test_a_null_from_a_read_node_short_of_s_plus_4_is_unknown_not_landed(monkeypatch, node_slot):
    """Gamma MEDIUM 2026-10-15: a lagging node answers null for a source that DID land; with both
    feeds missing the frame that used to sell a good copy."""
    monkeypatch.setattr(R.LandingWatch, 'ERROR_RETRY_S', (0.001, 0.001))
    monkeypatch.setattr(R.LandingWatch, 'UNKNOWN_AFTER_S', 0.05)
    w, rabbit, exits, calls, stats = watch(statuses=[None] * 1000, head=(SLOT + 4, time.monotonic()),
                                           node_slot=node_slot)
    expect(w, 'SigH')
    asyncio.run(settle(w, rounds=400))
    assert exits == [] and stats['source_not_landed'] == 0 and stats['unknown'] == 1
    assert stats['node_behind'] == len(calls) >= 2


def test_a_stale_head_waits_for_the_wall_cap():
    clock = Clock(100.0)
    w, *_ = watch(head=(SLOT + 9, 90.0), clock=clock)            # head seen 10 s ago: stale
    expect(w, 'SigF', mono=100.0)
    assert w.due() == []
    clock.t = 100.0 + R.LandingWatch.MAX_WAIT_S
    assert [r['signature'] for r in w.due()] == ['SigF']


def test_a_retry_under_a_new_signature_keeps_the_position():
    w, rabbit, exits, calls, stats = watch(statuses=[None, None], head=(SLOT + 4, time.monotonic()))
    expect(w, 'SigG', mono=time.monotonic())
    w.observe_buy(OWNER, MINT, time.monotonic() + 0.1,           # the owner's second try landed,
                  signature='SigG2', slot=SLOT + 1)              # in a later slot (Codex F3)
    asyncio.run(settle(w))
    assert stats['replaced'] == 1 and exits == []


def test_a_fire_registers_its_source_before_it_goes_to_the_lane():
    buyer, stats, fired = _buyer(fire_rpc())
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert len(fired) == 1 and FIRE['signature'] in buyer.landing._watch
    rec = buyer.landing._watch[FIRE['signature']]
    assert (rec['owner'], rec['mint'], rec['slot']) == (FIRE['owner'], FIRE_MINT, FIRE['slot'])


# --- the hot kill and the breaker -------------------------------------------------------------------

def test_the_hot_knob_holds_fires_per_signal_without_a_restart(monkeypatch):
    calls = []
    buyer, stats, fired = _buyer(fire_rpc(calls=calls))
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', False)
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and calls == [] and stats['held'] == 1
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', True)
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert len(fired) == 1


def test_the_cockpit_overlay_lands_on_the_knob_and_a_missing_row_keeps_env(monkeypatch, tmp_path):
    from sniper.services import memecoin_entry_knobs as knobs
    conn = sqlite3.connect(tmp_path / 'ledger.db')
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(knobs, '_DEFAULTS', {})
    for name in knobs.KNOBS:                         # whatever apply writes, the test restores
        monkeypatch.setattr(cfg, name, getattr(cfg, name, None))
    rabbit = backstop(lambda *a: None)
    assert knobs.apply_overrides(conn, cfg)['deferred'] is False
    assert rabbit.allowed() is True                  # no row: the .env value stands
    conn.execute("INSERT INTO memecoin_entry_knob_overrides(name, value, active) VALUES (?,?,1)",
                 ('MEMECOIN_SLOT_LANE_RABBIT_BUY', json.dumps(False)))
    knobs.apply_overrides(conn, cfg)
    assert rabbit.allowed() is False and rabbit.status['knob'] is False
    conn.execute("UPDATE memecoin_entry_knob_overrides SET active=0")
    knobs.apply_overrides(conn, cfg)
    assert rabbit.allowed() is True


def _verdict(rabbit, sig, reason=R.SOURCE_NOT_LANDED):
    rabbit.verdict(dict(signature=sig, reason=reason, owner=OWNER, source_user_id=UID, mint=MINT,
                        slot=SLOT, frame_mono=0.0, basis='x', lead_ms=None, age_ms=900.0))


def test_three_verdicts_in_fifteen_minutes_pause_fires_and_page_once():
    clock, pages = Clock(1000.0), []
    rabbit = R.RabbitBackstop(read=None, exit_offer=lambda rec: None, clock=clock,
                              alert_fn=lambda kind, text: pages.append((kind, text)))
    _verdict(rabbit, 'a', R.SOURCE_FAILED)
    rabbit.record_cost('a', -1.25)
    clock.t += 60
    _verdict(rabbit, 'b')
    assert rabbit.allowed() is True and pages == []
    clock.t += 60
    _verdict(rabbit, 'c')
    _verdict(rabbit, 'd')                                     # a fourth inside the pause: no page
    assert rabbit.allowed() is False and rabbit.status['paused'] is True and len(pages) == 1
    kind, text = pages[0]
    assert kind == 'SLOT-LANE RABBIT BREAKER'
    assert 'source_failed 1' in text and 'source_not_landed 2' in text and '$-1.25 over 1 closed' in text
    assert 'restart clears the pause' in text
    clock.t += 3600
    assert rabbit.allowed() is True and rabbit.status['last_resume'] == 'pause_elapsed'


def test_verdicts_spread_past_the_window_never_trip():
    clock, pages = Clock(0.0), []
    rabbit = R.RabbitBackstop(read=None, exit_offer=lambda rec: None, clock=clock,
                              alert_fn=lambda *a: pages.append(a))
    for sig in 'abc':
        _verdict(rabbit, sig)
        clock.t += 8 * 60                                     # 16 min between the first and third
    assert rabbit.allowed() is True and pages == [] and rabbit.status['window'] == 2


def test_turning_the_knob_off_is_the_manual_resume(monkeypatch):
    rabbit = R.RabbitBackstop(read=None, exit_offer=lambda rec: None, clock=Clock(), alert_fn=lambda *a: None)
    for sig in 'abc':
        _verdict(rabbit, sig)
    assert rabbit.allowed() is False
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', False)
    assert rabbit.allowed() is False and rabbit.status['last_resume'] == 'knob_off'
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', True)
    assert rabbit.allowed() is True


def test_landing_slots_and_fills_are_measured_per_fire():
    rabbit = backstop(lambda *a: None)
    rabbit.note_result({'status': 'filled', 'sig': 'x', 'landing_slot': SLOT}, SLOT)
    rabbit.note_result({'status': 'filled', 'sig': 'y', 'landing_slot': SLOT + 1}, SLOT)
    rabbit.note_result({'status': 'failed', 'sig': 'z', 'landing_slot': SLOT + 2}, SLOT)
    assert rabbit.status['fills'] == 2 and rabbit.status['sent'] == 3
    assert rabbit.status['land_minus_s'] == {'n': 3, 'p50': 1, 'p90': 2, 'hist': {'0': 1, '1': 1, '2': 1}}


# --- the lane: never sign after a verdict -----------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'ledger.db')
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _lane(conn, tmp_path, monkeypatch, *, result=None):
    from test_memecoin_slot_lane import _live_lane
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    if result is not None:
        def execute(*a, **k):
            sent.append(1)
            return dict(result)
        ln.executor = {'slot_execute_fn': execute}
    ln.rabbit_backstop = backstop(lambda *a: None)
    return ln, sent


def test_a_verdict_before_signing_releases_the_reservation_and_sends_nothing(conn, tmp_path, monkeypatch):
    ln, sent = _lane(conn, tmp_path, monkeypatch)
    buy = _rabbit_buy()
    ln.rabbit_backstop._verdicts[buy.signature] = R.SOURCE_FAILED
    result = ln.handle(buy)
    assert result['status'] == 'rejected' and result['reason'] == 'source_failed' and sent == []
    row = conn.execute('SELECT status, resolution_reason FROM memecoin_pending_entries').fetchone()
    assert row['status'] != 'reserved' and row['resolution_reason'] == 'source_failed:unsigned'


def test_the_lane_reports_each_rabbit_fire_with_its_landing_slot(conn, tmp_path, monkeypatch):
    buy = _rabbit_buy()
    ln, sent = _lane(conn, tmp_path, monkeypatch,
                     result={'status': 'filled', 'sig': 'Our1', 'landing_slot': buy.slot + 1})
    assert ln.handle(buy)['status'] == 'filled'
    assert ln.rabbit_backstop.status['fills'] == 1
    assert ln.rabbit_backstop.status['land_minus_s']['hist'] == {'1': 1}


# --- the mirror: exit only this fire's position -----------------------------------------------------

@pytest.fixture
def world(monkeypatch):
    c = sqlite3.connect(':memory:', check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    calls, lock = [], threading.Lock()

    def exit_fn(conn, pos, reason, now):
        calls.append((pos['id'], reason, lock.locked()))
        return {'status': 'closed', 'realized': -0.42}
    monkeypatch.setattr(rr, '_alert_exit_result', lambda *a, **k: None)
    monkeypatch.setattr(rr, 'check_wallet_demotion', lambda *a, **k: False)
    clock = SimpleNamespace(t=100.0)
    mirror = ssm.SourceSellMirror(enabled=True, shadow=True, min_fraction=0.30,     # shadow on purpose
                                  clock=lambda: clock.t, real_now_fn=lambda: '2026-10-15T12:00:01+00:00')
    mirror.rabbit_backstop = backstop(lambda *a: None)
    return SimpleNamespace(conn=c, calls=calls, lock=lock, clock=clock, mirror=mirror,
                           contexts={'solana': {'exit_fn': exit_fn}})


def _position(w, *, origin='slotlane:' + SIG, lane_name=R.DELIVERY_LANE, attempt='att1'):
    m.start_entry_attempt(w.conn, attempt_id=attempt, decision_started_at='2026-10-15T12:00:00+00:00',
                          origin_event_id=origin, signal_source='slot_lane', network_id='solana', mint=MINT,
                          source_user_id=UID, delivery_lane=lane_name)
    return m.open_position(w.conn, source_user_id=UID, source_handle='h', mint=MINT, ticker='T',
                           entry_price=1e-5, entry_notional=25.0, entry_fill_source='chain', entry_lag_s=0.5,
                           their_size_usd=100.0, opened_at='2026-10-15T12:00:00+00:00',
                           strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode='real', network_id='solana',
                           origin_event_id=origin, attempt_id=attempt, signal_source='slot_lane')


def _not_landed(w, reason=R.SOURCE_NOT_LANDED, lead=None):
    w.mirror.offer_not_landed(dict(signature=SIG, reason=reason, owner=OWNER, source_user_id=UID, mint=MINT,
                                   slot=SLOT, basis='signature_status_null', lead_ms=lead, age_ms=910.0))
    return w.mirror.run(w.conn, w.contexts, exit_lock=w.lock)


def test_the_fires_own_position_exits_at_once_under_the_lock_even_in_mirror_shadow(world):
    pid = _position(world)
    [rec] = _not_landed(world, R.SOURCE_FAILED, lead=312.0)
    assert rec['outcome'] == 'not_landed_exit' and world.calls == [(pid, 'source_failed', True)]
    row = world.conn.execute('SELECT allowed, detail, lane FROM memecoin_exit_trigger_audit WHERE position_id=? '
                             'AND reason=?', (pid, 'source_failed')).fetchone()
    detail = json.loads(row['detail'])
    assert row['lane'] == 'rabbit_landing' and row['allowed'] == 1
    assert detail['rabbit_lead_ms'] == 312.0 and detail['source_signature'] == SIG
    assert world.mirror.rabbit_backstop.status['realized_usd'] == -0.42


def test_an_older_position_of_the_same_coin_is_never_exited(world):
    _position(world, origin='slotlane:EarlierSig')
    assert _not_landed(world) == []                       # waiting for this fire's own position
    world.clock.t += ssm.NOT_LANDED_WINDOW_S
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'not_landed_no_position' and world.calls == []


def test_a_processed_path_position_is_never_exited(world):
    _position(world, lane_name='slot_lane_processed')
    world.clock.t += ssm.NOT_LANDED_WINDOW_S
    _not_landed(world)
    world.clock.t += ssm.NOT_LANDED_WINDOW_S
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.calls == []


def test_a_position_that_opens_after_the_verdict_still_exits(world):
    assert _not_landed(world) == []                       # our entry still confirming
    world.clock.t += 2.0
    pid = _position(world)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'not_landed_exit' and world.calls == [(pid, 'source_not_landed', True)]


def test_a_stuck_exit_is_retried_with_its_own_reason(world):
    pid = _position(world)
    answers = [{'status': 'stuck'}, {'status': 'closed', 'realized': -0.1}]
    world.contexts['solana']['exit_fn'] = lambda conn, pos, reason, now: (
        world.calls.append((pos['id'], reason)) or answers.pop(0))
    _not_landed(world)
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.calls == [(pid, 'source_not_landed'), (pid, 'source_not_landed')]


def test_the_exit_card_names_the_verdict_and_the_rabbit_lead(world):
    from sniper.persistence.db_memecoin_notifications import notification_exit_view
    from sniper.services.memecoin_telegram import render_entry_notification
    pid = _position(world)
    _not_landed(world, R.SOURCE_FAILED, lead=312.0)
    world.conn.execute("UPDATE memecoin_positions SET exit_reason='source_failed', is_open=0 WHERE id=?", (pid,))
    world.conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, notional, created_at) "
                       "VALUES (?, 'bracket_exit', 1e-5, 24.5, '2026-10-15T12:00:02+00:00')", (pid,))
    fill_id = world.conn.execute('SELECT MAX(id) FROM memecoin_fills').fetchone()[0]
    view = notification_exit_view(world.conn, fill_id)
    assert view['rabbit_landing']['lead_ms'] == 312.0
    text = render_entry_notification(view)
    assert 'source_failed (rabbit lead 312 ms)' in text


def test_heartbeat_reports_landing_and_breaker(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    rabbit = backstop(lambda *a: None)
    rabbit.note_result({'status': 'filled', 'sig': 'x', 'landing_slot': SLOT + 1}, SLOT)
    path = tmp_path / 'heartbeat'
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={
        'rabbit_breaker': rabbit.status,
        'signal': {'rabbit_landing': dict(watched=2, landed=1, source_failed=1, source_not_landed=0)}})
    row = json.loads(path.read_text())['slot_lane']
    assert row['rabbit_landing']['landed'] == 1 and row['rabbit_landing']['source_failed'] == 1
    assert row['rabbit_breaker']['fills'] == 1 and row['rabbit_breaker']['paused'] is False
    assert row['rabbit_breaker']['land_minus_s']['hist'] == {'1': 1}


# --- the lead measurement (Codex P2s on stage 1) ----------------------------------------------------

def test_an_owner_signed_frame_with_lookup_tables_is_stamped_before_they_are_resolved():
    from sniper.services import memecoin_shyft_prepare as prep
    update = frame(CURVE)
    msg = update.transaction.transaction.transaction.message
    msg.address_table_lookups.add(account_key=bytes(range(32)), writable_indexes=b'\x00', readonly_indexes=b'')
    stamped, stats = [], {}
    got = prep.preparation_candidate(update, {owner(CURVE): ('uid', 'h')}, recv_monotonic=7.0, generation=2,
                                     stats=stats, on_owner_frame=lambda *a: stamped.append(a))
    assert got is None and stats.get('alt') == 1                      # preparation still refused
    assert stamped == [(CURVE['signature'], owner(CURVE), CURVE['slot'], 7.0, 2)]


def test_only_decoded_buys_pair_and_a_late_decode_pairs_on_receipt_stamps():
    from sniper.services.memecoin_feed_arrivals import StampedArrivals
    a = StampedArrivals(('shyft:p', 'rabbit:r'))
    a.stamp('transfer', 'o', 'rabbit:r', 10.000)          # an owner-signed transfer, both feeds
    a.stamp('transfer', 'o', 'shyft:p', 10.005)
    a.stamp('buy', 'o', 'rabbit:r', 20.000)                # a routed buy, decoded seconds later
    a.stamp('buy', 'o', 'shyft:p', 20.040)
    a.mark_buy('buy', 'o')
    buys = a.snapshot(now=21)['pairs']['shyft:p|rabbit:r']
    assert buys['n'] == 1 and buys['p50_ms'] == pytest.approx(-40.0)  # rabbit 40 ms first, at receipt
    assert a.raw.snapshot(now=21)['pairs']['shyft:p|rabbit:r']['n'] == 2
    assert a.snapshot(now=21)['winners'] == {'shyft:p': 0, 'rabbit:r': 1}
    assert a.snapshot(now=21)['scope'] == 'decoded_buys_stamped_at_receipt'


def test_the_signal_loop_stamps_both_feeds_at_receipt_and_pairs_the_rabbit_decoded_buy(monkeypatch):
    from sniper.services import memecoin_shyft_prepare as prep
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    stop, status = threading.Event(), {}
    t0 = time.monotonic()
    owners = {owner(CURVE): ('uid', 'h')}

    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, *, on_owner_frame=None, **kw):
        got = prep.preparation_candidate(frame(CURVE), owners_fn(), recv_monotonic=t0, generation=1,
                                         on_owner_frame=on_owner_frame)
        on_prepare(got)
        while not stop.is_set():
            await asyncio.sleep(.01)

    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None, **kwargs):
        await asyncio.sleep(.02)
        on_metadata(frame(CURVE), recv_monotonic=t0 + .031, recv_utc='', head_slot=None, head_mono=None,
                    native_buys=(), receipt_owners=owners)
        await asyncio.sleep(.05)
        stop.set()
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(lambda b: None, lambda: owners, None, status, stop, None, None, None, None,
        ('token', ('https://grpc.ams.shyft.to',)), infra_provider='shyft',
        prepare_config=('token', 'https://rabbitstream.fra.shyft.to'), on_prepare=lambda c: None))
    pair = status['arrival_metrics'].snapshot(now=t0 + 1)['pairs'][
        'shyft:grpc.ams.shyft.to|rabbit:rabbitstream.fra.shyft.to']
    assert pair['n'] == 1 and pair['p50_ms'] == pytest.approx(-31.0, abs=0.5)
    assert status['raw_arrival_metrics'].snapshot(now=t0 + 1)['samples'] == 1


def test_lookup_table_frames_are_counted_by_how_they_arrive(tmp_path):
    from sniper.services import memecoin_shyft_prepare as prep
    from sniper.services.memecoin_runner import write_heartbeat
    update = frame(CURVE)
    msg = update.transaction.transaction.transaction.message
    msg.address_table_lookups.add(account_key=bytes(range(32)), writable_indexes=b'\x00', readonly_indexes=b'')
    stats, shapes = {}, {}
    prep.preparation_candidate(update, {owner(CURVE): ('uid', 'h')}, recv_monotonic=0.0, generation=1,
                               stats=stats, shapes=shapes)
    update.transaction.transaction.meta.loaded_writable_addresses.append(bytes([7]) * 32)
    prep.preparation_candidate(update, {owner(CURVE): ('uid', 'h')}, recv_monotonic=0.0, generation=1,
                               stats=stats, shapes=shapes)
    assert shapes == {'alt_frames': 2, 'alt_with_meta': 1, 'alt_resolved': 1}
    assert stats == {'alt': 1}                      # a shape is never a miss reason
    path = tmp_path / 'hb'
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={'signal': {'preparation': {
        'pending': {}, 'feed': dict(endpoint_hostname='rabbitstream.fra.shyft.to', miss_reasons=stats,
                                    alt_frames=shapes)}}})
    feed = json.loads(path.read_text())['slot_lane']['preparation']['feed']
    assert feed['alt_frames'] == {'alt_frames': 2, 'alt_with_meta': 1, 'alt_resolved': 1}


# --- F2 (Codex review through 648ada5d7): the source-failure exit survives a blocking claim ---------
# A source_failed / source_not_landed verdict is offered ONCE (LandingWatch drops its watch, the
# backstop caches the verdict). Discarding it on a close/derisk claim collision left the still-open
# owned position without its protective exit once the partial confirmed or the close failed.

def test_a_partial_in_flight_holds_the_verdict_and_the_exit_runs_once_it_resolves(world):
    """The Codex probe: a partial de-risk claim in flight, then cleared (confirmed or released)."""
    pid = _position(world)
    claim = m.claim_position_derisk(world.conn, pid, '2026-10-15T12:00:01+00:00')
    assert claim
    [rec] = _not_landed(world, R.SOURCE_FAILED)
    assert rec['outcome'] == 'close_in_flight' and rec['held'] and world.calls == []
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []   # reported once
    world.clock.t += ssm.NOT_LANDED_WINDOW_S * 4                                       # no deadline while held
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == [] and world.calls == []
    assert m.release_position_derisk_claim(world.conn, pid, claim)
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'not_landed_exit' and world.calls == [(pid, 'source_failed', True)]
    world.clock.t += ssm.NOT_LANDED_WINDOW_S
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []    # fires once (Gamma)
    assert world.calls == [(pid, 'source_failed', True)]


def test_a_failed_full_close_releases_its_claim_and_the_held_exit_runs(world):
    pid = _position(world)
    claim = m.claim_position_close(world.conn, pid, '2026-10-15T12:00:01+00:00', 'b_stop')
    [rec] = _not_landed(world)
    assert rec['outcome'] == 'close_in_flight' and world.calls == []
    assert m.release_position_close_claim(world.conn, pid, claim)      # that close failed unsigned
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'not_landed_exit' and world.calls == [(pid, 'source_not_landed', True)]


def test_a_signed_claim_stays_held_and_a_position_that_closes_drops_the_verdict(world):
    pid = _position(world)
    world.conn.execute("UPDATE memecoin_positions SET close_claim_id='c', close_sig='SignedSig' WHERE id=?", (pid,))
    world.conn.commit()
    [rec] = _not_landed(world)
    assert rec['outcome'] == 'close_in_flight'
    world.clock.t += 600.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == [] and world.calls == []
    world.conn.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))   # that close landed
    world.conn.commit()
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'closed_while_held' and world.calls == []
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []


def test_a_source_failure_retry_that_meets_a_claim_is_held_not_dropped(world):
    pid = _position(world)
    answers = [{'status': 'stuck'}, {'status': 'closed', 'realized': -0.1}]
    world.contexts['solana']['exit_fn'] = lambda conn, pos, reason, now: (
        world.calls.append((pos['id'], reason)) or answers.pop(0))
    _not_landed(world)
    claim = m.claim_position_derisk(world.conn, pid, '2026-10-15T12:00:02+00:00')
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'retry_held' and world.calls == [(pid, 'source_not_landed')]
    world.clock.t += 30.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []    # still held, quiet
    m.release_position_derisk_claim(world.conn, pid, claim)
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'mirror_retry' and rec['exit_status'] == 'closed'
    assert world.calls == [(pid, 'source_not_landed'), (pid, 'source_not_landed')]
# --- F3 (Codex review through 648ada5d7): only a proven LATER distinct buy replaces a failed source --
# Receipt order is not chain order: a delayed processed copy of the owner's slot S-1 buy can arrive
# after the failed slot-S rabbit frame. The replacement needs another signature in a later slot;
# same-slot order is unknown pre-execution (a shred carries no transaction index), so it keeps
# the verdict, as does a buy whose slot is unknown.

def _failed_with(buy_kw):
    clock = SimpleNamespace(monotonic=lambda: 102.0)
    verdicts = []
    w = R.LandingWatch(backstop=SimpleNamespace(verdict=verdicts.append), head_fn=lambda: (SLOT + 4, 102.0),
                       status={}, clock=clock)
    w.expect(signature='SigFail', owner=OWNER, source_user_id=UID, mint=MINT, slot=SLOT, frame_mono=100.0)
    if buy_kw is not None:
        w.observe_buy(OWNER, MINT, 100.1, **buy_kw)
    w._verdict(w._watch['SigFail'], R.SOURCE_FAILED, 'processed_feed')
    return verdicts, w.stats


@pytest.mark.parametrize('buy_kw', [
    dict(signature='SigOld', slot=SLOT - 1),      # the Codex probe: a delayed older buy
    dict(signature='SigSame', slot=SLOT),         # same slot: order unknown
    dict(signature='SigFail', slot=SLOT + 2),     # the watched transaction itself
    dict(signature='SigNoSlot', slot=None),       # order unknown
    dict(),                                       # an old caller with no identity at all
])
def test_a_buy_not_proven_later_keeps_the_verdict(buy_kw):
    verdicts, stats = _failed_with(buy_kw)
    assert len(verdicts) == 1 and verdicts[0]['reason'] == R.SOURCE_FAILED and stats['replaced'] == 0


def test_a_distinct_buy_in_a_later_slot_replaces_the_failed_source():
    verdicts, stats = _failed_with(dict(signature='SigRetry', slot=SLOT + 1))
    assert verdicts == [] and stats['replaced'] == 1


def test_an_older_buy_does_not_hide_a_later_one_of_the_same_coin():
    clock = SimpleNamespace(monotonic=lambda: 102.0)
    verdicts = []
    w = R.LandingWatch(backstop=SimpleNamespace(verdict=verdicts.append), head_fn=lambda: (SLOT + 4, 102.0),
                       status={}, clock=clock)
    w.expect(signature='SigFail', owner=OWNER, source_user_id=UID, mint=MINT, slot=SLOT, frame_mono=100.0)
    w.observe_buy(OWNER, MINT, 100.1, signature='SigRetry', slot=SLOT + 1)
    w.observe_buy(OWNER, MINT, 100.2, signature='SigOld', slot=SLOT - 1)    # delivered last, older
    w._verdict(w._watch['SigFail'], R.SOURCE_FAILED, 'processed_feed')
    assert verdicts == [] and w.stats['replaced'] == 1


def test_every_slot_signal_sink_hands_the_landing_watch_its_signature_and_slot():
    """The sinks are closures inside start_signal_thread; an observe_buy without the chain order
    silently degrades every replacement to 'unknown, keep the verdict' (F3's other half)."""
    import ast
    import inspect
    from sniper.services import memecoin_slot_signal as S
    calls = [n for n in ast.walk(ast.parse(inspect.getsource(S)))
             if isinstance(n, ast.Call) and getattr(n.func, 'attr', None) == 'observe_buy']
    assert len(calls) >= 2
    for call in calls:
        assert {k.arg for k in call.keywords} >= {'signature', 'slot'}, ast.unparse(call)


# --- F2 verification (Codex, 8cab385f2): a claim won while we wait for the exit lock ---------------
# The precheck sees no claim; a competing exit claims while _exit waits for the shared lock; the
# executor's own atomic claim answers already_closing. That is the same fact as a visible claim.

class _ClaimOnEntry:
    """The shared exit lock; on its first acquisition a competing exit takes a de-risk claim."""

    def __init__(self, world, pid):
        self.world, self.pid, self.claim, self.lock = world, pid, None, threading.Lock()

    def __enter__(self):
        self.lock.acquire()
        if self.claim is None:
            self.claim = m.claim_position_derisk(self.world.conn, self.pid, '2026-10-15T12:00:01+00:00')
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False

    def locked(self):
        return self.lock.locked()


def _claim_aware_exit(world):
    """The executor's atomic claim: refuses already_closing while another claim holds the position."""
    def exit_fn(conn, pos, reason, now):
        row = m.get_position(conn, pos['id'])
        if row.get('derisk_claim_id') or row.get('close_claim_id'):
            world.calls.append((pos['id'], reason, 'already_closing'))
            return {'status': 'already_closing'}
        world.calls.append((pos['id'], reason, 'closed'))
        return {'status': 'closed', 'realized': -0.2}
    world.contexts['solana']['exit_fn'] = exit_fn


def test_f2v_the_initial_exit_that_loses_its_claim_on_lock_entry_is_held_and_runs_once_resolved(world):
    pid = _position(world)
    _claim_aware_exit(world)
    lock = _ClaimOnEntry(world, pid)
    world.lock = lock
    [rec] = _not_landed(world, R.SOURCE_FAILED)
    assert rec['exit_status'] == 'already_closing' and rec['held']
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    assert world.mirror.run(world.conn, world.contexts, exit_lock=lock) == []      # still claimed: quiet
    assert m.release_position_derisk_claim(world.conn, pid, lock.claim)
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    world.mirror.run(world.conn, world.contexts, exit_lock=lock)
    assert [c for c in world.calls if c[2] == 'closed'] == [(pid, 'source_failed', 'closed')]
    world.clock.t += 30.0
    world.mirror.run(world.conn, world.contexts, exit_lock=lock)
    assert len([c for c in world.calls if c[2] == 'closed']) == 1                  # exactly one


def test_f2v_a_retry_that_loses_its_claim_on_lock_entry_is_held_and_runs_once_resolved(world):
    pid = _position(world)
    answers = iter([{'status': 'stuck'}])

    def exit_fn(conn, pos, reason, now):
        row = m.get_position(conn, pos['id'])
        if row.get('derisk_claim_id'):
            world.calls.append((pos['id'], reason, 'already_closing'))
            return {'status': 'already_closing'}
        nxt = next(answers, None)
        world.calls.append((pos['id'], reason, (nxt or {}).get('status', 'closed')))
        return nxt or {'status': 'closed', 'realized': -0.2}
    world.contexts['solana']['exit_fn'] = exit_fn
    _not_landed(world)                                     # first exit: stuck, a retry is scheduled
    lock = _ClaimOnEntry(world, pid)
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=lock)
    assert rec['exit_status'] == 'already_closing' and rec['held']
    m.release_position_derisk_claim(world.conn, pid, lock.claim)
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    world.mirror.run(world.conn, world.contexts, exit_lock=lock)
    world.clock.t += 30.0
    world.mirror.run(world.conn, world.contexts, exit_lock=lock)
    assert [c for c in world.calls if c[2] == 'closed'] == [(pid, 'source_not_landed', 'closed')]


def _unknown_then(world, pid, after):
    """Our own sell signs and its fate is unknown (close_sig set), then `after` answers."""
    answers = iter([None])

    def exit_fn(conn, pos, reason, now):
        row = m.get_position(conn, pos['id'])
        if row.get('close_claim_id') or row.get('close_sig'):
            world.calls.append((pos['id'], reason, 'already_closing'))
            return {'status': 'already_closing'}
        if next(answers, 'done') is None:
            conn.execute("UPDATE memecoin_positions SET close_claim_id='own', close_sig='OurSig' WHERE id=?", (pid,))
            conn.commit()
            world.calls.append((pos['id'], reason, 'unknown'))
            return {'status': 'unknown', 'sig': 'OurSig'}
        world.calls.append((pos['id'], reason, after))
        return {'status': after, 'realized': -0.2}
    world.contexts['solana']['exit_fn'] = exit_fn


def test_f2v_our_own_unknown_sell_holds_the_exit_until_it_resolves_failed_then_exits_once(world):
    pid = _position(world)
    _unknown_then(world, pid, 'closed')
    [rec] = _not_landed(world, R.SOURCE_FAILED)
    assert rec['exit_status'] == 'unknown' and rec['held']
    world.clock.t += 5.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []   # held behind close_sig
    world.conn.execute("UPDATE memecoin_positions SET close_claim_id=NULL, close_sig=NULL WHERE id=?", (pid,))
    world.conn.commit()                                                     # the sell resolved failed
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    world.clock.t += 30.0
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert [c for c in world.calls if c[2] == 'closed'] == [(pid, 'source_failed', 'closed')]


def test_f2v_an_unknown_sell_that_lands_closes_the_position_and_the_held_exit_drops(world):
    pid = _position(world)
    _unknown_then(world, pid, 'closed')
    _not_landed(world, R.SOURCE_FAILED)
    world.conn.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))   # our sell landed
    world.conn.commit()
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'retry_dropped'
    world.clock.t += 30.0
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []
    assert [c[2] for c in world.calls] == ['unknown']


def test_f2v_a_dust_route_holds_the_exit_and_rechecks_slowly_until_the_route_recovers(world):
    pid = _position(world)
    answers = [{'status': 'dust_route_hold'}, {'status': 'dust_route_hold'}, {'status': 'closed', 'realized': -0.2}]
    world.contexts['solana']['exit_fn'] = lambda conn, pos, reason, now: (
        world.calls.append((pos['id'], reason)) or answers.pop(0))
    [rec] = _not_landed(world, R.SOURCE_FAILED)
    assert rec['exit_status'] == 'dust_route_hold' and rec['held']
    world.clock.t += ssm.RETRY_DELAYS_S[0]
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == [] and len(world.calls) == 1
    world.clock.t += ssm.DUST_RECHECK_S
    assert world.mirror.run(world.conn, world.contexts, exit_lock=world.lock) == []   # still dust: quiet
    world.clock.t += ssm.DUST_RECHECK_S
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    world.clock.t += 60.0
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert world.calls == [(pid, 'source_failed')] * 3 and answers == []


def test_f2v_a_dust_held_exit_drops_when_the_position_closes(world):
    pid = _position(world)
    world.contexts['solana']['exit_fn'] = lambda conn, pos, reason, now: (
        world.calls.append((pos['id'], reason)) or {'status': 'dust_route_hold'})
    _not_landed(world, R.SOURCE_FAILED)
    world.conn.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))
    world.conn.commit()
    world.clock.t += ssm.DUST_RECHECK_S
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert rec['outcome'] == 'retry_dropped' and len(world.calls) == 1


def test_f2v_consecutive_dust_answers_back_off_to_the_cap_and_a_claim_answer_resets(world):
    """Gamma LOW on cc024f0da: each dust re-check is an exact quote on the funded key."""
    pid = _position(world)
    answers = ['dust_route_hold'] * 6 + ['already_closing', 'dust_route_hold']
    world.contexts['solana']['exit_fn'] = lambda conn, pos, reason, now: {'status': answers.pop(0)}
    _not_landed(world, R.SOURCE_FAILED)
    delays = []
    for _ in range(7):
        job = min(world.mirror._retries, key=lambda r: r['due'])
        delays.append(round(job['due'] - world.clock.t, 3))
        world.clock.t = job['due']
        world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, ssm.RETRY_DELAYS_S[0]]
    job = world.mirror._retries[0]
    assert job['due'] - world.clock.t == 5.0 and job['dust_n'] == 1          # reset after the claim answer
