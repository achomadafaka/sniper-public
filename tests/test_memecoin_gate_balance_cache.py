"""A transient balance-read failure must not refuse a funded entry.

The pre-gate read used to refuse outright ("quote balance unavailable"). A failed read is an
instrument failure, not a balance change: the balance does not move because we could not see it.
These tests pin the safety boundary of the fallback — it must never serve a value that predates
our own spend, and it must expire."""
import sniper.services.memecoin_real_runner as R


def _reset():
    with R._gate_balance_lock:
        R._gate_balance_cache.clear()


def test_a_good_read_is_served_live_and_cached():
    _reset()
    quote, native, cached = R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    assert (quote, native) == (500.0, 0.05)
    assert cached is False


def test_a_failed_read_inside_the_ttl_serves_the_cached_balance():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    quote, native, cached = R._gate_balances(
        "4663", lambda: None, lambda: None, now_ts=100.0 + R._GATE_BALANCE_TTL_S - 1)
    assert (quote, native) == (500.0, 0.05)
    assert cached is True


def test_a_raising_read_is_served_from_cache_not_propagated():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)

    def _boom():
        raise RuntimeError("rpc transport lost")

    quote, _native, cached = R._gate_balances("4663", _boom, _boom, now_ts=101.0)
    assert quote == 500.0
    assert cached is True


def test_a_failed_read_past_the_ttl_still_refuses():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    quote, _native, cached = R._gate_balances(
        "4663", lambda: None, lambda: None, now_ts=100.0 + R._GATE_BALANCE_TTL_S + 1)
    assert quote is None
    assert cached is False


def test_a_spend_invalidates_the_cache_so_a_stale_balance_is_never_served():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    R._gate_balance_invalidate("4663")
    quote, _native, cached = R._gate_balances("4663", lambda: None, lambda: None, now_ts=101.0)
    assert quote is None, "a pre-spend balance must never be served after we spent"
    assert cached is False


def test_one_chain_cache_never_answers_for_another_chain():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    quote, _native, _cached = R._gate_balances(
        "solana", lambda: None, lambda: None, now_ts=101.0)
    assert quote is None


def test_a_half_failure_keeps_the_live_half_live():
    _reset()
    R._gate_balances("4663", lambda: 500.0, lambda: 0.05, now_ts=100.0)
    quote, native, cached = R._gate_balances("4663", lambda: 400.0, lambda: None, now_ts=101.0)
    assert quote == 400.0, "a good quote read must win over its own cached value"
    assert native == 0.05
    assert cached is True


def test_the_solana_lane_caches_its_native_balance_shape_unchanged():
    """The Solana native read returns a structure, not a float. The cache must hand back
    exactly what the reader returned so native_sol_balance_value still parses it."""
    _reset()
    native_payload = {"value": 123456789}
    R._gate_balances("solana", lambda: 42.0, lambda: native_payload, now_ts=100.0)
    quote, native, cached = R._gate_balances(
        "solana", lambda: None, lambda: None, now_ts=101.0)
    assert quote == 42.0
    assert native is native_payload
    assert cached is True


def test_read_metadata_preserves_original_cached_read_start_and_tuple_contract():
    _reset()
    initial = {}
    assert R._gate_balances("solana", lambda: 200, lambda: 1, now_ts=100,
                            read_meta=initial) == (200, 1, False)
    assert initial == {"quote": 100, "native": 100, "started": 100}
    cached = {}
    assert R._gate_balances("solana", lambda: 150, lambda: None, now_ts=101,
                            read_meta=cached) == (150, 1, True)
    assert cached == {"quote": 101, "native": 100, "started": 100}
    assert R._gate_balance_cache["solana"]["native"] == (1, 100)


def test_preread_keeps_cached_start_before_its_own_launch():
    import time
    _reset()
    earlier = time.monotonic() - 1
    R._gate_balances("solana", lambda: 200, lambda: 1, now_ts=earlier)
    pre = R._EntryPreReads({"usdc_fn": lambda: None, "sol_fn": lambda: 1}, "mint",
                           object(), read_mint=False)
    assert pre.balances()[:3] == (200, 1, True)
    assert pre.balance_read_started == earlier < pre.started
