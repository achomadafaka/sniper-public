"""Cancelled BSC preparation attempts yield within their existing priority."""
import pytest

from sniper.services.memecoin_bsc_fast_lane import BscSafetyCache, WBNB, our_pair


LOW, MID, HIGH = ("0x%040x" % i for i in (1, 2, 3))


def candidate(mint, *, priority=99, quote=WBNB, at=1000.0):
    return {"mint": mint, "quote": quote, "pool": our_pair(mint, quote),
            "priority": priority, "observed_at": at}


@pytest.fixture
def cache():
    generation = [0.0]
    prepared = BscSafetyCache(lambda mint, usd=None: (True, None), quotes={},
        generation_fn=lambda: generation[0], ttl_s=60.0, usd=50.0, clock=lambda: 1000.0)
    def cancelled(mint, usd=None):
        generation[0] += 1
        return True, None
    prepared.safety_fn = cancelled
    return prepared


def test_cancelled_low_address_yields_to_peers_then_retries_fairly(cache):
    cache.note_candidates([candidate(m) for m in (LOW, MID, HIGH)], now=1000.0)
    for index, mint in enumerate((LOW, MID, HIGH, LOW, MID, HIGH)):
        now = 1000.0 + index
        assert cache.next_due(now) == mint
        assert cache.run_one(mint, now=now) is None
        assert cache.reason(mint, now=now) == "no_safety_verdict"
        assert cache.facts(mint) is None and cache.stats(mint, now=now) is None
    assert cache.counts["preparation_cancelled"] == 6
    assert not cache._verdicts and not cache._retry_at


def test_priority_remains_strict_after_a_cancelled_attempt(cache):
    cache.note_candidates([candidate(LOW, priority=0), candidate(HIGH, priority=1)], now=1000.0)
    assert cache.run_one(LOW, now=1000.0) is None
    assert cache.next_due(1001.0) == LOW


def test_route_replacement_drop_and_expiry_clear_attempt_ordering(cache):
    cache.note_candidates([candidate(m) for m in (LOW, HIGH)], now=1000.0)
    assert cache.run_one(LOW, now=1000.0) is None
    assert cache.next_due(1001.0) == HIGH
    cache.note_candidates([candidate(LOW, quote="0x" + "ab" * 20, at=1002.0)], now=1002.0)
    assert "warm_worker_start_at" not in cache._warmup[LOW]
    assert cache.next_due(1002.0) == LOW
    assert cache.reason(LOW, now=1002.0) == "no_safety_verdict"
    cache._drop(LOW)
    assert cache.next_due(1003.0) == HIGH
    assert cache.next_due(1601.0) is None and cache.size() == 0
    assert cache.counts["candidate_expired"] == 1


def test_provider_backoff_still_excludes_the_highest_priority_candidate(cache):
    def unavailable(mint, usd=None):
        raise RuntimeError("offline provider failure")
    cache.safety_fn = unavailable
    cache.note_candidates([candidate(LOW, priority=0), candidate(HIGH, priority=1)], now=1000.0)
    assert cache.run_one(LOW, now=1000.0) is None
    retry_at = cache._retry_at[LOW]
    assert cache.next_due(retry_at - 0.001) == HIGH
    assert cache.next_due(retry_at) == LOW
    assert cache.reason(LOW, now=retry_at) == "no_safety_verdict"


def test_existing_refresh_boundaries_and_inline_verdict_order_remain(cache):
    cache.refresh_lead_s = 20.0
    cache.note_candidates([candidate(LOW, priority=0), candidate(HIGH, priority=3)], now=1000.0)
    cache.put(LOW, now=1000.0)
    cache.put(HIGH, now=1000.0)
    assert cache.next_due(1040.0) is None
    assert cache.next_due(1040.001) == LOW
    cache.put(LOW, now=1041.0)
    assert cache.next_due(1060.0) is None
    assert cache.next_due(1060.001) == HIGH
    cache.note_candidates([candidate(HIGH, priority=0, at=1061.0)], now=1061.0)
    assert cache.next_due(1102.0) == HIGH


def test_live_pressure_and_job_ownership_do_not_create_attempts(cache):
    cache.note_candidates([candidate(m) for m in (LOW, HIGH)], now=1000.0)
    cache.admission_fn = lambda: False
    assert cache.next_due(1000.0) is None
    assert cache.run_one(LOW, now=1000.0) is None
    cache.admission_fn = lambda: True
    cache._job_lock.acquire()
    try:
        assert cache.run_one(LOW, now=1000.0) is None
    finally:
        cache._job_lock.release()
    assert "warm_worker_start_at" not in cache._warmup[LOW]
    assert cache.next_due(1001.0) == LOW
    assert cache.counts["preparation_skipped"] == 2
