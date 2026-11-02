"""A V4 PoolKey derived from the provider's pair and VERIFIED by hash — no log read.

Spec context: the pool id IS `keccak(abi.encode(PoolKey))`, so a provider that gives us a pool id
and its two tokens has told us everything but `(fee, tickSpacing, hooks)`. Those come from the
families this chain has already shown us, ranked by frequency, and the first candidate whose hash
reproduces the id is the answer. A hit is proof; a miss is silence.

WHY. The log scan cannot reach the pools we need. CryoPalmar/R (4663, 2026-09-06 12:44): the real
R/USDG pool held $15,982 and had existed for twenty minutes, GeckoTerminal listed it, and the only
key we held was a 91.06%-fee trap with $1.23 of liquidity. On Base the gap is structural — the
pools we would trade are a median 308 hours old against a scan that reaches about 33.

Every test here is offline. Real objects, an in-memory database, no network.
"""
import sqlite3
import time

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0x9ccd256d748c3a676020f428b053f78edafe81b7"
# The family that would have saved the CryoPalmar entry: HOOKED, fee 0, tick spacing 200.
CRYO_FAMILY = (0, 200, "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
TRAP_FAMILY = (910610, 200, NATIVE)


def _key(c0, c1, family):
    lo, hi = (c0, c1) if int(c0, 16) < int(c1, 16) else (c1, c0)
    return {"currency0": lo, "currency1": hi,
            "fee": family[0], "tick_spacing": family[1], "hooks": family[2]}


def _db(rows):
    """An in-memory key table holding `rows` of (network_id, fee, tick_spacing, hooks, n)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_evm_v4_pool_keys ("
                 "network_id TEXT, pool_id TEXT, pool_manager TEXT, key_json TEXT, verified_at TEXT)")
    n_row = 0
    for net, fee, ts, hooks, n in rows:
        for _ in range(n):
            n_row += 1
            key = _key(NATIVE, "0x" + format(n_row, "040x"), (fee, ts, hooks))
            conn.execute("INSERT INTO memecoin_evm_v4_pool_keys VALUES (?,?,?,?,?)",
                         (net, pool_id_for_key(key), "0xpm",
                          '{"currency0":"%s","currency1":"%s","fee":%d,"tick_spacing":%d,"hooks":"%s"}'
                          % (key["currency0"], key["currency1"], fee, ts, hooks), "2026-09-06T00:00:00"))
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _clean():
    V._family_cache.clear()
    V._pair_hint.clear()
    V._key_cache.clear()
    yield
    V._family_cache.clear()
    V._pair_hint.clear()
    V._key_cache.clear()


# --- the derivation itself -----------------------------------------------------------------------

def test_a_key_is_derived_from_its_pair_and_equals_the_real_key():
    families = (CRYO_FAMILY,)
    real = _key(USDG, COIN, CRYO_FAMILY)

    got = V.derive_pool_key(CHAIN, pool_id_for_key(real), COIN, USDG, families=families)

    assert got == real


def test_the_pair_order_does_not_matter():
    """V4 requires currency0 < currency1; the provider's pair arrives in either order."""
    families = (CRYO_FAMILY,)
    real = _key(USDG, COIN, CRYO_FAMILY)
    pid = pool_id_for_key(real)

    assert V.derive_pool_key(CHAIN, pid, COIN, USDG, families=families) == \
        V.derive_pool_key(CHAIN, pid, USDG, COIN, families=families) == real


def test_a_hooked_family_is_derivable_and_not_only_the_zero_hook():
    """The CryoPalmar family IS hooked. A candidate set narrowed to the zero hook would have
    missed the very case this exists for: among sane-fee pools only 37.6% have a zero hook,
    across 2,467 distinct hook addresses."""
    real = _key(USDG, COIN, CRYO_FAMILY)
    zero_only = ((CRYO_FAMILY[0], CRYO_FAMILY[1], NATIVE),)

    assert V.derive_pool_key(CHAIN, pool_id_for_key(real), COIN, USDG, families=zero_only) is None
    assert V.derive_pool_key(CHAIN, pool_id_for_key(real), COIN, USDG,
                             families=(CRYO_FAMILY,)) == real


def test_the_first_exact_hash_match_wins_and_near_misses_are_not_returned():
    """There is no ranking among hits and no partial match: a candidate either reproduces the id
    or it does not."""
    real = _key(USDG, COIN, CRYO_FAMILY)
    families = ((0, 199, CRYO_FAMILY[2]), (1, 200, CRYO_FAMILY[2]), CRYO_FAMILY)

    assert V.derive_pool_key(CHAIN, pool_id_for_key(real), COIN, USDG, families=families) == real


@pytest.mark.parametrize("bad", ["", None, "0xnothex", "0x" + "ab" * 20, "0x" + "ab" * 33])
def test_a_malformed_pool_id_is_refused(bad):
    assert V.derive_pool_key(CHAIN, bad, COIN, USDG, families=(CRYO_FAMILY,)) is None


@pytest.mark.parametrize("a,b", [(COIN, COIN), (COIN, "0xshort"), (None, USDG), (COIN, None)])
def test_a_malformed_pair_is_refused(a, b):
    real = _key(USDG, COIN, CRYO_FAMILY)
    assert V.derive_pool_key(CHAIN, pool_id_for_key(real), a, b, families=(CRYO_FAMILY,)) is None


def test_a_malformed_family_is_skipped_rather_than_fatal():
    real = _key(USDG, COIN, CRYO_FAMILY)
    families = (("not-a-fee", 200, CRYO_FAMILY[2]), (0, 200, "not-an-address"), CRYO_FAMILY)

    assert V.derive_pool_key(CHAIN, pool_id_for_key(real), COIN, USDG, families=families) == real


# --- Alpha review condition 1: the equality IS the store gate -------------------------------------

def test_a_wrong_pool_id_returns_none_and_stores_nothing(monkeypatch):
    """Nothing may be cached or persisted that does not hash to the provider's own pool id."""
    stored = []
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: stored.append(a))
    monkeypatch.setattr(V, "pool_key_families", lambda *a, **k: (CRYO_FAMILY,))
    wrong = "0x" + "ab" * 32
    V._note_pair(CHAIN, wrong, USDG)

    assert V._derive_and_cache_pool_key(CHAIN, wrong, COIN) is False
    assert stored == []
    assert V._key_cache == {}


