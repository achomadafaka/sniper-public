"""Delayed receipt time cannot postpone a confirmed 30-second pool-silence proof."""
from datetime import datetime
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_solana_swap_marks as sm
from sniper.services import memecoin_volume_death as vd
from sniper.settings import memecoin as cfg

MINT = "FzPujTgKNJVim7YYa5HYHngGtf19QwV6ACJ1zDLWpump"
POOL = "5fAjXZGX3eMkyzGmuKTSEM4CRjVQXdj1R7ge4s4TPyd8"
OPEN = "2026-10-09T22:43:01.616000+00:00"
NOW = "2026-10-09T22:43:59+00:00"
RECEIVED = "2026-10-09T22:43:31.473852+00:00"
LAST_SLOT = 455028450
NOW_TS = int(datetime.fromisoformat(NOW).timestamp())


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_MINT_WITNESS", False)
    monkeypatch.setattr(vd, "_EXACT_TAPE", {})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_position_marks "
                 "(chain_id TEXT, mint TEXT, sweep_ts TEXT, source TEXT, mid REAL)")
    conn.execute("INSERT INTO memecoin_position_marks VALUES (?,?,?,?,?)",
                 ("solana", MINT, RECEIVED, vd.SWAP_SOURCE, 1.0))
    yield conn
    conn.close()


def _decide(conn, cache, **kw):
    args = dict(now_iso=NOW, held_s=57.384, exit_s=30.0,
                min_opened_at="2026-10-01T00:00:00+00:00", stream_ok=True,
                reserve_witness_cache=cache,
                exit_quote_evidence={"route_plan": [{"inputMint": MINT, "ammKey": POOL}]})
    args.update(kw)
    return vd.armed_exit_decision(conn, {"id": 72806, "mint": MINT,
        "network_id": "solana", "opened_at": OPEN, "quote_route_target": POOL}, **args)


def _swap(cache, *, slot=LAST_SLOT, stamp=RECEIVED, pair=(1000, 500)):
    return cache.record({"kind": "sol_swap", "pool": "CoinVault", "block_no": slot,
        "amount0": "100", "amount1": "-10", "balance0": str(pair[0]),
        "balance1": str(pair[1]), "decimals0": 6, "decimals1": 9},
        sm.PoolBinding(MINT, "CoinVault", "CoinVault", "QuoteVault", sm.WSOL),
        now_ts=time.monotonic(), observed_at=stamp)


def _rpc(*, head=LAST_SLOT + 100, last_trade=NOW_TS - 31, head_time=NOW_TS):
    def call(method, params):
        if method == "getSlot":
            return head
        if method == "getBlockTime":
            return head_time
        assert method == "getSignaturesForAddress"
        assert params[1]["minContextSlot"] == head
        return [{"signature": "last_trade", "slot": min(LAST_SLOT, head),
                 "blockTime": last_trade, "err": None}]
    return call


def _cache(monkeypatch, rpc, entered=None):
    monkeypatch.setattr(sm, "time", SimpleNamespace(time=lambda: NOW_TS,
                                                   monotonic=time.monotonic))
    caller = threading.get_ident()

    def probe(_pid, _mint, start, now, _pool, minimum_slot=0):
        assert threading.get_ident() != caller
        if entered is not None:
            entered.set()
        return sm.probe_vault_activity("CoinVault", start, now, rpc_call=rpc,
                                      min_context_slot=minimum_slot)
    cache = sm.SolanaSwapMarkCache(onchain_probe=probe)
    _swap(cache)
    return cache


def _finish(cache):
    next(iter(cache._onchain_futures.values()))[1].result(timeout=1)


def test_delayed_receipt_uses_async_confirmed_window_without_changing_threshold(ledger, monkeypatch):
    entered = threading.Event()
    cache = _cache(monkeypatch, _rpc(), entered)
    try:
        assert vd.newest_swap_age_s(ledger, mint=MINT, since_iso=OPEN,
            now_iso=NOW, chain_id="solana") == pytest.approx(27.526148)
        assert _decide(ledger, cache) == (False, pytest.approx(27.526148), "onchain_pending")
        assert entered.wait(1)
        _finish(cache)
        # History proves the requested 30-second window, not an invented exact silence of 31s.
        assert _decide(ledger, cache) == (True, 30.0, "measured_onchain_still")
    finally:
        cache._onchain_pool.shutdown(wait=True)


@pytest.mark.parametrize("rpc,reason", [
    (_rpc(last_trade=NOW_TS - 20), "onchain_moving"),
    (_rpc(head=LAST_SLOT - 1), "onchain_unavailable"),
    (_rpc(head_time=NOW_TS - 4), "onchain_unavailable"),
    (_rpc(last_trade=None), "onchain_unavailable"),
    (_rpc(last_trade=NOW_TS + 1), "onchain_moving"),
])
def test_incomplete_or_moving_chain_proof_holds(ledger, monkeypatch, rpc, reason):
    cache = _cache(monkeypatch, rpc)
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[::2] == (False, reason)
    finally:
        cache._onchain_pool.shutdown(wait=True)


def test_cached_still_cannot_clear_new_same_slot_swap(ledger, monkeypatch):
    cache = _cache(monkeypatch, _rpc())
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[0] is True
        _swap(cache, stamp="2026-10-09T22:43:58+00:00", pair=(1100, 490))
        assert _decide(ledger, cache)[2] == "onchain_pending"
    finally:
        cache._onchain_pool.shutdown(wait=True)


def test_positive_swap_with_identical_post_reserves_still_invalidates_proof(ledger, monkeypatch):
    cache = _cache(monkeypatch, _rpc())
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[0] is True
        _swap(cache, slot=LAST_SLOT + 200)
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
    finally:
        cache._onchain_pool.shutdown(wait=True)


