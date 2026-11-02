"""The 4663 inline host warms the safety cache while idle (spec 2026-10-14 rh-fast-cached-only rev 2
§3.6; operator via Alpha: Chainstack 'unlimited / plenty'). It runs only under cached-only, shares
the safety cache's queue with the research warmer, never caches a provider failure, and pauses
600 s (counted, alerted once) on the first 429 or quota answer."""
import threading

import pytest

from sniper.services import memecoin_rh_fast_lane as lane


class Host:
    def __init__(self):
        self.counts = {"calls": 0, "http_429": 0}

    def paused(self):
        return False

    def dead(self):
        return False

    def quota_spent(self):
        return False


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def cached_only(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)


def _cache(stats_fn=None):
    cache = lane.SafetyCache(lambda m, usd=None: (True, "research"), ttl_s=60, usd=50,
                             facts_fn=lambda m: (18, 10 ** 27), stats_fn=stats_fn)
    cache.note_candidates([{"mint": "0xa", "priority": 0}, {"mint": "0xb", "priority": 11}], now=1000.0)
    return cache


def _warmer(cache, host, clock, safety=None, alerts=None):
    return lane.InlineWarmer(cache, safety_fn=safety or (lambda m, usd=None: (True, "inline")),
                             facts_fn=lambda m: (18, 10 ** 27), host=host, gap_s=0.25,
                             stats_pacer=lane.WarmStatsPacer(1.0, clock=clock), clock=clock,
                             log=lambda *_: None,
                             alert_fn=(alerts.append if alerts is not None else lambda _t: None))


def test_the_warmer_verdicts_the_most_urgent_coin_on_the_inline_host(cached_only):
    cache = _cache()
    assert _warmer(cache, Host(), Clock()).step(1000.0) == 0.25
    assert cache._verdicts["0xa"][:2] == (True, "inline") and "0xb" not in cache._verdicts


def test_off_cached_only_the_warmer_never_reads(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", False, raising=False)
    called = []
    w = _warmer(_cache(), Host(), Clock(), safety=lambda m, usd=None: called.append(m) or (True, None))
    assert w.step(1000.0) == 1.0 and called == []


def test_a_429_pauses_ten_minutes_counts_alerts_once_and_caches_nothing(cached_only):
    cache, host, clock, alerts = _cache(), Host(), Clock(), []

    def limited(m, usd=None):
        host.counts["http_429"] += 1
        return False, "code read failed: HTTP 429"
    w = _warmer(cache, host, clock, safety=limited, alerts=alerts)
    w.step(1000.0)
    assert w.counts["pauses"] == 1 and "0xa" not in cache._verdicts     # no unsafe verdict cached
    assert w.paused_until == clock.t + 600.0 and len(alerts) == 1
    clock.t += 599.0
    assert w.step(1000.0) == 1.0 and w.counts["verdicts"] == 1         # still paused
    clock.t += 2.0
    w.step(1000.0)                                                     # a second pause
    assert w.counts["pauses"] == 2 and len(alerts) == 1                # alerted once per process
    clock.t += 601.0
    w.safety_fn_raw = lambda m, usd=None: (True, "inline")
    assert w.step(5000.0) == 0.25                                      # past the failed coins' backoff


def test_a_quota_answer_pauses_like_a_429(cached_only):
    clock = Clock()

    def quota(m, usd=None):
        raise RuntimeError("rpc error -32005: monthly request quota exceeded")
    w = _warmer(_cache(), Host(), clock, safety=quota)
    w.step(1000.0)
    assert w.counts["pauses"] == 1 and w.paused_until == clock.t + 600.0


def test_a_genuine_token_refusal_is_cached_and_does_not_pause(cached_only):
    cache = _cache()
    w = _warmer(cache, Host(), Clock(), safety=lambda m, usd=None: (False, "honeypot: sell reverted"))
    w.step(1000.0)
    assert cache._verdicts["0xa"][:2] == (False, "honeypot: sell reverted") and w.counts["pauses"] == 0


def test_a_coin_in_flight_is_skipped_by_the_second_worker():
    cache = _cache()
    started, release = threading.Event(), threading.Event()

    def slow(m, usd=None):
        started.set()
        release.wait(2.0)
        return True, "slow"
    t = threading.Thread(target=cache.run_one, args=("0xa",), kwargs=dict(now=1000.0, safety_fn=slow))
    t.start()
    assert started.wait(2.0)
    assert cache.next_due(1000.0) == "0xb"                              # 0xa is in flight
    assert cache.run_one("0xa", now=1000.0) is None                     # a racing second run returns
    release.set()
    t.join()
    assert cache._verdicts["0xa"][1] == "slow"


def test_stats_are_read_only_when_absent_and_through_the_shared_pacer(cached_only):
    reads = []
    cache, clock = _cache(stats_fn=lambda m: reads.append(m) or {"price": 1.0}), Clock()
    w = _warmer(cache, Host(), clock)
    w.step(1000.0)                                     # 0xa: stats absent, the pacer's slot is free
    w.step(1000.0)                                     # 0xb: inside the pacer's 1 s gap: not read
    assert reads == ["0xa"] and "0xb" in cache._verdicts


@pytest.mark.parametrize("on", [True, False])
def test_the_lane_builds_the_warmer_on_its_inline_host_and_its_cache(monkeypatch, tmp_path, on):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _setup
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_INLINE_WARM", on, raising=False)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")
    built = _setup(monkeypatch, tmp_path, "live")[0]
    try:
        warm = built["inline_warm"]
        if not on:
            assert warm is None
            return
        assert isinstance(warm, lane.InlineWarmer) and warm.cache is built["cache"]
        assert warm.host is built["inline_paced"] and warm.stats_pacer is lane.WARM_STATS
    finally:
        built["live"]._safety_pool.shutdown(wait=True)