def test_a_verified_key_is_cached_and_persisted_once(monkeypatch):
    stored = []
    monkeypatch.setattr(V, "_store_pool_key", lambda c, p, k: stored.append((c, p, k)))
    monkeypatch.setattr(V, "pool_key_families", lambda *a, **k: (CRYO_FAMILY,))
    real = _key(USDG, COIN, CRYO_FAMILY)
    pid = pool_id_for_key(real)
    V._note_pair(CHAIN, pid, USDG)

    assert V._derive_and_cache_pool_key(CHAIN, pid, COIN) is True
    assert V._key_cache[(CHAIN, pid.lower())] == real
    assert stored == [(CHAIN, pid.lower(), real)]


def test_no_pair_hint_means_no_derivation(monkeypatch):
    monkeypatch.setattr(V, "pool_key_families", lambda *a, **k: (CRYO_FAMILY,))
    real = _key(USDG, COIN, CRYO_FAMILY)

    assert V._derive_and_cache_pool_key(CHAIN, pool_id_for_key(real), COIN) is False


# --- the safety property Alpha asked to be written down -------------------------------------------

def test_a_trap_fee_never_enters_the_candidate_set_so_a_trap_is_underivable():
    """Only `_fee_is_sane` families are ranked, so derivation CANNOT produce a trap pool. The
    91.06%-fee R pool that cost us the CryoPalmar entry is structurally out of reach; a provider
    handing us its id yields no match, derivation returns None, and the caller falls back to the
    scan exactly as before."""
    conn = _db([(CHAIN, TRAP_FAMILY[0], TRAP_FAMILY[1], TRAP_FAMILY[2], 500),
                (CHAIN, CRYO_FAMILY[0], CRYO_FAMILY[1], CRYO_FAMILY[2], 5)])
    families = V.warm_pool_key_families(CHAIN, conn_fn=lambda: conn)

    assert TRAP_FAMILY not in families, "a trap fee must never become a candidate"
    assert CRYO_FAMILY in families
    trap_pool = _key(NATIVE, COIN, TRAP_FAMILY)
    assert V.derive_pool_key(CHAIN, pool_id_for_key(trap_pool), COIN, NATIVE,
                             families=families) is None


