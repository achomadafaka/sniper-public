"""Offline bid-cache contracts: units, spend ceilings, freshness and stalled refreshes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import time

import pytest
from solders.pubkey import Pubkey


POOL = str(Pubkey.from_bytes(bytes([1]) * 32))
OTHER_POOL = str(Pubkey.from_bytes(bytes([2]) * 32))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def cache(priority=None, tip=None, clock=None, **kwargs):
    from sniper.execution.solana_entry_bids import EntryBidCache
    return EntryBidCache(priority, tip, clock=clock or Clock(), wall_clock=lambda: 1_800_000_000,
                         **kwargs)


def fees(values):
    return [{"slot": i + 100, "prioritizationFee": value} for i, value in enumerate(values)]


def tip_sample(value, timestamp=1_800_000_000):
    return [{"time": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
             "landed_tips_25th_percentile": 0.001,
             "landed_tips_50th_percentile": 0.001,
             "landed_tips_75th_percentile": 0.001,
             "landed_tips_95th_percentile": value,
             "landed_tips_99th_percentile": 0.1}]


def test_cold_fallback_preserves_total_priority_fee_after_cu_reduction():
    bid = cache().select(pool=POOL, cu_limit=200_000, tip_lamports=5_000_000)
    assert bid["micro_lamports_per_cu"] == 7_600_000
    assert bid["priority_fee_lamports"] == 1_520_000
    assert bid["combined_fee_lamports"] == 6_520_000
    assert bid["priority_sample_micro_lamports_per_cu"] is None
    assert bid["priority_source"] == "fallback_cold"


def test_account_filtered_p95_is_unit_price_and_bound_to_requested_pool():
    c = cache(lambda pool: fees([0] * 18 + [8_000_001, 90_000_000]))
    c.register(POOL)
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=200_000)
    assert bid["priority_sample_micro_lamports_per_cu"] == 8_000_001
    assert bid["priority_fee_lamports"] == 1_600_001
    assert bid["priority_source"] == "account_filtered_p95"
    assert c.select(pool=OTHER_POOL, cu_limit=200_000)["priority_source"] == "fallback_cold"


def test_valid_zero_observation_uses_the_fallback_floor_and_is_distinct_from_unknown():
    # operator 2026-10-23: a warm pool's floor is the 3.8M uL/CU constant (760,000 lamports at 200k CU), no longer
    # the cold fallback's total (Alpha 2026-10-16 rule, kept behind MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK).
    c = cache(lambda pool: fees([0]))
    c.register(POOL)
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=200_000)
    assert bid["priority_sample_micro_lamports_per_cu"] == 0
    assert bid["priority_source"] == "account_filtered_p95"
    assert bid["micro_lamports_per_cu"] == 3_800_000
    assert bid["priority_fee_lamports"] == 760_000


@pytest.mark.parametrize("cu,tip,price,unit,fee", [
    (300_001, 5_000_000, 90_000_000, 5_066_649, 1_520_000),
    (200_000, 1_000_000, 90_000_000, 27_600_000, 5_520_000),
    (400_000, 5_000_000, 3_800_000, 3_800_000, 1_520_000),
])
def test_integer_caps_include_ceiling_rounding(cu, tip, price, unit, fee):
    c = cache(lambda pool: fees([price]))
    c.register(POOL)
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=cu, tip_lamports=tip)
    assert bid["micro_lamports_per_cu"] == unit
    assert bid["priority_fee_lamports"] == fee
    assert bid["combined_fee_lamports"] <= 6_520_000
    assert bid["cap_saturated"] is (price > unit)


def test_stale_snapshot_falls_back_without_changing_last_good_stamp():
    clock = Clock()
    response = fees([9_000_000])
    c = cache(lambda pool: response, clock=clock)
    c.register(POOL)
    c._refresh_once()
    clock.now = 31
    response.clear()
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=200_000)
    assert bid["priority_fee_lamports"] == 1_520_000
    assert bid["priority_source"] == "fallback_stale"
    assert bid["priority_sample_age_s"] == 31
    assert bid["priority_failure"] == "empty"


def test_replayed_or_regressing_rpc_slots_cannot_refresh_freshness():
    clock = Clock()
    response = fees([9_000_000])
    c = cache(lambda pool: response, clock=clock)
    c.register(POOL)
    c._refresh_once()
    clock.now = 31
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=200_000)
    assert bid["priority_source"] == "fallback_stale"
    assert bid["priority_sample_age_s"] == 31
    assert bid["priority_failure"] == "stale_source"
    response[0]["slot"] = 99
    clock.now = 32
    c._refresh_once()
    assert c.select(pool=POOL, cu_limit=200_000)["priority_sample_age_s"] == 32
    response[0]["slot"] = 101
    clock.now = 33
    c._refresh_once()
    assert c.select(pool=POOL, cu_limit=200_000)["priority_source"] == "account_filtered_p95"


def test_last_good_sample_survives_failure_but_failure_does_not_renew_it():
    clock = Clock()
    response = fees([9_000_000])
    c = cache(lambda pool: response, clock=clock)
    c.register(POOL)
    c._refresh_once()
    clock.now = 1
    response.clear()
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=200_000)
    assert bid["priority_source"] == "account_filtered_p95"
    assert bid["priority_fee_lamports"] == 1_800_000
    assert bid["priority_sample_age_s"] == 1
    assert bid["priority_failure"] == "empty"


def test_selected_metadata_and_status_are_detached_from_shared_snapshots():
    c = cache(lambda pool: fees([9_000_000]))
    c.register(POOL)
    c._refresh_once()
    c.select(pool=POOL, cu_limit=200_000)["priority_sample_micro_lamports_per_cu"] = 0
    c.status()["pools"][POOL]["sample_micro_lamports_per_cu"] = 0
    assert c.select(pool=POOL, cu_limit=200_000)["priority_fee_lamports"] == 1_800_000


@pytest.mark.parametrize("response,reason", [
    (None, "unavailable"), ([], "empty"), ({"error": {"code": 429}}, "rate_limit"),
    ({"error": {"code": 403}}, "rejected"),
    ([{"slot": 10}], "malformed"), (fees([True]), "malformed"),
    (fees([1.0]), "malformed"), (fees([float("nan")]), "malformed"),
    (fees([-1]), "malformed"), (fees([2**64]), "malformed"),
    ([{"slot": True, "prioritizationFee": 0}], "malformed"),
    ([{"slot": 1, "prioritizationFee": 0}] * 2, "malformed"),
    (fees([0, 1]) + [{"slot": 999}], "malformed"),
    (fees([0] * 151), "malformed"),
])
def test_bad_priority_data_never_becomes_fresh_zero(response, reason):
    c = cache(lambda pool: response)
    c.register(POOL)
    c._refresh_once()
    bid = c.select(pool=POOL, cu_limit=400_000)
    assert bid["priority_sample_micro_lamports_per_cu"] is None
    assert bid["priority_sample_age_s"] is None
    assert bid["priority_fee_lamports"] == 1_520_000
    assert bid["priority_failure"] == reason


@pytest.mark.parametrize("error,reason", [
    (TimeoutError("secret-key"), "timeout"),
    (ConnectionError("secret-key"), "transport"),
    (RuntimeError("secret-key"), "provider_error"),
])
def test_exceptions_are_bounded_secret_free_and_do_not_fabricate_samples(error, reason):
    def fail(pool):
        raise error
    c = cache(fail)
    c.register(POOL)
    c._refresh_once()
    assert c.select(pool=POOL, cu_limit=400_000)["priority_failure"] == reason
    assert "secret-key" not in str(c.status())


@pytest.mark.parametrize("reason", ["timeout", "transport", "rejected", "rate_limit",
                                  "malformed", "stale_source", "future_source"])
def test_adapter_failure_reason_is_preserved_without_exception_text(reason):
    class EntryBidReadError(RuntimeError):
        pass
    error = EntryBidReadError("secret-key")
    error.reason = reason
    def fail(pool):
        raise error
    c = cache(fail)
    c.register(POOL)
    c._refresh_once()
    assert c.select(pool=POOL, cu_limit=400_000)["priority_failure"] == reason
    assert "secret-key" not in str(c.status())


@pytest.mark.parametrize("reason", ["secret-key", "", None, 429, ["timeout"]])
def test_unknown_adapter_failure_reasons_are_never_exposed(reason):
    class EntryBidReadError(RuntimeError):
        pass
    error = EntryBidReadError("secret-key")
    error.reason = reason
    def fail():
        raise error
    c = cache(tip=fail)
    c._refresh_once()
    assert c.status()["tip_failure"] == "provider_error"
    assert "secret-key" not in str(c.status())


@pytest.mark.parametrize("sol,lamports", [
    (0, 1_000_000), (0.0001, 1_000_000), (0.001, 1_000_000),
    (0.00100000001, 1_000_001), (0.0025, 2_500_000), (0.01, 5_000_000),
])
def test_nozomi_sol_percentile_ceil_and_minimum_cap(sol, lamports):
    c = cache(tip=lambda: tip_sample(sol))
    c._refresh_once()
    assert c.tip_lamports() == lamports
    assert c.status()["tip_source"] == "nozomi_p95"


def test_raw_nozomi_floor_remains_visible_when_tip_cap_saturates():
    clock = Clock()
    c = cache(tip=lambda: tip_sample(0.006503527), clock=clock)
    c._refresh_once()
    assert c.tip_lamports() == 5_000_000
    status = c.status()
    assert status["tip_sample_lamports"] == 6_503_527
    assert status["tip_lamports"] == 5_000_000
    assert status["tip_cap_saturated"] is True
    clock.now = 31
    status = c.status()
    assert status["tip_sample_lamports"] == 6_503_527
    assert status["tip_source"] == "fallback"
    assert status["tip_cap_saturated"] is False


def test_unknown_or_below_cap_tip_does_not_report_cap_saturation():
    c = cache(tip=lambda: tip_sample(0.0025))
    assert c.status()["tip_sample_lamports"] is None
    assert c.status()["tip_cap_saturated"] is False
    c._refresh_once()
    assert c.status()["tip_sample_lamports"] == 2_500_000
    assert c.status()["tip_cap_saturated"] is False


@pytest.mark.parametrize("sample,reason", [
    (None, "unavailable"), ([], "empty"), ([{}], "malformed"),
    (tip_sample(True), "malformed"), (tip_sample("0.001"), "malformed"),
    (tip_sample(float("nan")), "malformed"), (tip_sample(float("inf")), "malformed"),
    (tip_sample(-0.001), "malformed"), (tip_sample(0.002, 1_799_999_900), "stale_source"),
    (tip_sample(0.002, 1_800_000_001), "future_source"),
])
def test_unknown_or_old_tip_floor_uses_fallback(sample, reason):
    c = cache(tip=lambda: sample)
    c._refresh_once()
    assert c.tip_lamports() == 5_000_000
    assert c.status()["tip_failure"] == reason
    assert c.status()["tip_sample_age_s"] is None


def test_tip_stales_from_source_time_not_only_fetch_time():
    clock = Clock()
    c = cache(tip=lambda: tip_sample(0.002, 1_799_999_975), clock=clock)
    c._refresh_once()
    assert c.tip_lamports() == 2_000_000
    clock.now = 6
    assert c.tip_lamports() == 5_000_000


def test_tip_and_pool_rpc_calls_share_one_second_aggregate_pacing():
    clock = Clock()
    calls = []
    def priority(pool):
        calls.append((clock.now, pool))
        return fees([0])
    def tip():
        calls.append((clock.now, "tip"))
        return tip_sample(0.002)
    c = cache(priority, tip, clock=clock)
    c.register(POOL)
    c.register(OTHER_POOL)
    for step in range(23):
        clock.now = step / 2
        c._refresh_once()
    assert len(calls) == 12
    assert [at for at, kind in calls if kind == "tip"] == [0, 10]
    assert all(b[0] - a[0] >= 1 for a, b in zip(calls, calls[1:]))
    assert c.select(pool=OTHER_POOL, cu_limit=400_000)["priority_source"] == "account_filtered_p95"


def test_pool_registration_is_validated_deduplicated_and_bounded_under_threads():
    c = cache(lambda pool: fees([0]), max_pools=4)
    for invalid in (None, "", "not-a-pool", "0" * 44, True, POOL + " "):
        assert c.register(invalid) is False
    pools = [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in range(1, 65)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(c.register, pools * 2))
    assert c.status()["pending_pools"] <= 4
    c._refresh_once()
    assert len(c.status()["pools"]) <= 4
    assert c.register(POOL) is True
    assert c.register(POOL) is True


@pytest.mark.parametrize("cu,tip", [(True, 0), (0, 0), (1.0, 0),
    (1_400_001, 0), (200_000, True), (200_000, -1), (200_000, 5_000_001)])
def test_selection_rejects_invalid_cu_or_tip_budget(cu, tip):
    with pytest.raises(ValueError):
        cache().select(pool=POOL, cu_limit=cu, tip_lamports=tip)


def test_disabled_cache_does_not_register_or_start_network_work():
    def fail(*args):
        raise AssertionError("disabled cache called provider")
    c = cache(fail, fail, enabled=False)
    assert c.register(POOL) is False
    c.start()
    c._refresh_once()
    assert c.status()["running"] is False
    assert c.select(pool=POOL, cu_limit=200_000)["priority_source"] == "fallback_disabled"
    c.close()


def test_stalled_worker_cannot_block_entry_reads_and_close_is_bounded():
    entered, release = Event(), Event()
    def stalled(pool):
        entered.set()
        assert release.wait(2)
        return fees([9_000_000])
    c = cache(stalled, clock=time.monotonic)
    c.register(POOL)
    c.start()
    assert entered.wait(1)
    started = time.monotonic()
    try:
        for _ in range(1_000):
            assert c.select(pool=POOL, cu_limit=200_000)["priority_fee_lamports"] == 1_520_000
        c.close()
        assert time.monotonic() - started < 0.2
        assert c.status()["running"] is True
    finally:
        release.set()
        c._thread.join(1)
    assert c.status()["running"] is False
