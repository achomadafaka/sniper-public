"""The EVM gate's balance reads start beside the market-data GET (2026-09-27, Alpha GO "A").

Measured on 4663 (55 first-attempt funded fills since 09-20): decision -> ladder p50 842 ms. The
market-data GET is ~211 ms; after every gate, `_gate_balances` (WETH + ETH/USD || native) sat inside
a 319 ms gap before the reservation. Those reads depend on the chain and the wallet, never on the
coin, so they now start with the GET, bound to the attempt's trace, as Beta's Solana reads do. Flag
MEMECOIN_ENTRY_EARLY_GATE_READS_EVM (default on; 0 = the old order).

THE GENERATION GUARD: an early read that predates one of our own spends on the chain (every spend
calls `_gate_balance_invalidate` first) is discarded and read again. The accepted race is the one
that exists today: another attempt's fill landing between this read and this reservation.
"""
import time
from types import SimpleNamespace

import pytest

import sniper.services.memecoin_volume_writer  # noqa: F401  the hook imports it lazily
from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

GET_S = 0.30
READ_S = 0.15


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(R, "_gate_balance_cache", {})
    monkeypatch.setattr(R, "_gate_balance_generation", {}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", True, raising=False)


def _ctx(t, calls, quote_values=(300.0,)):
    values = list(quote_values)

    def usdc():
        calls.append("usdc")
        t.setdefault("usdc_start", time.monotonic())
        time.sleep(READ_S)
        return values.pop(0) if len(values) > 1 else values[0]

    def native():
        calls.append("native")
        time.sleep(READ_S)
        return 1.0

    return {"usdc_fn": usdc, "native_fn": native,
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "execute_fn": lambda *a, **k: {"status": "unknown"}, "kill_path": None}


def _trace():
    return SimpleNamespace(attempt_id="attempt-a", mark=lambda *a, **k: True,
                           finish=lambda *a, **k: True, mark_substage=lambda *a, **k: True)


def _entry_fn(monkeypatch, ctx, seen):
    def fake_entry(conn, ev, **kw):
        seen.update(usdc=kw["usdc_balance"], native=kw["native_balance"])
        return {"status": "unknown"}
    monkeypatch.setattr(leg, "evm_real_entry", fake_entry)
    return R._make_evm_entry_fn({"4663": ctx})


def _call(fn, ev, trace):
    return fn(None, ev, route="4663", day="2026-09-27", now_iso="2026-09-27T22:00:00+00:00",
              trade_size=10.0, trace=trace)


def test_the_early_read_is_bound_to_its_attempt_and_chain():
    t, calls = {}, []
    ev, first, second = SimpleNamespace(mint="0xabc"), _trace(), _trace()
    pre = R._start_entry_prereads(ev, {"4663": _ctx(t, calls)}, first, chain="4663")
    assert pre is not None and R._entry_prereads_for(ev, first) is pre
    assert R._entry_prereads_for(ev, second) is None
    quote, native, cached, read_ms = pre.balances()
    assert (quote, native, cached) == (300.0, 1.0, False) and read_ms >= READ_S * 1000 * 0.9


def test_the_rollback_flag_starts_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", False, raising=False)
    ev = SimpleNamespace(mint="0xabc")
    assert R._start_entry_prereads(ev, {"4663": _ctx({}, [])}, _trace(), chain="4663") is None


def test_the_gate_consumes_the_early_read_instead_of_reading_again(monkeypatch):
    t, calls, seen = {}, [], {}
    ctx = _ctx(t, calls)
    ev, trace = SimpleNamespace(mint="0xabc", source_user_id="u1"), _trace()
    fn = _entry_fn(monkeypatch, ctx, seen)
    R._start_entry_prereads(ev, {"4663": ctx}, trace, chain="4663")
    time.sleep(READ_S * 1.5)                       # the gates run while the read completes
    began = time.monotonic()
    _call(fn, ev, trace)
    assert seen == {"usdc": 300.0, "native": 1.0}
    assert sorted(calls) == ["native", "usdc"], "the gate read the balances a second time"
    assert time.monotonic() - began < READ_S / 2, "the gate blocked on a read already done"


def test_one_of_our_spends_after_the_early_read_forces_a_fresh_read(monkeypatch):
    t, calls, seen = {}, [], {}
    ctx = _ctx(t, calls, quote_values=(300.0, 250.0))
    ev, trace = SimpleNamespace(mint="0xabc", source_user_id="u1"), _trace()
    fn = _entry_fn(monkeypatch, ctx, seen)
    pre = R._start_entry_prereads(ev, {"4663": ctx}, trace, chain="4663")
    pre.balances()                                 # the early read has finished
    R._gate_balance_invalidate("4663")             # another attempt spends on this chain
    _call(fn, ev, trace)
    assert seen["usdc"] == 250.0, "a pre-spend balance reached the reservation"
    assert calls.count("usdc") == 2


def test_another_chains_spend_does_not_discard_the_read(monkeypatch):
    t, calls, seen = {}, [], {}
    ctx = _ctx(t, calls)
    ev, trace = SimpleNamespace(mint="0xabc", source_user_id="u1"), _trace()
    fn = _entry_fn(monkeypatch, ctx, seen)
    R._start_entry_prereads(ev, {"4663": ctx}, trace, chain="4663").balances()
    R._gate_balance_invalidate("56")
    _call(fn, ev, trace)
    assert calls.count("usdc") == 1


def test_the_evm_reads_run_inside_the_get(monkeypatch):
    """Through the real hook: the balance read starts before the market-data GET has returned."""
    from test_memecoin_entry_identity import FakeClock, NOW, _approve, _arm, _conn, _event
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    t, calls = {}, []
    ctx = _ctx(t, calls)

    def stats(mint, network_id=None):
        t["get_start"] = time.monotonic()
        time.sleep(GET_S)
        t["get_end"] = time.monotonic()
        return {}

    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected",
                                                                "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    hook = R.make_real_hook(None, {"4663": ctx}, now_fn=lambda: NOW, trade_size=10.0,
                            max_age_s=120, stats_fn=stats, trace_clock=FakeClock())
    hook(conn, _event(network_id="4663"))
    assert "usdc_start" in t and t["usdc_start"] < t["get_end"], t