# --- Alpha review condition 2: the family cache ---------------------------------------------------

def test_the_ranking_costs_one_group_by_not_one_per_call():
    conn = _db([(CHAIN, 0, 200, CRYO_FAMILY[2], 3)])
    calls = []

    def conn_fn():
        calls.append(1)
        return conn

    first = V.warm_pool_key_families(CHAIN, conn_fn=conn_fn)
    for _ in range(20):
        assert V.warm_pool_key_families(CHAIN, conn_fn=conn_fn) is first
    assert calls == [1], "the ranking is a full table GROUP BY; it is paid once per TTL"


def test_the_cache_is_per_chain():
    conn = _db([(CHAIN, 0, 200, CRYO_FAMILY[2], 3), ("8453", 3000, 60, NATIVE, 2)])

    assert V.warm_pool_key_families(CHAIN, conn_fn=lambda: conn) == (CRYO_FAMILY,)
    assert V.warm_pool_key_families("8453", conn_fn=lambda: conn) == ((3000, 60, NATIVE),)


def test_the_cache_expires_and_re_ranks():
    conn = _db([(CHAIN, 0, 200, CRYO_FAMILY[2], 3)])
    calls = []

    def conn_fn():
        calls.append(1)
        return conn

    clock = [1000.0]
    V.warm_pool_key_families(CHAIN, now_fn=lambda: clock[0], conn_fn=conn_fn)
    clock[0] += V._FAMILY_TTL_S - 1
    V.warm_pool_key_families(CHAIN, now_fn=lambda: clock[0], conn_fn=conn_fn)
    assert calls == [1]
    clock[0] += 2
    V.warm_pool_key_families(CHAIN, now_fn=lambda: clock[0], conn_fn=conn_fn)
    assert calls == [1, 1]


def test_an_empty_key_table_degrades_to_the_scan_rather_than_raising():
    """Base today, and every new chain. No families means no derivation and no exception."""
    conn = _db([])

    assert V.warm_pool_key_families("8453", conn_fn=lambda: conn) == ()
    real = _key(USDG, COIN, CRYO_FAMILY)
    assert V.derive_pool_key("8453", pool_id_for_key(real), COIN, USDG, families=()) is None


def test_a_database_failure_is_silent_and_empty_not_fatal():
    def boom():
        raise sqlite3.OperationalError("unable to open database file")

    assert V.warm_pool_key_families(CHAIN, conn_fn=boom) == ()


def test_the_candidate_count_is_bounded(monkeypatch):
    monkeypatch.setattr(V, "_FAMILY_CANDIDATES", 2)
    conn = _db([(CHAIN, 100, 1, NATIVE, 9), (CHAIN, 500, 10, NATIVE, 7),
                (CHAIN, 3000, 60, NATIVE, 5), (CHAIN, 10000, 200, NATIVE, 3)])

    families = V.warm_pool_key_families(CHAIN, conn_fn=lambda: conn)

    assert families == ((100, 1, NATIVE), (500, 10, NATIVE)), "ranked by frequency, then cut"


# --- the pair hint ---------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", None, "0xshort", "not-an-address", 42])
def test_a_malformed_counter_is_never_recorded(bad):
    V._note_pair(CHAIN, "0x" + "11" * 32, bad)
    assert V._pair_hint == {}


