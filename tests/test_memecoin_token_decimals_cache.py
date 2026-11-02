"""SPL decimals are read once per mint, and a failed read is never remembered.

Every paper Solana close made an uncached getTokenSupply on the funded custody URL from the main
loop (16.8/h over 7 days); one of those reads held the loop into the 2026-09-22 10:31Z sweep_stale
kill. Decimals cannot change, so a success is cached; a failure must stay unknown and be re-read.
"""
import pytest

from sniper.services import memecoin_quotes as q

MINT = "So1anaMintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
URL = "https://rpc.example/solana"


@pytest.fixture(autouse=True)
def _fresh_cache():
    q._token_decimals_cached.cache_clear()
    yield
    q._token_decimals_cached.cache_clear()


def _transport(monkeypatch, answers):
    """Default transport stub: pops one answer per call, raising when the answer is an exception."""
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(q, "_solana_http", http)
    return calls


def _ok(decimals):
    return 200, {"result": {"value": {"decimals": decimals}}}, None


# 0 is the value that makes a truthiness bug reachable: `if cached:` would re-read a 0-decimal mint.
@pytest.mark.parametrize("decimals", [0, 9])
def test_second_read_of_a_mint_makes_no_rpc(monkeypatch, decimals):
    calls = _transport(monkeypatch, [_ok(decimals)])
    assert q.token_decimals(MINT, URL) == decimals
    assert q.token_decimals(MINT, URL) == decimals
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError("dns stall"), (500, {}, None),
                                     (200, {"error": {"code": -32602}}, None)])
def test_a_failed_read_is_retried_not_cached(monkeypatch, failure):
    calls = _transport(monkeypatch, [failure, _ok(6)])
    assert q.token_decimals(MINT, URL) is None
    assert q.token_decimals(MINT, URL) == 6
    assert len(calls) == 2


def test_an_injected_transport_is_never_cached(monkeypatch):
    calls = _transport(monkeypatch, [])
    seen = []

    def seam(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        return _ok(6)

    assert q.token_decimals(MINT, URL, http=seam) == 6
    assert q.token_decimals(MINT, URL, http=seam) == 6
    assert len(seen) == 2 and calls == []
