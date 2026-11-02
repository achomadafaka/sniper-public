"""One line per RabbitStream owner-signed frame (Lambda 2026-10-16, Alpha's item 2b).

Since rabbit buys went live (2026-10-15 13:39Z) 5 slot-lane sources were direct SOL-quote pump-curve
buys and rabbit fired on none; the counters reset at every deploy and the read's own line is
logger.info, below the runner log's WARNING floor, so nothing said whether rabbit lost on arrival or
refused. Pins: each owner-signed frame gets ONE line GRACE_S after its receipt with its verdict (the
prep miss reason, or the buyer's outcome), venue, route and lead over the first processed feed; the
event loop only notes in memory and the line is written off the loop; the counters reach the
heartbeat."""
import asyncio
import json
import threading
import time

import pytest

from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from test_memecoin_shyft_prepare import OWNER, packet
from test_memecoin_rabbit_signal_stage2 import (FIRE, FIRE_OWNER, _buyer, _curve_candidate, _offer,
                                                fire_frame, fire_rpc, knobs)  # noqa: F401 (fixture)

RABBIT = 'rabbit:rabbitstream.fra.shyft.to'
SIG = FIRE['signature']


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t


def _log(arrivals=None, clock=None):
    lines = []
    frames = R.FrameLog(feed=RABBIT, arrivals_fn=lambda sig, own: (arrivals or {}).get((sig, own), ()),
                        log=lambda fmt, *args: lines.append(fmt % args), clock=clock or Clock())
    return frames, lines


def _fields(line):
    return dict(part.split('=', 1) for part in line.split() if '=' in part)


def test_a_frame_is_written_once_after_the_grace_with_its_lead():
    clock = Clock()
    arrivals = {(SIG, FIRE_OWNER): ((RABBIT, 1000.0), ('shyft:grpc.ams.shyft.to', 1000.0052),
                                    ('shyft:grpc.fra.shyft.to', 1000.0071))}
    frames, lines = _log(arrivals, clock)
    frames.frame(SIG, FIRE_OWNER, 457_000_000, 1000.0)
    frames.note(SIG, FIRE_OWNER, 'fired', venue='pump_curve', route='direct')
    assert frames.due() == []                                   # inside the grace: nothing yet
    clock.t += R.FrameLog.GRACE_S
    frames.write(frames.due())
    assert len(lines) == 1 and frames.due() == []               # once
    f = _fields(lines[0])
    assert lines[0].startswith('[memecoin] slot lane rabbit frame ')
    assert f['sig'] == SIG and f['slot'] == '457000000' and f['verdict'] == 'fired'
    assert f['venue'] == 'pump_curve' and f['route'] == 'direct'
    assert f['lead_ms'] == '5.2' and f['processed'] == 'shyft:grpc.ams.shyft.to'   # positive: rabbit first
    assert frames.stats['lines'] == 1 and frames.stats['verdicts'] == {'fired': 1}


def test_no_processed_copy_is_none_not_zero_and_a_processed_first_frame_is_negative():
    clock = Clock()
    arrivals = {('late', FIRE_OWNER): (('shyft:grpc.fra.shyft.to', 999.990), (RABBIT, 1000.0))}
    frames, lines = _log(arrivals, clock)
    frames.frame(SIG, FIRE_OWNER, 1, 1000.0)
    frames.frame('late', FIRE_OWNER, 1, 1000.0)
    clock.t += R.FrameLog.GRACE_S
    frames.write(frames.due())
    by_sig = {_fields(line)['sig']: _fields(line) for line in lines}
    assert by_sig[SIG]['lead_ms'] == 'none' and by_sig[SIG]['processed'] == 'none'
    assert by_sig[SIG]['verdict'] == 'pending' and by_sig[SIG]['route'] == 'none'
    assert by_sig['late']['lead_ms'] == '-10.0'


def test_the_memory_is_bounded_and_counts_what_it_drops(monkeypatch):
    monkeypatch.setattr(R.FrameLog, 'MAX', 2)
    frames, _ = _log()
    for i in range(3):
        frames.frame(str(i), FIRE_OWNER, 1, 1000.0)
    assert frames.stats['dropped'] == 1 and [k[0] for k in frames._recs] == ['1', '2']


