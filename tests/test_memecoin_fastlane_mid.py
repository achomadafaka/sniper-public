"""Batched mid lane: one call prices every open funded mint, at a 1s-safe cost (2026-07-27).

Measured open-position mark gaps were 14.6s median / 79s max while the recording floor was 5s, so
the freshness was real and being lost. >=10% moves landed entirely between samples on 1.4% of
intervals over 6h (max 77%). The exact per-position preview cannot run at 1s (2 round trips x 7
positions = 14/s); one batched DexScreener call can, and the rails read `mid` anyway.
"""
import pytest

from sniper.services import memecoin_fastlane as fl
from sniper.services.memecoin_solana_amounts import WSOL
from sniper.services.memecoin_solana_swap_marks import PoolBinding, SolanaSwapMarkCache


def _pos(pid, mint, net="solana"):
    return {"id": pid, "mint": mint, "network_id": net}


def _live_cache(now_ts=100.0):
    cache = SolanaSwapMarkCache()
    cache.record(
        {
            "pool": "PoolA",
            "kind": "sol_swap",
            "block_no": 8,
            "amount0": "-2000000",
            "amount1": "300000000",
            "decimals0": 6,
            "decimals1": 9,
        },
        PoolBinding("M1", "PoolA", "CoinV", "QuoteV", WSOL),
        now_ts=now_ts,
        observed_at="2026-08-12T12:00:00+00:00",
    )
    return cache


def test_scopes_are_disjoint_exact_is_evm_mid_is_solana():
    assert fl.mid_scope("solana") is True
    assert fl.mid_scope(None) is True                 # legacy rows default to solana
    assert fl.mid_scope("4663") is False
    assert fl.in_scope("solana") is False             # exact lane must never take solana at 1s


def test_one_batched_call_regardless_of_position_count():
    calls = []

    def batch(keys):
        calls.append(list(keys))
        return {k: {"price": 1.5, "liquidity": 900.0, "market_cap": 500_000.0} for k in keys}

    out = fl._batched_mid_marks([_pos(i, f"M{i}") for i in range(7)], batch_fn=batch)
    assert len(calls) == 1, "must be ONE call for the whole book, not one per position"
    assert len(calls[0]) == 7
    assert len(out) == 7
    mark = out[("solana", "M0")]
    assert mark["price"] == 1.5
    assert mark["liquidity"] == 900.0 and mark["mcap"] == 500_000.0   # ticker moves in tandem
    assert mark["source"] == fl.MID_SOURCE            # auditable: never mislabelled exact_route
    assert mark["quality"] == "live"                  # stays subject to spike quarantine


def test_evm_positions_are_not_priced_by_the_mid_lane():
    calls = []
    out = fl._batched_mid_marks([_pos(1, "0xabc", net="4663")],
                                batch_fn=lambda k: calls.append(k) or {})
    assert out == {} and calls == []                  # 4663 has no DexScreener pair at all


def test_a_failed_batch_yields_no_marks_never_a_substituted_price():
    def boom(keys):
        raise RuntimeError("dexscreener down")
    assert fl._batched_mid_marks([_pos(1, "M1")], batch_fn=boom) == {}


def test_zero_and_garbage_prices_are_dropped_not_written_as_zero():
    out = fl._batched_mid_marks(
        [_pos(1, "A"), _pos(2, "B"), _pos(3, "C")],
        batch_fn=lambda keys: {("solana", "A"): {"price": 0.0},
                               ("solana", "B"): {"price": None},
                               ("solana", "C"): {"price": 2.0}})
    assert set(out) == {("solana", "C")}              # a fabricated mark is worse than a missing one


def test_exact_route_wins_when_a_chain_has_both(monkeypatch):
    """An EVM position with a working preview must keep its EXACT price — the cheap mid must never
    overwrite it. Ordering is load-bearing: mid marks are laid down first, exact overwrites."""
    monkeypatch.setattr(fl, "mid_scope", lambda net: True)          # force overlap
    monkeypatch.setattr(fl, "in_scope", lambda net: True)
    monkeypatch.setattr(fl, "open_real_positions", lambda conn, limit=None: [_pos(1, "M1")])
    # PATCH THE PATH THAT ACTUALLY RUNS. This used to patch `executable_unit_price`, which
    # `refresh_open_positions` no longer calls: 8d502db8 inlined a parallelised copy of that logic
    # (its own `m.open_qty` read at ~line 568, then `_preview_unit_price` over a ThreadPoolExecutor)
    # and left the serial function behind. Production calls it from NOWHERE now — only tests do —
    # so the patch had no effect, the real code ran, and `conn=None` blew up inside `open_qty`.
    # The duplication is the real defect and it is deliberately NOT fixed here: whether the batch
    # should call `executable_unit_price` or the function should be deleted is a behaviour decision
    # for the author of 8d502db8, not something to guess at inside a test repair.
    monkeypatch.setattr(fl.m, "open_qty", lambda _conn, _pid: 10.0)
    monkeypatch.setattr(fl, "_preview_unit_price", lambda _preview, _qty: 9.99)
    out = fl.refresh_open_positions(
        None, {}, context_fn=lambda c, p: {"preview_exit_fn": lambda p: {"expected_usdc": 99.9}},
        mid_batch_fn=lambda keys: {k: {"price": 1.11} for k in keys})
    assert out[("solana", "M1")]["price"] == 9.99
    assert out[("solana", "M1")]["source"] == fl.SOURCE


