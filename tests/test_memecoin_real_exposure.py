import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, mint, notional, mode="real"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=notional, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET execution_mode=? WHERE id=?", (mode, pid)); c.commit()


def test_real_open_snapshot_excludes_paper():
    c = _c()
    _open_real(c, "M1", 10.0)
    _open_real(c, "M2", 10.0)
    _open_real(c, "M3", 10.0, mode="paper")     # paper -> excluded
    snap = m.real_open_snapshot(c)
    assert snap["count"] == 2
    assert abs(snap["notional"] - 20.0) < 1e-9
    assert snap["mints"] == {"M1", "M2"}
    assert snap["coin_ids"] == {("solana", "M1"), ("solana", "M2")}
    assert m.real_open_coin_ids(c) == snap["coin_ids"]
