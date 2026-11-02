"""4663 fast lane, cache miss: the inline safety read beside the inline inputs (Alpha GO (c),
2026-10-08, standing approval e97e4343; knob MEMECOIN_RH_FAST_PARALLEL_SAFETY, default off).

The worker only reads; the lane thread consumes the verdict inside the hook, after the
reservation. An inputs refusal abandons the read; a read that raises or overruns is a refusal."""
import sqlite3
import threading
import time

import pytest

from test_memecoin_rh_fast_entry import MINT, _go, _payload, _setup, _stats  # noqa: F401


@pytest.fixture
def dedicated(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    # The inline path's own tests: cached-only (2026-10-14, default on) is pinned off.
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", False, raising=False)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")
    built = _setup(monkeypatch, tmp_path, "live")
    cache = built[0]["cache"]
    for store in (cache._verdicts, cache._facts, cache._stats):
        store.pop(MINT, None)
    yield built
    built[0]["live"]._safety_pool.shutdown(wait=True)


@pytest.fixture
def knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_PARALLEL_SAFETY", True, raising=False)


def _attempts(ledger):
    return ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0]


def test_with_the_knob_the_safety_read_overlaps_the_inputs(dedicated, knob):
    built, sender, net, ledger = dedicated
    live = built["live"]
    safety_running, seen = threading.Event(), {}

    def safety(mint, usd=None):
        seen["thread"] = threading.current_thread().name
        safety_running.set()
        return True, None

    def facts(mint):
        assert safety_running.wait(1.0), "the safety read waited for the inputs"
        return 18, 10 ** 27
    live.inline_safety_fn, live.inline_facts_fn, live.stats_fn = safety, facts, lambda m: _stats()
    rec = _go(built, _payload())
    assert seen["thread"].startswith("fast-lane-safety") and rec["parallel_safety"] == "used", rec
    assert sender.raws and rec["safety_path"] == "inline", rec
    assert built["cache"].reason(MINT, now=time.time()) is None          # the pass is cached
    (col,) = sqlite3.connect(built["db_path"]).execute(
        "SELECT parallel_safety FROM rh_fast_lane_events").fetchone()
    assert col == "used"


def test_without_the_knob_the_safety_read_follows_the_inputs_on_the_lane_thread(dedicated):
    built, sender, net, ledger = dedicated
    live = built["live"]
    order = []
    live.inline_safety_fn = lambda m, usd=None: order.append(("safety", threading.current_thread().name)) \
        or (True, None)
    live.inline_facts_fn = lambda m: order.append(("facts", None)) or (18, 10 ** 27)
    live.stats_fn = lambda m: _stats()
    rec = _go(built, _payload())
    assert order == [("facts", None), ("safety", threading.current_thread().name)], order
    assert "parallel_safety" not in rec and sender.raws


def test_an_inputs_refusal_abandons_the_safety_read_and_caches_nothing(dedicated, knob):
    built, sender, net, ledger = dedicated
    live = built["live"]
    live.inline_safety_fn = lambda m, usd=None: (True, None)

    def facts(mint):
        raise RuntimeError("supply read failed")
    live.inline_facts_fn, live.stats_fn = facts, lambda m: _stats()
    rec = _go(built, _payload())
    assert rec["reason"] == "inline_facts_error:RuntimeError" and rec["parallel_safety"] == "abandoned"
    live._safety_future.result(timeout=1.0)                    # the read finished; its pass is unused
    assert built["cache"].reason(MINT, now=time.time()) == "no_safety_verdict"
    assert _attempts(ledger) == 0 and sender.raws == []


@pytest.mark.parametrize("safety, reason", [
    (lambda m, usd=None: (False, "honeypot"), "inline_unsafe:honeypot"),
    (lambda m, usd=None: 1 / 0, "inline_safety_error:ZeroDivisionError"),
    (lambda m, usd=None: (_ for _ in ()).throw(TimeoutError("read timed out")),
     "inline_safety_error:TimeoutError"),
])
def test_a_parallel_refusal_or_exception_is_never_a_pass(dedicated, knob, safety, reason):
    built, sender, net, ledger = dedicated
    live = built["live"]
    live.inline_safety_fn, live.inline_facts_fn = safety, lambda m: (18, 10 ** 27)
    live.stats_fn = lambda m: _stats()
    rec = _go(built, _payload())
    assert rec["reason"] == reason and rec["parallel_safety"] == "used" and not sender.raws, rec
    assert built["cache"].reason(MINT, now=time.time()) == "no_safety_verdict"


