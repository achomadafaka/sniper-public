"""Fresh exact-size cache evidence avoids native network work on healthy Jupiter routes."""
from solders.keypair import Keypair
import pytest

from sniper.execution import pumpswap_sell as sell
from sniper.execution import solana_real_seams as seams
from sniper.services import memecoin_live_marks as marks
from sniper.services import memecoin_native_exit_rung as rung
from sniper.services import memecoin_real_runner as runner
from sniper.services.memecoin_solana_swap_marks import PoolBinding, SolanaSwapMarkCache
from sniper.settings import memecoin as cfg
from test_pumpswap_native_exit import MINT, POOL, _native_quote


NOW = "2026-10-17T18:00:00+00:00"


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT", 25.0)
    monkeypatch.setattr(rung, "_VIRTUAL_QUOTE", {POOL: 0})
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(marks, "onchain_quote_usd", lambda: {rung.WSOL_MINT: (100.0, NOW)})
    cache = SolanaSwapMarkCache()
    cache.record({"kind": "sol_sync", "pool": POOL, "block_no": 100,
                  "amount0": "1000", "amount1": "20000000000", "decimals0": 6, "decimals1": 9},
                 PoolBinding(MINT, POOL, "coin", "quote", rung.WSOL_MINT),
                 now_ts=100.0, observed_at=NOW)
    calls = []
    monkeypatch.setattr(seams, "bound_pumpswap_pool", lambda *a, **k: calls.append("binding") or POOL)
    monkeypatch.setattr(sell, "build_pumpswap_native_exit", lambda *a, **k: calls.append("build") or _native_quote())
    monkeypatch.setattr(seams, "token_balance_raw", lambda *a, **k: 5)
    def http(*a, **k):
        raise AssertionError("no speculative HTTP allowed")
    executor = seams.build_real_solana_executor(keypair=Keypair(), rpc_url="https://custody.invalid",
                                               http=http, usdc_fn=lambda: 0, token_fn=lambda _m: 5)
    yield executor, cache, calls
    cache._onchain_pool.shutdown(wait=True)


def _quote(out="9800000", amount="5"):
    return {"inputMint": MINT, "inAmount": amount, "outputMint": rung.USDC_MINT,
            "outAmount": out, "otherAmountThreshold": out, "routePlan": []}


def _wire(executor, cache):
    assert runner.configure_native_exit_hint(executor, cache, now_fn=lambda: NOW)


def test_healthy_actual_cache_hint_skips_every_speculative_native_read_and_build(case, monkeypatch):
    executor, cache, calls = case
    _wire(executor, cache)
    quote = _quote()
    monkeypatch.setattr(seams, "jupiter_exit_quote", lambda *a, **k: quote)
    monkeypatch.setattr(seams, "_typed_jupiter_exit_quote", lambda *a, **k: (quote, None))
    def forbidden(*a, **k):
        raise AssertionError("a cache-only hint must not start a thread or call RPC/Jupiter")
    monkeypatch.setattr(rung.threading, "Thread", forbidden)
    monkeypatch.setattr(seams, "_rpc_call", forbidden)
    monkeypatch.setattr(seams, "_jup_call", forbidden)
    assert executor["exit_quote_fn"](MINT, 5, 3, atomic=5, position_id=42) is quote
    assert executor["preview_exit_fn"]({"id": 42, "mint": MINT}).expected_usdc == 9.8
    assert calls == []


@pytest.mark.parametrize("kind", ["blind", "divergent", "large_bag_divergent"])
def test_blind_and_exact_divergent_evidence_still_try_native(case, monkeypatch, kind):
    executor, cache, calls = case
    quote = None if kind == "blind" else _quote("1000000" if kind == "divergent" else "9800000")
    amount = 500 if kind == "large_bag_divergent" else 5
    if kind == "large_bag_divergent":
        quote["inAmount"] = str(amount)
    if kind != "blind":
        _wire(executor, cache)
    monkeypatch.setattr(seams, "jupiter_exit_quote", lambda *a, **k: quote)
    chosen = executor["exit_quote_fn"](MINT, amount, 3, atomic=amount, position_id=42)
    assert calls == ["binding", "build"]
    # The cache only decides whether to try: the final executable floor still picks the route.
    if kind in ("blind", "divergent"):
        assert chosen["_venue"] == sell.VENUE
    else:
        assert chosen is quote


@pytest.mark.parametrize("kind", ["changed_size", "stale", "stale_usd", "unknown_identity", "wrong_quote_size", "unwired", "throws"])
def test_existing_jupiter_with_unknown_native_evidence_has_no_speculative_native_work(case, monkeypatch, kind):
    executor, cache, calls = case
    quote = _quote("1000000")
    amount = 500 if kind == "changed_size" else 5
    if kind == "wrong_quote_size":
        quote["inAmount"] = "6"
    elif kind == "stale":
        cache.invalidate_prices(now_ts=100.0)
    elif kind == "stale_usd":
        monkeypatch.setattr(marks, "onchain_quote_usd", lambda: {rung.WSOL_MINT: (100.0, "2026-10-16T18:00:00+00:00")})
    elif kind == "unknown_identity":
        rung._VIRTUAL_QUOTE.clear()
    if kind == "throws":
        def failed_hint(*_args):
            raise RuntimeError("unavailable")
        executor["set_native_exit_hint_fn"](failed_hint)
    elif kind != "unwired":
        _wire(executor, cache)
    monkeypatch.setattr(seams, "jupiter_exit_quote", lambda *a, **k: quote)
    assert executor["exit_quote_fn"](MINT, amount, 3, atomic=amount, position_id=42) is quote
    assert calls == []


def test_known_foreign_pool_skips_a_native_build_without_treating_unknown_as_foreign(case, monkeypatch):
    executor, cache, calls = case
    rung._VIRTUAL_QUOTE[POOL] = rung.NOT_PUMPSWAP
    _wire(executor, cache)
    quote = _quote()
    monkeypatch.setattr(seams, "jupiter_exit_quote", lambda *a, **k: quote)
    assert executor["exit_quote_fn"](MINT, 5, 3, atomic=5) is quote
    assert calls == []