def test_a_failing_write_is_counted_and_never_raises():
    clock = Clock()
    frames = R.FrameLog(feed=RABBIT, arrivals_fn=lambda *a: (), log=lambda *a: 1 / 0, clock=clock)
    frames.frame(SIG, FIRE_OWNER, 1, 1000.0)
    clock.t += R.FrameLog.GRACE_S
    frames.write(frames.due())
    assert frames.stats['write_errors'] == 1 and frames.stats['lines'] == 0


def test_the_line_is_written_off_the_event_loop():
    threads = []
    clock = Clock()
    frames = R.FrameLog(feed=RABBIT, arrivals_fn=lambda *a: (),
                        log=lambda *a: threads.append(threading.get_ident()), clock=clock)
    frames.TICK_S = 0.001
    frames.frame(SIG, FIRE_OWNER, 1, 1000.0)
    clock.t += R.FrameLog.GRACE_S
    stop = threading.Event()

    async def main():
        loop_thread = threading.get_ident()
        task = asyncio.get_running_loop().create_task(frames.run(stop))
        while not threads:
            await asyncio.sleep(0.001)
        stop.set()
        await task
        return loop_thread
    loop_thread = asyncio.run(main())
    assert threads and threads[0] != loop_thread


# --- the verdicts --------------------------------------------------------------------------------

def _offered(read, *, seen=None):
    buyer, stats, fired = _buyer(read, seen=seen)
    frames, lines = _log()
    buyer.frames = frames
    cand = _curve_candidate()
    frames.frame(cand['signature'], cand['owner'], cand['slot'], cand['recv_monotonic'])
    asyncio.run(_offer(buyer, cand, fire_frame()))
    rec = frames._recs[(cand['signature'], cand['owner'])]
    return rec, stats, fired


def test_a_fire_notes_fired_and_its_route(knobs):
    rec, stats, fired = _offered(fire_rpc())
    assert stats['fired'] == 1 and rec['verdict'] == 'fired' and rec['route'] == 'direct'


def test_the_processed_feed_first_is_its_own_verdict(knobs):
    rec, stats, fired = _offered(fire_rpc(), seen={(FIRE['signature'], FIRE_OWNER): 1.0})
    assert fired == [] and rec['verdict'] == 'processed_first'


@pytest.mark.parametrize('between', ['processed_seen', 'knob_off'])
def test_a_repeat_frame_never_overwrites_the_first_offers_verdict(knobs, monkeypatch, between):
    """Gamma LOW-1 on 58c48188d: fired, then the processed copy marks `seen` (or the knob goes off),
    then a repeat rabbit frame of the same signature inside the grace: the line still reads fired."""
    seen = {}
    buyer, stats, fired = _buyer(fire_rpc(), seen=seen)
    frames, _ = _log()
    buyer.frames = frames
    cand = _curve_candidate()
    key = (cand['signature'], cand['owner'])
    frames.frame(*key, cand['slot'], cand['recv_monotonic'])
    asyncio.run(_offer(buyer, cand, fire_frame()))
    assert frames._recs[key]['verdict'] == 'fired'
    if between == 'processed_seen':
        seen[key] = 2.0
    else:
        from sniper.settings import memecoin as cfg
        monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', False)
    asyncio.run(_offer(buyer, cand, fire_frame()))
    assert frames._recs[key]['verdict'] == 'fired' and len(fired) == 1
    assert stats['duplicate' if between == 'processed_seen' else 'held'] == 1


def test_a_read_error_and_an_unmodelled_curve_are_verdicts(knobs):
    def broken(method, params):
        raise RuntimeError('getMultipleAccounts: http 429')
    broken.ping = lambda: None
    assert _offered(broken)[0]['verdict'] == 'read_error'
    assert _offered(fire_rpc(mayhem=1))[0]['verdict'] == 'mayhem_or_fee_unmodelled'


@pytest.mark.parametrize('bad,reason', [('unknown_buy', 'unsupported_target'), ('alt', 'alt'),
                                        ('index', 'structure')])
