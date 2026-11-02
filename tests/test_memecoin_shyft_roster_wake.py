"""A roster change re-subscribes the processed feed at once, not after the 5 s ping sleep (Alpha (c'),
2026-10-15: 70 roster-ack bursts, 506 s blind, in the journal to 10-15), at most once per 100 ms.
The RabbitStream prepare feed (memecoin_shyft_prepare.run_prepare_feed) gets the same wake; every
test runs against both feeds. Gamma's W3/W4/W6 pins on 4b1a24149 are at the bottom."""
import asyncio
import contextlib
import threading
import time

import grpc
import pytest

from sniper.services import memecoin_shyft_prepare as prep
from sniper.services import memecoin_shyft_signal as g

FEEDS = {'processed': (g, g.run_feed, 'https://beta-grpc.ams.shyft.to'),
         'rabbit': (prep, prep.run_prepare_feed, 'https://rabbitstream.fra.shyft.to')}

A = '11111111111111111111111111111112'
B = 'So11111111111111111111111111111111111111112'


def _owners(request):
    filters = request.transactions.get('owners') if request.transactions else None
    return tuple(filters.account_include) if filters is not None else None


class _Stream:
    """An in-process Subscribe call: answers each ping with its pong and streams a head update every
    20 ms (production heads never go quiet), recording every subscription request."""

    def __init__(self, request_iter, requests, pings=None, heads=True):
        self.out, self.requests, self.pings = asyncio.Queue(), requests, pings
        self.tasks = [asyncio.ensure_future(self._consume(request_iter))]
        if heads:                    # Rabbit subscribes transactions only: no head updates (Codex F7)
            self.tasks.append(asyncio.ensure_future(self._slots()))

    async def _consume(self, request_iter):
        async for req in request_iter:
            if req.HasField('ping'):
                if self.pings is not None:
                    self.pings.append(time.monotonic())
                pong = g.pb.SubscribeUpdate()
                pong.pong.id = req.ping.id
                await self.out.put(pong)
            else:
                self.requests.append((time.monotonic(), _owners(req)))

    async def _slots(self):
        slot = 1000
        while True:
            update = g.pb.SubscribeUpdate()
            update.slot.slot = slot
            slot += 1
            await self.out.put(update)
            await asyncio.sleep(0.02)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.out.get()

    def cancel(self):
        for task in self.tasks:
            task.cancel()


class _Channel:
    def __init__(self, requests, pings=None, heads=True):
        self.requests, self.pings, self.heads = requests, pings, heads

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream_stream(self, path, **kw):
        return lambda request_iter, metadata=None: _Stream(request_iter, self.requests, self.pings, self.heads)


def _run(monkeypatch, owners_fn, *, seconds, feed='processed', pings=None, heads=True):
    requests, stop, status = [], threading.Event(), {}
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: _Channel(requests, pings, heads))
    _module, run, url = FEEDS[feed]

    async def exercise():
        task = asyncio.create_task(run(url, 'secret', owners_fn, lambda buy: None, status, stop))
        await asyncio.sleep(seconds)
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    return requests, status


@pytest.fixture(params=sorted(FEEDS))
def feed(request):
    return request.param


def test_a_roster_change_resubscribes_within_one_round_trip(monkeypatch, feed):
    roster = {'owners': {A: ('u1', 'h1')}}
    started = time.monotonic()

    def owners_fn():
        if time.monotonic() - started > 1.0:
            roster['owners'] = {A: ('u1', 'h1'), B: ('u2', 'h2')}
            roster.setdefault('changed', time.monotonic())
        return roster['owners']
    requests, status = _run(monkeypatch, owners_fn, seconds=2.2, feed=feed)
    with_b = [t for t, owners in requests if owners and B in owners]
    assert with_b, requests
    assert with_b[0] - roster['changed'] < 0.5          # was up to 5 s (the ping sleep)


def test_a_flapping_roster_re_requests_at_most_every_100_ms(monkeypatch, feed):
    started = time.monotonic()

    def owners_fn():                                     # a new roster every 10 ms
        return {A: ('u1', 'h1'), ('Flap%028d' % int((time.monotonic() - started) * 100)): ('u', 'h')}
    requests, status = _run(monkeypatch, owners_fn, seconds=1.0, feed=feed)
    times = [t for t, owners in requests if owners]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 3 and min(gaps) >= FEEDS[feed][0].REROSTER_MIN_S * 0.9


