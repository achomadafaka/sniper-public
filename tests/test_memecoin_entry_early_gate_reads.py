"""The Solana gate's bankroll read and the token-safety mint read start beside the DexScreener GET.

2026-09-27, Alpha GO ("B"). Measured on the Solana general path (2026-09-26T21:30Z..09-27T20:5xZ):
the GET starts at detection and costs p50 233 / p90 493 ms, and only AFTER it did the entry read
the bankroll (p50 44 / p90 87 ms) and, after the reservation, the mint account (22 / 43 ms). Both
are independent of the GET, so they now start with it and the gate joins them. Flag
MEMECOIN_ENTRY_EARLY_GATE_READS (default on; 0 = the old order).

The same change closes a cache hazard it would otherwise widen: a balance read in flight when a
spend invalidates the gate cache must not write its pre-spend value into the cache afterwards.
"""
import threading
import time
from types import SimpleNamespace

import pytest

import sniper.services.memecoin_volume_writer  # noqa: F401  the hook imports it lazily (~130 ms)
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

GET_S = 0.30
READ_S = 0.15


def _mint_account():
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {"parsed": {"info": {"supply": "1000000000000000", "decimals": 6,
                                         "freezeAuthority": None, "mintAuthority": None}}}}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(R, "_gate_balance_cache", {})
    monkeypatch.setattr(R, "_gate_balance_generation", {}, raising=False)


# --- the cache hazard ---------------------------------------------------------------------------

def _read_during(invalidate):
    release, started, out = threading.Event(), threading.Event(), {}

    def usdc():
        started.set()
        release.wait(5)
        return 500.0

    worker = threading.Thread(target=lambda: out.setdefault(
        "r", R._gate_balances("solana", usdc, lambda: 1.0, now_ts=100.0)))
    worker.start()
    assert started.wait(5)
    if invalidate:
        R._gate_balance_invalidate("solana")          # a spend begins while the read is in flight
    release.set()
    worker.join(5)
    return out["r"]


def test_a_spend_during_an_in_flight_read_leaves_the_cache_unwritten():
    quote, _native, _cached = _read_during(invalidate=True)
    assert quote == 500.0, "the attempt still sees its own read"
    assert "solana" not in R._gate_balance_cache, "a pre-spend value was cached after the spend"


def test_without_a_spend_the_read_is_cached_as_before():
    _read_during(invalidate=False)
    assert R._gate_balance_cache["solana"]["quote"] == (500.0, 100.0)


# --- the hook: reads start with the GET -----------------------------------------------------------

def _hook(monkeypatch, *, flag, mint_result=None):
    from test_memecoin_entry_identity import FakeClock, NOW, _arm, _conn, _event, _solana_context
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", flag, raising=False)
    _arm(monkeypatch)
    t, marks, seen = {}, [], {}
    ctx = _solana_context({})

    def usdc():
        t.setdefault("usdc_start", time.monotonic())
        time.sleep(READ_S)
        return 300.0

    def mint_read(mint):
        t.setdefault("mint_start", time.monotonic())
        time.sleep(READ_S)
        return mint_result if mint_result is not None else (_mint_account(), None)

    def stats(mint, network_id=None):
        t["get_start"] = time.monotonic()
        time.sleep(GET_S)
        t["get_end"] = time.monotonic()
        return {}

    def entry(conn, ev, **kw):
        t["entry"] = time.monotonic()
        seen["ev"] = ev
        return {"status": "rejected", "reason": "stub"}

    ctx.update(usdc_fn=usdc, mint_read_fn=mint_read)
    monkeypatch.setattr(R, "real_entry", entry)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    trace = SimpleNamespace(
        attempt_id="attempt-b", mark=lambda *a, **k: True, finish=lambda *a, **k: True,
        mark_substage=lambda name, **kw: marks.append({"name": name, **kw}) or True)
    R.real_entry_hook(_conn(), _event(network_id="solana", mint="So1Mint"), {"solana": ctx},
                      now_fn=lambda: NOW, trade_size=10.0, max_age_s=120, stats_fn=stats,
                      trace=trace, trace_clock=FakeClock())
    return t, marks, seen


