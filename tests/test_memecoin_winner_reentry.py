"""A coin we just made money on is no longer cooled — with two bounded protections instead.

Operator order, 2026-08-27. The re-entry cooldown was blanket: any coin closed inside the window
was refused. The 07-09 study behind it never split the population by outcome, so it says nothing
about re-entering a coin we just won on. This is a policy change on operator judgment with the
ledger as the grader, and these tests pin the SHAPE of it rather than endorse the judgement.

Tested at the altitude the behaviour lives at. This morning a re-entry fix passed every builder and
reserve test and still refused the operator's buy, because the preflight hop was a layer above them
all. So the winner path is asserted through the shared builder that BOTH hops call.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as R

NOW = "2026-08-27T18:00:00+00:00"
MINT = "Coin11111111111111111111111111111111111111"
NET = "solana"


@pytest.fixture
def conn(monkeypatch):
    # Winner re-entry is chain-scoped since 2026-09-28 (MEMECOIN_REENTRY_WINNER_CHAINS, empty = no
    # chain). These tests pin the unblock itself, so they run on an in-scope chain.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_CHAINS", NET)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


def _close(c, *, pnl, closed_at, mint=MINT, exit_qty=1000.0, exit_usdc=110.0, opened_at=None):
    cur = c.execute(
        "INSERT INTO memecoin_positions(mint, network_id, execution_mode, is_open, opened_at, "
        "closed_at, realized_pnl, entry_notional) VALUES (?,?,'real',0,?,?,?,100.0)",
        (mint, NET, opened_at or closed_at, closed_at, pnl))
    c.execute("INSERT INTO memecoin_fills(position_id, kind, qty, notional, created_at) "
              "VALUES (?, 'bracket_exit', ?, ?, ?)", (cur.lastrowid, exit_qty, exit_usdc, closed_at))
    c.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ the cooldown set

def test_a_winner_is_not_cooled(conn):
    _close(conn, pnl=12.5, closed_at="2026-08-27T17:59:00")
    assert m.recently_closed_real_coin_ids(conn, NOW, 3600) == frozenset()


def test_a_loser_is_still_cooled(conn):
    _close(conn, pnl=-8.0, closed_at="2026-08-27T17:59:00")
    assert m.coin_identity(NET, MINT) in m.recently_closed_real_coin_ids(conn, NOW, 3600)


def test_breakeven_is_still_cooled(conn):
    """`> 0`, not `>= 0`. A flat close is not a win and must not unblock."""
    _close(conn, pnl=0.0, closed_at="2026-08-27T17:59:00")
    assert m.coin_identity(NET, MINT) in m.recently_closed_real_coin_ids(conn, NOW, 3600)


def test_a_null_pnl_is_still_cooled(conn):
    """Unknown is not a win. An unpriced close must never unblock by omission."""
    _close(conn, pnl=None, closed_at="2026-08-27T17:59:00")
    assert m.coin_identity(NET, MINT) in m.recently_closed_real_coin_ids(conn, NOW, 3600)


def test_the_most_recent_close_decides_not_any_win(conn):
    """A coin can close several times in one window. Letting an older win unblock a coin we have
    since LOST on would invert the gate."""
    _close(conn, pnl=50.0, closed_at="2026-08-27T17:00:00")
    _close(conn, pnl=-30.0, closed_at="2026-08-27T17:50:00")
    assert m.coin_identity(NET, MINT) in m.recently_closed_real_coin_ids(conn, NOW, 3600)


def test_a_recent_win_after_an_older_loss_unblocks(conn):
    _close(conn, pnl=-30.0, closed_at="2026-08-27T17:00:00")
    _close(conn, pnl=50.0, closed_at="2026-08-27T17:50:00")
    assert m.recently_closed_real_coin_ids(conn, NOW, 3600) == frozenset()


def test_the_flag_off_restores_the_blanket_cooldown(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False)
    _close(conn, pnl=12.5, closed_at="2026-08-27T17:59:00")
    assert m.coin_identity(NET, MINT) in m.recently_closed_real_coin_ids(conn, NOW, 3600)


# ------------------------------ BOTH HOPS: the altitude this morning's bug hid at

def test_a_winner_clears_the_context_both_hops_share(conn, monkeypatch):
    """preflight_entry and reserve_entry both build their context here, so an empty cooldown set
    for this coin is what lets a winner through BOTH — the hop that bit us and the one below it."""
    _close(conn, pnl=12.5, closed_at="2026-08-27T17:59:00")
    monkeypatch.setattr(m, "losing_real_coin_ids", lambda *a, **k: frozenset())
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_ENABLED", True)
    ctx = R.build_memecoin_risk_context(
        conn, usdc_balance=1000.0, sol_balance=1.0, day=None, kill_tripped=False)
    assert m.coin_identity(NET, MINT) not in ctx.cooldown_coin_ids
    assert R.memecoin_entry_allowed(ctx, mint=MINT, trade_size=ctx.trade_size_usd).allowed


def test_a_loser_is_still_refused_through_that_same_context(conn, monkeypatch):
    _close(conn, pnl=-8.0, closed_at="2026-08-27T17:59:00")
    # FREEZE THE CLOCK. Every sibling test passes NOW explicitly; this one goes through
    # build_memecoin_risk_context, which reads real_utc_now_iso() itself — so the fixed fixture
    # only sat inside the cooldown window on the day it was written and the test rotted at
    # NOW + cooldown. A test that reads the wall clock through the code it tests asserts the
    # calendar, not the behaviour.
    monkeypatch.setattr(R, "real_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(m, "losing_real_coin_ids", lambda *a, **k: frozenset())
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_ENABLED", True)
    ctx = R.build_memecoin_risk_context(
        conn, usdc_balance=1000.0, sol_balance=1.0, day=None, kill_tripped=False)
    d = R.memecoin_entry_allowed(ctx, mint=MINT, trade_size=ctx.trade_size_usd)
    assert not d.allowed and "cooldown" in d.reason


# ------------------------------------------------------------------ the exit price

def test_the_exit_unit_price_is_route_realized(conn):
    """proceeds / qty from the exit fill, which is what we actually received per token."""
    _close(conn, pnl=10.0, closed_at="2026-08-27T17:59:00", exit_qty=1000.0, exit_usdc=110.0)
    last = m.last_real_close_for_coin(conn, NET, MINT)
    assert last["won"] is True
    assert last["exit_unit_price"] == pytest.approx(0.11)


def test_an_undividable_exit_prices_as_unknown_not_zero(conn):
    _close(conn, pnl=10.0, closed_at="2026-08-27T17:59:00", exit_qty=0.0, exit_usdc=110.0)
    assert m.last_real_close_for_coin(conn, NET, MINT)["exit_unit_price"] is None


def test_a_coin_never_traded_has_no_last_close(conn):
    assert m.last_real_close_for_coin(conn, NET, "NeverSeen") is None


def test_recent_confirmed_real_exit_requires_matching_chain_fill(conn):
    pos = conn.execute(
        "INSERT INTO memecoin_positions(mint, network_id, execution_mode, is_open, opened_at, "
        "closed_at, close_sig) VALUES (?,?,'real',0,?,?,?)",
        (MINT, NET, "2026-08-27T17:00:00+00:00", "2026-08-27T17:59:00+00:00", "0xclose"),
    )
    conn.execute(
        "INSERT INTO memecoin_fills(position_id, kind, qty, notional, created_at, tx_sig, "
        "fill_source) VALUES (?, 'manual_close', 1000, 110, ?, '0xclose', 'chain')",
        (pos.lastrowid, "2026-08-27T17:59:00+00:00"),
    )
    conn.commit()

    assert m.recent_confirmed_real_exit_for_coin(
        conn, NET, MINT, NOW, max_age_s=120) == {
            "position_id": pos.lastrowid,
            "closed_at": "2026-08-27T17:59:00+00:00",
            "close_sig": "0xclose",
        }
    assert m.recent_confirmed_real_exit_for_coin(conn, NET, MINT, NOW, max_age_s=30) is None


# ------------------------------------------------------------------ the daily cap

def test_the_daily_cap_counts_todays_real_campaigns(conn):
    _close(conn, pnl=1.0, closed_at="2026-08-27T09:00:00", opened_at="2026-08-27T08:00:00")
    _close(conn, pnl=1.0, closed_at="2026-08-27T11:00:00", opened_at="2026-08-27T10:00:00")
    assert m.real_campaigns_today_for_coin(conn, NET, MINT, NOW) == 2


def test_yesterdays_campaigns_do_not_count(conn):
    _close(conn, pnl=1.0, closed_at="2026-08-26T23:00:00", opened_at="2026-08-26T22:00:00")
    assert m.real_campaigns_today_for_coin(conn, NET, MINT, NOW) == 0


def test_the_day_cutoff_is_built_in_python_not_sql():
    """`datetime('now', ...)` renders a SPACE separator and ' ' < 'T', so a SQL-built boundary
    reaches ~16h further back than intended (2026-08-23: 126,489 rows against a true 74,693)."""
    # code_of, not a hand-rolled strip. This function's docstring NAMES the bug it guards
    # against, so a raw scan matches the warning. My first fix dropped only the LEADING docstring,
    # which leaves every nested function's — Gamma caught that ast.unparse re-emits them at all.
    from _source_guard import code_of
    code = code_of(m.real_campaigns_today_for_coin)
    assert "datetime('now'" not in code and 'datetime("now"' not in code
    assert "strftime" in code, "the cutoff must be formatted in Python"
