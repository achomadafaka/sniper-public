"""DexScreener circuit breaker.

2026-08-24: the provider stopped answering — TLS completed in 16ms, then silence for the whole
`_http` timeout. No status to key on, so every caller re-paid the timeout on every poll, and the
batch paid it PER 30-MINT CHUNK. `/api/memecoin/real/positions` measured 10.03s on one chunk while
every non-DexScreener endpoint served in under 10ms. `real_exit_sweep` — the funded catastrophe
stop and timecap — calls the batch with no `price_fn` from all three of its call sites.
"""
import pytest

from sniper.execution import solana_quote as Q


@pytest.fixture(autouse=True)
def _reset_cells():
    Q._ds_cooldown_until[0] = 0.0
    Q._ds_funded_cooldown_until[0] = 0.0
    yield
    Q._ds_cooldown_until[0] = 0.0
    Q._ds_funded_cooldown_until[0] = 0.0


def _boom(*_a, **_k):
    raise TimeoutError("the read operation timed out")


def _counting_boom(calls):
    def http(*_a, **_k):
        calls.append(1)
        raise TimeoutError("the read operation timed out")
    return http


KEYS = [("solana", "M%d" % i) for i in range(90)]      # 3 chunks of 30


def test_a_timeout_trips_the_breaker_and_abandons_later_chunks():
    """The old code did `except: continue`, re-paying the timeout on every remaining chunk."""
    calls = []
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=_counting_boom(calls))
    assert out == {}
    assert len(calls) == 1, "must stop after the first failing chunk, not walk all three"
    assert Q.dexscreener_cooldown_active() is True


def test_while_open_the_breaker_makes_no_http_call_at_all():
    calls = []
    Q.dexscreener_stats_batch_keyed(KEYS, http=_counting_boom(calls))
    calls.clear()
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=_counting_boom(calls))
    assert out == {}
    assert calls == [], "second call must be served by the breaker without touching the network"


def test_the_value_is_unchanged_only_sooner():
    """Semantics guard: a dead provider ALREADY returned {}. That is what makes this safe on the
    funded exit path — no caller can see a value it could not have seen before."""
    assert Q.dexscreener_stats_batch_keyed(KEYS, http=_boom) == {}
    assert Q.dexscreener_stats_batch_keyed(KEYS, http=_boom) == {}


def test_a_research_failure_does_not_blind_the_funded_lane():
    """The 2026-08-14 GeckoTerminal bug: one shared cell let a discovery throttle refuse live
    entries while the funded lane had made no failing request."""
    Q.dexscreener_stats_batch_keyed(KEYS, http=_boom)            # research caller
    assert Q.dexscreener_cooldown_active() is True
    assert Q.dexscreener_cooldown_active(funded=True) is False
    calls = []
    Q.dexscreener_stats_batch_keyed(KEYS, http=_counting_boom(calls), funded=True)
    assert calls == [1], "the funded lane must still be allowed to try"


def test_a_funded_failure_trips_both_cells():
    """The provider is refusing the money path itself — research has no business retrying."""
    Q.dexscreener_stats_batch_keyed(KEYS, http=_boom, funded=True)
    assert Q.dexscreener_cooldown_active(funded=True) is True
    assert Q.dexscreener_cooldown_active() is True


def test_a_non_200_trips_it_too():
    """An edge block can answer with a status instead of hanging; both are the provider refusing."""
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=lambda *_a, **_k: (403, {}, None))
    assert out == {} and Q.dexscreener_cooldown_active() is True


def test_the_single_stat_path_shares_the_breaker():
    assert Q.dexscreener_stats("MINT", http=_boom) is None
    assert Q.dexscreener_cooldown_active() is True
    calls = []
    assert Q.dexscreener_stats("MINT", http=_counting_boom(calls)) is None
    assert calls == []


def test_a_200_never_trips_the_breaker():
    """Pair PARSING is not this module's job here — what matters is that a healthy response
    leaves both cells closed, so a coin with no pair is never mistaken for an outage."""
    calls = []
    def ok(*_a, **_k):
        calls.append(1)
        return (200, {"pairs": []}, None)
    Q.dexscreener_stats_batch_keyed(KEYS, http=ok)
    assert Q.dexscreener_cooldown_active() is False
    assert Q.dexscreener_cooldown_active(funded=True) is False
    assert len(calls) == 3, "all three chunks must still be walked when the provider is healthy"


def test_an_injected_seam_keeps_its_old_signature():
    """The short timeout binds to the DEFAULT seam only — an injected http that takes no
    `timeout` kwarg must still work."""
    def strict(method, url, headers=None, body=None):
        return (200, {"pairs": []}, None)
    assert Q.dexscreener_stats_batch_keyed([("solana", "M0")], http=strict) == {}
    assert Q.dexscreener_stats("M0", http=strict) is None