def test_without_an_inline_host_there_is_no_warmer(monkeypatch, tmp_path):
    from test_memecoin_rh_fast_entry import _setup
    monkeypatch.delenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", raising=False)
    built = _setup(monkeypatch, tmp_path, "live")[0]
    assert built["inline_warm"] is None


def test_the_heartbeat_carries_the_warmers_counters(tmp_path):
    import json
    import sqlite3
    from types import SimpleNamespace
    from test_memecoin_rh_fast_safety import _dispatch
    d, cache, _t = _dispatch(tmp_path)
    d.inline_warm = SimpleNamespace(counts={"verdicts": 3, "pauses": 1}, paused_until=42.0)
    research = lane.PacedHttp(lambda *a, **k: (200, {}, {}), min_gap_s=0.0)
    lane.write_heartbeat(d.db_path, d, cache, research)
    (got,) = sqlite3.connect(d.db_path).execute("SELECT inline_warm FROM rh_fast_lane_heartbeat").fetchone()
    assert json.loads(got) == {"verdicts": 3, "pauses": 1, "paused_until": 42.0}


@pytest.mark.parametrize("state", ["paused", "dead"])
def test_a_paused_or_dead_inline_host_is_never_read(cached_only, state):
    # Gamma LOW 2 on 96bcf06de: the host check was untested.
    host, called = Host(), []
    setattr(host, state, lambda: True)
    w = _warmer(_cache(), host, Clock(), safety=lambda m, usd=None: called.append(m) or (True, None))
    assert w.step(1000.0) == 1.0 and called == [] and w.counts["verdicts"] == 0


def test_a_coin_another_worker_holds_is_a_skip_not_an_error_and_spends_no_stats_slot(cached_only):
    # Gamma LOW 3 on 96bcf06de: the race between next_due and run_one counted "errors" after
    # the warmer had already spent its WARM_STATS slot.
    cache, taken = _cache(), []
    w = _warmer(cache, Host(), Clock())

    class Pacer:
        def take(self):
            taken.append(1)
            return True
    w.stats_pacer = Pacer()
    cache._inflight.add("0xa")                         # the research warmer holds it
    cache.next_due = lambda now: "0xa"                 # ...after next_due handed it out
    assert w.step(1000.0) == w.gap_s
    assert w.counts["skipped"] == 1 and w.counts["errors"] == 0 and w.counts["verdicts"] == 0
    assert taken == []


