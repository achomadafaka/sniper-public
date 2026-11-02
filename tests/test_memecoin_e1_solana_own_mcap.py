"""E1 on Solana: DexScreener never sets a band with the fallback off, and nothing we cannot price
skips the ceiling (spec 2026-09-28-memecoin-e1-solana-own-mcap-design.md).

Since the on-chain basis went live (2026-09-27 09:00Z) DexScreener decided 3 of 551 Solana bands.
With MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=0 those coins go to the route stage: our own entry quote at
the funded clip times the mint supply the prereads read, and that quote is handed to the executor so
the entry pays one quote. MEMECOIN_SOLANA_MCAP_UNRESOLVED=refuse keeps a coin no own stage prices
from banding assumed_nano and skipping the $250k ceiling.
"""
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg
from test_memecoin_band_mcap_basis import _ev, _hook, _mint_account, book  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _knobs(monkeypatch, *, fallback, unresolved="assume", route=None):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_DEX_MCAP_FALLBACK", fallback)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MCAP_UNRESOLVED", unresolved)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MCAP_ASSUME_MICRO", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(RR, "_default_solana_mcap_fn", lambda: (lambda mint: route))


# ---------------------------------------------------------------------------- the band stages

@pytest.mark.parametrize("basis", ["onchain", "dexscreener"])
def test_fallback_off_never_takes_dexscreener_market_cap(book, monkeypatch, basis):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis=basis, ds_mcap=40_000.0)
    _knobs(monkeypatch, fallback=False, route=77_000.0)
    ev = _ev()
    ev.their_price = None                       # the on-chain basis cannot price it
    hook(book, ev)
    ctx = seen["classification_context"]
    # No trace in this harness, so the new stage returns None and executable_mcap prices it:
    # the label says which route priced the band (Gamma L2).
    assert ctx["mcap_basis"] == "route" and ctx["effective_mcap"] == 77_000.0
    assert ctx["effective_mcap"] != 40_000.0


