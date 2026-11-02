"""Gamma's three survivors on 59849ebe9 (2026-10-15): the two-signer refusal, the rabbit frame's raw
stamp through the wiring, and a CPMM preparation target never paired as a buy."""
import asyncio
import threading
import time

import base58
import pytest

from sniper.execution import raydium_cpmm_buy as cpmm
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services import memecoin_shyft_signal as full
from sniper.services import memecoin_slot_signal as signal
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_signal_stage1 import CURVE, FX, frame, owner

NON_BUY = next(r for r in FX['routes'] if r['buy_leg'] is None)
PAIR = 'shyft:grpc.fra.shyft.to|rabbit:rabbitstream.fra.shyft.to'


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)


def test_two_tracked_owners_signing_one_frame_are_no_signal():
    update = frame(CURVE)
    signers = CURVE['signers']                       # the DFlow sponsor and the owner both sign
    assert len(signers) == 2
    both = {signers[0]: ('u0', 'h0'), signers[1]: ('u1', 'h1')}
    assert prep.owner_signer(update, both) is None
    assert prep.owner_signer(update, {signers[1]: ('u1', 'h1')}) == (CURVE['signature'], signers[1])
    stamped, stats = [], {}
    assert prep.preparation_candidate(update, both, recv_monotonic=0.0, generation=1, stats=stats,
                                      on_owner_frame=lambda *a: stamped.append(a)) is None
    assert stats == {'signature': 1} and stamped == []


def _run(monkeypatch, rabbit_steps, *, processed_sig):
    """The signal loop with a fake rabbit stream (`rabbit_steps(on_prepare, on_owner_frame, t0)`) and
    one processed frame of `processed_sig`'s route received 30 ms after the rabbit frame."""
    stop, status = threading.Event(), {}
    t0 = time.monotonic()
    route = next(r for r in FX['routes'] if r['signature'] == processed_sig)
    owners = {owner(route): ('uid', 'h')}

    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, *, on_owner_frame=None, **kw):
        rabbit_steps(on_prepare, on_owner_frame, t0, owners_fn())
        while not stop.is_set():
            await asyncio.sleep(.01)

    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata=None, **kwargs):
        await asyncio.sleep(.02)
        on_metadata(frame(route), recv_monotonic=t0 + .030, recv_utc='', head_slot=None, head_mono=None,
                    native_buys=(), receipt_owners=owners)
        await asyncio.sleep(.05)
        stop.set()
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(lambda b: None, lambda: owners, None, status, stop, None, None, None, None,
        ('token', ('https://grpc.fra.shyft.to',)), infra_provider='shyft',
        prepare_config=('token', 'https://rabbitstream.fra.shyft.to'), on_prepare=lambda c: None))
    return status, t0


def test_an_owner_signed_non_buy_is_a_raw_stamp_never_a_buy_pair(monkeypatch):
    def steps(on_prepare, on_owner_frame, t0, owners):
        got = prep.preparation_candidate(frame(NON_BUY), owners, recv_monotonic=t0, generation=1,
                                         on_owner_frame=on_owner_frame)
        assert got is None                            # the route's final leg is no buy we read
    status, t0 = _run(monkeypatch, steps, processed_sig=NON_BUY['signature'])
    raw = status['raw_arrival_metrics'].snapshot(now=t0 + 1)['pairs'][PAIR]
    buys = status['arrival_metrics'].snapshot(now=t0 + 1)
    assert raw['n'] == 1 and raw['p50_ms'] == pytest.approx(-30.0, abs=0.5)
    assert buys['samples'] == 0 and buys['pairs'][PAIR]['n'] == 0


def _cpmm_candidate(route, t0):
    accounts = (owner(route),) + tuple('Acct%040d' % i for i in range(12))
    data = cpmm.SWAP_BASE_INPUT_DISCRIMINATOR + bytes(16)
    return dict(signature=route['signature'], owner=owner(route), slot=route['slot'], recv_monotonic=t0,
                generation=1, account_keys=accounts, instructions=((cpmm.CPMM_PROGRAM, accounts, data),))


def test_a_cpmm_preparation_target_is_never_paired_as_a_buy(monkeypatch):
    def steps(on_prepare, on_owner_frame, t0, owners):
        on_owner_frame(NON_BUY['signature'], owner(NON_BUY), NON_BUY['slot'], t0, 1)
        candidate = _cpmm_candidate(NON_BUY, t0)
        assert prep.preparation_target(candidate)['venue'] == 'raydium_cpmm'
        on_prepare(candidate)
    status, t0 = _run(monkeypatch, steps, processed_sig=NON_BUY['signature'])
    assert status['arrival_metrics'].snapshot(now=t0 + 1)['samples'] == 0
    assert status['raw_arrival_metrics'].snapshot(now=t0 + 1)['pairs'][PAIR]['n'] == 1


def test_a_rabbit_decoded_buy_target_pairs_on_the_receipt_stamps(monkeypatch):
    def steps(on_prepare, on_owner_frame, t0, owners):
        on_prepare(prep.preparation_candidate(frame(CURVE), owners, recv_monotonic=t0, generation=1,
                                              on_owner_frame=on_owner_frame))
    status, t0 = _run(monkeypatch, steps, processed_sig=CURVE['signature'])
    pair = status['arrival_metrics'].snapshot(now=t0 + 1)['pairs'][PAIR]
    assert pair['n'] == 1 and pair['p50_ms'] == pytest.approx(-30.0, abs=0.5)