def test_an_owner_signed_frame_that_prep_cannot_use_notes_the_miss_reason(bad, reason):
    u = packet()
    msg = u.transaction.transaction.transaction.message
    if bad == 'unknown_buy':
        msg.instructions[0].data = bytes(25)
    if bad == 'alt':
        msg.versioned = True
        msg.address_table_lookups.add(account_key=bytes([33]) * 32, writable_indexes=b'\x00')
    if bad == 'index':
        msg.instructions[0].program_id_index = 255
    stamped, misses = [], []
    assert prep.preparation_candidate(u, {OWNER: ('u', 'h')}, recv_monotonic=0, generation=1,
                                      on_owner_frame=lambda *a: stamped.append(a[:2]),
                                      on_owner_miss=lambda *a: misses.append(a)) is None
    assert misses == [(*stamped[0], reason)]


def test_a_frame_without_the_owner_as_signer_or_a_good_frame_notes_nothing():
    misses = []
    u = packet()
    msg = u.transaction.transaction.transaction.message
    assert prep.preparation_candidate(u, {OWNER: ('u', 'h')}, recv_monotonic=0, generation=1,
                                      on_owner_miss=lambda *a: misses.append(a)) is not None
    msg.account_keys[0], msg.account_keys[4] = msg.account_keys[4], msg.account_keys[0]   # non-signer
    assert prep.preparation_candidate(u, {OWNER: ('u', 'h')}, recv_monotonic=0, generation=1,
                                      on_owner_miss=lambda *a: misses.append(a)) is None
    assert misses == []


def test_the_heartbeat_carries_the_frame_log_counters(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    counters = dict(lines=4, dropped=0, write_errors=0, verdicts={'fired': 1, 'processed_first': 3})
    lane_status = {'signal': {'rabbit_buy': dict(candidates=4, refused={}, frame_log=counters)}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane_status)
    assert json.loads(path.read_text())['slot_lane']['rabbit_buy']['frame_log'] == counters


def test_the_signal_loop_writes_one_line_per_frame_with_the_processed_lead(monkeypatch):
    """End to end on the coordinator: the rabbit frame is stamped, the processed copy of the same
    signature 5 ms later gives the lead, and the line names the frame's venue (no buyer wired: the
    verdict stays pending)."""
    from dataclasses import replace
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services.memecoin_slot_signal import SlotBuy
    from sniper.settings import memecoin as cfg
    from test_memecoin_rabbit_signal_stage1 import CURVE, frame, owner
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    monkeypatch.setattr(R.FrameLog, 'GRACE_S', 0.25)
    monkeypatch.setattr(R.FrameLog, 'TICK_S', 0.005)
    lines = []
    monkeypatch.setattr(R.logger, 'warning', lambda fmt, *args: lines.append(fmt % args))
    stop, status, at = threading.Event(), {}, {}

    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, **hooks):
        at['rabbit'] = signal.time.monotonic()
        cand = prep.preparation_candidate(frame(CURVE), owners_fn(), recv_monotonic=at['rabbit'],
                                          generation=1, on_owner_frame=hooks['on_owner_frame'],
                                          on_owner_miss=hooks['on_owner_miss'])
        if cand is not None:
            on_prepare(cand)
        while not stop.is_set():
            await asyncio.sleep(.01)

    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None, **kwargs):
        while 'rabbit' not in at:
            await asyncio.sleep(.001)
        buy = SlotBuy(venue='pump_curve', source_user_id='uid', handle='h', owner=owner(CURVE),
                      signature=CURVE['signature'], slot=CURVE['slot'], mint='M', pool=None, sol_amount=1,
                      token_amount=1, virtual_sol_reserves=1, virtual_token_reserves=1, real_sol_reserves=1,
                      real_token_reserves=1, token_program=None, trade_event_log='',
                      recv_monotonic=at['rabbit'] + .005, recv_utc='')
        on_buy(buy)
        for _ in range(300):
            if lines:
                break
            await asyncio.sleep(.01)
        stop.set()
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(lambda b: None, lambda: {owner(CURVE): ('uid', 'h')},
        None, status, stop, None, None, None, None, ('token', ('https://grpc.ams.shyft.to',)),
        infra_provider='shyft', prepare_config=('token', 'https://rabbitstream.fra.shyft.to'),
        on_prepare=lambda c: None))
    frame_lines = [line for line in lines if 'slot lane rabbit frame' in line]
    assert len(frame_lines) == 1, lines
    f = _fields(frame_lines[0])
    assert f['sig'] == CURVE['signature'] and f['venue'] == 'pump_curve' and f['verdict'] == 'pending'
    assert f['lead_ms'] == '5.0' and f['processed'] == 'shyft:grpc.ams.shyft.to'


