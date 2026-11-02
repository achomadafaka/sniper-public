"""Every receipt-handoff path that does not resume names why (Alpha 2026-10-15: 16 routed buys lost
2026-10-06..10-11, 8 of them with no line saying which check stopped them)."""
import dataclasses
import logging

from sniper.services import memecoin_slot_lane as L
from test_slot_lane_receipt_resume import setup


def notes(caplog, sig):
    return [r.getMessage() for r in caplog.records
            if 'slot lane receipt note' in r.getMessage() and sig in r.getMessage()]


def test_remember_without_a_receipt_names_the_wait(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        h.remember(buy)
    assert any('awaiting early=none' in m for m in notes(caplog, buy.signature))


def test_a_proof_first_receipt_is_named_when_cached(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev)
    assert any('early_proof_cached' in m for m in notes(caplog, buy.signature))
    h.remember(buy)
    assert len(queued) == 1                    # the cached proof still resumes the later candidate
    h.finish(queued[0])


def test_remember_names_an_early_proof_whose_identity_does_not_match(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    assert not h.offer(ev)
    buy = dataclasses.replace(buy, slot=buy.slot + 1)      # the candidate's slot is not the receipt's
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        h.remember(buy)
    assert not queued
    assert any('awaiting early=mismatch:slot' in m for m in notes(caplog, buy.signature))


def test_remember_names_an_early_proof_that_expired_first(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    assert not h.offer(ev)
    clock.now += L.MAX_SIGNAL_AGE_S + .01
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        h.remember(buy)
    assert not queued
    assert any('awaiting early=expired' in m for m in notes(caplog, buy.signature))


def test_an_offer_refused_before_the_candidate_is_named_twice(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    ev.source_initiation = None
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev)
        h.remember(buy)
    lines = notes(caplog, buy.signature)
    assert any('offer_skipped=no_proof' in m for m in lines)
    assert any('awaiting early=skipped:no_proof' in m for m in lines)


def test_remember_names_why_it_holds_nothing(monkeypatch, caplog):
    buy, ev, queued, h, clock = setup(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        h.remember(dataclasses.replace(buy, routed=None))
        h.remember(buy)
        h.remember(buy)
    lines = notes(caplog, buy.signature)
    assert any('remember_skipped=not_routed' in m for m in lines)
    assert any('remember_skipped=duplicate' in m for m in lines)


def test_notes_are_bounded(monkeypatch):
    buy, ev, queued, h, clock = setup(monkeypatch)
    ev.source_initiation = None
    for i in range(200):
        ev.fomo_event_id = 'onchain:sig%d' % i
        h.offer(ev)
    assert len(h._proof_notes) <= 64
