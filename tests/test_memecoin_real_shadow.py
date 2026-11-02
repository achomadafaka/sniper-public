"""real_shadow paper arm (2026-07-10): a paper arm that mirrors the LIVE real-leg exit rails
(MEMECOIN_REAL_STOP_PCT / _TP_PCT / _DERISK_PCT / _MAX_HOLD_S + mirror-on-sell) across the FULL
curated roster, so the real config is evaluated on 10-20x the data the tiny real book produces.
It reuses the EXACT real exit-decision function (alpha_b_real_exit_reason) — same knobs, so it can
never drift from the real leg. execution_mode stays 'paper'."""
import sqlite3
import types

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exits as X
from sniper.services import memecoin_copy_trader as CT
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, entry_price=1e-3, notional=30.0, mint="So1Mint", opened_at="2026-07-10T00:00:00+00:00"):
    pid = m.open_position(c, source_user_id="u1", source_handle="whale", mint=mint, ticker="X",
                          entry_price=entry_price, entry_notional=notional, entry_fill_source="dexscreener",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at=opened_at, strategy="real_shadow",
                          execution_mode="paper")
    qty = notional / entry_price
    m.insert_fill(c, position_id=pid, kind="entry", price=entry_price, qty=qty, notional=notional,
                  fill_source="dexscreener", fraction=1.0, created_at=opened_at)
    return m.get_position(c, pid)


def _pm(mint, price, liq=1e6):
    from sniper.execution.solana_quote import norm_mint
    return {norm_mint(mint): {"price": price, "liquidity": liq}}


def _rails(mp, stop=50.0, tp=40.0, derisk=0.0, hold=0.0, unquote=86400.0):
    mp.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", stop)
    mp.setattr(cfg, "MEMECOIN_REAL_TP_PCT", tp)
    mp.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", derisk)
    mp.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", hold)
    mp.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", unquote)


NOW = "2026-07-10T00:10:00+00:00"


# --- config wiring ---

def test_real_shadow_is_a_mirror_arm():
    assert "real_shadow" in CT.MIRROR_ARMS


# --- exit rails (reuse alpha_b_real_exit_reason) ---

def test_take_profit_fires_at_tp(monkeypatch):
    _rails(monkeypatch, tp=40.0)
    c = _c(); pos = _open(c, entry_price=1e-3)
    closed = X.sweep_exits(c, _pm("So1Mint", 1.45e-3), NOW)          # +45% > +40% TP
    assert any(r["exit_reason"] == "real_tp" for r in closed)
    assert m.get_position(c, pos["id"])["is_open"] == 0


def test_no_exit_below_tp(monkeypatch):
    _rails(monkeypatch, tp=40.0)
    c = _c(); pos = _open(c, entry_price=1e-3)
    closed = X.sweep_exits(c, _pm("So1Mint", 1.30e-3), NOW)          # +30% < +40%
    assert not any(r["strategy"] == "real_shadow" for r in closed)
    assert m.get_position(c, pos["id"])["is_open"] == 1              # rides


def test_catastrophe_stop_fires(monkeypatch):
    _rails(monkeypatch, stop=50.0, tp=40.0)
    c = _c(); pos = _open(c, entry_price=1e-3)
    closed = X.sweep_exits(c, _pm("So1Mint", 0.45e-3), NOW)          # -55% <= -50%
    assert any(r["exit_reason"] == "b_stop" for r in closed)


def test_no_timecap_when_disabled(monkeypatch):
    _rails(monkeypatch, hold=0.0)                                    # real config: no timecap
    c = _c(); pos = _open(c, entry_price=1e-3, opened_at="2026-07-01T00:00:00+00:00")  # 9 days old
    closed = X.sweep_exits(c, _pm("So1Mint", 1.05e-3), NOW)          # +5%, healthy
    assert not any(r["strategy"] == "real_shadow" for r in closed)   # rides forever without timecap


def test_timecap_fires_when_enabled(monkeypatch):
    _rails(monkeypatch, hold=43200.0)                               # 12h cap on
    c = _c(); pos = _open(c, entry_price=1e-3, opened_at="2026-07-01T00:00:00+00:00")
    closed = X.sweep_exits(c, _pm("So1Mint", 1.05e-3), NOW)
    assert any(r["exit_reason"] == "b_timecap" for r in closed)


# --- de-risk: one-shot partial, position stays open, both legs summed at close ---

def test_derisk_partial_keeps_open_and_marks(monkeypatch):
    _rails(monkeypatch, tp=0.0, derisk=100.0)                       # sell entry at +100%, ride rest
    c = _c(); pos = _open(c, entry_price=1e-3, notional=30.0)
    closed = X.sweep_exits(c, _pm("So1Mint", 2.0e-3), NOW)          # +100%
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["derisked_at"]                   # OPEN + marked
    # derisk emits a marker but does NOT fully close (no b_stop/real_tp/timecap close this sweep)
    assert [r["exit_reason"] for r in closed if r["strategy"] == "real_shadow"] == ["derisk_partial"]
    # a partial_exit fill was booked (recovering ~the entry notional)
    f = c.execute("SELECT COUNT(*), COALESCE(SUM(notional),0) FROM memecoin_fills "
                  "WHERE position_id=? AND kind='partial_exit'", (pos["id"],)).fetchone()
    assert f[0] == 1 and f[1] > 20                                  # recovered most of the $30 entry


def test_derisk_is_one_shot(monkeypatch):
    _rails(monkeypatch, tp=0.0, derisk=100.0)      # tp off so derisk (not full TP) fires at +100%
    c = _c(); pos = _open(c, entry_price=1e-3, notional=30.0)
    X.sweep_exits(c, _pm("So1Mint", 2.0e-3), NOW)                   # derisk fires
    X.sweep_exits(c, _pm("So1Mint", 3.0e-3), "2026-07-10T00:20:00+00:00")  # +200%, must NOT re-derisk
    n = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='partial_exit'",
                  (pos["id"],)).fetchone()[0]
    assert n == 1


def test_derisked_winner_realized_sums_both_legs(monkeypatch):
    _rails(monkeypatch, stop=50.0, derisk=100.0)
    c = _c(); pos = _open(c, entry_price=1e-3, notional=30.0)
    X.sweep_exits(c, _pm("So1Mint", 2.0e-3), NOW)                   # derisk at +100% (banks ~entry)
    # trader sells -> mirror close of the remainder
    ev = types.SimpleNamespace(source_user_id="u1", mint="So1Mint", their_price=2.0e-3, side="sell",
                               fomo_event_id="e1", ticker="X")
    CT.copy_event_to_arms(c, ev, 30.0, "2026-07-10T00:30:00+00:00", arms=["real_shadow"],
                          stats_fn=lambda mint: {"price": 2.0e-3, "liquidity": 1e6})
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 0
    assert p["realized_pnl"] > 20                                   # banked entry + rode remainder up


# --- mirror-on-sell ---

def test_mirror_close_on_trader_sell(monkeypatch):
    c = _c(); pos = _open(c, entry_price=1e-3, notional=30.0)
    ev = types.SimpleNamespace(source_user_id="u1", mint="So1Mint", their_price=1.5e-3, side="sell",
                               fomo_event_id="e1", ticker="X")
    out = CT.copy_event_to_arms(c, ev, 30.0, NOW, arms=["real_shadow"],
                                exit_quote_fn=lambda mint, net, qty: (1.5e-3, "route_quote"))
    assert out["real_shadow"] == "exit"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 0 and p["exit_reason"] == "b_mirror" and p["realized_pnl"] > 0
