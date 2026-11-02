"""Native speculative checks yield to fresh wallet snapshots without restarting the proof."""
import json
import collections
import time
from types import SimpleNamespace

import pytest
from eth_abi import encode

from sniper.services import memecoin_bsc_fast_lane as bsc
from test_memecoin_bsc_fast_lane_live import SUPPLY, _payload, live56


class RefreshClock:
    def __init__(self, live, *, recover=True, interrupt=None):
        self.live, self.recover, self.interrupt = live, recover, interrupt
        self.now, self.age, self.refresh_at = 10.0, 3.3, None
        self.waits, self.refreshes = [], 0
        self.live.snapshot._last["read_at"] = time.time() - self.age

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        for lock in (self.live.paced._background_lock, self.live.paced._lock):
            assert lock.acquire(blocking=False), "speculative wait held the account lock"
            lock.release()
        self.waits.append(seconds)
        self.now += max(seconds, 1e-6)
        self.age += max(seconds, 1e-6)
        # poll_once takes3.3s, THEN the owner sleeps9s: freshness lapses before refresh starts.
        if self.age >= 12.3 and self.refresh_at is None:
            self.refresh_at = self.now
            self.live.snapshot.refreshing.set()
            self.refreshes += 1
            if self.interrupt is not None:
                self.interrupt()
        if self.refresh_at is not None and self.recover and self.now - self.refresh_at >= 3.3:
            self.age = 3.3  # read_at is the essential refresh's START, never its completion.
            self.refresh_at = None
            self.live.snapshot.refreshing.clear()
            self.live.paced._last = self.now  # its fourth essential request just used the account.
        self.live.snapshot._last["read_at"] = time.time() - self.age


def setup_job(live56, monkeypatch, *, recover=True, interrupt=None):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S", 9.0)
    live, mint = live56[0]["live"], _payload()["mint"]
    for name in ("_verdicts", "_facts", "_facts_at", "_stats"):
        getattr(live.cache, name).pop(mint, None)
    live.cache.note_candidates([dict(mint=mint, quote=bsc.WBNB,
                                    pool=bsc.our_pair(mint, bsc.WBNB), observed_at=time.time())],
                               now=time.time())
    clock = RefreshClock(live, recover=recover, interrupt=interrupt)
    live.paced.clock, live.paced.sleep, live.paced.min_gap_s = clock, clock.sleep, 1.1
    return live, mint, clock


def full_safety_network(live, clock, calls):
    """Real code, route, storage-probe and sell simulation readers; only HTTP is synthetic."""
    from sniper.execution import evm_token_safety as safety
    def base(method, url, *, body, timeout=10, **kwargs):
        assert not live.snapshot.refreshing.is_set()
        # The fake clock owns snapshot age; wall time between two lines is host load.
        assert clock.age < 9.0
        request = json.loads(body)
        calls.append((clock(), request, timeout))
        def answer(row):
            if row["method"] == "eth_chainId":
                result = "0x38"
            elif row["method"] == "eth_getCode":
                result = "0x6001"
            else:
                data = row["params"][0]["data"]
                if data == "0x313ce567":
                    result = hex(18)
                elif data == "0x18160ddd":
                    result = hex(SUPPLY)
                elif data.startswith("0xd06ca61f"):
                    amount, count = int(data[10:74], 16), int(data[138:202], 16)
                    result = "0x" + encode(["uint256[]"], [[amount] * count]).hex()
                elif data.startswith(("0x70a08231", "0xdd62ed3e")):
                    result = safety._BIG_HEX
                else:
                    result = "0x"  # a successful sell eth_call.
            return {"jsonrpc": "2.0", "id": row["id"], "result": result}
        return 200, ([answer(row) for row in request] if isinstance(request, list)
                     else answer(request)), {}
    return base


