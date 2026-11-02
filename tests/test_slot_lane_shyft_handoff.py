"""Confirmed-first delivery must still use the processed candidate's original deadline."""
import dataclasses
import threading

import pytest

from sniper.services import memecoin_slot_lane as L
from test_slot_lane_receipt_resume import setup


def proof_first(monkeypatch):
    buy, ev, queued, handoff, clock = setup(monkeypatch)
    ev.provenance = "confirmed_yellowstone"
    ev.proof_ready_monotonic = 100.02
    ev.source_initiation["route_payment_atomic"] = str(buy.routed.usdc_spent_atomic)
    clock.now = 100.03
    return buy, ev, queued, handoff, clock


def test_confirmed_first_receipt_is_consumed_when_native_preparation_finishes(monkeypatch):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    assert not h.offer(ev)
    h.remember(buy)
    assert len(queued) == 1
    resumed = queued[0]
    assert resumed.recv_monotonic == 100.
    assert resumed.proof_ready_monotonic == 100.02
    assert resumed.proof_provenance == "confirmed_yellowstone"
    assert not h.offer(ev)
    h.finish(resumed)


def test_early_receipt_never_resets_an_expired_processed_deadline(monkeypatch):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    assert not h.offer(ev)
    clock.now = 100.501
    h.remember(buy)
    assert not queued
    assert not L.CONFIRMED_PENDING.holds(buy.signature, clock.now)


@pytest.mark.parametrize("field,value", [("source_wallet", "wrong"), ("source_user_id", "other"),
                                        ("mint", "other"), ("source_block_number", 123)])
def test_mismatching_early_receipt_cannot_consume_a_later_candidate(monkeypatch, field, value):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    original = getattr(ev, field)
    setattr(ev, field, value)
    assert not h.offer(ev)
    h.remember(buy)
    assert not queued
    setattr(ev, field, original)
    assert h.offer(ev)
    h.finish(queued[0])


def test_concurrent_receipt_and_preparation_enqueue_only_once(monkeypatch):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    barrier = threading.Barrier(3)
    errors = []
    def run(fn):
        try:
            barrier.wait(1.)
            fn()
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(lambda: h.offer(ev),)),
               threading.Thread(target=run, args=(lambda: h.remember(buy),))]
    for t in threads: t.start()
    barrier.wait(1.)
    for t in threads: t.join(1.)
    assert not errors and len(queued) == 1
    h.finish(queued[0])


def test_early_receipt_cache_is_bounded_and_forgets_stale_proofs(monkeypatch):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    import base58
    from types import SimpleNamespace
    for i in range(100):
        signature = base58.b58encode(i.to_bytes(64, "little")).decode()
        event = SimpleNamespace(**vars(ev))
        event.source_initiation = dict(ev.source_initiation, tx_hash=signature)
        event.fomo_event_id = "onchain:" + signature
        assert not h.offer(event)
    assert len(h._early_proofs) <= 64
    clock.now += 1.
    h.remember(dataclasses.replace(buy, recv_monotonic=clock.now))
    assert not h._early_proofs and not queued



def test_proof_first_availability_stamp_is_not_reset_when_preparation_finishes(monkeypatch):
    buy, ev, queued, h, clock = proof_first(monkeypatch)
    del ev.proof_ready_monotonic
    assert not h.offer(ev)
    clock.now = 100.2
    h.remember(buy)
    assert queued[0].proof_ready_monotonic == 100.03
    h.finish(queued[0])


@pytest.mark.parametrize('venue', ['damm', 'dbc'])
def test_routed_conversion_retains_the_observed_feed(venue):
    if venue == 'damm':
        from sniper.execution import meteora_damm_v2_buy as module
        from test_slot_lane_damm_v2 import CLASSIC, _routed, _learned
        routed = dataclasses.replace(_routed(CLASSIC), signal_feed='shyft_processed_ams')
        buy, reason = module.damm_slot_buy(routed, _learned(CLASSIC))
    else:
        from sniper.execution import meteora_dbc_buy as module
        from test_slot_lane_dbc import SOL, _routed, _facts
        routed = dataclasses.replace(_routed(SOL), signal_feed='shyft_processed_ams')
        buy, reason = module.dbc_slot_buy(routed, _facts(SOL))
    assert reason is None
    assert buy.signal_feed == 'shyft_processed_ams'
