"""Funded quote paths must not be starved by lower-priority callers.

Two separate starvation bugs, both found live on 2026-08-14 while a funded position sat above its
take-profit and could not exit:

1. The exact-quote token bucket was shared between the periodic heartbeat and the price rails. It
   refills at 0.5 tokens/s and a take costs 1.0, so with several open positions on a 1s tick the
   bucket never carried a whole token: one position consumed it and the rest were refused forever
   (5283 consecutive refusals on pos 37273).
2. GeckoTerminal's 429 cooldown was a single process-wide cell. The blind-capture research batch
   trips 429s constantly, and every one of them also gagged the funded entry path for up to 300s.
"""
import sqlite3

import pytest

from sniper.execution import geckoterminal_quote as gt
from sniper.services.memecoin_fastlane import build_demand_fn


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _pos(pid, entry=1.0):
    return {"id": pid, "entry_price": entry, "peak_price": entry}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_rail_demand_survives_when_heartbeats_drain_the_shared_bucket(conn, monkeypatch):
    """A position AT its take-profit must still get its confirming quote.

    This is the exact live failure: three open positions on a 1s tick, the shared 30/min bucket
    permanently below one token, and the position above TP refused on every single tick.
    """
    clock = FakeClock()
    demand = build_demand_fn(conn, near_pct=10.0, heartbeat_s=60.0, clock=clock)

    # Drain the shared (heartbeat) bucket dry the way the live system did.
    for _ in range(50):
        demand.budget.take()
    assert not demand.budget.take(), "precondition: shared bucket is exhausted"

    # A position sitting ABOVE its 30% take-profit. Frozen policy supplies tp_pct.
    monkeypatch.setattr(
        "sniper.services.memecoin_fastlane.frozen_policy_from_position",
        lambda pos: type("P", (), {"stop_pct": 70.0, "tp_pct": 30.0})())

    pos = _pos(37273, entry=1.0)
    # First tick: never quoted, so reason is "heartbeat" -- but it is the BOOTSTRAP, not a refresh.
    # Starving it is self-sealing: no quote -> age stays None -> classified a refresh forever.
    assert demand(pos, display_mid=1.35, trail_pct=0.0, derisk_pct=0.0) is True, \
        "the bootstrap exact quote was refused, so the position can never evaluate any rail"

    # Now it HAS a live exact price and sits above its 30% take-profit -> near_tp -> rail bucket.
    clock.advance(1.0)
    assert demand(pos, display_mid=1.35, trail_pct=0.0, derisk_pct=0.0) is True, \
        "a live take-profit was refused because heartbeats drained the bucket"


def test_rail_bucket_still_caps_a_runaway_predicate(conn, monkeypatch):
    """The backstop must survive the fix — an unbounded rail loop is still refused eventually."""
    clock = FakeClock()
    demand = build_demand_fn(conn, near_pct=10.0, heartbeat_s=60.0, clock=clock)
    monkeypatch.setattr(
        "sniper.services.memecoin_fastlane.frozen_policy_from_position",
        lambda pos: type("P", (), {"stop_pct": 70.0, "tp_pct": 30.0})())

    granted = sum(
        1 for i in range(5000)
        if demand(_pos(40000 + i, entry=1.0), display_mid=1.35, trail_pct=0.0, derisk_pct=0.0))
    assert granted < 5000, "rail bucket must still cap a runaway predicate"


def test_research_429_does_not_gag_the_funded_gt_path(monkeypatch):
    """A research-batch 429 must leave the funded entry path callable.

    Live symptom: `real entry REFUSED ... provider_rate_limit: v4 could not read: rate_limit` for
    three separate coins, while the funded lane itself had made no failing request.
    """
    gt.reset_gt_state()
    clock = FakeClock()

    # A research/price-batch 429 sets the SHARED cooldown only.
    gt._start_cooldown(clock(), {}, 60.0, funded=False)
    assert gt.cooldown_active(clock) is True, "research cooldown should suppress research"
    assert gt.cooldown_active(clock, funded=True) is False, \
        "a research 429 must not gag the funded path"

    calls = []

    def fake_http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        return 200, {"ok": True}, None

    got = gt.gt_get_json("https://api.geckoterminal.com/api/v2/x", http=fake_http, clock=clock)
    assert got == {"ok": True}
    assert calls, "funded call must actually reach the provider during a research cooldown"

    gt.reset_gt_state()


def test_funded_429_still_suppresses_both(monkeypatch):
    """When the provider refuses the FUNDED call, back off everywhere — that one is real."""
    gt.reset_gt_state()
    clock = FakeClock()

    def http_429(method, url, headers=None, body=None, timeout=10):
        return 429, {}, {}

    got = gt.gt_get_json("https://api.geckoterminal.com/api/v2/x", http=http_429, clock=clock)
    assert got is None
    assert gt.cooldown_active(clock, funded=True) is True
    assert gt.cooldown_active(clock) is True, "a funded 429 must also hold research back"

    gt.reset_gt_state()
