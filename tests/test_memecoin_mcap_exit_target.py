"""Operator mcap-band take-profit target (2026-07-13). The runner's rails are %-based (TP/stop/mirror);
this adds a per-position MARKET-CAP band that real_exit_sweep full-sells when sell-value mcap reaches
the lower bound. 'sell TripleT between 22.5M and 24M' -> set_exit_target(pid, 22.5e6, 24e6), fires at
>= 22.5e6. Precedence over the %-rails; marks filled on close; at most one active target per position."""
import sqlite3

from sniper.execution.solana_quote import norm_mint
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, mint="MintTT", entry=0.02):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="TT",
                           entry_price=entry, entry_notional=46.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-13T00:00:00Z",
                           strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                           network_id="solana", entry_mcap=20e6)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=46.0 / entry,
                  notional=46.0, fill_source="chain", fraction=1,
                  created_at="2026-07-13T00:00:00Z")
    return pid


def _ctx(calls, expected=50.0):
    return {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                       {"status": "closed"})[1],
            "preview_exit_fn": lambda pos: {"expected_usdc": expected}}


# --- helpers ---

def test_set_replace_and_fill_target():
    c = _c(); pid = _open_real(c)
    m.set_exit_target(c, pid, 22.5e6, 24e6, now_iso="t0")
    t = m.active_exit_target(c, pid)
    assert t["mcap_low"] == 22.5e6 and t["mcap_high"] == 24e6 and t["status"] == "active"
    # replacing cancels the prior -> exactly one active target
    m.set_exit_target(c, pid, 20e6, 21e6, now_iso="t1")
    assert m.active_exit_target(c, pid)["mcap_low"] == 20e6
    n = c.execute("SELECT COUNT(*) FROM memecoin_exit_targets WHERE position_id=? AND status='active'",
                  (pid,)).fetchone()[0]
    assert n == 1
    m.mark_exit_target_filled(c, pid, "t2")
    assert m.active_exit_target(c, pid) is None


# --- sweep integration ---

def test_fires_when_mcap_reaches_lower_bound():
    c = _c(); pid = _open_real(c, mint="MintTT", entry=0.02)
    m.set_exit_target(c, pid, 22.5e6, 24e6, now_iso="t0")
    calls = []
    pmap = {norm_mint("MintTT"): {"price": 0.0225, "market_cap": 22.6e6, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": _ctx(calls, 51.98)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-13T00:05:00Z")
    assert calls == [("MintTT", "mcap_target")]
    row = c.execute("SELECT status FROM memecoin_exit_targets WHERE position_id=?", (pid,)).fetchone()
    assert row["status"] == "filled"                 # marked filled on close


def test_does_not_fire_below_band():
    c = _c(); pid = _open_real(c, mint="MintTT", entry=0.02)
    m.set_exit_target(c, pid, 22.5e6, 24e6, now_iso="t0")
    calls = []
    # mcap 21M < 22.5M low AND price healthy (+5%) -> no mcap fire and no %-rail fire
    pmap = {norm_mint("MintTT"): {"price": 0.021, "market_cap": 21.0e6, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": _ctx(calls, 48.3)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-13T00:05:00Z")
    assert calls == []
    assert m.active_exit_target(c, pid)["status"] == "active"   # still armed


def test_target_takes_precedence_over_pct_rails():
    # a +12.5% move the %-TP (+60%) would HOLD still fires the operator mcap target
    c = _c(); pid = _open_real(c, mint="MintTT", entry=0.02)
    m.set_exit_target(c, pid, 22.5e6, 24e6, now_iso="t0")
    calls = []
    pmap = {norm_mint("MintTT"): {"price": 0.0225, "market_cap": 23.0e6, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": _ctx(calls, 51.75)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-13T00:05:00Z")
    assert calls == [("MintTT", "mcap_target")]


def test_no_target_leaves_rails_untouched():
    # position with NO target and a healthy price -> no exit (rails unchanged, no crash on missing target)
    c = _c(); _open_real(c, mint="MintTT", entry=0.02)
    calls = []
    pmap = {norm_mint("MintTT"): {"price": 0.021, "market_cap": 21.0e6, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": _ctx(calls)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-13T00:05:00Z")
    assert calls == []


# --- derived-mcap fallback (2026-07-17: pos 6605 — targets dead when feed lacks market_cap) ---

def test_target_fires_via_entry_ratio_derived_mcap():
    """Stats feed has price but NO market_cap and the marks tape is empty (the exact live state
    that killed the 6605 $1M target): cap derives from sell unit price * entry_mcap/entry_price."""
    c = _c()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="MintDD", ticker="DD",
                          entry_price=0.000435, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-17T00:00:00Z",
                          strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="solana", entry_mcap=646000.0)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.000435, qty=50 / 0.000435,
                  notional=50, fill_source="chain", fraction=1,
                  created_at="2026-07-17T00:00:00Z")
    m.set_exit_target(c, pid, 1_000_000.0, 1_500_000.0, now_iso="t0")
    calls = []
    # A $100 sell values the bag at twice entry, so the derived cap is $1.292M.
    pmap = {norm_mint("MintDD"): {"price": 0.0009, "liquidity": 1e9}}   # no market_cap key
    R.real_exit_sweep(c, {"solana": _ctx(calls, 100)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-17T00:05:00Z")
    assert calls == [("MintDD", "mcap_target")]


def test_no_derived_mcap_without_entry_fields_stays_closed():
    """Missing entry_mcap -> no derivation -> target must NOT fire on garbage (fail-closed kept)."""
    c = _c()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="MintEE", ticker="EE",
                          entry_price=0.000435, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-17T00:00:00Z",
                          strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="solana")
    m.set_exit_target(c, pid, 1_000_000.0, 1_500_000.0, now_iso="t0")
    calls = []
    pmap = {norm_mint("MintEE"): {"price": 0.0009, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": _ctx(calls)}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-17T00:05:00Z")
    assert calls == []