@pytest.mark.parametrize("limited", [False, True])
def test_a_coin_claimed_between_the_check_and_run_one_is_a_skip_that_keeps_a_429_pause(cached_only, limited):
    # Gamma LOW on 4b6814d47: the second skip was untested and returned before the 429/quota check.
    cache, host, clock, alerts = _cache(), Host(), Clock(), []
    w = _warmer(cache, host, clock, alerts=alerts)

    def raced(mint, **kw):
        if limited:
            host.counts["http_429"] += 1                   # this worker's own read drew the 429
        cache._inflight.add(mint)                          # ...and the research warmer claims it now
        return None
    cache.run_one = raced
    assert w.step(1000.0) == w.gap_s
    assert w.counts["skipped"] == 1 and w.counts["errors"] == 0 and w.counts["verdicts"] == 0
    assert (w.counts["pauses"], len(alerts), w.paused_until > clock.t) == ((1, 1, True) if limited else (0, 0, False))


def test_the_warmers_seam_sends_one_read_at_a_time_spaced_by_the_call_gap():
    # Live 2026-10-14T20:11:18Z: 4 verdicts in ~2 s sent 68 reads, the slot scan's 12-wide tail
    # concurrently, and the Chainstack account (~49 reads per second, measured 20:14Z) answered 429.
    import time
    gap, starts, active, peak = 0.03, [], [0], [0]
    guard = threading.Lock()

    def base(method, url, headers=None, body=None, timeout=10):
        with guard:
            starts.append(time.monotonic())
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.005)
        with guard:
            active[0] -= 1
        return 200, {"result": "0x1"}, {}

    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0)
    http = lane.lane_warm_http("https://own-4663.example.test/key", paced, base, gap)
    threads = [threading.Thread(target=http, args=("POST", "https://own-4663.example.test/key"))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    assert len(starts) == 6 and peak[0] == 1
    assert min(b - a for a, b in zip(starts, starts[1:])) >= gap - 0.002
    assert paced.counts["calls"] == 6


def test_a_429_on_the_warmers_seam_pauses_the_inline_host():
    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0)
    http = lane.lane_warm_http("https://own-4663.example.test/key", paced,
                               lambda *a, **k: (429, {}, {}), 0.0)
    assert http("POST", "https://own-4663.example.test/key")[0] == 429
    assert paced.paused() and paced.counts["http_429"] == 1
    with pytest.raises(lane.ResearchPaused):
        http("POST", "https://own-4663.example.test/key")


def test_the_lane_gives_the_warmer_the_paced_seam_for_facts_and_safety(monkeypatch, tmp_path):
    from sniper.execution import evm_token_safety
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _setup
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_INLINE_WARM", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S", 0.125, raising=False)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")

    class Marker(Exception):
        pass

    made, seams = [], []

    def warm_seam(method, url, headers=None, body=None, timeout=10):
        raise Marker()

    def fake_warm_http(url, paced, base, gap_s, gate=None):
        made.append((url, paced, gap_s))
        return warm_seam
    real_build = evm_token_safety.build_evm_token_safety

    def build(*a, **k):
        seams.append(k.get("http"))
        return real_build(*a, **k)
    monkeypatch.setattr(lane, "lane_warm_http", fake_warm_http)
    monkeypatch.setattr(evm_token_safety, "build_evm_token_safety", build)
    built = _setup(monkeypatch, tmp_path, "live")[0]
    try:
        assert made == [("https://own-4663.example.test/key", built["inline_paced"].hosts[0].paced, 0.125)]           # the rotation set holds one pacer per host
        assert warm_seam in seams                      # the warmer's safety check reads on it
        with pytest.raises(Marker):                    # and so do its facts
            built["inline_warm"].facts_fn("0x" + "ab" * 20)
    finally:
        built["live"]._safety_pool.shutdown(wait=True)


def test_reads_queued_behind_a_429_are_not_sent():
    # Gamma LOW on ade03bb98: the pause check ran before the serial wait, so 11 queued reads still
    # went out to a host that had just answered 429.
    import time
    sent = []
    guard = threading.Lock()

    def base(method, url, headers=None, body=None, timeout=10):
        with guard:
            sent.append(url)
            first = len(sent) == 1
        if first:
            time.sleep(0.2)                    # the other 11 queue behind the serial read
        return (429, {}, {}) if first else (200, {"result": "0x1"}, {})

    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0)
    http = lane.lane_warm_http("https://own-4663.example.test/key", paced, base, 0.01)
    errors = []

    def read():
        try:
            http("POST", "https://own-4663.example.test/key")
        except lane.ResearchPaused as exc:
            errors.append(exc)
    threads = [threading.Thread(target=read) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1 and len(errors) == 11
    assert paced.counts["calls"] == 1 and paced.counts["http_429"] == 1
