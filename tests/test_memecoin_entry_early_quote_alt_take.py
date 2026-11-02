"""A ready alternate early quote never waits on, or dies with, the primary (2026-10-08 review C07).

Sizing starts the primary quote at clip 25 and an approved alternate at 18.75. The alternate
finishes, the primary stalls, and the reservation approves 18.75. `take(18.75)` used to join the
primary thread first and, with the primary still alive at the end of the wait, discard the ready
alternate as `not_ready`. Now it waits only on the worker that owns the requested clip, inside one
total wait budget.
"""
import threading
import time

import pytest

from sniper.services import memecoin_early_quote as EQ

QUOTE = {"inAmount": "10000000", "outAmount": "500000000", "otherAmountThreshold": "470000000",
         "priceImpactPct": "0.01", "routePlan": [{"swapInfo": {"ammKey": "AMM1"}}]}
PRIMARY, ALT = 25.0, 18.75


@pytest.fixture(autouse=True)
def _fresh_stats(monkeypatch):
    monkeypatch.setattr(EQ, "_STATS", {"skipped": {}, "started": 0, "quoted": 0,
                                       "sizing_refused": {}, "used": 0, "discarded": {},
                                       "refusal_requoted": {}, "refusal_agreed": {}})


def _early(*, primary_gate=None, alt_gate=None, alt_sizing_gate=None):
    """Quotes answer their own clip; a gate holds that quote until it is set."""
    def quote_fn(mint, usd):
        gate = primary_gate if usd == PRIMARY else alt_gate
        if gate is not None:
            gate.wait(10)
        return dict(QUOTE, outAmount=str(int(usd * 1000)))

    def alts():
        if alt_sizing_gate is not None:
            alt_sizing_gate.wait(10)
        return [ALT]
    return EQ.EarlyEntryQuote(mint="MINT", quote_fn=quote_fn,
                              sizing_fn=lambda: (PRIMARY, None, {"alt_clips_fn": alts}))


def _alt_done(early, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = next(iter(early._alts.values()), None)
        if result is not None and result["done_at"] is not None:
            return
        time.sleep(0.005)
    raise AssertionError("the alternate never finished")


def test_a_ready_alternate_is_used_while_the_primary_stalls():
    stall = threading.Event()
    early = _early(primary_gate=stall)
    try:
        _alt_done(early)
        began = time.monotonic()
        quote = early.take(ALT, max_wait_s=2.0)
        waited = time.monotonic() - began
        assert quote is not None and quote["outAmount"] == "18750", EQ.stats()
        assert waited < 0.5, "the ready alternate waited %.2fs on the primary" % waited
        assert EQ.stats()["used"] == 1
    finally:
        stall.set()


def test_an_alternate_sized_late_is_used_while_the_primary_stalls():
    stall, sizing = threading.Event(), threading.Event()
    early = _early(primary_gate=stall, alt_sizing_gate=sizing)
    try:
        threading.Timer(0.05, sizing.set).start()
        quote = early.take(ALT, max_wait_s=2.0)
        assert quote is not None and quote["outAmount"] == "18750", EQ.stats()
    finally:
        stall.set()
        sizing.set()


def test_a_stalled_alternate_is_not_ready_and_never_hands_over_the_primary():
    stall = threading.Event()
    early = _early(alt_gate=stall)
    try:
        early._thread.join(5)                       # the primary (wrong clip) is ready
        began = time.monotonic()
        assert early.take(ALT, max_wait_s=0.1) is None
        assert time.monotonic() - began < 0.6
        assert EQ.stats()["discarded"] == {"not_ready": 1}
    finally:
        stall.set()


def test_the_wait_is_one_total_budget_not_one_per_worker():
    stall, sizing = threading.Event(), threading.Event()
    early = _early(primary_gate=stall, alt_sizing_gate=sizing)
    try:
        began = time.monotonic()
        assert early.take(ALT, max_wait_s=0.15) is None
        assert time.monotonic() - began < 0.4
    finally:
        stall.set()
        sizing.set()


def test_the_primary_clip_still_waits_on_the_primary_only():
    stall = threading.Event()
    early = _early(alt_gate=stall)
    try:
        quote = early.take(PRIMARY, max_wait_s=2.0)
        assert quote is not None and quote["outAmount"] == "25000"
    finally:
        stall.set()


def test_the_take_stays_one_shot():
    early = _early()
    _alt_done(early)
    assert early.take(ALT) is not None
    assert early.take(ALT) is None and early.take(PRIMARY) is None


@pytest.mark.parametrize("requested,used", [(15.0, False), (ALT, True)])
def test_trace_records_exact_requested_clip_and_bounded_candidate_set(requested, used):
    from types import SimpleNamespace
    seen = []
    early = EQ.EarlyEntryQuote(
        mint="MINT", quote_fn=lambda mint, clip: dict(QUOTE),
        sizing_fn=lambda: (PRIMARY, None, {"alt_clips": [ALT]}),
        trace=SimpleNamespace(mark_substage=lambda name, **fields: seen.append((name, fields))))
    _alt_done(early)
    assert (early.take(requested) is not None) is used
    name, fields = seen[-1]
    assert name == "early_quote"
    assert fields["requested_clip"] == requested
    assert fields["candidate_clips"] == [ALT, PRIMARY]
    assert fields["size_miss"] is (not used)
