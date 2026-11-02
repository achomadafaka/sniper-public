"""Do not re-enter a coin whose last real position lost.

Operator 2026-08-23, after KIRK: position 44596 lost $15.27 on b_timecap at 15:54:43, and 43
minutes later 44598 re-entered the SAME mint and lost $24.24 the same way -- $39.51 on a coin the
book had already been beaten by. MEMECOIN_REAL_REENTRY_COOLDOWN_S exists but is 0, and a time
window is the wrong shape anyway: it expires, and it blocks winners as well as losers.

Measured over 309 closed real positions: an entry whose coin's previous close LOST returned -12.5%
(n=15, 33% wins) against -4.8% for everything else (n=294, 48% wins). Bootstrap -7.6pp, 95% CI
[-27.3, +13.3], P(worse)=0.77 -- directional, not proof, and pinned here as a behavioural rule.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg

SOL = "So1Mint"
OTHER = "So2Mint"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_REENTRY_COOLDOWN_S", 0.0)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _closed(conn, mint, pnl, closed_at, network_id="solana"):
    conn.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, "
        "entry_notional, is_open, opened_at, closed_at, realized_pnl, execution_mode, "
        "network_id, strategy) VALUES (?,?,?,?,?,0,?,?,?, 'real', ?, 'alpha_b')",
        ("u1", mint, "T", 1.0, 20.0, closed_at, closed_at, pnl, network_id))
    conn.commit()


def test_a_coin_whose_last_close_lost_is_blocked():
    conn = _conn()
    _closed(conn, SOL, -15.27, "2026-08-23T15:54:43+00:00")
    assert m.coin_identity("solana", SOL) in m.losing_real_coin_ids(conn)


def test_a_coin_whose_last_close_won_is_not_blocked():
    conn = _conn()
    _closed(conn, SOL, +4.90, "2026-08-23T15:54:43+00:00")
    assert m.losing_real_coin_ids(conn) == frozenset()


def test_only_the_MOST_RECENT_close_counts():
    """A loss followed by a win is not blocked; a win followed by a loss is."""
    conn = _conn()
    _closed(conn, SOL, -10.0, "2026-08-23T10:00:00+00:00")
    _closed(conn, SOL, +10.0, "2026-08-23T11:00:00+00:00")     # recovered -> allowed
    _closed(conn, OTHER, +10.0, "2026-08-23T10:00:00+00:00")
    _closed(conn, OTHER, -10.0, "2026-08-23T11:00:00+00:00")   # then lost -> blocked
    ids = m.losing_real_coin_ids(conn)
    assert m.coin_identity("solana", SOL) not in ids
    assert m.coin_identity("solana", OTHER) in ids


def test_an_open_position_does_not_count_as_a_loss():
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, "
        "entry_notional, is_open, opened_at, execution_mode, network_id, strategy) "
        "VALUES ('u1',?,'T',1.0,20.0,1,'2026-08-23T10:00:00+00:00','real','solana','alpha_b')",
        (SOL,))
    conn.commit()
    assert m.losing_real_coin_ids(conn) == frozenset()


def test_a_paper_loss_never_blocks_a_real_entry():
    """The rule is about OUR funded book, not the paper arms."""
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, "
        "entry_notional, is_open, opened_at, closed_at, realized_pnl, execution_mode, "
        "network_id, strategy) VALUES ('u1',?,'T',1.0,100.0,0,'2026-08-23T10:00:00+00:00',"
        "'2026-08-23T10:05:00+00:00',-50.0,'paper','solana','control')", (SOL,))
    conn.commit()
    assert m.losing_real_coin_ids(conn) == frozenset()


def test_the_gate_refuses_the_reentry():
    conn = _conn()
    _closed(conn, SOL, -15.27, "2026-08-23T15:54:43+00:00")
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=300.0, sol_balance=1.0, day="2026-08-23")
    d = risk.memecoin_entry_allowed(ctx, mint=SOL, trade_size=ctx.trade_size_usd)
    assert not d.allowed
    assert "losing re-entry" in d.reason


def test_the_gate_allows_a_coin_we_never_traded():
    conn = _conn()
    _closed(conn, SOL, -15.27, "2026-08-23T15:54:43+00:00")
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=300.0, sol_balance=1.0, day="2026-08-23")
    d = risk.memecoin_entry_allowed(ctx, mint=OTHER, trade_size=ctx.trade_size_usd)
    assert d.allowed, d.reason


def test_the_switch_disables_it(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", False)
    conn = _conn()
    _closed(conn, SOL, -15.27, "2026-08-23T15:54:43+00:00")
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=300.0, sol_balance=1.0, day="2026-08-23")
    assert ctx.losing_coin_ids == frozenset()
    assert risk.memecoin_entry_allowed(ctx, mint=SOL, trade_size=ctx.trade_size_usd).allowed


def test_the_KIRK_case_end_to_end():
    """The exact sequence that motivated the rule."""
    conn = _conn()
    KIRK = "2ZVKH8FpNvmqaFhaH3Apgw1rctNzmfyVRSz4SaFFpump"
    _closed(conn, KIRK, -15.272718, "2026-08-23T15:54:43.273988+00:00")
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=300.0, sol_balance=1.0, day="2026-08-23")
    d = risk.memecoin_entry_allowed(ctx, mint=KIRK, trade_size=ctx.trade_size_usd)
    assert not d.allowed, "the second KIRK entry would still be admitted"
    # assert the REASON: without this the test passes on any refusal at all, and it did exactly
    # that on "real trading disabled" before the fixture set MEMECOIN_REAL_ENABLED.
    assert "losing re-entry" in d.reason, d.reason