def test_identical_state_refresh_does_not_queue_proof_forever(ledger, monkeypatch):
    cache = _cache(monkeypatch, _rpc())
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        binding = sm.PoolBinding(MINT, "CoinVault", "CoinVault", "QuoteVault", sm.WSOL)
        row = {"kind": "sol_sync", "pool": "CoinVault", "block_no": LAST_SLOT + 200,
               "amount0": "1000", "amount1": "500", "decimals0": 6, "decimals1": 9}
        cache.record(row, binding, now_ts=time.monotonic(), observed_at=NOW)
        assert _decide(ledger, cache)[0] is True
        cache.record({**row, "amount0": "1100"}, binding,
                     now_ts=time.monotonic(), observed_at=NOW)
        assert _decide(ledger, cache)[2] == "onchain_pending"
    finally:
        cache._onchain_pool.shutdown(wait=True)


def test_new_activity_invalidates_inflight_proof_and_rollover_keeps_bound(ledger, monkeypatch):
    gate = threading.Event()
    started = threading.Event()
    base = _rpc()
    def rpc(method, params):
        if method == "getSlot":
            started.set()
            assert gate.wait(1)
        return base(method, params)
    cache = _cache(monkeypatch, rpc)
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        assert started.wait(1)
        _swap(cache, slot=LAST_SLOT + 200, pair=(1100, 490))
        cache.invalidate_prices()
        gate.set()
        _finish(cache)
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
    finally:
        gate.set()
        cache._onchain_pool.shutdown(wait=True)


def test_invalid_local_slot_holds_and_lower_slot_cannot_erase_high_bound(ledger, monkeypatch):
    cache = _cache(monkeypatch, _rpc())
    try:
        _swap(cache, slot=LAST_SLOT + 200, pair=(1100, 490))
        _swap(cache, slot=LAST_SLOT - 1, pair=(1200, 480))
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
        _swap(cache, slot=-1, pair=(1300, 470))
        assert _decide(ledger, cache)[0] is False
    finally:
        cache._onchain_pool.shutdown(wait=True)


@pytest.mark.parametrize("slot", [None, "bad", True, 1.2])
def test_unknown_movement_slot_invalidates_cached_still(ledger, monkeypatch, slot):
    cache = _cache(monkeypatch, _rpc())
    try:
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[0] is True
        _swap(cache, slot=slot)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
    finally:
        cache._onchain_pool.shutdown(wait=True)


@pytest.mark.parametrize("kind", ["sol_sync", "sol_curve_state", "complete"])
def test_changed_state_or_curve_with_unknown_slot_invalidates_still(ledger, monkeypatch, kind):
    cache = _cache(monkeypatch, _rpc())
    binding = sm.PoolBinding(MINT, "CoinVault", "CoinVault", "QuoteVault", sm.WSOL)
    row = {"kind": "sol_sync", "pool": "CoinVault", "block_no": LAST_SLOT,
           "amount0": "1000", "amount1": "500", "decimals0": 6, "decimals1": 9}
    if kind != "sol_sync":
        row.update(kind="sol_curve_state", virtual_sol=1_000_000_000,
                   virtual_token=2_000_000_000, real_sol=50_000_000)
    try:
        cache.record(row, binding, now_ts=time.monotonic(), observed_at=RECEIVED)
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[0] is True
        changed = {**row, "block_no": None}
        if kind == "sol_sync":
            changed["amount0"] = "1100"
        elif kind == "complete":
            changed["complete"] = True
        else:
            changed["virtual_sol"] = 1_100_000_000
        cache.record(changed, binding, now_ts=time.monotonic(), observed_at=NOW)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
        # A later valid snapshot repairs the unknown, without erasing the historical slot bound.
        repaired = {**changed, "block_no": LAST_SLOT + 200}
        cache.record(repaired, binding, now_ts=time.monotonic(), observed_at=NOW)
        assert _decide(ledger, cache)[2] == "onchain_pending"
        _finish(cache)
        assert _decide(ledger, cache)[::2] == (False, "onchain_unavailable")
    finally:
        cache._onchain_pool.shutdown(wait=True)


def test_missing_cache_and_short_hold_never_fire(ledger):
    assert _decide(ledger, None)[::2] == (False, "onchain_unavailable")
    cache = SimpleNamespace(onchain_witness=lambda **_kw: pytest.fail("proof before threshold"))
    assert _decide(ledger, cache, held_s=29.9) == (
        False, pytest.approx(27.526148), "measured")


@pytest.mark.parametrize("slot", [None, True, -1, "455028450"])
def test_invalid_minimum_slot_never_spends_a_read(slot):
    assert sm.probe_vault_activity("CoinVault", NOW_TS - 30, NOW_TS,
        min_context_slot=slot,
        rpc_call=lambda *_args: pytest.fail("invalid slot reached provider")) == "unavailable"


@pytest.mark.parametrize("proof,reason", [("moving:mint", "mint_moving"),
                                          ("pending", "onchain_pending")])
def test_mint_movement_and_pending_proof_keep_their_veto(ledger, proof, reason):
    cache = SimpleNamespace(onchain_witness=lambda **_kw: proof)
    assert _decide(ledger, cache)[::2] == (False, reason)


def test_moving_exact_quotes_veto_delayed_receipt_fallback(ledger):
    for stamp, price in (("22:43:30", 1.0), ("22:43:40", 1.01), ("22:43:58", 1.1)):
        vd.record_exact_quote(72806, quoted_at="2026-10-09T" + stamp + "+00:00", price=price)
    cache = SimpleNamespace(onchain_witness=lambda **_kw: pytest.fail("positive quote veto lost"))
    assert _decide(ledger, cache)[::2] == (False, "exact_quote_moving")
