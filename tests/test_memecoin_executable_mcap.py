"""Vendor-free market cap: on-chain supply x our own route price.

2026-08-24: DexScreener's data plane died for ~3 hours. Every funded fill in that window banded
`assumed_nano` with mcap NULL, because ASSUME_MICRO promotes an unresolvable mcap — so the $250k
ceiling did not apply to a single entry. The feed could not help either: all 12 blind fills came
in on `follow_activity`, and 0 of 12 had a global-feed row, because the wallets we follow buy
coins before they trend. This path is the only one that works on an arbitrary fresh mint.

Calibrated against DexScreener while both were alive (6 solana coins, $13k-$2.1M): ratio
0.909-1.045, median ~1.02, so the existing $250k threshold transfers.
"""
import pytest

from sniper.execution import solana_real_seams as S


@pytest.fixture(autouse=True)
def _clear_supply_cache():
    S._SUPPLY_CACHE.clear()
    yield
    S._SUPPLY_CACHE.clear()


def _supply_http(amount, decimals, status=200, calls=None):
    def http(method, url, headers=None, body=None):
        if calls is not None:
            calls.append(1)
        return (status, {"result": {"value": {"amount": str(amount), "decimals": decimals}}}, None)
    return http


def _quote(out_atomic):
    return lambda mint, usd: {"outAmount": str(out_atomic)}


def test_mcap_is_supply_times_our_route_price():
    """1,000,000 tokens; $30 buys 1,000 of them -> $0.03 each -> $30,000 cap."""
    mc = S.executable_mcap("M", "http://rpc", usd=30.0,
                           http=_supply_http(1_000_000 * 10**6, 6),
                           quote_fn=_quote(1_000 * 10**6))
    assert mc == pytest.approx(30_000.0)


def test_decimals_come_from_the_same_response_as_supply():
    """Reading decimals from a second source is how the token maths drifts from the supply it is
    divided into. 9-decimal mint, same economics, same answer."""
    mc = S.executable_mcap("M", "http://rpc", usd=30.0,
                           http=_supply_http(1_000_000 * 10**9, 9),
                           quote_fn=_quote(1_000 * 10**9))
    assert mc == pytest.approx(30_000.0)


def test_supply_is_cached_so_the_second_call_makes_no_rpc():
    """Supply is constant for a mint with revoked authority; re-reading it is pure latency on the
    entry path."""
    calls = []
    http = _supply_http(1_000_000 * 10**6, 6, calls=calls)
    for _ in range(3):
        S.executable_mcap("M", "http://rpc", usd=30.0, http=http, quote_fn=_quote(1_000 * 10**6))
    assert len(calls) == 1


def test_a_failed_supply_read_returns_none_not_a_number():
    """Fail-open contract: None leaves the caller's existing behaviour untouched. Inventing a cap
    here would silently admit or refuse on a fabricated number."""
    assert S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(0, 6, status=500),
                             quote_fn=_quote(1_000 * 10**6)) is None


def test_no_route_returns_none():
    assert S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(1_000_000 * 10**6, 6),
                             quote_fn=lambda m, u: None) is None


def test_a_zero_out_amount_returns_none():
    assert S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(1_000_000 * 10**6, 6),
                             quote_fn=_quote(0)) is None


def test_a_raising_quote_returns_none():
    def boom(mint, usd):
        raise TimeoutError("provider down")
    assert S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(1_000_000 * 10**6, 6),
                             quote_fn=boom) is None


def test_zero_supply_is_not_cached_and_yields_none():
    """A zero supply is a bad read, not a fact. Caching it would poison the mint forever."""
    assert S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(0, 6),
                             quote_fn=_quote(1_000 * 10**6)) is None
    assert "M" not in S._SUPPLY_CACHE


def test_a_thin_pool_reports_a_LOWER_cap_than_a_deep_one():
    """Our clip moves a thin pool, so we receive fewer tokens and price them higher... which is
    the direction that matters: the cap we compute is what our own size can actually transact
    against. The 0.909x outlier in the calibration sweep is this term."""
    deep = S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(1_000_000 * 10**6, 6),
                             quote_fn=_quote(1_000 * 10**6))
    S._SUPPLY_CACHE.clear()
    thin = S.executable_mcap("M", "http://rpc", usd=30.0,
                             http=_supply_http(1_000_000 * 10**6, 6),
                             quote_fn=_quote(900 * 10**6))      # 10% fewer tokens for the same $30
    assert thin > deep
