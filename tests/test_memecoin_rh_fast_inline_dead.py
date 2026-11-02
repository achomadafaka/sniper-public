"""A dead 4663 inline safety host (Alpha 2026-10-02): QuickNode answered every call with an SSL
TLSV1_ALERT_INTERNAL_ERROR, so 2 of 2 live inline checks failed silently. Each transport failure is
logged with its class and message; three in a row mark the host dead for 10 minutes, during which a
cache miss falls back at once instead of spending its deadline; the heartbeat counts it. The research
rotation treats a transport failure on a slot as 'slot unusable', never as a spent quota."""
import logging
import socket
import ssl
import sqlite3
import time
import urllib.error

import pytest

from sniper.execution import research_rotation_4663 as rr
from sniper.services import memecoin_rh_fast_lane as lane

URL = "https://inline.example/k/"
TLS = urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _seam(answers, clock):
    calls = []

    def base(method, target, headers=None, body=None, timeout=10):
        calls.append(target)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0, clock=clock)
    return lane.lane_inline_http(URL, paced, base=base), paced, calls


def test_three_transport_failures_mark_the_inline_host_dead_for_ten_minutes(caplog):
    clock = Clock()
    http, paced, calls = _seam([TLS, TLS, TLS], clock)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(urllib.error.URLError):
                http("POST", URL)
    assert paced.dead() and paced.counts["transport_errors"] == 3 and paced.counts["dead_marked"] == 1
    assert sum("URLError" in r.message and "TLSV1_ALERT_INTERNAL_ERROR" in r.message for r in caplog.records) == 3
    with pytest.raises(lane.InlineHostDead):
        http("POST", URL)
    assert len(calls) == 3                                          # no request while dead
    clock.t += 601
    assert not paced.dead()


def test_a_success_resets_the_run_and_an_http_error_is_not_transport():
    clock = Clock()
    http, paced, _calls = _seam([TLS, TLS, (200, {"result": "0x1"}, {}), TLS, TLS,
                                 urllib.error.HTTPError(URL, 500, "boom", {}, None)], clock)
    for expect in (urllib.error.URLError, urllib.error.URLError, None, urllib.error.URLError,
                   urllib.error.URLError, urllib.error.HTTPError):
        if expect is None:
            http("POST", URL)
        else:
            with pytest.raises(expect):
                http("POST", URL)
    assert not paced.dead() and paced.counts["transport_errors"] == 4


def _live(host, safety_fn):
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.inline_host, live.inline_safety_fn, live.inline_deadline_s = host, safety_fn, 2.0

    class Cache:
        usd = 20.0
    live.cache = Cache()
    return live


def _timing_out(timeouts):
    def base(method, target, headers=None, body=None, timeout=10):
        timeouts.append(timeout)
        time.sleep(timeout)
        raise socket.timeout("timed out")
    return base


def test_reads_cut_by_the_checks_own_deadline_never_mark_the_host_dead():
    """Gamma MED on 84c020a34: a slow check cuts its parallel reads at once; the host is alive."""
    paced = lane.PacedHttp(min_gap_s=0.0)
    http = lane.lane_inline_http(URL, paced, base=_timing_out([]))
    for _ in range(3):
        with lane.inline_deadline(time.monotonic() + 0.05):
            with pytest.raises(OSError):
                http("POST", URL)
    assert paced.counts["transport_errors"] == 0 and not paced.dead()


def test_three_uncut_timeouts_still_mark_the_host_dead():
    paced = lane.PacedHttp(min_gap_s=0.0)
    timeouts = []
    http = lane.lane_inline_http(URL, paced, base=_timing_out(timeouts))
    for _ in range(3):
        with lane.inline_deadline(time.monotonic() + 5.0):
            with pytest.raises(OSError):
                http("POST", URL, timeout=0.01)          # the read's own timeout, not the deadline's
    assert timeouts == [0.01] * 3 and paced.counts["transport_errors"] == 3 and paced.dead()


def test_a_cache_miss_on_a_dead_host_falls_back_without_a_check():
    clock = Clock()
    host = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    for _ in range(3):
        host.transport(False)
    live = _live(host, lambda *a, **k: pytest.fail("no inline check while the host is dead"))
    rec = {}
    assert live._inline("0xmint", rec) == "inline_host_dead" and rec["safety_path"] == "inline"
    assert "safety_ms" not in rec                                   # not run: unmeasured, never 0


def test_a_refusal_during_a_host_failure_is_the_hosts_not_the_coins():
    """Live 2026-10-02 (rh_fast_lane_live ids 3, 4): a TLS alert read as 'inline_unsafe:code read
    failed (fail-closed)'."""
    host = lane.PacedHttp(min_gap_s=0.0, clock=Clock())

    def safety(mint, usd=None):
        host.transport(False)                       # the seam saw the TLS failure; the reader swallowed it
        return False, "code read failed (fail-closed)"
    assert _live(host, safety)._inline("0xmint", {}) == "inline_host_error"
    assert _live(host, lambda mint, usd=None: (False, "honeypot"))._inline("0xmint", {}) == \
        "inline_unsafe:honeypot"


def test_the_heartbeat_counts_transport_failures_and_dead_markings(tmp_path):
    clock = Clock()
    http, paced, _calls = _seam([TLS, TLS, TLS], clock)
    for _ in range(3):
        with pytest.raises(urllib.error.URLError):
            http("POST", URL)

    class Counts:
        counts = {"candidates_msgs": 0, "signals": 0, "verdicts_ok": 0, "verdicts_unsafe": 0,
                  "verdict_errors": 0, "cpu_ms": 0.0, "calls": 0, "http_429": 0}

        def size(self):
            return 0
    db = str(tmp_path / "lane.db")
    lane.write_heartbeat(db, Counts(), Counts(), Counts(), paced)
    row = sqlite3.connect(db).execute("SELECT inline_transport_errors, inline_dead_marked, inline_dead "
                                      "FROM rh_fast_lane_heartbeat").fetchone()
    assert row == (3, 1, 1)


