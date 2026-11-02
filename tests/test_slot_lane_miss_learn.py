"""A PumpSwap pool miss is learned at once and the buy retried on the lane ONCE (2026-09-27).

In the lane's first 25.7 h, 5 of the 7 PumpSwap fills that did not go via the lane were `pool_unknown`
refusals; the hand-off learned each pool on the prefetch thread's next tick and wrote its row 131-782
ms later, past the lane's 0.5 s signal-age gate, and J7 or the watcher filled them. MissLearner reads
the pool on its own thread at once and puts the same buy back on the lane, under Alpha's conditions:
(1) the retry pass hands off EVERY outcome but a fill; (2) 250 ms budget from the miss, then hand off
at once, and learn_ms is logged on every miss; (3) at most 8 misses wait and one read per pool is in
flight; (4) a signature is never re-queued twice, and one source buy has at most one funded attempt.

The buy and the pool reads are the real Tekkerrss 54bzG3hj miss (test_slot_lane_handoff's fixture).
"""
import contextlib
import dataclasses
from types import SimpleNamespace

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_onchain_lane as onchain
from sniper.services import memecoin_slot_lane as lane
from test_slot_lane_handoff import (  # noqa: F401  (db is a fixture)
    FX, MINT_54, SOL_USD, _Clock, _PoolRpc, _buy, _detections, _factory, _snap, db)


class Timers:
    """threading.Timer stand-in: the test decides when a budget runs out."""

    def __init__(self):
        self.made = []

    def __call__(self, interval, fn, args=()):
        t = SimpleNamespace(interval=interval, fn=fn, args=args, started=False, cancelled=False,
                            daemon=False)
        t.start = lambda: setattr(t, "started", True)
        t.cancel = lambda: setattr(t, "cancelled", True)
        self.made.append(t)
        return t

    def fire(self, i=-1):
        t = self.made[i]
        t.fn(*t.args)


class Rpc(_PoolRpc):
    """Each read takes 40 ms of the test clock once the lane is set up (the heartbeat's p50 is 38)."""

    def __init__(self, fail=False):
        super().__init__()
        self.calls, self.fail, self.clock = [], fail, None

    def __call__(self, method, params):
        self.calls.append(method)
        if self.clock is not None:
            self.clock.now += 0.04
        if self.fail:
            raise RuntimeError("node down")
        return super().__call__(method, params)


def _setup(db, monkeypatch, *, rpc=None, mode="live", traced_status="filled"):
    clock, lines, retried, traced = _Clock(), [], [], []
    rpc = rpc or Rpc()
    rpc.clock = clock
    pools = ps.PumpSwapPoolCache(rpc=rpc)
    handoff = lane.Handoff(pools=pools, conn_factory=_factory(db.path), max_age_s=45.0, clock=clock)
    offers = []
    real_offer = handoff.offer

    def offer(buy, reason, sol_usd):
        offers.append(reason)
        real_offer(buy, reason, sol_usd)
    handoff.offer = offer
    timers = Timers()
    learner = lane.MissLearner(pools=pools, put=retried.append, log=lines.append, clock=clock,
                               handoff_fn=lambda buy, reason: handoff.offer(buy, reason, SOL_USD),
                               timer=timers)
    armed = {FX["tekkerrss"]["source_user_id"]}
    ln = lane.SlotLane(conn=db.conn, executor={}, build_fn=None, snapshot_fn=lambda: _snap(clock),
                       armed_ids_fn=lambda: armed, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T23:12:00+00:00", lock_key="sol-test",
                       mode=mode, amm_pools=pools, clock=clock, handoff=handoff,
                       miss_learner=learner)

    def fake_traced(self, buy, snap, facts, t0):
        traced.append(buy.signature)
        return {"status": traced_status, "reason": traced_status}
    monkeypatch.setattr(lane.SlotLane, "_traced", fake_traced)

    @contextlib.contextmanager
    def free_lock(key, blocking=False):
        yield True
    monkeypatch.setattr("sniper.execution.exec_lock.wallet_exec_lock", free_lock)
    return SimpleNamespace(clock=clock, lines=lines, retried=retried, traced=traced, pools=pools,
                           handoff=handoff, offers=offers, timers=timers, learner=learner, lane=ln)