def test_an_overrunning_read_is_unsafe_and_the_next_miss_is_judged_sequentially(dedicated, knob):
    built, sender, net, ledger = dedicated
    live = built["live"]
    release = threading.Event()
    live.inline_deadline_s = 0.2
    live.inline_safety_fn = lambda m, usd=None: release.wait(2.0) and (True, None)
    live.inline_facts_fn, live.stats_fn = (lambda m: (18, 10 ** 27)), (lambda m: _stats())
    try:
        rec = _go(built, _payload())
        assert rec["reason"] == "inline_unsafe:safety_deadline_exceeded" and not sender.raws, rec
        assert built["cache"].reason(MINT, now=time.time()) == "no_safety_verdict"
        assert not live._safety_future.done()                 # the first read still runs
        _judged_while_busy(built, live, sender)
    finally:
        release.set()


def test_an_abandoned_read_never_refuses_the_next_miss(dedicated, knob):
    built, sender, net, ledger = dedicated
    live = built["live"]
    release = threading.Event()
    live.inline_safety_fn = lambda m, usd=None: release.wait(2.0) and (True, None)
    live.stats_fn = lambda m: _stats()

    def facts(mint):
        raise RuntimeError("supply read failed")
    live.inline_facts_fn = facts
    try:
        rec = _go(built, _payload())
        assert rec["reason"] == "inline_facts_error:RuntimeError" and rec["parallel_safety"] == "abandoned"
        assert not live._safety_future.done()                 # the abandoned read still runs
        _judged_while_busy(built, live, sender)
    finally:
        release.set()


def _judged_while_busy(built, live, sender):
    """A second miss while the earlier read runs: no new read, its hook checks it sequentially on
    the lane thread (the knob-off path), and it is judged, never refused busy."""
    seen = {}
    live.inline_safety_fn = lambda m, usd=None: seen.setdefault(
        "thread", threading.current_thread().name) and (True, None)
    live.inline_facts_fn = lambda m: (18, 10 ** 27)
    for store in (built["cache"]._facts, built["cache"]._stats):
        store.pop(MINT, None)
    p = _payload()
    p["snapshot"] = dict(p["snapshot"], read_at=time.time() + 2.0)
    again = _go(built, p)
    assert again["parallel_safety"] == "busy" and "busy" not in again["reason"], again
    assert seen["thread"] == threading.current_thread().name, seen     # sequential, lane thread
    assert again["safety_path"] == "inline" and sender.raws, again          # it reached the send
    assert built["cache"].reason(MINT, now=time.time()) is None, again   # judged: the pass is cached


def test_inline_duration_is_not_mislabeled_as_reservation_to_verdict_wait(dedicated, monkeypatch):
    from sniper.services import memecoin_rh_fast_lane as lane
    built, _, _, _ = dedicated
    live, clock = built['live'], [10.0]
    monkeypatch.setattr(lane.time, 'perf_counter', lambda: clock[0])
    def safety(mint, usd=None):
        clock[0] += 0.005
        return True, 'safe'
    def facts(mint):
        clock[0] += 0.050
        return 18, 10 ** 27
    def stats(mint):
        clock[0] += 0.100
        return _stats()
    live.inline_safety_fn, live.inline_facts_fn, live.stats_fn = safety, facts, stats
    rec = {}
    assert live._inline(MINT, rec) is None
    evidence = rec['execution_evidence']
    assert evidence.get('reservation_to_verdict_wait_ms') is None
    assert evidence['inline_safety_path_ms'] == pytest.approx(155.0)
    assert rec['safety_ms'] == pytest.approx(155.0)
    assert built['cache'].reason(MINT, now=time.time()) is None