def test_w3_a_change_inside_the_first_ping_window_is_never_lost(monkeypatch, feed):
    """The roster flips right after the first ping is consumed, while the request loop sits between
    its ping and its wait: the re-request still goes out inside 0.5 s."""
    pings, roster = [], {'owners': {A: ('u1', 'h1')}}

    def owners_fn():
        if pings and 'changed' not in roster:
            roster['owners'] = {A: ('u1', 'h1'), B: ('u2', 'h2')}
            roster['changed'] = time.monotonic()
        return roster['owners']
    requests, _status = _run(monkeypatch, owners_fn, seconds=1.2, feed=feed, pings=pings)
    with_b = [t for t, owners in requests if owners and B in owners]
    assert with_b and with_b[0] - roster['changed'] < 0.5, (roster, requests)


def test_w4_pings_keep_the_ping_cadence_and_a_stable_roster_subscribes_once(monkeypatch, feed):
    module = FEEDS[feed][0]
    assert module.PING_S == 5.0
    monkeypatch.setattr(module, 'PING_S', 0.2)
    pings, started = [], time.monotonic()
    requests, _status = _run(monkeypatch, lambda: {A: ('u1', 'h1')}, seconds=1.1, feed=feed, pings=pings)
    gaps = [b - a for a, b in zip(pings, pings[1:])]
    assert len(pings) >= 4 and 0.18 <= min(gaps) and max(gaps) <= 0.35, gaps
    assert [owners for _t, owners in requests] == [(A,)]                  # no change, no extra request
    assert pings[-1] - started > 2 * 0.2                                 # W6: pings go on past 2 x PING_S


def test_w6_an_owners_fn_that_raises_once_is_named_and_the_stream_keeps_pinging(monkeypatch, feed):
    monkeypatch.setattr(FEEDS[feed][0], 'PING_S', 0.2)
    pings, calls = [], [0]

    def owners_fn():
        calls[0] += 1
        if calls[0] == 3:
            raise RuntimeError('roster snapshot broke')
        return {A: ('u1', 'h1')}
    requests, status = _run(monkeypatch, owners_fn, seconds=1.1, feed=feed, pings=pings)
    assert status['owners_errors'] == 1 and status['owners_error'] == 'RuntimeError'
    assert status['reconnects'] == 0 and len(pings) >= 4
    assert [owners for _t, owners in requests] == [(A,)]


# --- F7 (Codex review through 648ada5d7): a QUIET Rabbit stream still sees a roster change -----------
# Rabbit subscribes transactions only, so with quiet owners nothing arrives between pongs and the
# receive loop never observes a new owner; the request loop slept to the 5 s ping. These streams
# send NO head updates (the tests above inject them, which hid this).

def test_f7_a_quiet_rabbit_stream_resubscribes_a_new_owner_within_half_a_second(monkeypatch):
    roster = {'owners': {A: ('u1', 'h1')}}
    started = time.monotonic()

    def owners_fn():
        if time.monotonic() - started > 1.0:
            roster['owners'] = {A: ('u1', 'h1'), B: ('u2', 'h2')}
            roster.setdefault('changed', time.monotonic())
        return roster['owners']
    requests, status = _run(monkeypatch, owners_fn, seconds=2.2, feed='rabbit', heads=False)
    with_b = [t for t, owners in requests if owners and B in owners]
    assert with_b, requests
    assert with_b[0] - roster['changed'] < 0.5          # was ~5 s on a quiet stream


def test_f7_a_quiet_flapping_roster_still_re_requests_at_most_every_100_ms_and_keeps_pinging(monkeypatch):
    started, pings = time.monotonic(), []

    def owners_fn():
        return {A: ('u1', 'h1'), ('Flap%028d' % int((time.monotonic() - started) * 100)): ('u', 'h')}
    requests, status = _run(monkeypatch, owners_fn, seconds=1.0, feed='rabbit', pings=pings, heads=False)
    times = [t for t, owners in requests if owners]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 3 and min(gaps) >= prep.REROSTER_MIN_S * 0.9


def test_f7_a_quiet_stable_roster_keeps_the_five_second_ping(monkeypatch):
    pings = []
    requests, status = _run(monkeypatch, lambda: {A: ('u1', 'h1')}, seconds=1.0, feed='rabbit',
                            pings=pings, heads=False)
    assert len([t for t, owners in requests if owners]) == 1 and len(pings) == 1