def _onchain_hook_calls(db):
    seen = []
    onchain.sweep_onchain_detections(
        db.conn, lambda _c, ev: seen.append(ev.fomo_event_id) or {"status": "rejected"}, cursor=0,
        now_fn=lambda: "2026-09-26T23:12:02+00:00", max_age_s=45.0,
        approved_ids_fn=lambda _c, _n: {FX["tekkerrss"]["source_user_id"]}, deferred={})
    return seen


def test_a_miss_is_learned_at_once_and_the_same_buy_retried_on_the_lane(db, monkeypatch):
    s = _setup(db, monkeypatch)
    buy = _buy(FX["tekkerrss"], s.clock)
    assert s.lane.handle(buy)["reason"] == "pool_unknown_learning"
    assert s.offers == [] and s.handoff.flush() == []       # the learner owns it: no hand-off
    assert s.timers.made[0].interval == 0.25 and s.timers.made[0].started
    s.learner.learn_once(buy.pool)                            # two reads, 40 ms each
    assert s.retried == [buy] and s.learner.is_retry(buy.signature)
    assert s.timers.made[0].cancelled
    assert s.lines == ["[memecoin] slot lane miss-learn Tekkerrss %s S=%d learn_ms=80.0 read_ms=80.0 "
                       "-> retry" % (buy.pool[:8], buy.slot)]
    result = s.lane.handle(buy)                               # the retry pass
    assert result["status"] == "filled" and s.traced == [buy.signature]
    assert s.offers == [] and s.handoff.flush() == [] and _detections(db.conn) == []
    assert s.pools.get(buy.pool)["base_mint"] == MINT_54


def test_a_lane_own_pool_miss_records_why_it_cannot_submit(db, monkeypatch):
    from sniper.services import memecoin_slot_skips as skips
    skips.drain()
    s = _setup(db, monkeypatch)
    buy = _buy(FX['tekkerrss'], s.clock)
    assert s.lane.handle(buy)['reason'] == 'pool_unknown_learning'
    assert [(sig, outcome, reason) for sig, outcome, reason, _ in skips.drain()] == [
        (buy.signature, 'rejected', 'pool_unknown_learning')]
    s.timers.fire()
    assert [(sig, outcome, reason) for sig, outcome, reason, _ in skips.drain()] == [
        (buy.signature, 'rejected', 'pool_learn_budget')]


@pytest.mark.parametrize("step, reason", [
    ("age", "retry:slot_lane_signal_too_old"),
    ("stale", "retry:slot_lane_stale_sol_usd"),
    ("failed", "retry:slot_window_expired"),
    ("aborted", "retry:aborted"),
])
def test_every_refusal_or_failure_on_the_retry_pass_is_handed_off(db, monkeypatch, step, reason):
    status = {"failed": "slot_window_expired", "aborted": "aborted"}.get(step, "filled")
    s = _setup(db, monkeypatch, traced_status=status)
    buy = _buy(FX["tekkerrss"], s.clock)
    s.lane.handle(buy)
    s.learner.learn_once(buy.pool)
    if step == "age":
        s.clock.now += 0.6                                    # the lane queue was backed up
    if step == "stale":
        s.lane.snapshot_fn = lambda: lane.PrefetchSnapshot(
            sol_usd=SOL_USD, usdc_balance=400.0, sol_balance=0.5, blockhash="H",
            last_valid_height=1, observed_mono={"sol_usd": s.clock.now - 60,
                                                "balances": s.clock.now, "blockhash": s.clock.now})
    s.lane.handle(buy)
    assert s.offers == [reason]
    [done] = s.handoff.flush()
    assert done["outcome"] == "inserted" and done["mint"] == MINT_54
    assert len(s.traced) + len(_onchain_hook_calls(db)) >= 1


