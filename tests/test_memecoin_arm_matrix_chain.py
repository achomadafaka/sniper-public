"""The wallet x arm matrix is scoped to ONE chain and never pools (spec 2026-07-30)."""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, pid, uid, net, arm, origin, pnl):
    c.execute(
        "INSERT INTO memecoin_positions(id,source_user_id,source_handle,mint,network_id,"
        "execution_mode,is_open,strategy,origin_event_id,realized_pnl,closed_at,"
        "entry_price,entry_notional,entry_fill_source)"
        " VALUES(?,?,?,?,?,'paper',0,?,?,?,'2026-07-30T00:00:00+00:00',0.001,100.0,'route_quote')",
        (pid, uid, "h-" + uid, "mint" + str(pid), net, arm, origin, pnl))
    # Entry + executable exit fills: the matrix requires positive entry quantity and an observed
    # exit, matching the report's measurable-outcome test (2026-08-04). Production positions
    # always carry both; a fixture without them is not a reachable shape.
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-07-30T00:00:00+00:00")
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=0.0011, qty=100_000.0,
                  notional=110.0, fill_source="dexscreener", fraction=1.0,
                  created_at="2026-07-30T00:00:00+00:00")


def test_matrix_returns_only_the_requested_chain():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "solana", "control", "e1", 10.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 20.0)
    _pos(c, 3, "u1", "4663", "control", "e2", -5.0)
    _pos(c, 4, "u1", "4663", "alpha_b", "e2", -7.0)
    c.commit()

    assert [w["total_pnl"] for w in m.trader_arm_matrix(c, chain="solana")["wallets"]] == [30.0]
    assert [w["total_pnl"] for w in m.trader_arm_matrix(c, chain="robin")["wallets"]] == [-12.0]


def test_aliases_address_the_same_rows():
    """'robin' and '4663' must not select different row sets."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "4663", "control", "e1", 3.0)
    _pos(c, 2, "u1", "4663", "alpha_b", "e1", 4.0)
    c.commit()
    assert (m.trader_arm_matrix(c, chain="robin")["wallets"]
            == m.trader_arm_matrix(c, chain="4663")["wallets"])


def test_signals_counts_distinct_origin_events_not_positions():
    """Two arms on one event is ONE signal, not two."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "solana", "control", "e1", 1.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 2.0)
    c.commit()
    w = m.trader_arm_matrix(c, chain="solana")["wallets"][0]
    assert w["signals"] == 1
    assert w["total_closed"] == 2


def test_dropped_wallets_are_reported_not_silently_omitted():
    """A single-arm wallet is not comparable, but must still be visible."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.add_wallet(c, "u2", "h2")
    _pos(c, 1, "u1", "solana", "control", "e1", 1.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 2.0)
    _pos(c, 3, "u2", "solana", "control", "e2", 9.0)      # only one arm
    c.commit()
    out = m.trader_arm_matrix(c, chain="solana")
    # Superseded 2026-07-30: non-comparable wallets are no longer withheld from `wallets` —
    # they appear with zeros and a status, so the roster is complete on the surface. The
    # `dropped` list stays as the machine-readable reason.
    by_uid = {w["source_user_id"]: w for w in out["wallets"]}
    assert by_uid["u1"]["status"] == "comparable"
    assert by_uid["u2"]["status"] == "not_comparable" and by_uid["u2"]["total_pnl"] == 0.0
    assert out["dropped"] == [
        {"source_user_id": "u2", "handle": "h-u2", "reason": "only 1 arm", "signals": 1}]
    assert out["copied_wallets"] == 2


def test_unknown_chain_raises_rather_than_pooling():
    c = _conn()
    with pytest.raises(ValueError, match="never pooled"):
        m.trader_arm_matrix(c, chain="all")
    with pytest.raises(ValueError):
        m.trader_arm_matrix(c, chain="dogecoin")


def test_fomo_priced_rows_never_enter_the_ranking():
    """Ranking on raw PnL would have put a fabricated $976,791 at the top of 4663."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "4663", "control", "e1", 1.0)
    _pos(c, 2, "u1", "4663", "alpha_b", "e1", 2.0)
    _pos(c, 3, "u1", "4663", "control", "e2", 976_791.0)
    _pos(c, 4, "u1", "4663", "alpha_b", "e2", 138_747.0)
    c.execute("INSERT INTO memecoin_fills(position_id,fill_source,kind)"
              " VALUES(3,'fomo','entry'),(4,'fomo','entry')")
    c.commit()
    w = m.trader_arm_matrix(c, chain="robin")["wallets"][0]
    assert w["total_pnl"] == 3.0
    assert w["signals"] == 1


def test_every_roster_wallet_appears_even_with_no_signal():
    """The roster IS the evidence surface: a wallet silent on this chain shows zeros,
    not an absence the operator has to infer."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.add_wallet(c, "u2", "h2")          # never copied anywhere
    m.add_wallet(c, "u3", "h3")
    c.execute("UPDATE memecoin_wallets SET excluded=1 WHERE source_user_id='u3'")
    _pos(c, 1, "u1", "solana", "control", "e1", 5.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 7.0)
    c.commit()

    out = m.trader_arm_matrix(c, chain="solana")
    by_uid = {w["source_user_id"]: w for w in out["wallets"]}
    assert by_uid["u1"]["status"] == "comparable" and by_uid["u1"]["total_pnl"] == 12.0
    assert by_uid["u2"]["status"] == "no_signal"
    assert by_uid["u2"]["signals"] == 0 and by_uid["u2"]["total_pnl"] == 0.0
    assert "u3" not in by_uid, "excluded wallets stay out"


def test_non_comparable_wallets_are_listed_with_zeros_not_dropped():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.add_wallet(c, "u2", "h2")
    _pos(c, 1, "u1", "solana", "control", "e1", 5.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 7.0)
    _pos(c, 3, "u2", "solana", "control", "e2", 9.0)      # single arm
    c.commit()
    by_uid = {w["source_user_id"]: w for w in m.trader_arm_matrix(c, chain="solana")["wallets"]}
    assert by_uid["u2"]["status"] == "not_comparable"
    assert by_uid["u2"]["signals"] == 1 and by_uid["u2"]["total_pnl"] == 0.0
