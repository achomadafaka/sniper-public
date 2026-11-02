"""A launchpad names its coin's pool before any log index can (2026-09-05).

Spec: docs/superpowers/specs/2026-09-05-memecoin-v4-launchpad-derived-discovery-design.md.
At the first feed signal the pool is 0-30 s old and this chain's log index lags 38-61 s, so the
launchpad contract is the only source that can answer in time.

Every test here is offline. Nothing in this file makes a network call.
"""
import pytest

from sniper.execution import evm_v4_launchpad as LP
from sniper.execution import evm_v4_quote as V


@pytest.fixture(autouse=True)
def _discovery_context_is_per_test():
    """Two tests here set `_blind.discovery_rpc_url` to drive the derived path and did not restore
    it. The value leaked into `tests/test_memecoin_v4_pool_discovery.py`, where a total provider
    blackout then ATTEMPTED the chain scan and read `discovery_all_blind` instead of `rate_limit`
    (deploy gate refusal, 2026-09-05 ba2ead0e). The module restores this at runtime itself; tests
    must too."""
    yield
    V._blind.discovery_rpc_url = None
    V._blind.discovery_http = None

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
LAUNCHPAD = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
# Golden value: a real stored pool of this hook family, read from the funded ledger 2026-09-05.
GOLD_COIN = "0x0057d5824de503e1f1bc55748edf2a77a9522dd7"
GOLD_POOL = "0xddbdaafabec9d4b0b00ea3e6839727e84f433be383e7f743935e0104683e476f"
GOLD_KEY = {"currency0": NATIVE, "currency1": GOLD_COIN,
            "fee": 0, "tick_spacing": 200, "hooks": HOOK}
ENTRY = {"address": LAUNCHPAD, "info_selector": "3cf28b5a", "hook_selector": "6651812c",
         "coin_word": 0, "counter_word": 4, "fee_word": 6, "tick_spacing_word": 7}


def _w(value):
    if isinstance(value, str):
        return value.lower().removeprefix("0x").rjust(64, "0")
    return format(int(value), "064x")


def _struct(coin, counter=NATIVE, fee=0, ts=200, words=15):
    """The 15-word coin-info struct the launchpad returns, by configured word index."""
    out = ["0" * 64] * words
    out[ENTRY["coin_word"]] = _w(coin)
    out[ENTRY["counter_word"]] = _w(counter)
    out[ENTRY["fee_word"]] = _w(fee)
    out[ENTRY["tick_spacing_word"]] = _w(ts)
    return "0x" + "".join(out)


# 1. the decoder reads each field by its configured word index
def test_the_struct_decoder_reads_each_field_by_its_configured_word():
    key = LP.key_from_struct(LP._words(_struct(GOLD_COIN, fee=0, ts=200)), ENTRY, GOLD_COIN)

    assert key["currency0"] == NATIVE       # V4 PoolKeys are address-sorted; native sorts first
    assert key["currency1"] == GOLD_COIN
    assert key["fee"] == 0
    assert key["tick_spacing"] == 200


def test_a_foreign_counter_currency_is_ordered_by_address_not_by_side():
    stock = "0x" + "ff" * 20                # sorts AFTER the coin, so the coin becomes currency0
    key = LP.key_from_struct(LP._words(_struct(GOLD_COIN, counter=stock)), ENTRY, GOLD_COIN)

    assert (key["currency0"], key["currency1"]) == (GOLD_COIN, stock)


# 2. a struct whose coin word is not the mint belongs to another launchpad
def test_a_struct_that_does_not_name_the_mint_is_skipped():
    other = "0x" + "ab" * 20

    assert LP.key_from_struct(LP._words(_struct(other)), ENTRY, GOLD_COIN) is None


def test_a_truncated_struct_is_skipped_rather_than_read_short():
    short = "0x" + "0" * 64 * 3             # shorter than the tick-spacing word index

    assert LP.key_from_struct(LP._words(short), ENTRY, GOLD_COIN) is None


# 3. the pool id is keccak(abi.encode(PoolKey)) -- golden value from the ledger
def test_the_pool_id_is_the_keccak_of_the_pool_key():
    assert LP.pool_id_for_key(GOLD_KEY) == GOLD_POOL


def test_the_derived_key_reproduces_the_stored_pool_end_to_end():
    """Struct in, stored pool id out: the whole derivation against a real recorded pool."""
    key = LP.key_from_struct(LP._words(_struct(GOLD_COIN)), ENTRY, GOLD_COIN)
    key["hooks"] = HOOK

    assert key == GOLD_KEY
    assert LP.pool_id_for_key(key) == GOLD_POOL


