"""The BSC lane's armed warm feeder on the shared warm set (lambda + Beta, Alpha GO 2026-10-14).

73% of armed 56 buys (10-10..10-14, n=237) were coins an armed wallet had decided on within 24 h,
while the presignal warm fed only new PancakeSwap pairs (one process: 750 preparations, 15 verdicts).
A cached safety verdict sent in 11 ms and landed S+1; an inline one took 443-1439 ms and landed
S+2..S+7. The feeder keeps the shared set's coins, with their lane routes, in the safety cache."""
import os
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_rh_fast_lane as rh
from test_memecoin_bsc_fast_lane_live import SUPPLY, _payload, live56  # noqa: F401 - fixture

A, B, C = ("0x" + c * 40 for c in ("a1", "b2", "c3"))


@pytest.fixture(autouse=True)
def warm_enabled(monkeypatch):
    monkeypatch.setenv("MEMECOIN_BSC_PRESIGNAL_WARM_ENABLED", "1")
    monkeypatch.setattr(rh, "WARM_STATS", rh.WarmStatsPacer(0.0))      # no cross-test slot wait


def _route(db_path, mint, quote, pool, *, rows=1):
    db = sqlite3.connect(db_path)
    try:
        db.executescript(bsc._SCHEMA)
        for i in range(rows):
            db.execute("INSERT INTO bsc_lane_events (tx_hash, mode, mint, pool, quote, reason, sent_epoch, "
                       "recv_epoch, handoff_ms, busy_ms, cpu_ms, lock_busy, written_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("0x%064x" % i, "live", mint, pool, quote, "test", 0, 0, 0, 0, 0, 0, "t"))
        db.commit()
    finally:
        db.close()


def _armed(monkeypatch, *, hour, window, sold=()):
    """The shared helpers' answers by window (their own reads are pinned in Lambda's tests)."""
    def bought(ledger_path, chain, since_iso):
        assert chain == "56"
        return set(hour) if since_iso >= _armed.hour_since else set(window)
    monkeypatch.setattr(m, "armed_bought_mints", bought)
    monkeypatch.setattr(m, "armed_sold_mints", lambda ledger_path, chain, since_iso: set(sold))


def _feed(live, lane, warm, routes=None, *, now):
    from datetime import datetime, timezone
    _armed.hour_since = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()
    return bsc.feed_warm_set(live.cache, warm, routes or {}, ledger_path="unused",
                             db_path=lane["db_path"], now=now)


def test_the_shared_set_feeds_the_cache_by_tier_with_each_coin_s_lane_route(live56, monkeypatch):
    lane, *_ = live56
    live, now = lane["live"], time.time()
    _route(lane["db_path"], A, bsc.WBNB, bsc.our_pair(A))
    _route(lane["db_path"], B, "0x" + "9b" * 20, "0x" + "bd" * 20)
    _armed(monkeypatch, hour={A}, window={A, B}, sold={C})            # C: no lane route, not warmed
    warm = {"hour": set(), "recent": set()}
    routes = _feed(live, lane, warm, now=now)
    assert set(routes) == {A, B}
    assert live.cache._priority[A][0] == 0 and live.cache._priority[B][0] == 1
    assert C not in live.cache._priority
    assert live.cache._identities[B][:2] == ("0x" + "9b" * 20, "0x" + "bd" * 20)
    assert live.cache.warm_tier == {A: 0, B: 1}
    counters = bsc.lane_counters(live)
    assert (counters["warm_set"], counters["warm_routed"]) == (3, 2)
    assert live.cache.next_due(now) == A                               # tier 0 first


def test_the_latest_lane_signal_names_the_route(tmp_path):
    db = str(tmp_path / "lane.db")
    _route(db, A, bsc.WBNB, "0x" + "01" * 20)
    _route(db, A, "0x" + "9b" * 20, "0x" + "02" * 20)        # graduated: a newer route
    assert bsc.lane_routes(db, {A.upper().replace("0X", "0x"), B}) == {A: ("0x" + "9b" * 20, "0x" + "02" * 20)}


def test_an_unreadable_lane_file_keeps_the_last_routes(live56, monkeypatch):
    lane, *_ = live56
    live, now = lane["live"], time.time()
    _route(lane["db_path"], A, bsc.WBNB, bsc.our_pair(A))
    _armed(monkeypatch, hour={A}, window={A})
    warm = {"hour": set(), "recent": set()}
    routes = _feed(live, lane, warm, now=now)
    lane["db_path"] = os.path.join(os.path.dirname(lane["db_path"]), "missing", "lane.db")
    routes = _feed(live, lane, warm, routes, now=now + 10)
    assert routes == {A: (bsc.WBNB, bsc.our_pair(A))} and live.cache.warm_tier == {A: 0}


def test_a_tier_one_coin_the_warmer_prepared_sends_from_the_cache_and_counts_as_tier_one(live56, monkeypatch):
    """The signal notes its own coin at priority 0 before the lookup: the evidence still names the
    coin's armed tier, so the per-tier hit rate measures the feeder, not the signal."""
    lane, _, net, sender, _ = live56
    live, payload = lane["live"], _payload()
    mint = payload["mint"]
    for name in ("_verdicts", "_facts", "_facts_at", "_stats"):
        getattr(live.cache, name).pop(mint, None)
    _route(lane["db_path"], mint, payload["quote"], payload["pool"])
    _armed(monkeypatch, hour=set(), window={mint})
    now = time.time()
    _feed(live, lane, {"hour": set(), "recent": set()}, now=now)
    live.cache.facts_fn = lambda m_: (18, SUPPLY)
    live.cache.safety_fn = lambda m_, usd=None: (True, "armed warm")
    assert live.cache.next_due(now) == mint and live.cache.run_one(mint, now=now)[0]
    live.inline_safety_fn = lambda *a, **k: pytest.fail("a warmed coin repeated safety inline")
    rec = lane["dispatch"].on_datagram(payload, os.getpid(), os.getuid(), time.time())
    assert sender.raws and rec["safety_path"] == "cache", rec
    assert live.cache.lookups["1:hit"] == 1 and "0:hit" not in live.cache.lookups
    assert bsc.lane_counters(live)["warm_tiers"]["lookups"] == {"1:hit": 1}


def test_a_warm_job_waits_one_second_after_an_entry(live56):
    lane, *_ = live56
    live = lane["live"]
    assert live.cache.admission_fn()
    live.entry_ended_at = time.monotonic()
    assert not live.cache.admission_fn()
    live.entry_ended_at = time.monotonic() - bsc.WARM_ENTRY_COOLDOWN_S - 0.1
    assert live.cache.admission_fn()


def test_the_warm_stats_slot_is_waited_for_on_the_worker_and_gives_up_absent():
    clock = {"t": 0.0}
    slots = iter([False, False, True])
    pacer = type("P", (), {"take": lambda self: next(slots)})()
    sleeps = []
    assert bsc.take_warm_stats(1.0, pacer=pacer, clock=lambda: clock["t"],
                               sleep=lambda s: sleeps.append(s) or clock.__setitem__("t", clock["t"] + s))
    assert sleeps == [0.05, 0.05]
    busy = type("P", (), {"take": lambda self: False})()
    assert not bsc.take_warm_stats(0.2, pacer=busy, clock=lambda: clock["t"],
                                   sleep=lambda s: clock.__setitem__("t", clock["t"] + s))


def test_a_busy_warm_stats_slot_leaves_the_verdict_without_stats_not_a_guess(live56, monkeypatch):
    lane, *_ = live56
    live = lane["live"]
    mint = A
    _route(lane["db_path"], mint, bsc.WBNB, bsc.our_pair(mint))
    _armed(monkeypatch, hour={mint}, window={mint})
    now = time.time()
    _feed(live, lane, {"hour": set(), "recent": set()}, now=now)
    monkeypatch.setattr(bsc, "take_warm_stats", lambda *a, **k: False)
    live.cache.stats_fn = lambda m_: pytest.fail("a warm stats read without the shared slot")
    live.cache.facts_fn = lambda m_: (18, SUPPLY)
    live.cache.safety_fn = lambda m_, usd=None: (True, "armed warm")
    live.cache.run_one(mint, now=now)
    assert mint not in live.cache._stats and live.cache.counts["stats_missing"] >= 1


def test_the_lane_starts_the_warm_feeder_only_with_presignal_warming_on(live56, monkeypatch):
    lane, *_ = live56
    started = []

    class Recorder:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name

        def start(self):
            started.append(self.name)
    monkeypatch.setattr(bsc, "build_bsc_lane", lambda *a, **k: lane)
    monkeypatch.setattr(threading, "Thread", Recorder)
    bsc.start_bsc_lane({}, log=lambda line: None)
    assert "bsc-fast-warmset" in started
    started.clear()
    lane["dispatch"].presignal_warm_enabled = False
    bsc.start_bsc_lane({}, log=lambda line: None)
    assert "bsc-fast-warmset" not in started and "bsc-fast-safety" in started


def test_an_entry_s_end_time_is_set_before_it_stops_counting_as_active(live56):
    """Gamma LOW 1: a capacity check between the two statements saw no entry and an old end time."""
    lane, _, _net, sender, _ = live56
    live, seen = lane["live"], []

    class Watched(type(live)):
        def __setattr__(self, name, value):
            if name == "entry_active" and value is False:
                seen.append(time.monotonic() - self.entry_ended_at)
            super().__setattr__(name, value)
    live.__class__ = Watched
    lane["dispatch"].on_datagram(_payload(), os.getpid(), os.getuid(), time.time())
    assert seen and 0 <= seen[-1] < 0.5


def test_capacity_is_checked_again_after_the_warm_stats_slot_wait(live56, monkeypatch):
    """Gamma LOW 2: capacity was checked only before the up-to-3 s slot wait, so the DexScreener call
    could go out during an entry that started meanwhile. Still checked first, so a busy account
    refuses at once instead of waiting for the slot."""
    lane, *_ = live56
    live, order = lane["live"], []
    _route(lane["db_path"], A, bsc.WBNB, bsc.our_pair(A))
    _armed(monkeypatch, hour={A}, window={A})
    now = time.time()
    _feed(live, lane, {"hour": set(), "recent": set()}, now=now)
    monkeypatch.setattr(bsc, "take_warm_stats", lambda *a, **k: order.append("slot") or True)
    monkeypatch.setattr(bsc.PreparationReads, "wait_for_capacity", lambda self: order.append("capacity"))
    live.cache.stats_fn = lambda m_: order.append("stats") or {"price": 1.0}
    live.cache.facts_fn = lambda m_: (18, SUPPLY)
    live.cache.safety_fn = lambda m_, usd=None: (True, "armed warm")
    live.cache.run_one(A, now=now)
    assert order[:4] == ["capacity", "slot", "capacity", "stats"]
