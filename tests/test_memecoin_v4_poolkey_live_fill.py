"""The live tape scan fills the V4 PoolKey cache from bytes it already receives.

`INGEST_TOPICS` carries V4 `Initialize`, so every new pool's fee, tickSpacing and hooks arrive on
the tick scan. `decode_v4_initialize` keeps only the two currencies, so those three words were
parsed straight past on every row. Filling the durable cache here costs no request and removes the
wide chain read from the entry path.
"""
import sqlite3

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.persistence import db_memecoin as m


CHAIN = "4663"
MANAGER = V.V4[CHAIN]["pool_manager"]


def _key(currency1="0x" + "11" * 20, fee=0, tick_spacing=200, hooks="0x" + "22" * 20):
    return {"currency0": "0x" + "00" * 20, "currency1": currency1,
            "fee": fee, "tick_spacing": tick_spacing, "hooks": hooks}


def _pool_id_of(key):
    """A V4 pool id IS the keccak of its encoded PoolKey."""
    return "0x" + V.keccak(bytes.fromhex(V._encode_pool_key(key))).hex()


def _init_log(key, *, pool_id=None, address=None, topic0=None):
    """One PoolManager Initialize log carrying `key`, shaped as the node returns it."""
    data = (V._w(key["fee"])
            + format(key["tick_spacing"] % (1 << 256), "064x")
            + V._wa(key["hooks"])
            + V._w(0)          # sqrtPriceX96
            + V._w(0))         # tick
    return {"address": MANAGER if address is None else address,
            "topics": [V._INIT_TOPIC if topic0 is None else topic0,
                       _pool_id_of(key) if pool_id is None else pool_id,
                       "0x" + V._wa(key["currency0"]),
                       "0x" + V._wa(key["currency1"])],
            "data": "0x" + data}


class _KeepOpen(sqlite3.Connection):
    """The writer closes its handle in `finally`; an in-memory DB dies with it."""

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    """Route the writer at one in-memory ledger and hand the test a reader for it."""
    conn = sqlite3.connect(":memory:", factory=_KeepOpen)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr("sniper.persistence.db_core.get_connection", lambda *a, **k: conn)
    return conn


def test_an_initialize_log_on_the_tape_fills_the_pool_key_cache(store):
    key = _key()

    assert V.store_pool_keys_from_logs(CHAIN, [_init_log(key)]) == 1

    assert m.get_evm_v4_pool_key(
        store, chain_id=CHAIN, pool_id=_pool_id_of(key), pool_manager=MANAGER) == key


def test_the_cached_key_is_the_one_the_quote_path_would_have_read(store):
    """Byte-identical, or a cache hit quotes a pool that hashes to a different id."""
    key = _key(fee=3000, tick_spacing=60)
    log = _init_log(key)

    V.store_pool_keys_from_logs(CHAIN, [log])

    assert V.decode_initialize_log(log) == key
    assert m.get_evm_v4_pool_key(
        store, chain_id=CHAIN, pool_id=_pool_id_of(key), pool_manager=MANAGER) == key


def test_a_key_that_does_not_hash_back_to_its_pool_id_is_refused(store):
    """The pool id is the only proof the decode and the log agree. A wrong key is worse than none."""
    key = _key()
    foreign = "0x" + "cd" * 32

    assert V.store_pool_keys_from_logs(CHAIN, [_init_log(key, pool_id=foreign)]) == 0

    assert m.get_evm_v4_pool_key(
        store, chain_id=CHAIN, pool_id=foreign, pool_manager=MANAGER) is None


def test_a_negative_tick_spacing_survives_the_round_trip(store):
    """tickSpacing is an int24 in a 256-bit word; read unsigned it becomes a huge positive."""
    key = _key(tick_spacing=-200)

    assert V.store_pool_keys_from_logs(CHAIN, [_init_log(key)]) == 1

    stored = m.get_evm_v4_pool_key(
        store, chain_id=CHAIN, pool_id=_pool_id_of(key), pool_manager=MANAGER)
    assert stored["tick_spacing"] == -200


