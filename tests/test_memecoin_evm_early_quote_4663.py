"""The 4663 early quote, alive again (2026-10-15, operator "fill fastest"; Alpha go, build 2).

26 of 29 4663 early quotes (10-12..10-15) never launched their ladder:
  * 17 balance_stale_generation: the entry's OWN invalidation advanced the spend generation ~0.2 ms
    before the early worker's fresh balance read returned (part A, MEMECOIN_EVM_EARLY_SIZING_OWN_GEN);
  * 6 still sizing: the gate cache was older than the 300 s sizing window on a quiet chain, so the
    worker paid a fresh ~280 ms read (part B, GateBalanceWarmer);
  * 3 sized from cache in 7 ms but stopped before the ladder: the quote-token price memo was older
    than 1 s, and only 56 ran a price warmer (part C, MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS).
Part D: the early ladder carries the source-route hint, or the entry would wait on an unhinted one."""
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

CHAIN = "4663"


def _pre(generation):
    pre = object.__new__(R._EntryPreReads)
    pre.chain, pre.generation = CHAIN, generation
    return pre


@pytest.fixture
def gen(monkeypatch):
    monkeypatch.setattr(R, "_gate_balance_generation", {CHAIN: 7})
    monkeypatch.setattr(R, "_gate_balance_cache", {})
    monkeypatch.delenv("MEMECOIN_EVM_EARLY_SIZING_OWN_GEN", raising=False)
    return R._gate_balance_generation


def _own_box():
    box = {}

    def note(g, at):
        box["own"] = (CHAIN, g, at)
    return box, note, (lambda: box.get("own"))


# --- part A: the attempt's own advance, and nothing else ----------------------------------------

def test_the_attempts_own_advance_alone_is_not_stale(gen):
    pre = _pre(7)
    box, note, own = _own_box()
    R._gate_balance_invalidate(CHAIN, on_advance=note)
    facts = {}
    assert pre.stale(evidence=facts, own_fn=own) is False
    assert facts == {"pre_generation": 7, "current_generation": 8, "own_generation_only": True}
    assert pre.stale() is True                                  # without own_fn: today's rule


def test_another_spend_before_or_after_the_own_one_stays_stale(gen):
    box, note, own = _own_box()
    pre = _pre(7)
    R._gate_balance_invalidate(CHAIN)                            # another attempt's spend
    R._gate_balance_invalidate(CHAIN, on_advance=note)           # then ours: gen 9 = read + 2
    assert pre.stale(own_fn=own) is True
    pre2 = _pre(9)
    R._gate_balance_invalidate(CHAIN, on_advance=note)           # ours: 10
    R._gate_balance_invalidate(CHAIN)                            # then another: 11
    assert pre2.stale(own_fn=own) is True


def test_another_attempts_single_spend_is_stale_and_another_chain_never_counts(gen):
    pre = _pre(7)
    R._gate_balance_invalidate(CHAIN)
    assert pre.stale(own_fn=lambda: None) is True
    assert pre.stale(own_fn=lambda: ("56", 8, 0.0)) is True
    assert pre.stale(own_fn=lambda: (CHAIN, 8, 0.0)) is False