def test_the_clip_route_stage_is_labelled_route_clip(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain", ds_mcap=40_000.0)
    _knobs(monkeypatch, fallback=False, route=77_000.0)
    monkeypatch.setattr(RR, "_route_band_mcap", lambda *a, **k: 31_000.0)
    ev = _ev()
    ev.their_price = None
    hook(book, ev)
    ctx = seen["classification_context"]
    assert ctx["mcap_basis"] == "route_clip" and ctx["effective_mcap"] == 31_000.0


@pytest.mark.parametrize("fallback", [True, False])
def test_a_shadow_decision_never_reaches_the_funded_quote_seam(book, monkeypatch, fallback):
    """Alpha on Gamma M1: neither route stage quotes for a J7 shadow decision, in either mode."""
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain", ds_mcap=None)
    _knobs(monkeypatch, fallback=fallback, unresolved="assume")
    monkeypatch.setattr(RR, "_default_solana_mcap_fn",
                        lambda: (lambda mint: pytest.fail("shadow quoted executable_mcap")))
    ev = _ev(delivery_lane=RR.J7_SHADOW_LANE)
    ev.their_price = None
    hook(book, ev)
    assert seen["classification_context"]["mcap_basis"] == "assumed"


def test_refuse_refuses_a_coin_no_own_stage_prices(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain")
    _knobs(monkeypatch, fallback=False, unresolved="refuse", route=None)
    ev = _ev()
    ev.their_price = None
    res = hook(book, ev)
    assert res["status"] == "skipped_mcap" and res["reason"] == "mcap_unresolved"
    assert "classification_context" not in seen, "a refused coin never reaches the entry hook"


def test_assume_bands_it_assumed_nano_as_today(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain")
    _knobs(monkeypatch, fallback=False, unresolved="assume", route=None)
    ev = _ev()
    ev.their_price = None
    hook(book, ev)
    ctx = seen["classification_context"]
    assert ctx["assumed_micro"] is True and ctx["mcap_basis"] == "assumed"


def test_the_defaults_keep_todays_dexscreener_fallback_and_never_run_the_new_stage(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain", ds_mcap=40_000.0)
    monkeypatch.setattr(RR, "_route_band_mcap", lambda *a, **k: pytest.fail("default ran E1"))
    assert cfg.MEMECOIN_SOLANA_DEX_MCAP_FALLBACK is True
    assert cfg.MEMECOIN_SOLANA_MCAP_UNRESOLVED == "assume"
    ev = _ev()
    ev.their_price = None
    hook(book, ev)
    ctx = seen["classification_context"]
    assert ctx["mcap_basis"] == "dexscreener_fallback" and ctx["effective_mcap"] == 40_000.0


# ---------------------------------------------------------------------------- the knobs

@pytest.mark.parametrize("raw, on", [(None, True), ("1", True), ("0", False), ("off", False),
                                     ("False", False), ("typo", True)])
def test_the_fallback_knob_turns_off_only_on_an_explicit_off(raw, on):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHONPATH": str(ROOT)}
    if raw is not None:
        env["MEMECOIN_SOLANA_DEX_MCAP_FALLBACK"] = raw
    out = subprocess.run([sys.executable, "-c", "import sniper.settings.memecoin as c; "
                          "print(c.MEMECOIN_SOLANA_DEX_MCAP_FALLBACK)"], env=env, cwd=str(ROOT),
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == str(on), out.stderr


def test_only_the_unsafe_pair_is_a_problem():
    assert cfg.solana_mcap_knob_problem(False, "assume")
    assert cfg.solana_mcap_knob_problem(False, "refuse") is None
    assert cfg.solana_mcap_knob_problem(True, "assume") is None
    assert cfg.solana_mcap_unresolved(None) == "assume"
    assert cfg.solana_mcap_unresolved(" Refuse ") == "refuse"
    assert cfg.solana_mcap_unresolved("nope") == "refuse", "malformed must never skip the ceiling"


@pytest.mark.parametrize("fallback, unresolved, warns", [("0", "assume", True), ("0", "refuse", False),
                                                        ("1", "assume", False)])
def test_the_unsafe_pair_warns_at_start(fallback, unresolved, warns):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "PYTHONPATH": str(ROOT), "MEMECOIN_SOLANA_DEX_MCAP_FALLBACK": fallback,
           "MEMECOIN_SOLANA_MCAP_UNRESOLVED": unresolved}
    out = subprocess.run([sys.executable, "-c", "import sniper.settings.memecoin"], env=env,
                         cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert ("WIDENS admission" in out.stderr) is warns, out.stderr


# ---------------------------------------------------------------------------- the route stage

class _Pre:
    def __init__(self, read):
        self.read = read

    def mint_read(self, mint):
        return self.read


def _stage(tmp_path, monkeypatch, *, read, quote, lane=None, clip=(120.0, None)):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(RR, "_early_quote_clip", lambda ev, pre, db_path: clip)
    calls = []

    def quote_fn(mint, usd):
        calls.append((mint, usd))
        return quote
    ev = types.SimpleNamespace(mint="FRESH", delivery_lane=lane)
    trace = types.SimpleNamespace(attempt_id="a1", mark_substage=lambda *a, **k: None)
    got = RR._route_band_mcap(conn, ev, {"solana": {"entry_quote_fn": quote_fn}}, trace, _Pre(read))
    return got, ev, calls


def test_the_route_stage_prices_supply_times_clip_over_out_and_hands_the_quote_over(tmp_path, monkeypatch):
    quote = {"outAmount": "4000000000000"}           # 4e6 tokens at 6 decimals for $120
    got, ev, calls = _stage(tmp_path, monkeypatch, read=(_mint_account(), None), quote=quote)
    assert got == pytest.approx(1e15 * 120.0 / 4e12)   # 1e9 tokens x $3e-5 = $30,000
    assert calls == [("FRESH", 120.0)], "one quote, at the funded clip"
    assert ev.entry_early_quote.take(120.0) == quote, "the executor gets this quote, not a second"


def test_a_shadow_decision_quotes_nothing(tmp_path, monkeypatch):
    """Gamma M1: shadow keeps today's executable_mcap path; the new stage never quotes for it."""
    got, ev, calls = _stage(tmp_path, monkeypatch, read=(_mint_account(), None),
                            quote={"outAmount": "4000000000000"}, lane=RR.J7_SHADOW_LANE)
    assert got is None and calls == [] and not hasattr(ev, "entry_early_quote")


def test_an_earlier_attempts_early_quote_does_not_block_the_stage(tmp_path, monkeypatch):
    """Gamma L1: a retry carries the first attempt's taken object; only THIS trace's object counts."""
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(RR, "_early_quote_clip", lambda ev, pre, db_path: (120.0, None))
    trace = types.SimpleNamespace(attempt_id="a2", mark_substage=lambda *a, **k: None)
    old = types.SimpleNamespace(trace=types.SimpleNamespace(attempt_id="a1"))
    ev = types.SimpleNamespace(mint="FRESH", delivery_lane=None, entry_early_quote=old)
    contexts = {"solana": {"entry_quote_fn": lambda mint, usd: {"outAmount": "4000000000000"}}}
    assert RR._route_band_mcap(conn, ev, contexts, trace, _Pre((_mint_account(), None)))
    assert ev.entry_early_quote is not old and ev.entry_early_quote.trace is trace
    same = ev.entry_early_quote
    assert RR._route_band_mcap(conn, ev, contexts, trace, _Pre((_mint_account(), None))) is None
    assert ev.entry_early_quote is same, "this trace already holds one: no second quote"


@pytest.mark.parametrize("read, quote, clip", [
    (RR._READ_TIMED_OUT, {"outAmount": "1"}, (120.0, None)),     # the mint read has not finished
    ((None, "read_failed"), {"outAmount": "1"}, (120.0, None)),  # it failed
    ((_mint_account(), None), {}, (120.0, None)),               # no outAmount
    ((_mint_account(), None), {"outAmount": "1"}, (None, "risk:daily loss")),   # sizing refused
])
def test_anything_missing_is_no_route_mcap_and_hands_nothing(tmp_path, monkeypatch, read, quote, clip):
    got, ev, calls = _stage(tmp_path, monkeypatch, read=read, quote=quote, clip=clip)
    assert got is None and not hasattr(ev, "entry_early_quote")
    if read is RR._READ_TIMED_OUT or read == (None, "read_failed"):
        assert calls == [], "no quote without a supply"