def test_the_pair_hint_is_bounded(monkeypatch):
    monkeypatch.setattr(V, "_PAIR_HINT_MAX", 4)
    for i in range(9):
        V._note_pair(CHAIN, "0x" + format(i, "064x"), USDG)
    assert len(V._pair_hint) <= 4


# --- discovery primes the key cache ----------------------------------------------------------------

def test_discovery_derives_every_unknown_key_before_it_ranks_them(monkeypatch):
    """The derived key lets `_fee_rank` tell an honest pool from a trap instead of ranking it
    unknown, and `_pool_key` then finds it without a log read."""
    real = _key(USDG, COIN, CRYO_FAMILY)
    pid = pool_id_for_key(real)
    monkeypatch.setattr(V, "pool_key_families", lambda *a, **k: (CRYO_FAMILY,))
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_gt_pools_ranked",
                        lambda c, m, **k: (V._note_pair(c, pid, USDG) or [(9000.0, pid)], False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: [])
    V._pool_cache.clear()

    pools = V._pools_for_mint(CHAIN, COIN)

    assert pid in pools
    assert V._key_cache[(CHAIN, pid.lower())] == real, \
        "discovery must derive the key while it still holds the provider's pair"


# --- the build never runs on the entry path -------------------------------------------------------

def test_the_entry_path_reads_the_ranking_and_never_builds_it(monkeypatch):
    """MEASURED REASON: building the ranking is a full GROUP BY over the key table, 340 ms at 165k
    rows, and the funded entry quote runs at a p50 of 545 ms on 4663. A cold build on that path
    would be a 62% add on exactly the entries that race other buyers and are judged by a chase cap.
    So the entry path READS a cache that the tick stream fills."""
    opened = []
    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection",
                        lambda *a, **k: opened.append(1) or (_ for _ in ()).throw(AssertionError(
                            "the entry path must never build the family ranking")))

    assert V.pool_key_families(CHAIN) == ()          # cold read: empty, no database touched
    assert V.derive_pool_key(CHAIN, "0x" + "11" * 32, COIN, USDG) is None
    assert V._derive_and_cache_pool_key(CHAIN, "0x" + "11" * 32, COIN) is False
    assert opened == []


def test_a_cold_ranking_degrades_to_no_derivation_not_to_a_synchronous_build():
    """The fallback is the log scan, exactly as before this existed."""
    real = _key(USDG, COIN, CRYO_FAMILY)
    pid = pool_id_for_key(real)
    V._note_pair(CHAIN, pid, USDG)

    assert V._derive_and_cache_pool_key(CHAIN, pid, COIN) is False   # cold: nothing derived
    conn = _db([(CHAIN, CRYO_FAMILY[0], CRYO_FAMILY[1], CRYO_FAMILY[2], 3)])
    V.warm_pool_key_families(CHAIN, conn_fn=lambda: conn)            # the tick stream's job
    assert V.derive_pool_key(CHAIN, pid, COIN, USDG) == real         # now the entry path can read


def test_the_tick_stream_warms_the_ranking_on_every_pass(monkeypatch):
    """`fill_v4_pool_key_cache` is the tick stream's existing per-chain pass and is off the money path."""
    from sniper.services import memecoin_tick_stream as TS

    warmed = []
    monkeypatch.setattr(TS.V4Q, "store_pool_keys_from_logs", lambda c, l: 0)
    monkeypatch.setattr(TS.V4Q, "warm_pool_key_families", lambda c: warmed.append(c))
    TS.fill_v4_pool_key_cache(CHAIN, [{"topics": []}])

    assert warmed == [CHAIN]


def test_a_warm_up_failure_never_breaks_the_tape(monkeypatch):
    from sniper.services import memecoin_tick_stream as TS

    monkeypatch.setattr(TS.V4Q, "store_pool_keys_from_logs", lambda c, l: 0)
    monkeypatch.setattr(TS.V4Q, "warm_pool_key_families",
                        lambda c: (_ for _ in ()).throw(RuntimeError("db locked")))
    TS.fill_v4_pool_key_cache(CHAIN, [{"topics": []}])             # must not raise