def test_an_event_without_a_source_mcap_starts_its_reads_with_the_copy_path_get(monkeypatch):
    """66384 (4663, 2026-09-28): an event with their_market_cap=0 takes the copy path's live-mcap
    fallback, whose GET started early reads for Solana only; the hook then had cached stats and
    started none, so the gate read the balances in line (279 ms blocking). 27 of 51 4663 fills
    since 09-21 arrive that way."""
    from test_memecoin_entry_identity import FakeClock, NOW, _approve, _arm, _conn, _event
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    t, calls = {}, []
    ctx = _ctx(t, calls)

    def stats(mint, network_id=None):
        t.setdefault("get_start", time.monotonic())
        time.sleep(GET_S)
        t.setdefault("get_end", time.monotonic())
        return {"price": 0.25, "market_cap": 50_000.0, "liquidity": 20_000.0}

    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected",
                                                                "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    hook = R.make_real_hook(None, {"4663": ctx}, now_fn=lambda: NOW, trade_size=10.0,
                            max_age_s=120, stats_fn=stats, trace_clock=FakeClock())
    hook(conn, _event(network_id="4663", market_cap=0.0))
    assert "get_end" in t, "the copy path's live-mcap GET did not run: the fixture misses the path"
    assert "usdc_start" in t and t["usdc_start"] < t["get_end"], t


@pytest.mark.parametrize('native',[1.,0.,None])
def test_existing_gate_mark_retains_native_units_without_another_read(monkeypatch,native):
    calls,marks,seen=[],[],{}
    ctx={'usdc_fn':lambda:calls.append('quote') or 300.,
         'native_fn':lambda:calls.append('native') or native,
         'token_safety_fn':lambda *a,**k:(True,'ok'),
         'execute_fn':lambda *a,**k:{'status':'unknown'},'kill_path':None}
    trace=_trace()
    trace.mark_substage=lambda name,**facts:marks.append((name,facts))
    result=_call(_entry_fn(monkeypatch,ctx,seen),SimpleNamespace(mint='0xabc',source_user_id='u1'),trace)
    assert result['status']=='unknown' and sorted(calls)==['native','quote']
    mark=next(facts for name,facts in marks if name=='timing_gate_balance_read')
    assert mark['network_id']=='4663' and mark['quote_balance_usd']==300.
    assert mark['native_balance_native_units']==native
    assert mark['gas_price_wei'] is None
