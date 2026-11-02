"""An exit must not sell another position's tokens.

Exits sell the WHOLE wallet balance, which is correct while one funded position owns the mint. On
2026-08-13 two reservations raced into EUB1eZBt with one-per-coin turned off, both bought $32.01,
and the first position's take-profit sold BOTH buys' tokens while being costed against one:
$33.04 booked against $1.03 true, and the second reservation orphaned and blocked deploys.

The guard is deliberately narrow. When the mint is NOT shared the whole-balance read is kept
exactly as it was — it is also what absorbs dust and fee-on-transfer remainders, and narrowing it
everywhere would strand those.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, pid, mint, *, is_open=1, mode="real", net="solana"):
    c.execute("INSERT INTO memecoin_positions (id, mint, entry_price, entry_notional, is_open,"
              " execution_mode, network_id) VALUES (?,?,?,?,?,?,?)",
              (pid, mint, 1.0, 10.0, is_open, mode, net))
    c.commit()


def test_a_lone_position_is_not_counted_as_shared():
    c = _conn()
    _pos(c, 1, "M")
    assert m.shared_mint_open_real_count(c, "M", "solana") == 1


def test_two_open_funded_positions_on_one_mint_are_shared():
    c = _conn()
    _pos(c, 1, "M"); _pos(c, 2, "M")
    assert m.shared_mint_open_real_count(c, "M", "solana") == 2


def test_closed_and_paper_rows_do_not_make_a_mint_look_shared():
    """Only OPEN FUNDED positions can compete for the same wallet balance."""
    c = _conn()
    _pos(c, 1, "M")
    _pos(c, 2, "M", is_open=0)            # closed: its tokens are already gone
    _pos(c, 3, "M", mode="paper")         # paper: holds no real tokens at all
    assert m.shared_mint_open_real_count(c, "M", "solana") == 1


def test_a_different_chain_is_a_different_balance():
    c = _conn()
    _pos(c, 1, "M"); _pos(c, 2, "M", net="4663")
    assert m.shared_mint_open_real_count(c, "M", "solana") == 1


def test_unknown_mint_and_bad_input_report_zero_rather_than_raising():
    c = _conn()
    assert m.shared_mint_open_real_count(c, "NOPE", "solana") == 0
    assert m.shared_mint_open_real_count(c, "", "solana") == 0
    assert m.shared_mint_open_real_count(c, None) == 0


@pytest.mark.parametrize("module", [
    "sniper.execution.solana_exit_executor", "sniper.execution.evm_exit_executor"])
def test_both_executors_constrain_only_when_the_mint_is_shared(module):
    import importlib, inspect
    src = inspect.getsource(importlib.import_module(module))
    assert "shared_mint_open_real_count(conn, mint, position.get(\"network_id\")) > 1" in src
    # the constraint must be a min(), never a bare share: a share LARGER than the balance means
    # someone already sold into it, and selling the balance would repeat the theft.
    assert "tokens = min(tokens, share)" in src
    # and it must apply to the FULL-close ladder only, once
    assert src.count("shared_mint_open_real_count") == 1


def test_open_qty_is_the_share_and_survives_partial_sells():
    """The share must be entered-minus-exited, or a de-risked position would try to sell tokens it
    has already sold."""
    c = _conn()
    _pos(c, 1, "M")
    m.insert_fill(c, position_id=1, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="chain", fraction=1.0, created_at="2026-08-13T00:00:00+00:00")
    assert m.open_qty(c, 1) == pytest.approx(100.0)
    m.insert_fill(c, position_id=1, kind="derisk", price=1.0, qty=40.0, notional=40.0,
                  fill_source="chain", fraction=0.4, created_at="2026-08-13T00:01:00+00:00")
    assert m.open_qty(c, 1) == pytest.approx(60.0)


def test_an_in_flight_reservation_counts_as_a_sharer():
    """A reservation that has broadcast already owns tokens in the wallet and is NOT a position
    yet, so counting positions alone missed the race this guard exists to stop: 37125 opened at
    19:13:08, reservation 382 broadcast at 19:13:38, and 37125's take-profit at 19:13:41 sold
    63,830 tokens having bought 31,584 — booked +$44.42 on what was truly -$3.69."""
    c = _conn()
    _pos(c, 1, "M")
    assert m.shared_mint_open_real_count(c, "M", "solana") == 1
    c.execute("INSERT INTO memecoin_pending_entries (id, mint, source_user_id, strategy,"
              " reserved_notional_usd, reserved_at, status, network_id)"
              " VALUES (1,'M','u','alpha_b',10.0,'2026-08-13T00:00:00+00:00','reserved','solana')")
    c.commit()
    assert m.shared_mint_open_real_count(c, "M", "solana") == 2


def test_a_settled_reservation_no_longer_counts():
    """Only 'reserved' rows hold tokens without a position. A confirmed one HAS its position (which
    the position count already sees) and a failed one bought nothing."""
    c = _conn()
    _pos(c, 1, "M")
    for i, st in ((1, "confirmed"), (2, "failed")):
        c.execute("INSERT INTO memecoin_pending_entries (id, mint, source_user_id, strategy,"
                  " reserved_notional_usd, reserved_at, status, network_id)"
                  " VALUES (?,'M','u','alpha_b',10.0,'2026-08-13T00:00:00+00:00',?,'solana')",
                  (i, st))
    c.commit()
    assert m.shared_mint_open_real_count(c, "M", "solana") == 1