def test_a_filled_or_shadow_retry_is_not_handed_off(db, monkeypatch):
    s = _setup(db, monkeypatch, mode="shadow")
    buy = _buy(FX["tekkerrss"], s.clock)
    s.lane.handle(buy)
    s.learner.learn_once(buy.pool)
    assert s.lane.handle(buy)["status"] == "shadow" and s.offers == []


def test_past_the_budget_the_buy_is_handed_off_and_the_late_read_never_retries(db, monkeypatch):
    s = _setup(db, monkeypatch)
    buy = _buy(FX["tekkerrss"], s.clock)
    s.lane.handle(buy)
    s.clock.now += 0.25
    s.timers.fire()                                           # the budget runs out first
    assert s.offers == ["pool_unknown"] and s.retried == []
    assert s.lines[-1].endswith("learn_ms=250.0 read_ms=- -> handoff:budget")
    s.clock.now += 0.4
    s.learner.learn_once(buy.pool)                            # the read lands late
    assert s.retried == [] and s.offers == ["pool_unknown"]
    assert s.lines[1:] == ["[memecoin] slot lane miss-learn late read %s read_ms=80.0 learned=1"
                           % buy.pool[:8]]                  # the late read's own time (2026-10-07)
    s.pools.learn_pending()
    [done] = s.handoff.flush()
    assert done["outcome"] == "inserted"
    assert _onchain_hook_calls(db) == ["onchain:" + buy.signature] and s.traced == []


@pytest.mark.parametrize("late", [False, True])
def test_a_failed_or_late_read_hands_off_and_logs_learn_ms(db, monkeypatch, late):
    s = _setup(db, monkeypatch, rpc=Rpc(fail=not late))
    buy = _buy(FX["tekkerrss"], s.clock)
    s.lane.handle(buy)
    s.clock.now += 0.3 if late else 0.05
    s.learner.learn_once(buy.pool)
    assert s.retried == [] and s.offers == ["pool_unknown"]
    assert s.lines[-1].endswith("-> handoff:late" if late else "-> handoff:unlearnable")
    assert ("learn_ms=380.0" if late else "learn_ms=90.0") in s.lines[-1]    # 1 read fails at 40 ms
    assert s.timers.made[0].cancelled


def test_one_read_per_pool_and_at_most_eight_misses_wait(db, monkeypatch):
    rpc = Rpc()
    s = _setup(db, monkeypatch, rpc=rpc)
    base = _buy(FX["tekkerrss"], s.clock)
    buys = [dataclasses.replace(base, signature="sig%d" % i) for i in range(9)]
    results = [s.lane.handle(b)["reason"] for b in buys]
    assert results == ["pool_unknown_learning"] * 8 + ["pool_unknown"]
    assert s.offers == ["pool_unknown"]                       # the ninth: handed off at once
    assert s.lines == ["[memecoin] slot lane miss-learn Tekkerrss %s S=%d learn_ms=0.0 read_ms=- "
                       "-> handoff:queue_full" % (base.pool[:8], base.slot)]
    assert s.learner._jobs.qsize() == 1                       # eight misses, one read
    s.learner.learn_once(s.learner._jobs.get())
    assert rpc.calls == ["getAccountInfo", "getMultipleAccounts"]
    assert [b.signature for b in s.retried] == ["sig%d" % i for i in range(8)]


def test_a_signature_is_never_requeued_twice(db, monkeypatch):
    s = _setup(db, monkeypatch, rpc=Rpc(fail=True))
    buy = _buy(FX["tekkerrss"], s.clock)
    assert s.learner.offer(buy) is True
    assert s.learner.offer(buy) is False                      # the same signature, never again
    s.learner.learn_once(buy.pool)
    s.timers.fire()                                           # a timer racing the settled read
    assert s.offers == ["pool_unknown"] and s.retried == []