def test_the_bankroll_and_mint_reads_run_inside_the_get(monkeypatch):
    t, marks, seen = _hook(monkeypatch, flag=True)
    assert t["usdc_start"] < t["get_end"] and t["mint_start"] < t["get_end"], t
    assert t["entry"] - t["get_start"] < GET_S + READ_S, "the reads still ran after the GET"
    gate = [x for x in marks if x["name"] == "timing_gate_balance_read"]
    assert gate and gate[0]["early"] is True, marks
    assert gate[0]["duration_ms"] < READ_S * 1000 / 2, "the gate blocked on a read already done"
    assert seen["ev"].mint_account_read == (_mint_account(), None), "safety judges the early read"


def test_the_rollback_flag_keeps_the_reads_after_the_get(monkeypatch):
    t, marks, seen = _hook(monkeypatch, flag=False)
    assert t["usdc_start"] > t["get_end"], t
    assert "mint_start" not in t, "the mint read belongs to the leg's safety gate again"
    assert not hasattr(seen["ev"], "mint_account_read")


def test_a_failed_early_mint_read_is_not_handed_to_safety(monkeypatch):
    from sniper.execution import solana_token_safety as TS
    _t, _marks, seen = _hook(monkeypatch, flag=True, mint_result=(None, TS.READ_FAILED))
    assert not hasattr(seen["ev"], "mint_account_read"), "a failure must be re-read, not replayed"


def test_reads_bound_to_one_attempt_are_never_served_to_another():
    ev = SimpleNamespace(mint="So1Mint")
    ctx = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0}
    first, second = object(), object()
    pre = R._start_entry_prereads(ev, {"solana": ctx}, first)
    assert pre is not None and R._entry_prereads_for(ev, first) is pre
    assert R._entry_prereads_for(ev, second) is None
    assert R._start_entry_prereads(ev, {"solana": ctx}, None) is None, "no trace, no binding"


def test_early_mint_read_is_bound_to_the_requested_mint():
    account = _mint_account()
    pre = R._EntryPreReads({"usdc_fn": lambda: 300., "sol_fn": lambda: 1.,
                           "mint_read_fn": lambda mint: (account, None)},
                          "So1Mint", object(), read_mint=True)
    assert pre.mint_read("So1Mint") == (account, None)
    assert pre.mint_read("another") is R._READ_TIMED_OUT


def test_the_band_consumes_the_early_mint_read_once_on_the_raw_path(monkeypatch):
    """No event mcap (the on-chain lane): the GET runs in make_real_hook and the on-chain band
    reads the mint. That read is now the early one; the band waits on it and never reads again."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_BAND_MCAP_BASIS", "onchain")
    book = sqlite3.connect(":memory:")
    book.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(book)
    m.add_wallet(book, "u", "tq")
    m.set_wallet_real_approved(book, "u", True, chains=["solana"], force=True)
    t, reads, seen = {}, [], {}

    def mint_read(mint):
        reads.append(mint)
        t.setdefault("mint_start", time.monotonic())
        time.sleep(READ_S)
        return (_mint_account(), None)

    def stats(mint, network_id=None):
        time.sleep(GET_S)
        t["get_end"] = time.monotonic()
        return {"market_cap": 40_000.0, "price": 3.0e-05, "liquidity": 9000.0}

    def spy(conn, ev, contexts, **kw):
        seen.update(kw, ev=ev)
        return {"status": "filled"}

    monkeypatch.setattr(R, "real_entry_hook", spy)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    ctx = {"mint_read_fn": mint_read, "usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0}
    hook = R.make_real_hook(None, {"solana": ctx}, now_fn=lambda: "2026-09-27T21:00:00+00:00",
                            trade_size=10.0, max_age_s=None, stats_fn=stats)
    ev = SimpleNamespace(side="buy", source_user_id="u", mint="FRESH", their_market_cap=None,
                         their_price=2.0e-05)
    hook(book, ev)
    assert reads == ["FRESH"], "the band read the mint a second time"
    assert t["mint_start"] < t["get_end"], t
    assert seen["classification_context"]["mcap_basis"] == "onchain_source_fill"
    assert seen["ev"].mint_account_read == (_mint_account(), None)
    book.close()