def _wire(monkeypatch, *, struct=None, hook=HOOK, quote=1234, sell_quote="same",
          entries=(ENTRY,)):
    """Stub every network edge.

    `quote` None means the BUY refused (pool not initialised). `sell_quote` None means the buy
    priced but the SELL reverted -- the trap this probe exists to catch.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {CHAIN: entries}, raising=False)
    LP._hook_cache.clear()
    calls = []

    def _call(_chain, to, data, _rpc, http=None):
        calls.append(data[:10])
        if data.startswith("0x" + ENTRY["hook_selector"]):
            return None if hook is None else "0x" + _w(hook)
        return struct
    monkeypatch.setattr(LP, "_call", _call)
    seen_dirs = []

    def _quote_single(_chain, key, zero_for_one, _amount, _rpc, http=None):
        seen_dirs.append(zero_for_one)
        coin_is_c1 = str(key["currency1"]).lower() == GOLD_COIN
        is_buy = (zero_for_one == coin_is_c1)
        if is_buy:
            return quote
        return quote if sell_quote == "same" else sell_quote
    monkeypatch.setattr("sniper.execution.evm_v4_quote._quote_single", _quote_single)
    monkeypatch.setattr(LP, "_probe_directions_for_test", seen_dirs, raising=False)
    stored = []
    monkeypatch.setattr("sniper.execution.evm_v4_quote._store_pool_key",
                        lambda chain, pid, key: stored.append((pid, key)))
    return stored, calls


# 4. a confirmed key is stored and primes the cache; discovery lists it
def test_a_confirmed_key_is_stored_and_returned(monkeypatch):
    stored, _calls = _wire(monkeypatch, struct=_struct(GOLD_COIN))

    out = LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")

    assert out == [(GOLD_POOL, GOLD_KEY, True)]
    assert stored == [(GOLD_POOL, GOLD_KEY)]


def test_discovery_lists_a_derived_pool_and_primes_the_key_cache(monkeypatch):
    monkeypatch.setattr(V, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: [])
    monkeypatch.setattr(V, "_onchain_pools_for_mint", lambda *a, **k: ([], False))
    monkeypatch.setattr("sniper.execution.evm_v4_launchpad.derived_pool_keys",
                        lambda *a, **k: [(GOLD_POOL, GOLD_KEY, True)])
    V._pool_cache.clear()
    V._key_cache.clear()
    V._blind.discovery_rpc_url = "http://rpc"

    pools = V._pools_for_mint(CHAIN, GOLD_COIN)

    assert GOLD_POOL in pools
    assert V._key_cache[(CHAIN, GOLD_POOL)] == GOLD_KEY   # primed, so the fee ranks as known-sane


# 5. a pool that is not initialised yet is pending, not stored
def test_a_pool_that_does_not_quote_yet_is_pending_and_not_stored(monkeypatch):
    stored, _calls = _wire(monkeypatch, struct=_struct(GOLD_COIN), quote=None)

    out = LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")

    assert out == [(GOLD_POOL, GOLD_KEY, False)]
    assert stored == []                     # a stored key must mean a pool we have quoted


def test_a_pending_pool_is_not_offered_to_discovery(monkeypatch):
    """The caller's transient requote covers the seconds until the pool opens."""
    monkeypatch.setattr(V, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: [])
    monkeypatch.setattr(V, "_onchain_pools_for_mint", lambda *a, **k: ([], False))
    monkeypatch.setattr("sniper.execution.evm_v4_launchpad.derived_pool_keys",
                        lambda *a, **k: [(GOLD_POOL, GOLD_KEY, False)])
    V._pool_cache.clear()
    V._key_cache.clear()
    V._blind.discovery_rpc_url = "http://rpc"

    assert V._pools_for_mint(CHAIN, GOLD_COIN) == []


def test_a_read_failure_yields_nothing_and_names_itself(monkeypatch):
    _wire(monkeypatch, struct=None)
    seen = []

    assert LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc", note_fn=seen.append) == []
    assert "launchpad_read_failed" in seen


def test_a_coin_from_another_launchpad_names_itself(monkeypatch):
    _wire(monkeypatch, struct=_struct("0x" + "ab" * 20))
    seen = []

    assert LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc", note_fn=seen.append) == []
    assert "launchpad_not_ours" in seen


def test_a_pending_pool_names_itself(monkeypatch):
    _wire(monkeypatch, struct=_struct(GOLD_COIN), quote=None)
    seen = []

    LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc", note_fn=seen.append)
    assert "launchpad_pool_pending" in seen


