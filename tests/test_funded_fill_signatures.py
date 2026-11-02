import sqlite3

from sniper.persistence import db_memecoin as M


def ledger(tmp_path):
    c = sqlite3.connect(str(tmp_path / "l.db"))
    c.execute("CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, mint TEXT, network_id TEXT, execution_mode TEXT, "
              "derisk_sig TEXT, close_sig TEXT)")
    c.execute("CREATE TABLE memecoin_fills(id INTEGER PRIMARY KEY, position_id INTEGER, kind TEXT, tx_sig TEXT)")
    c.executemany("INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?)", [
        (1, "M", "solana", "real", "DERISK1", "CLOSE1"), (2, "M", "solana", "real", None, ""),
        (3, "M", "solana", "paper", "PAPERDERISK", "PAPERCLOSE"), (4, "OTHER", "solana", "real", "X", "Y"),
        (5, "M", "base", "real", "BASESIG", None)])
    c.executemany("INSERT INTO memecoin_fills VALUES(?,?,?,?)", [
        (1, 1, "entry", "ENTRY1"), (2, 1, "partial_exit", "PART1"), (3, 2, "entry", "ENTRY2"), (4, 2, "exit", ""),
        (5, 2, "exit", None), (6, 3, "entry", "PAPERENTRY"), (7, 4, "entry", "OTHERENTRY"), (8, 5, "entry", "BASEENTRY")])
    c.commit()
    return c


def test_every_funded_fill_signature_on_the_mint_and_nothing_else(tmp_path):
    c = ledger(tmp_path)
    assert M.funded_fill_signatures(c, "M") == ("CLOSE1", "DERISK1", "ENTRY1", "ENTRY2", "PART1")
    assert M.funded_fill_signatures(c, "M", "base") == ("BASEENTRY", "BASESIG")
    assert M.funded_fill_signatures(c, "NOPE") == ()


def test_a_read_error_raises_instead_of_returning_an_empty_tuple(tmp_path):
    import pytest
    c = sqlite3.connect(str(tmp_path / "bad.db"))
    with pytest.raises(sqlite3.Error):
        M.funded_fill_signatures(c, "M")
