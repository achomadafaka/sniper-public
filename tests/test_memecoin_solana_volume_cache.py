"""A low partial sum must never reject an entry; complete zero must be able to."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.persistence import db_sol_volume as db
from sniper.services.memecoin_solana_volume_cache import VolumeCache

NOW = 1_800_000_000.0
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.ensure_tables(c)
    yield c
    c.close()


def coverage(c, **changes):
    row = dict(pool="vault", mint="Mint", venue="pump_amm", market_pool="Pool",
               quote_mint=WSOL, verified_since=NOW - 600, through=NOW,
               complete=1, reason="ok")
    row.update(changes)
    c.execute("DELETE FROM sol_volume_coverage_v1")
    c.execute("INSERT INTO sol_volume_coverage_v1 (%s) VALUES (%s)" %
              (",".join(row), ",".join("?" for _ in row)), tuple(row.values()))
    c.commit()


def swap(c, signature="sig", amount="1000000000", maker="Trader", ts=NOW - 20, index=0,
         payer="Trader"):
    c.execute("INSERT INTO sol_volume_swaps_v1 "
              "(pool,signature,event_index,block_ts,quote_atomic,maker,payer) VALUES (?,?,?,?,?,?,?)",
              ("vault", signature, index, ts, amount, maker, payer))
    c.commit()


def check(cache, **changes):
    args = dict(mint="Mint", venue="pump_amm", market_pool="Pool", source_signature="Source",
                mcap_usd=10_000.0, sol_usd=100.0, now_mono=100.0)
    args.update(changes)
    return cache.check(**args)


def refresh(c):
    cache = VolumeCache(clock=lambda: 100.0)
    cache.refresh(c, now=NOW, own_owners={"Us"}, published_mono=100.0)
    return cache


def test_complete_zero_refuses_but_exact_threshold_allows(conn):
    coverage(conn)
    assert check(refresh(conn)).state == "refuse"
    swap(conn, amount="2000000000")  # 2 SOL * $100 = $200 = 2% of $10,000.
    result = check(refresh(conn))
    assert result.state == "allow"
    assert result.volume_usd == 200.0 and result.required_usd == 200.0


def test_exact_source_signature_and_own_swaps_are_excluded(conn):
    coverage(conn)
    swap(conn, "Source", "3000000000")
    swap(conn, "Own", "4000000000", maker="Us")
    swap(conn, "source", "1000000000")  # Solana signatures are case-sensitive.
    result = check(refresh(conn))
    assert result.state == "refuse" and result.volume_usd == 100.0
    assert check(refresh(conn), source_signature="Absent").volume_usd == 400.0


@pytest.mark.parametrize("changes", [
    {"complete": 0}, {"verified_since": NOW - 299}, {"through": NOW - 6},
    {"through": NOW + 1}, {"verified_since": None}, {"quote_mint": "Junk"},
    {"reason": "decode_unknown"},
])
def test_incomplete_stale_future_or_unpriced_measurement_is_unknown(conn, changes):
    coverage(conn, **changes)
    assert check(refresh(conn)).state == "unknown"


def test_expiry_uses_measurement_age_not_cache_refresh_age(conn):
    coverage(conn, through=NOW - 4)
    cache = refresh(conn)
    assert check(cache, now_mono=100.5).state == "refuse"
    assert check(cache, now_mono=101.1).state == "unknown"
    assert check(cache, now_mono=99).state == "unknown"


@pytest.mark.parametrize("changes", [
    {"market_pool": "Other"}, {"mint": "mint"}, {"venue": "pump_curve"},
    {"mcap_usd": None}, {"mcap_usd": 0}, {"mcap_usd": float("nan")},
    {"sol_usd": None}, {"sol_usd": float("inf")}, {"source_signature": None},
    {"mcap_usd": 10 ** 1000},
])
def test_unknown_identity_or_price_passes(conn, changes):
    coverage(conn)
    assert check(refresh(conn), **changes).state == "unknown"


def test_usdc_decimals_and_block_time_window(conn):
    coverage(conn, quote_mint=USDC)
    swap(conn, amount="150000000")  # $150, independent of SOL/USD.
    swap(conn, "old", "9000000000", ts=NOW - 301)
    swap(conn, "future", "9000000000", ts=NOW + 1)
    result = check(refresh(conn), sol_usd=None)
    assert result.state == "refuse" and result.volume_usd == 150.0


def test_missing_own_identity_is_unknown_and_background_failure_clears_cache(conn):
    coverage(conn)
    cache = refresh(conn)
    cache.refresh(conn, now=NOW, own_owners=set(), published_mono=100)
    assert check(cache).state == "unknown"
    cache = refresh(conn)
    conn.execute("DROP TABLE sol_volume_coverage_v1")
    cache.refresh(conn, now=NOW, own_owners={"Us"}, published_mono=100)
    assert check(cache).state == "unknown"
    assert cache.status()["last_error"]


def test_readers_only_see_immutable_complete_publications(conn):
    coverage(conn)
    swap(conn, amount="2000000000")
    cache = refresh(conn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(lambda: [check(cache).state for _ in range(2000)])
        for _ in range(20):
            cache.refresh(conn, now=NOW, own_owners={"Us"}, published_mono=100)
        assert set(reader.result()) <= {"allow"}


@pytest.mark.parametrize("amount", ["-1", "NaN", "9" * 100])
def test_malformed_atomic_receipts_cannot_create_a_refusal(conn, amount):
    coverage(conn)
    swap(conn, amount=amount)
    assert check(refresh(conn)).state == "unknown"


def test_malformed_own_identity_cannot_certify_organic_volume(conn):
    coverage(conn)
    cache = refresh(conn)
    cache.refresh(conn, now=NOW, own_owners={None}, published_mono=100)
    assert check(cache).state == "unknown"


def test_own_routed_payer_is_excluded_even_when_event_user_is_router(conn):
    coverage(conn)
    swap(conn, amount="9000000000", maker="RouterPDA", payer="Us")
    assert check(refresh(conn)).volume_usd == 0.0


def test_producer_to_cache_requires_full_new_clean_window():
    import test_memecoin_solana_volume_evidence as producer
    c = producer.conn()
    try:
        producer.run(c, producer.BASE)
        producer.run(c, producer.BASE + 299)
        cache = VolumeCache(clock=lambda: 100.)
        args = dict(mint=producer.MINT, venue="pump_curve", market_pool=producer.MINT,
                    source_signature="Source", mcap_usd=10_000., sol_usd=100., now_mono=100.)
        cache.refresh(c, now=producer.BASE + 299, own_owners={"Us"}, published_mono=100.)
        assert cache.check(**args).state == "unknown"
        producer.run(c, producer.BASE + 300)
        cache.refresh(c, now=producer.BASE + 300, own_owners={"Us"}, published_mono=100.)
        result = cache.check(**args)
        assert result.state == "refuse" and result.volume_usd == 0.0
    finally:
        c.close()


def test_status_does_not_call_expired_coverage_ready(conn):
    coverage(conn)
    clock = [100.]
    cache = VolumeCache(clock=lambda: clock[0])
    cache.refresh(conn, now=NOW, own_owners={"Us"}, published_mono=100.)
    assert cache.status()["state"] == "ready"
    clock[0] = 106.
    assert cache.status()["state"] == "stale"
    assert cache.status()["pools"] == 0


@pytest.mark.parametrize("state", ["warming", "unknown", "error", "ready", "stale"])
def test_status_acknowledges_runtime_config_in_every_state(conn, state):
    clock = [100.]
    cache = VolumeCache(min_ratio=.031, max_age_s=4., clock=lambda: clock[0])
    if state in ("ready", "stale"):
        coverage(conn)
    if state == "error":
        conn.execute("DROP TABLE sol_volume_coverage_v1")
    if state != "warming":
        cache.refresh(conn, now=NOW, own_owners={"Us"}, published_mono=100.)
    if state == "stale":
        clock[0] = 105.
    status = cache.status()
    assert status["state"] == ("unknown" if state == "error" else state)
    assert {key: status.get(key) for key in (
        "enabled", "min_m5_mcap_ratio", "max_age_s", "window_s")} == {
        "enabled": True, "min_m5_mcap_ratio": .031, "max_age_s": 4., "window_s": 300.}