def test_a_foreign_pool_manager_is_never_cached(store):
    """A key from another PoolManager maps a pool we cannot trade."""
    key = _key()

    assert V.store_pool_keys_from_logs(
        CHAIN, [_init_log(key, address="0x" + "ee" * 20)]) == 0


def test_a_non_initialize_topic_is_ignored(store):
    """The scan carries swaps too; only Initialize describes a PoolKey."""
    key = _key()

    assert V.store_pool_keys_from_logs(
        CHAIN, [_init_log(key, topic0="0x" + "99" * 32)]) == 0


def test_an_unconfigured_chain_stores_nothing(store):
    assert V.store_pool_keys_from_logs("8453", [_init_log(_key())]) == 0


def test_every_valid_log_in_one_batch_is_stored(store):
    keys = [_key(currency1="0x" + f"{n:02x}" * 20) for n in range(1, 5)]

    assert V.store_pool_keys_from_logs(CHAIN, [_init_log(k) for k in keys]) == 4

    for k in keys:
        assert m.get_evm_v4_pool_key(
            store, chain_id=CHAIN, pool_id=_pool_id_of(k), pool_manager=MANAGER) == k


def test_one_malformed_log_does_not_cost_the_rest_of_the_batch(store):
    good, bad = _key(), _key(currency1="0x" + "33" * 20)
    broken = _init_log(bad)
    broken["data"] = "0x"          # truncated word -> decode returns None

    assert V.store_pool_keys_from_logs(CHAIN, [broken, _init_log(good)]) == 1

    assert m.get_evm_v4_pool_key(
        store, chain_id=CHAIN, pool_id=_pool_id_of(good), pool_manager=MANAGER) == good


def test_a_cache_write_failure_never_breaks_tick_ingest(monkeypatch):
    """The caller is the tape scan. A cache write must never cost a tick."""
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("sniper.persistence.db_core.get_connection", boom)

    assert V.store_pool_keys_from_logs(CHAIN, [_init_log(_key())]) == 0


def test_the_tape_scan_calls_the_cache_fill_with_the_logs_it_already_has(monkeypatch):
    """Wiring: the fill must ride the existing scan, not a second request."""
    from sniper.services import memecoin_tick_stream as T
    seen = {}
    monkeypatch.setattr(T.V4Q, "store_pool_keys_from_logs",
                        lambda chain, logs: seen.update(chain=chain, n=len(logs)) or 0)
    logs = [_init_log(_key())]

    T.fill_v4_pool_key_cache(CHAIN, logs)

    assert seen == {"chain": CHAIN, "n": 1}


def test_a_coin_seen_on_the_tape_becomes_discoverable_by_mint(monkeypatch):
    """The point of the whole change: entry asks by MINT, and must not need a chain read.

    `_stored_pools_for_mint` is the consumer. Filling the cache from the tape has to make a
    first-touch coin resolvable through it, or the write bought nothing.
    """
    conn = sqlite3.connect(":memory:", factory=_KeepOpen)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr("sniper.persistence.db_core.get_connection", lambda *a, **k: conn)
    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection", lambda *a, **k: conn)
    mint = "0x" + "7f" * 20
    key = _key(currency1=mint)

    assert V._stored_pools_for_mint(CHAIN, mint) == []

    V.store_pool_keys_from_logs(CHAIN, [_init_log(key)])

    assert V._stored_pools_for_mint(CHAIN, mint) == [_pool_id_of(key)]


def test_a_pass_with_no_initialize_never_opens_the_funded_ledger(monkeypatch):
    """Tick ingest runs continuously and its logs are overwhelmingly swaps. Opening a WRITE
    connection to the funded ledger on every pass to store nothing is churn the money path must
    not pay for."""
    opened = []

    def _boom(*_a, **_k):
        opened.append(1)
        raise AssertionError("the funded ledger must not be opened with nothing to store")

    monkeypatch.setattr("sniper.persistence.db_core.get_connection", _boom)
    swaps = [{"topics": ["0x" + "11" * 32, "0x" + "22" * 32], "address": "0xpool", "data": "0x"}]
    assert V.store_pool_keys_from_logs(4663, swaps) == 0
    assert V.store_pool_keys_from_logs(4663, []) == 0
    assert opened == []