def _rotation(clock):
    env = {rr.BASE: "https://base.robinhood-mainnet.quiknode.pro/k1/",
           rr.BASE + "_2": "https://spare.robinhood-mainnet.quiknode.pro/k2/"}
    return rr.Rotation(env, clock=clock, log=lambda line: None)


def test_a_transport_failure_on_a_research_slot_makes_it_unusable_never_spent():
    clock = Clock()
    rot = _rotation(clock)
    base, spare = rot.slots[0][1], rot.slots[1][1]

    def dead(target):
        raise TLS
    for _ in range(3):
        with pytest.raises(urllib.error.URLError):
            rot.send(base, dead)
    active, slots = rot.stats()
    assert active == 2 and slots[0]["quota"] == 0 and not slots[0]["spent"] and slots[0]["unusable"]
    seen = []
    rot.send(base, lambda target: seen.append(target) or (200, {"result": "0x1"}, {}))
    assert seen == [spare]
    clock.t += 601
    assert rot.active()[0] == 1


@pytest.mark.parametrize("fault", ["quota", "transport", "rate_limit"])
def test_background_provider_failures_never_cache_an_unsafe_coin(fault):
    clock = Clock()
    rot = _rotation(clock)
    paced = lane.PacedHttp(min_gap_s=0, clock=clock)
    def check(mint, usd=None):
        if fault == "rate_limit":
            paced.pause()
        else:
            rot._note(1, fault == "quota", transport=fault == "transport")
        return False, "code read failed (fail-closed)"
    cache = lane.SafetyCache(lane.background_safety(check, rot, paced), ttl_s=60, usd=50)
    assert cache.run_one("0xmint", now=1000) is None
    assert cache.reason("0xmint", now=1000) == "no_safety_verdict"
    assert cache.counts["verdict_errors"] == 1


def test_background_safety_still_caches_a_real_token_refusal():
    clock = Clock()
    rot = _rotation(clock)
    paced = lane.PacedHttp(min_gap_s=0, clock=clock)
    cache = lane.SafetyCache(lane.background_safety(lambda *a, **kw: (False, "honeypot"), rot, paced),
                             ttl_s=60, usd=50)
    cache.run_one("0xmint", now=1000)
    assert cache.reason("0xmint", now=1000) == "unsafe:honeypot"
    assert cache.counts["verdicts_unsafe"] == 1


@pytest.mark.parametrize("reason", ["code read failed (fail-closed)",
                                   "sell-route probe failed (fail-closed)",
                                   "sell_sim_rpc_unknown: result unavailable", "safety_deadline_exceeded"])
def test_answered_or_partial_provider_failure_stays_absent_in_the_safety_cache(reason):
    clock = Clock()
    rot = _rotation(clock)
    paced = lane.PacedHttp(min_gap_s=0, clock=clock)
    cache = lane.SafetyCache(lane.background_safety(lambda *a, **kw: (False, reason), rot, paced),
                             ttl_s=60, usd=50)
    assert cache.run_one("0xmint", now=1000) is None
    assert cache.reason("0xmint", now=1000) == "no_safety_verdict"


@pytest.mark.parametrize("status,reply", [(503, {"error": {"code": -32603}}),
                                         (200, {}), (200, []), (200, {"result": "not-hex"}),
                                         (200, [{"id": 1, "result": "0x1"}])])
def test_quote_only_provider_failure_cannot_poison_the_prefetch_cache(status, reply):
    clock = Clock();rot = _rotation(clock);paced = lane.PacedHttp(min_gap_s=0, clock=clock)
    def check(mint, usd=None):
        rot.send(rot.slots[0][1], lambda target: (status, reply))
        return False, "configured sell venue returned no executable quote (route=rh_fast_v4_direct)"
    cache = lane.SafetyCache(lane.background_safety(check, rot, paced), ttl_s=60, usd=50)
    assert cache.run_one("0xmint", now=1000) is None
    assert cache.reason("0xmint", now=1000) == "no_safety_verdict"


def test_clean_contract_revert_remains_a_cacheable_token_refusal():
    clock = Clock();rot = _rotation(clock);paced = lane.PacedHttp(min_gap_s=0, clock=clock)
    def check(mint, usd=None):
        rot.send(rot.slots[0][1], lambda target: (200, {"error": {"code": 3, "message": "execution reverted"}}))
        return False, "honeypot_sim_reverted: sell reverted"
    cache = lane.SafetyCache(lane.background_safety(check, rot, paced), ttl_s=60, usd=50)
    cache.run_one("0xmint", now=1000)
    assert cache.reason("0xmint", now=1000).startswith("unsafe:honeypot")


def test_deadline_cut_read_never_sleeps_for_a_transport_retry(monkeypatch):
    from sniper.execution import evm_token_safety as safety
    clock = Clock()
    monkeypatch.setattr(lane.time, 'monotonic', clock)

    def base(method, target, headers=None, body=None, timeout=10):
        clock.t += timeout
        raise socket.timeout('read cut at deadline')

    monkeypatch.setattr(safety.time, 'sleep', lambda *_: pytest.fail('deadline entered transport backoff'))
    paced = lane.PacedHttp(min_gap_s=0.0, clock=clock)
    http = lane.lane_inline_http(URL, paced, base=base)
    with lane.inline_deadline(1000.05):
        with pytest.raises(safety.EvmSafetyDeadlineExceeded):
            safety._read_with_retry(lambda _: http('POST', URL), None)
    assert paced.counts['transport_errors'] == 0 and not paced.dead()