def test_the_hook_is_read_once_per_launchpad(monkeypatch):
    _stored, calls = _wire(monkeypatch, struct=_struct(GOLD_COIN))

    LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")
    LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")

    assert calls.count("0x" + ENTRY["hook_selector"]) == 1   # immutable, so cached for the process


# 6. registry hygiene
@pytest.mark.parametrize("bad", [
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:3cf28b5a:0:4:6",        # too few fields
    "notanaddress:3cf28b5a:0:4:6:7:6651812c",                            # bad address
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:3cf28b5a:0:4:6:x:6651812c",  # bad word index
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:zz:0:4:6:7:6651812c",    # bad selector length
])
def test_a_malformed_registry_entry_is_skipped(monkeypatch, bad):
    monkeypatch.setenv("MEMECOIN_EVM_V4_LAUNCHPADS_9999", bad)
    import importlib
    from sniper.settings import memecoin as cfg
    reloaded = importlib.reload(cfg)
    try:
        assert "9999" not in reloaded.MEMECOIN_EVM_V4_LAUNCHPADS
    finally:
        monkeypatch.delenv("MEMECOIN_EVM_V4_LAUNCHPADS_9999", raising=False)
        importlib.reload(cfg)


def test_an_empty_registry_is_inert(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {}, raising=False)

    assert LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc") == []


def test_no_rpc_url_is_inert(monkeypatch):
    _wire(monkeypatch, struct=_struct(GOLD_COIN))

    assert LP.derived_pool_keys(CHAIN, GOLD_COIN, None) == []


# --- the sell probe (2026-09-05) ---------------------------------------------------------------
# Measured that day, 12 native-paired pools per hook family: every reverting pool priced its BUY
# perfectly, so a one-direction probe would have stored a coin we cannot sell.

def test_a_pool_that_buys_and_sells_is_stored_and_confirmed(monkeypatch):
    stored, _calls = _wire(monkeypatch, struct=_struct(GOLD_COIN))

    out = LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")

    assert out == [(GOLD_POOL, GOLD_KEY, True)]
    assert stored == [(GOLD_POOL, GOLD_KEY)]


def test_a_pool_that_buys_but_does_not_sell_is_never_stored(monkeypatch):
    """Buyable-and-not-sellable must not enter the candidate list through discovery."""
    stored, _calls = _wire(monkeypatch, struct=_struct(GOLD_COIN), sell_quote=None)
    seen = []

    out = LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc", note_fn=seen.append)

    assert out == [(GOLD_POOL, GOLD_KEY, False)]     # returned, but never confirmed
    assert stored == []
    assert "launchpad_pool_unsellable" in seen


def test_a_pool_that_does_not_buy_is_still_pending_not_unsellable(monkeypatch):
    """An uninitialised pool is a timing state, not a trap: the reason must stay distinguishable."""
    stored, _calls = _wire(monkeypatch, struct=_struct(GOLD_COIN), quote=None)
    seen = []

    out = LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc", note_fn=seen.append)

    assert out == [(GOLD_POOL, GOLD_KEY, False)]
    assert stored == []
    assert "launchpad_pool_pending" in seen
    assert "launchpad_pool_unsellable" not in seen


def test_the_probe_asks_both_directions_and_the_sell_uses_the_buys_output(monkeypatch):
    amounts = []

    def _quote_single(_chain, key, zero_for_one, amount, _rpc, http=None):
        amounts.append((zero_for_one, amount))
        return 7777 if zero_for_one else 4242
    _wire(monkeypatch, struct=_struct(GOLD_COIN))
    monkeypatch.setattr("sniper.execution.evm_v4_quote._quote_single", _quote_single)

    LP.derived_pool_keys(CHAIN, GOLD_COIN, "http://rpc")

    assert len(amounts) == 2
    assert amounts[0][0] is not amounts[1][0]        # opposite directions
    assert amounts[1][1] == 7777                     # the sell prices what the buy would have given


def test_an_unsellable_pool_is_not_offered_to_discovery(monkeypatch):
    """End to end: the trap never reaches `_pools_for_mint`'s candidate list."""
    monkeypatch.setattr(V, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: [])
    monkeypatch.setattr(V, "_onchain_pools_for_mint", lambda *a, **k: ([], False))
    monkeypatch.setattr("sniper.execution.evm_v4_launchpad.derived_pool_keys",
                        lambda *a, **k: [(GOLD_POOL, GOLD_KEY, False)])
    V._pool_cache.clear()
    V._key_cache.clear()
    V._blind.discovery_rpc_url = "http://rpc"

    assert V._pools_for_mint(CHAIN, GOLD_COIN) == []
    assert (CHAIN, GOLD_POOL) not in V._key_cache    # and its key never primes the cache