def test_the_knob_off_restores_the_old_rule(gen, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_EARLY_SIZING_OWN_GEN", "0")
    pre = _pre(7)
    box, note, own = _own_box()
    R._gate_balance_invalidate(CHAIN, on_advance=note)
    assert pre.stale(own_fn=own) is True


def test_the_own_advance_is_recorded_under_the_generation_lock(gen):
    seen = {}

    def note(g, at):
        seen["locked"] = R._gate_balance_lock.locked()
        seen["gen"] = g
    assert R._gate_balance_invalidate(CHAIN, on_advance=note)[0] == 8
    assert seen == {"locked": True, "gen": 8}

    def broken(_g, _at):
        raise RuntimeError("boom")
    assert R._gate_balance_invalidate(CHAIN, on_advance=broken)[0] == 9   # the advance stands


# --- part B: the warmer -----------------------------------------------------------------------

def _ctx(quote=150.0, native=0.08, **kw):
    return dict(usdc_fn=lambda: quote, native_fn=lambda: native, **kw)


def test_the_warmer_fills_the_cache_for_its_chains_only(gen, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", CHAIN)
    contexts = {CHAIN: _ctx(), "8453": _ctx(), "56": _ctx(recovery_only=True)}
    w = R.GateBalanceWarmer(lambda: contexts)
    w.run_once()
    assert set(R._gate_balance_cache) == {CHAIN}
    assert R._gate_balance_cache[CHAIN]["quote"][0] == 150.0
    st = w.status()
    assert (st["passes"], st["reads_ok"], st["reads_failed"], st["chains"]) == (1, 2, 0, [CHAIN])


def test_the_warmed_cache_sizes_the_early_quote_from_cache(gen, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", CHAIN)
    R.GateBalanceWarmer(lambda: {CHAIN: _ctx()}).run_once()
    assert R._early_cached_balances(CHAIN, _pre(7)) == (150.0, 0.08)


def test_a_spend_during_the_warm_read_writes_nothing(gen, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", CHAIN)

    def spending_read():
        R._gate_balance_invalidate(CHAIN)          # our entry began while the read was in flight
        return 150.0
    R.GateBalanceWarmer(lambda: {CHAIN: dict(_ctx(), usdc_fn=spending_read)}).run_once()
    assert "quote" not in R._gate_balance_cache.get(CHAIN, {})


def test_a_failed_warm_read_writes_nothing_and_is_counted(gen, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", CHAIN)

    def boom():
        raise TimeoutError("read timed out")
    w = R.GateBalanceWarmer(lambda: {CHAIN: dict(_ctx(), usdc_fn=boom)})
    w.run_once()
    assert "quote" not in R._gate_balance_cache.get(CHAIN, {})
    st = w.status()
    assert st["reads_failed"] == 1 and st["last_error"].startswith(CHAIN + ":quote=")


def test_the_warmer_reads_nothing_while_its_knob_is_empty(gen, monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", raising=False)
    calls = []
    w = R.GateBalanceWarmer(lambda: calls.append(1) or {CHAIN: _ctx()})
    w.run_once()
    assert calls == [] and R._gate_balance_cache == {}


@pytest.mark.parametrize("raw, want", [("60", 60.0), ("120", 120.0), ("x", 60.0), ("1", 60.0),
                                       ("nan", 60.0)])
def test_the_warm_period_parses(monkeypatch, raw, want):
    monkeypatch.setenv("MEMECOIN_EVM_GATE_BALANCE_WARM_S", raw)
    assert cfg.memecoin_evm_gate_balance_warm_s() == want


# --- part C: the price warmer chains ------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [(None, {"56"}), ("56,4663", {"56", "4663"}), ("", set()),
                                       ("56,x", {"56"})])
def test_the_price_warmer_chains_default_to_today(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS", raw)
    assert cfg.memecoin_evm_quote_price_warmer_chains() == want


def test_the_leg_starts_the_price_warmer_by_the_knob_not_by_chain_56():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "sniper/services/memecoin_evm_leg.py").read_text()
    assert 'if chain == "56" and http is None:\n        ex["start_quote_price_warmer_fn"]()' not in src
    assert "chain in cfg.memecoin_evm_quote_price_warmer_chains() and http is None" in src


# --- part D: the early ladder carries the hint ---------------------------------------------------

class _Ref:
    def __init__(self, post):
        self.post = post

    def payload(self):
        return self.post


def _start(tmp_path, monkeypatch, post):
    from sniper.execution import evm_source_route as src
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_pools", {})
    monkeypatch.setattr(R, "_early_evm_quote_clip", lambda *a, **k: (175.0, None, {}))
    monkeypatch.setattr(R, "_early_evm_safety_fn", lambda *a, **k: None)
    seen, done = [], threading.Event()

    def prepare(mint, usd, *, on_started=None, route_hint=None):
        seen.append(route_hint)
        done.set()
        return None
    prepare.early_deadline = True
    db = tmp_path / "l.db"
    conn = sqlite3.connect(db)
    ev = SimpleNamespace(mint="0x" + "22" * 20, signal_source="onchain_watch", delivery_lane=None,
                         evm_chase_reference=_Ref(post))
    trace = SimpleNamespace(mark_substage=lambda *a, **k: True)
    monkeypatch.setattr(R, "context_for", lambda contexts, chain: {"prepare_entry_quote_fn": prepare})
    monkeypatch.setattr("sniper.persistence.db_core._connection_db_path", lambda c: str(db))
    quote = R._start_early_evm_quote(conn, ev, {}, trace, object(), CHAIN)
    assert quote is not None and done.wait(2.0)
    return seen


POOL = "0x" + "ab" * 32


def test_the_early_ladder_carries_the_source_pool(tmp_path, monkeypatch):
    assert _start(tmp_path, monkeypatch, {"venue": "v4", "pool": POOL}) == [POOL]


def test_no_hint_leaves_the_early_ladder_unchanged(tmp_path, monkeypatch):
    assert _start(tmp_path, monkeypatch, {"reason": "split_route"}) == [None]
