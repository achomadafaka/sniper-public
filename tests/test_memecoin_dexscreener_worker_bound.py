"""A stuck resolver cannot accumulate unlimited default transport workers."""
import threading

import pytest

from sniper.execution import solana_quote as q

# Read before the autouse fixture swaps them: the shipped bounds (Alpha, 2026-09-26).
SHIPPED_SLOTS = {role: sem._value for role, sem in q._ds_http_slots.items()}


@pytest.fixture(autouse=True)
def isolated_slots(monkeypatch):
    monkeypatch.setattr(q, "_ds_http_slots", {
        role: threading.BoundedSemaphore(4) for role in (False, True)})


def test_hung_research_workers_are_bounded_and_funded_still_runs(monkeypatch):
    release = threading.Event()
    workers = []
    funded_calls = []

    def http(method, url, *args, **kwargs):
        if "FUNDED" in url:
            funded_calls.append(url)
            return 200, {"pairs": []}, None
        workers.append(threading.current_thread())
        release.wait(5)
        return 200, {"pairs": []}, None

    monkeypatch.setattr(q, "_http", http)
    monkeypatch.setattr(q, "_DS_DEADLINE_S", 0.02)
    monkeypatch.setattr(q, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(q, "_ds_funded_cooldown_until", [0.0])
    try:
        # Repeated breaker expiry cannot free a still-running underlying request.
        for _ in range(8):
            with pytest.raises(TimeoutError):
                q._ds_default_http("GET", "RESEARCH")
        assert len(workers) == 4
        # Slots full: this call is refused locally and trips NO cooldown (Alpha 2026-09-26,
        # replacing Codex's original "cooldown active" assertion here).
        assert q.dexscreener_stats_batch_keyed([("solana", "RESEARCH")]) == {}
        assert not q.dexscreener_cooldown_active()
        assert not q.dexscreener_cooldown_active(funded=True)
        q.dexscreener_stats_batch_keyed([("solana", "FUNDED_BATCH")], funded=True)
        q.dexscreener_stats("FUNDED_SINGLE", funded=True)
        assert len(funded_calls) >= 2
        assert not q.dexscreener_cooldown_active(funded=True)
    finally:
        release.set()
        for worker in workers:
            worker.join(1)
    assert q._ds_default_http("GET", "FUNDED_HEALTHY") == (200, {"pairs": []}, None)


@pytest.mark.parametrize("fails", [False, True])
def test_worker_slot_is_released_after_healthy_or_failed_request(monkeypatch, fails):
    def http(*args, **kwargs):
        if fails:
            raise OSError("provider unavailable")
        return 200, {"pairs": []}, None

    monkeypatch.setattr(q, "_http", http)
    for _ in range(10):
        if fails:
            with pytest.raises(OSError, match="provider unavailable"):
                q._ds_default_http("GET", "TEST")
        else:
            assert q._ds_default_http("GET", "TEST")[0] == 200


def test_shipped_bounds_are_research_8_funded_4():
    assert SHIPPED_SLOTS == {False: 8, True: 4}


def test_a_healthy_burst_refuses_one_call_and_never_blanks_the_role(monkeypatch):
    # Nine concurrent research reads against eight slots: the ninth is refused as capacity, NO
    # cooldown starts, and the next read after the burst reaches the provider.
    monkeypatch.setattr(q, "_ds_http_slots", {False: threading.BoundedSemaphore(8),
                                              True: threading.BoundedSemaphore(4)})
    monkeypatch.setattr(q, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(q, "_ds_funded_cooldown_until", [0.0])
    monkeypatch.setattr(q, "_DS_DEADLINE_S", 5.0)
    release, inflight, served = threading.Event(), threading.Semaphore(0), []

    def http(method, url, *args, **kwargs):
        served.append(url)
        inflight.release()
        release.wait(5)
        return 200, {"pairs": []}, None

    monkeypatch.setattr(q, "_http", http)
    burst = [threading.Thread(target=q.dexscreener_stats, args=("BURST%d" % i,))
             for i in range(8)]
    for t in burst:
        t.start()
    for _ in range(8):
        assert inflight.acquire(timeout=5)                # all eight hold a slot
    refused_before = q._ds_capacity_refusals[False]
    assert q.dexscreener_stats("NINTH") is None           # the ninth: refused locally
    assert q._ds_capacity_refusals[False] == refused_before + 1
    assert not q.dexscreener_cooldown_active()
    release.set()
    for t in burst:
        t.join(5)
    q.dexscreener_stats("TENTH")                          # after the burst: reaches the provider
    assert served[-1].endswith("TENTH") and not q.dexscreener_cooldown_active()