# --- the read on the frame line (Beta 2026-10-17, task 5a7e1967; Alpha (a)) ----------------------
# The per-handler read time died with every restart (heartbeat deques) and the curve read's own line
# was logger.info, below the runner log's WARNING floor; the AMM handlers logged no read at all. The
# read's time, its slot against the frame's and where the state came from ride the frame line.

def _written(frames, rec_key, clock):
    clock.t += R.FrameLog.GRACE_S
    lines = []
    frames.log = lambda fmt, *args: lines.append(fmt % args)
    frames.write(frames.due())
    return [_fields(line) for line in lines if rec_key[0] in line]


def _offer_with(read, *, handlers=None, clock=None):
    buyer, stats, fired = _buyer(read)
    if handlers is not None:
        buyer.handlers = list(handlers)
        for handler in buyer.handlers:
            if hasattr(handler, 'bind'):
                handler.bind(buyer)
    clock = clock or Clock(time.monotonic())
    frames, _ = _log(clock=clock)
    buyer.frames = frames
    cand = _curve_candidate()
    key = (cand['signature'], cand['owner'])
    frames.frame(*key, cand['slot'], clock.t)
    asyncio.run(_offer(buyer, cand, fire_frame()))
    return frames, key, clock, stats, fired, cand


def test_a_curve_fire_line_carries_its_read_and_the_folded_audit(knobs, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=R.logger.name)
    frames, key, clock, stats, fired, cand = _offer_with(fire_rpc())
    assert len(fired) == 1
    (f,) = _written(frames, key, clock)
    assert f['verdict'] == 'fired' and float(f['read_ms']) >= 0.0
    assert f['read_minus_frame'] == '0' and f['state_source'] == 'rpc'
    assert f['read_basis'] == R.PRE_TRADE and f['held'] == '0'
    assert not [r for r in caplog.records if 'rabbit read sig=' in r.getMessage()]   # one line per frame


class _AmmStub:
    """A non-curve handler: match answers, the read carries a context slot, the verdict refuses."""
    route = 'stub_pump_amm'

    def __init__(self, slot):
        self.slot = slot

    def match(self, candidate, update):
        return {'route': self.route}, None

    def read(self, plan):
        return {'slot': self.slot, 'accounts': {}}

    def verdict(self, plan, state, candidate):
        return None, 'read_stale'


def test_an_amm_handler_read_reaches_the_line_with_its_slot_delta(knobs):
    cand_slot = _curve_candidate()['slot']
    frames, key, clock, stats, fired, cand = _offer_with(fire_rpc(), handlers=[_AmmStub(cand_slot - 3)])
    (f,) = _written(frames, key, clock)
    assert f['verdict'] == 'read_stale' and f['route'] == 'stub_pump_amm'
    assert float(f['read_ms']) >= 0.0 and f['read_minus_frame'] == '-3' and f['state_source'] == 'rpc'
    assert f['read_basis'] == 'none' and f['held'] == 'none'


def test_a_read_error_still_carries_its_time_and_an_unread_slot(knobs):
    def broken(method, params):
        raise RuntimeError('getMultipleAccounts: http 429')
    broken.ping = lambda: None
    frames, key, clock, *_ = _offer_with(broken)
    (f,) = _written(frames, key, clock)
    assert f['verdict'] == 'read_error' and float(f['read_ms']) >= 0.0
    assert f['read_minus_frame'] == 'none' and f['state_source'] == 'rpc'


def test_a_frame_with_no_read_says_none_not_zero(knobs):
    clock = Clock()
    frames, _ = _log(clock=clock)
    frames.frame(SIG, FIRE_OWNER, 1, clock.t)
    frames.note(SIG, FIRE_OWNER, 'unsupported')
    (f,) = _written(frames, (SIG, FIRE_OWNER), clock)
    assert f['read_ms'] == 'none' and f['read_minus_frame'] == 'none' and f['state_source'] == 'none'


def test_the_knob_off_writes_the_line_exactly_as_before(knobs, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_FRAME_READ_FIELDS', False)
    frames, key, clock, *_ = _offer_with(fire_rpc())
    (f,) = _written(frames, key, clock)
    assert f['verdict'] == 'fired'
    assert not {'read_ms', 'read_minus_frame', 'state_source', 'read_basis', 'held'} & set(f)