def test_full_native_safety_finishes_across_snapshot_refresh_without_restarting(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.base = full_safety_network(live, clock, calls)
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None and result[0], (result, calls, dict(live.cache.counts))
    assert clock.refreshes >= 1 and clock.waits and max(clock.waits) <= 0.05
    assert live.cache.reason(mint, now=time.time()) is None
    assert live.cache.facts(mint) == (18, SUPPLY)
    assert live.cache.stats(mint, now=time.time()) is not None
    assert live.cache.counts["preparation_cancelled"] == 0
    requests = [row for _, request, _ in calls
                for row in (request if isinstance(request, list) else [request])]
    assert sum(row["method"] == "eth_getCode" for row in requests) == 1
    assert sum(row["method"] == "eth_call" and row["params"][0]["data"] == "0x18160ddd"
               for row in requests) == 1
    assert all(b[0] - a[0] >= 1.1 - 1e-6 for a, b in zip(calls, calls[1:]))
    assert all(timeout <= 10 for _, _, timeout in calls)


@pytest.mark.parametrize("interruption", ["entry", "inline", "generation", "identity", "expired"])
def test_snapshot_deferral_cancels_on_entry_or_lost_binding(live56, monkeypatch, interruption):
    generation = [0.0]
    def interrupt():
        if interruption == "entry":
            live.entry_active = True
        elif interruption == "inline":
            from concurrent.futures import Future
            live._safety_future = Future()
        elif interruption == "generation":
            generation[0] = 1.0
        elif interruption == "identity":
            live.cache.note_candidates([dict(mint=mint, quote="0x" + "23" * 20,
                                            pool=bsc.our_pair(mint, "0x" + "23" * 20))],
                                       now=time.time())
        else:
            priority, _ = live.cache._priority[mint]
            live.cache._priority[mint] = priority, time.time() - 601
    live, mint, clock = setup_job(live56, monkeypatch, interrupt=interrupt)
    live.cache.generation_fn = lambda: generation[0]
    calls = []
    live.paced.base = full_safety_network(live, clock, calls)
    assert live.cache.run_one(mint, now=time.time()) is None
    assert clock.refreshes == 1 and clock() < 20.0
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None
    assert not any(at > clock.refresh_at for at, _, _ in calls)


def test_never_recovered_snapshot_expires_job_at_existing_safety_ttl(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch, recover=False)
    calls = []
    live.paced.base = full_safety_network(live, clock, calls)
    assert live.cache.run_one(mint, now=time.time()) is None
    assert 60.0 <= clock() - 10.0 <= 60.05
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None
    assert all(at - 10.0 < 60 for at, _, _ in calls)


def test_snapshot_before_market_stats_defers_stats_without_repeating_facts(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls, stats_at, pressure_at = [], [], []
    base = full_safety_network(live, clock, calls)
    def pressured(method, url, **kwargs):
        answer = base(method, url, **kwargs)
        request = json.loads(kwargs["body"])
        rows = request if isinstance(request, list) else [request]   # the facts batch (cut 4)
        if any(row["method"] == "eth_call" and row["params"][0].get("data") == "0x18160ddd"
               for row in rows):
            clock.age, clock.refresh_at = 9.0, clock()
            pressure_at.append(clock())
            live.snapshot.refreshing.set()
        return answer
    live.paced.base = pressured
    original_stats = live.cache.stats_fn
    def stats(mint):
        assert not live.snapshot.refreshing.is_set()
        stats_at.append(clock())
        return original_stats(mint)
    live.cache.stats_fn = stats
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None and result[0]
    # Deferred past the whole refresh (3.3 s from the supply read's pressure), read exactly once.
    assert len(pressure_at) == 1 and len(stats_at) == 1
    assert stats_at[0] >= pressure_at[0] + 3.3 - 1e-6
    assert live.cache.facts(mint) == (18, SUPPLY)


@pytest.mark.parametrize("expired", [False, True])
def test_completed_checks_during_refresh_preserve_live_job_deadline(live56, monkeypatch, expired):
    live, mint, clock = setup_job(live56, monkeypatch)
    live.cache.facts_fn = lambda m: (18, SUPPLY)
    def safety(mint, usd=None):
        live.snapshot.refreshing.set()
        if expired:
            clock.now += 60.0
        return True, "completed actual checks"
    live.cache.safety_fn = safety
    result = live.cache.run_one(mint, now=time.time())
    if expired:
        assert result is None
        assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
        assert live.cache.facts(mint) is None
    else:
        assert result is not None and result[0]
        assert live.cache.reason(mint, now=time.time()) is None
        assert live.cache.facts(mint) == (18, SUPPLY)


def test_binding_race_after_unlocked_check_cannot_publish(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    generation = [0.0]
    live.cache.generation_fn = lambda: generation[0]
    calls = []
    base = full_safety_network(live, clock, calls)
    def raced(method, url, **kwargs):
        # The physical admission callback must not read execution-generation files under locks.
        owned = not live.paced._background_lock.acquire(blocking=False)
        assert owned
        generation[0] = 1.0
        return base(method, url, **kwargs)
    live.paced.base = raced
    assert live.cache.run_one(mint, now=time.time()) is None
    assert len(calls) == 1
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None


def test_provider_not_sent_exception_is_not_a_snapshot_retry():
    from sniper.execution.evm_rpc_pool import RequestNotSent
    from sniper.services.memecoin_rh_fast_lane import PacedHttp
    failure, sends = RequestNotSent("downstream read limit"), []
    def base(*args, **kwargs):
        sends.append(1)
        raise failure
    cache = SimpleNamespace(admission_fn=lambda: True, deferral_fn=lambda: True,
                            ttl_s=60.0, counts=collections.Counter())
    reads = bsc.PreparationReads(cache, PacedHttp(base, min_gap_s=0), "https://bsc.invalid")
    with pytest.raises(RequestNotSent) as error:
        reads("POST", reads.url, body=json.dumps({"id": 1, "method": "eth_getCode"}))
    assert error.value is failure and sends == [1] and reads.cancelled.is_set()


def test_late_closed_probe_does_not_dispatch_or_cancel_next_job(live56, monkeypatch):
    from sniper.execution.evm_rpc_pool import RequestNotSent
    live, mint, clock = setup_job(live56, monkeypatch)
    calls, instances = [], []
    factory = live.cache.http_factory
    def capture():
        reads = factory()
        instances.append(reads)
        return reads
    live.cache.http_factory = capture
    live.paced.base = full_safety_network(live, clock, calls)
    assert live.cache.run_one(mint, now=time.time())[0]
    old, current = instances[0], capture()
    before = len(calls)
    with pytest.raises(RequestNotSent):
        old("POST", old.url, body=json.dumps({"id": 1, "method": "eth_getCode", "params": []}))
    assert len(calls) == before and old.closed.is_set() and not current.cancelled.is_set()


@pytest.mark.parametrize("boundary", ["deferred_sleep", "request_timeout"])
def test_deadline_crossing_is_not_sent_without_negative_wait_or_provider_penalty(monkeypatch, boundary):
    from sniper.execution import evm_rpc_pool as pool, evm_wallet
    from sniper.execution.evm_rpc_pool import RequestNotSent
    from sniper.services.memecoin_rh_fast_lane import PacedHttp
    sends, sleeps, records = [], [], []
    clock_values = iter([0.0, 59.99, 60.01] if boundary == "deferred_sleep"
                        else [0.0, 59.98, 59.99, 60.01])
    def clock():
        return next(clock_values, 60.01)
    def sleep(seconds):
        sleeps.append(seconds)
        if seconds < 0:
            raise ValueError("negative sleep")
    cache = SimpleNamespace(admission_fn=lambda: boundary == "request_timeout",
                            deferral_fn=lambda: True, ttl_s=60.0, counts=collections.Counter())
    paced = PacedHttp(lambda *a, **kw: sends.append(kw) or (200, {"result": "0x6001"}, {}),
                      min_gap_s=0, clock=clock, sleep=sleep)
    reads = bsc.PreparationReads(cache, paced, "https://bsc.invalid")
    if boundary == "request_timeout":
        original_try = paced.try_background
        def checked_try(*args, **kwargs):
            assert kwargs["timeout"] > 0, "expired timeout reached the pacer"
            return original_try(*args, **kwargs)
        paced.try_background = checked_try
    monkeypatch.setattr(pool, "pool_for", lambda url: [url])
    monkeypatch.setattr(pool, "chain_of", lambda url: "56")
    monkeypatch.setattr(pool, "record", lambda *args: records.append(args))
    monkeypatch.setattr(pool, "penalize", lambda *args: records.append(args))
    with pytest.raises(RequestNotSent):
        evm_wallet._rpc(reads.url, "eth_getCode", ["0x" + "01" * 20, "latest"], http=reads)
    assert not records and not sends and all(seconds >= 0 for seconds in sleeps)