def test_fresh_swap_mark_replaces_dex_in_one_batch_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fl, "open_real_positions", lambda _conn, limit=None: [_pos(1, "M1")]
    )
    cache = _live_cache()

    def batch(keys):
        calls.append(list(keys))
        return {
            ("solana", "M1"): {
                "price": 10.0,
                "liquidity": 900.0,
                "market_cap": 500.0,
            },
            ("solana", WSOL): {"price": 200.0},
        }

    out = fl.refresh_open_positions(
        None,
        {},
        mid_batch_fn=batch,
        swap_mark_cache=cache,
        now_ts_fn=lambda: 101.0,
        now_iso_fn=lambda: "2026-08-12T12:00:01+00:00",
    )
    assert len(calls) == 1
    assert set(calls[0]) == {("solana", "M1"), ("solana", WSOL)}
    assert out[("solana", "M1")]["price"] == pytest.approx(30.0)
    assert out[("solana", "M1")]["source"] == "fastlane_sol_swap"


def test_stale_swap_mark_uses_dex_fallback(monkeypatch):
    monkeypatch.setattr(
        fl, "open_real_positions", lambda _conn, limit=None: [_pos(1, "M1")]
    )
    out = fl.refresh_open_positions(
        None,
        {},
        swap_mark_cache=_live_cache(),
        now_ts_fn=lambda: 106.0,
        mid_batch_fn=lambda keys: {key: {"price": 10.0} for key in keys},
    )
    assert out[("solana", "M1")]["price"] == 10.0
    assert out[("solana", "M1")]["source"] == fl.MID_SOURCE


def test_repeated_dex_values_keep_the_first_observation_time():
    state = fl.MidObservationState()
    first = state.observe(
        ("solana", "M1"),
        {"price": 1.0, "liquidity": 2.0, "mcap": 3.0},
        "t1",
    )
    second = state.observe(
        ("solana", "M1"),
        {"price": 1.0, "liquidity": 2.0, "mcap": 3.0},
        "t2",
    )
    changed = state.observe(
        ("solana", "M1"),
        {"price": 1.1, "liquidity": 2.0, "mcap": 3.0},
        "t3",
    )
    assert (first, second, changed) == ("t1", "t1", "t3")


# --- the two preview-caching policies are different ON PURPOSE ----------------------------------
# The runner reads PRESENCE, not value, so caching a miss means "attempted, do not re-preview" and
# omitting the key means "not attempted". The batch caches misses (it feeds the WO-4 exact-quote-
# miss counter, which stops a provider outage manufacturing a false unsellable CUT);
# `executable_unit_price` does not. Pinning both so neither can be "simplified" into the other.

def test_the_batch_caches_an_unpriced_preview_as_attempted(monkeypatch):
    monkeypatch.setattr(fl, "in_scope", lambda net: True)
    monkeypatch.setattr(fl, "open_real_positions", lambda conn, limit=None: [_pos(1, "M1")])
    monkeypatch.setattr(fl.m, "open_qty", lambda _c, _p: 10.0)
    seen = {}
    out = fl.refresh_open_positions(
        None, {}, context_fn=lambda c, p: {"preview_exit_fn": lambda p: None},
        preview_by_position=seen)
    assert out == {}
    # nothing priced, but the position must read as ATTEMPTED to the sweep
    assert 1 in seen and seen[1] is None, seen


def test_executable_unit_price_does_not_cache_an_unpriced_preview(monkeypatch):
    monkeypatch.setattr(fl.m, "open_qty", lambda _c, _p: 10.0)
    seen = {}
    px = fl.executable_unit_price(
        None, {"id": 1, "mint": "M1"},
        preview_fn=lambda _p: {"expected_usdc": 0.0},        # preview arrives, prices to nothing
        preview_by_position=seen)
    assert px is None
    assert seen == {}, seen


def test_executable_unit_price_still_caches_a_priced_preview(monkeypatch):
    monkeypatch.setattr(fl.m, "open_qty", lambda _c, _p: 10.0)
    seen = {}
    px = fl.executable_unit_price(
        None, {"id": 1, "mint": "M1"},
        preview_fn=lambda _p: {"expected_usdc": 100.0},
        preview_by_position=seen)
    assert px == 10.0
    assert seen == {1: {"expected_usdc": 100.0}}, seen


def test_default_fastlane_mid_uses_funded_provider_capacity(monkeypatch):
    from sniper.execution import solana_quote as q
    roles = []
    def batch(keys, *, funded=False, consumer=None):
        roles.append((funded, consumer))
        return {}
    monkeypatch.setattr(q, "dexscreener_stats_batch_keyed", batch)
    assert fl._batched_mid_marks([_pos(1, "M1")]) == {}
    assert roles == [(True, "X2")], "funded capacity, tagged as the fast lane (DexScreener out, X2)"