@pytest.mark.parametrize("path", ["retry_fills", "retry_refused", "read_fails", "budget"])
def test_one_source_buy_has_at_most_one_funded_attempt(db, monkeypatch, path):
    """Lane attempts (the traced, reserving path) plus on-chain lane attempts from any hand-off row."""
    s = _setup(db, monkeypatch, rpc=Rpc(fail=path == "read_fails"))
    buy = _buy(FX["tekkerrss"], s.clock)
    s.lane.handle(buy)
    if path == "budget":
        s.clock.now += 0.3
        s.timers.fire()
    s.learner.learn_once(buy.pool)
    if path == "retry_refused":
        s.clock.now += 0.6
    for again in list(s.retried):
        s.lane.handle(again)
    s.pools.learn_pending()
    s.handoff.flush()
    funded = len(s.traced) + len(_onchain_hook_calls(db))
    assert funded <= 1
    assert funded == (0 if path == "read_fails" else 1)       # an unlearnable pool: no row, no lane
    assert len(s.retried) <= 1


def test_the_pool_cache_writes_only_under_its_lock():
    """The miss learner is a second writer beside the prefetch thread."""
    cache = ps.PumpSwapPoolCache(rpc=_PoolRpc())

    class Guarded(dict):
        def __setitem__(self, k, v):
            assert cache._lock.locked(), "pool written without the cache lock"
            super().__setitem__(k, v)

    class GuardedList(list):
        def append(self, v):
            assert cache._lock.locked(), "order written without the cache lock"
            super().append(v)
    cache._pools, cache._order = Guarded(), GuardedList()
    assert cache.learn(_buy(FX["tekkerrss"], _Clock()).pool) is True


def test_the_flag_off_starts_no_learner(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_real_runner as runner
    from test_slot_lane_pool_prewarm import _Rpc
    # This checks the miss-learner flag, not the unconditional E14 pool warm-up's two workers.
    monkeypatch.setattr(runner, "_momentum_read_pool", lambda: SimpleNamespace(submit=lambda *a: None))
    starts = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            starts.append(self.name)
    monkeypatch.setattr("threading.Thread", Thread)
    amm = type("Amm", (), {"pools": ps.PumpSwapPoolCache(rpc=_Rpc({}))})()
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    refresh = ["slot-lane-prefetch-sol_usd", "slot-lane-prefetch-balances",
               "slot-lane-prefetch-blockhash", "slot-lane-prefetch", "slot-lane"]
    for flag, expect in ((False, refresh), (True, ["slot-lane-miss-learn", *refresh])):
        monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_MISS_LEARN", flag)
        starts.clear()
        lane.start_slot_lane(rc, builders={"pump_amm": amm}, signal_start_fn=lambda *a, **k: None,
                             sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                             conn_factory=object, readonly_conn_factory=object)
        assert starts == expect


@pytest.mark.parametrize("read_s, outcome", [(0.01, "retry"), (0.4, "handoff:budget")])
def test_real_threads_settle_each_miss_once(read_s, outcome):
    """Real Timer, real learner thread: a slow read loses to the budget and never retries after."""
    import threading
    import time

    class SlowRpc(_PoolRpc):
        def __call__(self, method, params):
            time.sleep(read_s / 2)
            return super().__call__(method, params)
    done, retried, handed, lines = threading.Event(), [], [], []
    learner = lane.MissLearner(pools=ps.PumpSwapPoolCache(rpc=SlowRpc()), put=retried.append,
                               handoff_fn=lambda b, r: handed.append(r),
                               log=lambda line: (lines.append(line), done.set()))
    threading.Thread(target=learner.run, daemon=True).start()
    buy = _buy(FX["tekkerrss"], _Clock())
    assert learner.offer(buy)
    assert done.wait(2.0)
    time.sleep(read_s + 0.1)                                  # let a late read finish
    assert lines[0].endswith("-> " + outcome)
    # A read that loses to the budget logs its own time once it lands (2026-10-07), never a retry.
    assert len(lines) == (1 if outcome == "retry" else 2)
    assert outcome == "retry" or " late read " in lines[1]
    assert (len(retried), len(handed)) == ((1, 0) if outcome == "retry" else (0, 1))
